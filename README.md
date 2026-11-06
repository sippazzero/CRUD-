# Helpdesk AI

Helpdesk AI accepts support-ticket webhooks, retrieves supporting articles, drafts a Ukrainian reply, and waits for an operator to approve it before delivery.

## Live demo

The service is deployed on Railway (API with embedded worker, plus PostgreSQL). Deployment notes are in [DEPLOY.md](DEPLOY.md).

- Health: https://crud-production-a698.up.railway.app/health
- Operator page: https://crud-production-a698.up.railway.app/operator/moderation
- Webhook: `POST https://crud-production-a698.up.railway.app/webhooks/helpdesk`
- API docs: https://crud-production-a698.up.railway.app/docs

The hosted demo is intentionally open: there is no operator authentication, so do not use real customer data. It runs without `LLM_API_KEY`, as the assignment allows: the worker still makes a real provider request, then uses a static cited draft after a 5–30 s delay. Real-model generation was checked only locally (see "Local real-model check" below).

Railway's free credit is limited, so the demo may be interrupted if the credit runs out. Limits and pricing change over time; see [DEPLOY.md](DEPLOY.md).

## Architecture

```text
Helpdesk mock ──webhook──> API ──persist──> PostgreSQL <──claim── Worker
      ^                     │                 │                │
      └──── reply API ──────┴─────────────────┴── operator UI ─┘
```

In Docker Compose, the API handles webhook ingestion and moderation while a separate worker retrieves knowledge, calls the configured LLM provider, and delivers approved replies. PostgreSQL stores tickets, articles, retry schedules, worker leases, and the shared rate-limit bucket. For a small hosted deployment, `RUN_EMBEDDED_WORKER=true` runs the worker in the API process; PostgreSQL remains the durable queue, and the helpdesk mock is a separate service. This saves one app service but shares CPU/memory and restart fate between API and worker, so it is a small-demo trade-off rather than independent scaling or fault isolation.

```text
new -> generating -> draft_ready -> approved -> sending -> sent
                         |                         |
                         +-> rejected              +-> approved (retry)
generating retry -> new; exhausted generation -> failed
exhausted or permanent delivery failure -> send_failed
```

## Run locally

Prerequisites: Docker Engine or Docker Desktop with Compose v2, `curl`, and Bash for the demo. Copy `.env.example` to `.env` if you want to customize local ports or settings. Its database password is a local-only placeholder; keep real credentials in an untracked `.env`.

```sh
cp .env.example .env
docker compose up --build
```

Compose starts PostgreSQL, runs Alembic migrations before serving the API, and starts the API, worker, and mock. The API health check is at <http://localhost:8000/health>, the mock is at <http://localhost:8001/health>, and the operator review page is at <http://localhost:8000/operator/moderation>. Basic Auth is disabled by default for local use. For the open reviewer demo on Railway, `OPERATOR_USERNAME` and `OPERATOR_PASSWORD` are unset, so the reviewer can access moderation. Set both variables to enable the optional Basic Auth on `/operator/*`; use HTTPS for public traffic. An open operator page can approve replies, so do not use it with real customer data.

Callback validation resolves every non-mock hostname and rejects a URL if any IPv4/IPv6 result is non-public (including loopback, private, link-local, and metadata ranges). The worker repeats this check immediately before sending, and HTTP redirects are disabled. The Compose callback hostname is `HELPDESK_MOCK_HOST=helpdesk-mock`; change that environment variable only when the helpdesk mock's service hostname differs. DNS is checked again on delivery, but resolution and connection are separate operations, so DNS rebinding between them remains a limitation. The assignment's reserved `helpdesk.example.com` sample is accepted at ingestion when it does not resolve; delivery still rejects an unresolvable target.

Webhook IDs are idempotent: the first payload persisted for an `event_id` wins. A later request reusing that ID, even with a different payload, returns the same `200` acknowledgement and does not replace the first event.

