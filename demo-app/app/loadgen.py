import asyncio
import collections
import os

import httpx

from app.common import env_float, env_int, setup_logging

log = setup_logging("loadgen")

TARGET = os.environ.get("TARGET", "http://api.storefront.svc.cluster.local/orders")
RPS = env_float("RPS", 20)
MAX_INFLIGHT = env_int("MAX_INFLIGHT", 200)
REPORT_EVERY_S = 10


async def main():
    stats = collections.Counter()
    inflight = set()
    limits = httpx.Limits(max_connections=MAX_INFLIGHT, max_keepalive_connections=MAX_INFLIGHT)
    async with httpx.AsyncClient(timeout=5, limits=limits) as client:

        async def one():
            try:
                response = await client.post(TARGET, json={"sku": "load", "qty": 1})
                stats[str(response.status_code)] += 1
            except Exception as exc:
                stats[exc.__class__.__name__] += 1

        loop = asyncio.get_running_loop()
        log.info("loadgen target=%s rps=%s", TARGET, RPS)
        if RPS <= 0:
            await asyncio.Event().wait()
        interval = 1 / RPS
        next_tick = loop.time()
        next_report = next_tick + REPORT_EVERY_S
        while True:
            if len(inflight) >= MAX_INFLIGHT:
                stats["dropped"] += 1
            else:
                task = asyncio.create_task(one())
                inflight.add(task)
                task.add_done_callback(inflight.discard)
            next_tick += interval
            now = loop.time()
            if now >= next_report:
                log.info("last %ds: %s inflight=%d", REPORT_EVERY_S, dict(stats), len(inflight))
                stats.clear()
                next_report = now + REPORT_EVERY_S
            await asyncio.sleep(max(0, next_tick - loop.time()))


def run():
    asyncio.run(main())
