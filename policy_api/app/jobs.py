"""A small job queue in Postgres. Workers claim with SKIP LOCKED and hold a lease; a crashed worker's job
is picked up again when the lease runs out. Failed jobs retry with backoff, then stop as 'dead'."""
from __future__ import annotations

import asyncio
import logging
import os
import socket
from pathlib import Path
from typing import Any

from psycopg.types.json import Jsonb

from .db import one_row
from .pipeline import Services, execute

log = logging.getLogger(__name__)
LEASE_S = 120
IDLE_S = 1.0


async def enqueue(conn: Any, kind: str, payload: dict, dedupe_key: str | None = None, max_attempts: int = 3) -> None:
    await conn.execute(
        """INSERT INTO jobs (kind, payload, dedupe_key, max_attempts) VALUES (%s,%s,%s,%s)
           ON CONFLICT (dedupe_key) WHERE dedupe_key IS NOT NULL AND status IN ('pending','running') DO NOTHING""",
        (kind, Jsonb(payload), dedupe_key, max_attempts))


async def claim(svc: Services, worker: str) -> dict | None:
    async with svc.db.system() as conn:
        return await one_row(conn, """
            UPDATE jobs SET status = 'running', attempts = attempts + 1, locked_by = %s,
                   locked_until = now() + make_interval(secs => %s)
            WHERE id = (SELECT id FROM jobs
                        WHERE run_at <= now() AND (status = 'pending' OR (status = 'running' AND locked_until < now()))
                        ORDER BY run_at FOR UPDATE SKIP LOCKED LIMIT 1)
            RETURNING id, kind, payload, attempts, max_attempts""", worker, LEASE_S)


async def finish(svc: Services, job: dict, error: str | None) -> None:
    async with svc.db.system() as conn:
        if error is None:
            await conn.execute("UPDATE jobs SET status = 'done', locked_until = NULL WHERE id = %s", (job["id"],))
        elif job["attempts"] >= job["max_attempts"]:
            await conn.execute("UPDATE jobs SET status = 'dead', last_error = %s WHERE id = %s", (error[:500], job["id"]))
        else:
            await conn.execute("""UPDATE jobs SET status = 'pending', last_error = %s, locked_by = NULL,
                                  run_at = now() + make_interval(secs => %s) WHERE id = %s""",
                               (error[:500], min(300, 10 * 2 ** job["attempts"]), job["id"]))


async def handle(svc: Services, job: dict) -> None:
    kind, payload = job["kind"], job["payload"]
    if kind in ("extract", "analyze"):
        await execute(svc, kind, payload["run_id"])
    elif kind == "ingest":
        from .ingest import ingest_store
        counts = await ingest_store(svc.db, svc.embedder, Path(svc.settings.doc_store), svc.settings.policy_id_pattern)
        log.info("ingest: %s", counts)
    elif kind == "review_alert":
        from .review import check_queue
        await check_queue(svc)
    else:
        raise ValueError(f"unknown job kind {kind}")


async def run_one(svc: Services, worker: str) -> bool:
    """Claim and run one job. Returns False when there was nothing to do."""
    job = await claim(svc, worker)
    if not job:
        return False
    try:
        await handle(svc, job)
        await finish(svc, job, None)
    except Exception as e:  # recorded on the job; the run row already says it failed
        await finish(svc, job, f"{type(e).__name__}: {e}")
    return True


async def run_forever(svc: Services) -> None:
    worker = f"{socket.gethostname()}-{os.getpid()}"
    while True:
        if not await run_one(svc, worker):
            await asyncio.sleep(IDLE_S)


async def drain(svc: Services, limit: int = 100) -> int:
    """Run jobs until the queue is empty. For the demo and tests."""
    n = 0
    while n < limit and await run_one(svc, "drain"):
        n += 1
    return n
