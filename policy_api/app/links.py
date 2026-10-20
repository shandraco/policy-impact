"""Links the person can open in a browser: the review page and workbook downloads.

Each link carries a freshly signed scope token with a short life, so it works only for the person it was
made for, only within their access, and only for a few minutes.
"""
from __future__ import annotations

from urllib.parse import quote

from .config import Settings
from .scope import Scope, sign


def _token(settings: Settings, scope: Scope, ttl_s: int) -> str:
    return sign(settings.scope_signing_key, user_id=scope.user_id, role=scope.role, entities=list(scope.entities),
                chat_id=scope.chat_id, turn_id=scope.turn_id, ttl_s=ttl_s)


def review_login_url(settings: Settings, scope: Scope) -> str:
    return f"{settings.public_base_url}/review/login?t={quote(_token(settings, scope, settings.link_ttl_minutes * 60))}"


def download_url(settings: Settings, run_id: str, scope: Scope) -> str:
    return f"{settings.public_base_url}/exports/{run_id}.xlsx?t={quote(_token(settings, scope, settings.link_ttl_minutes * 60))}"


def session_token(settings: Settings, scope: Scope, hours: int = 8) -> str:
    return _token(settings, scope, hours * 3600)
