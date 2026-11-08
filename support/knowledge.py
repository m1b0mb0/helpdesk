import re

from django.conf import settings
from django.urls import reverse

from .models import KnowledgeArticle


def _tokens(text):
    return re.findall(r"[^\W_]+", text.casefold(), flags=re.UNICODE)


def search_articles(message, limit=3):
    """Rank a tiny corpus using explicit bilingual topic aliases, not semantics.

    Prefix aliases cover common Ukrainian inflections and English endings.
    Generic words such as 'report' deliberately are not topic aliases.
    """
    tokens = _tokens(message)
    phrase = " ".join(tokens)
    scored = []
    for article in KnowledgeArticle.objects.all():
        score = 0
        for alias in article.keywords:
            words = _tokens(alias)
            if len(words) > 1:
                matched = f" {' '.join(words)} " in f" {phrase} "
            else:
                matched = bool(words) and any(
                    token.startswith(words[0]) if len(words[0]) >= 3 else token == words[0]
                    for token in tokens
                )
            if matched:
                score += 8 if len(words) > 1 else 4
        if score:
            scored.append((score, article))
    scored.sort(key=lambda item: (-item[0], item[1].slug))
    if not scored:
        return []
    # Avoid adding a weak generic topic match alongside a much stronger one.
    minimum = scored[0][0] * 0.6
    return [article for score, article in scored if score >= minimum][:limit]


def article_source(article):
    return {
        "id": article.pk,
        "title": article.title,
        "url": settings.PUBLIC_BASE_URL.rstrip("/")
        + reverse("knowledge_article", kwargs={"slug": article.slug}),
    }
