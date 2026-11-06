#!/bin/sh
set -e

export RUN_EMBEDDED_WORKER="${RUN_EMBEDDED_WORKER:-true}"

alembic upgrade head
exec uvicorn app.main:app --host 0.0.0.0 --port "${PORT:-8000}"
