"""PostgreSQL integration tests for safe ticket claiming and lease recovery."""

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from app.models import Ticket
from app.services.queue import claim_next, recover_stuck
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession


async def _create_ticket(
    session: AsyncSession,
    *,
    status: str = "new",
    locked_until: datetime | None = None,
) -> Ticket:
    """Create one independent ticket for queue-state integration tests."""
    ticket = Ticket(
        event_id=f"evt_queue_{uuid4()}",
        ticket_id=f"T-{uuid4()}",
        customer_email="queue@example.com",
        message="Потрібна допомога з експортом звіту",
        reply_url="https://helpdesk.example.com/replies",
        status=status,
        locked_until=locked_until,
    )
    session.add(ticket)
    await session.flush()
    return ticket


@pytest.mark.asyncio
async def test_parallel_claimers_claim_each_ticket_once(
    postgres_engine: AsyncEngine,
) -> None:
    """Row-level SKIP LOCKED claims distribute tickets without duplicates."""
    async with AsyncSession(postgres_engine) as session:
        for _ in range(20):
            await _create_ticket(session)
        await session.commit()

    async def claim_all() -> list[str]:
        claimed: list[str] = []
        async with AsyncSession(postgres_engine) as session:
            while ticket := await claim_next(session, lease=timedelta(minutes=2)):
                claimed.append(str(ticket.id))
        return claimed

    batches = await asyncio.gather(*(claim_all() for _ in range(5)))
    claimed_ids = [ticket_id for batch in batches for ticket_id in batch]

    assert len(claimed_ids) == 20
    assert len(set(claimed_ids)) == 20
    async with AsyncSession(postgres_engine) as session:
        generating_count = await session.scalar(
            select(func.count())
            .select_from(Ticket)
            .where(Ticket.status == "generating")
        )
    assert generating_count == 20


@pytest.mark.asyncio
async def test_recover_stuck_ticket_after_lease_expires(
    postgres_engine: AsyncEngine,
) -> None:
    """An expired lease makes work claimable again after its owner disappears."""
    async with AsyncSession(postgres_engine) as session:
        ticket = await _create_ticket(
            session,
            status="generating",
            locked_until=datetime.now(UTC) - timedelta(seconds=1),
        )
        ticket_id = ticket.id
        external_ticket_id = ticket.ticket_id
        await session.commit()

    async with AsyncSession(postgres_engine) as session:
        recovered = await recover_stuck(session)
    assert len(recovered) == 1
    assert recovered[0] == (external_ticket_id, 0)

    async with AsyncSession(postgres_engine) as session:
        ticket = await session.get(Ticket, ticket_id)

    assert ticket is not None
    assert ticket.status == "new"
    assert ticket.locked_until is None
    assert ticket.next_attempt_at <= datetime.now(UTC)
