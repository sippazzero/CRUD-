"""Concurrent background workers for durable ticket draft generation."""

import asyncio
import logging
import random
import signal
from collections.abc import Callable
from datetime import timedelta
from typing import Protocol
from uuid import UUID

import httpx
from sqlalchemy import func, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings, get_settings
from app.db import SessionFactory, engine
from app.logging_config import configure_logging
from app.models import KnowledgeArticle, Ticket
from app.services.knowledge import find_relevant_articles
from app.services.llm import GeneratedDraft, LLMClient
from app.services.queue import (
    claim_for_delivery,
    claim_next,
    recover_stuck,
    recover_stuck_sending,
)
from app.services.rate_limit import PostgresDailyCallQuota, PostgresTokenBucket
from app.services.sender import send_reply

configure_logging(get_settings().log_level)
logger: logging.Logger = logging.getLogger(__name__)


class DraftGenerator(Protocol):
    """Contract required by ticket processing for an LLM draft provider."""

    async def generate(
        self,
        message: str,
        articles: list[KnowledgeArticle],
    ) -> GeneratedDraft:
        """Return draft text and the stable slugs of its evidence."""


Uniform = Callable[[float, float], float]
SessionFactoryType = async_sessionmaker[AsyncSession]


def retry_delay(
    attempt: int,
    settings: Settings,
    *,
    uniform: Uniform = random.uniform,
) -> float:
    """Calculate capped exponential backoff with jitter."""
    exponential = min(
        settings.worker_backoff_max_seconds,
        settings.worker_backoff_base_seconds * 2 ** min(attempt - 1, 30),
    )
    return uniform(exponential / 2, exponential)


async def process_next(
    session_factory: SessionFactoryType,
    generator: DraftGenerator,
    settings: Settings,
    *,
    uniform: Uniform = random.uniform,
    helpdesk_client: httpx.AsyncClient | None = None,
) -> bool:
    """Claim and process one ticket, persisting every durable state transition."""
    async with session_factory() as session:
        recovered = await recover_stuck_sending(
            session,
            max_attempts=settings.sender_max_attempts,
        )
    for delivery in recovered:
        logger.warning(
            "Delivery lease recovered",
            extra={
                "ticket_id": delivery.ticket_id,
                "attempt": delivery.attempts,
                "status": delivery.status,
            },
        )

    async with session_factory() as session:
        ticket = await claim_next(
            session,
            lease=timedelta(seconds=settings.worker_lease_seconds),
        )
        if ticket is None:
            delivery = await claim_for_delivery(
                session,
                lease=timedelta(seconds=settings.sender_lease_seconds),
            )
            if delivery is None:
                return False

            logger.info(
                "Ticket transition",
                extra={
                    "ticket_id": delivery.ticket_id,
                    "attempt": delivery.attempts,
                    "status": "sending",
                },
            )
            await send_reply(
                session_factory=session_factory,
                ticket=delivery,
                settings=settings,
                client=helpdesk_client,
                uniform=uniform,
            )
            return True

        ticket_id = ticket.ticket_id
        ticket_pk: UUID = ticket.id
        message = ticket.message
        attempt = ticket.attempts

    logger.info(
        "Ticket transition",
        extra={
            "ticket_id": ticket_id,
            "attempt": attempt,
            "status": "generating",
        },
    )

    try:
        async with session_factory() as session:
            articles = await find_relevant_articles(session, message)
        draft = await generator.generate(message, articles)
    except Exception as error:
        await _record_failure(
            session_factory,
            ticket_pk=ticket_pk,
            ticket_id=ticket_id,
            attempt=attempt,
            error=error,
            settings=settings,
            uniform=uniform,
        )
        return True

    async with session_factory() as session:
        async with session.begin():
            result = await session.execute(
                update(Ticket)
                .where(
                    Ticket.id == ticket_pk,
                    Ticket.status == "generating",
                    Ticket.attempts == attempt,
                )
                .values(
                    status="draft_ready",
                    draft_text=draft.text,
                    cited_article_slugs=draft.cited_article_slugs,
                    locked_until=None,
                    error=None,
                    updated_at=func.now(),
                )
            )

    if result.rowcount:
        logger.info(
            "Ticket transition",
            extra={
                "ticket_id": ticket_id,
                "attempt": attempt,
                "status": "draft_ready",
                "cited_article_count": len(draft.cited_article_slugs),
            },
        )
    else:
        logger.warning(
            "Discarded stale worker result",
            extra={"ticket_id": ticket_id, "attempt": attempt},
        )
    return True


