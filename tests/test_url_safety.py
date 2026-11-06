"""Tests for reply URL validation at webhook and delivery boundaries."""

from collections.abc import Sequence

import pytest
from app.config import Settings
from app.services.url_safety import is_safe_reply_url


@pytest.mark.asyncio
async def test_public_reply_hostname_is_accepted() -> None:
    """Public DNS results permit normal helpdesk callback hosts."""

    async def resolve(hostname: str, port: int) -> Sequence[str]:
        assert hostname == "support.example.net"
        assert port == 443
        return ["93.184.216.34"]

    settings = Settings()

    assert await is_safe_reply_url(
        "https://support.example.net/replies",
        settings,
        resolver=resolve,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "addresses",
    [
        ["127.0.0.1"],
        ["10.0.0.4"],
        ["169.254.169.254"],
        ["2606:4700:4700::1111", "fd00::1"],
        ["100.100.100.200"],
        ["168.63.129.16"],
        ["fd00:ec2::254"],
    ],
)
async def test_any_non_public_dns_answer_blocks_reply_host(
    addresses: list[str],
) -> None:
    """One private answer rejects a mixed DNS response to prevent rebinding."""

    async def resolve(hostname: str, port: int) -> Sequence[str]:
        return addresses

    assert not await is_safe_reply_url(
        "https://support.example.net/replies",
        Settings(),
        resolver=resolve,
    )


@pytest.mark.asyncio
async def test_compose_mock_host_is_explicitly_permitted() -> None:
    """The configured internal mock hostname is the only private-host exception."""

    async def resolve_private_service(hostname: str, port: int) -> Sequence[str]:
        return ["10.0.0.1"]

    assert await is_safe_reply_url(
        "http://helpdesk-mock:8001/reply",
        Settings(helpdesk_mock_host="helpdesk-mock"),
        resolver=resolve_private_service,
    )
    assert not await is_safe_reply_url(
        "http://api:8000/health",
        Settings(helpdesk_mock_host="helpdesk-mock"),
        resolver=resolve_private_service,
    )


@pytest.mark.asyncio
async def test_documentation_sample_host_is_ingest_only_when_unresolvable() -> None:
    """The assignment's reserved sample URL can be persisted but not sent."""

    async def unresolved(hostname: str, port: int) -> Sequence[str]:
        return []

    url = "https://helpdesk.example.com/tickets/T-1042/replies"
    settings = Settings()

    assert await is_safe_reply_url(
        url,
        settings,
        allow_unresolvable_sample=True,
        resolver=unresolved,
    )
    assert not await is_safe_reply_url(url, settings, resolver=unresolved)
