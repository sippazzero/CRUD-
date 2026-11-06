"""Async SQLAlchemy engine and session factory."""

from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.config import get_settings


def normalize_database_url(database_url: str) -> str:
    """Adapt common managed-Postgres URL schemes to the async SQLAlchemy driver."""
    for scheme in ("postgres://", "postgresql://"):
        if database_url.startswith(scheme):
            return database_url.replace(scheme, "postgresql+asyncpg://", 1)
    return database_url


engine: AsyncEngine = create_async_engine(
    normalize_database_url(get_settings().database_url),
    pool_pre_ping=True,
)
SessionFactory: async_sessionmaker[AsyncSession] = async_sessionmaker(
    engine,
    expire_on_commit=False,
)


async def get_session() -> AsyncIterator[AsyncSession]:
    """Yield a session and close it so unfinished transactions are rolled back."""
    async with SessionFactory() as session:
        yield session
