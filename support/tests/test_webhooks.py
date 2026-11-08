import json
import statistics
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from django.db import DatabaseError, IntegrityError, connection, connections, transaction
from django.http import JsonResponse
from django.test import Client, RequestFactory, TransactionTestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from support.models import SupportRequest
from support.views import helpdesk_webhook


class WebhookFixture:
    def setUp(self):
        super().setUp()
        self.url = reverse("helpdesk_webhook")
        self.payload = {
            "event_id": "evt_8f2a",
            "ticket_id": "T-1042",
            "customer_email": "user@example.com",
            "message": "Не можу експортувати звіт у PDF",
            "reply_url": "https://helpdesk.example.com/tickets/T-1042/replies",
        }

    def post(self, payload=None, client=None):
        return (client or self.client).post(
            self.url,
            data=json.dumps(self.payload if payload is None else payload),
            content_type="application/json",
        )


@override_settings(
    HELPDESK_ALLOWED_ORIGINS=(
        "http://web:8000",
        "https://helpdesk.example.com",
    )
)
class WebhookTests(WebhookFixture, TransactionTestCase):
    def test_new_event_is_saved_with_recoverable_pending_state(self):
        payload = {
            **self.payload,
            "customer_email": "User@Example.com",
            "message": "  Please help.\nKeep this formatting.  ",
        }

        response = self.post(payload)

        self.assertEqual(response.status_code, 202)
        self.assertEqual(
            response.json(), {"event_id": payload["event_id"], "status": "accepted"}
        )
        incoming = SupportRequest.objects.get(event_id=payload["event_id"])
        for field, value in payload.items():
            self.assertEqual(getattr(incoming, field), value)
        self.assertEqual(incoming.status, "pending")
        self.assertEqual(incoming.attempt_count, 0)
        self.assertEqual(incoming.last_error, "")
        self.assertLessEqual(incoming.next_attempt_at, timezone.now())
        self.assertLessEqual(incoming.created_at, incoming.updated_at)
        self.assertEqual(
            SupportRequest.objects.filter(
                status="pending", next_attempt_at__lte=timezone.now()
            ).count(),
            1,
        )

    def test_event_is_committed_before_success_response_is_constructed(self):
        observed = []

        def read_from_another_connection():
            try:
                return SupportRequest.objects.filter(
                    event_id=self.payload["event_id"]
                ).exists()
            finally:
                connections.close_all()

        def response_after_commit(*args, **kwargs):
            if kwargs.get("status") == 202:
                self.assertFalse(connection.in_atomic_block)
                with ThreadPoolExecutor(max_workers=1) as executor:
                    observed.append(executor.submit(read_from_another_connection).result())
            return JsonResponse(*args, **kwargs)

        with patch("support.views.JsonResponse", side_effect=response_after_commit):
            response = self.post()

        self.assertEqual(response.status_code, 202)
        self.assertEqual(observed, [True])

    def test_webhook_does_not_publish_a_celery_task(self):
        with patch("config.celery.app.send_task") as send_task:
            response = self.post()

        self.assertEqual(response.status_code, 202)
        send_task.assert_not_called()

    def test_webhook_accepts_external_post_without_csrf_token(self):
        response = self.post(client=Client(enforce_csrf_checks=True))

        self.assertEqual(response.status_code, 202)

    def test_get_is_not_allowed(self):
        self.assertEqual(self.client.get(self.url).status_code, 405)
        self.assertEqual(SupportRequest.objects.count(), 0)

    def test_non_json_content_type_is_rejected(self):
        response = self.client.post(
            self.url, data=json.dumps(self.payload), content_type="text/plain"
        )

        self.assertEqual(response.status_code, 415)
        self.assertEqual(SupportRequest.objects.count(), 0)

    def test_invalid_json_is_rejected(self):
        for body in (b"", b"{", b'{"event_id":}', b"\xff"):
            with self.subTest(body=body):
                response = self.client.post(
                    self.url,
                    data=body,
                    content_type="application/json",
                    CONTENT_TYPE="application/json",
                )
                self.assertEqual(response.status_code, 400)
                self.assertEqual(response.json()["error"], "invalid_json")
        self.assertEqual(SupportRequest.objects.count(), 0)

    def test_ambiguous_or_nonstandard_json_is_rejected(self):
        bodies = (
            '{"event_id":"earlier",' + json.dumps(self.payload)[1:],
            json.dumps({**self.payload, "message": float("nan")}),
            json.dumps({**self.payload, "message": float("inf")}),
        )
        for body in bodies:
            with self.subTest(body=body):
                response = self.client.post(
                    self.url, data=body, content_type="application/json"
                )
                self.assertEqual(response.status_code, 400)
                self.assertEqual(response.json()["error"], "invalid_json")
        self.assertEqual(SupportRequest.objects.count(), 0)

    def test_oversized_body_is_rejected(self):
        response = self.client.post(
            self.url, data=b"x" * (128 * 1024 + 1), content_type="application/json"
        )

        self.assertEqual(response.status_code, 413)
        self.assertEqual(SupportRequest.objects.count(), 0)

    def test_non_object_json_is_rejected(self):
        for payload in ([], None, True, 1, "text"):
            with self.subTest(payload=payload):
                response = self.client.post(
                    self.url,
                    data=json.dumps(payload),
                    content_type="application/json",
                )
                self.assertEqual(response.status_code, 400)
        self.assertEqual(SupportRequest.objects.count(), 0)

    def test_missing_fields_are_rejected(self):
        for field in self.payload:
            with self.subTest(field=field):
                payload = dict(self.payload)
                payload.pop(field)
                response = self.post(payload)
                self.assertEqual(response.status_code, 400)
                self.assertEqual(response.json()["error"], "invalid_payload")
                self.assertIn(field, response.json()["fields"])
        self.assertEqual(SupportRequest.objects.count(), 0)

    def test_unknown_fields_are_rejected(self):
        response = self.post({**self.payload, "extra": "unexpected"})

        self.assertEqual(response.status_code, 400)
        self.assertEqual(SupportRequest.objects.count(), 0)

    def test_every_payload_field_requires_a_nonblank_string(self):
        for field in self.payload:
            for value in (None, True, 7, 1.5, [], {}, "", "   "):
                with self.subTest(field=field, value=value):
                    response = self.post({**self.payload, field: value})
                    self.assertEqual(response.status_code, 400)
                    self.assertIn(field, response.json()["fields"])
        self.assertEqual(SupportRequest.objects.count(), 0)

    def test_field_length_limits_are_enforced(self):
        limits = {
            "event_id": 255,
            "ticket_id": 255,
            "customer_email": 254,
            "message": 10000,
            "reply_url": 2048,
        }
        for field, limit in limits.items():
            with self.subTest(field=field):
                response = self.post({**self.payload, field: "x" * (limit + 1)})
                self.assertEqual(response.status_code, 400)
                self.assertIn(field, response.json()["fields"])
        self.assertEqual(SupportRequest.objects.count(), 0)

    def test_invalid_email_is_rejected(self):
        for value in ("not-an-email", "user@", "user@example.com\nother@example.com"):
            with self.subTest(value=value):
                response = self.post({**self.payload, "customer_email": value})
                self.assertEqual(response.status_code, 400)
                self.assertIn("customer_email", response.json()["fields"])

    def test_unstorable_text_is_rejected_before_database_write(self):
        for field in self.payload:
            for value in ("text\x00value", "text\ud800value"):
                with self.subTest(field=field, value=ascii(value)):
                    response = self.post({**self.payload, field: value})
                    self.assertEqual(response.status_code, 400)
                    self.assertIn(field, response.json()["fields"])
        self.assertEqual(SupportRequest.objects.count(), 0)

    def test_mock_origin_and_normalized_https_port_are_allowed(self):
        for index, reply_url in enumerate(
            (
                "http://web:8000/tickets/T-1042/replies",
                "https://helpdesk.example.com:443/tickets/T-1042/replies",
            )
        ):
            with self.subTest(reply_url=reply_url):
                response = self.post(
                    {**self.payload, "event_id": f"evt_url_{index}", "reply_url": reply_url}
                )
                self.assertEqual(response.status_code, 202)

    def test_reply_url_requires_allowed_origin_and_exact_ticket_path(self):
        invalid_urls = (
            "ftp://helpdesk.example.com/tickets/T-1042/replies",
            "https://evil.example/tickets/T-1042/replies",
            "https://helpdesk.example.com.evil.example/tickets/T-1042/replies",
            "https://helpdesk.example.com@evil.example/tickets/T-1042/replies",
            "https://user:password@helpdesk.example.com/tickets/T-1042/replies",
            "https://helpdesk.example.com:444/tickets/T-1042/replies",
            "https://helpdesk.example.com/tickets/T-other/replies",
            "https://helpdesk.example.com/tickets/../replies",
            "https://helpdesk.example.com/tickets/T-1042/replies?extra=1",
            "https://helpdesk.example.com/tickets/T-1042/replies#fragment",
            "https://helpdesk.example.com\\evil.example/tickets/T-1042/replies",
            "https://helpdesk.example.com\n/tickets/T-1042/replies",
            "//helpdesk.example.com/tickets/T-1042/replies",
            "http://169.254.169.254/latest/meta-data/",
            "http://127.0.0.1:8000/tickets/T-1042/replies",
            "http://web:8001/tickets/T-1042/replies",
        )
        for reply_url in invalid_urls:
            with self.subTest(reply_url=reply_url):
                response = self.post({**self.payload, "reply_url": reply_url})
                self.assertEqual(response.status_code, 400)
                self.assertIn("reply_url", response.json()["fields"])
        self.assertEqual(SupportRequest.objects.count(), 0)

    def test_dot_ticket_segments_are_rejected_even_when_reply_path_matches(self):
        for ticket_id in (".", ".."):
            with self.subTest(ticket_id=ticket_id):
                response = self.post(
                    {
                        **self.payload,
                        "ticket_id": ticket_id,
                        "reply_url": f"http://web:8000/tickets/{ticket_id}/replies",
                    }
                )
                self.assertEqual(response.status_code, 400)
                self.assertIn("reply_url", response.json()["fields"])
        self.assertEqual(SupportRequest.objects.count(), 0)

    def test_identical_redelivery_does_not_change_saved_request(self):
        first = self.post()
        incoming = SupportRequest.objects.get()
        snapshot = (incoming.pk, incoming.created_at, incoming.updated_at)

        duplicate = self.post(dict(reversed(list(self.payload.items()))))

        self.assertEqual(first.status_code, 202)
        self.assertEqual(duplicate.status_code, 200)
        self.assertEqual(duplicate.json()["status"], "duplicate")
        self.assertEqual(SupportRequest.objects.count(), 1)
        incoming.refresh_from_db()
        self.assertEqual((incoming.pk, incoming.created_at, incoming.updated_at), snapshot)
        self.assertEqual(incoming.status, "pending")
        self.assertEqual(incoming.attempt_count, 0)

    def test_conflicting_redelivery_keeps_original_payload(self):
        self.assertEqual(self.post().status_code, 202)
        changes = (
            {"message": "A different message"},
            {"customer_email": "other@example.com"},
            {"reply_url": "http://web:8000/tickets/T-1042/replies"},
            {
                "ticket_id": "T-other",
                "reply_url": "https://helpdesk.example.com/tickets/T-other/replies",
            },
        )
        for change in changes:
            with self.subTest(change=change):
                response = self.post({**self.payload, **change})
                self.assertEqual(response.status_code, 409)
                self.assertEqual(response.json()["error"], "event_conflict")
        self.assertEqual(SupportRequest.objects.count(), 1)
        incoming = SupportRequest.objects.get()
        for field, value in self.payload.items():
            self.assertEqual(getattr(incoming, field), value)

    def test_different_events_for_one_ticket_are_both_saved(self):
        self.assertEqual(self.post().status_code, 202)
        response = self.post({**self.payload, "event_id": "evt_another"})

        self.assertEqual(response.status_code, 202)
        self.assertEqual(
            SupportRequest.objects.filter(ticket_id=self.payload["ticket_id"]).count(), 2
        )

    def test_database_enforces_unique_event_id(self):
        self.assertEqual(self.post().status_code, 202)

        with self.assertRaises(IntegrityError), transaction.atomic():
            SupportRequest.objects.create(**self.payload)

        self.assertEqual(SupportRequest.objects.count(), 1)

    def test_insert_error_returns_retryable_failure_without_acknowledgement(self):
        with self.assertLogs("support.views", level="ERROR"), patch(
            "support.services.SupportRequest.objects.get_or_create",
            side_effect=DatabaseError("Simulated insertion failure"),
        ):
            response = self.post()

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["error"], "database_unavailable")
        self.assertEqual(response.headers["Retry-After"], "1")
        self.assertEqual(SupportRequest.objects.count(), 0)

    def test_commit_error_returns_failure_and_rolls_back_insert(self):
        with self.assertLogs("support.views", level="ERROR"), patch.object(
            connection, "commit", side_effect=DatabaseError("Simulated commit failure")
        ):
            response = self.post()

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["error"], "database_unavailable")
        self.assertEqual(SupportRequest.objects.count(), 0)

    def test_single_request_response_time_in_local_test_client(self):
        started = time.perf_counter()
        response = self.post()
        elapsed = time.perf_counter() - started

        self.assertEqual(response.status_code, 202)
        print(f"\nWebhook timing: Django Client, PostgreSQL, 1 request: {elapsed:.4f}s")
        self.assertLess(elapsed, 2.0, "Local test-client response exceeded two seconds")


