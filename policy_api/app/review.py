"""The review page, for analysts: confirm requirements, decide unsure findings one at a time (with undo),
and upload a draft of a changed policy. Server-rendered HTML with big buttons and plain words."""
from __future__ import annotations

import html
import logging
from datetime import datetime, timezone
from typing import Any

from psycopg.types.json import Jsonb

from .db import all_rows, audit, one_row
from .jobs import enqueue
from .pipeline import Services, build_result
from .scope import Scope

log = logging.getLogger(__name__)

DECISIONS = {  # button -> (finding result, override relation)
    "conflict": ("conflict", "conflict"),
    "compliant": ("compliant", "compliant"),
    "gap": ("gap", "not_related"),
}
BUTTONS = [("conflict", "It conflicts", "Their clause says something the new requirement doesn't allow."),
           ("compliant", "It already complies", "Their clause meets the requirement or is stricter."),
           ("gap", "It doesn't cover this", "Their clause is about something else, so this is a gap.")]

CSS = """
:root{--ink:#172033;--muted:#5B6578;--paper:#F6F7F4;--teal:#0E4F55;--violet:#5B3A9E;--line:#CDD2D9;--warn:#8A4B00}
*{box-sizing:border-box}body{margin:0;background:var(--paper);color:var(--ink);font:17px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif}
main{max-width:760px;margin:0 auto;padding:24px 16px 64px}h1{font-size:28px;margin:8px 0 4px}h2{font-size:20px;margin:28px 0 8px}
p{margin:6px 0}.muted{color:var(--muted)}.card{background:#fff;border:1px solid var(--line);border-radius:10px;padding:16px 18px;margin:12px 0}
.quote{border-left:4px solid var(--teal);padding:4px 12px;margin:8px 0;background:#F0F5F4;white-space:pre-wrap}
.btn{display:block;width:100%;text-align:left;font:inherit;font-weight:650;padding:16px 18px;margin:10px 0;border-radius:10px;
border:2px solid var(--teal);background:#fff;color:var(--teal);cursor:pointer;text-decoration:none}
.btn small{display:block;font-weight:400;color:var(--muted)}.btn.primary{background:var(--teal);color:#fff}
.btn.primary small{color:#CFE5E3}.btn:focus-visible{outline:3px solid var(--violet);outline-offset:2px}
.notice{background:#EEE8F7;border:1px solid var(--violet);border-radius:10px;padding:12px 16px}
.alert{background:#FFF4E5;border:1px solid var(--warn);border-radius:10px;padding:12px 16px}
textarea{width:100%;font:inherit;padding:8px;border:1px solid var(--line);border-radius:8px;min-height:64px}
label.keep{display:flex;gap:10px;align-items:center;font-weight:650;margin-bottom:6px}input[type=checkbox]{width:22px;height:22px}
a{color:var(--teal)}nav{display:flex;gap:18px;font-size:15px}
"""


def page(title: str, body: str) -> str:
    return (f"<!doctype html><html lang=en><head><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>"
            f"<title>{html.escape(title)}</title><style>{CSS}</style></head><body><main>"
            f"<nav><a href='/review'>Review home</a><a href='/review/next'>Next finding</a></nav>{body}</main></body></html>")


def e(text: Any) -> str:
    return html.escape(str(text or ""))


async def queue_stats(conn: Any) -> dict:
    row = await one_row(conn, """SELECT count(*) AS n, min(r.finished_at) AS oldest FROM findings f JOIN runs r ON r.id = f.run_id
                                 WHERE f.review_status = 'pending'""")
    hours = (datetime.now(timezone.utc) - row["oldest"]).total_seconds() / 3600 if row["oldest"] else 0
    return {"pending": row["n"], "oldest_hours": round(hours, 1)}


async def check_queue(svc: Services) -> dict:
    """Hourly job. Writes an alert to the log and the audit trail when the queue is too big or too old;
    the review page shows the same alert at the top."""
    async with svc.db.system() as conn:
        stats = await queue_stats(conn)
        s = svc.settings
        if stats["pending"] >= s.review_alert_count or stats["oldest_hours"] >= s.review_alert_hours:
            log.warning("review queue needs attention: %s", stats)
            await audit(conn, "system", "system", "review_alert", stats)
    return stats


