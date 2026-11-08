import json
import os
import signal
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import skipUnless
from unittest.mock import patch
from urllib.request import Request, urlopen
from uuid import uuid4

from django.contrib.auth import get_user_model
from django.contrib.auth.models import AnonymousUser, Permission
from django.core.exceptions import PermissionDenied
from django.db import DatabaseError, connection, connections
from django.test import Client, TransactionTestCase, override_settings
from django.utils import timezone
from kombu.exceptions import OperationalError as BrokerError

from support.delivery import (
    PermanentDeliveryError,
    TransientDeliveryError,
    claim_delivery,
    fail_delivery,
    finish_delivery,
    reconcile_delivery,
    reserve_delivery,
    retry_failed_delivery,
    run_delivery,
)
from support.knowledge import article_source
from support.models import KnowledgeArticle, MockReply, SupportRequest
from support.moderation import InvalidModerationState, approve_request, reject_request


@override_settings(
    GENERATION_ENABLED=False,
    DELIVERY_ENABLED=True,
    MOCK_HELPDESK_FAULTS_ENABLED=False,
    DELIVERY_MAX_ATTEMPTS=3,
    DELIVERY_MAX_DISPATCHES=10,
    DELIVERY_MAX_INFLIGHT=2,
    DELIVERY_QUEUE_LEASE_SECONDS=60,
    DELIVERY_LEASE_SECONDS=120,
    DELIVERY_RETRY_BASE_SECONDS=5,
    DELIVERY_RETRY_MAX_SECONDS=60,
)
class DeliveryProcessingTests(TransactionTestCase):
    """Delivery state and fencing against real PostgreSQL, not an eager task mock."""

    def setUp(self):
        self.assertEqual(connection.vendor, "postgresql")
        self.operator = get_user_model().objects.create_user(
            username="delivery-operator", is_staff=True,
        )
        self.operator.user_permissions.add(*Permission.objects.filter(
            content_type__app_label="support",
            codename__in=["view_supportrequest", "moderate_supportrequest"],
        ))
        self.article = KnowledgeArticle.objects.create(
            slug="delivery-pdf", title="PDF export", body="Choose Export then PDF.",
            keywords=["pdf", "export"],
        )

    def create_review(self):
        source = article_source(self.article)
        return SupportRequest.objects.create(
            event_id="evt_delivery_" + uuid4().hex,
            ticket_id="T-delivery", customer_email="customer@example.com",
            message="How do I export PDF?",
            reply_url="https://helpdesk.example.com/tickets/T-delivery/replies",
            status="review", draft_text="Choose Export then PDF.\n" + source["url"],
            draft_sources=[source], attempt_count=1, dispatch_count=1,
        )

    def create_approved(self):
        return approve_request(self.create_review().pk, actor=self.operator)

    def reserve_one(self, incoming):
        [(request_id, token)] = reserve_delivery()
        self.assertEqual(request_id, incoming.pk)
        incoming.refresh_from_db()
        self.assertEqual(incoming.status, "delivery_queued")
        self.assertEqual(str(incoming.delivery_token), token)
        return token

    def make_due(self, incoming):
        SupportRequest.objects.filter(pk=incoming.pk).update(
            delivery_next_attempt_at=timezone.now() - timedelta(seconds=1),
        )

    def expire_lease(self, incoming):
        SupportRequest.objects.filter(pk=incoming.pk).update(
            delivery_lease_expires_at=timezone.now() - timedelta(seconds=1),
        )

    def test_approve_persists_delivery_intent_without_publishing(self):
        incoming = self.create_review()
        with patch("support.delivery.reconcile_delivery") as reconcile:
            approved = approve_request(incoming.pk, actor=self.operator)
        reconcile.assert_not_called()
        approved.refresh_from_db()
        self.assertEqual(approved.status, "approved")
        self.assertEqual(approved.approved_text, approved.draft_text)
        self.assertIsNotNone(approved.delivery_key)
        self.assertEqual(approved.delivery_attempt_count, 0)
        self.assertEqual(approved.delivery_dispatch_count, 0)
        self.assertIsNone(approved.sent_at)

    def test_rejected_request_is_never_reserved_or_sent(self):
        incoming = reject_request(self.create_review().pk, actor=self.operator)
        calls = []
        self.assertEqual(reconcile_delivery(publish=lambda *args: calls.append(args)), 0)
        self.assertEqual(run_delivery(incoming.pk, str(uuid4()), sender=calls.append), "ignored")
        self.assertEqual(calls, [])
        incoming.refresh_from_db()
        self.assertEqual(incoming.status, "rejected")
        self.assertIsNone(incoming.delivery_key)

    def test_delivery_works_with_generation_disabled_and_uses_frozen_text(self):
        incoming = self.create_approved()
        original_text, original_key = incoming.approved_text, incoming.delivery_key
        SupportRequest.objects.filter(pk=incoming.pk).update(draft_text="Later draft change")
        calls = []

        def sender(row):
            self.assertFalse(connection.in_atomic_block)
            calls.append((row.ticket_id, row.approved_text, row.delivery_key))

        publications = []
        self.assertEqual(reconcile_delivery(publish=lambda *args: publications.append(args)), 1)
        [(request_id, token)] = publications
        self.assertEqual(run_delivery(request_id, token, sender=sender), "sent")
        incoming.refresh_from_db()
        self.assertEqual(calls, [(incoming.ticket_id, original_text, original_key)])
        self.assertEqual(incoming.status, "sent")
        self.assertIsNotNone(incoming.sent_at)
        self.assertIsNotNone(incoming.delivery_last_attempt_at)
        self.assertEqual(incoming.delivery_attempt_count, 1)
        self.assertEqual(incoming.delivery_dispatch_count, 1)
        self.assertEqual((incoming.attempt_count, incoming.dispatch_count), (1, 1))
        self.assertIsNone(incoming.delivery_token)
        self.assertIsNone(incoming.delivery_lease_expires_at)

    def test_repeated_approve_and_task_execution_do_not_create_new_delivery(self):
        incoming = self.create_approved()
        original_key = incoming.delivery_key
        with self.assertRaises(InvalidModerationState):
            approve_request(incoming.pk, actor=self.operator)
        token = self.reserve_one(incoming)
        calls = []
        self.assertEqual(run_delivery(incoming.pk, token, sender=calls.append), "sent")
        self.assertEqual(run_delivery(incoming.pk, token, sender=calls.append), "ignored")
        self.assertEqual(len(calls), 1)
        incoming.refresh_from_db()
        self.assertEqual(incoming.delivery_key, original_key)
        self.assertEqual(incoming.delivery_attempt_count, 1)
        self.assertEqual(reserve_delivery(), [])

    def test_real_celery_task_handler_ignores_duplicate_delivery(self):
        from support.tasks import deliver_reply

        incoming = self.create_approved()
        token = self.reserve_one(incoming)
        with patch("support.delivery.deliver_reply", return_value=None) as sender:
            self.assertEqual(deliver_reply.run(incoming.pk, token), "sent")
            self.assertEqual(deliver_reply.run(incoming.pk, token), "ignored")
        sender.assert_called_once()
        incoming.refresh_from_db()
        self.assertEqual(incoming.delivery_attempt_count, 1)

    def test_claim_rotates_only_delivery_token_and_counts_one_attempt(self):
        incoming = self.create_approved()
        dispatch_token = self.reserve_one(incoming)
        claimed = claim_delivery(incoming.pk, dispatch_token)
        self.assertEqual(claimed.status, "sending")
        self.assertNotEqual(str(claimed.delivery_token), dispatch_token)
        self.assertEqual(claimed.delivery_attempt_count, 1)
        self.assertGreater(claimed.delivery_lease_expires_at, timezone.now())
        self.assertIsNotNone(claimed.delivery_last_attempt_at)
        self.assertIsNone(claimed.processing_token)
        self.assertIsNone(claimed.lease_expires_at)
        self.assertIsNone(claim_delivery(incoming.pk, dispatch_token))

    def test_repeated_reconciliation_does_not_flood_live_reservations(self):
        incoming = self.create_approved()
        published = []
        for _ in range(10):
            reconcile_delivery(publish=lambda *args: published.append(args))
        self.assertEqual(len(published), 1)
        incoming.refresh_from_db()
        self.assertEqual(incoming.delivery_dispatch_count, 1)

    def test_delivery_inflight_limit_keeps_backlog_in_postgresql(self):
        for _ in range(8):
            self.create_approved()
        self.assertEqual(len(reserve_delivery()), 2)
        self.assertEqual(reserve_delivery(), [])
        self.assertEqual(SupportRequest.objects.filter(status="approved").count(), 6)
        self.assertEqual(SupportRequest.objects.filter(status="delivery_queued").count(), 2)

    def test_broker_unavailable_before_reservation_does_not_spend_budget(self):
        incoming = self.create_approved()
        with patch("support.delivery.current_app.connection_for_write") as connection_for_write:
            broker = connection_for_write.return_value.__enter__.return_value
            broker.ensure_connection.side_effect = BrokerError("Redis unavailable")
            with self.assertRaises(BrokerError):
                reconcile_delivery()
        incoming.refresh_from_db()
        self.assertEqual(incoming.status, "approved")
        self.assertEqual(incoming.delivery_dispatch_count, 0)
        self.assertEqual(incoming.delivery_attempt_count, 0)
        self.assertIsNone(incoming.delivery_token)

    def test_publication_failure_is_persisted_with_backoff(self):
        incoming = self.create_approved()

        def failed_publish(request_id, token):
            self.assertFalse(connection.in_atomic_block)
            raise BrokerError("Controlled publication failure")

        reconcile_delivery(publish=failed_publish)
        incoming.refresh_from_db()
        self.assertEqual(incoming.status, "approved")
        self.assertEqual(incoming.delivery_dispatch_count, 1)
        self.assertEqual(incoming.delivery_attempt_count, 0)
        self.assertIn("Controlled publication failure", incoming.delivery_last_error)
        self.assertGreater(incoming.delivery_next_attempt_at, timezone.now())
        self.assertIsNone(incoming.delivery_token)

    def test_ambiguous_publication_failure_cannot_reset_already_sent_request(self):
        incoming = self.create_approved()

        def delivered_then_error(request_id, token):
            self.assertEqual(run_delivery(request_id, token, sender=lambda row: None), "sent")
            raise BrokerError("Publication confirmation was lost")

        reconcile_delivery(publish=delivered_then_error)
        incoming.refresh_from_db()
        self.assertEqual(incoming.status, "sent")
        self.assertEqual(incoming.delivery_last_error, "")
        self.assertIsNotNone(incoming.sent_at)

    def test_lost_message_is_recovered_with_new_token_and_same_idempotency_key(self):
        incoming = self.create_approved()
        key = incoming.delivery_key
        old_token = self.reserve_one(incoming)
        self.expire_lease(incoming)
        self.assertEqual(reserve_delivery(), [])
        incoming.refresh_from_db()
        self.assertEqual(incoming.status, "approved")
        self.assertTrue(incoming.delivery_last_error)
        self.make_due(incoming)
        new_token = self.reserve_one(incoming)
        self.assertNotEqual(new_token, old_token)
        calls = []
        self.assertEqual(run_delivery(incoming.pk, old_token, sender=calls.append), "ignored")
        self.assertEqual(run_delivery(incoming.pk, new_token, sender=calls.append), "sent")
        self.assertEqual(len(calls), 1)
        incoming.refresh_from_db()
        self.assertEqual(incoming.delivery_key, key)

    def test_expired_result_and_error_are_stale_even_before_reconciliation(self):
        incoming = self.create_approved()
        claimed = claim_delivery(incoming.pk, self.reserve_one(incoming))
        self.expire_lease(incoming)
        self.assertFalse(finish_delivery(incoming.pk, claimed.delivery_token))
        self.assertEqual(fail_delivery(incoming.pk, claimed.delivery_token, RuntimeError("Old error")), "stale")
        incoming.refresh_from_db()
        self.assertEqual(incoming.status, "sending")
        self.assertIsNone(incoming.sent_at)

    def test_old_worker_cannot_overwrite_a_new_successful_delivery(self):
        incoming = self.create_approved()
        first = claim_delivery(incoming.pk, self.reserve_one(incoming))
        self.expire_lease(incoming)
        reserve_delivery()
        self.make_due(incoming)
        second = claim_delivery(incoming.pk, self.reserve_one(incoming))
        self.assertTrue(finish_delivery(incoming.pk, second.delivery_token))
        self.assertFalse(finish_delivery(incoming.pk, first.delivery_token))
        self.assertEqual(fail_delivery(incoming.pk, first.delivery_token, RuntimeError("Stale error")), "stale")
        incoming.refresh_from_db()
        self.assertEqual(incoming.status, "sent")
        self.assertEqual(incoming.delivery_last_error, "")
        self.assertEqual(incoming.delivery_attempt_count, 2)

    def test_temporary_failures_back_off_and_stop_after_three_delivery_attempts(self):
        incoming = self.create_approved()
        key = incoming.delivery_key
        delays = []

        def failing_sender(row):
            raise TransientDeliveryError("Controlled HTTP 503")

        for attempt in range(1, 4):
            self.make_due(incoming)
            token = self.reserve_one(incoming)
            before = timezone.now()
            result = run_delivery(incoming.pk, token, sender=failing_sender)
            incoming.refresh_from_db()
            self.assertEqual(incoming.delivery_attempt_count, attempt)
            self.assertEqual(incoming.delivery_key, key)
            self.assertIn("Controlled HTTP 503", incoming.delivery_last_error)
            self.assertIsNone(incoming.sent_at)
            self.assertEqual(result, "approved" if attempt < 3 else "delivery_failed")
            if attempt < 3:
                delays.append((incoming.delivery_next_attempt_at - before).total_seconds())
                self.assertEqual(reserve_delivery(), [])
        self.assertGreater(delays[1], delays[0])
        self.make_due(incoming)
        self.assertEqual(reserve_delivery(), [])
        self.assertEqual((incoming.attempt_count, incoming.dispatch_count), (1, 1))

    def test_retry_after_is_persisted_instead_of_waiting_inside_worker(self):
        incoming = self.create_approved()

        def throttled(row):
            raise TransientDeliveryError("HTTP 429", retry_after=120)

        before = timezone.now()
        self.assertEqual(run_delivery(incoming.pk, self.reserve_one(incoming), sender=throttled), "approved")
        incoming.refresh_from_db()
        self.assertGreaterEqual(incoming.delivery_next_attempt_at, before + timedelta(seconds=120))

    def test_permanent_error_stops_without_repeating_4xx(self):
        incoming = self.create_approved()

        def rejected(row):
            raise PermanentDeliveryError("HTTP 409 conflicting payload")

        self.assertEqual(run_delivery(incoming.pk, self.reserve_one(incoming), sender=rejected), "delivery_failed")
        incoming.refresh_from_db()
        self.assertEqual(incoming.delivery_attempt_count, 1)
        self.assertIn("HTTP 409", incoming.delivery_last_error)
        self.assertEqual(reserve_delivery(), [])

    @override_settings(DELIVERY_MAX_DISPATCHES=2)
    def test_lost_messages_have_a_finite_dispatch_budget(self):
        incoming = self.create_approved()
        for dispatch in range(1, 3):
            self.make_due(incoming)
            self.reserve_one(incoming)
            self.expire_lease(incoming)
            reserve_delivery()
            incoming.refresh_from_db()
            self.assertEqual(incoming.delivery_dispatch_count, dispatch)
        self.assertEqual(incoming.status, "delivery_failed")
        self.assertEqual(incoming.delivery_attempt_count, 0)
        self.assertTrue(incoming.delivery_last_error)

    def test_manual_retry_requires_permission_and_preserves_key_and_snapshot(self):
        incoming = self.create_approved()
        original_key, original_text = incoming.delivery_key, incoming.approved_text
        SupportRequest.objects.filter(pk=incoming.pk).update(
            status="delivery_failed", delivery_attempt_count=3,
            delivery_dispatch_count=10, delivery_last_error="Exhausted attempts",
        )
        with self.assertRaises(PermissionDenied):
            retry_failed_delivery(incoming.pk, actor=AnonymousUser())
        self.assertTrue(retry_failed_delivery(incoming.pk, actor=self.operator))
        self.assertFalse(retry_failed_delivery(incoming.pk, actor=self.operator))
        incoming.refresh_from_db()
        self.assertEqual(incoming.status, "approved")
        self.assertEqual(incoming.delivery_attempt_count, 0)
        self.assertEqual(incoming.delivery_dispatch_count, 0)
        self.assertEqual(incoming.delivery_key, original_key)
        self.assertEqual(incoming.approved_text, original_text)
        self.assertEqual(incoming.delivery_last_error, "")
        self.assertEqual(run_delivery(incoming.pk, self.reserve_one(incoming), sender=lambda row: None), "sent")

    def test_database_claim_failure_does_not_attempt_http(self):
        incoming = self.create_approved()
        token = self.reserve_one(incoming)
        calls = []
        with patch("support.delivery.claim_delivery", side_effect=DatabaseError("Unavailable")):
            with self.assertRaises(DatabaseError):
                run_delivery(incoming.pk, token, sender=calls.append)
        self.assertEqual(calls, [])

    def test_uncertain_database_completion_keeps_sending_lease_for_recovery(self):
        incoming = self.create_approved()
        token = self.reserve_one(incoming)
        calls = []
        with patch("support.delivery.finish_delivery", side_effect=DatabaseError("Commit unavailable")):
            with self.assertRaises(DatabaseError):
                run_delivery(incoming.pk, token, sender=calls.append)
        self.assertEqual(len(calls), 1)
        incoming.refresh_from_db()
        self.assertEqual(incoming.status, "sending")
        self.assertIsNotNone(incoming.delivery_token)
        self.assertIsNotNone(incoming.delivery_lease_expires_at)
        self.assertIsNone(incoming.sent_at)

    def test_two_workers_use_independent_connections_and_only_one_sends(self):
        incoming = self.create_approved()
        token = self.reserve_one(incoming)
        connection.close()
        barrier = threading.Barrier(2)
        entered = threading.Event()
        release = threading.Event()
        calls = []

        def controlled_sender(row):
            self.assertFalse(connections["default"].in_atomic_block)
            calls.append((row.pk, row.delivery_key))
            entered.set()
            if not release.wait(timeout=10):
                raise TimeoutError("Controlled sender was not released")

        def worker():
            database = connections["default"]
            try:
                with database.cursor() as cursor:
                    cursor.execute("SELECT pg_backend_pid()")
                    pid = cursor.fetchone()[0]
                barrier.wait(timeout=10)
                return pid, run_delivery(incoming.pk, token, sender=controlled_sender)
            finally:
                database.close()

        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(worker) for _ in range(2)]
            try:
                self.assertTrue(entered.wait(timeout=5))
                incoming.refresh_from_db()
                self.assertEqual(incoming.status, "sending")
                self.assertEqual(incoming.delivery_attempt_count, 1)
            finally:
                release.set()
            results = [future.result(timeout=15) for future in futures]
        self.assertEqual(len({pid for pid, _ in results}), 2)
        self.assertEqual(sorted(result for _, result in results), ["ignored", "sent"])
        self.assertEqual(calls, [(incoming.pk, incoming.delivery_key)])

    def test_concurrent_reconcilers_cannot_exceed_delivery_inflight_limit(self):
        for _ in range(8):
            self.create_approved()
        connection.close()
        barrier = threading.Barrier(2)

        def planner():
            database = connections["default"]
            try:
                with database.cursor() as cursor:
                    cursor.execute("SELECT pg_backend_pid()")
                    pid = cursor.fetchone()[0]
                barrier.wait(timeout=10)
                return pid, reserve_delivery()
            finally:
                database.close()

        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(planner) for _ in range(2)]
            results = [future.result(timeout=15) for future in futures]
        self.assertEqual(len({pid for pid, _ in results}), 2)
        reservations = [pair for _, batch in results for pair in batch]
        self.assertEqual(len(reservations), 2)
        self.assertEqual(len({pair[0] for pair in reservations}), 2)

    @skipUnless(hasattr(signal, "SIGKILL"), "Real process-kill recovery runs in Linux Docker")
    def test_worker_killed_during_http_after_mock_commit_is_recovered_without_duplicate(self):
        """A real HTTP server commits through the mock view before losing its client."""
        incoming = self.create_approved()
        token = self.reserve_one(incoming)
        committed = threading.Event()
        release = threading.Event()
        attempts = []
        errors = []

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                try:
                    response = Client().post(
                        "/tickets/T-delivery/replies", body,
                        content_type="application/json",
                        HTTP_IDEMPOTENCY_KEY=self.headers["Idempotency-Key"],
                    )
                    status, response_body = response.status_code, response.content
                    attempts.append(status)
                except Exception as error:
                    errors.append(repr(error))
                    status, response_body = 500, b"{}"
                finally:
                    connections.close_all()
                if len(attempts) == 1:
                    committed.set()
                    release.wait(timeout=10)
                try:
                    self.send_response(status)
                    self.send_header("Content-Length", str(len(response_body)))
                    self.end_headers()
                    self.wfile.write(response_body)
                except (BrokenPipeError, ConnectionResetError):
                    pass

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server.daemon_threads = True
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        endpoint = f"http://127.0.0.1:{server.server_port}/tickets/T-delivery/replies"
        script = """
import json, sys, django
from urllib.request import Request, urlopen
django.setup()
from django.db import connection
from support.delivery import run_delivery
def sender(row):
    assert not connection.in_atomic_block
    request = Request(sys.argv[3], data=json.dumps({'ticket_id': row.ticket_id, 'message': row.approved_text}).encode(), headers={'Content-Type': 'application/json', 'Idempotency-Key': str(row.delivery_key)}, method='POST')
    with urlopen(request, timeout=30) as response:
        assert response.status == 201
run_delivery(int(sys.argv[1]), sys.argv[2], sender=sender)
"""
        environment = os.environ.copy()
        environment["DJANGO_SETTINGS_MODULE"] = "config.settings"
        environment["POSTGRES_DB"] = connection.settings_dict["NAME"]
        child = None
        try:
            child = subprocess.Popen(
                [sys.executable, "-c", script, str(incoming.pk), token, endpoint],
                env=environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            )
            self.assertTrue(committed.wait(timeout=10), "Sender never reached committed mock reply")
            self.assertEqual(errors, [])
            self.assertEqual(attempts, [201])
            self.assertEqual(MockReply.objects.filter(idempotency_key=str(incoming.delivery_key)).count(), 1)
            incoming.refresh_from_db()
            self.assertEqual(incoming.status, "sending")
            child.kill()
            child.communicate(timeout=10)
            self.assertEqual(child.returncode, -signal.SIGKILL)
            release.set()

            self.expire_lease(incoming)
            self.assertEqual(reserve_delivery(), [])
            incoming.refresh_from_db()
            self.assertEqual(incoming.status, "approved")
            self.make_due(incoming)
            new_token = self.reserve_one(incoming)

            def actual_http_sender(row):
                self.assertFalse(connection.in_atomic_block)
                request = Request(endpoint, data=json.dumps({
                    "ticket_id": row.ticket_id, "message": row.approved_text,
                }).encode(), headers={
                    "Content-Type": "application/json", "Idempotency-Key": str(row.delivery_key),
                }, method="POST")
                with urlopen(request, timeout=5) as response:
                    self.assertEqual(response.status, 201)

            self.assertEqual(run_delivery(incoming.pk, new_token, sender=actual_http_sender), "sent")
            incoming.refresh_from_db()
            self.assertEqual(incoming.delivery_attempt_count, 2)
            self.assertEqual(MockReply.objects.filter(idempotency_key=str(incoming.delivery_key)).count(), 1)
            self.assertEqual(attempts, [201, 201])
        finally:
            release.set()
            if child is not None and child.poll() is None:
                child.kill()
                child.communicate(timeout=10)
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)


class IndependentDeliverySchedulerTests(TransactionTestCase):
    @override_settings(GENERATION_ENABLED=False)
    def test_generation_failure_does_not_prevent_delivery_reconciliation(self):
        from support.scheduling import DatabaseReconciliationScheduler

        scheduler = object.__new__(DatabaseReconciliationScheduler)
        with patch("support.scheduling.reconcile_generation", side_effect=DatabaseError("Generation failure")), patch(
            "support.scheduling.reconcile_delivery", return_value=1,
        ) as delivery:
            with self.assertLogs("support.scheduling", level="ERROR"):
                interval = scheduler.tick()
        delivery.assert_called_once_with()
        self.assertGreater(interval, 0)

    def test_delivery_failure_does_not_prevent_generation_reconciliation(self):
        from support.scheduling import DatabaseReconciliationScheduler

        scheduler = object.__new__(DatabaseReconciliationScheduler)
        with patch("support.scheduling.reconcile_generation", return_value=1) as generation, patch(
            "support.scheduling.reconcile_delivery", side_effect=DatabaseError("Delivery failure"),
        ):
            with self.assertLogs("support.scheduling", level="ERROR"):
                interval = scheduler.tick()
        generation.assert_called_once_with()
        self.assertGreater(interval, 0)
