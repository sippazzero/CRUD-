"""PostgreSQL-backed integration coverage for moderation and reply delivery."""

import asyncio
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import httpx
import pytest
from app.config import Settings
from app.helpdesk_mock import MockBehavior, create_mock_app
from app.models import KnowledgeArticle, Ticket
from app.services.llm import GeneratedDraft
from app.worker import process_next
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from starlette.types import ASGIApp


class AllowAllLimiter:
    """Keep these delivery tests independent of LLM rate-limit waiting."""

    async def acquire(self) -> None:
        """Allow processing to continue immediately."""


class UnusedGenerator:
    """Fail if a delivery-only fixture unexpectedly enters draft generation."""

    async def generate(
        self,
        message: str,
        articles: Sequence[KnowledgeArticle],
    ) -> GeneratedDraft:
        """Raise because all relevant fixtures bypass generation."""
        raise AssertionError("The test ticket should not enter generation")


class LoseFirstResponse(httpx.AsyncBaseTransport):
    """Drop the first mock response after the in-process app handles it."""

    def __init__(self, mock_app: ASGIApp) -> None:
        """Create a transport that forwards to the real mock ASGI application."""
        self._transport = httpx.ASGITransport(app=mock_app)
        self._dropped = False

    async def handle_async_request(
        self,
        request: httpx.Request,
    ) -> httpx.Response:
        """Simulate a lost response after the mock has committed its side effect."""
        response = await self._transport.handle_async_request(request)
        if not self._dropped:
            await response.aread()
            await response.aclose()
            self._dropped = True
            raise httpx.ReadTimeout("Simulated lost response", request=request)
        return response

    async def aclose(self) -> None:
        """Close the underlying in-process ASGI transport."""
        await self._transport.aclose()


class FailFirstConnection(httpx.AsyncBaseTransport):
    """Raise a connection error once, then forward subsequent requests."""

    def __init__(self, mock_app: ASGIApp) -> None:
        """Create a transport backed by the real mock ASGI application."""
        self._transport = httpx.ASGITransport(app=mock_app)
        self._failed = False

    async def handle_async_request(
        self,
        request: httpx.Request,
    ) -> httpx.Response:
        """Simulate a connect failure before the mock receives the first request."""
        if not self._failed:
            self._failed = True
            raise httpx.ConnectError("Simulated connection failure", request=request)
        return await self._transport.handle_async_request(request)

    async def aclose(self) -> None:
        """Close the underlying in-process ASGI transport."""
        await self._transport.aclose()


class ExpireLeaseBeforeResponse(httpx.AsyncBaseTransport):
    """Expire the claimed lease after mock processing but before its response."""

    def __init__(
        self,
        mock_app: ASGIApp,
        engine: AsyncEngine,
        ticket_id: UUID,
    ) -> None:
        """Create a response transport that transfers lease ownership mid-call."""
        self._transport = httpx.ASGITransport(app=mock_app)
        self._engine = engine
        self._ticket_id = ticket_id

    async def handle_async_request(
        self,
        request: httpx.Request,
    ) -> httpx.Response:
        """Expire the stored lease before allowing the sender to persist success."""
        response = await self._transport.handle_async_request(request)
        async with self._engine.begin() as connection:
            await connection.execute(
                update(Ticket)
                .where(Ticket.id == self._ticket_id)
                .values(locked_until=func.now() - timedelta(seconds=1))
            )
        return response

    async def aclose(self) -> None:
        """Close the underlying in-process ASGI transport."""
        await self._transport.aclose()


async def _create_ticket(
    postgres_engine: AsyncEngine,
    *,
    status: str = "approved",
    locked_until: datetime | None = None,
    delivery_attempts: int = 0,
) -> Ticket:
    """Persist a ticket in the requested delivery or moderation state."""
    async with AsyncSession(postgres_engine) as session:
        ticket = Ticket(
            event_id=f"evt_delivery_{uuid4()}",
            ticket_id=f"T-{uuid4()}",
            customer_email="delivery@example.com",
            message="Потрібна допомога з експортом звіту",
            reply_url="http://helpdesk/reply",
            status=status,
            draft_text="Відповідь оператора",
            locked_until=locked_until,
        )
        ticket.delivery_attempts = delivery_attempts
        session.add(ticket)
        await session.commit()
        await session.refresh(ticket)
        return ticket


