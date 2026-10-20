"""The fixed impact pipeline. The chat model never decides any of these steps.

  extract job:  diff two versions of the authority policy -> requirements -> wait for an analyst
  analyze job:  for every entity below the authority and every confirmed requirement:
                candidates (citations + similar wording, reranked) -> judge the top few
                -> gap if nothing covers it -> escalate unsure -> findings -> result built by code
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
from dataclasses import dataclass, field
from typing import Any

from psycopg.types.json import Jsonb

from .config import PROMPT_VERSION, Settings, config_hash
from .db import DB, all_rows, one_row
from .diff import diff_clauses
from .documents import Clause
from .models import Requirement, norm

log = logging.getLogger(__name__)

# Worst first. An entity's result for a requirement is the worst of its candidate clauses.
ORDER = ["conflict", "stale_reference", "unsure", "compliant", "gap"]
LABEL = {"conflict": "Conflict", "stale_reference": "Stale reference", "unsure": "Needs a person",
         "compliant": "Already compliant", "gap": "Gap"}


@dataclass
class Services:
    settings: Settings
    db: DB
    judge: Any
    escalate: Any | None
    embedder: Any
    reranker: Any | None
    config_hash: str = ""
    _inflight: dict = field(default_factory=dict)

    async def close(self) -> None:
        for m in {id(x): x for x in (self.judge, self.escalate, self.embedder, self.reranker) if x}.values():
            await m.aclose()
        await self.db.close()


def build_models(settings: Settings) -> tuple[Any, Any, Any, Any]:
    if settings.fake_models:
        from .models import FakeModels
        fake = FakeModels()
        return fake, None, fake, fake
    from .models import LLMJudge, TEIEmbedder, TEIReranker
    judge = LLMJudge(settings.judge_url, settings.llm_api_key, settings.judge_model, settings.seed)
    escalate = (LLMJudge(settings.escalate_url, settings.llm_api_key, settings.escalate_model, settings.seed)
                if settings.escalate_url else None)
    return judge, escalate, TEIEmbedder(settings.embed_url), (TEIReranker(settings.rerank_url) if settings.rerank_url else None)


async def build_services(settings: Settings, db: DB | None = None) -> Services:
    db = db or DB(settings.database_url)
    await db.open()
    judge, escalate, embedder, reranker = build_models(settings)
    return Services(settings, db, judge, escalate, embedder, reranker, config_hash(settings))


def person_key(req_text: str, clause_hash: str) -> str:
    """Identifies 'this requirement against this exact clause text'. Overrides use it, so a person's
    decision applies to every entity whose clause has the same text."""
    return hashlib.sha256(f"{norm(req_text)}|{clause_hash}".encode()).hexdigest()


def cache_key(req_text: str, clause_hash: str, model: str) -> str:
    return hashlib.sha256(f"{norm(req_text)}|{clause_hash}|{model}|{PROMPT_VERSION}".encode()).hexdigest()


def covers(cited: str | None, section: str) -> bool:
    """A citation of §4 covers a change to §4.2; a citation of the whole policy covers everything."""
    return cited is None or section == cited or section.startswith(cited + ".")


# --- extract ---------------------------------------------------------------------------------------------

async def _clauses(conn: Any, policy_id: str, version: int) -> list[Clause]:
    rows = await all_rows(conn, "SELECT number, heading, body FROM clauses WHERE policy_id = %s AND version = %s ORDER BY id",
                          policy_id, version)
    return [Clause(r["number"], r["heading"], r["body"]) for r in rows]


async def run_extract(svc: Services, run_id: str) -> None:
    async with svc.db.system() as conn:
        run = await one_row(conn, "SELECT * FROM runs WHERE id = %s", run_id)
        await conn.execute("UPDATE runs SET status = 'extracting', progress = 'Reading the change' WHERE id = %s", (run_id,))
        old = await _clauses(conn, run["policy_id"], run["old_version"])
        new = await _clauses(conn, run["policy_id"], run["new_version"])
    changes = diff_clauses(old, new)
    reqs: list[dict] = []
    for ch in changes:
        if ch.kind in ("modified", "added"):
            for text in await svc.judge.extract(ch.new_text):
                reqs.append({"text": text, "source": ch.number, "kind": "changed" if ch.kind == "modified" else "added", "keep": True})
        if ch.kind in ("removed", "renumbered", "modified"):
            number = ch.old_number or ch.number
            if ch.kind != "modified" or not any(r["source"] == ch.number for r in reqs):
                reqs.append({"text": f"§{number} of {run['policy_id']} was {'removed' if ch.kind == 'removed' else 'changed'}; "
                                     f"anything citing it must be updated.",
                             "source": number, "kind": "removed" if ch.kind == "removed" else "moved", "keep": True})
    status = "awaiting_confirmation" if reqs else "done"
    result = None if reqs else {"counts": {}, "rows": [], "entities": 0, "note": "No changes were found between the two versions."}
    async with svc.db.system() as conn:
        await conn.execute(
            "UPDATE runs SET status = %s, requirements = %s, result = %s, progress = %s, finished_at = CASE WHEN %s THEN now() END WHERE id = %s",
            (status, Jsonb(reqs), Jsonb(result) if result else None,
             f"{len(reqs)} requirements found; waiting for an analyst to confirm them" if reqs else "No changes found",
             status == "done", run_id))


# --- analyze ---------------------------------------------------------------------------------------------

async def _verdict(svc: Services, model: Any, req: Requirement, clause: dict) -> tuple[dict, str]:
    """Cached by exact text. Identical clauses in different entities are judged once."""
    key = cache_key(req.text, clause["text_hash"], model.name)
    if key in svc._inflight:
        return await svc._inflight[key], "cache"
    async with svc.db.system() as conn:
        hit = await one_row(conn, "SELECT verdict FROM judgement_cache WHERE key = %s", key)
    if hit:
        return hit["verdict"], "cache"

    async def go() -> dict:
        text = f"{clause['heading']}\n{clause['body']}".strip()
        v = await model.judge(req, text)
        async with svc.db.system() as conn:
            await conn.execute("INSERT INTO judgement_cache (key, verdict, model) VALUES (%s,%s,%s) ON CONFLICT (key) DO NOTHING",
                               (key, Jsonb(v), model.name))
        return v

    task = asyncio.ensure_future(go())
    svc._inflight[key] = task
    try:
        return await task, "judge"
    finally:
        svc._inflight.pop(key, None)


async def _candidates(svc: Services, conn: Any, entity: str, req: Requirement, policy_id: str, vector: list[float]) -> list[dict]:
    cols = "c.id, c.policy_id, c.number, c.heading, c.body, c.text_hash"
    cites = await all_rows(conn, f"""SELECT DISTINCT {cols}, r.to_number FROM clauses c JOIN clause_refs r ON r.clause_id = c.id
                                     WHERE c.entity_id = %s AND c.is_current AND r.to_policy = %s""", entity, policy_id)
    cites = [c for c in cites if covers(c["to_number"], req.source)]
    near = await all_rows(conn, f"""SELECT {cols} FROM clauses c WHERE c.entity_id = %s AND c.is_current
                                    ORDER BY c.embedding <=> %s::vector LIMIT %s""",
                          entity, "[" + ",".join(f"{x:.6f}" for x in vector) + "]", svc.settings.semantic_k)
    seen = {c["id"] for c in cites}
    rest = [c for c in near if c["id"] not in seen]
    if svc.reranker and rest:
        scores = await svc.reranker.rerank(req.text, [f"{c['heading']}\n{c['body']}" for c in rest])
        rest = [c for s, c in sorted(zip(scores, rest), key=lambda t: -t[0]) if s > 0]
    return cites + rest[: svc.settings.candidates_per_requirement]


async def _stale(conn: Any, entity: str, policy_id: str, number: str) -> list[dict]:
    rows = await all_rows(conn, """SELECT c.id, c.policy_id, c.number, c.heading, c.body, r.to_number
                                   FROM clauses c JOIN clause_refs r ON r.clause_id = c.id
                                   WHERE c.entity_id = %s AND c.is_current AND r.to_policy = %s AND r.to_number IS NOT NULL""",
                          entity, policy_id)
    return [r for r in rows if r["to_number"] == number or r["to_number"].startswith(number + ".")]


async def judge_entity(svc: Services, conn: Any, entity: str, idx: int, req: Requirement, policy_id: str,
                       vector: list[float], sem: asyncio.Semaphore) -> dict:
    """One finding: this entity against this requirement."""
    base = {"entity_id": entity, "requirement": idx, "clause_id": None, "why": "", "evidence": "", "stage": "rule",
            "model": None, "cache_key": None, "review_status": None}
    if req.kind in ("removed", "moved"):
        hits = await _stale(conn, entity, policy_id, req.source)
        if not hits:
            return {}
        h = hits[0]
        return {**base, "result": "stale_reference", "clause_id": h["id"],
                "why": f"{h['policy_id']} §{h['number']} cites §{h['to_number']} of {policy_id}, which changed.",
                "evidence": h["body"][:300]}
    cands = await _candidates(svc, conn, entity, req, policy_id, vector)
    if not cands:
        return {**base, "result": "gap", "why": "Nothing in this entity's policies addresses this requirement."}

    async def one(c: dict) -> tuple[dict, dict, str]:
        pk = person_key(req.text, c["text_hash"])
        async with svc.db.system() as oc:
            o = await one_row(oc, "SELECT result, note FROM overrides WHERE key = %s", pk)
        if o:
            return c, {"relation": o["result"], "why": o["note"] or "Decided by a reviewer.", "evidence": ""}, "override"
        async with sem:
            v, stage = await _verdict(svc, svc.judge, req, c)
            if v["relation"] == "unsure" and svc.escalate:
                v, stage = await _verdict(svc, svc.escalate, req, c)
                stage = "escalate" if stage == "judge" else stage
        return c, v, stage

    judged = await asyncio.gather(*(one(c) for c in cands))
    related = [(c, v, s) for c, v, s in judged if v["relation"] != "not_related"]
    if not related:
        return {**base, "result": "gap", "why": f"The {len(cands)} closest clauses don't cover this requirement."}
    c, v, stage = min(related, key=lambda t: ORDER.index(t[1]["relation"]))
    model = None if stage == "override" else (svc.escalate.name if stage == "escalate" else svc.judge.name)
    return {**base, "result": v["relation"], "clause_id": c["id"], "why": v["why"], "evidence": v["evidence"],
            "stage": stage, "model": model, "cache_key": person_key(req.text, c["text_hash"]),
            "review_status": "pending" if v["relation"] == "unsure" else None}


async def run_analyze(svc: Services, run_id: str) -> None:
    async with svc.db.system() as conn:
        run = await one_row(conn, "SELECT * FROM runs WHERE id = %s", run_id)
        await conn.execute("UPDATE runs SET status = 'running', progress = 'Checking entities' WHERE id = %s", (run_id,))
        await conn.execute("DELETE FROM findings WHERE run_id = %s", (run_id,))  # a retry starts clean
    targets = [e for e in await svc.db.visible_entities([run["scope_root"]]) if e != run["entity_id"]]
    reqs = [(i, Requirement(r["text"], r["source"], r["kind"])) for i, r in enumerate(run["requirements"] or []) if r.get("keep")]
    vectors = await svc.embedder.embed([r.text for _, r in reqs]) if reqs else []
    sem = asyncio.Semaphore(svc.settings.judge_concurrency)
    findings: list[dict] = []
    async with svc.db.system() as conn:
        for n, entity in enumerate(targets, 1):
            rows = await asyncio.gather(*(judge_entity(svc, conn, entity, i, r, run["policy_id"], v, sem)
                                          for (i, r), v in zip(reqs, vectors)))
            findings += [f for f in rows if f]
            await conn.execute("UPDATE runs SET progress = %s WHERE id = %s", (f"Checked {n} of {len(targets)} entities", run_id))
    async with svc.db.system() as conn:
        for f in findings:
            await conn.execute(
                """INSERT INTO findings (run_id, entity_id, requirement, result, clause_id, why, evidence, stage, model, cache_key, review_status)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (run_id, f["entity_id"], f["requirement"], f["result"], f["clause_id"], f["why"], f["evidence"],
                 f["stage"], f["model"], f["cache_key"], f["review_status"]))
        result = await build_result(conn, run_id)
        await conn.execute("UPDATE runs SET status = 'done', progress = 'Finished', result = %s, finished_at = now() WHERE id = %s",
                           (Jsonb(result), run_id))


