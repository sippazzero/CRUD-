"""Configure host-run integration tests against the published PostgreSQL port."""

import os
import subprocess
import sys
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio
from app.models import Ticket
from sqlalchemy import delete, text
from sqlalchemy.engine import URL
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_DEFAULT_TEST_DATABASE_URL = URL.create(
    "postgresql+asyncpg",
    username=os.getenv("POSTGRES_USER", "helpdesk"),
    password=os.getenv("POSTGRES_PASSWORD", "helpdesk_dev"),
    host="localhost",
    port=int(os.getenv("POSTGRES_PORT", "5432")),
    database=os.getenv("POSTGRES_DB", "helpdesk"),
).render_as_string(hide_password=False)
TEST_DATABASE_URL = os.getenv("TEST_DATABASE_URL", _DEFAULT_TEST_DATABASE_URL)

# Application modules imported by tests must use the same host-reachable database.
os.environ["DATABASE_URL"] = TEST_DATABASE_URL


@pytest.fixture(scope="session", autouse=True)
def migrate_test_database() -> None:
    """Apply the schema once so integration tests always use the current contract."""
    subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        check=True,
        cwd=_PROJECT_ROOT,
        env=os.environ.copy(),
    )


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def postgres_engine(
    migrate_test_database: None,
) -> AsyncIterator[AsyncEngine]:
    """Share a real PostgreSQL engine after the session migration completes."""
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
