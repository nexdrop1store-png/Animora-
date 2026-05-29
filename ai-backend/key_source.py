"""
API-key source abstraction.

There are two valid sources for the Anthropic API key used on a given
WS session:

  BYOK    The addon sent its own key in the WS `hello` message. The key
          is held in memory for the session lifetime, used for all calls,
          and discarded on disconnect. The user (or their org) is billed
          by Anthropic directly.

  POOLED  No key in the hello message. The backend uses its own master
          key (from AWS Secrets Manager / env var). The user pays Animora
          via Stripe; Animora pays Anthropic. Required for the commercial
          SaaS launch (blueprint §7).

`pick_key()` resolves which one to use for a session. The KeyDecision it
returns is the input to `AnthropicClient(api_key=...)`.

Operations / security notes:
  • The pooled master key is read from `settings.anthropic_api_key` at
    startup. If unset and a BYOK key isn't supplied either, the session
    is rejected before any LLM call.
  • Keys never appear in logs. Use `anthropic_client.fingerprint_key()`
    to log a sha256 prefix instead.
  • Per-session keys are NEVER written to Redis or Postgres. Memory only.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from .config import settings
from .llm_provider import LLMProvider, provider_from_env


class KeySource(str, Enum):
    BYOK = "byok"
    POOLED = "pooled"
    BEDROCK = "bedrock"  # Anthropic key unused; AnthropicClient runs against Bedrock


@dataclass(frozen=True)
class KeyDecision:
    api_key: str
    source: KeySource


class NoKeyAvailable(Exception):
    """Neither BYOK nor pooled key is configured. The session must close."""


def pick_key(byok_key: str | None) -> KeyDecision:
    """Choose which Anthropic key to use for this session.

    BYOK wins when the addon provides a non-empty key in the WS hello.
    Otherwise fall back to the backend's pooled key. If neither exists,
    raise NoKeyAvailable — the WS handler should close the connection
    with a clear error to the client.

    Special case: when ANIMORA_LLM_PROVIDER=bedrock, neither BYOK nor
    pooled Anthropic key is required (Bedrock auth is via
    AWS_BEARER_TOKEN_BEDROCK, server-side only). We return a sentinel
    decision so the rest of the WS hello path keeps working without
    needing an Anthropic key it would never use anyway."""
    if provider_from_env() is LLMProvider.BEDROCK:
        return KeyDecision(api_key="", source=KeySource.BEDROCK)

    if byok_key and byok_key.strip():
        return KeyDecision(api_key=byok_key.strip(), source=KeySource.BYOK)

    pooled = (settings.anthropic_api_key or "").strip()
    if pooled:
        return KeyDecision(api_key=pooled, source=KeySource.POOLED)

    raise NoKeyAvailable(
        "No Anthropic API key available — addon did not provide one and "
        "ANTHROPIC_API_KEY env is unset. (Set ANIMORA_LLM_PROVIDER=bedrock "
        "to skip Anthropic auth entirely and use Amazon Bedrock instead.)"
    )


def looks_like_anthropic_key(s: str) -> bool:
    """Cheap shape check used for client-side validation before we even
    try a network round-trip. NOT a security check — Anthropic is the
    authority on whether the key actually works."""
    if not s:
        return False
    s = s.strip()
    return s.startswith("sk-ant-") and len(s) > 20
