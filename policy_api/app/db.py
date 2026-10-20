"""Database access. Every query runs inside a transaction that first sets who is asking, so Postgres
row-level security filters it. There is no way to query without a scope."""
from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from .scope import Scope

SYSTEM = "system"


class DB:
    def __init__(self, url: str) -> None:
        self.pool = AsyncConnectionPool(url, open=False, kwargs={"row_factory": dict_row, "autocommit": False},
                                        min_size=1, max_size=10)

    async def open(self) -> None:
        await self.pool.open()

    async def close(self) -> None:
        await self.pool.close()

    @asynccontextmanager
    async def tx(self, role: str, entity_ids: list[str] | tuple[str, ...] = (), user_id: str = "") -> AsyncIterator[Any]:
        async with self.pool.connection() as conn:
            async with conn.transaction():
                await conn.execute(
                    "SELECT set_config('app.role', %s, true), set_config('app.entity_ids', %s, true),"
                    " set_config('app.user_id', %s, true)",
                    (role, ",".join(entity_ids), user_id))
                yield conn

    def system(self) -> Any:
        """For ingest, jobs and bookkeeping. Never used to answer a person's question directly."""
        return self.tx(SYSTEM, (), "system")

    @asynccontextmanager
    async def as_person(self, scope: Scope) -> AsyncIterator[Any]:
        visible = await self.visible_entities(scope.entities)
        async with self.tx(scope.role, visible, scope.user_id) as conn:
            yield conn

    async def visible_entities(self, roots: tuple[str, ...] | list[str]) -> list[str]:
        """The person's entities and everything below them."""
        if not roots:
            return []
        async with self.system() as conn:
            cur = await conn.execute(
                """WITH RECURSIVE tree AS (
                     SELECT id FROM entities WHERE id = ANY(%s)
                     UNION SELECT e.id FROM entities e JOIN tree t ON e.parent_id = t.id)
                   SELECT id FROM tree ORDER BY id""", (list(roots),))
            return [r["id"] for r in await cur.fetchall()]


async def all_rows(conn: Any, sql: str, *args: Any) -> list[dict]:
    cur = await conn.execute(sql, args)
    return await cur.fetchall()


async def one_row(conn: Any, sql: str, *args: Any) -> dict | None:
    cur = await conn.execute(sql, args)
    return await cur.fetchone()


async def audit(conn: Any, user_id: str, role: str, action: str, detail: dict, config_hash: str | None = None) -> None:
    from psycopg.types.json import Jsonb
    await conn.execute("INSERT INTO audit_log (user_id, role, action, detail, config_hash) VALUES (%s,%s,%s,%s,%s)",
                       (user_id, role, action, Jsonb(detail), config_hash))
