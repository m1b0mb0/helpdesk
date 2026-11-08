"""One real helpdesk HTTP POST using the immutable approval snapshot."""

import json
from http.client import HTTPException
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, Request, build_opener
from uuid import UUID

from django.conf import settings

from .generation import PermanentGenerationError
from .llm_http import NoRedirect, http_deadline
from .validation import is_allowed_reply_url


MAX_RESPONSE_BYTES = 64 * 1024


def _retry_after(headers):
    try:
        return min(3600, max(0, int(headers.get("Retry-After", "0"))))
    except (ValueError, TypeError):
        return 0


def deliver_reply(row):
    from .delivery import PermanentDeliveryError, TransientDeliveryError

    if (
        not isinstance(row.reply_url, str) or not isinstance(row.ticket_id, str)
        or not is_allowed_reply_url(row.reply_url, row.ticket_id)
    ):
        raise PermanentDeliveryError("Reply URL is not an allowed helpdesk destination.")
    if not isinstance(row.approved_text, str) or not row.approved_text.strip():
        raise PermanentDeliveryError("No frozen approved reply is available.")
    try:
        key = str(row.delivery_key)
        UUID(key)
        body = json.dumps({
            "ticket_id": row.ticket_id, "message": row.approved_text,
        }, ensure_ascii=False).encode("utf-8")
    except (ValueError, UnicodeError, TypeError):
        raise PermanentDeliveryError("Invalid approved reply or delivery key.") from None
    request = Request(row.reply_url, data=body, method="POST", headers={
        "Content-Type": "application/json",
        "Idempotency-Key": key,
        "User-Agent": "MetricNest-Helpdesk/1.0",
    })
    # Trusted origins/DNS are part of the allowlist's trust boundary. Proxies and
    # redirects must not route an approved destination to an arbitrary address.
    opener = build_opener(ProxyHandler({}), NoRedirect())
    try:
        with http_deadline(settings.DELIVERY_HTTP_TIMEOUT):
            try:
                response = opener.open(request, timeout=settings.DELIVERY_HTTP_TIMEOUT)
            except HTTPError as error:
                response = error
            with response:
                content = response.read(MAX_RESPONSE_BYTES + 1)
                length = response.headers.get("Content-Length")
                if len(content) > MAX_RESPONSE_BYTES or (
                    length is not None and len(content) != int(length)
                ):
                    raise TransientDeliveryError("Incomplete or oversized helpdesk HTTP response.")
                status = response.code
                if status == 201:
                    return
                if status in {408, 429} or 500 <= status <= 599:
                    raise TransientDeliveryError(
                        f"Helpdesk temporary HTTP {status}.",
                        retry_after=_retry_after(response.headers),
                    )
                # A 409 signals a mismatched payload under an existing key.
                # Other 4xx, redirects and unexpected 2xx also require attention.
                raise PermanentDeliveryError(f"Helpdesk rejected delivery: HTTP {status}.")
    except PermanentGenerationError:
        raise PermanentDeliveryError("Delivery HTTP deadline requires Linux prefork/main thread.") from None
    except (URLError, OSError, HTTPException, ValueError):
        # Never persist response bodies or arbitrary exception text.
        raise TransientDeliveryError("Helpdesk network error or HTTP deadline exceeded.") from None
