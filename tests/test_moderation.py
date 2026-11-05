"""PostgreSQL-backed integration tests for operator moderation actions."""

import asyncio
from uuid import uuid4

import httpx
import pytest
from app.models import Ticket
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession


async def _create_draft(postgres_engine: AsyncEngine) -> Ticket:
    """Create one ticket that is waiting for operator review."""
    async with AsyncSession(postgres_engine) as session:
        ticket = Ticket(
            event_id=f"evt_moderation_{uuid4()}",
            ticket_id=f"T-{uuid4()}",
            customer_email="operator-test@example.com",
            message="Потрібна допомога",
            reply_url="http://helpdesk/reply",
            status="draft_ready",
            draft_text="Чернетка",
        )
        session.add(ticket)
        await session.commit()
        await session.refresh(ticket)
        return ticket


@pytest.mark.asyncio
async def test_concurrent_approve_requests_have_one_winner(
    api_client: httpx.AsyncClient,
    postgres_engine: AsyncEngine,
) -> None:
    """The conditional transition lets only one concurrent approval succeed."""
    ticket = await _create_draft(postgres_engine)
    responses = await asyncio.gather(
        *(
            api_client.post(
                f"/operator/approve/{ticket.id}",
                data={"draft_text": "Approved reply"},
            )
            for _ in range(2)
        )
    )

    assert sorted(response.status_code for response in responses) == [303, 409]
    async with AsyncSession(postgres_engine) as session:
        saved = await session.get(Ticket, ticket.id)
    assert saved is not None
    assert saved.approved_at is not None
    assert saved.draft_text == "Approved reply"


@pytest.mark.asyncio
async def test_reject_marks_draft_terminal(
    api_client: httpx.AsyncClient,
    postgres_engine: AsyncEngine,
) -> None:
    """A rejected draft leaves the moderation queue without being approved."""
    ticket = await _create_draft(postgres_engine)

    response = await api_client.post(f"/operator/reject/{ticket.id}")

    assert response.status_code == 303
    async with AsyncSession(postgres_engine) as session:
        saved = await session.get(Ticket, ticket.id)
    assert saved is not None
    assert saved.status == "rejected"


@pytest.mark.asyncio
async def test_moderation_page_renders_pending_message_and_draft(
    api_client: httpx.AsyncClient,
    postgres_engine: AsyncEngine,
) -> None:
    """The operator page exposes ticket context and the editable reply."""
    ticket = await _create_draft(postgres_engine)

    response = await api_client.get("/operator/moderation")

    assert response.status_code == 200
    assert ticket.ticket_id in response.text
    assert ticket.message in response.text
    assert ticket.draft_text in response.text
