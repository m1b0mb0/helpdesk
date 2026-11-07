from django.db import connection, transaction

from .models import SupportRequest
from .validation import PAYLOAD_LIMITS, validate_webhook_payload


class EventConflict(ValueError):
    pass


def accept_webhook(payload):
    payload = validate_webhook_payload(payload)
    # A durable outer transaction must finish (including COMMIT) before the view
    # acknowledges delivery. Limits are local to this short ingestion transaction.
    with transaction.atomic(durable=True):
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT set_config('lock_timeout', '300ms', true), "
                "set_config('statement_timeout', '700ms', true), "
                "set_config('transaction_timeout', '1200ms', true)"
            )
        event_id = payload["event_id"]
        defaults = {key: value for key, value in payload.items() if key != "event_id"}
        support_request, created = SupportRequest.objects.get_or_create(
            event_id=event_id, defaults=defaults
        )
        if not created and any(
            getattr(support_request, field) != payload[field] for field in PAYLOAD_LIMITS
        ):
            raise EventConflict("The event_id already has a different payload.")
    return support_request, created
