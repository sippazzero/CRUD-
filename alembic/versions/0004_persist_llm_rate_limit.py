"""Persist one shared token bucket so restarts cannot reset the LLM quota."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004_persist_llm_rate_limit"
down_revision: str | None = "0003_seed_knowledge_articles"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create and initialize the single service-wide rate-limit row."""
    op.create_table(
        "llm_rate_limit_state",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("tokens", sa.Float(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("id = 1", name="ck_llm_rate_limit_singleton"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.execute(
        "INSERT INTO llm_rate_limit_state (id, tokens, updated_at) VALUES (1, 1, now())"
    )


def downgrade() -> None:
    """Drop the persisted limiter state when reverting this revision."""
    op.drop_table("llm_rate_limit_state")
