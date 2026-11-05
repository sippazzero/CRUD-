"""Create ticket and knowledge article tables for durable processing."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001_initial_schema"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Set up durable deduplication and indexed PostgreSQL ticket and article search."""
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")

    op.create_table(
        "knowledge_articles",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("slug", sa.String(length=120), nullable=False),
        sa.Column("title", sa.String(length=240), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column(
            "search_vector",
            postgresql.TSVECTOR(),
            sa.Computed(
                "to_tsvector('simple'::regconfig, "
                "coalesce(title, '') || ' ' || coalesce(content, ''))",
                persisted=True,
            ),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("slug"),
    )
    op.create_index(
        "ix_knowledge_articles_search_vector",
        "knowledge_articles",
        ["search_vector"],
        postgresql_using="gin",
    )
    op.create_index(
        "ix_knowledge_articles_content_trgm",
        "knowledge_articles",
        ["content"],
        postgresql_using="gin",
        postgresql_ops={"content": "gin_trgm_ops"},
    )

    op.create_table(
        "tickets",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("event_id", sa.String(length=255), nullable=False),
        sa.Column("ticket_id", sa.String(length=255), nullable=False),
        sa.Column("customer_email", sa.String(length=320), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("reply_url", sa.Text(), nullable=False),
        sa.Column(
            "status",
            sa.String(length=32),
            server_default=sa.text("'received'"),
            nullable=False,
        ),
        sa.Column(
            "attempts",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column(
            "next_attempt_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("locked_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("draft_text", sa.Text(), nullable=True),
        sa.Column(
            "cited_article_slugs",
            postgresql.ARRAY(sa.Text()),
            server_default=sa.text("'{}'::text[]"),
            nullable=False,
        ),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("event_id"),
    )
    op.create_index("ix_tickets_ticket_id", "tickets", ["ticket_id"])
    op.create_index(
        "ix_tickets_status_next_attempt_at",
        "tickets",
        ["status", "next_attempt_at"],
    )


def downgrade() -> None:
    """Remove application tables without disturbing a shared pg_trgm extension."""
    op.drop_index("ix_tickets_status_next_attempt_at", table_name="tickets")
    op.drop_index("ix_tickets_ticket_id", table_name="tickets")
    op.drop_table("tickets")
    op.drop_index(
        "ix_knowledge_articles_content_trgm",
        table_name="knowledge_articles",
    )
    op.drop_index(
        "ix_knowledge_articles_search_vector",
        table_name="knowledge_articles",
    )
    op.drop_table("knowledge_articles")
