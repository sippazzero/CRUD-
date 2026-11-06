# Railway deployment preparation

The service is deployed and verified on Railway at <https://crud-production-a698.up.railway.app>.

## Architecture and trade-offs

Use one always-on Railway web service for FastAPI and its embedded worker, plus Railway-managed PostgreSQL and a separate mock service. Set `RUN_EMBEDDED_WORKER=true` only on the API service; the default is false and Docker Compose keeps its independent worker container. The API and worker share a process and resource budget, so a process restart interrupts an in-flight call; ticket state and leases remain in PostgreSQL, and an expired lease lets work resume after restart. One web process avoids separate API/worker service overhead but does not provide worker/API fault isolation or independent scaling.

The mock is a separate, optional demo service. Its `/deliveries` ledger is in memory and its endpoints have no authentication; do not treat it as a production helpdesk or expose customer data through it. If a public domain is enabled for acceptance testing, remove the mock service or disable its public domain afterwards. `HELPDESK_MOCK_HOST` must exactly match the hostname used in the webhook `reply_url`; this is the deliberate callback-validation exception for the mock.

PostgreSQL is the only durable queue/state store. No SQLite or local filesystem state is required. Alembic migrations run automatically on every deploy before Uvicorn starts (via `scripts/start.sh`). The script uses `set -e` so Uvicorn and the embedded worker will not start if migrations fail. Uvicorn binds to `0.0.0.0` and Railway's `PORT` (defaulting to 8000). The health check is `/health`.

Railway currently lists a Free plan with $1/month usage credit, a per-service maximum of 0.5 GB RAM, 1 vCPU and one replica. Treat it as a temporary demo tier: exhausting credit may interrupt workloads, and it is not an always-on production guarantee. Railway's persistent services are not documented as sleeping after inbound inactivity; deploys, failures, or credit exhaustion can still cause downtime. The Hobby plan is currently $5/month and includes $5 of resource usage; usage above that is billed. Verify current limits and pricing in Railway before deployment. See [Railway plans](https://docs.railway.com/pricing/plans) and [persistent services](https://docs.railway.com/services).

## Click-by-click Railway setup

1. Sign in at <https://railway.com>, create a project, choose **Deploy from GitHub repo**, authorize Railway's GitHub app if prompted, and select `sippazzero/CRUD-` on `main`.
2. Select the API service. In **Settings → Build**, use the repository `Dockerfile`. The committed [`railway.json`](railway.json) and container `CMD` use [`scripts/start.sh`](scripts/start.sh), which runs `alembic upgrade head` before Uvicorn so database migrations run automatically on every deploy. If configuring the start command manually in Railway's **Settings → Deploy**, set it to `/app/scripts/start.sh` (or leave it empty to inherit the Dockerfile `CMD`). Railway documents config-as-code as deprecated, with existing support only through 2026-12-01; review the service's **Settings → Deploy** values directly in the dashboard.
3. In the project canvas, click **New → Database → PostgreSQL**. Keep the database private. On the API service's **Variables** page set `DATABASE_URL` to the reference variable of the Postgres service (e.g. `${{Postgres-0OQn.DATABASE_URL}}`), no manual scheme editing needed. Application settings normalize `postgres://` and `postgresql://` centrally to `postgresql+asyncpg://`, and leave URLs that already contain `+asyncpg` unchanged.
4. On the API service's **Variables** page, add these environment variable names:

   ```text
   RUN_EMBEDDED_WORKER
   DATABASE_URL
   LOG_LEVEL
   HELPDESK_MOCK_HOST
   LLM_PROVIDER
   LLM_BASE_URL
   LLM_MODEL
   LLM_API_KEY
   LLM_STATIC_FALLBACK_ENABLED
   LLM_TIMEOUT_SECONDS
   LLM_DAILY_CALL_CAP
   WORKER_CONCURRENCY
   WORKER_POLL_SECONDS
   WORKER_LEASE_SECONDS
   WORKER_MAX_ATTEMPTS
   WORKER_BACKOFF_BASE_SECONDS
   WORKER_BACKOFF_MAX_SECONDS
   SENDER_TIMEOUT_SECONDS
   SENDER_MAX_ATTEMPTS
   SENDER_LEASE_SECONDS
   ```

   `LLM_DAILY_CALL_CAP` defaults to 300 provider-call attempts per UTC day; set it to `0` only to disable the cap. PostgreSQL atomically coordinates the counter across workers and replicas. At the cap, generation skips the provider request and uses the static cited fallback after 5–30 seconds.

   For the intended open reviewer demo, do not set `OPERATOR_USERNAME` or `OPERATOR_PASSWORD`; the operator page will be open. Optional Basic Auth remains available by setting both variables through Railway's secure Variables form. The repository has not been deployed, so there are no live public URLs yet. Set `LLM_API_KEY` through the secure Variables form, never in a committed file. Do not copy `.env` into Railway. For the Free plan's constrained per-service resources, start with `WORKER_CONCURRENCY=2`; increase only after observing memory and CPU. The PostgreSQL daily call cap limits key usage to 300 provider-call attempts per UTC day by default.
