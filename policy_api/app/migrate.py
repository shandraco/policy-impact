"""Apply app/migrations/*.sql once each, in name order. db/init.sql is the baseline for new databases;
every migration is written to be safe on a database that already has its changes."""
from __future__ import annotations

import logging
from pathlib import Path

log = logging.getLogger(__name__)
MIGRATIONS = Path(__file__).parent / "migrations"
_LOCK = 7_301_442  # any constant; serialises migrators across processes


async def migrate(pool) -> list[str]:
    applied_now = []
    async with pool.connection() as conn:
        async with conn.transaction():
            await conn.execute("SELECT pg_advisory_xact_lock(%s)", (_LOCK,))
            await conn.execute("CREATE TABLE IF NOT EXISTS schema_migrations "
                               "(version text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())")
            cur = await conn.execute("SELECT version FROM schema_migrations")
            done = {r["version"] if isinstance(r, dict) else r[0] for r in await cur.fetchall()}
            for path in sorted(MIGRATIONS.glob("*.sql")):
                if path.stem in done:
                    continue
                await conn.execute(path.read_text())
                await conn.execute("INSERT INTO schema_migrations (version) VALUES (%s)", (path.stem,))
                applied_now.append(path.stem)
    for v in applied_now:
        log.info("applied migration %s", v)
    return applied_now
