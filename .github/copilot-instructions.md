# Project Instructions

## Stack and runtime

- Use Python 3.12, FastAPI, SQLAlchemy 2.0 async with `asyncpg`, Alembic, PostgreSQL 16, `httpx`, `pydantic-settings`, `pytest`, `pytest-asyncio`, and Ruff.
- Run the complete application through Docker Compose. The API, worker, PostgreSQL, and helpdesk mock are separate services.
- Do not add Redis or RabbitMQ. PostgreSQL is the durable work queue; workers claim jobs with `FOR UPDATE SKIP LOCKED`.

## Code and configuration

- Add type hints to all Python code.
- Read configuration only from environment variables through `pydantic-settings`. Keep `.env.example` committed as a template, and never commit credentials or other secrets.
- Keep webhook handling short: validate and persist the event, then respond. Do not call an LLM or make outbound HTTP requests from the webhook handler.
- Preserve idempotency, durable ticket processing, and safe state transitions when changing queue or delivery behavior.

## Documentation and comments

- Write code comments and docstrings professionally in English. Explain intent, invariants, and trade-offs—the “why”—rather than restating the code.
- Add Google-style docstrings to all public modules, classes, and functions.
- Avoid noise comments, emojis, and commented-out code.

## Quality and workflow

- Use small, focused commits with conventional commit messages. Never squash, amend, or rewrite commit history.
- After each change, run the tests and report the result. Run the relevant Ruff checks as appropriate, and do not claim a check passed if it was not run.
- Keep changes focused on the requested task and update related documentation when behavior or design changes.
