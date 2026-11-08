from io import StringIO

from django.core.management import call_command
from django.test import TestCase, override_settings
from django.urls import reverse

from support.knowledge import article_source, search_articles
from support.models import KnowledgeArticle


class KnowledgeTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command("seed_knowledge", stdout=StringIO())

    def test_seed_is_idempotent_and_updates_known_slugs(self):
        original_ids = dict(KnowledgeArticle.objects.values_list("slug", "pk"))
        KnowledgeArticle.objects.filter(slug="pdf-export").update(body="outdated")
        call_command("seed_knowledge", stdout=StringIO())
        self.assertEqual(KnowledgeArticle.objects.count(), 8)
        self.assertEqual(dict(KnowledgeArticle.objects.values_list("slug", "pk")), original_ids)
        self.assertNotEqual(KnowledgeArticle.objects.get(slug="pdf-export").body, "outdated")

    def test_bilingual_search_covers_all_topics(self):
        queries = [
            ("Не можу експортувати звіт у PDF", "pdf-export"),
            ("How can I export PDF?", "pdf-export"),
            ("Excel показує дивні символи у CSV", "csv-export"),
            ("CSV encoding is garbled", "csv-export"),
            ("Дані застарілі, синхронізація не працює", "data-refresh"),
            ("Data is stale, refresh my connector", "data-refresh"),
            ("За розкладом не надходять листи", "scheduled-email"),
            ("Scheduled email delivery failed", "scheduled-email"),
            ("Як надати доступ колезі?", "sharing-permissions"),
            ("Invite colleague with Viewer permissions", "sharing-permissions"),
            ("Часовий пояс впливає на фільтр дат", "timezone-filters"),
            ("My time zone and date filters differ", "timezone-filters"),
            ("Як змінити тариф та отримати рахунок?", "plans-billing"),
            ("Subscription invoice and billing limits", "plans-billing"),
            ("Забув пароль для входу", "account-login"),
            ("Lost my 2FA authenticator", "account-login"),
        ]
        for query, expected in queries:
            with self.subTest(query=query):
                self.assertEqual(search_articles(query)[0].slug, expected)

    def test_unrelated_or_generic_query_does_not_invent_sources(self):
        for query in ["", "hello", "weather in Kyiv", "потрібна допомога", "report", "звіт"]:
            with self.subTest(query=query):
                self.assertEqual(search_articles(query), [])

    def test_specific_topic_outranks_generic_export_match(self):
        self.assertEqual(
            [article.slug for article in search_articles("export PDF")],
            ["pdf-export"],
        )

    def test_search_limit(self):
        self.assertLessEqual(len(search_articles("pdf csv timezone billing password", limit=2)), 2)

    @override_settings(PUBLIC_BASE_URL="https://support.example.test")
    def test_sources_have_stable_public_urls_and_real_ids(self):
        article = KnowledgeArticle.objects.get(slug="pdf-export")
        source = article_source(article)
        self.assertEqual(source, {
            "id": article.pk,
            "title": article.title,
            "url": "https://support.example.test/knowledge/pdf-export",
        })

    def test_article_pages_and_index_are_read_only(self):
        index = self.client.get(reverse("knowledge_index"))
        self.assertEqual(index.status_code, 200)
        for article in KnowledgeArticle.objects.all():
            url = reverse("knowledge_article", kwargs={"slug": article.slug})
            self.assertContains(index, url)
            self.assertContains(self.client.get(url), article.title)
            self.assertEqual(self.client.head(url).status_code, 200)
            self.assertEqual(self.client.post(url).status_code, 405)
        self.assertEqual(self.client.post(reverse("knowledge_index")).status_code, 405)
        self.assertEqual(self.client.get("/knowledge/not-a-real-article").status_code, 404)

    def test_article_html_is_escaped(self):
        article = KnowledgeArticle.objects.get(slug="pdf-export")
        article.body = '<script>alert("untrusted")</script>'
        article.save()
        response = self.client.get(reverse("knowledge_article", kwargs={"slug": article.slug}))
        self.assertContains(response, "&lt;script&gt;")
        self.assertNotContains(response, "<script>")
