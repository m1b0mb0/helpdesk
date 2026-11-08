import json
import threading
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch
from urllib.parse import quote

from django.db import DatabaseError, connection, connections
from django.test import Client, RequestFactory, TransactionTestCase, override_settings

from support.mock_helpdesk import mock_reply
from support.models import MockReply


@override_settings(
    MOCK_HELPDESK_FAULTS_ENABLED=False,
    MOCK_HELPDESK_FAULTS={},
    MOCK_HELPDESK_FAULT_DELAY_SECONDS=10,
)
class MockHelpdeskTests(TransactionTestCase):
    def setUp(self):
        self.assertEqual(connection.vendor, "postgresql")
        self.ticket_id = "T-MOCK"
        self.url = "/tickets/T-MOCK/replies"
        self.key = "mock-key-001"
        self.payload = {"ticket_id": self.ticket_id, "message": "An approved support reply."}

    def post(self, payload=None, key=None, client=None):
        return (client or self.client).post(
            self.url, data=json.dumps(self.payload if payload is None else payload),
            content_type="application/json", HTTP_IDEMPOTENCY_KEY=self.key if key is None else key,
        )

    def direct_post(self, payload=None):
        request = RequestFactory().post(
            self.url, data=json.dumps(self.payload if payload is None else payload),
            content_type="application/json", HTTP_IDEMPOTENCY_KEY=self.key,
        )
        return mock_reply(request, ticket_id=self.ticket_id)

    def test_first_post_and_identical_retry_return_201_and_one_reply(self):
        first = self.post()
        repeated = self.post()
        self.assertEqual(first.status_code, 201)
        self.assertEqual(repeated.status_code, 201)
        self.assertEqual(first.json()["id"], repeated.json()["id"])
        self.assertEqual(first.json()["status"], "accepted")
        self.assertEqual(repeated.json()["status"], "duplicate")
        self.assertEqual(MockReply.objects.count(), 1)
        saved = MockReply.objects.get()
        self.assertEqual((saved.idempotency_key, saved.ticket_id, saved.message), (
            self.key, self.payload["ticket_id"], self.payload["message"],
        ))

    def test_same_key_with_changed_message_or_ticket_is_conflict(self):
        self.assertEqual(self.post().status_code, 201)
        self.assertEqual(self.post({**self.payload, "message": "A different response"}).status_code, 409)
        response = self.client.post(
            "/tickets/T-OTHER/replies", data=json.dumps({**self.payload, "ticket_id": "T-OTHER"}),
            content_type="application/json", HTTP_IDEMPOTENCY_KEY=self.key,
        )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(MockReply.objects.count(), 1)
        self.assertEqual(MockReply.objects.get().message, self.payload["message"])

    def test_exact_message_formatting_and_unicode_are_preserved(self):
        self.ticket_id = "T/folder/українська"
        self.url = "/tickets/" + quote(self.ticket_id, safe="") + "/replies"
        self.payload = {"ticket_id": self.ticket_id, "message": "  Відповідь клієнту.\nKeep formatting.  "}
        self.assertEqual(self.post().status_code, 201)
        saved = MockReply.objects.get()
        self.assertEqual(saved.ticket_id, self.ticket_id)
        self.assertEqual(saved.message, self.payload["message"])

    def test_requires_post_json_and_explicit_valid_idempotency_key(self):
        self.assertEqual(self.client.get(self.url).status_code, 405)
        response = self.client.post(self.url, data="{}", content_type="text/plain", HTTP_IDEMPOTENCY_KEY=self.key)
        self.assertEqual(response.status_code, 415)
        for key in ("", " ", " bad", "bad key", "x" * 129, "bad\nkey", "ключ"):
            with self.subTest(key=key):
                self.assertEqual(self.post(key=key).status_code, 400)
        self.assertEqual(MockReply.objects.count(), 0)

    def test_invalid_json_and_payloads_are_rejected_without_saving(self):
        for body in (b"", b"{", b"\xff", b'{"ticket_id":"T-MOCK","message":"one","message":"two"}',
                     b'{"ticket_id":"T-MOCK","message":NaN}'):
            with self.subTest(body=body):
                response = self.client.post(
                    self.url, data=body, content_type="application/json",
                    CONTENT_TYPE="application/json", HTTP_IDEMPOTENCY_KEY=self.key,
                )
                self.assertEqual(response.status_code, 400)
        for payload in (
            [], {}, None, {"ticket_id": self.ticket_id}, {**self.payload, "extra": True},
            {**self.payload, "ticket_id": "T-wrong"}, {**self.payload, "message": ""},
            {**self.payload, "message": " "}, {**self.payload, "message": 123},
            {**self.payload, "message": "x" * 20001}, {**self.payload, "message": "bad\x00text"},
            {**self.payload, "message": "bad\ud800text"}, {**self.payload, "ticket_id": 1},
        ):
            with self.subTest(payload=repr(payload)[:70]):
                response = self.client.post(self.url, data=json.dumps(payload), content_type="application/json", HTTP_IDEMPOTENCY_KEY=self.key)
                self.assertEqual(response.status_code, 400)
        self.assertEqual(MockReply.objects.count(), 0)

    def test_oversized_body_is_bounded(self):
        response = self.client.post(self.url, data=b" " * (128 * 1024 + 1), content_type="application/json", HTTP_IDEMPOTENCY_KEY=self.key)
        self.assertEqual(response.status_code, 413)
        self.assertEqual(MockReply.objects.count(), 0)

    def test_external_sender_does_not_require_a_csrf_token(self):
        self.assertEqual(self.post(client=Client(enforce_csrf_checks=True)).status_code, 201)

    def test_real_insert_error_rolls_back_and_never_acknowledges_success(self):
        def fail_after_insert(execute, sql, params, many, context):
            result = execute(sql, params, many, context)
            if sql.lstrip().upper().startswith("INSERT") and '"support_mockreply"' in sql:
                raise DatabaseError("Controlled database failure after insert")
            return result
        with connection.execute_wrapper(fail_after_insert):
            with self.assertLogs("support.mock_helpdesk", level="ERROR"):
                response = self.direct_post()
        self.assertEqual(response.status_code, 503)
        self.assertEqual(MockReply.objects.count(), 0)

    def race(self, payloads):
        connection.close()
        barrier = threading.Barrier(len(payloads))
        def sender(payload):
            database = connections["default"]
            try:
                database.ensure_connection()
                with database.cursor() as cursor:
                    cursor.execute("SELECT pg_backend_pid()")
                    pid = cursor.fetchone()[0]
                barrier.wait(timeout=10)
                response = self.direct_post(payload)
                return pid, response.status_code, json.loads(response.content)
            finally:
                database.close()
        with ThreadPoolExecutor(max_workers=len(payloads)) as executor:
            observed = list(executor.map(sender, payloads))
        self.assertEqual(len({row[0] for row in observed}), len(payloads))
        return observed

    def test_concurrent_identical_posts_create_exactly_one_response(self):
        observed = self.race([self.payload] * 4)
        self.assertEqual([row[1] for row in observed], [201] * 4)
        self.assertEqual(len({row[2]["id"] for row in observed}), 1)
        self.assertEqual(MockReply.objects.count(), 1)

    def test_concurrent_conflicting_posts_have_one_winner(self):
        observed = self.race([self.payload, {**self.payload, "message": "A competing response"}])
        self.assertEqual(sorted(row[1] for row in observed), [201, 409])
        self.assertEqual(MockReply.objects.count(), 1)

    @override_settings(MOCK_HELPDESK_FAULTS_ENABLED=True, MOCK_HELPDESK_FAULTS={"mock-key-001": "5xx_before_save"})
    def test_controlled_5xx_before_save_does_not_accept_reply(self):
        self.assertEqual(self.post().status_code, 503)
        self.assertEqual(MockReply.objects.count(), 0)

    @override_settings(MOCK_HELPDESK_FAULTS_ENABLED=True, MOCK_HELPDESK_FAULTS={"mock-key-001": "5xx_after_save"})
    def test_5xx_after_commit_is_recovered_by_same_key_without_duplicate(self):
        self.assertEqual(self.post().status_code, 503)
        self.assertEqual(MockReply.objects.count(), 1)
        response = self.post()
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()["status"], "duplicate")
        self.assertEqual(MockReply.objects.count(), 1)

    @override_settings(MOCK_HELPDESK_FAULTS_ENABLED=True, MOCK_HELPDESK_FAULTS={"mock-key-001": "timeout_after_save"})
    def test_timeout_after_save_waits_only_after_commit_and_only_on_first_post(self):
        observed = []
        def read_committed():
            try:
                return MockReply.objects.filter(idempotency_key=self.key).exists()
            finally:
                connections.close_all()
        def delayed_ack(seconds):
            self.assertEqual(seconds, 10)
            self.assertFalse(connection.in_atomic_block)
            with ThreadPoolExecutor(max_workers=1) as executor:
                observed.append(executor.submit(read_committed).result(timeout=5))
        with patch("support.mock_helpdesk.time.sleep", side_effect=delayed_ack) as sleep:
            self.assertEqual(self.post().status_code, 201)
            self.assertEqual(self.post().status_code, 201)
        self.assertEqual(observed, [True])
        sleep.assert_called_once_with(10)
        self.assertEqual(MockReply.objects.count(), 1)

    @override_settings(MOCK_HELPDESK_FAULTS_ENABLED=True, MOCK_HELPDESK_FAULTS={"mock-key-001": "timeout_before_save"})
    def test_timeout_before_save_does_not_hold_a_database_transaction(self):
        def delayed_save(seconds):
            self.assertFalse(connection.in_atomic_block)
            self.assertEqual(MockReply.objects.count(), 0)
        with patch("support.mock_helpdesk.time.sleep", side_effect=delayed_save) as sleep:
            self.assertEqual(self.post().status_code, 201)
        sleep.assert_called_once_with(10)

    @override_settings(MOCK_HELPDESK_FAULTS={"mock-key-001": "timeout_after_save"})
    def test_fault_settings_and_client_headers_do_nothing_without_explicit_flag(self):
        with patch("support.mock_helpdesk.time.sleep") as sleep:
            response = self.client.post(
                self.url, data=json.dumps(self.payload), content_type="application/json",
                HTTP_IDEMPOTENCY_KEY=self.key, HTTP_X_MOCK_FAULT="5xx_before_save",
            )
        self.assertEqual(response.status_code, 201)
        sleep.assert_not_called()

    @override_settings(MOCK_HELPDESK_FAULTS_ENABLED=True, MOCK_HELPDESK_FAULTS={"another-key": "5xx_before_save"})
    def test_fault_configuration_is_scoped_to_exact_idempotency_key(self):
        self.assertEqual(self.post().status_code, 201)
        self.assertEqual(MockReply.objects.count(), 1)
