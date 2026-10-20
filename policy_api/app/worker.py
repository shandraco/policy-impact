"""Background worker: runs extract and analyze jobs, re-ingests the document store every
INGEST_EVERY_S seconds, and checks the review queue every hour."""
from __future__ import annotations

import asyncio
import logging
import os

from .config import load_settings
from .jobs import enqueue, run_forever
from .pipeline import build_services


async def schedule(svc) -> None:
    every = int(os.environ.get("INGEST_EVERY_S") or 900)
    tick = 0
    while True:
        async with svc.db.system() as conn:
            await enqueue(conn, "ingest", {}, dedupe_key="ingest")
            if tick % max(1, 3600 // every) == 0:
                await enqueue(conn, "review_alert", {}, dedupe_key="review_alert")
        tick += 1
        await asyncio.sleep(every)


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    svc = await build_services(load_settings())
    try:
        await asyncio.gather(schedule(svc), run_forever(svc), run_forever(svc))
    finally:
        await svc.close()


if __name__ == "__main__":
    asyncio.run(main())
