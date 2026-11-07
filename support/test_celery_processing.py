"""Real Redis/prefork coverage, isolated from the application's queues.

The only generator here is a controlled test fixture. These tests do not call
an LLM provider or enable a production placeholder generator.
"""

import json
import os
import signal
import time
from contextlib import contextmanager
from datetime import timedelta
from unittest import skipUnless
from uuid import uuid4

from celery import Celery
from celery.contrib.testing.worker import start_worker
from django.conf import settings
from django.db import connection, connections
from django.test import TransactionTestCase, override_settings
from django.utils import timezone
from redis import Redis

from config.celery import app as application_app
from support.generation import Draft
from support.models import SupportRequest
from support.processing import reserve_generation, run_generation


@skipUnless(hasattr(os, "fork"), "Celery prefork integration requires Linux containers")
@override_settings(GENERATION_ENABLED=True)
class CeleryProcessingIntegrationTests(TransactionTestCase):
    """Use PostgreSQL plus a real Redis broker; Celery eager mode is disabled."""

    def setUp(self):
        super().setUp()
        self.assertEqual(connection.vendor, "postgresql")
        self.namespace = "processing-test-" + uuid4().hex
        self.queue = self.namespace + "-queue"
        self.result_key = self.namespace + ":results"
        self.pid_key = self.namespace + ":child"
        self.redis = Redis.from_url(
            settings.CELERY_BROKER_URL,
            socket_connect_timeout=2,
            socket_timeout=2,
        )
        # Broker failures should fail these integration checks, not silently skip
        # the very delivery path the tests exist to exercise.
        self.assertTrue(self.redis.ping())
        self.incoming = SupportRequest.objects.create(
            event_id=self.namespace,
            ticket_id="T-celery-test",
            customer_email="fixture@example.com",
            message="Controlled generator integration test",
            reply_url="http://web:8000/tickets/T-celery-test/replies",
        )

    def tearDown(self):
        try:
            # Kombu also leaves binding bookkeeping after queue_delete(). Remove
            # only this test's UUID namespace, never shared application keys.
            keys = list(self.redis.scan_iter(match=self.namespace + "*", count=100))
            if keys:
                self.redis.delete(*keys)
            self.assertEqual(list(self.redis.scan_iter(match=self.namespace + "*")), [])
            self.redis.close()
        finally:
            # start_worker changes Celery's current/default application. Restore
            # the Django app so later tests keep their usual shared-task binding.
            application_app.set_current()
            application_app.set_default()
            super().tearDown()

    def wait_for(self, predicate, timeout=15):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            result = predicate()
            if result:
                return result
            time.sleep(0.05)
        self.fail("Timed out waiting for the isolated Celery worker")

    @contextmanager
    def isolated_worker(self):
        broker_url = settings.CELERY_BROKER_URL
        app = Celery(self.namespace, broker=broker_url, set_as_current=False)
        app.conf.update(
            task_always_eager=False,
            task_ignore_result=True,
            accept_content=["json"],
            task_serializer="json",
            result_serializer="json",
            task_acks_late=settings.CELERY_TASK_ACKS_LATE,
            task_reject_on_worker_lost=settings.CELERY_TASK_REJECT_ON_WORKER_LOST,
            worker_prefetch_multiplier=1,
            worker_hijack_root_logger=False,
            task_soft_time_limit=settings.CELERY_TASK_SOFT_TIME_LIMIT,
            task_time_limit=settings.CELERY_TASK_TIME_LIMIT,
            task_default_queue=self.queue,
            broker_transport_options={
                "global_keyprefix": self.namespace + ":",
                "visibility_timeout": settings.CELERY_VISIBILITY_TIMEOUT,
                "socket_connect_timeout": 2,
                "socket_timeout": 2,
            },
        )

        @app.task(bind=True, name=self.namespace + ".generate")
        def controlled_task(task, request_id, token, mode, result_key, pid_key):
            client = Redis.from_url(
                broker_url, socket_connect_timeout=2, socket_timeout=2
            )
            try:
                def controlled_generator(incoming):
                    if connection.in_atomic_block:
                        raise AssertionError("Generator ran inside a DB transaction")
                    if mode == "block":
                        client.set(pid_key, os.getpid(), ex=60)
                        # The test kills this real prefork child while it waits,
                        # representing a process lost during an external call.
                        time.sleep(30)
                    return Draft(
                        text="Controlled test draft for " + incoming.event_id,
                        sources=[{"title": "Fixture source", "url": "https://example.com/fixture"}],
                    )

                result = run_generation(request_id, token, generator=controlled_generator)
                client.rpush(
                    result_key,
                    json.dumps({"task_id": task.request.id, "status": result, "pid": os.getpid()}),
                )
                client.expire(result_key, 60)
                return result
            finally:
                client.close()

        # Fork no live PostgreSQL connection or pool background threads. Child
        # processes create their own connections to Django's test database.
        connections.close_all()
        connection.close_pool()
        try:
            with start_worker(
                app,
                pool="prefork",
                concurrency=2,
                queues=[self.queue],
                perform_ping_check=False,
                loglevel="ERROR",
                shutdown_timeout=20,
            ) as worker:
                yield controlled_task, worker
        finally:
            try:
                with app.connection_for_write() as broker:
                    broker.default_channel.queue_delete(queue=self.queue)
            finally:
                app.close()
                application_app.set_current()
                application_app.set_default()

    def publish(self, task, token, mode="complete"):
        return task.apply_async(
            args=[self.incoming.pk, str(token), mode, self.result_key, self.pid_key],
            queue=self.queue,
            retry=False,
        )

    def test_real_broker_delivers_duplicates_but_only_one_draft_is_saved(self):
        [(request_id, token)] = reserve_generation()
        self.assertEqual(request_id, self.incoming.pk)

        with self.isolated_worker() as (task, _worker):
            first = self.publish(task, token)
            second = self.publish(task, token)
            self.wait_for(lambda: self.redis.llen(self.result_key) == 2)

        results = [json.loads(value) for value in self.redis.lrange(self.result_key, 0, -1)]
        self.assertEqual({result["task_id"] for result in results}, {first.id, second.id})
        self.assertEqual(sorted(result["status"] for result in results), ["ignored", "review"])
        self.incoming.refresh_from_db()
        self.assertEqual(self.incoming.status, "review")
        self.assertEqual(self.incoming.attempt_count, 1)
        self.assertTrue(self.incoming.draft_text.startswith("Controlled test draft"))
        self.assertEqual(len(self.incoming.draft_sources), 1)

    def test_killed_prefork_child_is_recovered_from_postgresql_state(self):
        [(request_id, token)] = reserve_generation()
        self.assertEqual(request_id, self.incoming.pk)

        with self.isolated_worker() as (task, worker):
            self.publish(task, token, mode="block")
            child_pid = int(self.wait_for(lambda: self.redis.get(self.pid_key)))
            pool_pids = {process.pid for process in worker.pool._pool._pool}
            self.assertIn(child_pid, pool_pids)
            self.incoming.refresh_from_db()
            self.assertEqual(self.incoming.status, "generating")
            self.assertEqual(self.incoming.attempt_count, 1)
            original_run_token = self.incoming.processing_token

            # The embedded worker shares its parent process with this test.
            # Keep that parent's pool closed while Billiard forks a replacement.
            connections.close_all()
            connection.close_pool()
            os.kill(child_pid, signal.SIGKILL)
            # Late ACK + reject-on-worker-lost redelivers the old message. Its
            # queued token is obsolete, so it returns without another generator.
            self.wait_for(lambda: self.redis.llen(self.result_key) >= 1)
            old_result = json.loads(self.redis.lindex(self.result_key, 0))
            self.assertEqual(old_result["status"], "ignored")
            self.wait_for(
                lambda: child_pid not in {
                    process.pid for process in worker.pool._pool._pool
                }
                and len(worker.pool._pool._pool) == 2
            )
            self.incoming.refresh_from_db()
            self.assertEqual(self.incoming.attempt_count, 1)
            self.assertEqual(self.incoming.processing_token, original_run_token)

            # Advance only this test row's lease instead of waiting the full
            # operational lease. The process crash above is a real SIGKILL.
            SupportRequest.objects.filter(pk=self.incoming.pk).update(
                lease_expires_at=timezone.now() - timedelta(seconds=1)
            )
            self.assertEqual(reserve_generation(), [])
            self.incoming.refresh_from_db()
            self.assertEqual(self.incoming.status, "pending")
            self.assertTrue(self.incoming.last_error)
            SupportRequest.objects.filter(pk=self.incoming.pk).update(
                next_attempt_at=timezone.now() - timedelta(seconds=1)
            )
            [(recovered_id, new_token)] = reserve_generation()
            self.assertEqual(recovered_id, self.incoming.pk)
            self.assertNotEqual(str(new_token), str(token))
            self.publish(task, new_token)
            self.wait_for(lambda: self.redis.llen(self.result_key) >= 2)

        self.incoming.refresh_from_db()
        self.assertEqual(self.incoming.status, "review")
        self.assertEqual(self.incoming.attempt_count, 2)
        self.assertTrue(self.incoming.draft_text.startswith("Controlled test draft"))
