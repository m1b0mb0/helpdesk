from celery import shared_task
from django.contrib.auth import get_user_model
from django.db import connection

from .processing import run_generation


@shared_task
def check_database():
    """Read through the ORM to verify worker-to-PostgreSQL connectivity."""
    return {
        "database": connection.vendor,
        "user_count": get_user_model().objects.count(),
    }


@shared_task
def generate_reply(request_id, dispatch_token):
    return run_generation(request_id, dispatch_token)
