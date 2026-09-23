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
    try:
        cache = public_cache()
        try:
            new_version = cache.incr(f"CACHE_VERSION_{model_name}")
            logger.info(f"Incremented {model_name} cache version to {new_version}")
        except ValueError:
            # The counter does not exist yet. Not an outage; start it.
            cache.set(f"CACHE_VERSION_{model_name}", 2, None)
            logger.info(f"Initialized {model_name} cache version to 2")
    except Exception:
        # Only `ValueError` was caught, so a Redis outage propagated out of a
        # `post_save` receiver and failed the WRITE. Every save of a FoodItem,
        # FoodCategory, Exercise, RoutineTemplate, SubscriptionPlan or Achievement
        # 500'd while Redis was down — including the FoodItem writes on the diet
        # generation path, which aborted the plan mid-transaction. Invalidation is an
        # optimisation and must never be able to fail the operation that triggered it;
        # the receiver ten lines below already knew that. Stale entries expire on TTL.
        logger.warning("Could not bump the %s cache version; entries will expire on TTL",
                       model_name, exc_info=True)

def increment_model_cache_version(sender, instance, **kwargs):
    """
    Invalidate cached responses by bumping the version embedded in their cache key.

    The tracked models come from training_platform/cache_config.MODEL_VERSION_KEYS, the
    same registry the middleware reads. Previously this list was maintained by hand and
    bumped counters (e.g. CACHE_VERSION_EXERCISE) that no cache key ever consulted,
    because the middleware matched paths that did not exist.
    """
    from training_platform.cache_config import MODEL_VERSION_KEYS
    # Keyed by "app_label.ModelName", not the bare class name: `Achievement` exists in
    # both `achievements` and `social`, so a write to either bumped the same counter and
    # flushed the achievements cache for a model that does not feed it.
    version_key = MODEL_VERSION_KEYS.get(sender._meta.label)
    if version_key:
        bump_cache_version(version_key)


def blank_translation_is_absent_not_empty(sender, instance, **kwargs):
    """An empty translated column is NULL, not "".

    modeltranslation propagates `unique=True` from the base field onto every language
    column, so `Recipe.name_ar` and `FoodCategory.name_ar` are UNIQUE and nullable. An
    admin form renders them as ordinary text inputs and submits "" when left blank —
    and Postgres allows any number of NULLs but only one empty string. The SECOND dish
    an admin added without an Arabic name was a 500.

    Normalised here rather than in each model's `save()`, because the set of translated
    fields is a registry and the fix should follow it.
    """
    try:
        from modeltranslation.translator import translator

        options = translator.get_options_for_model(sender)
    except Exception:
        return
    for lang_fields in getattr(options, "all_fields", {}).values():
        for lang_field in lang_fields:
            name = lang_field.name
            if getattr(lang_field, "unique", False) and getattr(instance, name, None) == "":
                setattr(instance, name, None)


def recent_progress_cache_keys(user_id) -> list:
    """Every key the home week can be cached under for one person.

    The payload carries translated exercise names, so the key carries the language
    (routine/views.py `RecentActivityProgressView`). A bust that deleted one key left
    the other language serving a week that had already changed.
    """
    from django.conf import settings

    codes = {code for code, _name in getattr(settings, 'LANGUAGES', ())}
    codes.add(getattr(settings, 'LANGUAGE_CODE', 'ar'))
    return [f"recent_progress:{user_id}:{code}" for code in sorted(codes)]


def bust_home(user_id) -> None:
    """Retire this person's cached home screens (`GET /api/v1/home`).

    The home payload is keyed by build as well as by user and language — it must be,
    because what a build may be shown differs — and the version in that key is whatever
    the installed app sends, so the keys cannot be listed and deleted. Instead the key
    carries a counter, and bumping it orphans every entry at once
    (`home/views.py cache_key`). Same idea as `bump_cache_version`, per person.

    Called from the receivers below, inside their guarded `on_commit` bust: like every
    invalidation here it is an optimisation and must never fail the training it follows.
    """
    from home.views import bump_stamp

    bump_stamp(user_id)


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
                private_cache().delete_many(recent_progress_cache_keys(instance.user_id))
                bust_home(instance.user_id)
                logger.info("Busted recent_progress cache for user %s", instance.user_id)
            except Exception:
                logger.warning(
                    "Could not bust recent_progress cache for user %s; it will expire on TTL",
                    instance.user_id, exc_info=True,
                )

        _txn.on_commit(_bust)
        return