Run the full integration suite against the Compose PostgreSQL service:

```sh
docker compose run --rm api python -m pytest
```

The test fixture creates and migrates an isolated `helpdesk_test` database on the same PostgreSQL server. The published database port is configurable with `POSTGRES_PORT`; when running tests directly on the host, set `TEST_DATABASE_URL` to that port and the matching local credentials.

Run both end-to-end demo scenarios with Bash and `curl`:

```sh
bash scripts/demo.sh
```

The demo leaves the Compose stack running so the reviewer can inspect the operator page and mock delivery log.

The independent runtime and code-review results, including known gaps, are in [docs/ACCEPTANCE.md](docs/ACCEPTANCE.md).

Run the measured webhook load test from the running API container:

```sh
docker compose exec api python scripts/load_test.py --duration-seconds 60
```

The script sends 500 distinct ticket events plus 50 duplicate deliveries over 60 seconds, auto-approves resulting drafts through the operator endpoint, and polls the run's database rows until they reach `sent`, `send_failed`, `failed`, or `rejected`. Use `--duration-seconds 600` for a 10-minute arrival window. The default worker concurrency is 30, and the measured 500-ticket run used that default. The no-key LLM fallback still attempts the configured provider and waits 5–30 seconds per draft, so processing can continue after the arrival window. The script enforces the two-second webhook budget and reports per-run rows and status counts without deleting existing data.

## Design decisions

**PostgreSQL queue, not Redis or RabbitMQ.** Tickets and retry schedules are already durable in PostgreSQL, so workers claim them with `FOR UPDATE SKIP LOCKED` and short-lived leases. This avoids another broker and supports recovery after a worker crash; the stated volume does not justify additional infrastructure.

**Fast, idempotent webhook.** The handler validates the payload and performs one `INSERT ... ON CONFLICT DO NOTHING` transaction, then returns `200`; it does not call the LLM or another service. The unique event ID makes concurrent retries a successful no-op rather than duplicate work.

**SSRF-resistant reply destinations.** The API and sender resolve callback hostnames and reject any non-public address, while allowing only the explicitly configured Compose mock hostname as a private-network exception. Redirects are disabled. Rechecking before sending reduces risk but does not pin the DNS answer through connection establishment.

**Global 60-request/minute limit.** A one-token-per-second bucket is persisted and transactionally coordinated in PostgreSQL, so it remains shared across concurrent worker tasks and restarts. Worker concurrency is separately configurable to bound simultaneous slow provider calls.

**PostgreSQL article retrieval.** The eight Ukrainian articles use `simple` full-text search combined with `pg_trgm` similarity, ranked to return at most three results. A small English-to-Ukrainian keyword map helps English-language tickets find the Ukrainian articles; unsupported terms still rely on PostgreSQL's configured search. This avoids maintaining a separate search service for a small corpus.

The fictional-product knowledge-base articles were generated by the AI agent during development.

**Ollama and no-key fallback.** By default, the worker sends Ollama's native chat request to `https://ollama.com/api/chat` using model `gemma4:31b`, a finite timeout, `stream: false`, and a Bearer key from `LLM_API_KEY`. `LLM_PROVIDER`, `LLM_BASE_URL`, and `LLM_MODEL` can be set in `.env`; `openai_compatible` is also supported. With no key and fallback enabled, the worker still attempts the configured provider, then substitutes a static cited reply after a random 5–30 seconds; the unauthenticated request may be rejected and is not anonymous inference.

