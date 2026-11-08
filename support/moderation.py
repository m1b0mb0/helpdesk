"""Atomic operator decisions. An approval persists intent; it never sends HTTP."""

from uuid import uuid4

from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.utils import timezone

from .generation import Draft, PermanentGenerationError, validate_draft
from .models import SupportRequest


class InvalidModerationState(ValueError):
    pass


class InvalidDraft(ValueError):
    pass


def _check_operator(actor):
    if not (
        actor is not None and actor.is_authenticated and actor.is_active
        and actor.is_staff and actor.has_perm("support.view_supportrequest")
        and actor.has_perm("support.moderate_supportrequest")
    ):
        raise PermissionDenied("Viewing and moderating support requests requires operator permissions.")


def _review_request(request_id):
    row = SupportRequest.objects.select_for_update().get(pk=request_id)
    if row.status != SupportRequest.Status.REVIEW:
        raise InvalidModerationState("Only drafts awaiting review can be moderated.")
    if (
        row.delivery_key is not None or row.approved_text
        or row.moderated_at is not None or row.moderated_by_id is not None
    ):
        raise InvalidModerationState("This request already contains a moderation decision.")
    return row


def _save_decision(row, actor):
    row.moderated_by = actor
    row.moderated_at = timezone.now()
    row.save(update_fields=[
        "status", "approved_text", "delivery_key", "moderated_at", "moderated_by", "updated_at",
    ])
    return row


def approve_request(request_id, *, actor):
    _check_operator(actor)
    with transaction.atomic():
        row = _review_request(request_id)
        if row.processing_token is not None or row.lease_expires_at is not None:
            raise InvalidDraft("Draft still has an active processing owner.")
        try:
            validate_draft(Draft(text=row.draft_text, sources=row.draft_sources))
        except PermanentGenerationError as error:
            raise InvalidDraft(str(error)) from error
        # The copied text and key belong to one persisted delivery intent. Later
        # draft edits or duplicate approvals cannot change either of them.
        row.approved_text = row.draft_text
        row.delivery_key = uuid4()
        row.status = SupportRequest.Status.APPROVED
        return _save_decision(row, actor)


def reject_request(request_id, *, actor):
    _check_operator(actor)
    with transaction.atomic():
        row = _review_request(request_id)
        # An operator can reject a malformed draft too; it must not gain intent.
        row.approved_text = ""
        row.delivery_key = None
        row.status = SupportRequest.Status.REJECTED
        return _save_decision(row, actor)