@override_settings(
    HELPDESK_ALLOWED_ORIGINS=(
        "http://web:8000",
        "https://helpdesk.example.com",
    )
)
class PostgreSQLConcurrencyTests(WebhookFixture, TransactionTestCase):
    """Actual concurrent PostgreSQL calls; HTTP transport is measured separately."""

    def setUp(self):
        super().setUp()
        if connection.vendor != "postgresql":
            self.fail("Webhook concurrency tests require PostgreSQL")

    def concurrent_post(self, payload, barrier=None):
        database = connections["default"]
        started = time.perf_counter()
        try:
            database.ensure_connection()
            with database.cursor() as cursor:
                cursor.execute("SELECT pg_backend_pid()")
                backend_pid = cursor.fetchone()[0]
            if barrier is not None:
                barrier.wait(timeout=10)
                started = time.perf_counter()
            request = RequestFactory().post(
                self.url, data=json.dumps(payload), content_type="application/json"
            )
            response = helpdesk_webhook(request)
            return response.status_code, backend_pid, time.perf_counter() - started
        finally:
            database.close()

    def test_simultaneous_identical_events_use_independent_connections(self):
        connection.close()
        barrier = threading.Barrier(4)
        with ThreadPoolExecutor(max_workers=4) as executor:
            futures = [
                executor.submit(self.concurrent_post, dict(self.payload), barrier)
                for _ in range(4)
            ]
            results = [future.result(timeout=15) for future in futures]

        self.assertEqual(sorted(status for status, _, _ in results), [200, 200, 200, 202])
        self.assertEqual(len({pid for _, pid, _ in results}), 4)
        self.assertEqual(SupportRequest.objects.count(), 1)
        self.assertEqual(SupportRequest.objects.get().status, "pending")

    def test_simultaneous_conflicting_events_keep_one_payload(self):
        connection.close()
        barrier = threading.Barrier(2)
        payloads = [self.payload, {**self.payload, "message": "Conflicting message"}]
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [
                executor.submit(self.concurrent_post, payload, barrier)
                for payload in payloads
            ]
            results = [future.result(timeout=15) for future in futures]

        self.assertEqual(sorted(status for status, _, _ in results), [202, 409])
        self.assertEqual(len({pid for _, pid, _ in results}), 2)
        self.assertEqual(SupportRequest.objects.count(), 1)
        self.assertIn(
            SupportRequest.objects.get().message,
            [payload["message"] for payload in payloads],
        )

    def test_wait_for_uncommitted_duplicate_is_bounded_and_retry_is_safe(self):
        inserted = threading.Event()
        release = threading.Event()

        def hold_uncommitted_insert():
            try:
                with transaction.atomic():
                    SupportRequest.objects.create(**self.payload)
                    inserted.set()
                    if not release.wait(timeout=10):
                        raise TimeoutError("Test did not release held transaction")
            finally:
                connections.close_all()

        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(hold_uncommitted_insert)
            try:
                self.assertTrue(inserted.wait(timeout=5))
                started = time.perf_counter()
                with self.assertLogs("support.views", level="ERROR"):
                    response = self.post()
                elapsed = time.perf_counter() - started
                self.assertEqual(response.status_code, 503)
                self.assertLess(elapsed, 2.0)
                print(f"\nWebhook held-duplicate lock timeout: {elapsed:.4f}s, HTTP 503")
            finally:
                release.set()
                future.result(timeout=10)

        self.assertEqual(self.post().status_code, 200)
        self.assertEqual(SupportRequest.objects.count(), 1)

    def test_all_500_unique_events_are_saved_under_concurrent_load(self):
        payloads = [
            {**self.payload, "event_id": f"evt_load_{index}"} for index in range(500)
        ]
        connection.close()
        started = time.perf_counter()
        with ThreadPoolExecutor(max_workers=4) as executor:
            results = list(executor.map(self.concurrent_post, payloads))
        total = time.perf_counter() - started
        timings = [elapsed for _, _, elapsed in results]
        percentile_95 = statistics.quantiles(timings, n=100, method="inclusive")[94]

        self.assertEqual({status for status, _, _ in results}, {202})
        self.assertEqual(SupportRequest.objects.count(), 500)
        self.assertEqual(SupportRequest.objects.filter(status="pending").count(), 500)
        self.assertSetEqual(
            set(SupportRequest.objects.values_list("event_id", flat=True)),
            {payload["event_id"] for payload in payloads},
        )
        self.assertGreaterEqual(len({pid for _, pid, _ in results}), 2)
        print(
            "\nWebhook load: PostgreSQL, RequestFactory, 500 unique events, 4 threads; "
            "no HTTP transport; timing includes connection acquisition/backend probe; "
            f"total={total:.4f}s min={min(timings):.4f}s "
            f"p50={statistics.median(timings):.4f}s p95={percentile_95:.4f}s "
            f"max={max(timings):.4f}s"
        )
        self.assertLess(
            max(timings), 2.0, "Local concurrent endpoint call exceeded two seconds"
        )
