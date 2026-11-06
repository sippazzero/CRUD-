"""Integration tests for optional HTTP Basic Auth on operator routes."""

import httpx
import pytest
from app.config import Settings


@pytest.mark.asyncio
async def test_operator_routes_require_credentials_when_enabled(
    api_client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Protected operator pages reject requests without Basic credentials."""
    monkeypatch.setattr(
        "app.main.get_settings",
        lambda: Settings(operator_username="reviewer", operator_password="secret"),
    )

    response = await api_client.get("/operator/moderation")

    assert response.status_code == 401
    assert response.headers["www-authenticate"].startswith("Basic")


@pytest.mark.asyncio
async def test_operator_routes_accept_correct_credentials(
    api_client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Correct Basic credentials grant access to operator pages."""
    monkeypatch.setattr(
        "app.main.get_settings",
        lambda: Settings(operator_username="reviewer", operator_password="secret"),
    )

    response = await api_client.get(
        "/operator/moderation",
        auth=("reviewer", "secret"),
    )

    assert response.status_code == 200


@pytest.mark.asyncio
async def test_operator_routes_reject_wrong_credentials(
    api_client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Invalid Basic credentials do not reveal or grant operator access."""
    monkeypatch.setattr(
        "app.main.get_settings",
        lambda: Settings(operator_username="reviewer", operator_password="secret"),
    )

    response = await api_client.get(
        "/operator/moderation",
        auth=("reviewer", "incorrect"),
    )

    assert response.status_code == 401


@pytest.mark.asyncio
async def test_health_remains_open_when_operator_auth_is_enabled(
    api_client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Operator credentials do not gate the process health check."""
    monkeypatch.setattr(
        "app.main.get_settings",
        lambda: Settings(operator_username="reviewer", operator_password="secret"),
    )

    response = await api_client.get("/health")

    assert response.status_code == 200
    assert response.text == "OK"
