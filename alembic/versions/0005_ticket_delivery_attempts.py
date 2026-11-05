"""Track reply-delivery retries independently from draft-generation attempts."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005_ticket_delivery_attempts"
down_revision: str | None = "0004_persist_llm_rate_limit"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add a durable counter for enforcing the delivery retry limit."""
    op.add_column(
        "tickets",
        sa.Column(
            "delivery_attempts",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
    )


def downgrade() -> None:
    """Remove only the delivery retry counter."""
    op.drop_column("tickets", "delivery_attempts")
