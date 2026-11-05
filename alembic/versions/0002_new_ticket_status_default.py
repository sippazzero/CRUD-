"""Use the webhook ingestion state as the default for new ticket rows."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002_new_ticket_status_default"
down_revision: str | None = "0001_initial_schema"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Align the database default with the webhook's explicit initial state."""
    op.alter_column(
        "tickets",
        "status",
        existing_type=sa.String(length=32),
        server_default=sa.text("'new'"),
    )


def downgrade() -> None:
    """Restore the prior default when reverting this change."""
    op.alter_column(
        "tickets",
        "status",
        existing_type=sa.String(length=32),
        server_default=sa.text("'received'"),
    )
