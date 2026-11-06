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
        (
            "postgres+asyncpg://user:pass@db:5432/app",
            "postgres+asyncpg://user:pass@db:5432/app",
        ),
    ],
)
def test_normalize_database_url(source: str, expected: str) -> None:
    """The normalizer handles postgres/postgresql and preserves asyncpg schemes."""
    assert normalize_database_url(source) == expected


@pytest.mark.parametrize(
    ("database_url", "message"),
    [
        ("", "DATABASE_URL must not be empty"),
        ("   ", "DATABASE_URL must not be empty"),
        ("postgresql://user:pass@db/app", "DATABASE_URL must include a port"),
        ("postgres://user:pass@db/app", "DATABASE_URL must include a port"),
    ],
)
def test_settings_reject_invalid_database_url(
    database_url: str,
    message: str,
) -> None:
    """Invalid managed database URLs fail with an actionable settings error."""
    with pytest.raises(ValidationError, match=message):
        Settings(database_url=database_url)


@pytest.mark.parametrize(
    "raw_url",
    [
        "postgres://user:pass@db:5432/helpdesk",
        "postgresql://user:pass@db:5432/helpdesk",
        "postgresql+asyncpg://user:pass@db:5432/helpdesk",
    ],
)
def test_alembic_reads_same_normalized_database_url_as_app(
    raw_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Alembic configuration receives the app's normalized asyncpg DATABASE_URL."""
    from alembic.config import Config
    from app.config import get_settings

    monkeypatch.setenv("DATABASE_URL", raw_url)
    get_settings.cache_clear()
    try:
        settings = get_settings()
        expected = "postgresql+asyncpg://user:pass@db:5432/helpdesk"
        assert settings.database_url == expected

        config = Config("alembic.ini")
        database_url = settings.database_url.replace("%", "%%")
        config.set_main_option("sqlalchemy.url", database_url)
        assert config.get_main_option("sqlalchemy.url") == expected
    finally:
        get_settings.cache_clear()
