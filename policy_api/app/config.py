"""Settings, read from environment variables when a service starts."""
from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path


def _get(name: str, default: str = "") -> str:
    # An empty variable means "use the default": Compose passes unset optional settings as "".
    return (os.environ.get(name) or "").strip() or default


def _int(name: str, default: int) -> int:
    return int(_get(name, str(default)))


@dataclass(frozen=True)
class Settings:
    database_url: str
    scope_signing_key: str        # shared with the Open WebUI filter only; Hermes never has it
    tool_key: str                 # the Hermes plugin's key for /tools
    gate_key: str                 # Hermes's key for the model gate
    link_signing_key: str
    public_base_url: str
    allowed_hosts: tuple[str, ...]
    doc_store: str
    fake_models: bool
    chat_url: str
    chat_model: str
    judge_url: str
    judge_model: str
    escalate_url: str
    escalate_model: str
    embed_url: str
    rerank_url: str
    llm_api_key: str
    policy_id_pattern: str
    candidates_per_requirement: int
    semantic_k: int
    judge_concurrency: int
    max_tool_calls_per_turn: int
    max_model_calls_per_turn: int
    max_output_tokens: int
    seed: int
    review_alert_count: int
    review_alert_hours: int
    link_ttl_minutes: int
    system_prompt_path: str


def load_settings() -> Settings:
    return Settings(
        database_url=_get("DATABASE_URL"),
        scope_signing_key=_get("SCOPE_SIGNING_KEY"),
        tool_key=_get("TOOL_KEY"),
        gate_key=_get("GATE_KEY"),
        link_signing_key=_get("LINK_SIGNING_KEY"),
        public_base_url=_get("PUBLIC_BASE_URL", "http://localhost:8080").rstrip("/"),
        allowed_hosts=tuple(h.strip() for h in _get("ALLOWED_HOSTS", "localhost,127.0.0.1,policy-api,testserver").split(",") if h.strip()),
        doc_store=_get("DOC_STORE", "/docs"),
        fake_models=_get("FAKE_MODELS", "0").lower() in ("1", "true", "yes"),
        chat_url=_get("CHAT_URL", "http://chat:8000/v1"),
        chat_model=_get("CHAT_MODEL", "chat"),
        judge_url=_get("JUDGE_URL", "http://judge:8000/v1"),
        judge_model=_get("JUDGE_MODEL", "judge"),
        escalate_url=_get("ESCALATE_URL"),
        escalate_model=_get("ESCALATE_MODEL", "escalate"),
        embed_url=_get("EMBED_URL", "http://embed").rstrip("/"),
        rerank_url=_get("RERANK_URL").rstrip("/"),
        llm_api_key=_get("LLM_API_KEY"),
        policy_id_pattern=_get("POLICY_ID_PATTERN", r"[A-Z]{2,6}-\d{1,5}"),
        candidates_per_requirement=_int("CANDIDATES_PER_REQUIREMENT", 3),
        semantic_k=_int("SEMANTIC_K", 20),
        judge_concurrency=_int("JUDGE_CONCURRENCY", 16),
        max_tool_calls_per_turn=_int("MAX_TOOL_CALLS_PER_TURN", 3),
        max_model_calls_per_turn=_int("MAX_MODEL_CALLS_PER_TURN", 6),
        max_output_tokens=_int("MAX_OUTPUT_TOKENS", 1200),
        seed=_int("SEED", 7),
        review_alert_count=_int("REVIEW_ALERT_COUNT", 50),
        review_alert_hours=_int("REVIEW_ALERT_HOURS", 48),
        link_ttl_minutes=_int("LINK_TTL_MINUTES", 15),
        system_prompt_path=_get("SYSTEM_PROMPT_PATH", str(Path(__file__).parent / "system_prompt.md")),
    )


PROMPT_VERSION = "2026-10-20.1"  # bump when any judge or extract prompt changes; it is part of every cache key


def config_hash(settings: Settings) -> str:
    """Hash of everything that changes an answer. Stored on each run so a result can be traced to its setup."""
    parts = [PROMPT_VERSION, settings.judge_model, settings.escalate_model, settings.chat_model,
             str(settings.candidates_per_requirement), str(settings.semantic_k), str(settings.seed),
             Path(settings.system_prompt_path).read_text() if Path(settings.system_prompt_path).exists() else ""]
    return hashlib.sha256("\n".join(parts).encode()).hexdigest()[:16]
