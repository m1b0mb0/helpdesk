import multiprocessing
import os
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.error import HTTPError
from urllib.request import ProxyHandler, Request, build_opener
from uuid import uuid4

from django.conf import settings
from django.test import SimpleTestCase, override_settings
from redis import Redis

from support.rate_limit import (
    LIMIT,
    WINDOW_MS,
    RateLimitExceeded,
    RateLimitUnavailable,
    acquire_slot,
    finish_slot,
)


def _controlled_provider(ready, stop, count, timestamps):
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length", "0")))
            with count.get_lock():
                index = count.value
                timestamps[index] = time.monotonic()
                count.value += 1
            # Both successful and failed actual HTTP attempts must spend permits.
            self.send_response(500 if index % 2 else 200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"{}")

        def log_message(self, *args):
            pass

    with HTTPServer(("127.0.0.1", 0), Handler) as server:
        server.timeout = 0.1
        ready.put(f"http://127.0.0.1:{server.server_port}/completions")
        while not stop.is_set():
            server.handle_request()


def _concurrent_acquisitions(start, results, endpoint):
    """A distinct process uses its own Redis connections, like a prefork worker."""
    start.wait(10)
    accepted = denied = failures = 0
    opener = build_opener(ProxyHandler({}))
    try:
        for _ in range(10):
            try:
                ticket = acquire_slot()
            except RateLimitExceeded:
                denied += 1
                continue
            try:
                try:
                    response = opener.open(Request(endpoint, data=b"{}", method="POST"), timeout=5)
                except HTTPError as error:
                    response = error
                with response:
                    response.read()
                    failures += response.code == 500
                accepted += 1
            finally:
                finish_slot(ticket)
        results.put((os.getpid(), accepted, denied, "", failures))
    except Exception as error:
        results.put((os.getpid(), accepted, denied, repr(error), failures))


