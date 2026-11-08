"""Small idempotent HTTP helpdesk receiver used by the local demonstration."""

import json
import logging
import time

from django.conf import settings
from django.db import DatabaseError, transaction
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from .models import MockReply
from .views import _invalid_json_constant, _json_object


logger = logging.getLogger(__name__)
MAX_BODY_BYTES = 128 * 1024


class ReplyConflict(ValueError):
    pass


def _valid_text(value, limit):
    if not isinstance(value, str) or not value.strip() or len(value) > limit or "\x00" in value:
        return False
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def accept_mock_reply(idempotency_key, payload):
    """Commit one response per key; uniqueness resolves concurrent first POSTs."""
    with transaction.atomic(durable=True):
        reply, created = MockReply.objects.get_or_create(
            idempotency_key=idempotency_key,
            defaults={"ticket_id": payload["ticket_id"], "message": payload["message"]},
        )
        if not created and (
            reply.ticket_id != payload["ticket_id"] or reply.message != payload["message"]
        ):
            raise ReplyConflict("This idempotency key already has a different response.")
    return reply, created


def _fault_response(mode):
    if mode.startswith("timeout_"):
        time.sleep(settings.MOCK_HELPDESK_FAULT_DELAY_SECONDS)
        return None
    return JsonResponse({"error": "controlled_mock_failure"}, status=503)


@csrf_exempt
@transaction.non_atomic_requests
@require_POST
def mock_reply(request, ticket_id):
    if request.content_type != "application/json":
        return JsonResponse({"error": "unsupported_media_type"}, status=415)
    key = request.headers.get("Idempotency-Key", "")
    if not key or len(key) > 128 or any(not 33 <= ord(char) <= 126 for char in key):
        return JsonResponse({"error": "invalid_idempotency_key"}, status=400)
    body = request.read(MAX_BODY_BYTES + 1)
    if len(body) > MAX_BODY_BYTES:
        return JsonResponse({"error": "payload_too_large"}, status=413)
    try:
        payload = json.loads(
            body.decode("utf-8"), object_pairs_hook=_json_object,
            parse_constant=_invalid_json_constant,
        )
    except (ValueError, RecursionError):
        return JsonResponse({"error": "invalid_json"}, status=400)
    if (
        not isinstance(payload, dict) or set(payload) != {"ticket_id", "message"}
        or not _valid_text(payload["ticket_id"], 255)
        or not _valid_text(payload["message"], 20000)
        or payload["ticket_id"] != ticket_id
    ):
        return JsonResponse({"error": "invalid_payload"}, status=400)

    # Faults are keyed server configuration, never caller-supplied headers or a
    # public control endpoint. Enable only for an isolated local/test instance.
    mode = (
        settings.MOCK_HELPDESK_FAULTS.get(key, "")
        if settings.MOCK_HELPDESK_FAULTS_ENABLED else ""
    )
    if mode in {"5xx_before_save", "timeout_before_save"}:
        failure = _fault_response(mode)
        if failure is not None:
            return failure
    try:
        reply, created = accept_mock_reply(key, payload)
    except ReplyConflict:
        return JsonResponse({"error": "idempotency_conflict"}, status=409)
    except DatabaseError:
        logger.exception("Mock reply persistence failed; sender must retry the same key.")
        response = JsonResponse({"error": "database_unavailable"}, status=503)
        response["Retry-After"] = "1"
        return response

    # The durable transaction has committed before simulating lost acknowledgement.
    # Replayed requests bypass this one-shot post-save fault and return 201.
    if created and mode in {"5xx_after_save", "timeout_after_save"}:
        failure = _fault_response(mode)
        if failure is not None:
            return failure
    return JsonResponse({
        "id": reply.pk, "ticket_id": reply.ticket_id,
        "status": "accepted" if created else "duplicate",
    }, status=201)
