"""AI backend configuration — loaded from environment variables."""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    anthropic_api_key: str = ""
    deepgram_api_key: str = ""

    redis_url: str = "redis://localhost:6379/0"

    jwt_secret: str = "dev_secret_change_in_production"
    jwt_algorithm: str = "HS256"

    # Rate limits (messages per hour / per day)
    rate_trial_hour: int = 20
    rate_trial_day: int = 100
    rate_standard_hour: int = 200
    rate_standard_day: int = 2000
    rate_studio_hour: int = 2000
    rate_studio_day: int = 20000

    # Script execution sandbox
    max_script_length: int = 8000
    max_poly_delta: int = 10_000_000
    max_render_samples: int = 10_000

    # Session history retention (days)
    history_trial_days: int = 3
    history_standard_days: int = 30
    history_studio_days: int = 180

    # Scene graph history
    scene_graph_history_size: int = 50


settings = Settings()
