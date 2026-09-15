"""Bulk admin changes that go through the model, and leave a record of who made them.

`queryset.update()` skips `save()`, every signal and every validator, and writes nothing
to Django's admin log. So a bulk privilege change left no trace of who did it, and
activating a `user_type='admin'` account in bulk never gave it `is_staff` — the rule
lives in `CustomUser.save()` — so it silently could not reach the admin at all.

These helpers lived in admin_dashboard/admin.py beside a `CustomUserAdmin` that used them
correctly and that no admin site ever registered, while the class that IS registered, in
users/admin.py, kept calling `.update()`. Moving them here lets both import them without
a circular import.
"""
import logging

logger = logging.getLogger(__name__)


def log_admin_action(request, obj, message):
    """Record one change in Django's admin log. Never breaks the action itself."""
    from django.contrib.admin.models import CHANGE, LogEntry
    from django.contrib.contenttypes.models import ContentType

    try:
        LogEntry.objects.log_action(
            user_id=request.user.pk,
            content_type_id=ContentType.objects.get_for_model(obj.__class__).pk,
            object_id=obj.pk,
            object_repr=str(obj),
            action_flag=CHANGE,
            change_message=message,
        )
    except Exception:
        logger.warning("could not write admin log for %r", obj, exc_info=True)


def bulk_apply(request, queryset, message, **fields):
    """Apply field changes one row at a time through `save()`, logging each.

    A FULL save, not `update_fields=`: `CustomUser.save()` derives `is_staff` from the
    role, and restricting the write to the changed columns would drop exactly that.
    """
    count = 0
    for obj in queryset:
        for name, value in fields.items():
            setattr(obj, name, value)
        obj.save()
        log_admin_action(request, obj, message)
        count += 1
    return count
