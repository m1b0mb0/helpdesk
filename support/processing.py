from datetime import timedelta
from uuid import uuid4

from celery import current_app
from django.conf import settings
from django.db import DatabaseError, connection, transaction
from django.utils import timezone

from .generation import (
    DeferredGenerationError,
    PermanentGenerationError,
    generate_draft,
    validate_draft,
)
from .models import SupportRequest


def _backoff(count):
    return min(
        settings.GENERATION_RETRY_BASE_SECONDS * 2 ** min(max(count - 1, 0), 16),
        settings.GENERATION_RETRY_MAX_SECONDS,
    )


def _defer_locked(row, reason, now, permanent=False, retry_after=0):
    count = row.attempt_count if row.status == row.Status.GENERATING else row.dispatch_count
    exhausted = (
        permanent
        or row.attempt_count >= settings.GENERATION_MAX_ATTEMPTS
        or row.dispatch_count >= settings.GENERATION_MAX_DISPATCHES
    )
    row.status = row.Status.FAILED if exhausted else row.Status.PENDING
    row.last_error = reason[:2000]
    row.next_attempt_at = now + timedelta(seconds=max(_backoff(count), retry_after))
    row.processing_token = None
    row.lease_expires_at = None
    row.save(
        update_fields=[
            "status", "last_error", "next_attempt_at", "processing_token",
            "lease_expires_at", "updated_at",
        ]
    )
    return row.status


def reserve_generation():
    """Recover stale work and reserve a bounded number of publication intents."""
    if not settings.GENERATION_ENABLED:
        return []
    now = timezone.now()
    reservations = []
    with transaction.atomic():
        with connection.cursor() as cursor:
            # Serialize planners, including accidentally started extra Beat instances.
            cursor.execute("SELECT pg_try_advisory_xact_lock(715483, 1)")
            if not cursor.fetchone()[0]:
                return []
        expired = SupportRequest.objects.select_for_update(skip_locked=True).filter(
            status__in=[SupportRequest.Status.QUEUED, SupportRequest.Status.GENERATING],
            lease_expires_at__lte=now,
        ).order_by("lease_expires_at", "pk")[:100]
        for row in expired:
            reason = (
                "Generation lease expired; the worker did not finish."
                if row.status == row.Status.GENERATING
                else "Queue reservation expired; the message was lost or not consumed."
            )
            _defer_locked(row, reason, now)

        active = SupportRequest.objects.filter(
            status__in=[SupportRequest.Status.QUEUED, SupportRequest.Status.GENERATING]
        ).count()
        slots = max(0, settings.GENERATION_MAX_INFLIGHT - active)
        candidates = SupportRequest.objects.select_for_update(skip_locked=True).filter(
            status=SupportRequest.Status.PENDING, next_attempt_at__lte=now,
        ).order_by("next_attempt_at", "pk")[:slots]
        for row in candidates:
            if (
                row.attempt_count >= settings.GENERATION_MAX_ATTEMPTS
                or row.dispatch_count >= settings.GENERATION_MAX_DISPATCHES
            ):
                _defer_locked(row, "Generation or publication attempt budget exhausted.", now)
                continue
            row.status = row.Status.QUEUED
            row.dispatch_count += 1
            row.processing_token = uuid4()
            row.lease_expires_at = now + timedelta(
                seconds=settings.GENERATION_QUEUE_LEASE_SECONDS
            )
            row.save(update_fields=[
                "status", "dispatch_count", "processing_token", "lease_expires_at", "updated_at",
            ])
            reservations.append((row.pk, str(row.processing_token)))
    return reservations


def claim_generation(request_id, dispatch_token):
    if not settings.GENERATION_ENABLED:
        return None
    now = timezone.now()
    with transaction.atomic():
        row = SupportRequest.objects.select_for_update(skip_locked=True).filter(
            pk=request_id,
            status=SupportRequest.Status.QUEUED,
            processing_token=dispatch_token,
            lease_expires_at__gt=now,
        ).first()
        if row is None:
            return None
        if row.attempt_count >= settings.GENERATION_MAX_ATTEMPTS:
            _defer_locked(row, "Generation attempt budget exhausted.", now)
            return None
        row.status = row.Status.GENERATING
        row.attempt_count += 1
        row.processing_token = uuid4()
        row.lease_expires_at = now + timedelta(seconds=settings.GENERATION_LEASE_SECONDS)
        row.save(update_fields=[
            "status", "attempt_count", "processing_token", "lease_expires_at", "updated_at",
        ])
    return row


