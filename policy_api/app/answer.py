"""Answers are written by code, not by the model. The first line of every run answer is a one-line summary
that Open WebUI keeps when it shortens old messages."""
from __future__ import annotations

from .pipeline import LABEL, ORDER

MAX_ROWS = 10
_MD = set("\\`*_[]()<>!|~#")


def md(text) -> str:
    return "".join("\\" + ch if ch in _MD else ch for ch in str(text or "")).replace("\n", " ")


def short(run_id: str) -> str:
    return "R-" + str(run_id).replace("-", "")[:8]


def plural(n: int, one: str, many: str | None = None) -> str:
    return f"{n} {one if n == 1 else (many or one + 's')}"


def summary_line(run: dict) -> str:
    head = f"**Run {short(run['id'])}: {run['policy_id']} change**"
    res = run.get("result") or {}
    if run["status"] == "done" and res.get("entities") is not None:
        c = res.get("counts", {})
        bits = [f"{c[k]} {LABEL[k].lower()}" for k in ORDER if c.get(k)]
        return f"{head}, {plural(res['entities'], 'entity', 'entities')} affected or checked: " + (", ".join(bits) or "nothing affected")
    return f"{head}, {run['progress'].lower()}"


def render_status(run: dict, review_url: str | None) -> str:
    lines = [summary_line(run), ""]
    st = run["status"]
    if st in ("queued", "extracting"):
        lines.append("I'm reading the change now. Ask me again in a minute.")
    elif st == "awaiting_confirmation":
        n = sum(1 for r in run["requirements"] or [] if r.get("keep"))
        lines.append(f"I found {plural(n, 'requirement')} in the change. An analyst must confirm them before I check any entity.")
        lines.append(f"[Confirm the requirements]({review_url})" if review_url else "Ask an analyst to confirm them on the review page.")
    elif st in ("confirmed", "running"):
        lines.append(f"Checking now: {run['progress'].lower()}. Ask me again in a minute.")
    elif st == "failed":
        lines.append("This run stopped with an error. Try starting it again; if it fails twice, tell an admin.")
    return "\n".join(lines)


def render_result(run: dict, requirements: list[dict], rows: list[dict], download_url: str | None,
                  review_url: str | None, filtered: str = "") -> str:
    out = [summary_line(run), ""]
    if not rows:
        out.append("No entity is affected." if not filtered else f"Nothing matches {md(filtered)}.")
        return "\n".join(out)
    rows = sorted(rows, key=lambda r: (ORDER.index(r["result"]), r["entity"]))
    out += ["| Entity | Result | Their clause | Requirement | Why |", "|---|---|---|---|---|"]
    for r in rows[:MAX_ROWS]:
        clause = f"{r['policy_id']} §{r['number']}" if r.get("policy_id") else "none"
        req = requirements[r["requirement"]]["text"] if r["requirement"] < len(requirements) else ""
        out.append(f"| {md(r['entity'])} | {LABEL[r['result']]} | {md(clause)} | {md(req[:90])} | {md(r['why'][:140])} |")
    if len(rows) > MAX_ROWS:
        out.append(f"\nShowing {MAX_ROWS} of {len(rows)}. The workbook has every row.")
    pending = sum(1 for r in rows if r.get("pending"))
    if pending:
        out.append(f"\n{plural(pending, 'finding')} need a person to decide." + (f" [Open the review page]({review_url})" if review_url else ""))
    if download_url:
        out.append(f"\n[Download the workbook]({download_url})")
    return "\n".join(out)


OUT_OF_SCOPE = ("I can only help with policy changes and their impact: finding a policy, checking how a change "
                "affects the entities below it, and explaining a finding.")
BUDGET_SPENT = "I've reached the limit of steps for one message. Ask again more specifically, for example with a policy ID."
