import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from unittest.mock import patch
from uuid import UUID, uuid4

from django.contrib.auth import get_user_model
from django.contrib.auth.models import AnonymousUser, Permission
from django.core.exceptions import PermissionDenied
from django.db import DatabaseError, IntegrityError, connection, connections, transaction
from django.test import TransactionTestCase, override_settings
from django.utils import timezone

from support.knowledge import article_source
from support.models import KnowledgeArticle, SupportRequest
from support.moderation import (
    InvalidDraft,
    InvalidModerationState,
    approve_request,
    reject_request,
)
from support.processing import reserve_generation, retry_failed_generation


class ModerationServiceTests(TransactionTestCase):
    """Persist real moderation decisions and exercise races against PostgreSQL."""

    def setUp(self):
        self.assertEqual(connection.vendor, "postgresql")
        self.operator = get_user_model().objects.create_user(
            username="moderator", is_staff=True,
        )
        self.operator.user_permissions.add(*Permission.objects.filter(
            content_type__app_label="support",
            codename__in=["view_supportrequest", "moderate_supportrequest"],
        ))
        self.assertTrue(self.operator.has_perm("support.moderate_supportrequest"))
        self.article = KnowledgeArticle.objects.create(
            slug="moderation-pdf", title="Export a PDF report",
            body="Open the report and choose Export then PDF.", keywords=["pdf", "export"],
        )

    def create_review(self, **changes):
        source = article_source(self.article)
        values = {
            "event_id": "evt_moderation_" + uuid4().hex,
            "ticket_id": "T-moderation",
            "customer_email": "customer@example.com",
            "message": "How do I export a PDF report?",
            "reply_url": "https://helpdesk.example.com/tickets/T-moderation/replies",
            "status": "review",
            "draft_text": "Choose Export then PDF.\n\nSource: " + source["url"],
            "draft_sources": [source],
            "attempt_count": 1,
            "dispatch_count": 1,
        }
        return SupportRequest.objects.create(**{**values, **changes})

    def snapshot(self, incoming):
        incoming.refresh_from_db()
        return (
            incoming.status, incoming.approved_text, incoming.delivery_key,
            incoming.moderated_at, incoming.moderated_by_id,
        )

    def test_approve_atomically_freezes_reply_and_records_operator_and_stable_key(self):
        incoming = self.create_review()
        before = timezone.now()

        result = approve_request(incoming.pk, actor=self.operator)

        incoming.refresh_from_db()
        self.assertEqual(result.pk, incoming.pk)
        self.assertEqual(incoming.status, "approved")
        self.assertEqual(incoming.approved_text, incoming.draft_text)
        self.assertIsInstance(incoming.delivery_key, UUID)
        self.assertEqual(incoming.moderated_by_id, self.operator.pk)
        self.assertGreaterEqual(incoming.moderated_at, before)
        self.assertIsNone(incoming.processing_token)
        self.assertIsNone(incoming.lease_expires_at)
        self.assertEqual(incoming.attempt_count, 1)
        self.assertEqual(incoming.dispatch_count, 1)

    def test_approve_does_not_call_provider_publish_celery_or_send_http(self):
        incoming = self.create_review()
        with patch("support.tasks.generate_reply.apply_async") as publish, patch(
            "support.llm_http.post_completion"
        ) as provider, patch("urllib.request.OpenerDirector.open") as http:
            approve_request(incoming.pk, actor=self.operator)
        publish.assert_not_called()
        provider.assert_not_called()
        http.assert_not_called()

    def test_later_draft_edits_do_not_change_approved_snapshot_or_delivery_key(self):
        incoming = self.create_review()
        approved = approve_request(incoming.pk, actor=self.operator)
        original_text, original_key = approved.approved_text, approved.delivery_key

        SupportRequest.objects.filter(pk=incoming.pk).update(draft_text="A later draft edit")

        incoming.refresh_from_db()
        self.assertEqual(incoming.draft_text, "A later draft edit")
        self.assertEqual(incoming.approved_text, original_text)
        self.assertEqual(incoming.delivery_key, original_key)

    def test_repeated_approve_is_rejected_and_cannot_create_another_delivery_intent(self):
        incoming = self.create_review()
        approve_request(incoming.pk, actor=self.operator)
        original = self.snapshot(incoming)

        with self.assertRaises(InvalidModerationState):
            approve_request(incoming.pk, actor=self.operator)

        self.assertEqual(self.snapshot(incoming), original)
        self.assertEqual(SupportRequest.objects.filter(delivery_key__isnull=False).count(), 1)

    def test_resetting_status_through_orm_cannot_replace_an_existing_decision(self):
        for decide in (approve_request, reject_request):
            with self.subTest(original_decision=decide.__name__):
                incoming = self.create_review()
                decide(incoming.pk, actor=self.operator)
                # Simulate an unsafe maintenance write. The persisted decision
                # must still prevent both actions from creating/replacing intent.
                SupportRequest.objects.filter(pk=incoming.pk).update(status="review")
                original = self.snapshot(incoming)
                for action in (approve_request, reject_request):
                    with self.assertRaises(InvalidModerationState):
                        action(incoming.pk, actor=self.operator)
                    self.assertEqual(self.snapshot(incoming), original)

    def test_postgresql_rejects_incomplete_decisions_and_rejected_delivery_intents(self):
        now = timezone.now()
        invalid_updates = [
            {"approved_text": "Frozen text without a delivery key"},
            {"delivery_key": uuid4()},
            {"status": "approved"},
            {
                "status": "approved", "approved_text": "Frozen text",
                "delivery_key": uuid4(), "moderated_at": now,
            },
            {
                "status": "approved", "approved_text": "Frozen text",
                "delivery_key": uuid4(), "moderated_by_id": self.operator.pk,
            },
            {"status": "rejected"},
            {
                "status": "rejected", "approved_text": "Must never be sent",
                "delivery_key": uuid4(), "moderated_at": now,
                "moderated_by_id": self.operator.pk,
            },
        ]
        for updates in invalid_updates:
            with self.subTest(updates=updates):
                incoming = self.create_review()
                original = self.snapshot(incoming)
                with self.assertRaises(IntegrityError):
                    with transaction.atomic():
                        SupportRequest.objects.filter(pk=incoming.pk).update(**updates)
                self.assertEqual(self.snapshot(incoming), original)

    def test_separate_approved_requests_receive_distinct_delivery_keys(self):
        first = approve_request(self.create_review().pk, actor=self.operator)
        second = approve_request(self.create_review().pk, actor=self.operator)
        self.assertNotEqual(first.delivery_key, second.delivery_key)

    def test_reject_records_decision_without_delivery_intent(self):
        incoming = self.create_review()
        original_draft = incoming.draft_text

        result = reject_request(incoming.pk, actor=self.operator)

        incoming.refresh_from_db()
        self.assertEqual(result.pk, incoming.pk)
        self.assertEqual(incoming.status, "rejected")
        self.assertEqual(incoming.draft_text, original_draft)
        self.assertEqual(incoming.approved_text, "")
        self.assertIsNone(incoming.delivery_key)
        self.assertEqual(incoming.moderated_by_id, self.operator.pk)
        self.assertIsNotNone(incoming.moderated_at)

    def test_invalid_draft_can_be_rejected_without_approving_its_content(self):
        incoming = self.create_review(draft_text="", draft_sources=[])
        reject_request(incoming.pk, actor=self.operator)
        incoming.refresh_from_db()
        self.assertEqual(incoming.status, "rejected")
        self.assertEqual(incoming.approved_text, "")
        self.assertIsNone(incoming.delivery_key)

    def test_only_review_accepts_moderation_and_existing_decisions_are_immutable(self):
        for status in ("pending", "queued", "generating", "failed", "approved", "rejected"):
            with self.subTest(status=status):
                incoming = self.create_review()
                if status == "approved":
                    approve_request(incoming.pk, actor=self.operator)
                elif status == "rejected":
                    reject_request(incoming.pk, actor=self.operator)
                else:
                    updates = {"status": status}
                    if status in {"queued", "generating"}:
                        updates.update(
                            processing_token=uuid4(),
                            lease_expires_at=timezone.now() + timedelta(seconds=120),
                        )
                    SupportRequest.objects.filter(pk=incoming.pk).update(**updates)
                original = self.snapshot(incoming)
                for action in (approve_request, reject_request):
                    with self.subTest(action=action.__name__):
                        with self.assertRaises(InvalidModerationState):
                            action(incoming.pk, actor=self.operator)
                        self.assertEqual(self.snapshot(incoming), original)

    def test_empty_or_unverified_draft_cannot_be_approved(self):
        canonical = article_source(self.article)
        variants = [
            {"draft_text": ""},
            {"draft_text": "   "},
            {"draft_sources": []},
            {"draft_sources": [{**canonical, "url": "https://evil.example/fake"}]},
            {"draft_text": "Read https://evil.example/fake"},
        ]
        for changes in variants:
            with self.subTest(changes=changes):
                incoming = self.create_review(**changes)
                with self.assertRaises(InvalidDraft):
                    approve_request(incoming.pk, actor=self.operator)
                incoming.refresh_from_db()
                self.assertEqual(incoming.status, "review")
                self.assertEqual(incoming.approved_text, "")
                self.assertIsNone(incoming.delivery_key)
                self.assertIsNone(incoming.moderated_at)

    def test_review_with_active_processing_ownership_cannot_be_approved(self):
        incoming = self.create_review(
            processing_token=uuid4(),
            lease_expires_at=timezone.now() + timedelta(seconds=120),
        )
        with self.assertRaises((InvalidDraft, InvalidModerationState)):
            approve_request(incoming.pk, actor=self.operator)
        incoming.refresh_from_db()
        self.assertEqual(incoming.status, "review")
        self.assertIsNone(incoming.delivery_key)

    def test_service_enforces_staff_active_and_both_operator_permissions(self):
        incoming = self.create_review()
        for actor in (AnonymousUser(), None):
            with self.subTest(actor=actor):
                for action in (approve_request, reject_request):
                    with self.assertRaises(PermissionDenied):
                        action(incoming.pk, actor=actor)
        for is_staff, is_active, permissions in (
            (False, True, ["view_supportrequest", "moderate_supportrequest"]),
            (True, False, ["view_supportrequest", "moderate_supportrequest"]),
            (True, True, ["view_supportrequest", "change_supportrequest"]),
            (True, True, ["moderate_supportrequest"]),
            (True, True, ["view_supportrequest"]),
        ):
            with self.subTest(is_staff=is_staff, is_active=is_active, permissions=permissions):
                actor = get_user_model().objects.create_user(
                    username="denied_" + uuid4().hex,
                    is_staff=is_staff, is_active=is_active,
                )
                actor.user_permissions.add(*Permission.objects.filter(
                    content_type__app_label="support", codename__in=permissions,
                ))
                for action in (approve_request, reject_request):
                    with self.assertRaises(PermissionDenied):
                        action(incoming.pk, actor=actor)
        incoming.refresh_from_db()
        self.assertEqual(incoming.status, "review")
        self.assertIsNone(incoming.delivery_key)

    def test_database_failure_rolls_back_entire_moderation_decision(self):
        incoming = self.create_review()
        original = self.snapshot(incoming)

        def fail_after_update(execute, sql, params, many, context):
            result = execute(sql, params, many, context)
            if sql.lstrip().upper().startswith("UPDATE") and '"support_supportrequest"' in sql:
                raise DatabaseError("Controlled persistence fault after PostgreSQL UPDATE")
            return result

        with connection.execute_wrapper(fail_after_update):
            with self.assertRaises(DatabaseError):
                approve_request(incoming.pk, actor=self.operator)

        self.assertEqual(self.snapshot(incoming), original)

    @override_settings(GENERATION_ENABLED=True)
    def test_rejected_or_approved_requests_are_not_returned_to_generation(self):
        rejected = reject_request(self.create_review().pk, actor=self.operator)
        approved = approve_request(self.create_review().pk, actor=self.operator)

        self.assertEqual(reserve_generation(), [])
        self.assertFalse(retry_failed_generation(rejected.pk))
        self.assertFalse(retry_failed_generation(approved.pk))
        rejected.refresh_from_db()
        approved.refresh_from_db()
        self.assertEqual(rejected.status, "rejected")
        self.assertIsNone(rejected.delivery_key)
        self.assertEqual(approved.status, "approved")

    def race_actions(self, actions):
        incoming = self.create_review()
        request_id, operator_id = incoming.pk, self.operator.pk
        connection.close()
        barrier = threading.Barrier(2)

        def worker(action):
            database = connections["default"]
            try:
                actor = get_user_model().objects.get(pk=operator_id)
                with database.cursor() as cursor:
                    cursor.execute("SELECT pg_backend_pid()")
                    backend_pid = cursor.fetchone()[0]
                barrier.wait(timeout=10)
                try:
                    result = action(request_id, actor=actor)
                except InvalidModerationState:
                    return backend_pid, "conflict", None
                return backend_pid, result.status, result.delivery_key
            finally:
                database.close()

        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(worker, action) for action in actions]
            results = [future.result(timeout=15) for future in futures]

        self.assertEqual(len({result[0] for result in results}), 2)
        self.assertEqual(sum(result[1] == "conflict" for result in results), 1)
        incoming.refresh_from_db()
        self.assertEqual(incoming.moderated_by_id, self.operator.pk)
        self.assertIsNotNone(incoming.moderated_at)
        winner = next(result for result in results if result[1] != "conflict")
        self.assertEqual(incoming.status, winner[1])
        self.assertEqual(incoming.delivery_key, winner[2])
        if incoming.status == "approved":
            self.assertEqual(incoming.approved_text, incoming.draft_text)
            self.assertIsNotNone(incoming.delivery_key)
        else:
            self.assertEqual(incoming.status, "rejected")
            self.assertEqual(incoming.approved_text, "")
            self.assertIsNone(incoming.delivery_key)
        return incoming

    def test_concurrent_approve_and_reject_leave_exactly_one_consistent_decision(self):
        incoming = self.race_actions((approve_request, reject_request))
        self.assertIn(incoming.status, {"approved", "rejected"})

    def test_concurrent_approves_create_only_one_delivery_intent(self):
        incoming = self.race_actions((approve_request, approve_request))
        self.assertEqual(incoming.status, "approved")
        self.assertEqual(SupportRequest.objects.filter(delivery_key__isnull=False).count(), 1)
