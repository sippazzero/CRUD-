"""Unit and PostgreSQL integration tests for LLM generation and throttling."""

import asyncio
from datetime import UTC, date, datetime, timedelta

import httpx
import pytest
from app.config import Settings
from app.models import KnowledgeArticle, LLMDailyCallUsage, LLMRateLimitState
from app.services.llm import LLMClient
from app.services.rate_limit import PostgresDailyCallQuota, PostgresTokenBucket
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker


class FakeClock:
    """Provide deterministic wall-clock time for rate-limit tests."""

    def __init__(self) -> None:
        self.current = datetime(2026, 1, 1, tzinfo=UTC)

    def now(self) -> datetime:
        """Return the current simulated time."""
        return self.current

    async def sleep(self, seconds: float) -> None:
        """Advance simulated time instead of waiting in real time."""
        self.current += timedelta(seconds=seconds)


class RecordingLimiter:
    """Count rate-limit tokens consumed by actual provider attempts."""

    def __init__(self) -> None:
        self.calls = 0

    async def acquire(self) -> None:
        """Record one provider request slot."""
        self.calls += 1


@pytest.mark.asyncio
async def test_ollama_chat_request_and_response_format() -> None:
    """Ollama uses its documented chat endpoint, bearer header, and response."""
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "message": {
                    "role": "assistant",
                    "content": "Ось інструкція з експорту.",
                },
                "done": True,
            },
        )

    settings = Settings(
        llm_provider="ollama",
        llm_base_url="https://ollama.example",
        llm_model="gemma4:31b",
        llm_api_key="test-key",
        llm_static_fallback_enabled=False,
        llm_daily_call_cap=0,
    )
    article = KnowledgeArticle(
        slug="export-pdf-report",
        title="Експорт звіту",
        content="Оберіть формат PDF.",
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        draft = await LLMClient(settings, client=client).generate(
            "Як зберегти PDF?",
            [article],
        )

    assert len(requests) == 1
    assert requests[0].url == "https://ollama.example/api/chat"
    assert requests[0].headers["authorization"] == "Bearer test-key"
    assert b'"stream":false' in requests[0].content
    assert draft.text == "Ось інструкція з експорту."
    assert draft.cited_article_slugs == ["export-pdf-report"]


@pytest.mark.asyncio
async def test_ollama_timeout_is_propagated_for_worker_retry() -> None:
    """A provider timeout is surfaced so the durable worker can retry it."""

    def timeout(request: httpx.Request) -> httpx.Response:
        assert request.extensions["timeout"]["read"] == 0.01
        raise httpx.ReadTimeout("simulated timeout", request=request)

    settings = Settings(
        llm_provider="ollama",
        llm_base_url="https://ollama.example",
        llm_model="gemma4:31b",
        llm_api_key="test-key",
        llm_static_fallback_enabled=False,
        llm_timeout_seconds=0.01,
        llm_daily_call_cap=0,
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(timeout)) as client:
        generator = LLMClient(settings, client=client)
        with pytest.raises(httpx.ReadTimeout):
            await generator.generate("Question", [])


@pytest.mark.asyncio
async def test_ollama_http_error_is_propagated_for_worker_retry() -> None:
    """Ollama non-success statuses reach the worker retry policy unchanged."""

    def reject(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"error": "temporarily unavailable"})

    settings = Settings(
        llm_provider="ollama",
        llm_base_url="https://ollama.example",
        llm_api_key="test-key",
        llm_static_fallback_enabled=False,
        llm_daily_call_cap=0,
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(reject)) as client:
        generator = LLMClient(settings, client=client)
        with pytest.raises(httpx.HTTPStatusError) as raised:
            await generator.generate("Question", [])

    assert raised.value.response.status_code == 503


