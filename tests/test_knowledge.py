"""PostgreSQL integration coverage for seeded knowledge retrieval."""

import pytest
from app.services.knowledge import find_relevant_articles
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession


@pytest.mark.asyncio
async def test_pdf_export_message_retrieves_seeded_article(
    postgres_engine: AsyncEngine,
) -> None:
    """FTS and trigram ranking return the seeded article for its topic."""
    async with AsyncSession(postgres_engine) as session:
        articles = await find_relevant_articles(
            session,
            "Як експортувати звіт у PDF?",
        )

    assert 1 <= len(articles) <= 3
    assert articles[0].slug == "export-pdf-report"


@pytest.mark.asyncio
async def test_english_pdf_export_message_retrieves_seeded_article(
    postgres_engine: AsyncEngine,
) -> None:
    """English task text still finds the Ukrainian PDF-export article."""
    async with AsyncSession(postgres_engine) as session:
        articles = await find_relevant_articles(
            session,
            "Cannot export my report to PDF",
        )

    assert articles
    assert articles[0].slug == "export-pdf-report"
