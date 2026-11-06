# Independent acceptance review

## Verdict

**READY WITH KNOWN GAPS.** The application passed the local 500-ticket peak run at the configured default concurrency, webhook latency remained below two seconds, the measured provider-request rate stayed within the limit, and the fresh-clone demo completed. Two bugs found during this review were fixed with regression tests. Follow-up verification also confirmed a real Ollama response, optional operator Basic Auth, and callback SSRF checks. This is not a production-readiness certification: no real helpdesk was available, and the mock ledger is in-memory.

The distinction below is intentional:

- **Run-verified** means a command or live request was executed and the result is recorded here.
- **Code/test-verified** means inspected implementation or automated tests support the behavior, but the exact production scenario was not exercised end to end.

## Baseline and final test run

Run-verified on the original Compose stack and a clean clone on drive D:

| Check | Command | Actual result |
|---|---|---|
| Compose services | `docker compose ps` | `api`, `worker`, `db`, and `helpdesk-mock` were running; PostgreSQL reported healthy. |
| API health | `curl.exe -i http://127.0.0.1:8000/health` | HTTP 200; response body `OK` as plain text. |
| Mock health | `curl.exe -i http://127.0.0.1:8001/health` | HTTP 200. |
| Full tests | `D:\CRUD\.venv\Scripts\python.exe -m pytest` | `32 passed in 6.57s` |
| Ruff | `ruff check .` | `All checks passed!` |
| Formatting | `ruff format --check .` | `39 files already formatted` |
| Clean-clone demo | From `D:\crud-acceptance-clean`: `& 'C:\Program Files\Git\bin\bash.exe' scripts/demo.sh` with `COMPOSE_PROJECT_NAME=crud-clean` | Both scenarios completed; exact output summary is included in requirement 14. |

## Phase 2: requirement-by-requirement results

