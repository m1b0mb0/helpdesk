from django.db import models
from django.utils import timezone


class SupportRequest(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", "Pending"

    event_id = models.CharField(max_length=255, unique=True)
    ticket_id = models.CharField(max_length=255)
    customer_email = models.EmailField(max_length=254)
    message = models.TextField()
    reply_url = models.CharField(max_length=2048)
    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.PENDING
    )
    attempt_count = models.PositiveIntegerField(default=0)
    next_attempt_at = models.DateTimeField(default=timezone.now)
    last_error = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = (models.Index(fields=["status", "next_attempt_at"]),)

    def __str__(self):
        return self.event_id
