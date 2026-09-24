"""Keep an exercise's ownership and its visibility changing together.

`Exercise.is_global` is derived from `created_by`: an exercise either belongs to the
platform (no owner, visible to everyone) or to a trainer (owned, private to them), and
a check constraint holds the pair together so the two can never disagree the way they
used to.

`Exercise.created_by` is `on_delete=SET_NULL`, and Django implements that as a bare
`UPDATE routine_exercise SET created_by_id = NULL`. No `save()`, so the derivation in
`Exercise.save()` never runs, so the row lands with no owner and `is_global` still
false — which the constraint rejects. Deleting a trainer therefore fails with an
`IntegrityError` naming a constraint the caller has never heard of.

Nothing hits this today only because `Wallet.owner` is PROTECT and every user gets a
wallet, so no user can be deleted at all. That is two problems masking each other, and
the day the first one moves the second one surfaces.

Handing the exercises to the platform *before* Django's own SET_NULL pass sets both
columns in one statement, which satisfies the constraint and leaves Django nothing to
do. It is also the right outcome: an exercise whose author is gone belongs to the
platform, not to nobody.
"""
import logging

from django.conf import settings
from django.db.models.signals import pre_delete
from django.dispatch import receiver

logger = logging.getLogger(__name__)


@receiver(pre_delete, sender=settings.AUTH_USER_MODEL,
          dispatch_uid="routine.adopt_exercises_before_owner_is_deleted")
def adopt_exercises_before_owner_is_deleted(sender, instance, **kwargs):
    from routine.models import Exercise

    adopted = Exercise.objects.filter(created_by=instance).update(
        created_by=None, is_global=True
    )
    if adopted:
        logger.info(
            "Adopted %d exercise(s) into the platform catalogue from deleted user %s",
            adopted, instance.pk,
        )


# ------------------------------------------------------ template usage (B-108)
def _bump_template_cache():
    from training_platform.signals import bump_cache_version

    bump_cache_version("ROUTINETEMPLATE")


def template_usage_saved(sender, instance, **kwargs):
    """`templates/` is served from the response cache, versioned by template writes
    (training_platform/cache_config.py), and `used_by_count` is not a template column:
    it moves when a routine generated from a template is written, deleted, or gains or
    loses a client. Without this a trainer who had just generated a client's program
    read the old count for up to the cache's five minutes. Only routines that came from
    a template bump it."""
    if getattr(instance, "source_template_id", None) is not None:
        _bump_template_cache()


def template_usage_assigned(sender, instance, action, reverse, pk_set, **kwargs):
    if action not in ("post_add", "post_remove", "post_clear"):
        return
    from routine.models import Routine

    if not reverse:
        if getattr(instance, "source_template_id", None) is not None:
            _bump_template_cache()
    elif action == "post_clear" or Routine.objects.filter(
            pk__in=pk_set or (), source_template__isnull=False).exists():
        _bump_template_cache()


def connect_template_usage():
    from django.db.models.signals import m2m_changed, post_delete, post_save

    from routine.models import Routine

    post_save.connect(template_usage_saved, sender=Routine,
                      dispatch_uid="routine.template_usage_saved")
    post_delete.connect(template_usage_saved, sender=Routine,
                        dispatch_uid="routine.template_usage_deleted")
    m2m_changed.connect(template_usage_assigned, sender=Routine.assigned_to.through,
                        dispatch_uid="routine.template_usage_assigned")


connect_template_usage()
