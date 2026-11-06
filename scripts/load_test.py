"""Exercise webhook throughput and follow accepted tickets to terminal states."""

import argparse
import asyncio
import math
import os
import sys
import time
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from uuid import uuid4

import httpx
from app.models import Ticket
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

TERMINAL_STATUSES = frozenset({"failed", "rejected", "send_failed", "sent"})
TICKETS_PER_DUPLICATE = 10


@dataclass(frozen=True)
class WebhookDelivery:
    """One webhook attempt, including its stable event and ticket identifiers."""

    event_id: str
    ticket_id: str


@dataclass(frozen=True)
class TicketSnapshot:
    """Database state needed to approve drafts and report final outcomes."""

    id: str
    event_id: str
    status: str
    draft_text: str | None


def build_deliveries(unique_tickets: int, run_id: str) -> list[WebhookDelivery]:
    """Create unique events followed by a 10% replay of previously sent events."""
    deliveries = [
        WebhookDelivery(
            event_id=f"load-{run_id}-{index}",
            ticket_id=f"T-load-{run_id}-{index}",
        )
        for index in range(unique_tickets)
    ]
    duplicate_count = round(unique_tickets / TICKETS_PER_DUPLICATE)
    deliveries.extend(
        WebhookDelivery(
            event_id=deliveries[index].event_id,
            ticket_id=deliveries[index].ticket_id,
        )
        for index in range(duplicate_count)
    )
    return deliveries


async def send_webhooks(
    client: httpx.AsyncClient,
    api_url: str,
    deliveries: Sequence[WebhookDelivery],
    duration_seconds: float,
) -> tuple[list[float], list[str]]:
    """Schedule deliveries evenly across the requested duration and measure latency."""
    start = time.monotonic()
    latencies: list[float] = []
    errors: list[str] = []

    async def send_one(index: int, delivery: WebhookDelivery) -> None:
        scheduled_at = start + duration_seconds * index / len(deliveries)
        await asyncio.sleep(max(0.0, scheduled_at - time.monotonic()))
        payload = {
            "event_id": delivery.event_id,
            "ticket_id": delivery.ticket_id,
            "customer_email": "load-test@example.com",
            "message": "Не можу експортувати звіт у PDF",
            "reply_url": "http://helpdesk-mock:8001/reply",
        }
        request_started = time.monotonic()
        try:
            response = await client.post(
                f"{api_url}/webhooks/helpdesk",
                json=payload,
            )
            latencies.append(time.monotonic() - request_started)
            if response.status_code != 200:
                errors.append(
                    f"{delivery.event_id}: webhook returned {response.status_code}"
                )
        except httpx.HTTPError as error:
            latencies.append(time.monotonic() - request_started)
            errors.append(f"{delivery.event_id}: {type(error).__name__}: {error}")

    await asyncio.gather(
        *(send_one(index, delivery) for index, delivery in enumerate(deliveries))
    )
    return latencies, errors


async def load_ticket_snapshots(
    session_factory: async_sessionmaker[AsyncSession],
    event_prefix: str,
) -> list[TicketSnapshot]:
    """Read only rows created by this run, leaving existing tickets untouched."""
    async with session_factory() as session:
        result = await session.execute(
            select(Ticket.id, Ticket.event_id, Ticket.status, Ticket.draft_text).where(
                Ticket.event_id.like(f"{event_prefix}%")
            )
        )
    return [
        TicketSnapshot(
            id=str(row.id),
            event_id=row.event_id,
            status=row.status,
            draft_text=row.draft_text,
        )
        for row in result
    ]


async def approve_ready_tickets(
    client: httpx.AsyncClient,
    api_url: str,
    snapshots: Sequence[TicketSnapshot],
    approved_events: set[str],
) -> list[str]:
    """Approve generated drafts through the operator endpoint for end-to-end testing."""
    ready = [
        ticket
        for ticket in snapshots
        if ticket.status == "draft_ready" and ticket.event_id not in approved_events
    ]
    semaphore = asyncio.Semaphore(10)
    errors: list[str] = []

    async def approve(ticket: TicketSnapshot) -> None:
        async with semaphore:
            try:
                response = await client.post(
                    f"{api_url}/operator/approve/{ticket.id}",
                    data={"draft_text": ticket.draft_text or ""},
                )
            except httpx.HTTPError as error:
                errors.append(
                    f"{ticket.event_id}: approval request failed: "
                    f"{type(error).__name__}: {error}"
                )
                return

            if response.status_code not in {303, 409}:
                errors.append(
                    f"{ticket.event_id}: approval returned {response.status_code}"
                )
            approved_events.add(ticket.event_id)

    await asyncio.gather(*(approve(ticket) for ticket in ready))
    return errors


async def wait_for_terminal_tickets(
    client: httpx.AsyncClient,
    api_url: str,
    session_factory: async_sessionmaker[AsyncSession],
    event_prefix: str,
    unique_tickets: int,
    timeout_seconds: float,
    poll_interval_seconds: float,
) -> tuple[list[TicketSnapshot], list[str], bool]:
    """Approve drafts and poll until all run tickets finish or the timeout expires."""
    deadline = time.monotonic() + timeout_seconds
    approved_events: set[str] = set()
    errors: list[str] = []

    while True:
        snapshots = await load_ticket_snapshots(session_factory, event_prefix)
        errors.extend(
            await approve_ready_tickets(
                client,
                api_url,
                snapshots,
                approved_events,
            )
        )
        terminal_count = sum(ticket.status in TERMINAL_STATUSES for ticket in snapshots)
        if len(snapshots) == unique_tickets and terminal_count == unique_tickets:
            return snapshots, errors, True
        if time.monotonic() >= deadline:
            return snapshots, errors, False
        await asyncio.sleep(poll_interval_seconds)


