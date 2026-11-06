"""PostgreSQL integration tests for draft processing and retry state."""

from collections.abc import Sequence
from datetime import UTC, datetime
from uuid import uuid4

import httpx
import pytest
from app.config import Settings
from app.models import KnowledgeArticle, Ticket
from app.services.llm import GeneratedDraft, LLMClient
from app.worker import process_next
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker


class FailingGenerator:
    """Represent a provider failure while preserving the generator contract."""

    async def generate(
        self,
        message: str,
        articles: Sequence[KnowledgeArticle],
    ) -> GeneratedDraft:
        """Raise the same exception shape surfaced by a timed-out provider."""
        raise TimeoutError("provider timed out")


async def _create_ticket(
    postgres_engine: AsyncEngine,
    *,
    event_id: str | None = None,
    message: str = "Як експортувати звіт у PDF?",
) -> Ticket:
    """Create one persisted job through the same new state as webhook ingress."""
    async with AsyncSession(postgres_engine) as session:
        ticket = Ticket(
            event_id=event_id or f"evt_worker_{uuid4()}",
            ticket_id=f"T-{uuid4()}",
            customer_email="worker@example.com",
            message=message,
            reply_url="https://helpdesk.example.com/replies",
            status="new",
        )
        session.add(ticket)
        await session.commit()
        await session.refresh(ticket)
        return ticket


@pytest.mark.asyncio
async def test_failed_generation_retries_then_fails_at_attempt_limit(
    postgres_engine: AsyncEngine,
) -> None:
    """A transient failure schedules backoff until the configured final attempt."""
    ticket = await _create_ticket(postgres_engine)
    session_factory = async_sessionmaker(postgres_engine, expire_on_commit=False)
    settings = Settings(
        worker_max_attempts=2,
        worker_backoff_base_seconds=10,
        worker_backoff_max_seconds=60,
    )

    processed = await process_next(
        session_factory,
        FailingGenerator(),
        settings,
        uniform=lambda minimum, maximum: maximum,
    )
    assert processed
    async with AsyncSession(postgres_engine) as session:
        first_attempt = await session.get(Ticket, ticket.id)

    assert first_attempt is not None
    assert first_attempt.status == "new"
    assert first_attempt.attempts == 1
    assert first_attempt.next_attempt_at > datetime.now(UTC)
    assert first_attempt.error == "provider timed out"

    async with AsyncSession(postgres_engine) as session:
        await session.execute(
            update(Ticket)
            .where(Ticket.id == ticket.id)
            .values(next_attempt_at=func.now())
        )
        await session.commit()

    await process_next(
        session_factory,
        FailingGenerator(),
        settings,
        uniform=lambda minimum, maximum: maximum,
    )
    async with AsyncSession(postgres_engine) as session:
        final_attempt = await session.get(Ticket, ticket.id)

    assert final_attempt is not None
    assert final_attempt.status == "failed"
    assert final_attempt.attempts == 2
    assert final_attempt.error == "provider timed out"


@pytest.mark.asyncio
async def test_webhook_ticket_becomes_draft_ready(
    api_client: httpx.AsyncClient,
    postgres_engine: AsyncEngine,
) -> None:
    """A persisted webhook event flows through retrieval and draft persistence."""
    event_id = f"evt_e2e_{uuid4()}"
    payload = {
        "event_id": event_id,
        "ticket_id": "T-e2e",
        "customer_email": "customer@example.com",
        "message": "Як експортувати звіт у PDF?",
        "reply_url": "https://helpdesk.example.com/tickets/T-e2e/replies",
    }
    response = await api_client.post("/webhooks/helpdesk", json=payload)
    assert response.status_code == 200
    async with AsyncSession(postgres_engine) as session:
        ticket = await session.scalar(select(Ticket).where(Ticket.event_id == event_id))
    assert ticket is not None

    def provider_attempt(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "missing API key"})

    async def skip_fallback_delay(seconds: float) -> None:
        assert 5 <= seconds <= 30

    settings = Settings(
        llm_provider="openai_compatible",
        llm_base_url="https://provider.example",
        llm_api_key=None,
        llm_static_fallback_enabled=True,
        llm_daily_call_cap=0,
    )
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(provider_attempt)
    ) as client:
        generator = LLMClient(
            settings,
            client=client,
            sleep=skip_fallback_delay,
            uniform=lambda minimum, maximum: minimum + (maximum - minimum) * 0.5,
        )
        processed = await process_next(
            async_sessionmaker(postgres_engine, expire_on_commit=False),
            generator,
            settings,
        )
    assert processed

    async with AsyncSession(postgres_engine) as session:
        ticket = await session.get(Ticket, ticket.id)

    assert ticket is not None
    assert ticket.status == "draft_ready"
    assert ticket.draft_text
    assert "export-pdf-report" in ticket.cited_article_slugs
