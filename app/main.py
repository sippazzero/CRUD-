"""FastAPI application entry point."""

import logging
from typing import Annotated
from uuid import UUID

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import (
    HTMLResponse,
    PlainTextResponse,
    RedirectResponse,
    Response,
)
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db import get_session
from app.logging_config import configure_logging
from app.models import Ticket
from app.schemas import HelpdeskWebhook
from app.services.ingest import ingest_ticket

configure_logging(get_settings().log_level)
app: FastAPI = FastAPI(title="Helpdesk AI")
templates = Jinja2Templates(directory="app/templates")
logger: logging.Logger = logging.getLogger(__name__)


async def _render_moderation(
    request: Request,
    session: AsyncSession,
    *,
    error: str | None = None,
    status_code: int = 200,
) -> HTMLResponse:
    """Render the review queue with current drafts after a conflict or list request."""
    result = await session.scalars(
        select(Ticket)
        .where(Ticket.status == "draft_ready")
        .order_by(Ticket.created_at, Ticket.id)
    )
    return templates.TemplateResponse(
        request=request,
        name="moderation.html",
        context={"tickets": result.all(), "error": error},
        status_code=status_code,
    )


@app.get("/health", response_class=PlainTextResponse)
async def health() -> str:
    """Expose a lightweight process health check without database dependency."""
    return "OK"


@app.get("/operator/moderation", response_class=HTMLResponse)
async def operator_moderation(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> HTMLResponse:
    """Render drafts that are waiting for an operator decision."""
    return await _render_moderation(request, session)


@app.post("/operator/approve/{ticket_id}")
async def approve_ticket(
    ticket_id: UUID,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
    draft_text: Annotated[str, Form()],
) -> Response:
    """Approve the submitted draft only while the ticket remains reviewable."""
    async with session.begin():
        result = await session.execute(
            update(Ticket)
            .where(Ticket.id == ticket_id, Ticket.status == "draft_ready")
            .values(
                status="approved",
                draft_text=draft_text,
                approved_at=func.now(),
                updated_at=func.now(),
            )
        )

    if result.rowcount == 0:
        return await _render_moderation(
            request,
            session,
            error="This ticket was already processed or is no longer ready.",
            status_code=409,
        )
    return RedirectResponse("/operator/moderation", status_code=303)


@app.post("/operator/reject/{ticket_id}")
async def reject_ticket(
    ticket_id: UUID,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> Response:
    """Reject the draft only while the ticket remains in the review queue."""
    async with session.begin():
        result = await session.execute(
            update(Ticket)
            .where(Ticket.id == ticket_id, Ticket.status == "draft_ready")
            .values(status="rejected", updated_at=func.now())
        )

    if result.rowcount == 0:
        return await _render_moderation(
            request,
            session,
            error="This ticket was already processed or is no longer ready.",
            status_code=409,
        )
    return RedirectResponse("/operator/moderation", status_code=303)


@app.post("/webhooks/helpdesk")
async def receive_helpdesk_webhook(
    event: HelpdeskWebhook,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, str]:
    """Persist and acknowledge an event without performing downstream work."""
    if not get_settings().is_reply_url_allowed(str(event.reply_url)):
        raise HTTPException(
            status_code=422,
            detail="reply_url host is not in HELPDESK_ALLOWED_REPLY_HOSTS",
        )

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