| # | Requirement | Exact command or test used | Actual output/result | Status |
|---:|---|---|---|---|
| 1 | Webhook returns in under two seconds while workers are busy. | `scripts/load_test.py --duration-seconds 60` with the default 30 worker tasks, run against 120 active tickets. | 132 webhook requests; p95 `0.292s`, maximum `0.340s`; all 120 tickets reached `sent`. | PASS |
| 2 | Sequential and 20-way concurrent duplicate event IDs produce one row. | `D:\CRUD\.venv\Scripts\python.exe -m pytest` (`tests/test_ingest.py`) and live duplicate requests. | Sequential repeats returned 200 and retained one row; 20 concurrent requests produced exactly one row. | PASS |
| 3 | Invalid payload returns 422 and persists nothing. | `D:\CRUD\.venv\Scripts\python.exe -m pytest` (`tests/test_ingest.py`); live invalid payload probes. | Empty message, 10,001-character message, and malformed reply URL returned 422 and created no row. | PASS |
| 4 | Run 500 tickets using the Compose default; no loss and no more than 60 LLM calls per minute. | `scripts/load_test.py --duration-seconds 60`; separate 120-ticket run through an HTTP request counter. | 500 unique rows; `sent=500`; 550 webhook requests; p95 `0.013s`, max `0.071s`; 504 generation attempts and 500 delivery attempts; mock had 500 distinct deliveries. At default concurrency 30, the separate counter received 120 requests and measured a maximum of 59 arrivals in any rolling 60-second window. | PASS |
| 5 | Draft cites knowledge articles; no-key mode attempts a real provider request and then returns a static draft after 5–30 seconds. | Full pytest suite (`tests/test_knowledge.py`, `tests/test_llm.py`, `tests/test_worker.py`); direct no-key provider probe. | PDF-export drafts cited `export-pdf-report`. The real provider endpoint returned HTTP 401 without a key; fallback produced a static cited draft after `5.46s` with a five-second test delay. Random-delay bounds and the production provider path were also inspected in code, not sampled across the full interval. | PASS |
| 6 | Operator page lists drafts; approve succeeds, double approve gets 409, reject sends nothing. | Full pytest suite (`tests/test_moderation.py`, `tests/test_sender_moderation.py`); clean-clone demo. | Page and conditional transitions are covered by tests. Demo approval redirected successfully and the ticket reached `sent`; concurrent/double approval permits one success, and rejected tickets are not delivered. | PASS |
| 7 | Exactly one delivery per ticket appears in the mock's `/deliveries`. | Query of the mock ledger after the 500-ticket run; `bash scripts/demo.sh`. | 500 ticket IDs, 500 delivery records, zero missing or duplicate IDs. Each demo scenario printed exactly one record for its ticket. | PASS |
| 8 | High 5xx and timeout/hang recover to `sent` with one record; permanent 4xx becomes `send_failed` without retries. | Demo on the clean clone; full pytest suite (`tests/test_sender_moderation.py`); permanent-404 probe. | High-5xx demo: `Ticket status: sent after 4 attempts`, one ledger record. Timeout-after-record regression test retries with the same key and one record. Permanent 404: `send_failed` after one attempt. | PASS |
| 9 | Killing the worker during `generating` and `sending` recovers all work without duplicate delivery. | `docker compose kill worker` during an observed `sending` request, restart, then inspect ticket and mock ledger; generation-lease tests. | Sending crash recovered: ticket became `sent` after two attempts with one mock record and the same key. Expired-generation lease recovery is covered by PostgreSQL tests. A process kill during an actively observed `generating` LLM call was not separately captured in this run. | PARTIAL |
| 10 | Two worker replicas do not process a ticket twice. | Scaled-worker run with five tickets; PostgreSQL queue-claim tests. | Five tickets each reached `draft_ready` once and cited the expected article; concurrent claim test passes. The exact replica count for that live run was not retained, so the specific two-replica configuration is not certified here. | PARTIAL |
| 11 | `docker compose stop worker` exits cleanly on SIGTERM and leaves no stuck tickets. | `docker compose stop worker`; inspect worker exit code and `generating`/`sending` rows. | Idle worker exited with code 0 and logged `Worker service stopped`; no `generating` or `sending` rows remained. In-flight graceful shutdown was not separately demonstrated. | PARTIAL |
| 12 | No secrets in repository/history; `.env` ignored and `.env.example` contains placeholders. | `git check-ignore .env`; tracked-file and history pattern scan (details in Hygiene). | `.env` is ignored and not tracked; `.env.example` uses placeholders. No high-confidence API-token or private-key pattern was found in the current tree or scanned history. Earlier history includes a generic local-only database password, not a deployed credential; history was not rewritten. | PASS |
| 13 | API runs migrations on startup; restarting it is safe. | Recreate API, request `/health`, and query `alembic_version` in PostgreSQL. | API returned 200 after restart; Alembic remained at `0005_ticket_delivery_attempts`; no duplicate or failed migration. | PASS |
| 14 | Follow README from a fresh clone and identify wrong/missing instructions. | Fresh clone on D:, copy `.env.example` to `.env`, `docker compose -p crud-clean up --build -d`, then `& 'C:\Program Files\Git\bin\bash.exe' scripts/demo.sh`. | Clean-clone build and startup succeeded. Demo output: `Persisted rows for duplicate event: 1`; first ticket `sent` with one delivery; second scenario `sent after 4 attempts` with one delivery; `Demo complete. Operator page: http://127.0.0.1:18080/operator/moderation`. README steps were sufficient. On this Windows host, the `bash` alias resolved to a WSL shim, so the installed Git Bash executable was used; this is a shell selection issue, not a missing project step. | PASS |

### Load measurements

The 500-ticket run used the Compose default `WORKER_CONCURRENCY=30`, not an override:

```text
Unique tickets sent: 500
Webhook requests sent: 550
Rows in DB for this run: 500
Per-status counts: sent=500
Webhook latency p95: 0.013s
Longest webhook latency: 0.071s
```

Database timestamps showed about `8m59s` from first ticket creation to last delivery. The 60 requests/minute cap implies an approximately `8m20s` theoretical minimum for 500 requests. The mock ledger contained one record for each ticket. In the separate counter run, default concurrency 30 processed 120 unique tickets and 12 duplicate deliveries:

```text
Unique tickets sent: 120
Webhook requests sent: 132
Rows in DB for this run: 120
Per-status counts: sent=120
Webhook latency p95: 0.292s
Longest webhook latency: 0.340s
counter_requests=120 max_in_any_60s_window=59
```

The counter run observed actual HTTP arrivals at a local provider-compatible endpoint; the 500-ticket run additionally exercised the configured external provider endpoint without credentials (401 responses followed by the required fallback). These results are local measurements, not a production capacity guarantee.

## Phase 3: adversarial checks

### Run-verified inputs and failure behavior

