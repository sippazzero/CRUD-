"""PostgreSQL full-text and trigram retrieval for the support knowledge base."""

import re

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import KnowledgeArticle

_ENGLISH_TO_UKRAINIAN = {
    "export": "експорт",
    "report": "звіту",
    "pdf": "pdf",
    "import": "імпорт",
    "data": "даних",
    "password": "пароля",
    "account": "облікового запису",
    "notification": "сповіщень",
    "notifications": "сповіщень",
    "billing": "рахунків",
    "invoice": "рахунків",
    "team": "команди",
    "invite": "запрошення",
    "troubleshooting": "проблем",
}


async def find_relevant_articles(
    session: AsyncSession,
    message: str,
) -> list[KnowledgeArticle]:
    """Return up to three articles ranked by token relevance and trigram overlap."""
    queries = [func.plainto_tsquery("simple", message)]
    translated_terms = [
        _ENGLISH_TO_UKRAINIAN[token]
        for token in re.findall(r"[a-z]+", message.lower())
        if token in _ENGLISH_TO_UKRAINIAN
    ]
    if translated_terms:
        queries.append(func.plainto_tsquery("simple", " ".join(translated_terms)))

    full_text_matches = [
        KnowledgeArticle.search_vector.bool_op("@@")(query) for query in queries
    ]
    trigram_matches = [
        KnowledgeArticle.title.bool_op("%")(query_text)
        | KnowledgeArticle.content.bool_op("%")(query_text)
        for query_text in (message, " ".join(translated_terms))
        if query_text
    ]
    ranks = [
        func.ts_rank_cd(KnowledgeArticle.search_vector, query) for query in queries
    ]
    similarities = [
        func.similarity(KnowledgeArticle.title, query_text)
        + func.similarity(KnowledgeArticle.content, query_text)
        for query_text in (message, " ".join(translated_terms))
        if query_text
    ]
    statement = (
        select(KnowledgeArticle)
        .where(or_(*full_text_matches, *trigram_matches))
        .order_by((sum(ranks) + sum(similarities)).desc(), KnowledgeArticle.slug)
        .limit(3)
    )
    result = await session.scalars(statement)
    return list(result.all())