async def build_result(conn: Any, run_id: str) -> dict:
    """Counts and rows, from the findings table. Recomputed after every reviewer decision."""
    rows = await all_rows(conn, """SELECT f.id, f.entity_id, e.name AS entity, f.requirement, f.result, f.why, f.evidence,
                                          f.review_status, c.policy_id, c.number
                                   FROM findings f JOIN entities e ON e.id = f.entity_id
                                   LEFT JOIN clauses c ON c.id = f.clause_id
                                   WHERE f.run_id = %s ORDER BY e.name, f.requirement""", run_id)
    per_entity: dict[str, str] = {}
    for r in rows:
        cur = per_entity.get(r["entity_id"])
        if cur is None or ORDER.index(r["result"]) < ORDER.index(cur):
            per_entity[r["entity_id"]] = r["result"]
    counts = {k: sum(1 for v in per_entity.values() if v == k) for k in ORDER}
    return {"counts": counts, "entities": len(per_entity),
            "rows": [{"finding": r["id"], "entity_id": r["entity_id"], "entity": r["entity"], "requirement": r["requirement"],
                      "result": r["result"], "policy_id": r["policy_id"], "number": r["number"], "why": r["why"],
                      "pending": r["review_status"] == "pending"} for r in rows]}


async def execute(svc: Services, kind: str, run_id: str) -> None:
    try:
        await (run_extract if kind == "extract" else run_analyze)(svc, run_id)
    except Exception as e:
        log.exception("run %s failed in %s", run_id, kind)
        async with svc.db.system() as conn:
            await conn.execute("UPDATE runs SET status = 'failed', error = %s, progress = 'Failed' WHERE id = %s",
                               (f"{type(e).__name__}: {e}"[:500], run_id))
        raise
