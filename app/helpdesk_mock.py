"""HTTP mock for helpdesk reply delivery and webhook redelivery."""

import asyncio
import logging
import random
import time
from dataclasses import dataclass, field
from typing import Annotated

import httpx
from fastapi import FastAPI, Header, HTTPException, Request, Response
from pydantic import BaseModel

from app.config import Settings, get_settings
from app.schemas import HelpdeskWebhook

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Delivery:
    """One idempotently accepted reply stored by the mock helpdesk."""

    ticket_id: str
    message: str
    idempotency_key: str
    timestamp: float


@dataclass
class MockBehavior:
    """Configurable deterministic faults for exercising delivery guarantees."""

    failure_rate: float = 0.0
    hang_rate: float = 0.0
    hang_seconds: float = 15.0
    fail_before_record: int = 0
    fail_after_record: int = 0
    hang_after_record: int = 0
    permanent_status: int | None = None
    duplicate_webhooks: bool = False
    success_status: int = 201


@dataclass
class MockState:
    """Process-local ledgers and synchronization for the mock service."""

    behavior: MockBehavior
    api_url: str
    deliveries: dict[str, Delivery] = field(default_factory=dict)
    delivery_log: list[Delivery] = field(default_factory=list)
    request_keys: list[str] = field(default_factory=list)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    webhook_client: httpx.AsyncClient | None = None


class ReplyRequest(BaseModel):
    """Helpdesk reply payload accepted by the mock endpoint."""

    ticket_id: str
    message: str


def create_mock_app(
    behavior: MockBehavior | None = None,
    *,
    settings: Settings | None = None,
    webhook_client: httpx.AsyncClient | None = None,
) -> FastAPI:
    """Create an isolated mock app with durable-for-process idempotency behavior."""
    app_settings = settings or get_settings()
    mock_behavior = behavior or MockBehavior(
        failure_rate=app_settings.mock_failure_rate,
        hang_rate=app_settings.mock_hang_rate,
        hang_seconds=app_settings.mock_hang_seconds,
        fail_before_record=app_settings.mock_fail_before_record,
        duplicate_webhooks=app_settings.mock_duplicate_mode,
    )
    app = FastAPI(title="Helpdesk Mock")
    app.state.mock = MockState(
        behavior=mock_behavior,
        api_url=app_settings.mock_api_url,
        webhook_client=webhook_client,
    )

    @app.post("/reply")
    async def receive_reply(
        request: Request,
        reply: ReplyRequest,
        idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    ) -> Response:
        """Record one reply before simulating an ambiguous external outcome."""
        state: MockState = request.app.state.mock
        if not idempotency_key:
            raise HTTPException(
                status_code=400,
                detail="Missing Idempotency-Key header",
            )

        should_hang = False
        should_fail = False
        async with state.lock:
            state.request_keys.append(idempotency_key)
            existing = state.deliveries.get(idempotency_key)
            if existing is not None:
                if (
                    existing.ticket_id != reply.ticket_id
                    or existing.message != reply.message
                ):
                    return Response(
                        status_code=409,
                        content="Idempotency-Key was reused with a different reply",
                    )
                return Response(
                    status_code=state.behavior.success_status,
                    content="Already Delivered",
                )

            if state.behavior.permanent_status is not None:
                return Response(status_code=state.behavior.permanent_status)
            if state.behavior.fail_before_record > 0:
                state.behavior.fail_before_record -= 1
                return Response(status_code=500, content="Temporary failure")

            delivery = Delivery(
                ticket_id=reply.ticket_id,
                message=reply.message,
                idempotency_key=idempotency_key,
                timestamp=time.time(),
            )
            state.deliveries[idempotency_key] = delivery
            state.delivery_log.append(delivery)

            if state.behavior.hang_after_record > 0:
                should_hang = True
                state.behavior.hang_after_record -= 1
            if state.behavior.fail_after_record > 0:
                state.behavior.fail_after_record -= 1
                should_fail = True
            else:
                should_fail = random.random() < state.behavior.failure_rate
            should_hang = should_hang or random.random() < state.behavior.hang_rate

        if should_hang:
            await asyncio.sleep(state.behavior.hang_seconds)
        if should_fail:
            return Response(status_code=500, content="Processed; response failed")
        logger.info(
            "Mock helpdesk accepted reply",
            extra={"ticket_id": reply.ticket_id, "idempotency_key": idempotency_key},
        )
        return Response(
            status_code=state.behavior.success_status,
            content="Delivered",
        )

    @app.post("/simulate-webhook")
    async def simulate_webhook(
        request: Request,
        event: HelpdeskWebhook,
    ) -> dict[str, list[int]]:
        """Send one webhook or two identical deliveries when duplicate mode is on."""
        state: MockState = request.app.state.mock
        target_count = 2 if state.behavior.duplicate_webhooks else 1
        payload = event.model_dump(mode="json")
        if state.webhook_client is not None:
            statuses = []
            for _ in range(target_count):
                result = await state.webhook_client.post(state.api_url, json=payload)
                statuses.append(result.status_code)
        else:
            async with httpx.AsyncClient(timeout=2.0) as client:
                statuses = []
                for _ in range(target_count):
                    result = await client.post(state.api_url, json=payload)
                    statuses.append(result.status_code)
        return {"attempts": statuses}

    @app.get("/deliveries")
    async def get_deliveries(request: Request) -> list[dict[str, str]]:
        """Expose recorded replies for local integration testing."""
        state: MockState = request.app.state.mock
        return [
            {
                "ticket_id": delivery.ticket_id,
                "idempotency_key": delivery.idempotency_key,
                "message": delivery.message,
            }
            for delivery in state.delivery_log
        ]

    @app.get("/health")
    async def health() -> dict[str, str]:
        """Report that the mock HTTP process is responding."""
        return {"status": "OK"}

    return app


app = create_mock_app()
