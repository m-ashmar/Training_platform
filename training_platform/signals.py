import logging
from django.db.models.signals import post_save, post_delete, pre_save
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
    """
    Invalidate cached responses by bumping the version embedded in their cache key.

    The tracked models come from training_platform/cache_config.MODEL_VERSION_KEYS, the
    same registry the middleware reads. Previously this list was maintained by hand and
    bumped counters (e.g. CACHE_VERSION_EXERCISE) that no cache key ever consulted,
    because the middleware matched paths that did not exist.
    """
    from training_platform.cache_config import MODEL_VERSION_KEYS
    version_key = MODEL_VERSION_KEYS.get(sender.__name__)
    if version_key:
        bump_cache_version(version_key)


@receiver(post_save, sender='routine.WorkoutSession')
def bust_recent_progress_cache(sender, instance, **kwargs):
    """
    Bust per-user recent_progress cache in private_cache (DB3) when a
    WorkoutSession transitions to 'completed'. This ensures the dashboard
    chart reflects completed workouts immediately after the next request,
    rather than waiting up to 120s for TTL expiry.
    """
    if instance.status == 'completed' and instance.end_time is not None:
        # Guarded + deferred: this used to run inline and unprotected, so a Redis
        # outage raised straight out of the user's `save()` and they could not
        # complete a workout at all. Cache invalidation is an optimisation; it must
        # never be able to fail the business operation.
        from django.db import transaction as _txn

        def _bust():
            try:
                from training_platform.cache import private_cache
                private_cache().delete(f"recent_progress:{instance.user_id}")
                logger.info("Busted recent_progress cache for user %s", instance.user_id)
            except Exception:
                logger.warning(
                    "Could not bust recent_progress cache for user %s; it will expire on TTL",
                    instance.user_id, exc_info=True,
                )

        _txn.on_commit(_bust)
        return


# ---------------------------------------------------------------------------
# Orphaned upload cleanup
# ---------------------------------------------------------------------------
# Django never removes the file backing a FileField/ImageField — not when the row
# is deleted, and not when the field is pointed at a new file. On a Fly volume of
# 1 GB with scale-to-zero, every re-uploaded profile picture and every deleted post
# leaked its bytes permanently until the disk filled.
#
# These receivers are registered for ALL models in our own apps rather than
# per-model, so a FileField added to any future model is covered automatically.

LOCAL_APPS_FOR_FILE_CLEANUP = {
    'users', 'wallet', 'diet', 'routine', 'subscription', 'social',
    'ai_assistant', 'achievements', 'analytics', 'notifications', 'challenges',
}

_FILE_FIELD_CACHE = {}


def _file_fields(sender):
    # Memoised: pre_save fires for every model in the project, and the answer for
    # a given model never changes at runtime.
    cached = _FILE_FIELD_CACHE.get(sender)
    if cached is not None:
        return cached
    from django.db.models import FileField
    if getattr(sender, '_meta', None) is None:
        result = []
    elif sender._meta.app_label not in LOCAL_APPS_FOR_FILE_CLEANUP:
        result = []
    else:
        result = [f.name for f in sender._meta.get_fields()
                  if isinstance(f, FileField)]
    _FILE_FIELD_CACHE[sender] = result
    return result


def _delete_after_commit(file_obj, label):
    """Remove the stored file only once the surrounding transaction commits.

    Deleting inline would destroy the bytes even when the save/delete is rolled
    back afterwards, leaving a live row pointing at a file that no longer exists.
    """
    from django.db import transaction

    def _do_delete():
        try:
            file_obj.delete(save=False)
        except Exception as exc:  # storage may be unreachable; never raise here
            logger.warning("Could not delete %s (%s): %s", label, file_obj.name, exc)

    transaction.on_commit(_do_delete)


def _is_shared(sender, field_name, file_name, exclude_pk=None):
    """True if another row still points at the same stored file.

    Fixtures and defaults can legitimately share one path across rows; deleting
    the file for one of them would break the others.
    """
    qs = sender._default_manager.filter(**{field_name: file_name})
    if exclude_pk is not None:
        qs = qs.exclude(pk=exclude_pk)
    return qs.exists()


@receiver(post_delete)
def delete_orphaned_files_on_delete(sender, instance, **kwargs):
    for field_name in _file_fields(sender):
        f = getattr(instance, field_name, None)
        if not f or not getattr(f, 'name', None):
            continue
        try:
            if _is_shared(sender, field_name, f.name, exclude_pk=instance.pk):
                continue
        except Exception as exc:  # never block the delete itself
            logger.warning("Shared-file check failed for %s.%s: %s",
                           sender.__name__, field_name, exc)
            continue
        _delete_after_commit(f, f"{sender.__name__}.{field_name}")


@receiver(pre_save)
def delete_replaced_files_on_update(sender, instance, **kwargs):
    if instance.pk is None:
        return
    field_names = _file_fields(sender)
    if not field_names:
        return
    try:
        previous = sender._default_manager.get(pk=instance.pk)
    except Exception:
        return
    for field_name in field_names:
        old = getattr(previous, field_name, None)
        new = getattr(instance, field_name, None)
        old_name = getattr(old, 'name', None)
        new_name = getattr(new, 'name', None)
        if not old_name or old_name == new_name:
            continue
        try:
            if _is_shared(sender, field_name, old_name, exclude_pk=instance.pk):
                continue
        except Exception as exc:
            logger.warning("Shared-file check failed for %s.%s: %s",
                           sender.__name__, field_name, exc)
            continue
        _delete_after_commit(old, f"replaced {sender.__name__}.{field_name}")