Ollama request/response format and cloud authentication follow the official [Ollama chat API](https://docs.ollama.com/api/chat), [Ollama Cloud](https://docs.ollama.com/cloud), and [OpenAI compatibility](https://docs.ollama.com/api/openai-compatibility) documentation. The model was selected from Ollama's documented cloud example; it avoids introducing a local model download for the take-home demo.

The shared PostgreSQL daily quota defaults to 300 provider-call attempts per UTC day. Set `LLM_DAILY_CALL_CAP=0` to disable it. An atomic upsert coordinates all workers and replicas; once the cap is reached, the worker skips the provider request and uses the existing static cited reply after a 5–30 second delay. This cap also applies when an API key is configured.

## Load-test results

On 2026-11-06, a local Compose run sent 500 unique tickets plus 50 duplicate webhook deliveries over 60 seconds with the default `WORKER_CONCURRENCY=30`. All 500 persisted rows reached `sent` in about 8m59s; webhook p95 was 0.013s and the longest response was 0.071s. The database recorded 504 generation attempts and 500 delivery attempts, and the mock recorded exactly 500 deliveries with no missing or duplicate ticket IDs. The run took about 8m59s because the 60 requests/minute limit makes about 8m20s the theoretical minimum, so the rate limiter held the limit and no ticket was lost.

A separate 120-ticket run at the same default concurrency sent 132 webhook requests, and all 120 tickets reached `sent`. An HTTP counter observed 120 provider requests, with at most 59 arrivals in any rolling 60-second window; webhook p95 was 0.292s and the longest response was 0.340s. A direct no-key provider probe received HTTP 401, then produced a static cited draft after a test-configured five-second fallback delay.

### Local real-model check

On 2026-11-06, one existing ticket was reprocessed through the configured Ollama Cloud endpoint using `gemma4:31b`. It reached `draft_ready` after one generation attempt, cited `export-pdf-report`, and returned a real-model draft (not the static fallback). The first 200 characters were:

> Вітаємо! Щоб експортувати звіт у PDF, відкрийте потрібний звіт у Зорі, натисніть «Експорт» і виберіть формат PDF. Зверніть увагу, що великі звіти можуть готуватися кілька хвилин, після чого готовий фа

These are local measurements, not a production capacity guarantee. The 500-ticket result covers the assignment's peak volume with the default concurrency, but provider latency, production hardware, and behavior under representative deployment conditions still need measurement.

## Delivery guarantees and limits

The worker commits a `sending` lease before making the HTTP request and sends a stable per-ticket `Idempotency-Key`. The reply body contains exactly `ticket_id` and `message`; the mock returns `201 Created` both for the first delivery and for a replay of the same key. Delivery attempts are at-least-once: a timeout can happen after the helpdesk has accepted the reply, so the sender retries with the same key. If the receiver durably honors that key, it returns the original result and does not create a second reply; without receiver-side deduplication, a duplicate remains possible.

If a worker crashes mid-send, the expired lease returns the ticket to `approved` and another attempt uses the same key. The mock records and deduplicates replies in process memory, before configured post-processing failures, but loses its ledger when restarted; a production helpdesk must persist its idempotency records.

## What I would do with more time

- Repeat the load test over a longer period and tune production worker and database limits from representative provider latency and deployment measurements.
- Replace Basic Auth with the deployment's identity provider and add role-based operator authorization.
- Add operational metrics, structured tracing, and a durable mock ledger for longer-running delivery tests.
- Validate the end-to-end idempotency contract against the real helpdesk API before claiming stronger delivery guarantees.

## How I used AI agents

I used GitHub Copilot in Agent mode inside VS Code to write code. I prepared the written specification and step-by-step prompts in a separate chat with Claude. I split the work into small steps: the skeleton, idempotent webhook, worker with retries, moderation and sender, load test, acceptance test, and hosting preparation. After each step, I reviewed the agent's report, ran the tests, and ran Docker locally. I rejected a report that claimed work without tests being run. I checked concurrency-sensitive code by hand: `INSERT ON CONFLICT`, `SKIP LOCKED` with leases, conditional `UPDATE`, and the stable `Idempotency-Key`. A separate acceptance pass found and fixed an SSRF path and a rate-limiter race. I kept project rules in `.github/copilot-instructions.md`. I used PostgreSQL-backed tests for webhook duplicates, queue claims, and reply delivery. The separate commits show the work in small steps.
