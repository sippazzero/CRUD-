"""Environment-backed settings for the helpdesk service."""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration loaded exclusively from environment variables."""

    database_url: str = "postgresql+asyncpg://helpdesk:helpdesk@db:5432/helpdesk"
    log_level: str = "INFO"

    model_config = SettingsConfigDict(
        case_sensitive=False,
        env_file=None,
        extra="ignore",
    )


@lru_cache
def get_settings() -> Settings:
    """Return one settings instance so process configuration stays consistent."""
    return Settings()
