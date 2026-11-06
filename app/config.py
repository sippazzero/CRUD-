"""Environment-backed settings for the helpdesk service."""

from functools import lru_cache
from urllib.parse import urlsplit

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration loaded exclusively from environment variables."""

    database_url: str = (
        "postgresql+asyncpg://helpdesk:change-me-local-only@db:5432/helpdesk"
    )
    log_level: str = "INFO"
    llm_api_url: str = "https://api.openai.com/v1/chat/completions"
    llm_model: str = "gpt-4o-mini"
    llm_api_key: str | None = None
    llm_static_fallback_enabled: bool = True
    llm_timeout_seconds: float = 30.0
    # Thirty tasks can keep the one-per-second bucket fed even at 30-second latency.
    worker_concurrency: int = 30
    worker_poll_seconds: float = 1.0
    worker_lease_seconds: int = 120
    worker_max_attempts: int = 5
    worker_backoff_base_seconds: float = 5.0
    worker_backoff_max_seconds: float = 300.0
    sender_timeout_seconds: float = 10.0
    sender_max_attempts: int = 5
    sender_lease_seconds: int = 300
    helpdesk_allowed_reply_hosts: str = "helpdesk-mock"
    mock_failure_rate: float = 0.0
    mock_hang_rate: float = 0.0
    mock_hang_seconds: float = 15.0
    mock_fail_before_record: int = 0
    mock_duplicate_mode: bool = False
    mock_api_url: str = "http://api:8000/webhooks/helpdesk"

    @field_validator("llm_api_key", mode="before")
    @classmethod
    def normalize_empty_api_key(cls, value: str | None) -> str | None:
        """Treat an empty Compose environment value as an absent credential."""
        return value or None

    def is_reply_url_allowed(self, url: str) -> bool:
        """Allow callback requests only to explicitly configured hostnames."""
        try:
            parsed_url = urlsplit(url)
            hostname = parsed_url.hostname
        except ValueError:
            return False

        allowed_hosts = {
            host.strip().lower()
            for host in self.helpdesk_allowed_reply_hosts.split(",")
            if host.strip()
        }
        return (
            parsed_url.scheme.lower() in {"http", "https"}
            and hostname is not None
            and hostname.lower() in allowed_hosts
            and parsed_url.username is None
            and parsed_url.password is None
        )

    model_config = SettingsConfigDict(
        case_sensitive=False,
        env_file=None,
        extra="ignore",
    )


@lru_cache
def get_settings() -> Settings:
    """Return one settings instance so process configuration stays consistent."""
    return Settings()
