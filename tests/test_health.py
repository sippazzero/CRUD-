"""Tests for the API health contract."""

import httpx
import pytest
from app.main import app


@pytest.mark.asyncio
async def test_health_returns_ok() -> None:
    """The health endpoint remains a lightweight plain-text readiness response."""
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://test",
    ) as client:
        response: httpx.Response = await client.get("/health")

    assert response.status_code == 200
    assert response.text == "OK"
    assert response.headers["content-type"].startswith("text/plain")
