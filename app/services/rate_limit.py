"""PostgreSQL-coordinated token bucket for the provider's strict RPM ceiling."""

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import LLMRateLimitState

Clock = Callable[[], datetime]
Sleeper = Callable[[float], Awaitable[None]]


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
            now = self._clock()
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
