"""PostgreSQL-backed tests for helpdesk webhook acceptance."""

import asyncio
from time import perf_counter

import httpx
import pytest
from app.models import Ticket
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

WEBHOOK_PATH = "/webhooks/helpdesk"
PAYLOAD = {
    "event_id": "evt_test_ingest",
    "ticket_id": "T-test-ingest",
    "customer_email": "customer@example.com",
    "message": "I cannot export my report to PDF.",
    "reply_url": "https://helpdesk.example.com/tickets/T-test-ingest/replies",
}
ASSIGNMENT_SAMPLE_PAYLOAD = {
    "event_id": "evt_sample1",
    "ticket_id": "T-1042",
    "customer_email": "user@example.com",
    "message": "Cannot export report to PDF",
    "reply_url": "https://helpdesk.example.com/tickets/T-1042/replies",
}


async def _ticket_count(postgres_engine: AsyncEngine, event_id: str) -> int:
    """Count persisted rows for an event using the real integration database."""
    async with AsyncSession(postgres_engine) as session:
        result = await session.scalar(
            select(func.count()).select_from(Ticket).where(Ticket.event_id == event_id)
        )
    return result or 0


@pytest.mark.asyncio
async def test_first_delivery_persists_one_new_ticket(
    api_client: httpx.AsyncClient,
    postgres_engine: AsyncEngine,
) -> None:
    """A first delivery is acknowledged only after its new ticket is stored."""
    response = await api_client.post(WEBHOOK_PATH, json=PAYLOAD)

    assert response.status_code == 200
    assert response.json() == {"status": "accepted"}
    async with AsyncSession(postgres_engine) as session:
        ticket = await session.scalar(
            select(Ticket).where(Ticket.event_id == PAYLOAD["event_id"])
        )

    assert ticket is not None
    assert ticket.status == "new"
    assert ticket.attempts == 0
    assert ticket.next_attempt_at is not None
    assert await _ticket_count(postgres_engine, PAYLOAD["event_id"]) == 1


@pytest.mark.asyncio
async def test_assignment_sample_callback_is_accepted(
    api_client: httpx.AsyncClient,
    postgres_engine: AsyncEngine,
) -> None:
    """The provided sample payload remains valid despite DNS safety checks."""
    response = await api_client.post(
        WEBHOOK_PATH,
        json=ASSIGNMENT_SAMPLE_PAYLOAD,
    )

    assert response.status_code == 200
    assert await _ticket_count(postgres_engine, "evt_sample1") == 1


@pytest.mark.asyncio
async def test_sequential_duplicate_returns_same_response_without_second_row(
    api_client: httpx.AsyncClient,
    postgres_engine: AsyncEngine,
) -> None:
    """A duplicate event receives the same acknowledgement without a new row."""
    first = await api_client.post(WEBHOOK_PATH, json=PAYLOAD)
    duplicate = await api_client.post(WEBHOOK_PATH, json=PAYLOAD)

    assert duplicate.status_code == first.status_code == 200
    assert duplicate.json() == first.json()
    assert await _ticket_count(postgres_engine, PAYLOAD["event_id"]) == 1


@pytest.mark.asyncio
async def test_concurrent_duplicates_persist_exactly_one_ticket(
    api_client: httpx.AsyncClient,
    postgres_engine: AsyncEngine,
) -> None:
    """Concurrent deliveries rely on the unique index for atomic deduplication."""
    responses = await asyncio.gather(
        *(api_client.post(WEBHOOK_PATH, json=PAYLOAD) for _ in range(20))
    )

    assert all(response.status_code == 200 for response in responses)
    assert all(response.json() == {"status": "accepted"} for response in responses)
    assert await _ticket_count(postgres_engine, PAYLOAD["event_id"]) == 1


@pytest.mark.asyncio
async def test_invalid_payload_returns_422_without_persisting(
    api_client: httpx.AsyncClient,
    postgres_engine: AsyncEngine,
) -> None:
    """Invalid webhook data is rejected before any database row is created."""
    invalid_payload = {"event_id": "evt_invalid", "ticket_id": "T-invalid"}

    response = await api_client.post(WEBHOOK_PATH, json=invalid_payload)

    assert response.status_code == 422
    assert await _ticket_count(postgres_engine, "evt_invalid") == 0


@pytest.mark.asyncio
async def test_untrusted_reply_host_returns_422_without_persisting(
    api_client: httpx.AsyncClient,
    postgres_engine: AsyncEngine,
) -> None:
    """Webhook callers cannot direct the sender to an unapproved network host."""
    payload = {
        **PAYLOAD,
        "event_id": "evt_untrusted_reply_host",
        "reply_url": "http://api:8000/health",
    }

    response = await api_client.post(WEBHOOK_PATH, json=payload)

    assert response.status_code == 422
    assert await _ticket_count(postgres_engine, payload["event_id"]) == 0


@pytest.mark.asyncio
async def test_allowed_reply_host_is_accepted(
    api_client: httpx.AsyncClient,
    postgres_engine: AsyncEngine,
) -> None:
    """Configured helpdesk hosts remain valid webhook destinations."""
    payload = {
        **PAYLOAD,
        "event_id": "evt_allowed_reply_host",
        "reply_url": "https://helpdesk.example.com/tickets/T-test/replies",
    }

    response = await api_client.post(WEBHOOK_PATH, json=payload)

    assert response.status_code == 200
    assert await _ticket_count(postgres_engine, payload["event_id"]) == 1


@pytest.mark.asyncio
async def test_webhook_response_is_under_two_seconds(
    api_client: httpx.AsyncClient,
) -> None:
    """Webhook acknowledgement stays below the sender's retry deadline."""
    started_at = perf_counter()

    response = await api_client.post(WEBHOOK_PATH, json=PAYLOAD)

    elapsed = perf_counter() - started_at
    assert response.status_code == 200
    assert elapsed < 2
