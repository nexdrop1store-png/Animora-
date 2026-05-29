"""AI backend configuration — loaded from environment variables."""

from __future__ import annotations

import os
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

# .env lives next to this file (ai-backend/.env). Anchoring on __file__
# means imports work no matter what CWD the process was started from —
# previously the default `env_file=".env"` only resolved when the process
# ran from inside ai-backend/, silently using defaults from anywhere else.
_ENV_FILE = Path(__file__).resolve().parent / ".env"

# Sentinel value used as the in-code default. If `jwt_secret` ever equals
# this in a production deploy, the safety check refuses to start.
_DEV_JWT_SENTINEL = "dev_secret_change_in_production"  # noqa: S105

# Known dev-only secrets that ALSO must be rejected in production. Any
# string in this set is treated identically to the bare sentinel above.
# Includes the placeholder we ship in the local .env for dev_server.py.
_KNOWN_DEV_SECRETS = frozenset({
    _DEV_JWT_SENTINEL,
    "animora_local_dev_secret_do_not_use_in_production",
    "change_me_to_a_long_random_string",
    "secret",
    "changeme",
    "password",
    "",
})

# Minimum length for a secret to be considered production-grade.
_MIN_SECRET_LENGTH = 32

# Set ANIMORA_ENV=dev to opt out of the safety check. Any other value
# (or unset) means "production-shaped" and the safety check fires.
_DEV_MODE_ENV_FLAG = "ANIMORA_ENV"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=str(_ENV_FILE), extra="ignore")

    anthropic_api_key: str = ""
    deepgram_api_key: str = ""

    # LLM provider switch. `anthropic` = direct API (production default).
    # `bedrock` = Amazon Bedrock with AWS_BEARER_TOKEN_BEDROCK long-term
    # API key. See docs/BEDROCK.md. When `bedrock`, ANTHROPIC_API_KEY is
    # ignored (Bedrock has its own auth) and BYOK from the addon is also
    # ignored — Bedrock keys are server-side only.
    animora_llm_provider: str = "anthropic"

    # Bedrock auth — only read when animora_llm_provider == "bedrock".
    # The bearer token is a long-term API key minted in the AWS Bedrock
    # console (format `ABSK...`, base64-encoded account+secret). The
    # Anthropic SDK reads AWS_BEARER_TOKEN_BEDROCK directly from os.environ,
    # so we copy this setting into the env at startup (see __init__.py).
    aws_bearer_token_bedrock: str = ""
    bedrock_aws_region: str = "us-east-1"

    redis_url: str = "redis://localhost:6379/0"

    jwt_secret: str = _DEV_JWT_SENTINEL
    jwt_algorithm: str = "HS256"

    # Rate limits (messages per hour / per day)
    rate_trial_hour: int = 20
    rate_trial_day: int = 100
    rate_standard_hour: int = 200
    rate_standard_day: int = 2000
    rate_studio_hour: int = 2000
    rate_studio_day: int = 20000

    # WebSocket hardening (H4, H5 from the audit pass).
    #
    # allowed_ws_origins: comma-separated allowlist for the `Origin` header
    # on WebSocket upgrade. The local Animora addon doesn't send a
    # standard browser Origin header at all (it's a Python websocket-client
    # connection) — that's the empty-string case, which we permit. Browsers
    # MUST match one of the listed origins.
    allowed_ws_origins: str = "https://animora.tech,http://localhost:3000"

    # Per-frame size cap for incoming WebSocket binary frames. The viewport
    # stream sends JPEG-encoded frames; a 4K frame at q=80 is < 2 MB, so
    # 8 MB is generous. Frames exceeding this are dropped silently with a
    # log warning. The cap blocks a malicious client from OOMing the
    # backend by sending one 100 MB frame.
    ws_max_binary_frame_bytes: int = 8 * 1024 * 1024  # 8 MB

    # Per-session message rate limit applied to EVERY incoming message
    # type (not just user_message which already has its own quota check
    # against plan-based limits). Stops floods of scene_graph / hd_capture
    # / interrupt frames from bypassing the user-message rate limit.
    ws_messages_per_minute: int = 1000

    # JWT validation (H5). The auth-server (when deployed) MUST mint
    # tokens with these claims; the backend rejects tokens missing them.
    jwt_issuer: str = "animora-auth"
    jwt_audience: str = "animora-backend"

    # Script execution sandbox.
    #
    # max_script_length is the hard cap on the bpy script the LLM hands us.
    # Sized to comfortably exceed Opus 4.7's 32 k max_tokens output budget:
    # 32 768 tokens × ~3.5 chars/token ≈ 115 KB. We use 160 KB to give
    # headroom for whitespace + the small amount of explanation text that
    # also lives inside the assistant message.
    #
    # Trade-off: a longer cap means a malicious script has more room to
    # hide payloads. The AST denylist in quality_enforcer.py is independent
    # of length; it still catches every banned import/call regardless of
    # script size. The cap is a runaway-output safety net, not the security
    # boundary.
    max_script_length: int = 160_000
    max_poly_delta: int = 10_000_000
    max_render_samples: int = 10_000

    # Session history retention (days)
    history_trial_days: int = 3
    history_standard_days: int = 30
    history_studio_days: int = 180

    # Scene graph history
    scene_graph_history_size: int = 50