async def home(svc: Services, scope: Scope) -> str:
    async with svc.db.as_person(scope) as conn:
        waiting = await all_rows(conn, """SELECT id, policy_id, created_at, requirements FROM runs
                                          WHERE status = 'awaiting_confirmation' ORDER BY created_at""")
        stats = await queue_stats(conn)
    s = svc.settings
    parts = [f"<h1>Review</h1><p class=muted>Signed in as {e(scope.user_id)}.</p>"]
    if stats["pending"] >= s.review_alert_count or stats["oldest_hours"] >= s.review_alert_hours:
        parts.append(f"<p class=alert>The queue needs attention: {stats['pending']} findings are waiting, "
                     f"the oldest for {stats['oldest_hours']:.0f} hours.</p>")
    parts.append("<h2>Requirements to confirm</h2>")
    if waiting:
        for r in waiting:
            n = sum(1 for x in r["requirements"] or [] if x.get("keep"))
            parts.append(f"<a class='btn primary' href='/review/run/{r['id']}'>{e(r['policy_id'])}: check {n} requirements"
                         f"<small>Started {r['created_at']:%d %b %Y, %H:%M}. Nothing is checked until you confirm.</small></a>")
    else:
        parts.append("<p class=muted>Nothing to confirm.</p>")
    parts.append("<h2>Findings that need a person</h2>")
    parts.append(f"<a class='btn primary' href='/review/next'>Decide the next finding<small>{stats['pending']} waiting, "
                 "one at a time, with undo.</small></a>" if stats["pending"] else "<p class=muted>None waiting.</p>")
    if scope.can("upload"):
        parts.append("""<h2>Upload a draft change</h2><form class=card method=post action=/review/upload enctype=multipart/form-data>
          <p>Upload the new version of a policy before it takes effect. Its ID must match the existing policy.</p>
          <input type=file name=file accept=.md,.txt,.docx,.pdf required>
          <button class='btn primary'>Upload the draft<small>Then ask in chat how the change affects the entities below.</small></button></form>""")
    return page("Review", "".join(parts))


async def run_form(svc: Services, scope: Scope, run_id: str) -> str | None:
    async with svc.db.as_person(scope) as conn:
        run = await one_row(conn, "SELECT * FROM runs WHERE id = %s AND status = 'awaiting_confirmation'", run_id)
    if not run:
        return None
    items = []
    for i, r in enumerate(run["requirements"] or []):
        items.append(f"""<div class=card><label class=keep><input type=checkbox name=keep value={i} {'checked' if r.get('keep') else ''}>
            Check this requirement (from §{e(r['source'])})</label><textarea name=text_{i} aria-label="Requirement {i + 1}">{e(r['text'])}</textarea></div>""")
    return page("Confirm requirements", f"""<h1>{e(run['policy_id'])}: confirm the requirements</h1>
        <p>These are the obligations found in the change. Untick any that aren't real, fix the wording if needed, and add any
        that were missed in the last box. Nothing is checked until you confirm.</p>
        <form method=post action=/review/run/{run['id']}>{''.join(items)}
        <div class=card><label class=keep>Add a missed requirement (optional)</label><textarea name=extra></textarea>
        <input name=extra_source placeholder="Section number, e.g. 4.2" style="margin-top:8px;padding:8px;font:inherit"></div>
        <button class='btn primary'>Confirm and check every entity<small>This starts the check. You can't edit the list afterwards.</small></button></form>""")


async def confirm_run(svc: Services, scope: Scope, run_id: str, form: dict) -> bool:
    scope.require("review")
    async with svc.db.as_person(scope) as conn:
        run = await one_row(conn, "SELECT * FROM runs WHERE id = %s AND status = 'awaiting_confirmation' FOR UPDATE", run_id)
        if not run:
            return False
        keep = {int(k) for k in form.get("keep", [])}
        reqs = []
        for i, r in enumerate(run["requirements"] or []):
            text = (form.get(f"text_{i}") or [r["text"]])[0].strip() or r["text"]
            reqs.append({**r, "text": text, "keep": i in keep})
        extra = (form.get("extra") or [""])[0].strip()
        if extra:
            reqs.append({"text": extra, "source": (form.get("extra_source") or [""])[0].strip() or "?", "kind": "added",
                         "keep": True, "added_by": scope.user_id})
        await conn.execute("""UPDATE runs SET requirements = %s, status = 'confirmed', confirmed_by = %s, confirmed_at = now(),
                              progress = 'Confirmed; waiting to check entities' WHERE id = %s""", (Jsonb(reqs), scope.user_id, run_id))
        await enqueue(conn, "analyze", {"run_id": str(run_id)})
        await audit(conn, scope.user_id, scope.role, "confirm_requirements",
                    {"run_id": str(run_id), "kept": sum(r["keep"] for r in reqs), "total": len(reqs)})
    return True