def _settings(**overrides: object) -> Settings:
    """Use short bounded delivery retries for deterministic integration tests."""
    values: dict[str, object] = {
        "sender_timeout_seconds": 0.02,
        "sender_max_attempts": 4,
        "worker_backoff_base_seconds": 0.01,
        "worker_backoff_max_seconds": 0.02,
    }
    values.update(overrides)
    return Settings(**values)


def test_mock_fault_window_is_loaded_from_settings() -> None:
    """Compose can configure bounded pre-record failures for repeatable demos."""
    app = create_mock_app(settings=_settings(mock_fail_before_record=2))

    assert app.state.mock.behavior.fail_before_record == 2


async def _process_once(
    postgres_engine: AsyncEngine,
    *,
    mock_app: ASGIApp,
    settings: Settings | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> bool:
    """Run one actual worker cycle against an in-process helpdesk mock."""
    actual_transport = transport or httpx.ASGITransport(app=mock_app)
    async with httpx.AsyncClient(transport=actual_transport) as client:
        return await process_next(
            async_sessionmaker(postgres_engine, expire_on_commit=False),
            UnusedGenerator(),
            AllowAllLimiter(),
            settings or _settings(),
            helpdesk_client=client,
        )


async def _load_ticket(postgres_engine: AsyncEngine, ticket_id: UUID) -> Ticket:
    """Load a persisted ticket after one state transition."""
    async with AsyncSession(postgres_engine) as session:
        ticket = await session.get(Ticket, ticket_id)
        assert ticket is not None
        return ticket


async def _make_due(postgres_engine: AsyncEngine, ticket_id: UUID) -> None:
    """Release retry backoff in the database without sleeping in the test."""
    async with AsyncSession(postgres_engine) as session:
        await session.execute(
            update(Ticket)
            .where(Ticket.id == ticket_id)
            .values(next_attempt_at=func.now() - timedelta(seconds=1))
        )
        await session.commit()


async def _delivery_count(mock_app: ASGIApp) -> int:
    """Read the mock's externally visible delivery ledger."""
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=mock_app),
        base_url="http://helpdesk",
    ) as client:
        response = await client.get("/deliveries")
    assert response.status_code == 200
    return len(response.json())


@pytest.mark.asyncio
async def test_double_approve_allows_exactly_one_operator(
    api_client: httpx.AsyncClient,
    postgres_engine: AsyncEngine,
) -> None:
    """Concurrent approval requests have one winner and one conflict."""
    ticket = await _create_ticket(postgres_engine, status="draft_ready")

    responses = await asyncio.gather(
        *(
            api_client.post(
                f"/operator/approve/{ticket.id}",
                data={"draft_text": "Відповідь оператора"},
            )
            for _ in range(2)
        )
    )

    assert sorted(response.status_code for response in responses) == [303, 409]
    saved = await _load_ticket(postgres_engine, ticket.id)
    assert saved.status == "approved"


@pytest.mark.asyncio
async def test_successful_delivery_marks_sent_once(
    postgres_engine: AsyncEngine,
) -> None:
    """A successful helpdesk response persists sent and one delivery record."""
    ticket = await _create_ticket(postgres_engine)
    mock_app = create_mock_app()

    assert await _process_once(postgres_engine, mock_app=mock_app)

    saved = await _load_ticket(postgres_engine, ticket.id)
    assert saved.status == "sent"
    assert await _delivery_count(mock_app) == 1


