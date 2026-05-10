"""Auth server configuration."""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+asyncpg://user:password@localhost:5432/animora"
    redis_url: str = "redis://localhost:6379/1"

    jwt_secret: str = "dev_secret_change_in_production"
    jwt_algorithm: str = "HS256"
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


settings = Settings()
