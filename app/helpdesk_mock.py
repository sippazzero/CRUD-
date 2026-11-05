"""Minimal HTTP service placeholder for the helpdesk mock container."""

from fastapi import FastAPI
from fastapi.responses import PlainTextResponse

app: FastAPI = FastAPI(title="Helpdesk Mock")


@app.get("/health", response_class=PlainTextResponse)
async def health() -> str:
    """Keep the stub probeable while its helpdesk routes are not implemented."""
    return "OK"
