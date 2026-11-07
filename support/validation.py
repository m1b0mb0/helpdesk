from urllib.parse import quote, urlsplit

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import validate_email


PAYLOAD_LIMITS = {
    "event_id": 255,
    "ticket_id": 255,
    "customer_email": 254,
    "message": 10000,
    "reply_url": 2048,
}


class InvalidPayload(ValueError):
    def __init__(self, errors):
        self.errors = errors
        super().__init__("Invalid webhook payload.")


def _parse_url(value):
    if any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in value):
        return None
    if "\\" in value or "?" in value or "#" in value:
        return None
    try:
        parsed = urlsplit(value)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.netloc.endswith(":")
        ):
            return None
        parsed.hostname.encode("ascii")
        port = parsed.port
    except (ValueError, UnicodeError):
        return None
    origin = (
        parsed.scheme,
        parsed.hostname.lower(),
        port if port is not None else (443 if parsed.scheme == "https" else 80),
    )
    return parsed, origin


def is_allowed_reply_url(value, ticket_id):
    """Check a trusted origin and exact reply path without DNS or network I/O."""
    result = _parse_url(value)
    if result is None:
        return False
    parsed, origin = result
    expected_path = f"/tickets/{quote(ticket_id, safe='')}/replies"
    if parsed.path != expected_path or any(
        segment in {".", ".."} for segment in parsed.path.split("/")
    ):
        return False
    for allowed in settings.HELPDESK_ALLOWED_ORIGINS:
        configured = _parse_url(allowed)
        if configured is not None:
            configured_url, configured_origin = configured
            if configured_url.path in {"", "/"} and origin == configured_origin:
                return True
    return False


def validate_webhook_payload(payload):
    """Validate all fields while preserving their exact original string values."""
    if not isinstance(payload, dict):
        raise InvalidPayload({"__all__": "Expected a JSON object."})

    errors = {}
    if payload.keys() - PAYLOAD_LIMITS.keys():
        errors["__all__"] = "Unknown fields are not allowed."
    for field, limit in PAYLOAD_LIMITS.items():
        if field not in payload:
            errors[field] = "This field is required."
            continue
        value = payload[field]
        if not isinstance(value, str):
            errors[field] = "Expected a string."
        elif not value.strip():
            errors[field] = "This field must not be blank."
        elif len(value) > limit:
            errors[field] = f"Must contain at most {limit} characters."
        elif "\x00" in value:
            errors[field] = "NUL characters are not allowed."
        else:
            try:
                value.encode("utf-8")
            except UnicodeEncodeError:
                errors[field] = "Must contain valid Unicode text."

    if errors:
        raise InvalidPayload(errors)
    try:
        validate_email(payload["customer_email"])
    except ValidationError:
        errors["customer_email"] = "Enter a valid email address."
    if not is_allowed_reply_url(payload["reply_url"], payload["ticket_id"]):
        errors["reply_url"] = "Use an allowed helpdesk origin and the ticket reply path."
    if errors:
        raise InvalidPayload(errors)
    return dict(payload)
