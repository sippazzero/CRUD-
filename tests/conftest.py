"""Shared fixtures that verify integration tests use the Compose PostgreSQL."""

import pytest_asyncio
from app.config import get_settings
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine


@pytest_asyncio.fixture(scope="session", loop_scope="session", autouse=True)
async def postgres_is_available() -> None:
    """Fail early unless the configured real PostgreSQL service is reachable."""
    engine = create_async_engine(get_settings().database_url, pool_pre_ping=True)
    try:
        async with engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
    finally:
        await engine.dispose()
