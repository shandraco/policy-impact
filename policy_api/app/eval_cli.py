"""Scores a finished run against known answers.

  docker compose exec policy-api python -m app.eval_cli /eval/cases.json [run-id]

Run it after any change to a model, a prompt, or the settings. A score that drops means the change made answers worse.
"""
from __future__ import annotations

import asyncio
import json
import sys

from .config import config_hash, load_settings
from .db import DB, all_rows, one_row


async def score(db: DB, cases: dict, run_id: str | None) -> tuple[int, list[str]]:
    async with db.system() as conn:
        run = await one_row(conn, "SELECT id, requirements FROM runs WHERE policy_id = %s AND status = 'done' "
                                  "AND (%s::uuid IS NULL OR id = %s::uuid) ORDER BY created_at DESC LIMIT 1",
                            cases["policy"], run_id, run_id)
        if not run:
            raise SystemExit(f"No finished run for {cases['policy']}. Run one first.")
        rows = await all_rows(conn, "SELECT entity_id, requirement, result FROM findings WHERE run_id = %s", run["id"])
    reqs = run["requirements"] or []
    misses = []
    for c in cases["cases"]:
        idx = [i for i, r in enumerate(reqs) if c["requirement"].lower() in r["text"].lower() or c["requirement"] in r.get("source", "")]
        got = {r["result"] for r in rows if r["entity_id"] == c["entity"] and r["requirement"] in idx}
        if c["expect"] not in got:
            misses.append(f"{c['entity']} / {c['requirement']}: expected {c['expect']}, got {', '.join(sorted(got)) or 'nothing'}")
    return len(cases["cases"]) - len(misses), misses


async def main() -> None:
    settings = load_settings()
    cases = json.loads(open(sys.argv[1]).read())
    db = DB(settings.database_url)
    await db.open()
    try:
        right, misses = await score(db, cases, sys.argv[2] if len(sys.argv) > 2 else None)
    finally:
        await db.close()
    total = len(cases["cases"])
    print(f"{right} of {total} right ({100 * right // max(total, 1)}%), setup {config_hash(settings)}")
    for m in misses:
        print("  MISS", m)
    sys.exit(0 if not misses else 1)


if __name__ == "__main__":
    asyncio.run(main())
