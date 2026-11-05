"""PostgreSQL ticket claiming and expired-lease recovery."""

from datetime import timedelta

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Ticket


async def claim_next(
    session: AsyncSession,
    *,
    lease: timedelta,
) -> Ticket | None:
    """Atomically lease the oldest due ticket that is still new."""
    async with session.begin():
        candidate = await session.scalar(
            select(Ticket)
            .where(
                Ticket.status == "new",
                Ticket.next_attempt_at <= func.now(),
            )
            .order_by(Ticket.created_at, Ticket.id)
            .limit(1)
            .with_for_update(skip_locked=True)
        )
        if candidate is None:
            return None

        statement = (
            update(Ticket)
            .where(Ticket.id == candidate.id, Ticket.status == "new")
            .values(
                status="generating",
                attempts=Ticket.attempts + 1,
                locked_until=func.now() + lease,
                updated_at=func.now(),
            )
            .returning(Ticket)
        )
        return (await session.execute(statement)).scalar_one()


async def recover_stuck(session: AsyncSession) -> int:
    """Return expired generating leases to the due queue and report the count.

    A time-bounded lease allows PostgreSQL to release row locks before an LLM
    call; holding a transaction lock for that remote call would block other
    workers and could not recover promptly after a process crash.
    """
    async with session.begin():
        result = await session.execute(
            update(Ticket)
            .where(
                Ticket.status == "generating",
                Ticket.locked_until < func.now(),
            )
            .values(
                status="new",
                next_attempt_at=func.now(),
                locked_until=None,
                updated_at=func.now(),
            )
        )
    return result.rowcount or 0