class GlobalRateLimitTests(SimpleTestCase):
    """Real Redis, isolated keys; never flush the broker or production limiter."""

    def setUp(self):
        self.key = "rate-limit-test:" + uuid4().hex
        self.url = getattr(settings, "LLM_REDIS_URL", settings.CELERY_BROKER_URL)
        self.client = Redis.from_url(self.url, decode_responses=True)
        self.client.ping()  # A missing Redis is a failed required integration check.
        self.config = override_settings(
            LLM_REDIS_URL=self.url,
            LLM_RATE_LIMIT_KEY=self.key,
            CELERY_TASK_TIME_LIMIT=90,
        )
        self.config.enable()
        self.addCleanup(self.config.disable)
        self.addCleanup(self.cleanup_keys)

    def cleanup_keys(self):
        self.client.delete(self.key + ":state", self.key + ":slots")
        self.client.close()

    def make_ready(self):
        with self.assertRaises(RateLimitExceeded):
            acquire_slot()
        # Controlled passage of the startup quarantine, not a production bypass.
        self.client.hset(self.key + ":state", "warm_until", 0)

    def now_ms(self):
        seconds, micros = self.client.time()
        return seconds * 1000 + micros // 1000

    def test_cold_start_is_quarantined_for_old_task_lifetime_plus_window(self):
        with self.assertRaises(RateLimitExceeded) as first:
            acquire_slot()
        self.assertEqual(first.exception.retry_after, 156)
        with self.assertRaises(RateLimitExceeded) as second:
            acquire_slot()
        self.assertLessEqual(second.exception.retry_after, 156)
        self.assertGreaterEqual(second.exception.retry_after, 155)
        self.assertEqual(self.client.zcard(self.key + ":slots"), 1)

    def test_sixty_slots_are_global_and_finished_attempts_still_count(self):
        self.make_ready()
        tickets = [acquire_slot() for _ in range(LIMIT)]
        with self.assertRaises(RateLimitExceeded):
            acquire_slot()
        for ticket in tickets:
            finish_slot(ticket)
        with self.assertRaises(RateLimitExceeded) as denied:
            acquire_slot()
        self.assertGreaterEqual(denied.exception.retry_after, 60)
        self.assertEqual(self.client.zcard(self.key + ":slots"), 61)

    def test_unfinished_worker_reservation_outlives_hard_task_and_window(self):
        self.make_ready()
        now = self.now_ms()
        ticket = acquire_slot()
        deadline = self.client.zscore(self.key + ":slots", ticket.token)
        self.assertGreaterEqual(deadline - now, 156_000)
        finish_slot(ticket)
        shortened = self.client.zscore(self.key + ":slots", ticket.token)
        self.assertGreaterEqual(shortened - self.now_ms(), WINDOW_MS - 1000)
        self.assertLess(shortened, deadline)

    def test_only_expired_slots_are_released_at_sliding_window_boundary(self):
        self.make_ready()
        tickets = [acquire_slot() for _ in range(LIMIT)]
        for ticket in tickets:
            finish_slot(ticket)
        # Move exactly one completion past the window using controlled Redis data.
        self.client.zadd(self.key + ":slots", {tickets[0].token: self.now_ms() - 1})
        replacement = acquire_slot()
        self.assertNotEqual(replacement.token, tickets[0].token)
        with self.assertRaises(RateLimitExceeded):
            acquire_slot()
        self.assertIsNone(self.client.zscore(self.key + ":slots", tickets[0].token))

    def test_abandoned_slots_are_still_held_after_one_complete_minute(self):
        self.make_ready()
        tickets = [acquire_slot() for _ in range(LIMIT)]
        # Model a worker paused before sending HTTP for 61 seconds. Unlike a
        # start-time-only sliding window, these unfinished permits remain held.
        for ticket in tickets:
            score = self.client.zscore(self.key + ":slots", ticket.token)
            self.client.zadd(self.key + ":slots", {ticket.token: score - 61_000})
        with self.assertRaises(RateLimitExceeded) as denied:
            acquire_slot()
        self.assertGreaterEqual(denied.exception.retry_after, 94)
        self.assertEqual(self.client.zcard(self.key + ":slots"), 61)

    def test_changed_redis_run_id_quarantines_even_restored_aof_state(self):
        self.make_ready()
        old_ticket = acquire_slot()
        self.client.hset(self.key + ":state", "epoch", "previous-redis-run")
        with self.assertRaises(RateLimitExceeded) as denied:
            acquire_slot()
        self.assertEqual(denied.exception.retry_after, 156)
        self.assertIsNone(self.client.zscore(self.key + ":slots", old_ticket.token))
        finish_slot(old_ticket)
        self.assertEqual(self.client.zcard(self.key + ":slots"), 1)

    def test_missing_slots_with_surviving_metadata_is_also_quarantined(self):
        self.make_ready()
        acquire_slot()
        self.client.delete(self.key + ":slots")
        with self.assertRaises(RateLimitExceeded) as denied:
            acquire_slot()
        self.assertEqual(denied.exception.retry_after, 156)

    def test_redis_unavailable_fails_closed_without_long_wait(self):
        started = time.monotonic()
        with override_settings(LLM_REDIS_URL="redis://127.0.0.1:1/0"):
            with self.assertRaises(RateLimitUnavailable):
                acquire_slot()
        self.assertLess(time.monotonic() - started, 3)

    def test_failed_cleanup_does_not_free_the_original_slot(self):
        self.make_ready()
        ticket = acquire_slot()
        original = self.client.zscore(self.key + ":slots", ticket.token)
        with override_settings(LLM_REDIS_URL="redis://127.0.0.1:1/0"):
            finish_slot(ticket)
        self.assertEqual(self.client.zscore(self.key + ":slots", ticket.token), original)

    def test_eight_independent_processes_atomically_share_sixty_slots(self):
        if "fork" not in multiprocessing.get_all_start_methods():
            self.skipTest("Run the real prefork concurrency test inside Linux Docker.")
        self.make_ready()
        context = multiprocessing.get_context("fork")
        start = context.Event()
        stop = context.Event()
        results = context.Queue()
        ready = context.Queue()
        count = context.Value("i", 0)
        timestamps = context.Array("d", 80)
        provider = context.Process(target=_controlled_provider, args=(ready, stop, count, timestamps))
        processes = []
        try:
            provider.start()
            endpoint = ready.get(timeout=5)
            processes = [
                context.Process(target=_concurrent_acquisitions, args=(start, results, endpoint))
                for _ in range(8)
            ]
            for process in processes:
                process.start()
            start.set()
            observed = [results.get(timeout=15) for _ in processes]
            self.assertEqual(len({row[0] for row in observed}), 8)
            self.assertEqual([row[3] for row in observed], [""] * 8)
            self.assertEqual(sum(row[1] for row in observed), 60)
            self.assertEqual(sum(row[2] for row in observed), 20)
            self.assertEqual(sum(row[4] for row in observed), 30)
            self.assertEqual(count.value, 60)
            received = list(timestamps)[:count.value]
            self.assertLess(max(received) - min(received), 60)
            self.assertEqual(self.client.zcard(self.key + ":slots"), 61)
        finally:
            for process in processes:
                if process.pid is None:
                    continue
                process.join(timeout=3)
                if process.is_alive():
                    process.terminate()
                    process.join(timeout=3)
            stop.set()
            if provider.pid is not None:
                provider.join(timeout=3)
                if provider.is_alive():
                    provider.terminate()
                    provider.join(timeout=3)
            ready.close()
            results.close()