async def next_finding(svc: Services, scope: Scope, saved: str | None = None, after: int = 0) -> str:
    async with svc.db.as_person(scope) as conn:
        f = await one_row(conn, """SELECT f.*, e.name AS entity, r.policy_id AS authority, r.requirements, c.policy_id, c.number, c.heading, c.body
                                   FROM findings f JOIN entities e ON e.id = f.entity_id JOIN runs r ON r.id = f.run_id
                                   LEFT JOIN clauses c ON c.id = f.clause_id
                                   WHERE f.review_status = 'pending' AND f.id > %s ORDER BY f.id LIMIT 1""", after)
        left = (await queue_stats(conn))["pending"]
    top = ""
    if saved:
        top = (f"<form class=notice method=post action=/review/undo/{e(saved)}>Saved. "
               "<button class=btn style='display:inline;width:auto;padding:6px 12px;margin:0 0 0 8px'>Undo</button></form>")
    if not f:
        return page("Review", top + "<h1>All done</h1><p>No findings are waiting. Thank you.</p>")
    req = (f["requirements"] or [])[f["requirement"]]
    buttons = "".join(f"<button class='btn' name=decision value={k}>{label}<small>{hint}</small></button>" for k, label, hint in BUTTONS)
    clause = (f"<p><b>{e(f['entity'])}, {e(f['policy_id'])} §{e(f['number'])}</b></p><div class=quote>{e(f['heading'])}\n{e(f['body'])}</div>"
              if f["policy_id"] else f"<p><b>{e(f['entity'])}</b> has no clause about this.</p>")
    return page("Decide a finding", f"""{top}<p class=muted>{left} waiting</p><h1>Does this clause conflict with the change?</h1>
        <div class=card><p><b>New requirement</b> from {e(f['authority'])} §{e(req['source'])}</p><div class=quote>{e(req['text'])}</div></div>
        <div class=card>{clause}<p class=muted>The model wasn't sure: {e(f['why'])}</p></div>
        <form method=post action=/review/finding/{f['id']}>{buttons}
        <a class=btn href='/review/next?after={f['id']}'>I can't tell<small>Skip it for now; it stays in the queue for someone else.</small></a></form>""")


async def decide(svc: Services, scope: Scope, finding_id: int, decision: str) -> bool:
    scope.require("review")
    if decision not in DECISIONS:
        return False
    result, relation = DECISIONS[decision]
    async with svc.db.as_person(scope) as conn:
        f = await one_row(conn, "SELECT * FROM findings WHERE id = %s AND review_status = 'pending' FOR UPDATE", finding_id)
        if not f:
            return False
        prev_override = None
        if f["cache_key"]:
            prev_override = await one_row(conn, "SELECT result, note, decided_by FROM overrides WHERE key = %s", f["cache_key"])
            await conn.execute("""INSERT INTO overrides (key, result, note, decided_by) VALUES (%s,%s,%s,%s)
                                  ON CONFLICT (key) DO UPDATE SET result = EXCLUDED.result, note = EXCLUDED.note,
                                  decided_by = EXCLUDED.decided_by, decided_at = now()""",
                               (f["cache_key"], relation, f"Decided by {scope.user_id}.", scope.user_id))
        previous = {"result": f["result"], "why": f["why"], "stage": f["stage"], "override": prev_override}
        await conn.execute("""UPDATE findings SET result = %s, stage = 'reviewer', review_status = 'done', reviewed_by = %s,
                              reviewed_at = now(), previous = %s, why = %s WHERE id = %s""",
                           (result, scope.user_id, Jsonb(previous), f"Decided by a reviewer ({scope.user_id}).", finding_id))
        await conn.execute("UPDATE runs SET result = %s WHERE id = %s", (Jsonb(await build_result(conn, f["run_id"])), f["run_id"]))
        await audit(conn, scope.user_id, scope.role, "review_decision", {"finding": finding_id, "decision": decision})
    return True


async def undo(svc: Services, scope: Scope, finding_id: int) -> bool:
    scope.require("review")
    async with svc.db.as_person(scope) as conn:
        f = await one_row(conn, "SELECT * FROM findings WHERE id = %s AND review_status = 'done' AND reviewed_by = %s FOR UPDATE",
                          finding_id, scope.user_id)
        if not f or not f["previous"]:
            return False
        p = f["previous"]
        if f["cache_key"]:
            if p.get("override"):
                o = p["override"]
                await conn.execute("UPDATE overrides SET result = %s, note = %s, decided_by = %s WHERE key = %s",
                                   (o["result"], o["note"], o["decided_by"], f["cache_key"]))
            else:
                await conn.execute("DELETE FROM overrides WHERE key = %s", (f["cache_key"],))
        await conn.execute("""UPDATE findings SET result = %s, why = %s, stage = %s, review_status = 'pending', reviewed_by = NULL,
                              reviewed_at = NULL, previous = NULL WHERE id = %s""", (p["result"], p["why"], p["stage"], finding_id))
        await conn.execute("UPDATE runs SET result = %s WHERE id = %s", (Jsonb(await build_result(conn, f["run_id"])), f["run_id"]))
        await audit(conn, scope.user_id, scope.role, "review_undo", {"finding": finding_id})
    return True
