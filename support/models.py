from django.db import models
from django.utils import timezone


class SupportRequest(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        QUEUED = "queued", "Queued"
        GENERATING = "generating", "Generating"
        REVIEW = "review", "Awaiting review"
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
    next_attempt_at = models.DateTimeField(default=timezone.now)
    last_error = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = (models.Index(fields=["status", "next_attempt_at"]),)

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
