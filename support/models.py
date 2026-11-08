from django.conf import settings
from django.db import models
from django.utils import timezone


class SupportRequest(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        QUEUED = "queued", "Queued"
        GENERATING = "generating", "Generating"
        REVIEW = "review", "Awaiting review"
        APPROVED = "approved", "Approved; awaiting delivery"
        DELIVERY_QUEUED = "delivery_queued", "Delivery queued"
        SENDING = "sending", "Sending"
        SENT = "sent", "Sent"
        DELIVERY_FAILED = "delivery_failed", "Delivery failed"
        REJECTED = "rejected", "Rejected"
        FAILED = "failed", "Failed"

    event_id = models.CharField(max_length=255, unique=True)
    ticket_id = models.CharField(max_length=255)
    customer_email = models.EmailField(max_length=254)
    message = models.TextField()
    reply_url = models.CharField(max_length=2048)
    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.PENDING
    )
    attempt_count = models.PositiveIntegerField(default=0)
    dispatch_count = models.PositiveIntegerField(default=0)
    processing_token = models.UUIDField(null=True, blank=True)
    lease_expires_at = models.DateTimeField(null=True, blank=True)
    draft_text = models.TextField(blank=True)
    draft_sources = models.JSONField(default=list, blank=True)
    approved_text = models.TextField(blank=True)
    delivery_key = models.UUIDField(null=True, blank=True, unique=True)
    delivery_attempt_count = models.PositiveIntegerField(default=0)
    delivery_dispatch_count = models.PositiveIntegerField(default=0)
    delivery_token = models.UUIDField(null=True, blank=True)
    delivery_lease_expires_at = models.DateTimeField(null=True, blank=True)
    delivery_next_attempt_at = models.DateTimeField(default=timezone.now)
    delivery_last_attempt_at = models.DateTimeField(null=True, blank=True)
    delivery_last_error = models.TextField(blank=True)
    sent_at = models.DateTimeField(null=True, blank=True)
    moderated_at = models.DateTimeField(null=True, blank=True)
    moderated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True,
        on_delete=models.PROTECT, related_name="+",
    )
    next_attempt_at = models.DateTimeField(default=timezone.now)
    last_error = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = (
            models.Index(fields=["status", "next_attempt_at"]),
            models.Index(fields=["status", "delivery_next_attempt_at"]),
        )
        permissions = (("moderate_supportrequest", "Can moderate support request drafts"),)
        constraints = (
            models.CheckConstraint(
                condition=(
                    models.Q(approved_text="", delivery_key__isnull=True)
                    | (~models.Q(approved_text="") & models.Q(delivery_key__isnull=False))
                ),
                name="support_delivery_snapshot_pair",
            ),
            models.CheckConstraint(
                condition=(
                    ~models.Q(status__in=[
                        "approved", "delivery_queued", "sending", "sent", "delivery_failed",
                    ])
                    | (~models.Q(approved_text="") & models.Q(
                        delivery_key__isnull=False, moderated_at__isnull=False,
                        moderated_by__isnull=False,
                    ))
                ),
                name="support_approved_has_decision",
            ),
            models.CheckConstraint(
                condition=(
                    ~models.Q(status="rejected")
                    | models.Q(
                        approved_text="", delivery_key__isnull=True,
                        moderated_at__isnull=False, moderated_by__isnull=False,
                    )
                ),
                name="support_rejected_has_no_delivery",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(status="sent", sent_at__isnull=False)
                    | (~models.Q(status="sent") & models.Q(sent_at__isnull=True))
                ),
                name="support_sent_has_timestamp",
            ),
        )

    def __str__(self):
        return self.event_id


class KnowledgeArticle(models.Model):
    slug = models.SlugField(max_length=100, unique=True)
    title = models.CharField(max_length=200)
    body = models.TextField()
    keywords = models.JSONField(default=list)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("slug",)

    def __str__(self):
        return self.title


class MockReply(models.Model):
    idempotency_key = models.CharField(max_length=128, unique=True)
    ticket_id = models.CharField(max_length=255)
    message = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.ticket_id}: {self.idempotency_key}"
