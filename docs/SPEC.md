# Service Specification

## Goal and scope

Build a small, durable helpdesk-ticket workflow that generates an LLM-assisted reply draft with knowledge-base references, requires operator approval, and then delivers the approved reply through a helpdesk mock. The service must accept webhook retries and concurrent duplicates without losing work. This document records the current implementation contract and its operational limits. Update it alongside behavior changes so reviewers can compare the design with the code and tests.

## Components and runtime

- **API:** FastAPI handles `POST /webhooks/helpdesk`, `GET /health`, and the server-rendered operator pages and actions.
- **Worker:** A separate container running an async loop from the same codebase in Docker Compose. For a small hosted setup, `RUN_EMBEDDED_WORKER=true` runs the same worker loop in the API process. The default is off. Embedded hosting shares API/worker resources and restart fate; PostgreSQL still persists all work and retry state, and the rate-limit bucket remains shared across processes.
- **PostgreSQL 16:** Primary store for tickets, webhook event IDs, knowledge-base articles, drafts, queue scheduling, and leases.
- **Helpdesk mock:** A separate container accepts simulated webhook deliveries and reply submissions. It implements idempotency-key handling and configurable faults so delivery retries can be tested.

Run the local components with Docker Compose. Configure secrets and service settings through environment variables; commit `.env.example`, never secret values. Hosted deployments bind the API to the platform-provided `PORT`, run migrations before serving, and use managed PostgreSQL rather than local disk. The current Railway preparation is in `DEPLOY.md`; it has not been deployed.

## Webhook acceptance and idempotency

`POST /webhooks/helpdesk` validates the request body, performs a bounded DNS safety check for its callback URL, then persists the event and its initial `new` ticket state in one database transaction. It performs no LLM work or outbound HTTP request. Return `200` after the transaction commits, comfortably within the helpdesk's two-second deadline. A duplicate `event_id`, including a concurrent duplicate, is a successful no-op and also receives `200`.

Enforce a database unique constraint on `event_id`; use `INSERT ... ON CONFLICT DO NOTHING` rather than a read-before-insert check. If persistence fails, return an error rather than acknowledge unpersisted work, allowing the helpdesk to retry. The database is the durable source of truth; an accepted event must not exist only in process memory.

The first persisted payload wins: if a producer reuses an `event_id` with different ticket data, the later request still receives `200` but does not replace the original row. Producers must use a distinct event ID for each distinct event.

Resolve every non-mock callback hostname and reject the URL if any IPv4/IPv6 result is non-public, including loopback, private, link-local, and cloud-metadata ranges. Recheck before sending and disable redirects. The only private-network exception is the exact Compose mock hostname configured by `HELPDESK_MOCK_HOST` (default `helpdesk-mock`). The reserved `helpdesk.example.com` task sample may be accepted at ingestion if unresolvable, but delivery always rejects an unresolved address. DNS resolution and socket connection remain separate operations, so DNS rebinding between the check and connection is a documented residual risk.

The operator pages and actions optionally use HTTP Basic Auth when both `OPERATOR_USERNAME` and `OPERATOR_PASSWORD` are configured. Local Compose leaves these unset; any public deployment must set both and use HTTPS. Credentials are compared with a constant-time comparison and are never logged.

## Ticket lifecycle

The normal path is:

```text
new -> generating -> draft_ready -> approved -> sending -> sent
                                     \-> rejected
                 processing/delivery exhaustion -> failed
```

- `new`: durably accepted and ready for generation.
- `generating`: leased to a worker while knowledge retrieval and draft generation run.
- `draft_ready`: draft and its knowledge references are persisted for review.
- `approved`: operator approved the current draft; eligible for delivery.
- `rejected`: operator rejected the draft; terminal for this event.
- `sending`: a worker has atomically claimed the approved reply and is delivering it.
- `sent`: helpdesk confirmed successful delivery.
- `failed`: a non-retryable error or exhausted retry policy needs operator attention.

Transient failures remain retryable and are rescheduled; do not mark a ticket failed until its retry policy is exhausted or the error is permanent. State-changing operations use conditional database updates so stale workers and concurrent operator actions cannot overwrite newer states.

## PostgreSQL work queue and leases

Use the ticket rows as the queue; do not add Redis or RabbitMQ. Workers claim due rows in a short transaction with a query following this shape:

```sql
UPDATE tickets
SET status = 'generating',
    locked_until = :lease_deadline
WHERE id IN (
    SELECT id
    FROM tickets
    WHERE next_attempt_at <= now()
      AND (
          (status = 'new' AND (locked_until IS NULL OR locked_until < now()))
          OR (status = 'generating' AND locked_until < now())
      )
    ORDER BY created_at
    FOR UPDATE SKIP LOCKED
    LIMIT :batch_size
)
RETURNING *;
```

