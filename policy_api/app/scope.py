"""Who is asking, and what they may do.

Open WebUI's filter signs a short-lived token for every message: user, role, entities, chat and turn.
Only Open WebUI and the Policy API hold the key, so Hermes and the model can carry the token but never
make or widen one.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from dataclasses import dataclass

ROLES = ("admin", "authority_analyst", "entity_manager", "viewer")

PERMISSIONS: dict[str, set[str]] = {
    "admin":             {"find", "analyze", "query", "list_runs", "explain", "review", "upload", "export"},
    "authority_analyst": {"find", "analyze", "query", "list_runs", "explain", "review", "upload", "export"},
    "entity_manager":    {"find", "query", "list_runs", "explain", "upload", "export"},
    "viewer":            {"find", "query", "list_runs", "explain"},
}


class ScopeError(Exception):
    """The token is missing, forged, expired, or doesn't allow this action."""


@dataclass(frozen=True)
class Scope:
    user_id: str
    role: str
    entities: tuple[str, ...]   # entities the person is assigned to; access covers everything below them
    chat_id: str
    turn_id: str
    exp: int

    def can(self, action: str) -> bool:
        return action in PERMISSIONS.get(self.role, set())

    def require(self, action: str) -> None:
        if not self.can(action):
            raise ScopeError(f"Your role ({self.role.replace('_', ' ')}) can't do this. Ask an admin if you need it.")


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def sign(key: str, *, user_id: str, role: str, entities: list[str], chat_id: str, turn_id: str,
         ttl_s: int = 600, now: float | None = None) -> str:
    if role not in ROLES:
        raise ValueError(f"unknown role {role!r}")
    body = {"sub": user_id, "role": role, "ent": sorted(set(entities)), "chat": chat_id, "turn": turn_id,
            "exp": int((now or time.time()) + ttl_s)}
    payload = _b64(json.dumps(body, separators=(",", ":"), sort_keys=True).encode())
    sig = _b64(hmac.new(key.encode(), payload.encode(), hashlib.sha256).digest())
    return f"{payload}.{sig}"


def verify(key: str, token: str, now: float | None = None) -> Scope:
    if not key:
        raise ScopeError("The scope signing key is not set.")
    try:
        payload, sig = token.strip().split(".")
    except (ValueError, AttributeError):
        raise ScopeError("Missing or malformed scope token.") from None
    want = _b64(hmac.new(key.encode(), payload.encode(), hashlib.sha256).digest())
    if not hmac.compare_digest(want, sig):
        raise ScopeError("Scope token signature is wrong.")
    body = json.loads(_unb64(payload))
    if body["exp"] < (now or time.time()):
        raise ScopeError("Scope token has expired. Send your message again.")
    if body["role"] not in ROLES:
        raise ScopeError("Unknown role in scope token.")
    return Scope(body["sub"], body["role"], tuple(body["ent"]), body["chat"], body["turn"], body["exp"])


def from_groups(groups: list[str]) -> tuple[str | None, list[str]]:
    """Map sign-in groups to (role, entities). Groups are named role:<role> and entity:<entity-id>.
    The strongest role wins if someone is in several."""
    roles = [g.split(":", 1)[1].replace("-", "_") for g in groups if g.startswith("role:")]
    ents = [g.split(":", 1)[1] for g in groups if g.startswith("entity:")]
    role = next((r for r in ROLES if r in roles), None)
    return role, ents
