import asyncio
import os
import time

import httpx
from prometheus_client import Counter, Histogram, start_http_server

from app.common import env_float, env_int, setup_logging

log = setup_logging("slo-probe")

TARGET = os.environ.get("TARGET", "http://api.storefront.svc.cluster.local/orders")
PORT = env_int("PORT", 8000)
INTERVAL_S = env_float("INTERVAL_S", 1)
TIMEOUT_S = env_float("TIMEOUT_S", 2)
RESULTS = ("ok", "error", "timeout")

REQUESTS = Counter("probe_requests_total", "probe requests by result", ["result"])
LATENCY = Histogram(
    "probe_latency_seconds",
    "probe request latency",
    buckets=(0.025, 0.05, 0.1, 0.2, 0.3, 0.5, 1, 2, 5),
)


async def main():
    for result in RESULTS:
        REQUESTS.labels(result)
    loop = asyncio.get_running_loop()
    log.info("slo-probe target=%s interval=%ss", TARGET, INTERVAL_S)
    async with httpx.AsyncClient(timeout=TIMEOUT_S, limits=httpx.Limits(max_keepalive_connections=0)) as client:
        next_tick = loop.time()
        while True:
            start = time.perf_counter()
            try:
                response = await client.post(TARGET, json={"sku": "probe", "qty": 1})
                result = "ok" if response.status_code == 200 else "error"
            except httpx.TimeoutException:
                result = "timeout"
            except Exception:
                result = "error"
            REQUESTS.labels(result).inc()
            LATENCY.observe(time.perf_counter() - start)
            next_tick += INTERVAL_S
            await asyncio.sleep(max(0, next_tick - loop.time()))


def run():
    start_http_server(PORT)
    asyncio.run(main())