@pytest.mark.asyncio
async def test_no_key_attempts_provider_then_returns_static_cited_draft() -> None:
    """The no-key path attempts HTTP before substituting a static cited reply."""
    request_headers: list[httpx.Headers] = []

    def respond(request: httpx.Request) -> httpx.Response:
        request_headers.append(request.headers)
        return httpx.Response(401, json={"error": "missing key"})

    async def skip_delay(seconds: float) -> None:
        assert seconds == 5

    settings = Settings(
        llm_provider="ollama",
        llm_base_url="https://provider.example",
        llm_model="test-model",
        llm_api_key=None,
        llm_static_fallback_enabled=True,
        llm_daily_call_cap=0,
    )
    article = KnowledgeArticle(
        slug="export-pdf-report",
        title="Експорт звіту у PDF",
        content="Оберіть формат PDF у меню експорту.",
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        generator = LLMClient(
            settings=settings,
            client=client,
            sleep=skip_delay,
            uniform=lambda minimum, maximum: minimum + (maximum - minimum) * 0,
        )
        draft = await generator.generate(
            "Як експортувати PDF?",
            [article],
        )

    assert request_headers
    assert "authorization" not in request_headers[0]
    assert draft.cited_article_slugs == ["export-pdf-report"]
    assert "Експорт звіту у PDF" in draft.text


@pytest.mark.asyncio
async def test_daily_cap_allows_provider_calls_below_limit(
    postgres_engine: AsyncEngine,
) -> None:
    """A positive daily allowance reserves a PostgreSQL slot before the call."""
    clock = FakeClock()
    async with postgres_engine.begin() as connection:
        await connection.execute(
            delete(LLMDailyCallUsage).where(
                LLMDailyCallUsage.utc_day == date(2026, 1, 1)
            )
        )
    quota = PostgresDailyCallQuota(
        async_sessionmaker(postgres_engine, expire_on_commit=False),
        clock=clock.now,
    )
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"message": {"content": "Provider draft"}})

    settings = Settings(
        llm_base_url="https://provider.example",
        llm_api_key="test-key",
        llm_daily_call_cap=2,
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        draft = await LLMClient(
            settings,
            client=client,
            daily_call_quota=quota,
        ).generate("Question", [])

    assert len(requests) == 1
    assert draft.text == "Provider draft"
    async with AsyncSession(postgres_engine) as session:
        usage = await session.get(LLMDailyCallUsage, date(2026, 1, 1))
    assert usage is not None
    assert usage.call_count == 1


@pytest.mark.asyncio
async def test_call_after_daily_cap_uses_delayed_static_fallback(
    postgres_engine: AsyncEngine,
) -> None:
    """Once full, the daily quota prevents HTTP and returns a delayed citation."""
    clock = FakeClock()
    async with postgres_engine.begin() as connection:
        await connection.execute(
            delete(LLMDailyCallUsage).where(
                LLMDailyCallUsage.utc_day == date(2026, 1, 1)
            )
        )
    quota = PostgresDailyCallQuota(
        async_sessionmaker(postgres_engine, expire_on_commit=False),
        clock=clock.now,
    )
    requests: list[httpx.Request] = []
    delays: list[float] = []
    limiter = RecordingLimiter()

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"message": {"content": "Provider draft"}})

    async def record_delay(seconds: float) -> None:
        delays.append(seconds)

    settings = Settings(
        llm_base_url="https://provider.example",
        llm_api_key="test-key",
        llm_daily_call_cap=1,
    )
    article = KnowledgeArticle(
        slug="export-pdf-report",
        title="Експорт звіту у PDF",
        content="Оберіть формат PDF.",
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        generator = LLMClient(
            settings,
            client=client,
            daily_call_quota=quota,
            request_limiter=limiter,
            uniform=lambda minimum, maximum: minimum,
            sleep=record_delay,
        )
        await generator.generate("First question", [article])
        draft = await generator.generate("Second question", [article])

    assert len(requests) == 1
    assert limiter.calls == 1
    assert delays == [5.0]
    assert draft.cited_article_slugs == ["export-pdf-report"]
    assert "Експорт звіту у PDF" in draft.text


@pytest.mark.asyncio
async def test_daily_cap_resets_at_next_utc_day(
    postgres_engine: AsyncEngine,
) -> None:
    """A new UTC date starts a new allowance without deleting prior usage."""
    clock = FakeClock()
    async with postgres_engine.begin() as connection:
        await connection.execute(
            delete(LLMDailyCallUsage).where(
                LLMDailyCallUsage.utc_day.in_([date(2026, 1, 1), date(2026, 1, 2)])
            )
        )
    quota = PostgresDailyCallQuota(
        async_sessionmaker(postgres_engine, expire_on_commit=False),
        clock=clock.now,
    )
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"message": {"content": "Provider draft"}})

    settings = Settings(
        llm_base_url="https://provider.example",
        llm_api_key="test-key",
        llm_daily_call_cap=1,
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        generator = LLMClient(
            settings,
            client=client,
            daily_call_quota=quota,
        )
        await generator.generate("First day", [])
        clock.current += timedelta(days=1)
        await generator.generate("Second day", [])

    assert len(requests) == 2
    async with AsyncSession(postgres_engine) as session:
        first_day = await session.get(LLMDailyCallUsage, date(2026, 1, 1))
        second_day = await session.get(LLMDailyCallUsage, date(2026, 1, 2))
    assert first_day is not None and first_day.call_count == 1
    assert second_day is not None and second_day.call_count == 1


@pytest.mark.asyncio
async def test_concurrent_daily_quota_reservations_never_exceed_cap(
    postgres_engine: AsyncEngine,
) -> None:
    """Concurrent workers reserve no more than the remaining daily slots."""
    day = date(2026, 1, 5)
    async with postgres_engine.begin() as connection:
        await connection.execute(
            delete(LLMDailyCallUsage).where(LLMDailyCallUsage.utc_day == day)
        )
    quota = PostgresDailyCallQuota(
        async_sessionmaker(postgres_engine, expire_on_commit=False),
        clock=lambda: datetime(2026, 1, 5, 12, tzinfo=UTC),
    )

    reservations = await asyncio.gather(*(quota.try_acquire(7) for _ in range(40)))

    assert sum(reservations) == 7
    async with AsyncSession(postgres_engine) as session:
        usage = await session.get(LLMDailyCallUsage, day)
    assert usage is not None
    assert usage.call_count == 7


@pytest.mark.asyncio
async def test_postgres_token_bucket_caps_requests_with_fake_clock(
    postgres_engine: AsyncEngine,
) -> None:
    """The persisted bucket grants at most one token per second at 60 RPM."""
    clock = FakeClock()
    async with AsyncSession(postgres_engine) as session:
        await session.execute(
            update(LLMRateLimitState)
            .where(LLMRateLimitState.id == 1)
            .values(tokens=1, updated_at=clock.now())
        )
        await session.commit()

    limiter = PostgresTokenBucket(
        session_factory=async_sessionmaker(
            postgres_engine,
            expire_on_commit=False,
        ),
        clock=clock.now,
        sleep=clock.sleep,
    )
    for _ in range(60):
        await limiter.acquire()

    assert clock.now() - datetime(2026, 1, 1, tzinfo=UTC) >= timedelta(seconds=59)
    assert clock.now() - datetime(2026, 1, 1, tzinfo=UTC) < timedelta(seconds=60)
    async with AsyncSession(postgres_engine) as session:
        state = await session.scalar(
            select(LLMRateLimitState).where(LLMRateLimitState.id == 1)
        )
    assert state is not None


@pytest.mark.asyncio
async def test_token_bucket_does_not_regress_clock_while_waiting_for_row_lock(
    postgres_engine: AsyncEngine,
) -> None:
    """Sample time under lock to prevent stale contenders moving time backward."""
    clock = FakeClock()
    sleep_started = asyncio.Event()
    release_sleep = asyncio.Event()

    def sample_time() -> datetime:
        return clock.now()

    async def hold_for_inspection(seconds: float) -> None:
        assert seconds == 1
        sleep_started.set()
        await release_sleep.wait()
        clock.current += timedelta(seconds=seconds)

    async with AsyncSession(postgres_engine) as session:
        await session.execute(
            update(LLMRateLimitState)
            .where(LLMRateLimitState.id == 1)
            .values(tokens=0, updated_at=clock.now())
        )
        await session.commit()

    limiter = PostgresTokenBucket(
        session_factory=async_sessionmaker(
            postgres_engine,
            expire_on_commit=False,
        ),
        clock=sample_time,
        sleep=hold_for_inspection,
    )
    async with AsyncSession(postgres_engine) as lock_session:
        async with lock_session.begin():
            await lock_session.execute(
                select(LLMRateLimitState)
                .where(LLMRateLimitState.id == 1)
                .with_for_update()
            )
            acquire_task = asyncio.create_task(limiter.acquire())
            await asyncio.sleep(0.05)
            clock.current += timedelta(seconds=5)
            await lock_session.execute(
                update(LLMRateLimitState)
                .where(LLMRateLimitState.id == 1)
                .values(tokens=0, updated_at=clock.now())
            )

    try:
        await sleep_started.wait()
        async with AsyncSession(postgres_engine) as session:
            state = await session.scalar(
                select(LLMRateLimitState).where(LLMRateLimitState.id == 1)
            )
        assert state is not None
        assert state.updated_at == clock.now()
    finally:
        release_sleep.set()
        await acquire_task
