"""Tests use a real Postgres with pgvector, because row-level security can't be faked.

TEST_DATABASE_URL must point at a throwaway database whose name ends in _test; it is wiped. Start one with
scripts/test.sh. Without it the database tests FAIL rather than skip, so a missing database is never
mistaken for a pass.
"""
from __future__ import annotations

import os
from pathlib import Path

import psycopg
import pytest

ROOT = Path(__file__).resolve().parents[2]
OWNER_URL = os.environ.get("TEST_DATABASE_URL", "")
APP_PASSWORD = "pi_app_test_password"

os.environ.update({
    "SCOPE_SIGNING_KEY": "test-scope-key-0123456789abcdef", "TOOL_KEY": "test-tool-key-0123456789",
    "GATE_KEY": "test-gate-key-0123456789", "FAKE_MODELS": "1", "PUBLIC_BASE_URL": "http://testserver",
    "DOC_STORE": str(ROOT / "samples" / "store"), "MAX_TOOL_CALLS_PER_TURN": "3",
})


def app_url() -> str:
    from psycopg.conninfo import conninfo_to_dict, make_conninfo
    d = conninfo_to_dict(OWNER_URL)
    return make_conninfo(**{**d, "user": "pi_app", "password": APP_PASSWORD})


def reset_database() -> None:
    if not OWNER_URL:
        pytest.fail("TEST_DATABASE_URL is not set. Run scripts/test.sh, which starts a test database.")
    if not psycopg.conninfo.conninfo_to_dict(OWNER_URL).get("dbname", "").endswith("_test"):
        pytest.fail("TEST_DATABASE_URL must name a database ending in _test; it gets wiped.")
    with psycopg.connect(OWNER_URL, autocommit=True) as conn:
        conn.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
        if not conn.execute("SELECT 1 FROM pg_roles WHERE rolname = 'pi_app'").fetchone():
            conn.execute(f"CREATE ROLE pi_app LOGIN PASSWORD '{APP_PASSWORD}' NOBYPASSRLS")
        conn.execute("GRANT USAGE, CREATE ON SCHEMA public TO CURRENT_USER")
        for f in sorted((ROOT / "policy_api" / "app" / "migrations").glob("*.sql")):
            conn.execute(f.read_text())


@pytest.fixture(scope="session")
async def svc():
    reset_database()
    os.environ["DATABASE_URL"] = app_url()
    from app.config import load_settings
    from app.ingest import ingest_store
    from app.pipeline import build_services
    s = await build_services(load_settings())
    counts = await ingest_store(s.db, s.embedder, Path(os.environ["DOC_STORE"]), s.settings.policy_id_pattern)
    assert counts["problems"] == 0, counts
    yield s
    await s.close()


def make_scope(role: str, entities: list[str], user: str = "ana", chat: str = "chat-1", turn: str = "t1"):
    from app.scope import sign, verify
    key = os.environ["SCOPE_SIGNING_KEY"]
    return verify(key, sign(key, user_id=user, role=role, entities=entities, chat_id=chat, turn_id=turn))


def token(role: str, entities: list[str], user: str = "ana", chat: str = "chat-1", turn: str = "t1") -> str:
    from app.scope import sign
    return sign(os.environ["SCOPE_SIGNING_KEY"], user_id=user, role=role, entities=entities, chat_id=chat, turn_id=turn)
