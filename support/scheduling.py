import logging

from celery.beat import Scheduler
from django.conf import settings
from django.db import close_old_connections

from .processing import reconcile_generation
from .delivery import reconcile_delivery


logger = logging.getLogger(__name__)


class DatabaseReconciliationScheduler(Scheduler):
    """Poll both PostgreSQL workflows; never queue periodic reconciler messages."""

    def tick(self, *args, **kwargs):
        for name, reconcile in (
            ("Generation", reconcile_generation), ("Delivery", reconcile_delivery),
        ):
            close_old_connections()
            try:
                published = reconcile()
                if published:
                    logger.info("Published %s %s tasks.", published, name.lower())
            except Exception:
                logger.exception("%s reconciliation failed; persisted work will be retried.", name)
            finally:
                close_old_connections()
        return settings.GENERATION_RECONCILE_INTERVAL
