"""Track actual provider-call reservations by UTC calendar day."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006_daily_llm_call_cap"
down_revision: str | None = "0005_ticket_delivery_attempts"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the shared daily usage table used by all worker instances."""
    op.create_table(
        "llm_daily_call_usage",
        sa.Column("utc_day", sa.Date(), nullable=False),
        sa.Column("call_count", sa.Integer(), nullable=False),
        sa.CheckConstraint(
            "call_count >= 0",
            name="ck_llm_daily_call_usage_nonnegative",
        ),
        sa.PrimaryKeyConstraint("utc_day"),
    )


def downgrade() -> None:
    """Remove daily provider-call usage when reverting this revision."""
    op.drop_table("llm_daily_call_usage")