5. To provide the demo mock, choose **New → GitHub Repo** and select the same repository again. In the mock service's **Settings → Deploy**, override its start command to:

   ```sh
   uvicorn app.helpdesk_mock:app --host 0.0.0.0 --port $PORT
   ```

   Add `MOCK_API_URL` pointing to the API service's private Railway hostname and its listening port followed by `/webhooks/helpdesk`. Set `MOCK_FAILURE_RATE`, `MOCK_HANG_RATE`, `MOCK_HANG_SECONDS`, `MOCK_FAIL_BEFORE_RECORD`, and `MOCK_DUPLICATE_MODE` to the desired demo behavior. The API image runs migrations automatically on deploy via `scripts/start.sh`; the mock service must override that command.
6. Deploy the API and mock. In API **Settings → Networking → Public Networking**, click **Generate Domain**. In the mock's networking settings, generate a domain only if you need public acceptance-test access to `/deliveries`; this exposes unauthenticated demo endpoints. Copy the mock domain's hostname into the API's `HELPDESK_MOCK_HOST`.
7. Set the API networking health check path to `/health`, if not already populated from config. Wait until the API reports healthy, then record:

   Verified live deployment endpoints:

   ```text
   Base URL: https://crud-production-a698.up.railway.app
   Health:   https://crud-production-a698.up.railway.app/health
   Webhook:  https://crud-production-a698.up.railway.app/webhooks/helpdesk
   Operator: https://crud-production-a698.up.railway.app/operator/moderation
   ```

   For the open reviewer demo, the operator URL requires no credentials. If Basic Auth is enabled instead, use HTTPS and the configured credentials.
8. Verified live behavior:
   - `GET /health` returns `HTTP 200 OK`.
   - `POST /webhooks/helpdesk` validates payload and returns `HTTP 200 {"status":"accepted"}` within milliseconds.
   - Sequential duplicate deliveries with the same `event_id` return the same `HTTP 200` without creating a duplicate ticket.
   - The embedded worker retrieves knowledge base articles, generates a cited draft, and the ticket appears at `https://crud-production-a698.up.railway.app/operator/moderation` in `draft_ready` status.

Railway's documented [public networking](https://docs.railway.com/networking/public-networking) provides a Railway domain and managed TLS. Railway variables are managed in the service's **Variables** page; see [Using Variables](https://docs.railway.com/variables) and [PostgreSQL connections](https://docs.railway.com/databases/postgresql).

## If using Render instead

The existing [`render.yaml`](render.yaml) describes a separate API, worker, and mock plus managed PostgreSQL. It is an alternative architecture, not the preferred single-web-service setup in this guide. A Free Render web service spins down after 15 minutes without inbound traffic and may take about one minute to spin back up; free PostgreSQL expires after 30 days. Therefore Render Free is unsuitable for an always-awake webhook. See [Render Free](https://render.com/docs/free). Use paid persistent services if choosing Render for this workload.
