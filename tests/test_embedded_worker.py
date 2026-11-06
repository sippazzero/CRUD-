"""Tests for optional worker lifecycle integration with the API."""

import asyncio

import pytest
from app.config import Settings
from app.main import app, lifespan


@pytest.mark.asyncio
async def test_embedded_worker_starts_and_stops_with_api_lifespan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The hosted worker shares API process lifetime and receives shutdown."""
    started = asyncio.Event()
    stopped = asyncio.Event()

    async def fake_run_workers(
        settings: Settings,
        stop_event: asyncio.Event,
    ) -> None:
        assert settings.run_embedded_worker
        started.set()
        await stop_event.wait()
        stopped.set()

    monkeypatch.setattr(
        "app.main.get_settings",
        lambda: Settings(run_embedded_worker=True),
    )
    monkeypatch.setattr("app.worker.run_workers", fake_run_workers)

    async with lifespan(app):
        await asyncio.wait_for(started.wait(), timeout=1)

    assert stopped.is_set()


@pytest.mark.asyncio
async def test_embedded_worker_is_disabled_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Docker Compose API instances remain separate from the worker service."""
    monkeypatch.setattr("app.main.get_settings", lambda: Settings())

    async with lifespan(app):
        pass
