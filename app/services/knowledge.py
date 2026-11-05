"""PostgreSQL full-text and trigram retrieval for the support knowledge base."""

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import KnowledgeArticle


async def find_relevant_articles(
    session: AsyncSession,
    message: str,
) -> list[KnowledgeArticle]:
    """Return up to three articles ranked by token relevance and trigram overlap."""
    query = func.plainto_tsquery("simple", message)
    full_text_match = KnowledgeArticle.search_vector.bool_op("@@")(query)
    title_trigram_match = KnowledgeArticle.title.bool_op("%")(message)
    content_trigram_match = KnowledgeArticle.content.bool_op("%")(message)
    rank = func.ts_rank_cd(KnowledgeArticle.search_vector, query)
    title_similarity = func.similarity(KnowledgeArticle.title, message)
    content_similarity = func.similarity(KnowledgeArticle.content, message)
    statement = (
        select(KnowledgeArticle)
        .where(or_(full_text_match, title_trigram_match, content_trigram_match))
        .order_by(
            (rank + title_similarity + content_similarity).desc(),
            KnowledgeArticle.slug,
        )
        .limit(3)
    )
    result = await session.scalars(statement)
    return list(result.all())
