"""A rule-based stand-in for the chat model, for the demo and tests. It picks a tool from keywords and,
after a tool result, repeats the tool's display text, which is what the real model is told to do."""
from __future__ import annotations

import json
import re
import uuid


def _call(name: str, args: dict) -> dict:
    return {"role": "assistant", "content": "",
            "tool_calls": [{"id": f"call_{uuid.uuid4().hex[:12]}", "type": "function",
                            "function": {"name": name, "arguments": json.dumps(args)}}]}


def fake_completion(payload: dict) -> dict:
    msgs = payload["messages"]
    last = msgs[-1]
    if last.get("role") == "tool":
        try:
            content = json.loads(last.get("content") or "{}").get("display", "")
        except ValueError:
            content = last.get("content") or ""
        message = {"role": "assistant", "content": content}
    else:
        text = str(last.get("content") or "")
        low = text.lower()
        pid = re.search(r"[A-Z]{2,6}-\d{1,5}", text)
        tools = {t["function"]["name"] for t in payload.get("tools") or []}
        result = next((w for w in ("conflict", "gap", "stale", "compliant", "review") if w in low), None)
        if "explain" in low and "explain_finding" in tools:
            m = re.search(r"explain(?: the finding)? for (.+?)[?.]*$", text, re.I)
            message = _call("explain_finding", {"entity": m.group(1) if m else text})
        elif any(w in low for w in ("affect", "impact")) and "analyze_impact" in tools:
            message = _call("analyze_impact", {"policy": pid.group(0) if pid else text})
        elif any(w in low for w in ("status", "ready", "result", "show")) and "query_run" in tools:
            message = _call("query_run", {"result": result} if result else {})
        elif any(w in low for w in ("runs", "history")) and "list_runs" in tools:
            message = _call("list_runs", {})
        elif (pid or "find" in low) and "find_policy" in tools:
            message = _call("find_policy", {"query": pid.group(0) if pid else text})
        else:
            message = {"role": "assistant", "content": "I can only help with policy changes and their impact."}
    return {"choices": [{"index": 0, "message": message, "finish_reason": "tool_calls" if message.get("tool_calls") else "stop"}],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}}