def finish_generation(request_id, run_token, draft):
    validate_draft(draft)
    return bool(SupportRequest.objects.filter(
        pk=request_id,
        status=SupportRequest.Status.GENERATING,
        processing_token=run_token,
        lease_expires_at__gt=timezone.now(),
    ).update(
        status=SupportRequest.Status.REVIEW,
        draft_text=draft.text,
        draft_sources=draft.sources,
        last_error="",
        processing_token=None,
        lease_expires_at=None,
        updated_at=timezone.now(),
    ))


def fail_generation(request_id, run_token, error, permanent=False):
    now = timezone.now()
    with transaction.atomic():
        row = SupportRequest.objects.select_for_update().filter(
            pk=request_id,
            status=SupportRequest.Status.GENERATING,
            processing_token=run_token,
            lease_expires_at__gt=now,
        ).first()
        if row is None:
            return "stale"
        return _defer_locked(
            row, f"{type(error).__name__}: {error}", now, permanent,
            retry_after=getattr(error, "retry_after", 0),
        )


def defer_generation(request_id, run_token, error):
    """Limiter capacity/outage is waiting, not a failed HTTP execution attempt."""
    now = timezone.now()
    with transaction.atomic():
        row = SupportRequest.objects.select_for_update().filter(
            pk=request_id, status=SupportRequest.Status.GENERATING,
            processing_token=run_token, lease_expires_at__gt=now,
        ).first()
        if row is None:
            return "stale"
        row.status = row.Status.PENDING
        row.attempt_count = max(0, row.attempt_count - 1)
        row.dispatch_count = max(0, row.dispatch_count - 1)
        row.processing_token = None
        row.lease_expires_at = None
        row.last_error = f"{type(error).__name__}: {error}"[:2000]
        row.next_attempt_at = now + timedelta(seconds=max(5, error.retry_after))
        row.save(update_fields=[
            "status", "attempt_count", "dispatch_count", "processing_token",
            "lease_expires_at", "last_error", "next_attempt_at", "updated_at",
        ])
        return row.status


def run_generation(request_id, dispatch_token, generator=None):
    if not settings.GENERATION_ENABLED:
        return "disabled"
    row = claim_generation(request_id, dispatch_token)
    if row is None:
        return "ignored"
    # No PostgreSQL transaction or row lock is held during this external operation.
    try:
        draft = (generator or generate_draft)(row)
        return "review" if finish_generation(row.pk, row.processing_token, draft) else "stale"
    except DatabaseError:
        # A failed/uncertain write is recovered from its persisted lease, not a
        # second competing Celery retry mechanism.
        raise
    except DeferredGenerationError as error:
        return defer_generation(row.pk, row.processing_token, error)
    except PermanentGenerationError as error:
        return fail_generation(row.pk, row.processing_token, error, permanent=True)
    except Exception as error:
        return fail_generation(row.pk, row.processing_token, error)


def _publication_failed(request_id, token, error):
    with transaction.atomic():
        row = SupportRequest.objects.select_for_update().filter(
            pk=request_id, status=SupportRequest.Status.QUEUED, processing_token=token,
        ).first()
        if row is not None:
            _defer_locked(row, f"Publication failed: {type(error).__name__}: {error}", timezone.now())


def _reconcile_with_publisher(publish):
    published = 0
    for request_id, token in reserve_generation():
        try:
            publish(request_id, token)
        except Exception as error:
            # Publishing may have succeeded before its acknowledgement was lost.
            # Never overwrite a task that has already claimed this reservation.
            _publication_failed(request_id, token, error)
            break
        published += 1
    return published


def reconcile_generation(publish=None):
    if not settings.GENERATION_ENABLED:
        return 0
    if publish is not None:
        return _reconcile_with_publisher(publish)

    from .tasks import generate_reply

    # Establish broker availability before reserving rows. A Redis outage must
    # not spend the publication budget of otherwise healthy pending requests.
    with current_app.connection_for_write() as broker:
        broker.ensure_connection(max_retries=0)

        def publish_task(request_id, token):
            generate_reply.apply_async(
                args=[request_id, token],
                connection=broker,
                queue="generation",
                retry=False,
                expires=settings.GENERATION_QUEUE_LEASE_SECONDS,
            )

        return _reconcile_with_publisher(publish_task)


def retry_failed_generation(request_id):
    return bool(SupportRequest.objects.filter(
        pk=request_id, status=SupportRequest.Status.FAILED,
    ).update(
        status=SupportRequest.Status.PENDING,
        attempt_count=0,
        dispatch_count=0,
        processing_token=None,
        lease_expires_at=None,
        next_attempt_at=timezone.now(),
        last_error="",
        updated_at=timezone.now(),
    ))
