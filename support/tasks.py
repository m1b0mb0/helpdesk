from celery import shared_task
from django.contrib.auth import get_user_model
from django.db import connection


@shared_task
def check_database():
    """Read through the ORM to verify worker-to-PostgreSQL connectivity."""
    return {
        "database": connection.vendor,
        "user_count": get_user_model().objects.count(),
    }
