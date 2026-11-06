FROM python:3.12.16-slim@sha256:4840d514c583a4d3d8cad3940e68dee6f0f6f3635c12fcd0a3d74c21a5b93a54

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY pyproject.toml ./
COPY app ./app
COPY alembic.ini ./
COPY alembic ./alembic
COPY scripts ./scripts
COPY tests ./tests

RUN pip install --no-cache-dir ".[dev]" \
    && groupadd --system app \
    && useradd --system --gid app --create-home app \
    && chmod +x scripts/start.sh

USER app

EXPOSE 8000

CMD ["/app/scripts/start.sh"]