async def _record_failure(
    session_factory: SessionFactoryType,
    *,
    ticket_pk: UUID,
    ticket_id: str,
    attempt: int,
    error: Exception,
    settings: Settings,
    uniform: Uniform,
) -> None:
    """Persist retry timing or terminal failure without overwriting newer work."""
    terminal = attempt >= settings.worker_max_attempts
    status = "failed" if terminal else "new"
    delay = retry_delay(attempt, settings, uniform=uniform)
    error_text = str(error) or type(error).__name__
    if settings.llm_api_key:
        error_text = error_text.replace(settings.llm_api_key, "[REDACTED]")

    async with session_factory() as session:
        async with session.begin():
            result = await session.execute(
                update(Ticket)
                .where(
                    Ticket.id == ticket_pk,
                    Ticket.status == "generating",
                    Ticket.attempts == attempt,
                )
                .values(
                    status=status,
                    next_attempt_at=func.now() + timedelta(seconds=delay),
                    locked_until=None,
                    error=error_text[:2000],
                    updated_at=func.now(),
                )
            )

    if result.rowcount:
        logger.warning(
            "Ticket transition",
            extra={
                "ticket_id": ticket_id,
                "attempt": attempt,
                "status": status,
                "error_type": type(error).__name__,
                "retry_delay_seconds": delay if not terminal else None,
            },
        )
    else:
        logger.warning(
            "Ignored stale worker failure",
            extra={"ticket_id": ticket_id, "attempt": attempt},
        )


async def worker_loop(
    worker_number: int,
    stop_event: asyncio.Event,
    session_factory: SessionFactoryType,
    generator: DraftGenerator,
    settings: Settings,
    helpdesk_client: httpx.AsyncClient,
) -> None:
    """Continuously recover expired leases and process due tickets."""
    while not stop_event.is_set():
        try:
            async with session_factory() as session:
                recovered = await recover_stuck(session)
            for ticket_id, attempt in recovered:
                logger.warning(
                    "Ticket lease recovered",
                    extra={
                        "ticket_id": ticket_id,
                        "attempt": attempt,
                        "status": "new",
                    },
                )

            if await process_next(
                session_factory,
                generator,
                settings,
                helpdesk_client=helpdesk_client,
            ):
                continue
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(
                "Worker iteration failed",
                extra={"worker_number": worker_number},
            )

        try:
            await asyncio.wait_for(
                stop_event.wait(),
                timeout=settings.worker_poll_seconds,
            )
        except TimeoutError:
            pass


async def run_workers(settings: Settings, stop_event: asyncio.Event) -> None:
    """Run worker tasks until shutdown, sharing clients and the database pool."""
    limiter = PostgresTokenBucket(SessionFactory)
    generator = LLMClient(
        settings,
        daily_call_quota=PostgresDailyCallQuota(SessionFactory),
        request_limiter=limiter,
    )
    async with httpx.AsyncClient(follow_redirects=False) as helpdesk_client:
        tasks = [
            asyncio.create_task(
                worker_loop(
                    worker_number,
                    stop_event,
                    SessionFactory,
                    generator,
                    settings,
                    helpdesk_client,
                ),
                name=f"ticket-worker-{worker_number}",
            )
            for worker_number in range(settings.worker_concurrency)
        ]
        logger.info(
            "Worker service started",
            extra={"worker_count": settings.worker_concurrency},
        )
        try:
            await stop_event.wait()
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
    await generator.close()
    logger.info("Worker service stopped")


async def main() -> None:
    """Run configured concurrent workers and stop promptly on container signals."""
    settings = get_settings()
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for stop_signal in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(stop_signal, stop_event.set)
        except NotImplementedError:
            signal.signal(stop_signal, lambda _signum, _frame: stop_event.set())

    try:
        await run_workers(settings, stop_event)
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