@pytest.mark.asyncio
async def test_two_transient_failures_retry_then_send_once(
    postgres_engine: AsyncEngine,
) -> None:
    """Transient failures retain one idempotent record and eventually send."""
    ticket = await _create_ticket(postgres_engine)
    mock_app = create_mock_app(MockBehavior(fail_before_record=2, success_status=200))
    for expected_attempt in (1, 2, 3):
        assert await _process_once(
            postgres_engine,
            mock_app=mock_app,
            settings=_settings(sender_max_attempts=4),
        )
        saved = await _load_ticket(postgres_engine, ticket.id)
        if expected_attempt < 3:
            assert saved.status == "approved"
            await _make_due(postgres_engine, ticket.id)
        else:
            assert saved.status == "sent"
            assert saved.delivery_attempts == 3

    assert await _delivery_count(mock_app) == 1


@pytest.mark.asyncio
async def test_lost_response_retries_with_same_key_and_one_delivery(
    postgres_engine: AsyncEngine,
) -> None:
    """A recorded first request survives a lost response without duplication."""
    ticket = await _create_ticket(postgres_engine)
    mock_app = create_mock_app(MockBehavior(hang_after_record=1, hang_seconds=0.03))

    await _process_once(
        postgres_engine,
        mock_app=mock_app,
        transport=LoseFirstResponse(mock_app),
    )
    saved = await _load_ticket(postgres_engine, ticket.id)
    assert saved.status == "approved"
    await _make_due(postgres_engine, ticket.id)

    await _process_once(postgres_engine, mock_app=mock_app)

    saved = await _load_ticket(postgres_engine, ticket.id)
    assert saved.status == "sent"
    assert saved.delivery_attempts == 2
    assert await _delivery_count(mock_app) == 1
    assert mock_app.state.mock.request_keys == [
        f"reply:{ticket.ticket_id}",
        f"reply:{ticket.ticket_id}",
    ]


@pytest.mark.asyncio
async def test_permanent_client_error_fails_without_retry(
    postgres_engine: AsyncEngine,
) -> None:
    """A permanent 4xx response ends delivery on its first attempt."""
    ticket = await _create_ticket(postgres_engine)
    mock_app = create_mock_app(MockBehavior(permanent_status=422))

    await _process_once(postgres_engine, mock_app=mock_app)

    saved = await _load_ticket(postgres_engine, ticket.id)
    assert saved.status == "send_failed"
    assert saved.delivery_attempts == 1
    assert await _delivery_count(mock_app) == 0


@pytest.mark.asyncio
async def test_transient_failures_exhaust_delivery_attempt_limit(
    postgres_engine: AsyncEngine,
) -> None:
    """The configured final transient failure moves the ticket to send_failed."""
    ticket = await _create_ticket(postgres_engine)
    mock_app = create_mock_app(MockBehavior(fail_before_record=2))
    settings = _settings(sender_max_attempts=2)

    await _process_once(postgres_engine, mock_app=mock_app, settings=settings)
    await _make_due(postgres_engine, ticket.id)
    await _process_once(postgres_engine, mock_app=mock_app, settings=settings)

    saved = await _load_ticket(postgres_engine, ticket.id)
    assert saved.status == "send_failed"
    assert saved.delivery_attempts == 2
    assert await _delivery_count(mock_app) == 0


@pytest.mark.asyncio
async def test_connection_error_is_retried(
    postgres_engine: AsyncEngine,
) -> None:
    """A connection error schedules another attempt with the same stable key."""
    ticket = await _create_ticket(postgres_engine)
    mock_app = create_mock_app()

    await _process_once(
        postgres_engine,
        mock_app=mock_app,
        transport=FailFirstConnection(mock_app),
    )
    saved = await _load_ticket(postgres_engine, ticket.id)
    assert saved.status == "approved"
    assert saved.delivery_attempts == 1
    await _make_due(postgres_engine, ticket.id)

    await _process_once(postgres_engine, mock_app=mock_app)

    saved = await _load_ticket(postgres_engine, ticket.id)
    assert saved.status == "sent"
    assert saved.delivery_attempts == 2
    assert mock_app.state.mock.request_keys == [f"reply:{ticket.ticket_id}"]


