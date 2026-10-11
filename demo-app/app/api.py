import asyncio
import json
import os
import random
import time
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import httpx
import redis.asyncio as aioredis
import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest
from psycopg_pool import AsyncConnectionPool
from pydantic import BaseModel

from app.common import QUEUE, SCHEMA, SCHEMA_LOCK, db_conninfo, env_float, env_int, setup_logging

log = setup_logging("api")

CONNINFO = db_conninfo()
SHOP_NAME = os.environ["SHOP_NAME"]
REDIS_URL = os.environ.get("REDIS_URL", "redis://redis:6379/0")
PAYMENTS_URL = os.environ.get("PAYMENTS_URL", "http://payments-gw.external.svc.cluster.local/charge")
UPLOAD_DIR = Path(os.environ.get("UPLOAD_DIR", "/uploads"))
PORT = env_int("PORT", 8000)
STARTUP_DELAY_S = env_float("STARTUP_DELAY_S", 0)
FAIL_RATE = env_float("FAIL_RATE", 0)
LEAK_MB_PER_MIN = env_float("LEAK_MB_PER_MIN", 0)
HANG_AFTER = env_int("HANG_AFTER", 0)
DB_POOL_SIZE = env_int("DB_POOL_SIZE", 5)
CACHE_MB = env_float("CACHE_MB", 64)
CACHE_WARM_S = 90

PATHS = {"/orders", "/healthz", "/ready"}
REQUESTS = Counter("api_requests_total", "HTTP requests handled by the api", ["path", "status"])
LATENCY = Histogram("api_request_seconds", "api request latency", ["path"])
QUEUE_DEPTH = Gauge("queue_depth", "jobs waiting in the redis queue")

state = SimpleNamespace(pool=None, redis=None, http=None, schema_ready=False, orders_seen=0, cache=[], leaked=[])


async def ensure_schema():
    while True:
        try:
            async with state.pool.connection(timeout=5) as conn:
                async with conn.transaction():
                    await conn.execute("SELECT pg_advisory_xact_lock(%s)", (SCHEMA_LOCK,))
                    for statement in SCHEMA:
                        await conn.execute(statement)
            state.schema_ready = True
            log.info("schema ready")
            return
        except Exception as exc:
            log.warning("schema not ready yet: %s", exc)
            await asyncio.sleep(2)


async def watch_queue():
    while True:
        try:
            QUEUE_DEPTH.set(await state.redis.llen(QUEUE))
        except Exception as exc:
            log.warning("queue depth unavailable: %s", exc)
        await asyncio.sleep(5)


async def warm_cache():
    chunk = int(CACHE_MB * 1024 * 1024 / CACHE_WARM_S)
    for _ in range(CACHE_WARM_S):
        state.cache.append(b"\x02" * chunk)
        await asyncio.sleep(1)
    log.info("cache warm: %.0f MB", CACHE_MB)


async def leak():
    chunk = int(LEAK_MB_PER_MIN * 1024 * 1024 / 60)
    while True:
        state.leaked.append(b"\x01" * chunk)
        await asyncio.sleep(1)


@asynccontextmanager
async def lifespan(_app):
    state.pool = AsyncConnectionPool(
        CONNINFO, min_size=DB_POOL_SIZE, max_size=DB_POOL_SIZE, max_lifetime=60, timeout=5, open=False
    )
    await state.pool.open(wait=False)
    state.redis = aioredis.from_url(REDIS_URL, socket_timeout=2, socket_connect_timeout=2)
    state.http = httpx.AsyncClient(timeout=2)
    tasks = [asyncio.create_task(ensure_schema()), asyncio.create_task(watch_queue())]
    if CACHE_MB > 0:
        tasks.append(asyncio.create_task(warm_cache()))
    if LEAK_MB_PER_MIN > 0:
        tasks.append(asyncio.create_task(leak()))
    log.info("api started shop=%r pool=%d", SHOP_NAME, DB_POOL_SIZE)
    yield
    for task in tasks:
        task.cancel()
    await state.http.aclose()
    await state.redis.aclose()
    await state.pool.close()


app = FastAPI(lifespan=lifespan)


@app.get("/metrics")
async def metrics():
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.middleware("http")
async def record(request: Request, call_next):
    path = request.url.path if request.url.path in PATHS else "other"
    start = time.perf_counter()
    status = 500
    try:
        response = await call_next(request)
        status = response.status_code
        return response
    finally:
        if path != "other":
            REQUESTS.labels(path, str(status)).inc()
            LATENCY.labels(path).observe(time.perf_counter() - start)


class Order(BaseModel):
    sku: str = "demo"
    qty: int = 1


@app.get("/healthz")
async def healthz():
    return {"ok": True}


@app.get("/ready")
async def ready():
    try:
        if not state.schema_ready:
            raise RuntimeError("schema not ready")
        async with state.pool.connection(timeout=2) as conn:
            await conn.execute("SELECT 1")
        await asyncio.wait_for(state.redis.ping(), 2)
    except Exception as exc:
        return JSONResponse({"ready": False, "reason": str(exc) or exc.__class__.__name__}, status_code=503)
    return {"ready": True}


@app.post("/orders")
async def create_order(order: Order):
    state.orders_seen += 1
    if HANG_AFTER and state.orders_seen > HANG_AFTER:
        await asyncio.Event().wait()
    if FAIL_RATE and random.random() < FAIL_RATE:
        raise HTTPException(status_code=500, detail="order failed")
    async with state.pool.connection() as conn:
        cur = await conn.execute(
            "INSERT INTO orders (sku, qty) VALUES (%s, %s) RETURNING id", (order.sku, order.qty)
        )
        order_id = (await cur.fetchone())[0]
    payment = await state.http.post(PAYMENTS_URL, json={"order_id": order_id, "amount": order.qty})
    if payment.status_code != 200:
        raise HTTPException(status_code=502, detail=f"payment provider returned {payment.status_code}")
    receipt = {"id": order_id, "sku": order.sku, "qty": order.qty, "shop": SHOP_NAME}
    (UPLOAD_DIR / f"receipt-{order_id % 100}.json").write_text(json.dumps(receipt))
    await state.redis.lpush(QUEUE, json.dumps({"order_id": order_id}))
    return {"id": order_id}


def run():
    if STARTUP_DELAY_S > 0:
        log.info("warming up for %.0fs before listening", STARTUP_DELAY_S)
        time.sleep(STARTUP_DELAY_S)
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="warning", access_log=False)
