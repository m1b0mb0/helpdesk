import json
import logging

from django.db import DatabaseError, transaction
from django.http import HttpResponse, JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST, require_safe

from .services import EventConflict, accept_webhook
from .validation import InvalidPayload


logger = logging.getLogger(__name__)
WEBHOOK_MAX_BODY_BYTES = 128 * 1024


@require_safe
def health(request):
    return HttpResponse("OK", content_type="text/plain")


def _json_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON keys are not allowed.")
        result[key] = value
    return result


def _invalid_json_constant(value):
    raise ValueError("Non-finite JSON numbers are not allowed.")


@csrf_exempt
@transaction.non_atomic_requests
@require_POST
def helpdesk_webhook(request):
    if request.content_type != "application/json":
        return JsonResponse({"error": "unsupported_media_type"}, status=415)
    body = request.read(WEBHOOK_MAX_BODY_BYTES + 1)
    if len(body) > WEBHOOK_MAX_BODY_BYTES:
        return JsonResponse({"error": "payload_too_large"}, status=413)
    try:
        payload = json.loads(
            body.decode("utf-8"),
            object_pairs_hook=_json_object,
            parse_constant=_invalid_json_constant,
        )
    except (ValueError, RecursionError):
        return JsonResponse({"error": "invalid_json"}, status=400)

    try:
        support_request, created = accept_webhook(payload)
    except InvalidPayload as error:
        return JsonResponse(
            {"error": "invalid_payload", "fields": error.errors}, status=400
        )
    except EventConflict:
        return JsonResponse({"error": "event_conflict"}, status=409)
    except DatabaseError:
        logger.exception("Webhook persistence failed; delivery must be retried.")
        response = JsonResponse({"error": "database_unavailable"}, status=503)
        response["Retry-After"] = "1"
        return response

    return JsonResponse(
        {
            "event_id": support_request.event_id,
            "status": "accepted" if created else "duplicate",
        },
        status=202 if created else 200,
    )
