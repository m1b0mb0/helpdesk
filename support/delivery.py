"""PostgreSQL-owned delivery scheduling, leases and bounded retry state."""

from datetime import timedelta
from uuid import uuid4

from celery import current_app
from django.conf import settings
from django.db import DatabaseError, connection, transaction
from django.utils import timezone

from .delivery_http import deliver_reply
from .models import SupportRequest
from .moderation import _check_operator


class TransientDeliveryError(Exception):
    def __init__(self, message, retry_after=0):
        super().__init__(message)
        self.retry_after = retry_after


class PermanentDeliveryError(Exception):
    pass


def _backoff(count):
    return min(
        settings.DELIVERY_RETRY_BASE_SECONDS * 2 ** min(max(count - 1, 0), 16),
        settings.DELIVERY_RETRY_MAX_SECONDS,
    )


def _defer_locked(row, reason, now, *, permanent=False, retry_after=0):
    count = row.delivery_attempt_count if row.status == row.Status.SENDING else row.delivery_dispatch_count
    exhausted = (
        permanent or row.delivery_attempt_count >= settings.DELIVERY_MAX_ATTEMPTS
        or row.delivery_dispatch_count >= settings.DELIVERY_MAX_DISPATCHES
    )
    row.status = row.Status.DELIVERY_FAILED if exhausted else row.Status.APPROVED
    row.delivery_last_error = reason[:2000]
    row.delivery_next_attempt_at = now + timedelta(seconds=max(_backoff(count), retry_after))
    row.delivery_token = None
    row.delivery_lease_expires_at = None
    row.save(update_fields=[
        "status", "delivery_last_error", "delivery_next_attempt_at", "delivery_token",
        "delivery_lease_expires_at", "updated_at",
    ])
    return row.status


def reserve_delivery():
    if not settings.DELIVERY_ENABLED:
        return []
    now = timezone.now()
    reservations = []
    with transaction.atomic():
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_try_advisory_xact_lock(715483, 2)")
            if not cursor.fetchone()[0]:
                return []
        expired = SupportRequest.objects.select_for_update(skip_locked=True).filter(
            status__in=[SupportRequest.Status.DELIVERY_QUEUED, SupportRequest.Status.SENDING],
            delivery_lease_expires_at__lte=now,
        ).order_by("delivery_lease_expires_at", "pk")[:100]
        for row in expired:
            _defer_locked(row, "Delivery reservation or execution lease expired.", now)
        active = SupportRequest.objects.filter(
            status__in=[SupportRequest.Status.DELIVERY_QUEUED, SupportRequest.Status.SENDING],
        ).count()
        slots = max(0, settings.DELIVERY_MAX_INFLIGHT - active)
        candidates = SupportRequest.objects.select_for_update(skip_locked=True).filter(
            status=SupportRequest.Status.APPROVED, delivery_next_attempt_at__lte=now,
        ).order_by("delivery_next_attempt_at", "pk")[:slots]
        for row in candidates:
            if (
                row.delivery_attempt_count >= settings.DELIVERY_MAX_ATTEMPTS
                or row.delivery_dispatch_count >= settings.DELIVERY_MAX_DISPATCHES
            ):
                _defer_locked(row, "Delivery execution or publication budget exhausted.", now)
                continue
            row.status = row.Status.DELIVERY_QUEUED
            row.delivery_dispatch_count += 1
            row.delivery_token = uuid4()
            row.delivery_lease_expires_at = now + timedelta(seconds=settings.DELIVERY_QUEUE_LEASE_SECONDS)
            row.save(update_fields=[
                "status", "delivery_dispatch_count", "delivery_token",
                "delivery_lease_expires_at", "updated_at",
            ])
            reservations.append((row.pk, str(row.delivery_token)))
    return reservations