@receiver(post_save, sender='routine.DayOutcome')
def bust_recent_progress_cache_on_day_outcome(sender, instance, **kwargs):
    """The home screen's week carries the adherence block, so closing a day must show
    there at once — not up to 120 s later. Same guarded, deferred bust as above:
    cache invalidation must never fail the training it describes."""
    from django.db import transaction as _txn

    def _bust():
        try:
            from training_platform.cache import private_cache
            private_cache().delete_many(recent_progress_cache_keys(instance.user_id))
            bust_home(instance.user_id)
        except Exception:
            logger.warning(
                "Could not bust recent_progress cache for user %s; it will expire on TTL",
                instance.user_id, exc_info=True,
            )

    _txn.on_commit(_bust)


@receiver(post_save, sender='chat.Message')
def bust_home_on_a_new_message(sender, instance, created, **kwargs):
    """A coach's message is a home section (`trainer_message`), so it must not wait out
    the home cache's two minutes. Only the home entry is retired — the week is not
    about chat — and only for the CLIENT, whose screen carries the card.

    Same guarded, deferred bust as above: a cache that cannot be reached must never
    fail the message it describes."""
    if not created:
        return
    from django.db import transaction as _txn

    def _bust():
        try:
            from chat.models import Conversation

            client_id = (Conversation.objects
                         .filter(pk=instance.conversation_id)
                         .values_list('client_id', flat=True).first())
            if client_id:
                bust_home(client_id)
        except Exception:
            logger.warning(
                "Could not bust the home cache for conversation %s; it will expire on TTL",
                instance.conversation_id, exc_info=True,
            )

    _txn.on_commit(_bust)


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
    'mediakit',
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


def delete_replaced_files_on_update(sender, instance, **kwargs):
    if instance.pk is None:
        return
    field_names = _file_fields(sender)
    # A save limited by `update_fields` writes only those columns, so a file field left
    # out of it keeps its stored value and its file is still referenced. Comparing it
    # anyway deleted the file a STALE instance no longer knew about (SIDE_BUGS S-064).
    update_fields = kwargs.get('update_fields')
    if update_fields is not None:
        field_names = [name for name in field_names if name in update_fields]
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


# ---------------------------------------------------------------------------
# Connecting: to NAMED senders, never to every model
# ---------------------------------------------------------------------------
# The four receivers above used to be registered with no `sender`, which means Django
# calls them for every row saved or deleted anywhere. That is a cost on every write, and
# a much larger one on deletes: `Collector.can_fast_delete()` returns False whenever
# `post_delete.has_listeners(model)` is true, so a single senderless `post_delete`
# receiver switched off the fast path for the WHOLE platform. Deleting one user then
# materialised every related row across some fifty models into memory.
#
# Each of them applies to a knowable set of models, so each is connected to that set.


def _models_with_a_file_field():
    from django.apps import apps
    from django.db.models import FileField

    return [model for model in apps.get_models()
            if any(isinstance(f, FileField) for f in model._meta.get_fields())]


def _translated_models_with_a_unique_column():
    try:
        from modeltranslation.translator import translator
    except Exception:
        return []
    out = []
    for model in translator.get_registered_models(abstract=False):
        options = translator.get_options_for_model(model)
        if any(getattr(f, "unique", False)
               for fields in getattr(options, "all_fields", {}).values()
               for f in fields):
            out.append(model)
    return out


def connect_model_signals():
    """Wire the receivers above to the models they actually concern. Idempotent."""
    from django.apps import apps

    from training_platform.cache_config import MODEL_VERSION_KEYS

    for label in MODEL_VERSION_KEYS:
        try:
            model = apps.get_model(*label.split("."))
        except LookupError:
            logger.error("cache_config names %s, which is not a model", label)
            continue
        post_save.connect(increment_model_cache_version, sender=model,
                          dispatch_uid=f"cache-version-save-{label}")
        post_delete.connect(increment_model_cache_version, sender=model,
                            dispatch_uid=f"cache-version-delete-{label}")

    for model in _translated_models_with_a_unique_column():
        pre_save.connect(blank_translation_is_absent_not_empty, sender=model,
                         dispatch_uid=f"blank-translation-{model._meta.label}")

    for model in _models_with_a_file_field():
        label = model._meta.label
        post_delete.connect(delete_orphaned_files_on_delete, sender=model,
                            dispatch_uid=f"orphan-files-{label}")
        pre_save.connect(delete_replaced_files_on_update, sender=model,
                         dispatch_uid=f"replaced-files-{label}")


connect_model_signals()
