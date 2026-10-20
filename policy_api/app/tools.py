"""The intent gate: the only five things Hermes can ask the Policy API to do.

Every call carries the signed scope token (added by the model gate, never written by the model). Names,
IDs and aliases are resolved here in code. Each reply is {"display", "facts"}: display is finished
Markdown, facts are the IDs it mentions, which Open WebUI's outlet filter checks the reply against.
"""
from __future__ import annotations

import re
from typing import Any

from psycopg.types.json import Jsonb

from . import links
from .answer import BUDGET_SPENT, OUT_OF_SCOPE, md, render_result, render_status, short
from .db import all_rows, audit, one_row
from .jobs import enqueue
from .pipeline import ORDER, Services
from .scope import Scope, ScopeError

ACTIONS = {"find_policy": "find", "analyze_impact": "analyze", "query_run": "query",
           "list_runs": "list_runs", "explain_finding": "explain"}
RESULT_WORDS = {"conflict": "conflict", "conflicts": "conflict", "gap": "gap", "gaps": "gap",
                "stale": "stale_reference", "stale_reference": "stale_reference", "compliant": "compliant",
                "unsure": "unsure", "review": "unsure", "pending": "unsure"}
SHORT_RUN = re.compile(r"\bR-([0-9a-f]{8})\b", re.I)


class Reply(dict):
    def __init__(self, display: str, ids: list[str] | None = None, run_id: str | None = None) -> None:
        super().__init__(display=display, facts={"ids": sorted(set(ids or [])), "run_id": run_id},
                         instruction="Show the display text to the person exactly as written.")


# --- turn budget and chat state -------------------------------------------------------------------------

async def count_call(svc: Services, scope: Scope, column: str, limit: int) -> bool:
    async with svc.db.system() as conn:
        row = await one_row(conn, f"""INSERT INTO turns (chat_id, turn_id, {column}) VALUES (%s,%s,1)
                                      ON CONFLICT (chat_id, turn_id) DO UPDATE SET {column} = turns.{column} + 1
                                      RETURNING {column} AS n""", scope.chat_id, scope.turn_id)
    return row["n"] <= limit


async def get_state(svc: Services, scope: Scope) -> dict:
    async with svc.db.as_person(scope) as conn:
        row = await one_row(conn, "SELECT state FROM chat_state WHERE chat_id = %s", scope.chat_id)
    return row["state"] if row else {}


async def update_state(svc: Services, scope: Scope, **changes: Any) -> None:
    state = await get_state(svc, scope)
    if changes.get("facts"):
        turn = state.get("turn_facts") if state.get("turn_id") == scope.turn_id else {"ids": [], "display": ""}
        ids = sorted(set(turn["ids"]) | set(changes["facts"]["ids"]))
        changes["turn_facts"] = {"ids": ids, "display": changes.pop("display", turn.get("display", ""))}
        changes["turn_id"] = scope.turn_id
        changes["known_ids"] = sorted(set(state.get("known_ids", [])) | set(ids))[-500:]
        changes.pop("facts")
    state.update(changes)
    async with svc.db.as_person(scope) as conn:
        await conn.execute("""INSERT INTO chat_state (chat_id, user_id, state) VALUES (%s,%s,%s)
                              ON CONFLICT (chat_id) DO UPDATE SET state = EXCLUDED.state, updated_at = now()""",
                           (scope.chat_id, scope.user_id, Jsonb(state)))


def state_block(state: dict) -> str:
    """The short memory sent to the model each turn (about 100 tokens)."""
    bits = []
    if state.get("policy_id"):
        bits.append(f"Current policy: {state['policy_id']}")
    if state.get("scope_root"):
        bits.append(f"Current scope: {state['scope_root']}")
    if state.get("run_id"):
        bits.append(f"Last run: {short(state['run_id'])}")
    if state.get("filter"):
        bits.append(f"Last filter: {state['filter']}")
    return "Conversation state (from the Policy API): " + ("; ".join(bits) if bits else "nothing yet") + "."


