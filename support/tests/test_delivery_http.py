import json
import signal
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from unittest import skipUnless
from uuid import uuid4

from django.test import SimpleTestCase, override_settings

from support.delivery import PermanentDeliveryError, TransientDeliveryError
from support.delivery_http import deliver_reply


@skipUnless(hasattr(signal, "setitimer"), "Delivery total HTTP deadline requires Linux")
class DeliveryHTTPTests(SimpleTestCase):
    def setUp(self):
        self.calls = []
        self.behavior = {"status": 201, "body": b"{}", "headers": {}, "delay": 0}
        calls, behavior = self.calls, self.behavior

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                calls.append({"path": self.path, "headers": dict(self.headers), "body": body})
                time.sleep(behavior.get("delay", 0))
                self.send_response(behavior["status"])
                for name, value in behavior.get("headers", {}).items():
                    self.send_header(name, value)
                if "Content-Length" not in behavior.get("headers", {}):
                    self.send_header("Content-Length", str(len(behavior["body"])))
                self.end_headers()
                try:
                    if behavior.get("slow_stream"):
                        for byte in behavior["body"]:
                            self.wfile.write(bytes([byte]))
                            self.wfile.flush()
                            time.sleep(0.03)
                    else:
                        self.wfile.write(behavior["body"])
                except (BrokenPipeError, ConnectionResetError):
                    pass
                self.close_connection = True

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop_server)
        self.origin = f"http://127.0.0.1:{self.server.server_port}"
        self.configuration = override_settings(
            HELPDESK_ALLOWED_ORIGINS=(self.origin,), DELIVERY_HTTP_TIMEOUT=1,
        )
        self.configuration.enable()
        self.addCleanup(self.configuration.disable)
        self.row = SimpleNamespace(
            reply_url=self.origin + "/tickets/T-HTTP/replies",
            ticket_id="T-HTTP", approved_text="Frozen approved reply",
            draft_text="A changed draft which must never be sent", delivery_key=uuid4(),
        )

    def stop_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def test_real_post_sends_only_frozen_text_ticket_and_stable_key(self):
        self.assertIsNone(deliver_reply(self.row))
        self.assertEqual(len(self.calls), 1)
        call = self.calls[0]
        self.assertEqual(call["path"], "/tickets/T-HTTP/replies")
        self.assertEqual(json.loads(call["body"]), {
            "ticket_id": "T-HTTP", "message": "Frozen approved reply",
        })
        self.assertEqual(call["headers"]["Idempotency-Key"], str(self.row.delivery_key))
        self.assertEqual(call["headers"]["Content-Type"], "application/json")
        self.assertNotIn("A changed draft", call["body"].decode())

    def test_another_http_attempt_preserves_the_idempotency_key_and_payload(self):
        deliver_reply(self.row)
        self.row.draft_text = "Yet another draft edit"
        deliver_reply(self.row)
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(self.calls[0]["body"], self.calls[1]["body"])
        self.assertEqual(
            self.calls[0]["headers"]["Idempotency-Key"],
            self.calls[1]["headers"]["Idempotency-Key"],
        )

    def test_unicode_approved_reply_is_preserved(self):
        self.row.approved_text = "Підтверджена відповідь про експорт PDF."
        deliver_reply(self.row)
        self.assertEqual(json.loads(self.calls[0]["body"])["message"], self.row.approved_text)

    def test_disallowed_url_never_sends_http(self):
        original = self.row.reply_url
        invalid = (
            None, "https://evil.example/tickets/T-HTTP/replies",
            original + "?redirect=evil", original + "#fragment",
            self.origin + "/tickets/another-ticket/replies",
            self.origin + "/tickets/../replies",
            self.origin.replace("127.0.0.1", "localhost") + "/tickets/T-HTTP/replies",
            self.origin.replace("http://", "http://user:password@") + "/tickets/T-HTTP/replies",
            self.origin + "\\tickets\\T-HTTP\\replies",
        )
        for url in invalid:
            with self.subTest(url=url):
                self.row.reply_url = url
                with self.assertRaises(PermanentDeliveryError):
                    deliver_reply(self.row)
        self.assertEqual(self.calls, [])

    def test_missing_snapshot_or_key_never_sends_http(self):
        for text, key in (("", uuid4()), ("   ", uuid4()), (None, uuid4()), ("Reply", None), ("Reply", "not-a-uuid")):
            with self.subTest(text=text, key=key):
                self.row.approved_text, self.row.delivery_key = text, key
                with self.assertRaises(PermanentDeliveryError):
                    deliver_reply(self.row)
        self.assertEqual(self.calls, [])

    def test_only_201_is_success_and_permanent_errors_are_not_retried(self):
        for status in (200, 202, 400, 401, 403, 404, 409, 422):
            with self.subTest(status=status):
                self.behavior["status"] = status
                with self.assertRaises(PermanentDeliveryError) as caught:
                    deliver_reply(self.row)
                self.assertIn(str(status), str(caught.exception))
        self.assertEqual(len(self.calls), 8)

    def test_redirect_is_not_followed(self):
        self.behavior.update(status=307, headers={"Location": "http://evil.example/steal"})
        with self.assertRaises(PermanentDeliveryError):
            deliver_reply(self.row)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.calls[0]["path"], "/tickets/T-HTTP/replies")

    def test_temporary_statuses_and_retry_after_are_classified(self):
        for status in (408, 429, 500, 502, 503, 504):
            with self.subTest(status=status):
                self.behavior.update(status=status, headers={"Retry-After": "120"})
                with self.assertRaises(TransientDeliveryError) as caught:
                    deliver_reply(self.row)
                self.assertEqual(caught.exception.retry_after, 120)
                self.assertIn(str(status), str(caught.exception))

    def test_retry_after_is_bounded_and_invalid_values_are_ignored(self):
        self.behavior["status"] = 429
        for header, expected in (("9999999", 3600), ("-10", 0), ("not-a-duration", 0)):
            with self.subTest(header=header):
                self.behavior["headers"] = {"Retry-After": header}
                with self.assertRaises(TransientDeliveryError) as caught:
                    deliver_reply(self.row)
                self.assertEqual(caught.exception.retry_after, expected)

    @override_settings(DELIVERY_HTTP_TIMEOUT=0.15)
    def test_timeout_is_bounded_and_transient(self):
        self.behavior["delay"] = 0.5
        started = time.monotonic()
        with self.assertRaises(TransientDeliveryError):
            deliver_reply(self.row)
        self.assertLess(time.monotonic() - started, 0.8)
        self.assertEqual(len(self.calls), 1)

    @override_settings(DELIVERY_HTTP_TIMEOUT=0.15)
    def test_total_deadline_bounds_slowly_streamed_response(self):
        self.behavior.update(body=b"x" * 30, slow_stream=True)
        started = time.monotonic()
        with self.assertRaises(TransientDeliveryError):
            deliver_reply(self.row)
        self.assertLess(time.monotonic() - started, 0.8)

    def test_truncated_response_is_a_retryable_uncertain_outcome(self):
        self.behavior.update(body=b"{}", headers={"Content-Length": "50"})
        with self.assertRaises(TransientDeliveryError):
            deliver_reply(self.row)

    def test_oversized_response_is_bounded(self):
        self.behavior["body"] = b"x" * (64 * 1024 + 1)
        with self.assertRaises(TransientDeliveryError):
            deliver_reply(self.row)