- **Large and empty messages:** a 10,001-character message and an empty message each returned 422 and created no ticket.
- **Unicode and emoji:** accepted and persisted without alteration.
- **Malformed `reply_url`:** returned 422 and created no ticket.
- **Reused event ID with changed payload:** returned 200, preserved the first persisted payload, and did not update or create a row. This first-write-wins behavior is documented in [SPEC.md](SPEC.md).
- **Database outage:** while PostgreSQL was stopped, a webhook returned 500 rather than acknowledging unpersisted work. After PostgreSQL restarted, retry returned 200 and exactly one row was stored.
- **Worker crash during sending:** the mock recorded the request before the worker was killed. Lease recovery retried with the same idempotency key; the ticket became `sent` with two attempts and one delivery record.
- **Migration restart:** the API restarted successfully and retained the current Alembic revision.

### Code/test-verified behavior

- The webhook route validates and calls the persistence service only; inspection of [app/main.py](../app/main.py) and [app/services/ingest.py](../app/services/ingest.py) found no LLM, outbound HTTP, or other heavy work in the request handler.
- Ingestion uses one PostgreSQL `INSERT ... ON CONFLICT DO NOTHING`, not a SELECT-then-INSERT sequence.
- Queue claims and recovery use conditional status/lease predicates with `FOR UPDATE SKIP LOCKED`; moderation updates are conditional on `status='draft_ready'`.
- Sender final writes are fenced by the sending status, attempt, and lease ownership. Provider and helpdesk HTTP requests set timeouts. The idempotency key is stable per ticket.
- The mock records a first delivery before configured post-record hangs/failures and deduplicates by idempotency key.
- Static source review found no other unguarded ticket state update or unstable delivery key in the relevant queue, moderation, or sender paths.

The code review does not establish that a real helpdesk durably honors idempotency keys. The mock ledger is process-local and clears when the mock restarts.

## Follow-up verification: provider and security changes

The following checks were run after the original acceptance review. This addendum supersedes earlier statements in this report that operator authentication was absent or that no valid-key inference had been tested.

| Requirement | Evidence | Result |
|---|---|---|
| English ticket retrieves a Ukrainian knowledge article. | `D:\CRUD\.venv\Scripts\python.exe -m pytest -p no:cacheprovider -q tests\test_knowledge.py` | `2 passed in 1.64s`; the English PDF-export query ranks `export-pdf-report` first. |
| Real configured Ollama call creates a cited draft. | Existing ticket `evt_ollama_local_20261106_1409` was reset to `new` in PostgreSQL and processed by the rebuilt Compose worker. | `status=draft_ready`, `attempts=1`, `citations=["export-pdf-report"]`; the draft came from the real model, not fallback. |
| Operator Basic Auth is optional and constant-time checked. | `tests/test_operator_auth.py` and the full test suite. | Missing and incorrect credentials receive 401; correct credentials succeed; `/health` remains open. |
| SSRF protections retain the assignment's sample payload. | Exact sample webhook posted to the local API; regression tests in `tests/test_url_safety.py` and `tests/test_ingest.py`. | Sample returned HTTP 200 and its test row was deleted. Private/loopback destinations are blocked, and the configured mock host is explicitly allowed. |
| Full tests and lint after the follow-up code changes. | `D:\CRUD\.venv\Scripts\ruff.exe check .`; `D:\CRUD\.venv\Scripts\ruff.exe format --check .`; `D:\CRUD\.venv\Scripts\python.exe -m pytest -q` | Ruff: `All checks passed!`; formatting: `43 files already formatted`; pytest: `52 passed in 9.26s` with no warnings. |

The captured real-model response began: “Вітаємо! Щоб експортувати звіт у PDF, відкрийте потрібний звіт у Зорі, натисніть «Експорт» і виберіть формат PDF.” The run used the configured cloud endpoint/model; no credential value is recorded here.

## Bugs found and fixes

1. **Untrusted reply URL could reach internal hosts (SSRF).** A webhook-controlled callback URL was accepted and the sender could reach an internal endpoint in a local probe. Added a hostname allowlist at ingestion and a second check immediately before sending, with regression tests and configuration/documentation. Fixed in commit `e7c06ad` (`fix: restrict helpdesk reply destinations`).
2. **Rate-limit timestamp race.** A caller sampled time before waiting for the PostgreSQL bucket lock, so a delayed caller could move the bucket's persisted timestamp backwards. The pre-fix counter observed 61 arrivals in 59.766 seconds, and a regression test reproduced the timestamp regression. Clock sampling now happens under the row lock; the post-fix counter observed at most 59 arrivals in any 60-second window. Fixed in commit `dd24563` (`fix: serialize limiter time sampling`).

## Hygiene and environment

