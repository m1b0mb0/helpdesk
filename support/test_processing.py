import json
import os
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from unittest.mock import patch
from uuid import uuid4

from billiard.exceptions import SoftTimeLimitExceeded
from django.db import DatabaseError, connection, connections
from django.test import SimpleTestCase, TransactionTestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from kombu.exceptions import OperationalError as BrokerError

from support.generation import Draft, PermanentGenerationError, TransientGenerationError
from support.models import SupportRequest
from support.processing import (
    claim_generation,
    fail_generation,
    finish_generation,
    reconcile_generation,
    reserve_generation,
    retry_failed_generation,
    run_generation,
)


@override_settings(
    GENERATION_ENABLED=True,
    GENERATION_MAX_ATTEMPTS=3,
    GENERATION_MAX_DISPATCHES=10,
    GENERATION_MAX_INFLIGHT=2,
    GENERATION_QUEUE_LEASE_SECONDS=60,
    GENERATION_LEASE_SECONDS=120,
    GENERATION_RETRY_BASE_SECONDS=5,
    GENERATION_RETRY_MAX_SECONDS=60,
)
class GenerationProcessingTests(TransactionTestCase):
    def setUp(self):
        super().setUp()
        if connection.vendor != "postgresql":
            self.fail("Generation reliability tests require PostgreSQL")
        self.payload = {
            "event_id": "evt_generation",
            "ticket_id": "T-generation",
            "customer_email": "user@example.com",
            "message": "How do I export a PDF report?",
            "reply_url": "https://helpdesk.example.com/tickets/T-generation/replies",
        }
        self.draft = Draft(
            text="Controlled generator output for this test only.",
            sources=[{"title": "Controlled source", "url": "https://example.com/test-source"}],
        )

    def create_request(self, **changes):
        return SupportRequest.objects.create(
            **{**self.payload, "event_id": "evt_" + uuid4().hex, **changes}
        )

    def reserve_one(self, incoming):
        reserved = reserve_generation()
        self.assertEqual(len(reserved), 1)
        request_id, token = reserved[0]
        self.assertEqual(request_id, incoming.pk)
        incoming.refresh_from_db()
        self.assertEqual(incoming.status, "queued")
        self.assertEqual(str(incoming.processing_token), str(token))
        return str(token)

    def make_due(self, incoming):
        SupportRequest.objects.filter(pk=incoming.pk).update(
            next_attempt_at=timezone.now() - timedelta(seconds=1)
        )

    def expire_lease(self, incoming):
        SupportRequest.objects.filter(pk=incoming.pk).update(
            lease_expires_at=timezone.now() - timedelta(seconds=1)
        )

    def test_saved_webhook_is_found_without_publishing_from_webhook(self):
        response = self.client.post(
            reverse("helpdesk_webhook"),
            data=json.dumps(self.payload),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 202)
        incoming = SupportRequest.objects.get(event_id=self.payload["event_id"])
        publications = []

        def publish(request_id, token):
            self.assertFalse(connection.in_atomic_block)
            publications.append((request_id, token))

        self.assertEqual(reconcile_generation(publish=publish), 1)
        self.assertEqual(len(publications), 1)
        request_id, token = publications[0]
        self.assertEqual(request_id, incoming.pk)
        self.assertEqual(
            run_generation(request_id, token, generator=lambda request: self.draft),
            "review",
        )
        incoming.refresh_from_db()
        self.assertEqual(incoming.status, "review")
        self.assertEqual(incoming.draft_text, self.draft.text)
        self.assertEqual(incoming.draft_sources, self.draft.sources)
        self.assertEqual(incoming.attempt_count, 1)
        self.assertIsNone(incoming.processing_token)
        self.assertIsNone(incoming.lease_expires_at)

    @override_settings(GENERATION_ENABLED=False)
    def test_disabled_generation_preserves_pending_events_without_spending_attempts(self):
        incoming = self.create_request()
        publications = []

        self.assertEqual(reconcile_generation(publish=lambda *args: publications.append(args)), 0)
        self.assertEqual(run_generation(incoming.pk, str(uuid4())), "disabled")

        incoming.refresh_from_db()
        self.assertEqual(incoming.status, "pending")
        self.assertEqual(incoming.attempt_count, 0)
        self.assertEqual(incoming.dispatch_count, 0)
        self.assertEqual(incoming.draft_text, "")
        self.assertEqual(publications, [])

    def test_repeated_planner_ticks_do_not_republish_a_live_reservation(self):
        incoming = self.create_request()
        publications = []
        publish = lambda *args: publications.append(args)

        self.assertEqual(reconcile_generation(publish=publish), 1)
        for _ in range(10):
            self.assertEqual(reconcile_generation(publish=publish), 0)

        incoming.refresh_from_db()
        self.assertEqual(len(publications), 1)
        self.assertEqual(incoming.dispatch_count, 1)
        self.assertEqual(incoming.attempt_count, 0)

    def test_inflight_limit_keeps_backlog_in_postgresql(self):
        for _ in range(10):
            self.create_request()

        reserved = reserve_generation()

        self.assertEqual(len(reserved), 2)
        self.assertEqual(reserve_generation(), [])
        self.assertEqual(SupportRequest.objects.filter(status="queued").count(), 2)
        self.assertEqual(SupportRequest.objects.filter(status="pending").count(), 8)
        self.assertEqual(
            sum(SupportRequest.objects.values_list("dispatch_count", flat=True)), 2
        )

    def test_publish_failure_retains_work_and_records_backoff(self):
        incoming = self.create_request()

        def unavailable_broker(request_id, token):
            self.assertFalse(connection.in_atomic_block)
            raise BrokerError("Controlled broker outage")

        reconcile_generation(publish=unavailable_broker)

        incoming.refresh_from_db()
        self.assertEqual(incoming.status, "pending")
        self.assertEqual(incoming.attempt_count, 0)
        self.assertEqual(incoming.dispatch_count, 1)
        self.assertGreater(incoming.next_attempt_at, timezone.now())
        self.assertIn("Controlled broker outage", incoming.last_error)
        self.assertIsNone(incoming.processing_token)
        self.make_due(incoming)
        self.assertEqual(reconcile_generation(publish=lambda *args: None), 1)

    def test_ambiguous_publish_failure_cannot_reset_an_already_completed_task(self):
        incoming = self.create_request()

        def completed_before_publish_error(request_id, token):
            self.assertEqual(
                run_generation(request_id, token, generator=lambda request: self.draft),
                "review",
            )
            raise BrokerError("Publish confirmation was lost")

        reconcile_generation(publish=completed_before_publish_error)

        incoming.refresh_from_db()
        self.assertEqual(incoming.status, "review")
        self.assertEqual(incoming.draft_text, self.draft.text)
        self.assertEqual(incoming.last_error, "")

    def test_lost_message_is_replaced_and_its_old_token_is_ignored(self):
        incoming = self.create_request()
        old_token = self.reserve_one(incoming)
        self.expire_lease(incoming)

        reserve_generation()
        incoming.refresh_from_db()
        self.assertEqual(incoming.status, "pending")
        self.assertEqual(incoming.attempt_count, 0)
        self.assertTrue(incoming.last_error)
        self.make_due(incoming)
        new_token = self.reserve_one(incoming)
        self.assertNotEqual(old_token, new_token)
        self.assertEqual(
            run_generation(incoming.pk, old_token, generator=lambda request: self.draft),
            "ignored",
        )
        self.assertEqual(
            run_generation(incoming.pk, new_token, generator=lambda request: self.draft),
            "review",
        )
        incoming.refresh_from_db()
        self.assertEqual(incoming.attempt_count, 1)

    def test_duplicate_task_delivery_does_not_generate_a_second_draft(self):
        incoming = self.create_request()
        token = self.reserve_one(incoming)
        calls = []

        def generator(request):
            calls.append(request.pk)
            self.assertFalse(connection.in_atomic_block)
            return self.draft

        self.assertEqual(run_generation(incoming.pk, token, generator=generator), "review")
        self.assertEqual(run_generation(incoming.pk, token, generator=generator), "ignored")
        self.assertEqual(calls, [incoming.pk])
        incoming.refresh_from_db()
        self.assertEqual(incoming.attempt_count, 1)

    def test_claim_rotates_the_token_and_counts_one_execution_attempt(self):
        incoming = self.create_request()
        dispatch_token = self.reserve_one(incoming)

        claimed = claim_generation(incoming.pk, dispatch_token)

        self.assertIsNotNone(claimed)
        self.assertNotEqual(str(claimed.processing_token), dispatch_token)
        self.assertEqual(claimed.status, "generating")
        self.assertEqual(claimed.attempt_count, 1)
        self.assertGreater(claimed.lease_expires_at, timezone.now())
        self.assertIsNone(claim_generation(incoming.pk, dispatch_token))

    def test_expired_worker_result_is_rejected_before_any_reconciler_runs(self):
        incoming = self.create_request()
        dispatch_token = self.reserve_one(incoming)
        claimed = claim_generation(incoming.pk, dispatch_token)
        self.expire_lease(incoming)

        self.assertFalse(finish_generation(incoming.pk, claimed.processing_token, self.draft))
        self.assertEqual(
            fail_generation(incoming.pk, claimed.processing_token, RuntimeError("Old error")),
            "stale",
        )
        incoming.refresh_from_db()
        self.assertEqual(incoming.status, "generating")
        self.assertEqual(incoming.draft_text, "")

    def test_old_worker_cannot_overwrite_the_new_workers_result(self):
        incoming = self.create_request()
        first = claim_generation(incoming.pk, self.reserve_one(incoming))
        self.expire_lease(incoming)
        reserve_generation()
        self.make_due(incoming)
        second = claim_generation(incoming.pk, self.reserve_one(incoming))

        self.assertTrue(finish_generation(incoming.pk, second.processing_token, self.draft))
        stale_draft = Draft(text="Stale result", sources=self.draft.sources)
        self.assertFalse(finish_generation(incoming.pk, first.processing_token, stale_draft))

        incoming.refresh_from_db()
        self.assertEqual(incoming.status, "review")
        self.assertEqual(incoming.draft_text, self.draft.text)
        self.assertEqual(incoming.attempt_count, 2)

    def test_transient_errors_use_persisted_backoff_and_stop_after_max_attempts(self):
        incoming = self.create_request()
        delays = []

        def failing_generator(request):
            raise TransientGenerationError("Controlled temporary provider failure")

        for attempt in range(1, 4):
            self.make_due(incoming)
            dispatch_token = self.reserve_one(incoming)
            before = timezone.now()
            result = run_generation(incoming.pk, dispatch_token, generator=failing_generator)
            incoming.refresh_from_db()
            self.assertEqual(incoming.attempt_count, attempt)
            self.assertIn("Controlled temporary provider failure", incoming.last_error)
            self.assertEqual(incoming.draft_text, "")
            if attempt < 3:
                self.assertEqual(result, "pending")
                self.assertEqual(incoming.status, "pending")
                self.assertGreater(incoming.next_attempt_at, before)
                delays.append((incoming.next_attempt_at - before).total_seconds())
                self.assertEqual(reserve_generation(), [])
            else:
                self.assertEqual(result, "failed")
                self.assertEqual(incoming.status, "failed")
        self.assertGreater(delays[1], delays[0])
        self.make_due(incoming)
        self.assertEqual(reserve_generation(), [])

    def test_permanent_error_stops_immediately_with_visible_reason(self):
        incoming = self.create_request()
        dispatch_token = self.reserve_one(incoming)

        def permanent_failure(request):
            raise PermanentGenerationError("Controlled invalid configuration")

        self.assertEqual(run_generation(incoming.pk, dispatch_token, generator=permanent_failure), "failed")
        incoming.refresh_from_db()
        self.assertEqual(incoming.status, "failed")
        self.assertEqual(incoming.attempt_count, 1)
        self.assertIn("Controlled invalid configuration", incoming.last_error)
        self.assertEqual(reserve_generation(), [])

    def test_soft_time_limit_is_a_bounded_retryable_failure(self):
        incoming = self.create_request()
        dispatch_token = self.reserve_one(incoming)

        def time_limit(request):
            raise SoftTimeLimitExceeded()

        self.assertEqual(run_generation(incoming.pk, dispatch_token, generator=time_limit), "pending")
        incoming.refresh_from_db()
        self.assertEqual(incoming.attempt_count, 1)
        self.assertIn("SoftTimeLimitExceeded", incoming.last_error)
        self.assertGreater(incoming.next_attempt_at, timezone.now())

    @override_settings(GENERATION_MAX_DISPATCHES=2)
    def test_lost_messages_have_a_finite_dispatch_budget(self):
        incoming = self.create_request()
        for dispatch in range(1, 3):
            self.make_due(incoming)
            self.reserve_one(incoming)
            self.expire_lease(incoming)
            reserve_generation()
            incoming.refresh_from_db()
            self.assertEqual(incoming.dispatch_count, dispatch)
        self.assertEqual(incoming.status, "failed")
        self.assertEqual(incoming.attempt_count, 0)
        self.assertTrue(incoming.last_error)
        self.make_due(incoming)
        self.assertEqual(reserve_generation(), [])

    def test_manual_retry_only_reopens_failed_work(self):
        incoming = self.create_request(
            status="failed", attempt_count=3, dispatch_count=10, last_error="Stopped"
        )

        self.assertTrue(retry_failed_generation(incoming.pk))
        self.assertFalse(retry_failed_generation(incoming.pk))

        incoming.refresh_from_db()
        self.assertEqual(incoming.status, "pending")
        self.assertEqual(incoming.attempt_count, 0)
        self.assertEqual(incoming.dispatch_count, 0)
        self.assertEqual(incoming.last_error, "")
        self.assertIsNone(incoming.processing_token)
        self.assertIsNone(incoming.lease_expires_at)

    def test_database_error_during_claim_does_not_call_generator(self):
        incoming = self.create_request()
        dispatch_token = self.reserve_one(incoming)
        calls = []
        with patch("support.processing.claim_generation", side_effect=DatabaseError("DB unavailable")):
            with self.assertRaises(DatabaseError):
                run_generation(
                    incoming.pk,
                    dispatch_token,
                    generator=lambda request: calls.append(request.pk),
                )
        self.assertEqual(calls, [])
        incoming.refresh_from_db()
        self.assertEqual(incoming.status, "queued")

    def test_broker_unavailable_before_reservation_preserves_accepted_work(self):
        incoming = self.create_request()
        with patch("support.processing.current_app.connection_for_write") as connection_for_write:
            broker = connection_for_write.return_value.__enter__.return_value
            broker.ensure_connection.side_effect = BrokerError("Redis is unavailable")
            with self.assertRaises(BrokerError):
                reconcile_generation()

        incoming.refresh_from_db()
        self.assertEqual(incoming.status, "pending")
        self.assertEqual(incoming.dispatch_count, 0)
        self.assertEqual(incoming.attempt_count, 0)
        self.assertIsNone(incoming.processing_token)

    def test_celery_task_redelivery_is_idempotent(self):
        from support.tasks import generate_reply

        incoming = self.create_request()
        dispatch_token = self.reserve_one(incoming)
        with patch("support.processing.generate_draft", return_value=self.draft) as generator:
            self.assertEqual(generate_reply.run(incoming.pk, dispatch_token), "review")
            self.assertEqual(generate_reply.run(incoming.pk, dispatch_token), "ignored")

        generator.assert_called_once()
        incoming.refresh_from_db()
        self.assertEqual(incoming.attempt_count, 1)
        self.assertEqual(incoming.draft_text, self.draft.text)

    def test_unconfigured_generator_cannot_create_a_fake_success(self):
        incoming = self.create_request()
        dispatch_token = self.reserve_one(incoming)

        self.assertEqual(run_generation(incoming.pk, dispatch_token), "failed")

        incoming.refresh_from_db()
        self.assertEqual(incoming.status, "failed")
        self.assertEqual(incoming.draft_text, "")
        self.assertEqual(incoming.draft_sources, [])
        self.assertIn("not configured", incoming.last_error)

    def test_beat_tick_runs_the_reconciler_without_queuing_tick_tasks(self):
        from support.scheduling import DatabaseReconciliationScheduler

        incoming = self.create_request()
        publications = []
        scheduler = object.__new__(DatabaseReconciliationScheduler)
        with patch(
            "support.scheduling.reconcile_generation",
            side_effect=lambda: reconcile_generation(publish=lambda *args: publications.append(args)),
        ), patch("config.celery.app.send_task") as send_task:
            interval = scheduler.tick()

        self.assertGreater(interval, 0)
        self.assertEqual(len(publications), 1)
        self.assertEqual(publications[0][0], incoming.pk)
        send_task.assert_not_called()

    def test_beat_tick_survives_temporary_reconciliation_errors(self):
        from support.scheduling import DatabaseReconciliationScheduler

        scheduler = object.__new__(DatabaseReconciliationScheduler)
        with patch("support.scheduling.reconcile_generation", side_effect=DatabaseError("DB outage")):
            with self.assertLogs("support.scheduling", level="ERROR"):
                interval = scheduler.tick()

        self.assertGreater(interval, 0)

    def test_worker_process_crash_during_generator_is_recovered_from_postgresql(self):
        incoming = self.create_request()
        dispatch_token = self.reserve_one(incoming)
        script = """
import os
import sys
import django
django.setup()
from django.conf import settings
from support.processing import run_generation
settings.GENERATION_ENABLED = True
def controlled_crash(request):
    os._exit(23)
run_generation(int(sys.argv[1]), sys.argv[2], generator=controlled_crash)
"""
        environment = os.environ.copy()
        environment["DJANGO_SETTINGS_MODULE"] = "config.settings"
        environment["POSTGRES_DB"] = connection.settings_dict["NAME"]
        crashed = subprocess.run(
            [sys.executable, "-c", script, str(incoming.pk), dispatch_token],
            env=environment,
            capture_output=True,
            text=True,
            timeout=15,
        )

        self.assertEqual(crashed.returncode, 23, crashed.stdout + crashed.stderr)
        incoming.refresh_from_db()
        self.assertEqual(incoming.status, "generating")
        self.assertEqual(incoming.attempt_count, 1)
        self.assertEqual(incoming.draft_text, "")
        self.expire_lease(incoming)
        reserve_generation()
        incoming.refresh_from_db()
        self.assertEqual(incoming.status, "pending")
        self.assertTrue(incoming.last_error)
        self.make_due(incoming)
        new_token = self.reserve_one(incoming)
        self.assertEqual(
            run_generation(incoming.pk, new_token, generator=lambda request: self.draft),
            "review",
        )
        incoming.refresh_from_db()
        self.assertEqual(incoming.attempt_count, 2)

    def test_repeated_worker_losses_stop_at_max_execution_attempts(self):
        incoming = self.create_request()
        for attempt in range(1, 4):
            self.make_due(incoming)
            claim_generation(incoming.pk, self.reserve_one(incoming))
            self.expire_lease(incoming)
            reserve_generation()
            incoming.refresh_from_db()
            self.assertEqual(incoming.attempt_count, attempt)
            self.assertEqual(incoming.status, "pending" if attempt < 3 else "failed")
        self.assertEqual(reserve_generation(), [])

    def test_two_workers_use_independent_connections_and_only_one_calls_generator(self):
        incoming = self.create_request()
        dispatch_token = self.reserve_one(incoming)
        connection.close()
        barrier = threading.Barrier(2)
        started = threading.Event()
        release = threading.Event()
        calls = []

        def controlled_generator(request):
            self.assertFalse(connections["default"].in_atomic_block)
            calls.append(request.pk)
            started.set()
            if not release.wait(timeout=10):
                raise TimeoutError("Controlled generator was not released")
            return self.draft

        def worker():
            database = connections["default"]
            try:
                database.ensure_connection()
                with database.cursor() as cursor:
                    cursor.execute("SELECT pg_backend_pid()")
                    pid = cursor.fetchone()[0]
                barrier.wait(timeout=10)
                result = run_generation(incoming.pk, dispatch_token, generator=controlled_generator)
                return pid, result
            finally:
                database.close()

        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(worker) for _ in range(2)]
            try:
                self.assertTrue(started.wait(timeout=5))
                incoming.refresh_from_db()
                self.assertEqual(incoming.status, "generating")
                self.assertEqual(incoming.attempt_count, 1)
                self.assertEqual(
                    run_generation(incoming.pk, dispatch_token, generator=controlled_generator),
                    "ignored",
                )
            finally:
                release.set()
            results = [future.result(timeout=15) for future in futures]

        self.assertEqual(len({pid for pid, _ in results}), 2)
        self.assertEqual(sorted(result for _, result in results), ["ignored", "review"])
        self.assertEqual(calls, [incoming.pk])
        incoming.refresh_from_db()
        self.assertEqual(incoming.draft_text, self.draft.text)

    def test_concurrent_reconcilers_cannot_exceed_global_inflight_limit(self):
        for _ in range(10):
            self.create_request()
        connection.close()
        barrier = threading.Barrier(2)

        def planner():
            database = connections["default"]
            try:
                database.ensure_connection()
                with database.cursor() as cursor:
                    cursor.execute("SELECT pg_backend_pid()")
                    pid = cursor.fetchone()[0]
                barrier.wait(timeout=10)
                return pid, reserve_generation()
            finally:
                database.close()

        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(planner) for _ in range(2)]
            results = [future.result(timeout=15) for future in futures]

        reservations = [reservation for _, batch in results for reservation in batch]
        self.assertEqual(len({pid for pid, _ in results}), 2)
        self.assertEqual(len(reservations), 2)
        self.assertEqual(len({request_id for request_id, _ in reservations}), 2)
        self.assertEqual(SupportRequest.objects.filter(status="queued").count(), 2)
        self.assertEqual(SupportRequest.objects.filter(status="pending").count(), 8)


class GenerationConfigurationTests(SimpleTestCase):
    def test_unsafe_visibility_timeout_is_rejected(self):
        from support.checks import check_generation_configuration

        with override_settings(CELERY_VISIBILITY_TIMEOUT=120):
            errors = check_generation_configuration(None)

        self.assertIn("support.E003", [error.id for error in errors])

    def test_hard_limit_must_be_shorter_than_execution_lease(self):
        from support.checks import check_generation_configuration

        with override_settings(CELERY_TASK_TIME_LIMIT=120):
            errors = check_generation_configuration(None)

        self.assertIn("support.E002", [error.id for error in errors])

    def test_disabling_late_acknowledgement_is_rejected(self):
        from support.checks import check_generation_configuration

        with override_settings(CELERY_TASK_ACKS_LATE=False):
            errors = check_generation_configuration(None)

        self.assertIn("support.E004", [error.id for error in errors])
