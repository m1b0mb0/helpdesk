import logging

from celery.beat import Scheduler
from django.conf import settings
from django.db import close_old_connections

from .processing import reconcile_generation


logger = logging.getLogger(__name__)


class DatabaseReconciliationScheduler(Scheduler):
    """Run the small DB reconciler in Beat; enqueue only generation work."""

    def tick(self, *args, **kwargs):
        close_old_connections()
        try:
            published = reconcile_generation()
            if published:
                logger.info("Published %s generation tasks.", published)
        except Exception:
            logger.exception("Reconciliation failed; persisted work will be retried.")
        finally:
            close_old_connections()
        return settings.GENERATION_RECONCILE_INTERVAL