- `.env` is ignored and untracked; `.env.example` contains placeholders only.
- Current files and Git history were scanned for high-confidence API-key, bearer-token, and private-key patterns; none were found. Older commits retain a generic local-development database password. It was not a deployed credential, and commit history was intentionally not rewritten.
- The clean-clone acceptance stack and temporary counter container are test-only and are removed after the report is committed. The original `crud` stack is not part of cleanup.
- Deployment is prepared in [DEPLOY.md](../DEPLOY.md), but **not deployed**. Optional Basic Auth is now available; public deployments must configure both credentials and use HTTPS.

## Remaining gaps

- The clean-clone demonstration used the no-key fallback; the real-model check covered one ticket only, so provider latency and deployed capacity remain unmeasured.
- No integration with a real helpdesk was tested.
- The mock's delivery ledger is in memory; production receiver-side idempotency must be durable.
- A worker kill during active draft generation, a live run with exactly two worker replicas, and SIGTERM during active work were not separately demonstrated in this acceptance session.
- The README contains one `TODO(Max)` asking the author to add any agents or tools used outside the repository's recorded work.

## Original assignment re-verification

This section records the checks performed for the follow-up request. "Earlier recorded run" means the command and output were captured in the acceptance evidence above, but were not repeated during this re-verification. No public deployment exists, so all HTTP results below are local unless stated otherwise.

| Requirement from `docs/TASK.md` | Status | Command/evidence | Actual result |
|---|---|---|---|
| Accept the specified helpdesk event at `POST /webhooks/helpdesk`. | PASS | Live local request to `http://api:8000/webhooks/helpdesk` using `event_id`, `ticket_id`, `customer_email`, Ukrainian `message`, and mock `reply_url`; temporary test row removed afterward. | `HTTP 200, time_total=0.064s, body={"status":"accepted"}`. |
| Acknowledge within two seconds. | PASS | Same live request, with a two-second client timeout; `tests/test_ingest.py::test_webhook_response_is_under_two_seconds`. | `time_total=0.064s`; focused test passed. |
| Handle repeated and simultaneous duplicate events idempotently. | PASS | Focused Docker test command below. | The sequential-duplicate and concurrent-duplicate tests passed; the earlier recorded run reports 20 concurrent requests persisted one row. The focused run completed `14 passed in 2.87s`. |
| Do not lose tickets at the peak of 500 in 10 minutes. | PASS (earlier recorded run) | Earlier `scripts/load_test.py --duration-seconds 60` run and persisted PostgreSQL/mock-ledger checks; see "Load measurements" above. | `Unique tickets sent: 500`; `Rows in DB for this run: 500`; `Per-status counts: sent=500`; 550 webhook requests; webhook p95 `0.013s`, max `0.071s`; 500 distinct mock deliveries. This was not rerun during the current re-verification and is not a production capacity guarantee. |
| Use 5–10 AI-written knowledge-base articles, retrieve relevant material, and cite it in drafts. | PARTIAL | Earlier live PostgreSQL query and `tests/test_knowledge.py`; current full suite: `docker compose run --rm api python -m pytest -q -o cache_dir=/tmp/pytest-cache`. | The local database has eight seeded article slugs: `account-setup`, `billing`, `export-pdf-report`, `import-data`, `notifications`, `password-recovery`, `team-access`, `troubleshooting`. Tests cover relevant retrieval/citations. The tracked repository does not establish that article authorship was AI-generated; citation coverage is not universal (the earlier demo recorded one draft with no cited article). |
| Provide a basic operator moderation interface to approve or reject drafts. | PASS | Focused `tests/test_moderation.py` and `tests/test_sender_moderation.py`; current five-ticket local demo remains unapproved. | The operator-page rendering and state-transition tests passed; no ticket was approved as part of this verification. |
| Send approved drafts to each event's `reply_url`; handle helpdesk `201 Created`, transient failures, and prevent duplicate replies. | PASS (mock; real helpdesk unavailable) | Focused `tests/test_sender_moderation.py` and earlier 500-ticket Compose run, which inspected the mock `/deliveries` ledger. | Focused retry/idempotency tests passed. Earlier run recorded 500 mock deliveries for 500 tickets, zero missing/duplicate ticket IDs; timeout-after-record retries reuse an idempotency key. A real helpdesk's interpretation and durability of that key cannot be verified here. |
| Expose `/health` returning HTTP 200 and `OK`. | PASS | `curl.exe -sS -w "\nHTTP %{http_code} time_total=%{time_total}s\n" http://127.0.0.1:8000/health` | `OK`; `HTTP 200 time_total=0.004194s`. |
| Without an API key, attempt a real provider request, then return a static answer after 5–30 seconds. | PASS (earlier recorded provider probe) | Earlier direct no-key provider probe and `tests/test_llm.py`; the focused provider/fallback test passed in the current `14 passed` run. | The earlier real provider endpoint returned HTTP 401 without a key; fallback produced a cited static draft after `5.46s` with a five-second test delay. The full random-delay range was verified from implementation, not sampled end to end. |
| Use PostgreSQL. | PASS | `docker compose ps` and Docker-backed full pytest run. | `db` is running and healthy; integration tests use the Compose PostgreSQL service. |
| Use Python with FastAPI or Django. | PASS | Repository files and running Compose API service; full pytest run. | The application is Python/FastAPI. |
| Start the stack using Docker Compose. | PASS | `docker compose ps` after the Compose stack was running; API and mock health requests above. | `api`, `worker`, `db`, and `helpdesk-mock` are running; `db` is healthy. |
| Include detailed README startup instructions. | PASS | Earlier fresh-clone procedure: `docker compose -p crud-clean up --build -d`, followed by `scripts/demo.sh`; see requirement 14 above. | Fresh-clone build/start and demo succeeded; the README was sufficient for that run. |
| Commit agent configuration/specification files used for the work. | PASS | `git ls-files .github/copilot-instructions.md docs/TASK.md docs/SPEC.md README.md DEPLOY.md railway.json` | Tracked output includes `.github/copilot-instructions.md`, `docs/TASK.md`, `docs/SPEC.md`, `README.md`, `DEPLOY.md`, and `railway.json`. |
| Preserve commit history without squash. | PASS (visible repository history) | `git --no-pager log --oneline -n 10` | The repository contains multiple separate conventional commits; the latest visible commits are `7a67ff4` and `e56c6f4`. This output cannot establish what actions may have occurred before the visible history. |
| Provide public webhook/operator links to a deployed service (strongly preferred by the assignment). | PARTIAL / NOT VERIFIED | `Get-Command railway -ErrorAction SilentlyContinue`; deployment notes in `DEPLOY.md` and `README.md`. | Railway CLI is not installed; no public service URLs or deployment credentials are available in this environment. No public endpoint, public moderation page, or public mock behavior was tested. The assignment describes deployment as very desirable, not mandatory. |
| Full current test suite. | PASS | `docker compose run --rm api python -m pytest -q -o cache_dir=/tmp/pytest-cache` | `57 passed in 6.42s`. |
| Focused assignment behavior tests. | PASS | The focused Docker command shown above, with 14 webhook, queue, LLM, knowledge, moderation, and sender tests. | `14 passed in 2.87s`. |

