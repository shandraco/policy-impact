"""The whole flow on the sample store with fake models: draft upload, run, confirm, findings, cache, RLS."""
from pathlib import Path

from conftest import ROOT, make_scope

from app import tools
from app.documents import parse_policy
from app.ingest import store_version
from app.jobs import drain
from app.review import confirm_run

ANALYST = ["doe"]


async def _upload_draft(svc):
    data = (ROOT / "samples" / "drafts" / "DOE-512-v2.md").read_bytes()
    doc = parse_policy("DOE-512-v2.md", data, svc.settings.policy_id_pattern)
    return await store_version(svc.db, svc.embedder, doc, "doe", "draft-sha", svc.settings.policy_id_pattern, source="draft")


async def _run(svc, chat="chat-p"):
    scope = make_scope("authority_analyst", ANALYST, chat=chat, turn="t1")
    reply = await tools.dispatch(svc, scope, "analyze_impact", {"policy": "DOE-512"})
    assert "started" in reply["display"], reply["display"]
    run_id = reply["facts"]["run_id"]
    await drain(svc)
    status = await tools.dispatch(svc, make_scope("authority_analyst", ANALYST, chat=chat, turn="t2"), "query_run", {})
    assert "confirm" in status["display"].lower()
    async with svc.db.system() as conn:
        reqs = (await (await conn.execute("SELECT requirements FROM runs WHERE id = %s", (run_id,))).fetchone())["requirements"]
    form = {"keep": [str(i) for i in range(len(reqs))]}
    assert await confirm_run(svc, scope, run_id, form)
    await drain(svc)
    return run_id, reqs


async def test_end_to_end(svc):
    version = await _upload_draft(svc)
    assert version == 2
    run_id, reqs = await _run(svc)
    kinds = sorted(r["kind"] for r in reqs)
    assert kinds == ["changed", "removed"], reqs
    async with svc.db.system() as conn:
        run = await (await conn.execute("SELECT status, result FROM runs WHERE id = %s", (run_id,))).fetchone()
    assert run["status"] == "done"
    worst = {}
    for r in run["result"]["rows"]:
        worst.setdefault(r["entity"], []).append(r["result"])
    assert "conflict" in worst["Lincoln Elementary"]
    assert worst["Roosevelt High"] == ["compliant"]
    assert worst["Westside Charter"] == ["gap"]
    assert "stale_reference" in worst["District A"]
    assert run["result"]["counts"] == {"conflict": 1, "stale_reference": 1, "unsure": 0, "compliant": 1, "gap": 1}


async def test_second_run_uses_cache_not_the_model(svc):
    calls = {"n": 0}
    real = svc.judge.judge

    async def counting(req, text):
        calls["n"] += 1
        return await real(req, text)

    svc.judge.judge = counting
    try:
        run_id, _ = await _run(svc, chat="chat-cache")
    finally:
        svc.judge.judge = real
    assert calls["n"] == 0
    async with svc.db.system() as conn:
        stages = {r["stage"] for r in await (await conn.execute("SELECT stage FROM findings WHERE run_id = %s", (run_id,))).fetchall()}
    assert "judge" not in stages and "cache" in stages


async def test_viewer_sees_only_their_entity(svc):
    viewer = make_scope("viewer", ["lincoln-elementary"], user="lin", chat="chat-v")
    found = await tools.dispatch(svc, viewer, "find_policy", {"query": "DOE-512"})
    assert "couldn't find" in found["display"]  # the authority's policy isn't in a school viewer's scope
    async with svc.db.as_person(viewer) as conn:
        ents = {r["entity_id"] for r in await (await conn.execute("SELECT entity_id FROM findings")).fetchall()}
        pols = {r["id"] for r in await (await conn.execute("SELECT id FROM policies")).fetchall()}
    assert ents == {"lincoln-elementary"} and pols == {"LIN-20"}


async def test_viewer_cannot_start_a_run(svc):
    viewer = make_scope("viewer", ["doe"], user="v2", chat="chat-v2")
    reply = await tools.dispatch(svc, viewer, "analyze_impact", {"policy": "DOE-512"})
    assert "can't do this" in reply["display"]


async def test_turn_budget(svc):
    s = make_scope("authority_analyst", ANALYST, chat="chat-budget", turn="t1")
    for _ in range(3):
        assert "limit" not in (await tools.dispatch(svc, s, "list_runs", {}))["display"]
    assert "limit of steps" in (await tools.dispatch(svc, s, "list_runs", {}))["display"]


async def test_unknown_tool_gets_fixed_reply(svc):
    s = make_scope("admin", ANALYST, chat="chat-x")
    assert "only help" in (await tools.dispatch(svc, s, "run_shell", {"cmd": "ls"}))["display"]


async def test_check_reply_replaces_invented_ids(svc):
    s = make_scope("authority_analyst", ANALYST, chat="chat-check", turn="t1")
    reply = await tools.dispatch(svc, s, "find_policy", {"query": "DOE-512"})
    assert (await tools.check_reply(svc, s, "DOE-512 is the device policy."))["ok"]
    bad = await tools.check_reply(svc, s, "DOE-512 and also DOE-999 apply.")
    assert not bad["ok"] and bad["unknown_ids"] == ["DOE-999"] and bad["replacement"] == reply["display"]


async def test_eval_set_scores_sample_run(svc):
    import json
    from app.eval_cli import score
    run_id, _ = await _run(svc, chat="chat-eval")
    right, misses = await score(svc.db, json.loads((ROOT / "eval" / "cases.json").read_text()), run_id)
    assert misses == [] and right == 4
