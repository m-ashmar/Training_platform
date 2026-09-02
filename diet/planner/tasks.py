"""Scheduled learning."""
import logging

from celery import shared_task

logger = logging.getLogger(__name__)


@shared_task(
    name="diet.planner.refresh_food_weights",
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_backoff_max=600,
    retry_jitter=True,
    max_retries=3,
)
def refresh_food_weights():
    """Nightly: turn what users actually ate into planner ranking weights."""
    from django.contrib.auth import get_user_model

    from .learning import update_weights

    User = get_user_model()
    total = 0
    for user in User.objects.filter(is_active=True).iterator(chunk_size=500):
        try:
            total += len(update_weights(user))
        except Exception:
            logger.exception("Weight refresh failed for user %s", user.pk)
    return f"adjusted {total} food weights"
