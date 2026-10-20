"""
title: Policy scope
description: Signs who is asking into every message, shortens old answers, and checks replies against tool results.
version: 0.1.0
"""
# Install in Open WebUI: Admin Panel > Functions > Import, then turn it on and make it Global.
# It needs the same SCOPE_SIGNING_KEY as the Policy API. Hermes never sees this key.

import base64
import hashlib
import hmac
import json
import time
import urllib.request

from pydantic import BaseModel, Field

ROLES = ("admin", "authority_analyst", "entity_manager", "viewer")
KEEP_MESSAGES = 12
NO_ACCESS = "You don't have access to the policy assistant yet. Ask an admin to add you to a role group and an entity group."


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def sign(key: str, user_id: str, role: str, entities: list, chat_id: str, turn_id: str, ttl_s: int = 600) -> str:
    # Must match policy_api/app/scope.py sign().
    body = {"sub": user_id, "role": role, "ent": sorted(set(entities)), "chat": chat_id, "turn": turn_id,
            "exp": int(time.time() + ttl_s)}
    payload = _b64(json.dumps(body, separators=(",", ":"), sort_keys=True).encode())
    return payload + "." + _b64(hmac.new(key.encode(), payload.encode(), hashlib.sha256).digest())


def from_groups(names: list) -> tuple:
    roles = [n.split(":", 1)[1].replace("-", "_") for n in names if n.startswith("role:")]
    ents = [n.split(":", 1)[1] for n in names if n.startswith("entity:")]
    return next((r for r in ROLES if r in roles), None), ents


class Filter:
    class Valves(BaseModel):
        scope_signing_key: str = Field(default="", description="Same value as SCOPE_SIGNING_KEY on the Policy API")
        policy_api_url: str = Field(default="http://policy-api:8000")

    def __init__(self):
        self.valves = self.Valves()

    async def _token(self, user: dict, metadata: dict) -> str | None:
        from open_webui.models.groups import Groups
        groups = await Groups.get_groups_by_member_id(user["id"])
        role, ents = from_groups([g.name for g in groups])
        if not role or not ents:
            return None
        return sign(self.valves.scope_signing_key, user["id"], role, ents,
                    str(metadata.get("chat_id") or "no-chat"), str(metadata.get("message_id") or "no-turn"))

    async def inlet(self, body: dict, __user__: dict, __metadata__: dict = None) -> dict:
        token = await self._token(__user__, __metadata__ or {})
        if not token:
            raise Exception(NO_ACCESS)
        msgs = [m for m in body.get("messages", []) if m.get("role") != "system"]
        for m in msgs[:-1]:  # old run answers keep only their summary line; details stay in the database
            if m.get("role") == "assistant" and isinstance(m.get("content"), str) and m["content"].startswith("**Run R-"):
                m["content"] = m["content"].split("\n", 1)[0]
        msgs = msgs[-KEEP_MESSAGES:]
        # The token rides on the newest user message too, in case Hermes drops client system messages.
        # The model gate strips it out of every message before the model sees anything.
        if msgs and msgs[-1].get("role") == "user" and isinstance(msgs[-1].get("content"), str):
            msgs[-1] = {**msgs[-1], "content": f"{msgs[-1]['content']}\n[[scope:{token}]]"}
        body["messages"] = [{"role": "system", "content": f"[[scope:{token}]]"}] + msgs
        for k in ("temperature", "top_p", "seed", "max_tokens"):  # sampling is fixed by the Policy API
            body.pop(k, None)
        return body

    async def outlet(self, body: dict, __user__: dict, __metadata__: dict = None) -> dict:
        msgs = body.get("messages", [])
        if not msgs or msgs[-1].get("role") != "assistant":
            return body
        token = await self._token(__user__, __metadata__ or {})
        if not token:
            return body
        req = urllib.request.Request(self.valves.policy_api_url + "/check-reply", method="POST",
                                     data=json.dumps({"token": token, "reply": msgs[-1].get("content") or ""}).encode(),
                                     headers={"Content-Type": "application/json", "Host": "policy-api"})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                verdict = json.loads(r.read())
        except Exception:
            msgs[-1]["content"] = "I couldn't check this answer, so I'm not showing it. Please ask again."
            return body
        if not verdict.get("ok"):
            msgs[-1]["content"] = verdict.get("replacement") or "I can only help with policy changes and their impact."
        return body
