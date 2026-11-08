from uuid import uuid4

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from support.models import MockReply, SupportRequest


@override_settings(STORAGES={
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
})
class DeliveryAdminTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        users = get_user_model()
        cls.operator = users.objects.create_user(username="delivery-operator", is_staff=True)
        cls.viewer = users.objects.create_user(username="delivery-viewer", is_staff=True)
        view = Permission.objects.get(codename="view_supportrequest")
        moderate = Permission.objects.get(codename="moderate_supportrequest")
        mock_view = Permission.objects.get(codename="view_mockreply")
        cls.operator.user_permissions.add(view, moderate, mock_view)
        cls.viewer.user_permissions.add(view)

    def setUp(self):
        self.incoming = SupportRequest.objects.create(
            event_id="evt_delivery_admin", ticket_id="T-DELIVERY", customer_email="delivery@example.com",
            message="PDF export question", reply_url="https://helpdesk.example.com/tickets/T-DELIVERY/replies",
            status=SupportRequest.Status.DELIVERY_FAILED,
            draft_text="Changed draft", approved_text="Frozen approved answer", delivery_key=uuid4(),
            moderated_by=self.operator, moderated_at=timezone.now(),
            attempt_count=2, dispatch_count=3,
            delivery_attempt_count=3, delivery_dispatch_count=10,
            delivery_last_attempt_at=timezone.now(), delivery_last_error="Controlled HTTP timeout",
        )
        self.list_url = reverse("admin:support_supportrequest_changelist")
        self.detail_url = reverse("admin:support_supportrequest_change", args=[self.incoming.pk])

    def retry(self, client=None, **extra):
        return (client or self.client).post(self.list_url, {
            "action": "retry_selected_deliveries", "_selected_action": [self.incoming.pk],
            "index": "0", **extra,
        }, follow=True)

    def test_operator_sees_delivery_status_attempts_error_and_frozen_key(self):
        self.client.force_login(self.operator)
        page = self.client.get(self.detail_url)
        for text in ("Controlled HTTP timeout", "Frozen approved answer", str(self.incoming.delivery_key),
                     "Delivery attempt count", "Delivery last attempt at", "Sent at"):
            self.assertContains(page, text)
        self.assertNotContains(page, 'name="_save"')
        self.assertContains(self.client.get(self.list_url), "Retry selected failed deliveries")

    def test_manual_retry_preserves_payload_and_key_and_does_not_reset_generation(self):
        self.client.force_login(self.operator)
        key, text = self.incoming.delivery_key, self.incoming.approved_text
        response = self.retry()
        self.assertContains(response, "Delivery retries scheduled: 1")
        self.incoming.refresh_from_db()
        self.assertEqual(self.incoming.status, SupportRequest.Status.APPROVED)
        self.assertEqual(self.incoming.delivery_key, key)
        self.assertEqual(self.incoming.approved_text, text)
        self.assertEqual(self.incoming.delivery_attempt_count, 0)
        self.assertEqual(self.incoming.delivery_dispatch_count, 0)
        self.assertEqual(self.incoming.attempt_count, 2)
        self.assertEqual(self.incoming.dispatch_count, 3)
        self.assertIsNone(self.incoming.delivery_token)

    def test_repeat_retry_skips_an_already_reopened_delivery(self):
        self.client.force_login(self.operator)
        self.retry()
        self.assertContains(self.retry(), "Delivery retries scheduled: 0")
        self.incoming.refresh_from_db()
        self.assertEqual(self.incoming.status, "approved")

    def test_sent_reply_cannot_be_reopened_by_manual_retry(self):
        self.incoming.status = SupportRequest.Status.SENT
        self.incoming.sent_at = timezone.now()
        self.incoming.save()
        self.client.force_login(self.operator)
        self.assertContains(self.retry(), "Delivery retries scheduled: 0")
        self.incoming.refresh_from_db()
        self.assertEqual(self.incoming.status, "sent")
        self.assertIsNotNone(self.incoming.sent_at)

    def test_view_permission_does_not_allow_manual_retry(self):
        self.client.force_login(self.viewer)
        self.assertNotContains(self.client.get(self.list_url), "Retry selected failed deliveries")
        self.retry()
        self.incoming.refresh_from_db()
        self.assertEqual(self.incoming.status, "delivery_failed")

    def test_direct_admin_post_cannot_edit_delivery_fields(self):
        self.client.force_login(self.operator)
        response = self.client.post(self.detail_url, {
            "status": "sent", "delivery_attempt_count": 0, "delivery_last_error": "",
            "approved_text": "Replacement text", "delivery_key": str(uuid4()), "_save": "Save",
        })
        self.assertEqual(response.status_code, 403)
        self.incoming.refresh_from_db()
        self.assertEqual(self.incoming.status, "delivery_failed")
        self.assertEqual(self.incoming.delivery_attempt_count, 3)
        self.assertEqual(self.incoming.approved_text, "Frozen approved answer")

    def test_manual_retry_requires_csrf(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.operator)
        self.assertEqual(self.retry(client=client).status_code, 403)
        self.incoming.refresh_from_db()
        self.assertEqual(self.incoming.status, "delivery_failed")
        client.get(self.list_url)
        token = client.cookies["csrftoken"].value
        self.assertContains(self.retry(client=client, csrfmiddlewaretoken=token), "Delivery retries scheduled: 1")

    def test_mock_reply_admin_is_visible_but_read_only(self):
        reply = MockReply.objects.create(
            idempotency_key=str(self.incoming.delivery_key), ticket_id=self.incoming.ticket_id,
            message=self.incoming.approved_text,
        )
        self.client.force_login(self.operator)
        url = reverse("admin:support_mockreply_change", args=[reply.pk])
        page = self.client.get(url)
        self.assertContains(page, "Frozen approved answer")
        self.assertContains(page, str(self.incoming.delivery_key))
        self.assertNotContains(page, 'name="_save"')
        self.assertEqual(self.client.post(url, {"message": "replacement", "_save": "Save"}).status_code, 403)
        reply.refresh_from_db()
        self.assertEqual(reply.message, "Frozen approved answer")
