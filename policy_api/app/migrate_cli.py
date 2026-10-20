"""One-shot: apply migrations as the owner role. Run by the `migrate` service before the API starts."""
from __future__ import annotations

import asyncio
import logging
import os

from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from .migrate import migrate


async def main() -> None:
    logging.basicConfig(level=logging.INFO)
    url = os.environ["MIGRATE_DATABASE_URL"]
    async with AsyncConnectionPool(url, kwargs={"row_factory": dict_row}, min_size=1, max_size=1) as pool:
        applied = await migrate(pool)
    print(f"migrations applied: {applied or 'none needed'}")


if __name__ == "__main__":
    asyncio.run(main())