def _enforce_secrets_safety(s: Settings) -> None:
    """Refuse to start if a deploy left a known dev JWT secret active.

    Two ways to satisfy this check:
      • Set JWT_SECRET to a long random string (≥32 chars, not in the
        known-dev list) via env or .env, OR
      • Set ANIMORA_ENV=dev to explicitly opt into local-dev mode

    Production deploys MUST set JWT_SECRET to a production-grade value.
    The dev sentinels are public in this source file and in the local
    .env we ship for `dev_server.py` — anyone with the repo could forge
    JWTs against a deploy that fell back to them.
    """
    env_mode = os.environ.get(_DEV_MODE_ENV_FLAG, "").lower()
    is_dev_mode = env_mode in ("dev", "development", "local")
    if is_dev_mode:
        return  # explicit opt-in; trust the operator

    if s.jwt_secret in _KNOWN_DEV_SECRETS:
        raise RuntimeError(
            "JWT_SECRET matches a known dev/placeholder value. Refusing to "
            "start. Set JWT_SECRET to a long random string in your "
            "environment (≥32 chars), or set ANIMORA_ENV=dev to opt into "
            "local-dev mode explicitly. See ai-backend/.env.example."
        )
    if len(s.jwt_secret) < _MIN_SECRET_LENGTH:
        raise RuntimeError(
            f"JWT_SECRET is too short ({len(s.jwt_secret)} chars, need "
            f"≥{_MIN_SECRET_LENGTH}). Use a long random string. Set "
            f"ANIMORA_ENV=dev to opt out of this check for local development."
        )


settings = Settings()
_enforce_secrets_safety(settings)


def _propagate_bedrock_env(s: Settings) -> None:
    """Mirror .env-loaded Bedrock + provider values into os.environ so
    they're visible to modules that read directly from the environment:
      • The Anthropic SDK's BedrockClient reads AWS_BEARER_TOKEN_BEDROCK
        via its default credential chain.
      • `llm_provider.provider_from_env` reads ANIMORA_LLM_PROVIDER to
        decide which client to construct.
    pydantic-settings only populates the `Settings` instance; this is the
    bridge to the env-reading paths. All operations are no-ops when the
    setting isn't populated."""
    if s.aws_bearer_token_bedrock and not os.environ.get("AWS_BEARER_TOKEN_BEDROCK"):
        os.environ["AWS_BEARER_TOKEN_BEDROCK"] = s.aws_bearer_token_bedrock
    if s.animora_llm_provider and not os.environ.get("ANIMORA_LLM_PROVIDER"):
        os.environ["ANIMORA_LLM_PROVIDER"] = s.animora_llm_provider


_propagate_bedrock_env(settings)
