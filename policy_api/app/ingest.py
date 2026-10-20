"""Read the document store into Postgres.

Layout (read-only volume):
    entities.yaml                 the tree: id, name, kind, parent
    <entity-id>/<any folders>/*.md|.txt|.docx|.pdf

A file is identified by its path and SHA-256. Unchanged files are skipped; a changed file becomes a new
version of its policy and the old clauses stop being current. Nothing is ever deleted.
"""
from __future__ import annotations

import hashlib
import logging
from pathlib import Path
from typing import Any

import yaml

from .db import DB, all_rows, one_row
from .documents import SUPPORTED_SUFFIXES, PolicyDoc, find_refs
from .models import text_hash
from .safe_parse import ParseTimeout, parse_isolated

log = logging.getLogger(__name__)


def _vec(v: list[float]) -> str:
    return "[" + ",".join(f"{x:.6f}" for x in v) + "]"


async def load_entities(db: DB, store: Path) -> int:
    path = store / "entities.yaml"
    if not path.exists():
        raise ValueError(f"{path} is missing. It lists every entity: id, name, kind and parent.")
    items = yaml.safe_load(path.read_text()) or []
    by_id = {str(e["id"]): e for e in items}
    for e in items:  # parents must exist before children
        p = e.get("parent")
        if p and str(p) not in by_id:
            raise ValueError(f"Entity {e['id']} names parent {p}, which is not in entities.yaml.")
    done: set[str] = set()

    async def put(conn: Any, eid: str) -> None:
        if eid in done:
            return
        e = by_id[eid]
        if e.get("parent"):
            await put(conn, str(e["parent"]))
        await conn.execute(
            """INSERT INTO entities (id, name, kind, parent_id) VALUES (%s,%s,%s,%s)
               ON CONFLICT (id) DO UPDATE SET name = EXCLUDED.name, kind = EXCLUDED.kind, parent_id = EXCLUDED.parent_id""",
            (eid, str(e.get("name") or eid), str(e.get("kind") or "entity"), str(e["parent"]) if e.get("parent") else None))
        done.add(eid)

    async with db.system() as conn:
        for eid in by_id:
            await put(conn, eid)
    return len(by_id)


async def store_version(db: DB, embedder: Any, doc: PolicyDoc, entity_id: str, sha: str, id_pattern: str,
                        source: str = "store") -> int:
    """Save one parsed policy as a new version. Store versions become current; drafts never do."""
    assert doc.id
    vectors = await embedder.embed([c.text for c in doc.clauses]) if doc.clauses else []
    async with db.system() as conn:
        existing = await one_row(conn, "SELECT entity_id, current_version FROM policies WHERE id = %s", doc.id)
        if existing and existing["entity_id"] != entity_id:
            raise ValueError(f"{doc.id} already belongs to {existing['entity_id']}, not {entity_id}.")
        last = await one_row(conn, "SELECT max(version) AS v FROM policy_versions WHERE policy_id = %s", doc.id)
        version = (last["v"] or 0) + 1 if last else 1
        current = source == "store"
        if not existing:
            await conn.execute("INSERT INTO policies (id, entity_id, title, restricted, current_version) VALUES (%s,%s,%s,%s,%s)",
                               (doc.id, entity_id, doc.title, doc.restricted, version))
        elif current:
            await conn.execute("UPDATE policies SET title = %s, restricted = %s, current_version = %s WHERE id = %s",
                               (doc.title, doc.restricted, version, doc.id))
            await conn.execute("UPDATE clauses SET is_current = false WHERE policy_id = %s", (doc.id,))
        await conn.execute(
            """INSERT INTO policy_versions (policy_id, version, entity_id, restricted, sha256, body, effective, source)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""",
            (doc.id, version, entity_id, doc.restricted, sha, doc.body, doc.effective, source))
        for clause, vec in zip(doc.clauses, vectors):
            row = await one_row(conn,
                """INSERT INTO clauses (policy_id, version, entity_id, restricted, number, heading, body, text_hash, is_current, embedding)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::vector) RETURNING id""",
                doc.id, version, entity_id, doc.restricted, clause.number, clause.heading, clause.body,
                text_hash(clause.text), current, _vec(vec))
            for to_policy, to_number in find_refs(clause.text, id_pattern, doc.id):
                await conn.execute("INSERT INTO clause_refs (clause_id, entity_id, to_policy, to_number) VALUES (%s,%s,%s,%s)",
                                   (row["id"], entity_id, to_policy, to_number))
    return version


async def ingest_store(db: DB, embedder: Any, store: Path, id_pattern: str) -> dict:
    """Ingest every new or changed file. Returns counts for the log and the health page."""
    await load_entities(db, store)
    async with db.system() as conn:
        known = {(r["path"], r["sha256"]) for r in await all_rows(conn, "SELECT path, sha256 FROM documents")}
        entity_ids = {r["id"] for r in await all_rows(conn, "SELECT id FROM entities")}
    counts = {"new": 0, "skipped": 0, "problems": 0}
    for eid in sorted(entity_ids):
        folder = store / eid
        if not folder.is_dir():
            continue
        for f in sorted(folder.rglob("*")):
            if not f.is_file() or f.suffix.lower() not in SUPPORTED_SUFFIXES:
                continue
            rel = str(f.relative_to(store))
            data = f.read_bytes()
            sha = hashlib.sha256(data).hexdigest()
            if (rel, sha) in known:
                counts["skipped"] += 1
                continue
            status, note, pid = "ok", None, None
            try:
                doc = await parse_isolated(f.name, data, id_pattern)
                if not doc.id:
                    status, note = "no_id", "No policy ID found. Add 'id: XXX-123' at the top or put it in the file name."
                else:
                    pid = doc.id
                    await store_version(db, embedder, doc, eid, sha, id_pattern)
            except (ValueError, ParseTimeout) as e:
                status, note = "unreadable", str(e) or "The file took too long to read."
            async with db.system() as conn:
                await conn.execute("INSERT INTO documents (entity_id, path, sha256, policy_id, status, note) VALUES (%s,%s,%s,%s,%s,%s)",
                                   (eid, rel, sha, pid, status, note))
            counts["new" if status == "ok" else "problems"] += 1
            if status != "ok":
                log.warning("ingest %s: %s", rel, note)
    return counts