Exact focused test command:

```powershell
docker compose run --rm api python -m pytest -q -o cache_dir=/tmp/pytest-cache tests/test_ingest.py::test_assignment_sample_callback_is_accepted tests/test_ingest.py::test_webhook_response_is_under_two_seconds tests/test_ingest.py::test_sequential_duplicate_returns_same_response_without_second_row tests/test_ingest.py::test_concurrent_duplicates_persist_exactly_one_ticket tests/test_knowledge.py::test_pdf_export_message_retrieves_seeded_article tests/test_knowledge.py::test_english_pdf_export_message_retrieves_seeded_article tests/test_llm.py::test_no_key_attempts_provider_then_returns_static_cited_draft tests/test_moderation.py::test_moderation_page_renders_pending_message_and_draft tests/test_moderation.py::test_reject_marks_draft_terminal tests/test_sender_moderation.py::test_successful_delivery_marks_sent_once tests/test_sender_moderation.py::test_two_transient_failures_retry_then_send_once tests/test_sender_moderation.py::test_mock_replay_returns_created_without_duplicate_record tests/test_sender_moderation.py::test_lost_response_retries_with_same_key_and_one_delivery tests/test_queue.py::test_parallel_claimers_claim_each_ticket_once
```

### Current re-verification verdict

**READY WITH KNOWN GAPS.** Local behavior and tests support the core assignment, including the live sub-two-second webhook, PostgreSQL persistence, moderation, and idempotent delivery through the mock. The public deployment was not created or checked; article AI provenance is not evidenced in the repository; and the earlier-recorded peak-load and real no-key provider probes were not repeated in this pass. Do not treat this verdict as evidence of public deployment or production readiness.
