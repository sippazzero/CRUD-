"""SQLAlchemy models for durable ticket processing and knowledge articles."""

from datetime import date, datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    Computed,
    Date,
    DateTime,
    Float,
    Index,
    Integer,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import (
    ARRAY,
    TSVECTOR,
)
from sqlalchemy.dialects.postgresql import (
    UUID as PostgreSQLUUID,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """Declarative base shared by application tables and migrations."""


class KnowledgeArticle(Base):
    """A stable, searchable knowledge-base article."""

    __tablename__ = "knowledge_articles"
    __table_args__ = (
        Index(
            "ix_knowledge_articles_search_vector",
            "search_vector",
            postgresql_using="gin",
        ),
        Index(
            "ix_knowledge_articles_content_trgm",
            "content",
            postgresql_using="gin",
            postgresql_ops={"content": "gin_trgm_ops"},
        ),
    )

    id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        primary_key=True,
        default=uuid4,
        server_default=text("gen_random_uuid()"),
    )
    slug: Mapped[str] = mapped_column(String(120), unique=True, nullable=False)
    title: Mapped[str] = mapped_column(String(240), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    search_vector: Mapped[str] = mapped_column(
        TSVECTOR,
        Computed(
            "to_tsvector('simple'::regconfig, "
            "coalesce(title, '') || ' ' || coalesce(content, ''))",
            persisted=True,
        ),
        nullable=False,
    )


class Ticket(Base):
    """A deduplicated helpdesk event and its persisted workflow state."""

    __tablename__ = "tickets"
    __table_args__ = (
        Index("ix_tickets_ticket_id", "ticket_id"),
        Index("ix_tickets_status_next_attempt_at", "status", "next_attempt_at"),
    )

    id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        primary_key=True,
        default=uuid4,
        server_default=text("gen_random_uuid()"),
    )
    event_id: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    ticket_id: Mapped[str] = mapped_column(String(255), nullable=False)
    customer_email: Mapped[str] = mapped_column(String(320), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    reply_url: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="new",
        server_default=text("'new'"),
    )
    attempts: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default=text("0"),
    )
    delivery_attempts: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default=text("0"),
    )
    next_attempt_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    locked_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    draft_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    cited_article_slugs: Mapped[list[str]] = mapped_column(
        ARRAY(Text),
        nullable=False,
        default=list,
        server_default=text("'{}'::text[]"),
    )
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )
    approved_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    sent_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )


class LLMRateLimitState(Base):
    """Persisted token-bucket state shared by all worker processes."""

    __tablename__ = "llm_rate_limit_state"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tokens: Mapped[float] = mapped_column(Float, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )


class LLMDailyCallUsage(Base):
    """Count provider-call reservations for each UTC calendar day."""

    __tablename__ = "llm_daily_call_usage"

    utc_day: Mapped[date] = mapped_column(Date, primary_key=True)
    call_count: Mapped[int] = mapped_column(Integer, nullable=False)
