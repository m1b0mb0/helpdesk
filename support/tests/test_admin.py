from unittest.mock import patch

from django.contrib import admin
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from support.knowledge import article_source
from support.models import KnowledgeArticle, SupportRequest


@override_settings(STORAGES={
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
})
class OperatorAdminTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        users = get_user_model()
        cls.operator = users.objects.create_user(username="operator", is_staff=True)
        cls.viewer = users.objects.create_user(username="viewer", is_staff=True)
        cls.change_only = users.objects.create_user(username="change-only", is_staff=True)
        cls.no_view = users.objects.create_user(username="no-view", is_staff=True)
        cls.customer = users.objects.create_user(username="customer")
        cls.inactive = users.objects.create_user(username="inactive", is_staff=True, is_active=False)
        view = Permission.objects.get(codename="view_supportrequest")
        moderate = Permission.objects.get(codename="moderate_supportrequest")
        change = Permission.objects.get(codename="change_supportrequest")
        cls.operator.user_permissions.add(view, moderate)
        cls.viewer.user_permissions.add(view)
        cls.change_only.user_permissions.add(view, change)
        cls.no_view.user_permissions.add(moderate)
        cls.customer.user_permissions.add(view, moderate)
        cls.inactive.user_permissions.add(view, moderate)
        cls.article = KnowledgeArticle.objects.create(
            slug="admin-pdf", title="PDF export guide", body="Choose Export then PDF.",
            keywords=["pdf", "export"],
        )

    def setUp(self):
        self.incoming = SupportRequest.objects.create(
            event_id="evt_admin_review", ticket_id="T-ADMIN", customer_email="reader@example.com",
            message="Original PDF question", reply_url="https://helpdesk.example.com/tickets/T-ADMIN/replies",
            status=SupportRequest.Status.REVIEW,
            draft_text="Choose Export then PDF.\n" + article_source(self.article)["url"],
            draft_sources=[article_source(self.article)],
        )
        self.list_url = reverse("admin:support_supportrequest_changelist")
        self.detail_url = reverse("admin:support_supportrequest_change", args=[self.incoming.pk])

    def action(self, name, ids=None, client=None, follow=True):
        return (client or self.client).post(self.list_url, {
            "action": name,
            "_selected_action": ids or [self.incoming.pk],
            "index": "0",
        }, follow=follow)

    def test_operator_can_view_list_details_and_verified_links(self):
        self.client.force_login(self.operator)
        listing = self.client.get(self.list_url)
        for value in ("T-ADMIN", "reader@example.com", "Awaiting review", "Created at", "Updated at"):
            self.assertContains(listing, value)
        self.assertContains(listing, "Approve selected drafts")
        self.assertContains(listing, "Reject selected drafts")
        detail = self.client.get(self.detail_url)
        self.assertContains(detail, "Original PDF question")
        self.assertContains(detail, "Choose Export then PDF.")
        self.assertContains(detail, '<a href="' + article_source(self.article)["url"] + '">PDF export guide</a>', html=True)
        self.assertNotContains(detail, 'name="_save"')

    def test_filters_and_search_include_ticket_event_and_email(self):
        self.client.force_login(self.operator)
        for query in ("T-ADMIN", "evt_admin_review", "reader@example.com"):
            self.assertContains(self.client.get(self.list_url, {"q": query}), "T-ADMIN")
        self.assertContains(self.client.get(self.list_url, {"status__exact": "review"}), "T-ADMIN")
        self.assertNotContains(self.client.get(self.list_url, {"status__exact": "pending"}), "T-ADMIN")

    def test_generation_error_and_state_are_visible(self):
        self.incoming.status = SupportRequest.Status.FAILED
        self.incoming.last_error = "Controlled provider timeout"
        self.incoming.save()
        self.client.force_login(self.viewer)
        detail = self.client.get(self.detail_url)
        self.assertContains(detail, "Controlled provider timeout")
        self.assertContains(detail, "Failed")

    def test_anonymous_requests_redirect_to_login_without_mutation(self):
        self.assertRedirects(self.client.get(self.detail_url), reverse("admin:login") + "?next=" + self.detail_url)
        response = self.action("approve_selected", follow=False)
        self.assertEqual(response.status_code, 302)
        self.incoming.refresh_from_db()
        self.assertEqual(self.incoming.status, "review")

    def test_nonstaff_and_inactive_users_cannot_moderate(self):
        for user in (self.customer, self.inactive):
            with self.subTest(user=user.username):
                self.client.force_login(user)
                self.assertEqual(self.client.get(self.list_url).status_code, 302)
                self.assertEqual(self.action("approve_selected", follow=False).status_code, 302)
        self.incoming.refresh_from_db()
        self.assertEqual(self.incoming.status, "review")

    def test_viewer_and_change_permission_do_not_grant_moderation(self):
        for user in (self.viewer, self.change_only):
            with self.subTest(user=user.username):
                self.client.force_login(user)
                page = self.client.get(self.list_url)
                self.assertEqual(page.status_code, 200)
                self.assertNotContains(page, "Approve selected drafts")
                self.action("approve_selected")
                self.incoming.refresh_from_db()
                self.assertEqual(self.incoming.status, "review")

    def test_moderation_permission_without_view_does_not_grant_access(self):
        self.client.force_login(self.no_view)
        self.assertEqual(self.client.get(self.list_url).status_code, 403)
        self.assertEqual(self.action("approve_selected", follow=False).status_code, 403)
        self.incoming.refresh_from_db()
        self.assertEqual(self.incoming.status, "review")

    def test_admin_approve_snapshots_draft_and_replay_does_not_change_key(self):
        self.client.force_login(self.operator)
        with patch("support.tasks.generate_reply.delay") as publish:
            self.assertContains(self.action("approve_selected"), "Approved: 1")
            self.incoming.refresh_from_db()
            key = self.incoming.delivery_key
            self.assertEqual(self.incoming.status, "approved")
            self.assertEqual(self.incoming.approved_text, self.incoming.draft_text)
            self.assertEqual(self.incoming.moderated_by_id, self.operator.pk)
            self.assertIsNotNone(self.incoming.moderated_at)
            self.assertIsNotNone(key)
            self.assertContains(self.action("approve_selected"), "Skipped")
            publish.assert_not_called()
        self.incoming.refresh_from_db()
        self.assertEqual(self.incoming.delivery_key, key)

    def test_admin_reject_has_no_delivery_intent(self):
        self.client.force_login(self.operator)
        self.assertContains(self.action("reject_selected"), "Rejected: 1")
        self.incoming.refresh_from_db()
        self.assertEqual(self.incoming.status, "rejected")
        self.assertEqual(self.incoming.approved_text, "")
        self.assertIsNone(self.incoming.delivery_key)

    def test_bulk_action_rechecks_every_selected_state(self):
        pending = SupportRequest.objects.create(
            event_id="evt_admin_pending", ticket_id="T-PENDING", customer_email="pending@example.com",
            message="Question", reply_url="https://helpdesk.example.com/tickets/T-PENDING/replies",
        )
        self.client.force_login(self.operator)
        response = self.action("approve_selected", ids=[self.incoming.pk, pending.pk])
        self.assertContains(response, "Approved: 1. Skipped (state changed or invalid draft): 1.")
        pending.refresh_from_db()
        self.assertEqual(pending.status, "pending")
        self.assertIsNone(pending.delivery_key)

    def test_invalid_review_draft_is_skipped_by_bulk_approve(self):
        self.incoming.draft_sources = [{"id": self.article.pk, "title": "Forged", "url": "https://evil.example"}]
        self.incoming.save()
        self.client.force_login(self.operator)
        self.assertContains(self.action("approve_selected"), "Approved: 0")
        self.incoming.refresh_from_db()
        self.assertEqual(self.incoming.status, "review")
        self.assertIsNone(self.incoming.delivery_key)

    def test_detail_post_cannot_edit_state_technical_fields_or_draft(self):
        for user in (self.operator, self.change_only):
            with self.subTest(user=user.username):
                self.client.force_login(user)
                response = self.client.post(self.detail_url, {
                    "status": "approved", "draft_text": "Attacker edit", "attempt_count": 999,
                    "delivery_key": "f4f55ced-1687-48f0-a7a4-65d825076fdf", "_save": "Save",
                })
                self.assertEqual(response.status_code, 403)
                self.incoming.refresh_from_db()
                self.assertEqual(self.incoming.status, "review")
                self.assertNotEqual(self.incoming.draft_text, "Attacker edit")
                self.assertEqual(self.incoming.attempt_count, 0)
                self.assertIsNone(self.incoming.delivery_key)

    def test_csrf_is_required_for_moderation_post(self):
        secure_client = Client(enforce_csrf_checks=True)
        secure_client.force_login(self.operator)
        self.assertEqual(self.action("approve_selected", client=secure_client, follow=False).status_code, 403)
        secure_client.get(self.list_url)
        token = secure_client.cookies["csrftoken"].value
        response = secure_client.post(self.list_url, {
            "action": "approve_selected", "_selected_action": [self.incoming.pk],
            "index": "0", "csrfmiddlewaretoken": token,
        })
        self.assertEqual(response.status_code, 302)
        self.incoming.refresh_from_db()
        self.assertEqual(self.incoming.status, "approved")

    def test_source_links_never_use_forged_urls_or_unescaped_titles(self):
        self.client.force_login(self.viewer)
        original = article_source(self.article)
        for url in ("javascript:alert(1)", "https://evil.example/fake", "/invented-guide"):
            with self.subTest(url=url):
                self.incoming.draft_sources = [{**original, "url": url}]
                self.incoming.save()
                page = self.client.get(self.detail_url)
                self.assertNotContains(page, '<a href="' + url + '"')
                self.assertContains(page, "Some stored sources could not be verified.")
        self.article.title = '<img src=x onerror="alert(1)">'
        self.article.save()
        self.incoming.draft_sources = [article_source(self.article)]
        self.incoming.save()
        page = self.client.get(self.detail_url)
        self.assertContains(page, "&lt;img")
        self.assertNotContains(page, '<img src=x onerror="alert(1)">')

    def test_missing_or_malformed_sources_are_not_links(self):
        model_admin = admin.site._registry[SupportRequest]
        for sources in ([], {}, [None], [{"id": True}], [{"id": 999999}], ["https://evil.example"]):
            with self.subTest(sources=sources):
                self.incoming.draft_sources = sources
                self.assertNotIn("<a ", model_admin.source_links(self.incoming))
