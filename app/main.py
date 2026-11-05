"""FastAPI application entry point."""

from fastapi import FastAPI
from fastapi.responses import PlainTextResponse

from app.config import get_settings
from app.logging_config import configure_logging

configure_logging(get_settings().log_level)
app: FastAPI = FastAPI(title="Helpdesk AI")


@app.get("/health", response_class=PlainTextResponse)
async def health() -> str:
    """Expose a lightweight process health check without database dependency."""
    return "OK"
