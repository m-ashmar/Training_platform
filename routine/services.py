import uuid

from django.db import transaction

from .tasks import send_async_notification

#: Routine notifications that may legitimately happen again for the same object. A
#: trainer re-assigning the same routine (new sets, a new block) is a new event and must
#: reach the client again. Each emission gets its own event_id, which is what the
#: notification store deduplicates on, so only a redelivery of THIS emission is
#: suppressed. Keyed on the routine id instead, the second assignment was dropped by a
#: permanent unique constraint: no push, no unread badge.
#:
#: Everything else stays once per object: a session is completed once, and two racing
#: "finish" requests must not congratulate twice.
REPEATABLE_TYPES = frozenset({"routine_assignment", "custom"})


def send_notification(user, notif_type, message, related_object=None, data=None,
                      context=None):
    """
    Service function to create a notification for a user asynchronously.
    Args:
        user: CustomUser instance (recipient)
        notif_type: str — must be a key of notifications.channels.fcm.EVENT_CLASS_REGISTRY,
            which is also what GET /api/notifications/event-types/ returns
        message: str (notification message)
        related_object: Optional model instance (Routine, WorkoutSession, etc.)
        data: Optional `{key: id}` added to the push's `data` beside `related_object_id`
            and `related_object_type`: the ids the app's page opens with, by name
            (Road to 10 S3.1). Values are sent as strings, as FCM requires.
        context: Optional `{"variant": key, <params>}`: the sentence `message` says, by
            its key on the event's template (`notifications/domain/routine_events.py`
            `variants`), so the inbox renders it in the reader's language and a push in
            the saved one. Stored beside `message`, which stays the fallback.
    Returns:
        None (Async execution)

    Queued on commit: called inside a transaction that later rolls back, the task used
    to tell the client about an assignment or a completion that never happened.
    """
    related_object_id = None
    related_object_type = None
    if related_object is not None:
        related_object_id = related_object.id
        related_object_type = related_object.__class__.__name__
    event_id = str(uuid.uuid4()) if notif_type in REPEATABLE_TYPES else None

    transaction.on_commit(lambda: send_async_notification.delay(
        user.id,
        notif_type,
        message,
        related_object_id,
        related_object_type,
        event_id=event_id,
        extra_data={k: str(v) for k, v in (data or {}).items()} or None,
        context=dict(context) if context else None,
    ))
    return None


def in_recipients_language(user, render):
    """`render()` evaluated in `user`'s own language.

    These messages are rendered once, here, and delivered as they are (the templates
    are `%(message)s`), so rendering them in the request's language — or not at all, as
    English f-strings — sent English to Arabic users (BACKEND_BUGS B-030).
    """
    from django.utils import translation

    with translation.override(getattr(user, 'preferred_language', None) or 'ar'):
        return render()


def routine_name_for(routine, user):
    """The routine's name in `user`'s language: `Routine.translations` for a language
    other than the default, the column otherwise."""
    from django.conf import settings

    language = getattr(user, 'preferred_language', None) or 'ar'
    if language == settings.LANGUAGE_CODE:
        return routine.name
    translated = (routine.translations or {}).get(language, {})
    return (translated.get('name') if isinstance(translated, dict) else None) or routine.name


def notify_assignment(client, routine):
    """"A new routine has been assigned" for `client`, in their language."""
    from django.utils.translation import gettext as _

    params = {'routine': routine_name_for(routine, client)}
    message = in_recipients_language(client, lambda: _(
        "A new routine, %(routine)s, has been assigned to you.") % params)
    send_notification(user=client, notif_type="routine_assignment", message=message,
                      related_object=routine, data={'routine_id': routine.pk},
                      context={'variant': 'assigned', **params})

