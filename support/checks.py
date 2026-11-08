from urllib.parse import urlsplit

from django.conf import settings
from django.core.checks import Error, register


@register()
def check_generation_configuration(app_configs, **kwargs):
    positive = (
        "GENERATION_RECONCILE_INTERVAL", "GENERATION_MAX_INFLIGHT",
        "GENERATION_MAX_ATTEMPTS", "GENERATION_MAX_DISPATCHES",
        "GENERATION_QUEUE_LEASE_SECONDS", "GENERATION_LEASE_SECONDS",
        "GENERATION_RETRY_BASE_SECONDS", "GENERATION_RETRY_MAX_SECONDS",
    )
    errors = [
        Error(f"{name} must be positive.", id="support.E001")
        for name in positive if getattr(settings, name) <= 0
    ]
    if not (
        0 < settings.CELERY_TASK_SOFT_TIME_LIMIT
        < settings.CELERY_TASK_TIME_LIMIT
        < settings.GENERATION_LEASE_SECONDS
    ):
        errors.append(Error(
            "Require soft task limit < hard task limit < generation lease.",
            id="support.E002",
        ))
    if settings.CELERY_VISIBILITY_TIMEOUT <= (
        settings.GENERATION_QUEUE_LEASE_SECONDS + settings.CELERY_TASK_TIME_LIMIT
    ):
        errors.append(Error(
            "Redis visibility timeout must exceed queue lease plus hard task limit.",
            id="support.E003",
        ))
    if not (
        settings.CELERY_TASK_ACKS_LATE
        and settings.CELERY_TASK_REJECT_ON_WORKER_LOST
        and settings.CELERY_WORKER_PREFETCH_MULTIPLIER == 1
        and settings.CELERY_WORKER_CANCEL_LONG_RUNNING_TASKS_ON_CONNECTION_LOSS
    ):
        errors.append(Error("Generation requires late ACK, worker-loss rejection, prefetch 1 and cancellation on broker loss.", id="support.E004"))
    if not (
        0 < settings.LLM_HTTP_TIMEOUT <= 20
        and 5 <= settings.LLM_DEMO_DELAY_SECONDS <= 25
        and max(settings.LLM_HTTP_TIMEOUT, settings.LLM_DEMO_DELAY_SECONDS) + 5
        < settings.CELERY_TASK_SOFT_TIME_LIMIT
    ):
        errors.append(Error(
            "Require HTTP timeout <=20s, demo delay 5..25s and 5s margin before soft task limit.",
            id="support.E005",
        ))
    try:
        origin = urlsplit(settings.PUBLIC_BASE_URL)
        valid_origin = (
            origin.scheme in {"http", "https"} and origin.hostname
            and not origin.username and not origin.password
            and origin.path in {"", "/"} and not origin.query and not origin.fragment
            and not any(c.isspace() for c in settings.PUBLIC_BASE_URL)
        )
        origin.port  # Reject malformed ports too.
    except ValueError:
        valid_origin = False
    if not valid_origin:
        errors.append(Error("PUBLIC_BASE_URL must be a public HTTP(S) origin.", id="support.E006"))
    return errors