def print_summary(
    *,
    unique_tickets: int,
    snapshots: Sequence[TicketSnapshot],
    latencies: Sequence[float],
) -> None:
    """Print measured webhook latency and per-run database outcome counts."""
    status_counts = Counter(ticket.status for ticket in snapshots)
    ordered_latencies = sorted(latencies)
    p95_index = max(0, math.ceil(0.95 * len(ordered_latencies)) - 1)
    p95 = ordered_latencies[p95_index] if ordered_latencies else float("nan")
    longest = max(ordered_latencies, default=float("nan"))

    print(f"Unique tickets sent: {unique_tickets}")
    print(f"Webhook requests sent: {len(latencies)}")
    print(f"Rows in DB for this run: {len(snapshots)}")
    print(
        "Per-status counts: "
        + (
            ", ".join(
                f"{status}={count}" for status, count in sorted(status_counts.items())
            )
            or "none"
        )
    )
    print(f"Webhook latency p95: {p95:.3f}s")
    print(f"Longest webhook latency: {longest:.3f}s")


async def run_load_test(args: argparse.Namespace) -> int:
    """Run the webhook load, report outcomes, and return a process exit status."""
    run_id = uuid4().hex
    event_prefix = f"load-{run_id}-"
    deliveries = build_deliveries(args.unique_tickets, run_id)
    engine = create_async_engine(os.environ["DATABASE_URL"])
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    async with httpx.AsyncClient(
        timeout=args.http_timeout_seconds,
        follow_redirects=False,
        limits=httpx.Limits(max_connections=100, max_keepalive_connections=20),
    ) as client:
        latencies, errors = await send_webhooks(
            client,
            args.api_url.rstrip("/"),
            deliveries,
            args.duration_seconds,
        )
        snapshots, poll_errors, completed = await wait_for_terminal_tickets(
            client,
            args.api_url.rstrip("/"),
            session_factory,
            event_prefix,
            args.unique_tickets,
            args.timeout_seconds,
            args.poll_interval_seconds,
        )
    await engine.dispose()

    errors.extend(poll_errors)
    print_summary(
        unique_tickets=args.unique_tickets,
        snapshots=snapshots,
        latencies=latencies,
    )
    if errors:
        print(f"Errors: {len(errors)}", file=sys.stderr)
        for error in errors[:10]:
            print(f"- {error}", file=sys.stderr)
    if not completed:
        print(
            f"Timed out with {len(snapshots)} rows; "
            f"{sum(ticket.status in TERMINAL_STATUSES for ticket in snapshots)} "
            "tickets reached a terminal status.",
            file=sys.stderr,
        )
    if len(latencies) != len(deliveries):
        print(
            f"Expected {len(deliveries)} webhook measurements, got {len(latencies)}.",
            file=sys.stderr,
        )
    if latencies and max(latencies) >= 2.0:
        print("Webhook latency exceeded the 2-second requirement.", file=sys.stderr)
    if len(snapshots) != args.unique_tickets:
        print(
            f"Expected {args.unique_tickets} database rows, got {len(snapshots)}.",
            file=sys.stderr,
        )
    return int(
        bool(errors)
        or not completed
        or len(latencies) != len(deliveries)
        or (bool(latencies) and max(latencies) >= 2.0)
        or len(snapshots) != args.unique_tickets
    )


def parse_args() -> argparse.Namespace:
    """Parse the bounded workload and polling settings from command-line options."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--unique-tickets",
        type=int,
        default=500,
        help="number of distinct ticket events to submit (default: 500)",
    )
    parser.add_argument(
        "--duration-seconds",
        type=float,
        default=60,
        help="time window for webhook delivery (default: 60; use 600 for full run)",
    )
    parser.add_argument(
        "--timeout-seconds",
        type=float,
        default=1200,
        help="maximum time to wait for terminal ticket states (default: 1200)",
    )
    parser.add_argument(
        "--poll-interval-seconds",
        type=float,
        default=1,
        help="database polling interval (default: 1)",
    )
    parser.add_argument(
        "--http-timeout-seconds",
        type=float,
        default=10,
        help="HTTP request timeout (default: 10)",
    )
    parser.add_argument(
        "--api-url",
        default=os.getenv("LOAD_TEST_API_URL", "http://api:8000"),
        help="API base URL (default: LOAD_TEST_API_URL or http://api:8000)",
    )
    args = parser.parse_args()
    if args.unique_tickets < 1:
        parser.error("--unique-tickets must be positive")
    if (
        args.duration_seconds <= 0
        or args.timeout_seconds <= 0
        or args.poll_interval_seconds <= 0
        or args.http_timeout_seconds <= 0
    ):
        parser.error("durations and timeouts must be positive")
    return args


def main() -> None:
    """Run the asynchronous test and exit nonzero when a check fails."""
    raise SystemExit(asyncio.run(run_load_test(parse_args())))


if __name__ == "__main__":
    main()
