# Helpdesk AI

Helpdesk AI accepts support-ticket webhooks, retrieves supporting articles, drafts a Ukrainian reply, and waits for an operator to approve it before delivery.

## Architecture

```text
Helpdesk mock ──webhook──> API ──persist──> PostgreSQL <──claim── Worker
      ^                     │                 │                │
      └──── reply API ──────┴─────────────────┴── operator UI ─┘
```

The API handles webhook ingestion and serves the server-rendered moderation page. The separate worker retrieves knowledge, calls the configured LLM provider, and delivers approved replies. PostgreSQL stores tickets, articles, retry schedules, worker leases, and the shared rate-limit bucket; the helpdesk mock is a separate Compose service.

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

Compose starts PostgreSQL, runs Alembic migrations before serving the API, and starts the API, worker, and mock. The API health check is at <http://localhost:8000/health>, the mock is at <http://localhost:8001/health>, and the operator review page is at <http://localhost:8000/operator/moderation>. Do not expose the operator page publicly without authentication.

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

Run the measured webhook load test from the running API container:

```sh
docker compose exec api python scripts/load_test.py --duration-seconds 60
```

The script sends 500 distinct ticket events plus 50 duplicate deliveries over 60 seconds, auto-approves resulting drafts through the operator endpoint, and polls the run's database rows until they reach `sent`, `send_failed`, `failed`, or `rejected`. Use `--duration-seconds 600` for a 10-minute arrival window. The default worker concurrency is intentionally small; for a throughput run, set `WORKER_CONCURRENCY=60` in `.env` before starting Compose. The no-key LLM fallback still attempts the configured provider and waits 5–30 seconds per draft, so processing can continue after the arrival window. The script enforces the two-second webhook budget and reports per-run rows and status counts without deleting existing data.

## Design decisions

**PostgreSQL queue, not Redis or RabbitMQ.** Tickets and retry schedules are already durable in PostgreSQL, so workers claim them with `FOR UPDATE SKIP LOCKED` and short-lived leases. This avoids another broker and supports recovery after a worker crash; the stated volume does not justify additional infrastructure.

**Fast, idempotent webhook.** The handler validates the payload and performs one `INSERT ... ON CONFLICT DO NOTHING` transaction, then returns `200`; it does not call the LLM or another service. The unique event ID makes concurrent retries a successful no-op rather than duplicate work.

**Global 60-request/minute limit.** A one-token-per-second bucket is persisted and transactionally coordinated in PostgreSQL, so it remains shared across concurrent worker tasks and restarts. Worker concurrency is separately configurable to bound simultaneous slow provider calls.

**PostgreSQL article retrieval.** The eight Ukrainian articles use `simple` full-text search combined with `pg_trgm` similarity, ranked to return at most three results. This gives token and typo/partial-word matching without maintaining a separate search service for a small corpus.

**No-key fallback.** With no API key and fallback enabled, the worker still attempts the configured provider request, discards the result, waits a random 5–30 seconds, and substitutes a static cited reply. This preserves the same queue and moderation flow while making local runs possible; the unauthenticated provider request may fail and is not anonymous inference.

## Load-test results

One local Compose run on 2026-11-06 sent 500 unique tickets plus 50 duplicate webhook deliveries over 60 seconds. With `WORKER_CONCURRENCY=60`, all 500 persisted rows reached `sent`; the database timestamps show 8m50.47s from first ticket creation to the last delivery. Webhook latency was 0.013s at p95 and 0.074s maximum, both below the two-second requirement.

This is one local run, not a production capacity guarantee. Worker concurrency was raised from the default of 2 to 60, and the no-key fallback still attempts the configured provider before its 5–30 second delay. The result demonstrates the 500-ticket/10-minute target under that test configuration; default-concurrency and real-provider throughput remain unmeasured.

## Delivery guarantees and limits

The worker commits a `sending` lease before making the HTTP request and sends a stable per-ticket `Idempotency-Key`. Delivery attempts are at-least-once: a timeout can happen after the helpdesk has accepted the reply, so the sender retries with the same key. If the receiver durably honors that key, it returns the original result and does not create a second reply; without receiver-side deduplication, a duplicate remains possible.

If a worker crashes mid-send, the expired lease returns the ticket to `approved` and another attempt uses the same key. The mock records and deduplicates replies in process memory, before configured post-processing failures, but loses its ledger when restarted; a production helpdesk must persist its idempotency records.

## What I would do with more time

- Add operator authentication and authorization before any non-local deployment.
- Repeat the load test with the default worker concurrency and with a configured LLM provider; tune production worker and database limits from representative provider latency and deployment measurements.
- Add operational metrics, structured tracing, and a durable mock ledger for longer-running delivery tests.
- Validate the end-to-end idempotency contract against the real helpdesk API before claiming stronger delivery guarantees.

## How I used AI agents

The implementation was developed with the Copilot SDK in VS Code. Work was split into the project skeleton and migrations, idempotent webhook ingestion, knowledge retrieval and worker/rate limiting, operator moderation, reply delivery, and the helpdesk mock. The agent reviewed the implementation against the written specification and added PostgreSQL-backed tests; fixes included isolating the test database from the running worker and fencing delivery status writes by lease ownership.

Personal authorship and review details are intentionally left for the submitter:

- TODO(Max): Describe which changes you personally reviewed or corrected by hand.
- TODO(Max): Add any additional agents or tools you used outside this repository's recorded work.
