import logging
from django.db.models.signals import post_save, post_delete
from django.dispatch import receiver
from training_platform.cache import public_cache

logger = logging.getLogger(__name__)

def bump_cache_version(model_name):
    """
    Atomically increments a global integer in DB2 (public_cache).
    This acts as a dynamic hash suffix. Instead of O(N) delete_pattern() sweeps,
    all HTTP CacheMiddleware checks immediately miss and regenerate payload on model change.
    """
    cache = public_cache()
    try:
        new_version = cache.incr(f"CACHE_VERSION_{model_name}")
        logger.info(f"Incremented {model_name} cache version to {new_version}")
    except ValueError:
        cache.set(f"CACHE_VERSION_{model_name}", 2, None)
        logger.info(f"Initialized {model_name} cache version to 2")

@receiver([post_save, post_delete])
def increment_model_cache_version(sender, instance, **kwargs):
    tracked_models = ['RoutineTemplate', 'Achievement', 'SubscriptionPlan', 'Exercise']
    if sender.__name__ in tracked_models:
        bump_cache_version(sender.__name__.upper())


@receiver(post_save, sender='routine.WorkoutSession')
def bust_recent_progress_cache(sender, instance, **kwargs):
    """
    Bust per-user recent_progress cache in private_cache (DB3) when a
    WorkoutSession transitions to 'completed'. This ensures the dashboard
    chart reflects completed workouts immediately after the next request,
    rather than waiting up to 120s for TTL expiry.
    """
    if instance.status == 'completed' and instance.end_time is not None:
        from training_platform.cache_backends import private_cache
        cache_key = f"recent_progress:{instance.user_id}"
        private_cache().delete(cache_key)
        logger.info(f"Busted recent_progress cache for user {instance.user_id}")