# --- resolution -----------------------------------------------------------------------------------------

async def resolve_policies(svc: Services, scope: Scope, query: str, limit: int = 5) -> list[dict]:
    q = (query or "").strip()
    async with svc.db.as_person(scope) as conn:
        ids = re.findall(svc.settings.policy_id_pattern, q.upper())
        if ids:
            rows = await all_rows(conn, "SELECT id, title, entity_id FROM policies WHERE id = ANY(%s)", ids)
            if rows:
                return rows
        alias = await one_row(conn, """SELECT p.id, p.title, p.entity_id FROM aliases a JOIN policies p ON p.id = a.policy_id
                                       WHERE a.alias = %s""", q.lower())
        if alias:
            return [alias]
        words = [w for w in re.findall(r"[a-z0-9]{3,}", q.lower())][:8]
        if not words:
            return []
        rows = await all_rows(conn, """SELECT id, title, entity_id,
                                              (SELECT count(*) FROM unnest(%s::text[]) w WHERE lower(title) LIKE '%%' || w || '%%') AS score
                                       FROM policies ORDER BY score DESC, id LIMIT %s""", words, limit)
        rows = [r for r in rows if r["score"] > 0]
    if not rows:
        async with svc.db.system() as conn:
            await conn.execute("""INSERT INTO alias_misses (query) VALUES (%s)
                                  ON CONFLICT (query) DO UPDATE SET count = alias_misses.count + 1, last_at = now()""", (q.lower()[:200],))
    return rows


def _choices(rows: list[dict]) -> Reply:
    lines = ["Which policy do you mean?", ""] + [f"- **{r['id']}**: {md(r['title'])}" for r in rows]
    return Reply("\n".join(lines), [r["id"] for r in rows])


async def _run_for(svc: Services, scope: Scope, run_ref: str | None) -> dict | None:
    state = await get_state(svc, scope)
    ref = (run_ref or "").strip() or state.get("run_id") or ""
    m = SHORT_RUN.search(ref)
    async with svc.db.as_person(scope) as conn:
        if m:
            return await one_row(conn, "SELECT * FROM runs WHERE replace(id::text, '-', '') LIKE %s ORDER BY created_at DESC LIMIT 1",
                                 m.group(1).lower() + "%")
        if re.fullmatch(r"[0-9a-f-]{36}", ref):
            return await one_row(conn, "SELECT * FROM runs WHERE id = %s", ref)
    return None


def review_url(svc: Services, scope: Scope) -> str | None:
    if not scope.can("review"):
        return None
    return links.review_login_url(svc.settings, scope)


# --- the five actions -----------------------------------------------------------------------------------

async def find_policy(svc: Services, scope: Scope, args: dict) -> Reply:
    rows = await resolve_policies(svc, scope, str(args.get("query", "")))
    if not rows:
        return Reply("I couldn't find a policy matching that. Try its ID, for example DOE-512, or a word from its title.")
    if len(rows) == 1:
        r = rows[0]
        await update_state(svc, scope, policy_id=r["id"])
        return Reply(f"**{r['id']}**: {md(r['title'])}", [r["id"]])
    return _choices(rows)


