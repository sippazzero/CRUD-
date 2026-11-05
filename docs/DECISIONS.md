# Key Decisions

## Use PostgreSQL as the durable queue

**Decision:** Store pending work and retry metadata in PostgreSQL, and claim jobs with row locks and `FOR UPDATE SKIP LOCKED`.

**Why:** PostgreSQL is already required for ticket state and knowledge-base storage. A durable queue in the same database makes webhook acceptance, deduplication, and queue persistence transactional, while leases allow recovery after worker crashes. The stated peak of 500 tickets per 10 minutes does not justify another service.

**Rejected alternative — RabbitMQ (or Redis):** It would add another container, operational configuration, and coordination between broker acknowledgements and ticket state. No stated throughput requirement needs that extra infrastructure.

## Use PostgreSQL full-text search with trigram similarity

**Decision:** Combine PostgreSQL FTS (`simple` configuration) with `pg_trgm` similarity and return the top three articles.

**Why:** Both mechanisms operate alongside the article data without an external search service. FTS finds meaningful token matches, while trigram similarity helps with partial terms, spelling variation, and short Ukrainian queries. Ranking both is a small, adequate approach for the required eight-article corpus.

**Rejected alternative — external search/vector database:** It would add infrastructure and synchronization for a tiny knowledge base. The assignment does not require semantic vector search, and its operational cost is not warranted at this scale.

## Separate the webhook from slow work

**Decision:** Persist and acknowledge a valid webhook before asynchronous generation or reply delivery.

**Why:** LLM requests take 5–30 seconds, while the helpdesk retries after two seconds. A database-backed queue keeps the handler responsive and makes accepted work recoverable.

**Rejected alternative — synchronous generation in the request:** It would exceed the webhook deadline, trigger duplicate deliveries, and make ticket acceptance depend on provider availability.

## Require receiver-side idempotency for safe reply retries

**Decision:** Send a stable per-ticket `Idempotency-Key` on every retry and have the helpdesk mock deduplicate it.

**Why:** A sender cannot distinguish a lost response from a request the remote service never received. Local atomic state changes prevent concurrent duplicate sends, but only receiver-side deduplication closes the crash/timeout gap.

**Rejected alternative — claim exactly-once delivery with local state alone:** A PostgreSQL transaction cannot atomically commit with an unrelated HTTP service. Without receiver support, retrying after an ambiguous timeout retains a duplicate-delivery risk.

## Run a single worker for the initial global LLM limit

**Decision:** Start one worker container and enforce a 60-requests-per-minute token bucket plus a concurrency cap, persisting bucket state in PostgreSQL.

**Why:** Persisted limiter state enforces the service-wide limit across worker restarts, while the initial single-worker deployment keeps operations simple. PostgreSQL row locking can coordinate the bucket if additional workers are introduced, without adding another service. PostgreSQL leases provide durable job recovery.

**Rejected alternative — process-local independent limiters:** Each process would apply its own cap, and a process restart would reset its token balance, potentially exceeding the provider's service-wide limit. Limiter state therefore belongs in PostgreSQL even with one worker instance.