@pytest.mark.asyncio
async def test_expired_sending_lease_is_retried_with_ticket_key(
    postgres_engine: AsyncEngine,
) -> None:
    """A crashed sender is recovered and the stable ticket key is reused."""
    ticket = await _create_ticket(
        postgres_engine,
        status="sending",
        locked_until=datetime.now(UTC) - timedelta(seconds=1),
        delivery_attempts=1,
    )
    mock_app = create_mock_app()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=mock_app),
        base_url="http://helpdesk",
    ) as client:
        initial = await client.post(
            "/reply",
            json={"ticket_id": ticket.ticket_id, "message": ticket.draft_text},
            headers={"Idempotency-Key": f"reply:{ticket.ticket_id}"},
        )
    assert initial.status_code == 201

    await _process_once(postgres_engine, mock_app=mock_app)

    saved = await _load_ticket(postgres_engine, ticket.id)
    assert saved.status == "sent"
    assert saved.delivery_attempts == 2
    assert await _delivery_count(mock_app) == 1


@pytest.mark.asyncio
async def test_stale_success_cannot_write_after_lease_expires(
    postgres_engine: AsyncEngine,
) -> None:
    """A success arriving after lease expiry cannot overwrite the next owner."""
    ticket = await _create_ticket(postgres_engine)
    mock_app = create_mock_app()

    await _process_once(
        postgres_engine,
        mock_app=mock_app,
        transport=ExpireLeaseBeforeResponse(mock_app, postgres_engine, ticket.id),
    )
    stale = await _load_ticket(postgres_engine, ticket.id)
    assert stale.status == "sending"
    assert stale.locked_until is not None
    assert stale.locked_until < datetime.now(UTC)

    await _process_once(postgres_engine, mock_app=mock_app)

    saved = await _load_ticket(postgres_engine, ticket.id)
    assert saved.status == "sent"
    assert saved.delivery_attempts == 2
    assert await _delivery_count(mock_app) == 1


@pytest.mark.asyncio
async def test_rejected_ticket_is_never_delivered(
    api_client: httpx.AsyncClient,
    postgres_engine: AsyncEngine,
) -> None:
    """Rejection is terminal and does not enqueue a helpdesk delivery."""
    ticket = await _create_ticket(postgres_engine, status="draft_ready")
    mock_app = create_mock_app()

    response = await api_client.post(f"/operator/reject/{ticket.id}")
    assert response.status_code == 303
    assert await _process_once(postgres_engine, mock_app=mock_app) is False

    saved = await _load_ticket(postgres_engine, ticket.id)
    assert saved.status == "rejected"
    assert await _delivery_count(mock_app) == 0


@pytest.mark.asyncio
async def test_duplicate_webhook_mode_delivers_same_event_twice_but_ingests_once(
    api_client: httpx.AsyncClient,
    postgres_engine: AsyncEngine,
) -> None:
    """The mock duplicate mode exercises webhook idempotency end to end."""
    event_id = f"evt_mock_duplicate_{uuid4()}"
    mock_api_client = httpx.AsyncClient(
        transport=httpx.ASGITransport(
            app=create_mock_app(
                MockBehavior(duplicate_webhooks=True),
                settings=Settings(mock_api_url="http://test/webhooks/helpdesk"),
                webhook_client=api_client,
            ),
        ),
        base_url="http://helpdesk",
    )
    async with mock_api_client as client:
        response = await client.post(
            "/simulate-webhook",
            json={
                "event_id": event_id,
                "ticket_id": "T-mock-duplicate",
                "customer_email": "customer@example.com",
                "message": "Duplicate mode test",
                "reply_url": "http://helpdesk/reply",
            },
        )
    assert response.status_code == 200
    assert response.json()["attempts"] == [200, 200]

    async with AsyncSession(postgres_engine) as session:
        count = await session.scalar(
            select(func.count()).select_from(Ticket).where(Ticket.event_id == event_id)
        )
    assert count == 1