async def analyze_impact(svc: Services, scope: Scope, args: dict) -> Reply:
    state = await get_state(svc, scope)
    rows = await resolve_policies(svc, scope, str(args.get("policy") or state.get("policy_id") or ""))
    if not rows:
        return Reply("Which policy changed? Give me its ID or a word from its title.")
    if len(rows) > 1:
        return _choices(rows)
    pol = rows[0]
    async with svc.db.as_person(scope) as conn:
        versions = await all_rows(conn, "SELECT version, source FROM policy_versions WHERE policy_id = %s ORDER BY version", pol["id"])
        current = await one_row(conn, "SELECT current_version FROM policies WHERE id = %s", pol["id"])
        root = pol["entity_id"]
        if args.get("scope"):
            ents = await all_rows(conn, "SELECT id, name FROM entities WHERE lower(name) LIKE %s OR id = %s",
                                  f"%{str(args['scope']).lower()}%", str(args["scope"]).lower())
            if len(ents) != 1:
                names = ", ".join(e["name"] for e in ents[:6]) or "none I can see"
                return Reply(f"Which group of entities should I check? Matches: {md(names)}.", [pol["id"]])
            root = ents[0]["id"]
    cur = current["current_version"]
    drafts = [v["version"] for v in versions if v["source"] == "draft" and v["version"] > cur]
    if drafts:
        old, new = cur, drafts[-1]
    elif cur > 1:
        old, new = cur - 1, cur
    else:
        return Reply(f"**{pol['id']}** has only one version, so there is no change to check yet. "
                     "Upload the new draft on the review page, then ask me again.", [pol["id"]])
    async with svc.db.as_person(scope) as conn:
        run = await one_row(conn, """INSERT INTO runs (policy_id, entity_id, scope_root, old_version, new_version, requested_by, chat_id, config_hash)
                                     VALUES (%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *""",
                            pol["id"], pol["entity_id"], root, old, new, scope.user_id, scope.chat_id, svc.config_hash)
        await enqueue(conn, "extract", {"run_id": str(run["id"])})
        await audit(conn, scope.user_id, scope.role, "analyze_impact",
                    {"run_id": str(run["id"]), "policy": pol["id"], "versions": [old, new], "scope_root": root}, svc.config_hash)
    await update_state(svc, scope, policy_id=pol["id"], run_id=str(run["id"]), scope_root=root, filter=None)
    return Reply(f"**Run {short(run['id'])}: {pol['id']} change**, started\n\nI'm comparing version {old} with version {new}. "
                 "I'll list the requirements in the change for an analyst to confirm, then check every entity. "
                 "Ask me about this run in a minute.", [pol["id"], short(run["id"])], str(run["id"]))


async def query_run(svc: Services, scope: Scope, args: dict) -> Reply:
    run = await _run_for(svc, scope, args.get("run"))
    if not run:
        return Reply("Which run do you mean? Start one by asking how a policy change affects the entities below it.")
    ids = [run["policy_id"], short(run["id"])]
    if run["status"] != "done":
        return Reply(render_status(run, review_url(svc, scope)), ids, str(run["id"]))
    want = RESULT_WORDS.get(str(args.get("result") or "").lower().strip())
    entity = str(args.get("entity") or "").strip().lower()
    async with svc.db.as_person(scope) as conn:  # RLS: a viewer sees only their own entities' rows
        rows = await all_rows(conn, """SELECT f.id AS finding, f.entity_id, e.name AS entity, f.requirement, f.result, f.why,
                                              f.review_status = 'pending' AS pending, c.policy_id, c.number
                                       FROM findings f JOIN entities e ON e.id = f.entity_id
                                       LEFT JOIN clauses c ON c.id = f.clause_id WHERE f.run_id = %s""", run["id"])
    if want:
        rows = [r for r in rows if r["result"] == want]
    if entity:
        rows = [r for r in rows if entity in r["entity"].lower() or entity == r["entity_id"]]
    filt = " and ".join(x for x in (want or "", entity) if x)
    await update_state(svc, scope, run_id=str(run["id"]), filter=filt or None)
    url = links.download_url(svc.settings, str(run["id"]), scope) if scope.can("export") else None
    ids += [r["policy_id"] for r in rows if r["policy_id"]]
    return Reply(render_result(run, run["requirements"] or [], rows, url, review_url(svc, scope), filt), ids, str(run["id"]))


async def list_runs(svc: Services, scope: Scope, args: dict) -> Reply:
    async with svc.db.as_person(scope) as conn:
        rows = await all_rows(conn, """SELECT id, policy_id, status, created_at FROM runs
                                       WHERE (%s = '' OR policy_id = %s) ORDER BY created_at DESC LIMIT 10""",
                              str(args.get("policy") or "").upper(), str(args.get("policy") or "").upper())
    if not rows:
        return Reply("There are no runs you can see yet.")
    lines = ["| Run | Policy | Status | Started |", "|---|---|---|---|"]
    lines += [f"| {short(r['id'])} | {r['policy_id']} | {r['status'].replace('_', ' ')} | {r['created_at']:%Y-%m-%d %H:%M} |" for r in rows]
    return Reply("\n".join(lines), [r["policy_id"] for r in rows] + [short(r["id"]) for r in rows])


