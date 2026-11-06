"""Tests for managed PostgreSQL URL normalization."""

import pytest
from app.db import normalize_database_url


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
def test_normalize_database_url(source: str, expected: str) -> None:
    """Managed provider URLs use the async PostgreSQL driver expected by the app."""
    assert normalize_database_url(source) == expected
