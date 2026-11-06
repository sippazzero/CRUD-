"""Tests for managed PostgreSQL URL normalization and validation."""

import pytest
from app.config import Settings, normalize_database_url
from pydantic import ValidationError


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        (
            "postgres://user:pass@db:5432/app",
            "postgresql+asyncpg://user:pass@db:5432/app",
        ),
        (
            "postgresql://user:pass@db:5432/app",
            "postgresql+asyncpg://user:pass@db:5432/app",
        ),
        (
            "postgresql+asyncpg://user:pass@db:5432/app",
            "postgresql+asyncpg://user:pass@db:5432/app",
        ),
    ],
)
def test_settings_normalize_database_url(source: str, expected: str) -> None:
    """Settings normalize managed Postgres schemes once for every consumer."""
    assert Settings(database_url=source).database_url == expected


@pytest.mark.parametrize(
    ("database_url", "message"),
    [
        ("", "DATABASE_URL must not be empty"),
        ("postgresql://user:pass@db/app", "DATABASE_URL must include a port"),
    ],
)
def test_settings_reject_invalid_database_url(
    database_url: str,
    message: str,
) -> None:
    """Invalid managed database URLs fail with an actionable settings error."""
    with pytest.raises(ValidationError, match=message):
        Settings(database_url=database_url)


def test_normalize_database_url_is_config_level() -> None:
    """The public normalizer remains available to config consumers."""
    assert (
        normalize_database_url("postgres://user:pass@db:5432/app")
        == "postgresql+asyncpg://user:pass@db:5432/app"
    )