async def explain_finding(svc: Services, scope: Scope, args: dict) -> Reply:
    run = await _run_for(svc, scope, args.get("run"))
    entity = str(args.get("entity") or "").strip().lower()
    if not run or not entity:
        return Reply("Tell me which entity's finding to explain, for example: explain the finding for Lincoln Elementary.")
    async with svc.db.as_person(scope) as conn:
        rows = await all_rows(conn, """SELECT f.*, e.name AS entity, c.policy_id, c.number, c.heading, c.body
                                       FROM findings f JOIN entities e ON e.id = f.entity_id LEFT JOIN clauses c ON c.id = f.clause_id
                                       WHERE f.run_id = %s AND (lower(e.name) LIKE %s OR e.id = %s)""",
                              run["id"], f"%{entity}%", entity)
    if not rows:
        return Reply("I couldn't find a finding for that entity in this run.", [short(run["id"])])
    reqs = run["requirements"] or []
    out, ids = [], [run["policy_id"], short(run["id"])]
    for f in sorted(rows, key=lambda r: ORDER.index(r["result"]))[:5]:
        req = reqs[f["requirement"]]["text"] if f["requirement"] < len(reqs) else ""
        out.append(f"**{md(f['entity'])}: {f['result'].replace('_', ' ')}**")
        out.append(f"- Requirement (§{reqs[f['requirement']]['source'] if req else '?'}): {md(req)}")
        if f["policy_id"]:
            ids.append(f["policy_id"])
            out.append(f"- Their clause, {f['policy_id']} §{f['number']}: {md(f['body'][:400])}")
        if f["evidence"]:
            out.append(f"- Evidence: \"{md(f['evidence'][:300])}\"")
        out.append(f"- Why: {md(f['why'])}" + (" (a reviewer decided this)" if f["stage"] in ("override", "reviewer") else ""))
        out.append("")
    return Reply("\n".join(out).strip(), ids, str(run["id"]))


HANDLERS = {"find_policy": find_policy, "analyze_impact": analyze_impact, "query_run": query_run,
            "list_runs": list_runs, "explain_finding": explain_finding}


async def dispatch(svc: Services, scope: Scope, name: str, args: dict) -> Reply:
    if name not in HANDLERS:
        return Reply(OUT_OF_SCOPE)
    if not await count_call(svc, scope, "tool_calls", svc.settings.max_tool_calls_per_turn):
        return Reply(BUDGET_SPENT)
    try:
        scope.require(ACTIONS[name])
    except ScopeError as e:
        return Reply(str(e))
    reply = await HANDLERS[name](svc, scope, args or {})
    await update_state(svc, scope, facts=reply["facts"], display=reply["display"])
    return reply


async def check_reply(svc: Services, scope: Scope, reply: str) -> dict:
    """Outlet check: every policy or run ID in the model's reply must come from a tool result.
    If not, the person gets the tool's own text instead."""
    state = await get_state(svc, scope)
    turn = state.get("turn_facts") if state.get("turn_id") == scope.turn_id else None
    allowed = set(turn["ids"] if turn else state.get("known_ids", []))
    allowed_upper = {a.upper() for a in allowed}
    found = set(re.findall(svc.settings.policy_id_pattern, reply)) | {m.group(0).upper() for m in SHORT_RUN.finditer(reply)}
    bad = sorted(x for x in found if x.upper() not in allowed_upper)
    if not bad:
        return {"ok": True}
    return {"ok": False, "unknown_ids": bad,
            "replacement": (turn or {}).get("display") or OUT_OF_SCOPE}
