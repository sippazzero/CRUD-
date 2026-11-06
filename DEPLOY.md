# Railway deployment preparation

This repository is prepared for Railway, but no service has been deployed. The Railway CLI is not installed in the current environment, and no Railway dashboard login is available, so public URLs and live deployment checks cannot be produced here.

## Architecture and trade-offs

Use one always-on Railway web service for FastAPI and its embedded worker, plus Railway-managed PostgreSQL and a separate mock service. Set `RUN_EMBEDDED_WORKER=true` only on the API service; the default is false and Docker Compose keeps its independent worker container. The API and worker share a process and resource budget, so a process restart interrupts an in-flight call; ticket state and leases remain in PostgreSQL, and an expired lease lets work resume after restart. One web process avoids separate API/worker service overhead but does not provide worker/API fault isolation or independent scaling.

The mock is a separate, optional demo service. Its `/deliveries` ledger is in memory and its endpoints have no authentication; do not treat it as a production helpdesk or expose customer data through it. If a public domain is enabled for acceptance testing, remove the mock service or disable its public domain afterwards. `HELPDESK_MOCK_HOST` must exactly match the hostname used in the webhook `reply_url`; this is the deliberate callback-validation exception for the mock.

PostgreSQL is the only durable queue/state store. No SQLite or local filesystem state is required. The API startup command runs Alembic migrations before Uvicorn, and binds to Railway's `PORT`. The health check is `/health`.

Railway currently lists a Free plan with $1/month usage credit, a per-service maximum of 0.5 GB RAM, 1 vCPU and one replica. Treat it as a temporary demo tier: exhausting credit may interrupt workloads, and it is not an always-on production guarantee. Railway's persistent services are not documented as sleeping after inbound inactivity; deploys, failures, or credit exhaustion can still cause downtime. The Hobby plan is currently $5/month and includes $5 of resource usage; usage above that is billed. Verify current limits and pricing in Railway before deployment. See [Railway plans](https://docs.railway.com/pricing/plans) and [persistent services](https://docs.railway.com/services).

## Click-by-click Railway setup

1. Sign in at <https://railway.com>, create a project, choose **Deploy from GitHub repo**, authorize Railway's GitHub app if prompted, and select `sippazzero/CRUD-` on `main`.
2. Select the API service. In **Settings → Build**, use the repository `Dockerfile`. The committed [`railway.json`](railway.json) supplies the Dockerfile builder, migration-plus-Uvicorn start command, `/health` check and restart policy. Railway documents config-as-code as deprecated, with existing support only through 2026-12-01; review the service's **Settings → Deploy** values directly and set them in the dashboard if the file is no longer honored.
3. In the project canvas, click **New → Database → PostgreSQL**. Keep the database private. On the API service's **Variables** page add `DATABASE_URL` using a Railway reference to the PostgreSQL service's `DATABASE_URL`. The application converts Railway's `postgres://` or `postgresql://` URL scheme to SQLAlchemy's asyncpg driver scheme at startup.
4. On the API service's **Variables** page, add these environment variable names:

   ```text
   RUN_EMBEDDED_WORKER
   DATABASE_URL
   LOG_LEVEL
   HELPDESK_MOCK_HOST
   OPERATOR_USERNAME
   OPERATOR_PASSWORD
   LLM_PROVIDER
   LLM_BASE_URL
   LLM_MODEL
   LLM_API_KEY
   LLM_STATIC_FALLBACK_ENABLED
   LLM_TIMEOUT_SECONDS
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

   Set `OPERATOR_PASSWORD` using a generated strong value in Railway's secure Variables form, never in the repository. Set `LLM_API_KEY` through the same secure form. Do not copy `.env` into Railway. For the Free plan's constrained per-service resources, start with `WORKER_CONCURRENCY=2`; increase only after observing memory and CPU. The database-backed rate limiter remains the cap of 60 provider requests per minute.
5. To provide the demo mock, choose **New → GitHub Repo** and select the same repository again. In the mock service's **Settings → Deploy**, override its start command to:

   ```sh
   uvicorn app.helpdesk_mock:app --host 0.0.0.0 --port $PORT
   ```

   Add `MOCK_API_URL` pointing to the API service's private Railway hostname and its listening port followed by `/webhooks/helpdesk`. Set `MOCK_FAILURE_RATE`, `MOCK_HANG_RATE`, `MOCK_HANG_SECONDS`, `MOCK_FAIL_BEFORE_RECORD`, and `MOCK_DUPLICATE_MODE` to the desired demo behavior. The API image's Railway start command runs migrations; the mock service must override that command.
6. Deploy the API and mock. In API **Settings → Networking → Public Networking**, click **Generate Domain**. In the mock's networking settings, generate a domain only if you need public acceptance-test access to `/deliveries`; this exposes unauthenticated demo endpoints. Copy the mock domain's hostname into the API's `HELPDESK_MOCK_HOST`.
7. Set the API networking health check path to `/health`, if not already populated from config. Wait until the API reports healthy, then record:

   ```text
   Webhook: https://<api-domain>/webhooks/helpdesk
   Operator: https://<api-domain>/operator/moderation
   Mock:     https://<mock-domain>
   ```

   These are URL templates only, not live deployment URLs. The hosted operator page requires HTTP Basic Auth; use the configured `OPERATOR_USERNAME` and `OPERATOR_PASSWORD` over HTTPS.
8. For a live acceptance check, POST a webhook with `reply_url` set to `https://<mock-domain>/reply`, wait for `draft_ready`, open the operator URL with Basic Auth, approve exactly one ticket, then inspect `https://<mock-domain>/deliveries`. Remove the mock public domain after testing if public ledger access is not intended.

Railway's documented [public networking](https://docs.railway.com/networking/public-networking) provides a Railway domain and managed TLS. Railway variables are managed in the service's **Variables** page; see [Using Variables](https://docs.railway.com/variables) and [PostgreSQL connections](https://docs.railway.com/databases/postgresql).

## If using Render instead

The existing [`render.yaml`](render.yaml) describes a separate API, worker, and mock plus managed PostgreSQL. It is an alternative architecture, not the preferred single-web-service setup in this guide. A Free Render web service spins down after 15 minutes without inbound traffic and may take about one minute to spin back up; free PostgreSQL expires after 30 days. Therefore Render Free is unsuitable for an always-awake webhook. See [Render Free](https://render.com/docs/free). Use paid persistent services if choosing Render for this workload.
