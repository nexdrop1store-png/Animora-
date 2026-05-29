"""
LLM provider abstraction — direct Anthropic API vs Amazon Bedrock.

Animora ships against the direct Anthropic API in production (BYOK or
pooled key). For development and CI we also support Amazon Bedrock so
work continues when direct-API credits run out and so eval runs can
be billed to AWS instead of a personal Anthropic account.

The router and orchestrator stay provider-agnostic by working with
**logical model names** (the same IDs that work against the direct
Anthropic API: `claude-opus-4-7`, `claude-sonnet-4-6`,
`claude-haiku-4-5-20251001`). This module owns the single
"logical → Bedrock" translation map plus the small client-construction
fork. Adding a third provider in the future means one new branch here
and zero changes upstream.

## Capability deltas you need to know

  • **Opus 4.7 is not available on Bedrock for the current account.**
    Listed in the model catalog but a real invocation returns 403. We
    substitute **Opus 4.6** as the closest accessible peer — it's the
    direct predecessor and supports the same `adaptive` thinking +
    `output_config.effort` API, so no code-shape changes are needed.

  • **Bedrock requires the `us.` cross-region inference prefix** for
    on-demand invocation of every Claude 4.x model. Bare IDs return
    "Invocation of model ID ... with on-demand throughput isn't
    supported." See the mapping below.

  • **Extended thinking** is supported on Opus 4.6 via Bedrock with
    BOTH the new (`adaptive` + `output_config.effort`) and old
    (`enabled` + `budget_tokens`) shapes. Our code uses the new shape
    everywhere; no Bedrock-specific branch needed.

  • **Vision** (image content blocks) works on Bedrock the same as
    direct API — the artist's-eye check fires unchanged.

## Why this lives at module-level instead of a class

The translation is pure data; making it a class would invite per-session
state that doesn't exist. `client_for(provider, ...)` is the single
factory the rest of the codebase should call; `translate_model(name,
provider)` is the only public function the router needs.
"""

from __future__ import annotations

import os
from enum import Enum

import anthropic


class LLMProvider(str, Enum):
    ANTHROPIC = "anthropic"  # direct API, production default
    BEDROCK = "bedrock"      # AWS Bedrock, dev/CI


# Logical name → Bedrock model ID. Logical names match the direct-API
# model IDs (router.py returns these). Add a new entry here when:
#   • Anthropic ships a model we want to use, OR
#   • AWS publishes the Bedrock equivalent
#
# Verified accessible on this account 2026-05-24:
#   - us.anthropic.claude-haiku-4-5-20251001-v1:0
#   - us.anthropic.claude-sonnet-4-6
#   - us.anthropic.claude-opus-4-6-v1     (Opus 4.7 substitute)
#   - us.anthropic.claude-opus-4-5-20251101-v1:0
#
# Verified NOT accessible 2026-05-24:
#   - us.anthropic.claude-opus-4-7        (403 — request access via Bedrock console)
_BEDROCK_MODEL_MAP: dict[str, str] = {
    "claude-haiku-4-5-20251001": "us.anthropic.claude-haiku-4-5-20251001-v1:0",
    "claude-sonnet-4-6":         "us.anthropic.claude-sonnet-4-6",
    # Substitute: Opus 4.7 → Opus 4.6 (4.7 is gated on this account).
    # Capability gap is minor; both support adaptive thinking + tool use.
    "claude-opus-4-7":           "us.anthropic.claude-opus-4-6-v1",
    # Direct mappings for models the router may pick in future:
    "claude-opus-4-6":           "us.anthropic.claude-opus-4-6-v1",
    "claude-opus-4-5":           "us.anthropic.claude-opus-4-5-20251101-v1:0",
}


def provider_from_env(default: LLMProvider = LLMProvider.ANTHROPIC) -> LLMProvider:
    """Read ANIMORA_LLM_PROVIDER from the environment.

    `bedrock` selects Amazon Bedrock with the AWS_BEARER_TOKEN_BEDROCK
    long-term API key. `anthropic` (default) uses the direct API."""
    raw = os.environ.get("ANIMORA_LLM_PROVIDER", "").lower().strip()
    if raw == "bedrock":
        return LLMProvider.BEDROCK
    if raw == "anthropic":
        return LLMProvider.ANTHROPIC
    return default


def translate_model(logical_name: str, provider: LLMProvider) -> str:
    """Map a logical model name to the actual model ID expected by the
    chosen provider.

    For Anthropic, the logical name IS the model ID. For Bedrock, we
    look up the cross-region inference profile. Unknown names pass
    through unchanged — the SDK will surface the API error if the ID
    isn't valid, which is the right behavior for "we added a model and
    forgot to update the map."
    """
    if provider is LLMProvider.ANTHROPIC:
        return logical_name
    return _BEDROCK_MODEL_MAP.get(logical_name, logical_name)


def client_for(
    provider: LLMProvider,
    *,
    api_key: str = "",
    aws_region: str = "us-east-1",
) -> anthropic.AsyncAnthropic | anthropic.AsyncAnthropicBedrock:
    """Construct the right SDK client for the chosen provider.

    Anthropic: takes the API key directly.

    Bedrock: ignores api_key (Bedrock auth is the AWS_BEARER_TOKEN_BEDROCK
    env var read by boto3 / the SDK). Requires the env var to be set
    before this function is called — we don't pass the token explicitly
    because the SDK's BedrockClient picks it up from the standard AWS
    credential chain, which is the supported configuration path.
    """
    if provider is LLMProvider.ANTHROPIC:
        if not api_key:
            raise ValueError(
                "Anthropic provider requires a non-empty api_key. "
                "Set ANTHROPIC_API_KEY in .env or have the addon send "
                "one in the WS hello, or switch to ANIMORA_LLM_PROVIDER=bedrock."
            )
        return anthropic.AsyncAnthropic(api_key=api_key, max_retries=0)

    # Bedrock — the SDK reads AWS_BEARER_TOKEN_BEDROCK from the environment.
    # We verify it's set so the failure mode is "config error at startup"
    # rather than "first request returns 401."
    if not os.environ.get("AWS_BEARER_TOKEN_BEDROCK"):
        raise ValueError(
            "Bedrock provider requires AWS_BEARER_TOKEN_BEDROCK in the "
            "environment. See docs/BEDROCK.md for setup."
        )
    return anthropic.AsyncAnthropicBedrock(
        aws_region=aws_region,
        max_retries=0,
    )
