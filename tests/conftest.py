"""Configure host-run integration tests against the published PostgreSQL port."""

import asyncio
import os
import subprocess
import sys
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio
from app.models import LLMRateLimitState, Ticket
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, func, text, update
from sqlalchemy.engine import URL, make_url
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_DEFAULT_TEST_DATABASE_URL = URL.create(
    "postgresql+asyncpg",
    username=os.getenv("POSTGRES_USER", "helpdesk"),
    password=os.getenv("POSTGRES_PASSWORD", "change-me-local-only"),
    host="localhost",
    port=int(os.getenv("POSTGRES_PORT", "5432")),
    database=os.getenv("TEST_DATABASE_NAME", "helpdesk_test"),
).render_as_string(hide_password=False)
TEST_DATABASE_URL = os.getenv("TEST_DATABASE_URL", _DEFAULT_TEST_DATABASE_URL)

# Application modules imported by tests must use the same host-reachable database.
os.environ["DATABASE_URL"] = TEST_DATABASE_URL


@pytest_asyncio.fixture(loop_scope="session")
async def api_client() -> AsyncIterator[AsyncClient]:
    """Exercise the FastAPI app in-process while retaining real database I/O."""
    from app.main import app

    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport,
        base_url="http://test",
        timeout=2,
    ) as client:
        yield client


@pytest.fixture(scope="session", autouse=True)
def migrate_test_database() -> None:
    """Apply the schema once so integration tests always use the current contract."""
    asyncio.run(_ensure_test_database())
    subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        check=True,
        cwd=_PROJECT_ROOT,
        env=os.environ.copy(),
    )


async def _ensure_test_database() -> None:
    """Create the isolated real-Postgres test database when it does not exist."""
    test_url = make_url(TEST_DATABASE_URL)
    if not test_url.database:
        raise ValueError("TEST_DATABASE_URL must include a database name")
    admin_engine = create_async_engine(
        test_url.set(database="postgres"),
        isolation_level="AUTOCOMMIT",
    )
    try:
        async with admin_engine.connect() as connection:
            exists = await connection.scalar(
                text("SELECT 1 FROM pg_database WHERE datname = :database_name"),
                {"database_name": test_url.database},
            )
            if not exists:
                escaped_name = test_url.database.replace('"', '""')
                await connection.exec_driver_sql(f'CREATE DATABASE "{escaped_name}"')
    finally:
        await admin_engine.dispose()


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def postgres_engine(
    migrate_test_database: None,
) -> AsyncIterator[AsyncEngine]:
    """Share a real PostgreSQL engine after the session migration completes."""
    assert migrate_test_database is None
    engine = create_async_engine(TEST_DATABASE_URL, pool_pre_ping=True)
    try:
        async with engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
        yield engine
    finally:
        await engine.dispose()


@pytest_asyncio.fixture(loop_scope="session", autouse=True)
async def clean_ticket_rows(postgres_engine: AsyncEngine) -> AsyncIterator[None]:
    """Isolate tests by removing their persisted tickets before and after each run."""
    async with postgres_engine.begin() as connection:
        await connection.execute(delete(Ticket))
    yield
    async with postgres_engine.begin() as connection:
        await connection.execute(delete(Ticket))


@pytest_asyncio.fixture(loop_scope="session", autouse=True)
async def reset_rate_limiter(postgres_engine: AsyncEngine) -> AsyncIterator[None]:
    """Prevent one rate-limit integration test from delaying unrelated cases."""
    async with postgres_engine.begin() as connection:
        await connection.execute(
            update(LLMRateLimitState)
            .where(LLMRateLimitState.id == 1)
            .values(tokens=1, updated_at=func.now())
        )
    yield
    async with postgres_engine.begin() as connection:
        await connection.execute(
            update(LLMRateLimitState)
            .where(LLMRateLimitState.id == 1)
            .values(tokens=1, updated_at=func.now())
        )