For delivery, claim due `approved` rows in a short transaction, change them to `sending`, and commit before the HTTP call. Keep `delivery_attempts` separate from draft-generation `attempts`. An expired `sending` lease returns the ticket to `approved`; a later claim reuses its stable idempotency key and receives a new lease. Final state writes must match the status, delivery attempt, and lease deadline captured by the claim so a stale response cannot overwrite a newer owner. Persist retry scheduling (`next_attempt_at`) and attempt counts in PostgreSQL so restart does not lose queued or delayed work. Clear or expire a lease when scheduling a retry. Keep external calls outside the row-claim transaction.

## Knowledge base and draft generation

Seed PostgreSQL with eight short Ukrainian-language articles for a fictional product, **Зоря**. Cover account setup, password recovery, report export to PDF, data import, notifications, billing, team access, and troubleshooting. Store stable article IDs, titles, Ukrainian content, and a searchable representation.

Search entirely in PostgreSQL: combine full-text search using the `simple` text-search configuration with `pg_trgm` similarity, rank candidates, and provide the top three relevant articles to the LLM. Expand a small documented set of English support keywords into Ukrainian search terms because ticket messages may be English while the seeded articles are Ukrainian; unsupported vocabulary is not translated. Preserve article IDs and titles in the generated draft so the operator can review and follow the cited sources. If no useful articles match, the prompt must instruct the model not to invent product policies or unsupported facts.

The worker calls the configured provider through `httpx`, with a service-wide cap of 60 requests per minute and a concurrency cap. Default to Ollama's native `POST /api/chat` endpoint at `https://ollama.com` using model `gemma4:31b`, with the Bearer API key in `LLM_API_KEY`; `LLM_PROVIDER`, `LLM_BASE_URL`, and `LLM_MODEL` are environment settings. The chat request disables streaming, and the assistant content is read from `message.content`. Implement the request-rate cap as a one-token bucket that refills at one token per second, with state persisted in PostgreSQL; this enforces a strict rolling rate and remains global across worker restarts. Default to 30 concurrent tasks: at the 30-second upper latency bound this can sustain one request per second, while the shared token bucket remains the authoritative provider limit. Run one worker instance for the initial deployment; the persisted bucket also prevents independent worker replicas from multiplying the limit if the deployment later scales. Apply exponential backoff with jitter to retryable provider failures, and persist retry timing and attempts.

Without an API key, still issue the configured real HTTP request to the provider, discard its response, and use the static fallback reply after a simulated 5–30 second delay. Feed that reply through the same persistence and moderation flow as a real model response. An unauthenticated request to a provider may be rejected; this mode provides the required fallback behavior, not anonymous successful inference. Never log credentials or include them in committed configuration.

## Operator moderation

Render the operator interface with server-side Jinja2 templates. Show tickets with their customer message, draft, cited knowledge articles, status, and available actions. Approve and reject through atomic conditional updates such as `UPDATE ... WHERE status = 'draft_ready'`; only the request that changes the row succeeds. A stale or repeated action must not re-approve, reject, or send a ticket in another state.

## Reply delivery and guarantees

The worker atomically claims an `approved` ticket by changing it to `sending` before making the outbound request. Send the persisted approved draft to `reply_url` as JSON containing exactly the ticket ID and message, and include an `Idempotency-Key` stable for that ticket (for example, `reply:<ticket_id>`). Apply a finite timeout. Retry timeouts and 5xx responses with exponential backoff and jitter, reusing the same key; do not automatically retry permanent 4xx responses. Treat `201 Created` as success and then atomically record `sent`.

Atomic local claims prevent concurrent workers from intentionally sending the same ticket at once. They cannot by themselves provide exactly-once delivery across PostgreSQL and an independent HTTP service: the process can lose the response or crash after the helpdesk accepts a reply but before `sent` is committed. In that case, retrying is safe only if the receiver durably deduplicates the stable `Idempotency-Key` and returns the original result. The mock records a reply before its configurable post-processing hang or 5xx fault and deduplicates concurrent and repeated keys in process memory; restarting the mock clears this test ledger, so a production receiver must persist its keys. `MOCK_FAIL_BEFORE_RECORD` provides a bounded sequence of 5xx responses for a repeatable retry demonstration. Duplicate-webhook mode exercises event idempotency. Without receiver-side idempotency, the strongest honest guarantee is at-least-once attempts with a residual duplicate-delivery risk; exactly-once external side effects cannot be guaranteed by a local transaction.

## Health and operational expectations

`GET /health` returns HTTP `200` with `OK` as required. Keep health handling independent of the worker's slow LLM and outbound requests. Persist queue state and retry metadata so all accepted tickets can be recovered after process restarts. The expected peak is 500 tickets per 10 minutes; the webhook should only do validation and a small database transaction, while worker concurrency and rate limiting absorb processing delays.

## Out of scope

This assignment does not define multi-tenant isolation or horizontal worker scaling. Optional Basic Auth is provided for operator routes, but deployments should use HTTPS and stronger identity controls for production. If scaling beyond one worker instance, ensure all replicas use the persisted, transactionally coordinated token bucket and validate the resulting database contention before increasing replicas.
