"""Review decisions with undo, and the HTTP surface: keys, the model gate, the review cookie, downloads."""
import json

import httpx
from conftest import make_scope, token

from app.review import decide, undo


async def _pending_finding(svc) -> int:
    async with svc.db.system() as conn:
        f = await (await conn.execute("""SELECT id FROM findings WHERE entity_id = 'lincoln-elementary' AND cache_key IS NOT NULL
                                         ORDER BY id LIMIT 1""")).fetchone()
        await conn.execute("UPDATE findings SET review_status = 'pending', result = 'unsure' WHERE id = %s", (f["id"],))
    return f["id"]


async def test_decide_creates_override_and_undo_restores(svc):
    fid = await _pending_finding(svc)
    analyst = make_scope("authority_analyst", ["doe"], user="rev")
    assert await decide(svc, analyst, fid, "compliant")
    async with svc.db.system() as conn:
        f = await (await conn.execute("SELECT * FROM findings WHERE id = %s", (fid,))).fetchone()
        o = await (await conn.execute("SELECT * FROM overrides WHERE key = %s", (f["cache_key"],))).fetchone()
    assert f["result"] == "compliant" and f["stage"] == "reviewer" and o["result"] == "compliant"
    assert await undo(svc, analyst, fid)
    async with svc.db.system() as conn:
        f = await (await conn.execute("SELECT * FROM findings WHERE id = %s", (fid,))).fetchone()
        o = await (await conn.execute("SELECT * FROM overrides WHERE key = %s", (f["cache_key"],))).fetchone()
    assert f["result"] == "unsure" and f["review_status"] == "pending" and o is None


async def test_viewer_cannot_decide(svc):
    fid = await _pending_finding(svc)
    viewer = make_scope("viewer", ["lincoln-elementary"], user="v")
    try:
        await decide(svc, viewer, fid, "conflict")
        raise AssertionError("viewer decided a finding")
    except Exception as e:
        assert "can't do this" in str(e)


def _client(svc):
    from app import main
    main.app.state.svc = svc
    from app.gate import Gate
    main.app.state.gate = Gate(svc)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://testserver")


async def test_http_keys_and_gate_loop(svc):
    async with _client(svc) as c:
        assert (await c.post("/tools/list_runs", json={})).status_code == 401
        assert (await c.post("/gate/v1/chat/completions", json={})).status_code == 401
        gate = {"Authorization": "Bearer test-gate-key-0123456789"}
        tool = {"Authorization": "Bearer test-tool-key-0123456789"}
        tok = token("authority_analyst", ["doe"], chat="chat-http", turn="h1")
        tools = [{"type": "function", "function": {"name": n, "parameters": {"type": "object", "properties": {
            "query": {"type": "string"}, "scope_token": {"type": "string"}}}}} for n in ("find_policy", "query_run")]
        msgs = [{"role": "system", "content": f"Hermes system prompt [[scope:{tok}]]"}, {"role": "user", "content": "find DOE-512"}]
        r = (await c.post("/gate/v1/chat/completions", headers=gate, json={"messages": msgs, "tools": tools})).json()
        call = r["choices"][0]["message"]["tool_calls"][0]
        args = json.loads(call["function"]["arguments"])
        assert args["scope_token"] == tok  # added by the gate, not the model
        result = (await c.post(f"/tools/{call['function']['name']}", headers=tool, json=args)).json()
        assert "DOE-512" in result["display"]
        msgs += [r["choices"][0]["message"], {"role": "tool", "tool_call_id": call["id"], "content": json.dumps(result)}]
        r2 = (await c.post("/gate/v1/chat/completions", headers=gate, json={"messages": msgs, "tools": tools, "stream": True}))
        assert r2.headers["content-type"].startswith("text/event-stream") and "DOE-512" in r2.text
        no_token = (await c.post("/gate/v1/chat/completions", headers=gate,
                                 json={"messages": [{"role": "user", "content": "hi"}]})).json()
        assert "confirm who you are" in no_token["choices"][0]["message"]["content"]


async def test_review_cookie_and_scoped_download(svc):
    from app.links import download_url, review_login_url
    analyst = make_scope("authority_analyst", ["doe"], user="rev2")
    async with _client(svc) as c:
        assert (await c.get("/review")).status_code == 401
        r = await c.get(review_login_url(svc.settings, analyst).replace("http://testserver", ""))
        assert r.status_code == 303 and "pi_review" in r.headers["set-cookie"] and "HttpOnly" in r.headers["set-cookie"]
        c.cookies.set("pi_review", r.cookies["pi_review"])
        home = await c.get("/review")
        assert home.status_code == 200 and "Review" in home.text
        async with svc.db.system() as conn:
            run = await (await conn.execute("SELECT id FROM runs WHERE status = 'done' LIMIT 1")).fetchone()
        viewer = make_scope("entity_manager", ["westside-charter"], user="w")
        x = await c.get(download_url(svc.settings, str(run["id"]), viewer).replace("http://testserver", ""))
        assert x.status_code == 404  # a Westside manager can't see the authority's run at all
        a = await c.get(download_url(svc.settings, str(run["id"]), analyst).replace("http://testserver", ""))
        assert a.status_code == 200 and a.content[:2] == b"PK"
        bad = await c.get(f"/exports/{run['id']}.xlsx?t=forged.token")
        assert bad.status_code == 403
