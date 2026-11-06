"""PostgreSQL-coordinated token bucket for the provider's strict RPM ceiling."""

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import LLMDailyCallUsage, LLMRateLimitState

Clock = Callable[[], datetime]
Sleeper = Callable[[float], Awaitable[None]]


class PostgresDailyCallQuota:
    """Atomically reserve a bounded number of provider calls per UTC day."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        clock: Clock | None = None,
    ) -> None:
        """Create a quota backed by PostgreSQL and an injectable UTC clock."""
        self._session_factory = session_factory
        self._clock = clock or (lambda: datetime.now(UTC))

    async def try_acquire(self, cap: int) -> bool:
        """Reserve one call only when the UTC day's count remains below cap."""
        if cap <= 0:
            raise ValueError("A daily call quota requires a positive cap")

        utc_day = self._clock().astimezone(UTC).date()
        statement = (
            insert(LLMDailyCallUsage)
            .values(utc_day=utc_day, call_count=1)
            .on_conflict_do_update(
                index_elements=[LLMDailyCallUsage.utc_day],
                set_={
                    "call_count": LLMDailyCallUsage.call_count + 1,
                },
                where=LLMDailyCallUsage.call_count < cap,
            )
            .returning(LLMDailyCallUsage.call_count)
        )
        async with self._session_factory() as session:
            async with session.begin():
                result = await session.execute(statement)
                return result.scalar_one_or_none() is not None


class PostgresTokenBucket:
    """Limit provider calls to one per second across concurrent workers."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        clock: Clock | None = None,
        sleep: Sleeper = asyncio.sleep,
    ) -> None:
        """Create a limiter whose clock and waiting behavior can be tested."""
        self._session_factory = session_factory
        self._clock = clock or (lambda: datetime.now(UTC))
        self._sleep = sleep

    async def acquire(self) -> None:
        """Wait until a token is available and atomically consume it in PostgreSQL."""
        while True:
            wait_seconds = 0.0
            async with self._session_factory() as session:
                async with session.begin():
                    state = await session.scalar(
                        select(LLMRateLimitState)
                        .where(LLMRateLimitState.id == 1)
                        .with_for_update()
                    )
                    if state is None:
                        raise RuntimeError(
                            "LLM rate-limit state is missing; apply database migrations"
                        )

                    now = self._clock()
                    elapsed = max((now - state.updated_at).total_seconds(), 0.0)
                    state.tokens = min(1.0, state.tokens + elapsed)
                    if state.tokens >= 1.0:
                        state.tokens -= 1.0
                        state.updated_at = now
                    else:
                        wait_seconds = 1.0 - state.tokens
                        state.updated_at = now

            if wait_seconds == 0:
                return
            await self._sleep(wait_seconds)
