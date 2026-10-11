import json
import os
import time
from collections import deque
from pathlib import Path

import psycopg
import redis
from prometheus_client import Counter, start_http_server

from app.common import QUEUE, SCHEMA, SCHEMA_LOCK, db_conninfo, env_int, setup_logging

log = setup_logging("worker")

CONNINFO = db_conninfo()
REDIS_URL = os.environ.get("REDIS_URL", "redis://redis:6379/0")
PORT = env_int("PORT", 8000)
WORK_MS = env_int("WORK_MS", 20)
TMP_DIR = Path(os.environ.get("TMP_DIR", "/tmp/jobs"))
TMP_KEEP = env_int("TMP_KEEP", 100)
TMP_FILE_KB = env_int("TMP_FILE_KB", 256)

PROCESSED = Counter("worker_jobs_processed_total", "jobs written to postgres")
ERRORS = Counter("worker_errors_total", "worker loop errors")


def connect():
    conn = psycopg.connect(CONNINFO, autocommit=True, connect_timeout=5)
    with conn.transaction():
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (SCHEMA_LOCK,))
        for statement in SCHEMA:
            conn.execute(statement)
    return conn


def run():
    start_http_server(PORT)
    TMP_DIR.mkdir(parents=True, exist_ok=True)
    queue = redis.Redis.from_url(REDIS_URL, socket_timeout=5, socket_connect_timeout=5)
    payload = b"\x00" * (TMP_FILE_KB * 1024)
    kept = deque()
    conn = None
    log.info("worker started work_ms=%d", WORK_MS)
    while True:
        try:
            if conn is None or conn.closed:
                conn = connect()
            item = queue.brpop([QUEUE], timeout=1)
            if item is None:
                continue
            job = json.loads(item[1])
            if WORK_MS:
                time.sleep(WORK_MS / 1000)
            conn.execute("INSERT INTO jobs_done (order_id) VALUES (%s)", (job["order_id"],))
            path = TMP_DIR / f"job-{job['order_id']}.bin"
            path.write_bytes(payload)
            kept.append(path)
            while len(kept) > TMP_KEEP:
                kept.popleft().unlink(missing_ok=True)
            PROCESSED.inc()
        except Exception as exc:
            ERRORS.inc()
            log.warning("worker loop error: %s", exc)
            if conn is not None and conn.broken:
                conn = None
            time.sleep(1)
