"""The model gate: Hermes's model provider points here instead of at vLLM.

For every model call it:
  - finds and checks the scope token Open WebUI added to the conversation, and strips it out;
  - replaces every system message with the approved prompt plus the chat-state block;
  - keeps the current turn and a short window of earlier text messages;
  - removes any tool not on the allowlist, and hides the scope_token argument from the model;
  - fixes the sampling: temperature 0, a fixed seed, capped output, one tool call at a time;
  - counts model calls per turn and stops at the limit;
  - after the model answers, adds the scope token to each allowed tool call, so the plugin can pass it on.
The model never sees the token, so it can't copy, change or leak it.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import time
import uuid
from pathlib import Path
from typing import Any

import httpx

from .answer import BUDGET_SPENT, OUT_OF_SCOPE
from .db import audit
from .pipeline import Services
from .scope import Scope, ScopeError, verify
from .tools import HANDLERS, count_call, get_state, state_block

log = logging.getLogger(__name__)
TOKEN_RE = re.compile(r"\[\[scope:([A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+)\]\]")
HISTORY_MESSAGES = 12   # earlier user/assistant text messages kept (about 6 turns)
NO_SCOPE = "I can't confirm who you are. Reload the page and send your message again."


def _text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(p.get("text", "") for p in content if isinstance(p, dict))
    return ""


def find_token(messages: list[dict]) -> str | None:
    found = None
    for m in messages:
        for t in TOKEN_RE.findall(_text(m.get("content"))):
            found = t
    return found


def _strip_token(content: Any) -> Any:
    if isinstance(content, str):
        return TOKEN_RE.sub("", content).strip()
    if isinstance(content, list):
        return [{**p, "text": TOKEN_RE.sub("", p.get("text", ""))} if isinstance(p, dict) and "text" in p else p for p in content]
    return content


def shorten_run_answer(text: str) -> str:
    """Old run answers keep only their one-line summary; the details stay in the database."""
    return text.split("\n", 1)[0] if text.startswith("**Run R-") else text


def build_messages(messages: list[dict], system_prompt: str, state: dict) -> list[dict]:
    convo = [m for m in messages if m.get("role") not in ("system", "developer")]
    last_user = max((i for i, m in enumerate(convo) if m.get("role") == "user"), default=len(convo))
    earlier = [m for m in convo[:last_user]
               if m.get("role") in ("user", "assistant") and not m.get("tool_calls") and _text(m.get("content")).strip()]
    earlier = [{"role": m["role"], "content": shorten_run_answer(_strip_token(_text(m.get("content"))))}
               for m in earlier[-HISTORY_MESSAGES:]]
    current = [{**m, "content": _strip_token(m.get("content"))} for m in convo[last_user:]]
    return [{"role": "system", "content": f"{system_prompt}\n\n{state_block(state)}"}] + earlier + current


def filter_tools(tools: list[dict] | None) -> list[dict]:
    out = []
    for t in tools or []:
        fn = t.get("function") or {}
        if t.get("type") != "function" or fn.get("name") not in HANDLERS:
            continue
        params = json.loads(json.dumps(fn.get("parameters") or {"type": "object", "properties": {}}))
        params.get("properties", {}).pop("scope_token", None)
        if "required" in params:
            params["required"] = [r for r in params["required"] if r != "scope_token"]
        out.append({"type": "function", "function": {**fn, "parameters": params}})
    return out


def add_token_to_calls(message: dict, token: str) -> dict:
    calls = []
    for c in message.get("tool_calls") or []:
        fn = c.get("function") or {}
        if fn.get("name") not in HANDLERS:
            continue  # the model named a tool it doesn't have; drop it
        try:
            args = json.loads(fn.get("arguments") or "{}")
        except ValueError:
            args = {}
        args = args if isinstance(args, dict) else {}
        args["scope_token"] = token
        calls.append({**c, "function": {**fn, "arguments": json.dumps(args)}})
    message = {**message, "tool_calls": calls or None}
    if not calls:
        message.pop("tool_calls")
        if not (message.get("content") or "").strip():
            message["content"] = OUT_OF_SCOPE
    return message


def completion(model: str, message: dict, finish: str = "stop", usage: dict | None = None) -> dict:
    return {"id": f"chatcmpl-{uuid.uuid4().hex[:24]}", "object": "chat.completion", "created": int(time.time()),
            "model": model, "choices": [{"index": 0, "message": message,
                                         "finish_reason": "tool_calls" if message.get("tool_calls") else finish}],
            "usage": usage or {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}}


def as_sse(resp: dict) -> list[str]:
    """The same answer as a stream, for clients that asked for one. Sent in one piece."""
    base = {k: resp[k] for k in ("id", "created", "model")} | {"object": "chat.completion.chunk"}
    msg = resp["choices"][0]["message"]
    delta: dict[str, Any] = {"role": "assistant", "content": msg.get("content") or ""}
    if msg.get("tool_calls"):
        delta["tool_calls"] = [{"index": i, **c} for i, c in enumerate(msg["tool_calls"])]
    chunks = [{**base, "choices": [{"index": 0, "delta": delta, "finish_reason": None}]},
              {**base, "choices": [{"index": 0, "delta": {}, "finish_reason": resp["choices"][0]["finish_reason"]}]}]
    return [f"data: {json.dumps(c)}\n\n" for c in chunks] + ["data: [DONE]\n\n"]


class Gate:
    def __init__(self, svc: Services) -> None:
        self.svc = svc
        s = svc.settings
        self.prompt = Path(s.system_prompt_path).read_text().strip()
        self.prompt_hash = hashlib.sha256(self.prompt.encode()).hexdigest()[:16]
        self.http = httpx.AsyncClient(base_url=s.chat_url, timeout=180,
                                      headers={"Authorization": f"Bearer {s.llm_api_key or 'unused'}"})

    async def aclose(self) -> None:
        await self.http.aclose()

    async def upstream(self, payload: dict) -> dict:
        if self.svc.settings.fake_models:
            from .fake_chat import fake_completion
            return fake_completion(payload)
        r = await self.http.post("/chat/completions", json=payload)
        r.raise_for_status()
        return r.json()

    async def chat(self, body: dict) -> dict:
        s = self.svc.settings
        messages = body.get("messages") or []
        token = find_token(messages)
        try:
            scope: Scope = verify(s.scope_signing_key, token or "")
        except ScopeError:
            return completion(s.chat_model, {"role": "assistant", "content": NO_SCOPE})
        if not await count_call(self.svc, scope, "model_calls", s.max_model_calls_per_turn):
            return completion(s.chat_model, {"role": "assistant", "content": BUDGET_SPENT})
        state = await get_state(self.svc, scope)
        payload: dict[str, Any] = {
            "model": s.chat_model,
            "messages": build_messages(messages, self.prompt, state),
            "temperature": 0, "seed": s.seed, "stream": False,
            "max_tokens": min(int(body.get("max_tokens") or body.get("max_completion_tokens") or s.max_output_tokens),
                              s.max_output_tokens),
        }
        tools = filter_tools(body.get("tools"))
        if tools:
            payload |= {"tools": tools, "tool_choice": "auto", "parallel_tool_calls": False}
        resp = await self.upstream(payload)
        msg = add_token_to_calls(resp["choices"][0]["message"], token or "")
        async with self.svc.db.system() as conn:
            await audit(conn, scope.user_id, scope.role, "model_call",
                        {"chat": scope.chat_id, "turn": scope.turn_id, "prompt": self.prompt_hash,
                         "messages": len(payload["messages"]), "tools_called": [c["function"]["name"] for c in msg.get("tool_calls") or []],
                         "usage": resp.get("usage")}, self.svc.config_hash)
        return completion(s.chat_model, msg, resp["choices"][0].get("finish_reason") or "stop", resp.get("usage"))
