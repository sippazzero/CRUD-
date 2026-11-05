"""Durable and idempotent persistence for helpdesk webhook events."""

from sqlalchemy import func
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Ticket
from app.schemas import HelpdeskWebhook


async def ingest_ticket(
    session: AsyncSession,
    event: HelpdeskWebhook,
) -> bool:
    """Insert a new event once and return whether this delivery created it.

    The unique event ID and PostgreSQL conflict handling arbitrate concurrent
    deliveries in one statement; a prior SELECT would leave a race between the
    existence check and insert, allowing duplicate work to be queued.
    """
    statement = (
        insert(Ticket)
        .values(
            event_id=event.event_id,
            ticket_id=event.ticket_id,
            customer_email=str(event.customer_email),
            message=event.message,
            reply_url=str(event.reply_url),
            status="new",
            attempts=0,
            next_attempt_at=func.now(),
        )
        .on_conflict_do_nothing(index_elements=[Ticket.event_id])
        .returning(Ticket.id)
    )
    async with session.begin():
        inserted_id = (await session.execute(statement)).scalar_one_or_none()

    return inserted_id is not None
