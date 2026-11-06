# Deployment preparation

This guide prepares the service for Render or Railway; it does not deploy anything. Configure both operator Basic Auth variables before any public deployment and serve the API over HTTPS. Do not commit provider credentials, operator passwords, or database URLs; enter them in the hosting dashboard.

## Render

The optional [render.yaml](render.yaml) Blueprint describes a managed PostgreSQL database and three services built from the same repository `Dockerfile`: a public API, a background worker, and a private helpdesk mock. It uses `sync: false` entries for values that must be supplied in the dashboard. Blueprint variables do not transform Render's PostgreSQL connection-string scheme, so set `DATABASE_URL` manually as described below.

1. Push the repository to GitHub and connect it to Render. Create a new Blueprint from the repository root and review the resources before applying them. Alternatively, create the resources manually using the service settings below.
2. Create a managed PostgreSQL 16 database in the same Render region as the services. In its **Connect** panel, copy the internal connection URL. Set the scheme to `postgresql+asyncpg://` (instead of `postgresql://`) and place the resulting URL in the API and worker `DATABASE_URL` dashboard variables. Treat the URL as a secret.
3. Configure the API as a **Web Service** using the repository root and `Dockerfile`. Set its start command to:

   ```sh
   sh -c 'alembic upgrade head && exec uvicorn app.main:app --host 0.0.0.0 --port "${PORT:-8000}"'
   ```

   Set the health check path to `/health`. Migrations run before Uvicorn starts; if migration fails, the API process will not start.
4. Configure the worker as a **Background Worker** from the same repository and `Dockerfile`. Use:

   ```sh
   python -m app.worker
   ```

   Set one worker instance initially. All worker replicas share the PostgreSQL rate limiter, but they also add database contention and should not be scaled without measurement.
5. Configure the mock as a **Private Service** from the same repository and `Dockerfile`. Use:

   ```sh
   sh -c 'exec uvicorn app.helpdesk_mock:app --host 0.0.0.0 --port "${PORT:-10000}"'
   ```

   Keep it private; the mock's `/deliveries` endpoint has no authentication. Set `MOCK_API_URL` to the API's internal URL plus `/webhooks/helpdesk`. For simulated tickets, use the mock's private service address with `/reply` as `reply_url`.
6. Set dashboard environment variables:

   - **API:** `DATABASE_URL`, `LOG_LEVEL=INFO`, `HELPDESK_MOCK_HOST` set to the private mock service hostname, and both `OPERATOR_USERNAME` and `OPERATOR_PASSWORD` set to unique credentials.
   - **Worker:** the same `DATABASE_URL` and `HELPDESK_MOCK_HOST`; `LLM_PROVIDER=ollama`, `LLM_BASE_URL=https://ollama.com`, `LLM_MODEL=gemma4:31b`, and `LLM_API_KEY` (optional for the static fallback); `LLM_STATIC_FALLBACK_ENABLED=true`, `LLM_TIMEOUT_SECONDS=30`; `WORKER_CONCURRENCY=30`, `WORKER_POLL_SECONDS=1`, `WORKER_LEASE_SECONDS=120`, `WORKER_MAX_ATTEMPTS=5`, `WORKER_BACKOFF_BASE_SECONDS=5`, `WORKER_BACKOFF_MAX_SECONDS=300`; and `SENDER_TIMEOUT_SECONDS=10`, `SENDER_MAX_ATTEMPTS=5`, `SENDER_LEASE_SECONDS=300`.
   - **Helpdesk mock:** `MOCK_API_URL`, `MOCK_FAILURE_RATE=0`, `MOCK_HANG_RATE=0`, `MOCK_HANG_SECONDS=15`, `MOCK_FAIL_BEFORE_RECORD=0`, `MOCK_DUPLICATE_MODE=false`.

   Render services in the same region can communicate over private service hostnames; copy the actual internal addresses from each service's **Connect** panel rather than assuming a hostname. If no LLM key is set, the worker still attempts the provider request and uses the configured static fallback after its simulated delay.
7. Deploy the services and wait for the API health check to pass. Copy the API service's public `onrender.com` address from its dashboard and give the helpdesk this webhook URL:

   ```text
   https://<api-service-name>.onrender.com/webhooks/helpdesk
   ```

   The same public API also serves `/operator/moderation`; Basic Auth is enabled by the required dashboard credentials. Use the same credentials for approve/reject actions and do not distribute them in webhook configuration.

## Railway alternative

Create a Railway project with a managed PostgreSQL service, then create three services from the same GitHub repository so each builds from the root `Dockerfile`. Set the API start command to the migration-plus-Uvicorn command above, the worker command to `python -m app.worker`, and the mock command to the Uvicorn command above. Configure the same per-service environment variables listed for Render, using Railway's private service addresses and a `postgresql+asyncpg://` `DATABASE_URL`; set both operator credentials in the dashboard and do not commit generated credentials. Generate a public domain for the API service and append `/webhooks/helpdesk` to form the webhook URL. The operator page uses HTTP Basic Auth and must be served only over HTTPS.

## Platform references

- [Render Docker deploys](https://render.com/docs/docker)
- [Render Blueprints](https://render.com/docs/blueprint-spec)
- [Render managed PostgreSQL](https://render.com/docs/postgresql-creating-connecting)
- [Render private networking](https://render.com/docs/private-network)
- [Render health checks](https://render.com/docs/health-checks)
- [Railway Dockerfiles](https://docs.railway.com/builds/dockerfiles)
- [Railway start commands](https://docs.railway.com/builds/build-and-start-commands)
