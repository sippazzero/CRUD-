"""Asynchronous validation for webhook-controlled helpdesk callback URLs."""

import asyncio
import ipaddress
import socket
from collections.abc import Awaitable, Callable, Sequence
from urllib.parse import urlsplit

from app.config import Settings

AddressResolver = Callable[[str, int], Awaitable[Sequence[str]]]
_BLOCKED_METADATA_ADDRESSES = {
    ipaddress.ip_address("100.100.100.200"),
    ipaddress.ip_address("168.63.129.16"),
    ipaddress.ip_address("fd00:ec2::254"),
}


async def _resolve_addresses(hostname: str, port: int) -> Sequence[str]:
    """Resolve all stream addresses so mixed public/private DNS is rejected."""
    records = await asyncio.get_running_loop().getaddrinfo(
        hostname,
        port,
        type=socket.SOCK_STREAM,
    )
    return tuple(record[4][0] for record in records)


async def is_safe_reply_url(
    raw_url: str,
    settings: Settings,
    *,
    allow_unresolvable_sample: bool = False,
    resolver: AddressResolver = _resolve_addresses,
) -> bool:
    """Reject callback URLs that could address loopback or non-public networks.

    The Compose mock is a deliberate private-network exception, identified by
    its configured service hostname. The assignment's reserved example URL is
    accepted only at ingestion when DNS cannot resolve it; delivery validation
    never grants that exception.
    """
    try:
        parsed_url = urlsplit(raw_url)
        hostname = parsed_url.hostname
        port = parsed_url.port
    except ValueError:
        return False

    if (
        parsed_url.scheme.lower() not in {"http", "https"}
        or hostname is None
        or parsed_url.username is not None
        or parsed_url.password is not None
    ):
        return False

    normalized_host = hostname.rstrip(".").lower()
    if normalized_host == settings.helpdesk_mock_host.rstrip(".").lower():
        return True

    try:
        literal_address = ipaddress.ip_address(normalized_host)
    except ValueError:
        literal_address = None
    if literal_address is not None:
        return literal_address.is_global

    try:
        resolved = await asyncio.wait_for(
            resolver(
                normalized_host, port or (443 if parsed_url.scheme == "https" else 80)
            ),
            timeout=1.0,
        )
    except (OSError, TimeoutError, ValueError):
        resolved = ()

    if not resolved:
        return allow_unresolvable_sample and normalized_host == "helpdesk.example.com"

    try:
        return all(
            (parsed := ipaddress.ip_address(address)).is_global
            and parsed not in _BLOCKED_METADATA_ADDRESSES
            for address in resolved
        )
    except ValueError:
        return False
