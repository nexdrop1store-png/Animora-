"""Auth server configuration."""

from __future__ import annotations

import os

from pydantic_settings import BaseSettings, SettingsConfigDict

_DEV_JWT_SENTINEL = "dev_secret_change_in_production"  # noqa: S105
_KNOWN_DEV_SECRETS = frozenset({
    _DEV_JWT_SENTINEL,
    "animora_local_dev_secret_do_not_use_in_production",
    "change_me_to_a_long_random_string",
    "secret", "changeme", "password", "",
})
_MIN_SECRET_LENGTH = 32
_DEV_MODE_ENV_FLAG = "ANIMORA_ENV"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+asyncpg://user:password@localhost:5432/animora"
    redis_url: str = "redis://localhost:6379/1"

    jwt_secret: str = _DEV_JWT_SENTINEL
    jwt_algorithm: str = "HS256"
    jwt_issuer: str = "animora-auth"
    jwt_audience: str = "animora-backend"
    access_token_expire_minutes: int = 60
    refresh_token_expire_days: int = 30

    website_url: str = "https://animora.tech"
    frontend_auth_url: str = "https://animora.tech/auth"

    # Abuse detection
    ipqs_api_key: str = ""
    disposable_domain_list_url: str = "https://raw.githubusercontent.com/disposable/disposable-email-domains/master/domains.txt"

    # Trial settings
    trial_duration_days: int = 3

    # Device limits per plan
    devices_trial: int = 1
    devices_standard: int = 2
    devices_studio: int = 10


def _enforce_secrets_safety(s: Settings) -> None:
    """See ai-backend/config.py for rationale. Same logic, same flag."""
    env_mode = os.environ.get(_DEV_MODE_ENV_FLAG, "").lower()
    is_dev_mode = env_mode in ("dev", "development", "local")
    if is_dev_mode:
        return
    if s.jwt_secret in _KNOWN_DEV_SECRETS:
        raise RuntimeError(
            "auth-server: JWT_SECRET matches a known dev placeholder. "
            "Refusing to start. Set JWT_SECRET to a long random string, "
            "or set ANIMORA_ENV=dev for local development."
        )
    if len(s.jwt_secret) < _MIN_SECRET_LENGTH:
        raise RuntimeError(
            f"auth-server: JWT_SECRET is too short ({len(s.jwt_secret)} chars, "
            f"need ≥{_MIN_SECRET_LENGTH})."
        )


settings = Settings()
_enforce_secrets_safety(settings)
