"""Idempotent delivery of approved replies to the helpdesk."""

import logging
import random
from collections.abc import Callable
from datetime import timedelta

import httpx
from sqlalchemy import func, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.models import Ticket
from app.services.queue import DeliveryClaim

logger = logging.getLogger(__name__)

Uniform = Callable[[float, float], float]
SessionFactoryType = async_sessionmaker[AsyncSession]


def delivery_retry_delay(
    attempt: int,
    settings: Settings,
    *,
    uniform: Uniform = random.uniform,
) -> float:
    """Calculate capped exponential backoff with jitter for a retry."""
    exponential = min(
        settings.worker_backoff_max_seconds,
        settings.worker_backoff_base_seconds * 2 ** min(attempt - 1, 30),
    )
    return uniform(exponential / 2, exponential)


async def send_reply(
    session_factory: SessionFactoryType,
    ticket: DeliveryClaim,
    settings: Settings,
    *,
    client: httpx.AsyncClient | None = None,
    uniform: Uniform = random.uniform,
) -> bool:
    """Send one leased reply and persist the outcome only while its lease is owned."""
    idempotency_key = f"reply:{ticket.ticket_id}"
    payload = {"ticket_id": ticket.ticket_id, "message": ticket.draft_text}
    if not settings.is_reply_url_allowed(ticket.reply_url):
        await _record_delivery_failure(
            session_factory,
            ticket,
            error="Helpdesk reply host is not in HELPDESK_ALLOWED_REPLY_HOSTS",
            settings=settings,
            uniform=uniform,
            terminal=True,
        )
        return False

    try:
        if client is None:
            async with httpx.AsyncClient(
                timeout=settings.sender_timeout_seconds,
                follow_redirects=False,
            ) as request_client:
                response = await request_client.post(
                    ticket.reply_url,
                    json=payload,
                    headers={"Idempotency-Key": idempotency_key},
                    timeout=settings.sender_timeout_seconds,
                )
        else:
            response = await client.post(
                ticket.reply_url,
                json=payload,
                headers={"Idempotency-Key": idempotency_key},
                timeout=settings.sender_timeout_seconds,
                follow_redirects=False,
            )
    except httpx.TransportError as error:
        await _record_delivery_failure(
            session_factory,
            ticket,
            error=f"Helpdesk transport failed: {type(error).__name__}",
            settings=settings,
            uniform=uniform,
        )
        return False

    if 200 <= response.status_code < 300:
        return await _record_sent(session_factory, ticket)

    if 500 <= response.status_code < 600 or response.status_code in (408, 429):
        await _record_delivery_failure(
            session_factory,
            ticket,
            error=f"Retryable helpdesk response: {response.status_code}",
            settings=settings,
            uniform=uniform,
        )
        return False

    await _record_delivery_failure(
        session_factory,
        ticket,
        error=f"Permanent helpdesk response: {response.status_code}",
        settings=settings,
        terminal=True,
        uniform=uniform,
    )
    return False


async def _record_sent(
    session_factory: SessionFactoryType,
    ticket: DeliveryClaim,
) -> bool:
    """Mark a reply sent only if this exact lease still owns the ticket."""
    async with session_factory() as session:
        async with session.begin():
            result = await session.execute(
                update(Ticket)
                .where(
                    Ticket.id == ticket.id,
                    Ticket.status == "sending",
                    Ticket.delivery_attempts == ticket.attempts,
                    Ticket.locked_until == ticket.locked_until,
                )
                .values(
                    status="sent",
                    sent_at=func.now(),
                    locked_until=None,
                    error=None,
                    updated_at=func.now(),
                )
            )

    if result.rowcount == 0:
        logger.warning(
            "Discarded stale delivery success",
            extra={"ticket_id": ticket.ticket_id, "attempt": ticket.attempts},
        )
        return False
    logger.info(
        "Ticket transition",
        extra={
            "ticket_id": ticket.ticket_id,
            "attempt": ticket.attempts,
            "status": "sent",
        },
    )
    return True


async def _record_delivery_failure(
    session_factory: SessionFactoryType,
    ticket: DeliveryClaim,
    *,
    error: str,
    settings: Settings,
    uniform: Uniform,
    terminal: bool = False,
) -> None:
    """Schedule a transient retry or record a terminal failure for the owning lease."""
    is_terminal = terminal or ticket.attempts >= settings.sender_max_attempts
    status = "send_failed" if is_terminal else "approved"
    values: dict[str, object] = {
        "status": status,
        "locked_until": None,
        "error": error[:2000],
        "updated_at": func.now(),
    }
    if not is_terminal:
        delay = delivery_retry_delay(ticket.attempts, settings, uniform=uniform)
        values["next_attempt_at"] = func.now() + timedelta(seconds=delay)

    async with session_factory() as session:
        async with session.begin():
            result = await session.execute(
                update(Ticket)
                .where(
                    Ticket.id == ticket.id,
                    Ticket.status == "sending",
                    Ticket.delivery_attempts == ticket.attempts,
                    Ticket.locked_until == ticket.locked_until,
                )
                .values(**values)
            )

    if result.rowcount == 0:
        logger.warning(
            "Ignored stale delivery failure",
            extra={"ticket_id": ticket.ticket_id, "attempt": ticket.attempts},
        )
        return
    logger.warning(
        "Ticket transition",
        extra={
            "ticket_id": ticket.ticket_id,
            "attempt": ticket.attempts,
            "status": status,
            "error": error,
        },
    )
