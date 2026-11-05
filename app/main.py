"""FastAPI application entry point."""

import logging
from typing import Annotated

from fastapi import Depends, FastAPI
from fastapi.responses import PlainTextResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db import get_session
from app.logging_config import configure_logging
from app.schemas import HelpdeskWebhook
from app.services.ingest import ingest_ticket

configure_logging(get_settings().log_level)
app: FastAPI = FastAPI(title="Helpdesk AI")
logger: logging.Logger = logging.getLogger(__name__)


@app.get("/health", response_class=PlainTextResponse)
async def health() -> str:
    """Expose a lightweight process health check without database dependency."""
    return "OK"


@app.post("/webhooks/helpdesk")
async def receive_helpdesk_webhook(
    event: HelpdeskWebhook,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, str]:
    """Persist and acknowledge an event without performing downstream work."""
    accepted = await ingest_ticket(session, event)
    logger.info(
        "Helpdesk webhook processed",
        extra={
            "event_id": event.event_id,
            "ticket_id": event.ticket_id,
            "outcome": "accepted" if accepted else "duplicate",
        },
    )
    return {"status": "accepted"}
