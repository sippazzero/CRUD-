"""PostgreSQL ticket claiming and expired-lease recovery."""

from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import UUID

from sqlalchemy import case, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Ticket


@dataclass(frozen=True)
class ClaimedTicket:
    """Immutable ticket details captured before the claim transaction closes."""

    id: UUID
    ticket_id: str
    message: str
    attempts: int


@dataclass(frozen=True)
class DeliveryClaim:
    """Approved reply data and fencing token captured by one delivery lease."""

    id: UUID
    ticket_id: str
    reply_url: str
    draft_text: str
    attempts: int
    locked_until: datetime


@dataclass(frozen=True)
class RecoveredDelivery:
    """A delivery lease recovery result for transition logging."""

    ticket_id: str
    attempts: int
    status: str


async def claim_next(
    session: AsyncSession,
    *,
    lease: timedelta,
) -> ClaimedTicket | None:
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
            .returning(
                Ticket.id,
                Ticket.ticket_id,
                Ticket.message,
                Ticket.attempts,
            )
        )
        claimed = (await session.execute(statement)).one()
        result = ClaimedTicket(
            id=claimed.id,
            ticket_id=claimed.ticket_id,
            message=claimed.message,
            attempts=claimed.attempts,
        )
    return result


async def claim_for_delivery(
    session: AsyncSession,
    *,
    lease: timedelta,
) -> DeliveryClaim | None:
    """Commit a sending lease before returning the reply for external delivery."""
    async with session.begin():
        candidate = await session.scalar(
            select(Ticket)
            .where(
                Ticket.status == "approved",
                Ticket.next_attempt_at <= func.now(),
            )
            .order_by(Ticket.created_at, Ticket.id)
            .limit(1)
            .with_for_update(skip_locked=True)
        )
        if candidate is None:
            return None

        result = await session.execute(
            update(Ticket)
            .where(Ticket.id == candidate.id, Ticket.status == "approved")
            .values(
                status="sending",
                delivery_attempts=Ticket.delivery_attempts + 1,
                locked_until=func.now() + lease,
                updated_at=func.now(),
            )
            .returning(
                Ticket.id,
                Ticket.ticket_id,
                Ticket.reply_url,
                Ticket.draft_text,
                Ticket.delivery_attempts,
                Ticket.locked_until,
            )
        )
        claimed = result.one()
        if claimed.locked_until is None:
            raise RuntimeError("The delivery claim did not receive a lease deadline")
        delivery = DeliveryClaim(
            id=claimed.id,
            ticket_id=claimed.ticket_id,
            reply_url=claimed.reply_url,
            draft_text=claimed.draft_text or "",
            attempts=claimed.delivery_attempts,
            locked_until=claimed.locked_until,
        )
    return delivery


async def recover_stuck_sending(
    session: AsyncSession,
    *,
    max_attempts: int,
) -> list[RecoveredDelivery]:
    """Recover expired sender leases without allowing attempts beyond the limit."""
    async with session.begin():
        result = await session.execute(
            update(Ticket)
            .where(
                Ticket.status == "sending",
                Ticket.locked_until < func.now(),
            )
            .values(
                status=case(
                    (Ticket.delivery_attempts >= max_attempts, "send_failed"),
                    else_="approved",
                ),
                next_attempt_at=func.now(),
                locked_until=None,
                error=case(
                    (
                        Ticket.delivery_attempts >= max_attempts,
                        "Delivery lease expired after the maximum attempts",
                    ),
                    else_=Ticket.error,
                ),
                updated_at=func.now(),
            )
            .returning(Ticket.ticket_id, Ticket.delivery_attempts, Ticket.status)
        )
    return [
        RecoveredDelivery(ticket_id, attempts, status)
        for ticket_id, attempts, status in result.all()
    ]


async def recover_stuck(session: AsyncSession) -> list[tuple[str, int]]:
    """Return expired leases to the queue and identify each recovered ticket.

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
            .returning(Ticket.ticket_id, Ticket.attempts)
        )
    return [(ticket_id, attempts) for ticket_id, attempts in result.all()]
