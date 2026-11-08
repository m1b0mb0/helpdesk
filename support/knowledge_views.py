from django.shortcuts import get_object_or_404, render
from django.views.decorators.http import require_safe

from .models import KnowledgeArticle


@require_safe
def knowledge_index(request):
    return render(request, "support/knowledge_index.html", {
        "articles": KnowledgeArticle.objects.all(),
    })


@require_safe
def knowledge_article(request, slug):
    return render(request, "support/knowledge_article.html", {
        "article": get_object_or_404(KnowledgeArticle, slug=slug),
    })
