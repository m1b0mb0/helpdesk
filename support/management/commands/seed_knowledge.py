from django.core.management.base import BaseCommand
from django.db import transaction

from support.knowledge_seed import ARTICLES
from support.models import KnowledgeArticle


class Command(BaseCommand):
    help = "Idempotently load the eight AI-authored MetricNest knowledge articles."

    @transaction.atomic
    def handle(self, *args, **options):
        created_count = 0
        for article in ARTICLES:
            values = dict(article)
            slug = values.pop("slug")
            _, created = KnowledgeArticle.objects.update_or_create(slug=slug, defaults=values)
            created_count += created
        self.stdout.write(self.style.SUCCESS(
            f"Loaded {len(ARTICLES)} articles ({created_count} created)."
        ))