def claim_delivery(request_id, dispatch_token):
    if not settings.DELIVERY_ENABLED:
        return None
    now = timezone.now()
    with transaction.atomic():
        row = SupportRequest.objects.select_for_update(skip_locked=True).filter(
            pk=request_id, status=SupportRequest.Status.DELIVERY_QUEUED,
            delivery_token=dispatch_token, delivery_lease_expires_at__gt=now,
        ).first()
        if row is None:
            return None
        if row.delivery_attempt_count >= settings.DELIVERY_MAX_ATTEMPTS:
            _defer_locked(row, "Delivery attempt budget exhausted.", now)
            return None
        row.status = row.Status.SENDING
        row.delivery_attempt_count += 1
        row.delivery_last_attempt_at = now
        row.delivery_token = uuid4()
        row.delivery_lease_expires_at = now + timedelta(seconds=settings.DELIVERY_LEASE_SECONDS)
        row.save(update_fields=[
            "status", "delivery_attempt_count", "delivery_last_attempt_at", "delivery_token",
            "delivery_lease_expires_at", "updated_at",
        ])
    return row


def finish_delivery(request_id, run_token):
    now = timezone.now()
    return bool(SupportRequest.objects.filter(
        pk=request_id, status=SupportRequest.Status.SENDING,
        delivery_token=run_token, delivery_lease_expires_at__gt=now,
    ).update(
        status=SupportRequest.Status.SENT, sent_at=now, delivery_last_error="",
        delivery_token=None, delivery_lease_expires_at=None, updated_at=now,
    ))


def fail_delivery(request_id, run_token, error, permanent=False):
    now = timezone.now()
    with transaction.atomic():
        row = SupportRequest.objects.select_for_update().filter(
            pk=request_id, status=SupportRequest.Status.SENDING,
            delivery_token=run_token, delivery_lease_expires_at__gt=now,
        ).first()
        if row is None:
            return "stale"
        return _defer_locked(
            row, f"{type(error).__name__}: {error}", now, permanent=permanent,
            retry_after=getattr(error, "retry_after", 0),
        )


def run_delivery(request_id, dispatch_token, sender=None):
    if not settings.DELIVERY_ENABLED:
        return "disabled"
    row = claim_delivery(request_id, dispatch_token)
    if row is None:
        return "ignored"
    try:
        # No PostgreSQL transaction is held while the external endpoint runs.
        (sender or deliver_reply)(row)
        return "sent" if finish_delivery(row.pk, row.delivery_token) else "stale"
    except DatabaseError:
        raise  # Persisted lease handles uncertain/failed result writes.
    except PermanentDeliveryError as error:
        return fail_delivery(row.pk, row.delivery_token, error, permanent=True)
    except Exception as error:
        return fail_delivery(row.pk, row.delivery_token, error)


def _publication_failed(request_id, token, error):
    with transaction.atomic():
        row = SupportRequest.objects.select_for_update().filter(
            pk=request_id, status=SupportRequest.Status.DELIVERY_QUEUED, delivery_token=token,
        ).first()
        if row is not None:
            _defer_locked(row, f"Delivery publication failed: {type(error).__name__}: {error}", timezone.now())


def _reconcile_with_publisher(publish):
    published = 0
    for request_id, token in reserve_delivery():
        try:
            publish(request_id, token)
        except Exception as error:
            _publication_failed(request_id, token, error)
            break
        published += 1
    return published


def reconcile_delivery(publish=None):
    if not settings.DELIVERY_ENABLED:
        return 0
    if publish is not None:
        return _reconcile_with_publisher(publish)
    from .tasks import deliver_reply as delivery_task

    with current_app.connection_for_write() as broker:
        broker.ensure_connection(max_retries=0)

        def publish_task(request_id, token):
            delivery_task.apply_async(
                args=[request_id, token], connection=broker, queue="delivery",
                retry=False, expires=settings.DELIVERY_QUEUE_LEASE_SECONDS,
            )

        return _reconcile_with_publisher(publish_task)


def retry_failed_delivery(request_id, *, actor):
    _check_operator(actor)
    # Delivery identity/payload/audit are deliberately absent from this UPDATE.
    return bool(SupportRequest.objects.filter(
        pk=request_id, status=SupportRequest.Status.DELIVERY_FAILED,
    ).update(
        status=SupportRequest.Status.APPROVED,
        delivery_attempt_count=0, delivery_dispatch_count=0,
        delivery_token=None, delivery_lease_expires_at=None,
        delivery_next_attempt_at=timezone.now(), delivery_last_error="",
        updated_at=timezone.now(),
    ))
