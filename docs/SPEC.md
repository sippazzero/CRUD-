# Service Specification

## Goal and scope

Build a small, durable helpdesk-ticket workflow that generates an LLM-assisted reply draft with knowledge-base references, requires operator approval, and then delivers the approved reply through a helpdesk mock. The service must accept webhook retries and concurrent duplicates without losing work. This document describes the intended design; it does not imply that application code has been implemented.

## Components and runtime

- **API:** FastAPI handles `POST /webhooks/helpdesk`, `GET /health`, and the server-rendered operator pages and actions.
- **Worker:** A separate container running an async loop from the same codebase. Run one worker instance initially; the token-bucket state persisted in PostgreSQL enforces the LLM limit across the service and worker restarts. PostgreSQL also persists all work and retry state.
- **PostgreSQL 16:** Primary store for tickets, webhook event IDs, knowledge-base articles, drafts, queue scheduling, and leases.
- **Helpdesk mock:** A separate container accepts simulated webhook deliveries and reply submissions. It should implement idempotency-key handling so delivery retries can be tested.

Run all components with Docker Compose. Configure secrets and service settings through environment variables; commit `.env.example`, never secret values.

## Webhook acceptance and idempotency

`POST /webhooks/helpdesk` validates the request body, then persists the event and its initial `new` ticket state in one database transaction. It performs no LLM work and no outbound HTTP call. Return `200` after the transaction commits, comfortably within the helpdesk's two-second deadline. A duplicate `event_id`, including a concurrent duplicate, is a successful no-op and also receives `200`.

Enforce a database unique constraint on `event_id`; use `INSERT ... ON CONFLICT DO NOTHING` rather than a read-before-insert check. If persistence fails, return an error rather than acknowledge unpersisted work, allowing the helpdesk to retry. The database is the durable source of truth; an accepted event must not exist only in process memory.

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

For delivery, use the corresponding eligibility condition for `approved` rows and `sending` rows whose lease has expired. Set a lease deadline when claiming work, and renew it during long operations. If a worker crashes, an expired lease makes the job eligible for another claim. Persist retry scheduling (`next_attempt_at`) and attempt counts in PostgreSQL so restart does not lose queued or delayed work. Clear or expire a lease when scheduling a retry. Keep external calls outside the row-claim transaction.

## Knowledge base and draft generation

Seed PostgreSQL with eight short Ukrainian-language articles for a fictional product, **Зоря**. Cover account setup, password recovery, report export to PDF, data import, notifications, billing, team access, and troubleshooting. Store stable article IDs, titles, Ukrainian content, and a searchable representation.

Search entirely in PostgreSQL: combine full-text search using the `simple` text-search configuration with `pg_trgm` similarity, rank candidates, and provide the top three relevant articles to the LLM. Preserve article IDs and titles in the generated draft so the operator can review and follow the cited sources. If no useful articles match, the prompt must instruct the model not to invent product policies or unsupported facts.

The worker calls the configured provider through `httpx`, with a service-wide cap of 60 requests per minute and a concurrency cap. Implement the request-rate cap as a token bucket whose state is persisted in PostgreSQL, so it remains global across worker restarts. Run one worker instance for the initial deployment; the persisted bucket also prevents independent worker replicas from multiplying the limit if the deployment later scales. Apply exponential backoff with jitter to retryable provider failures, and persist retry timing and attempts.

Without an API key, still issue the configured real HTTP request to the provider, discard its response, and use the static fallback reply after a simulated 5–30 second delay. Feed that reply through the same persistence and moderation flow as a real model response. An unauthenticated request to a provider may be rejected; this mode provides the required fallback behavior, not anonymous successful inference. Never log credentials or include them in committed configuration.

## Operator moderation

Render the operator interface with server-side Jinja2 templates. Show tickets with their customer message, draft, cited knowledge articles, status, and available actions. Approve and reject through atomic conditional updates such as `UPDATE ... WHERE status = 'draft_ready'`; only the request that changes the row succeeds. A stale or repeated action must not re-approve, reject, or send a ticket in another state.

## Reply delivery and guarantees

The worker atomically claims an `approved` ticket by changing it to `sending` before making the outbound request. Send the persisted approved draft to `reply_url` as JSON with the ticket ID and message, and include an `Idempotency-Key` stable for that ticket (for example, `reply:<ticket_id>`). Apply a finite timeout. Retry timeouts and 5xx responses with exponential backoff and jitter, reusing the same key; do not automatically retry permanent 4xx responses. Treat `201 Created` as success and then atomically record `sent`.

Atomic local claims prevent concurrent workers from intentionally sending the same ticket at once. They cannot by themselves provide exactly-once delivery across PostgreSQL and an independent HTTP service: the process can lose the response or crash after the helpdesk accepts a reply but before `sent` is committed. In that case, retrying is safe only if the receiver durably deduplicates the stable `Idempotency-Key` and returns the original result. The mock must implement that contract. Without receiver-side idempotency, the strongest honest guarantee is at-least-once attempts with a residual duplicate-delivery risk; exactly-once external side effects cannot be guaranteed by a local transaction.

## Health and operational expectations

`GET /health` returns HTTP `200` with `OK` as required. Keep health handling independent of the worker's slow LLM and outbound requests. Persist queue state and retry metadata so all accepted tickets can be recovered after process restarts. The expected peak is 500 tickets per 10 minutes; the webhook should only do validation and a small database transaction, while worker concurrency and rate limiting absorb processing delays.

## Out of scope

This assignment does not define production operator authentication, multi-tenant isolation, or horizontal worker scaling. Do not expose the operator interface publicly without an appropriate access-control layer. If scaling beyond one worker instance, ensure all replicas use the persisted, transactionally coordinated token bucket and validate the resulting database contention before increasing replicas.
