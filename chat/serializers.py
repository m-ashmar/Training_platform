"""What the chat puts on the wire.

One `MessageSerializer` serves the REST route AND the socket, because a message that
arrives over a socket must be the same object the app just read from a page of history —
two serializers would drift on the first field anyone added.
"""
from django.utils.translation import gettext_lazy as _
from rest_framework import serializers

from .models import (KIND_TEXT, MAX_BODY_CHARS, MEDIA_KINDS, Conversation, Message)


#: Kept for importers: the kind every message had before attachments (B-091).
MESSAGE_KIND_TEXT = KIND_TEXT


class MessageSerializer(serializers.ModelSerializer):
    """One message.

    `body` is capped at 4000 characters and is otherwise stored exactly as it was typed:
    nothing here trims, folds or rewrites a person's words. A text message that is only
    whitespace is refused (`blank_message`) rather than silently turned into an empty
    bubble. On a photo, voice note or video `body` is the caption and may be empty.

    A tombstoned message keeps its place in the thread and loses its text AND its media:
    `deleted` is true, `body` is empty and `media` is null, whoever is reading. The
    column and the file still hold what was sent — the row is a record, not a display —
    and no endpoint serves either.

    `kind` says what the message IS — `text`, `image`, `voice` or `video` — so the app
    switches on it before reading anything else, and draws a value it does not know as
    a placeholder rather than guessing from which fields happen to be present
    (BACKEND_BUGS B-042). The media fields are ADDITIVE (B-091) and present on every
    message:

        "media": null | {"url": "<signed, expires within the hour>",
                         "duration_seconds": 18.4 | null,   voice, video
                         "width": 1080 | null, "height": 1350 | null},   image
        "exercise": null | {"id": 12, "name": "Deadlift"}   the move a video shows

    A CLIENT's video with an `exercise` is a form check: it sits in the trainer's
    attention queue (`GET /api/v1/trainer/attention/`) until the trainer writes in the
    thread after it. `media.url` is private: it appears only in answers the two people
    in this conversation receive, it is signed, and it expires — use it, never store it.
    """
    body = serializers.CharField(max_length=MAX_BODY_CHARS, allow_blank=False,
                                 trim_whitespace=False)
    deleted = serializers.SerializerMethodField()
    media = serializers.SerializerMethodField()
    exercise = serializers.SerializerMethodField()

    class Meta:
        model = Message
        fields = ['id', 'conversation', 'sender', 'kind', 'body', 'media', 'exercise',
                  'client_uuid', 'created_at', 'read_at', 'deleted_at', 'deleted']
        # `client_uuid` is read-only HERE and written by `chat.services.send_message`:
        # the id is the key of an idempotent replay, not a field, and the replay has to
        # run before validation — a retry of a message sent while the coaching was
        # still paid must be ANSWERED, not refused.
        read_only_fields = ['id', 'conversation', 'sender', 'kind', 'media', 'exercise',
                            'client_uuid', 'created_at', 'read_at', 'deleted_at',
                            'deleted']

    def get_deleted(self, obj):
        return obj.deleted_at is not None

    def get_media(self, obj):
        from .media import payload

        return payload(obj, self.context.get('request'))

    def get_exercise(self, obj):
        if obj.exercise_id is None or obj.deleted_at is not None:
            return None
        exercise = obj.exercise
        # Read off the instance, not `.values()`: modeltranslation serves the name in the
        # active language only through the attribute.
        return {'id': exercise.pk, 'name': exercise.name}

    def validate_body(self, value):
        if not value.strip():
            raise serializers.ValidationError(_('Write something first.'),
                                              code='blank_message')
        return value

    def to_representation(self, instance):
        data = super().to_representation(instance)
        if instance.deleted_at is not None:
            data['body'] = ''
        return data


class MediaMessageSerializer(serializers.Serializer):
    """The form fields of `POST /api/v1/chat/media/` other than the file itself.

    `body` is the caption: optional, capped like a text message, stored as typed.
    `exercise_id` is accepted on a video only (`exercise_only_on_video`).
    """
    conversation = serializers.IntegerField()
    kind = serializers.ChoiceField(choices=list(MEDIA_KINDS))
    body = serializers.CharField(max_length=MAX_BODY_CHARS, allow_blank=True,
                                 required=False, default='', trim_whitespace=False)
    exercise_id = serializers.IntegerField(required=False, allow_null=True, default=None)


def person_card(user, request):
    """The other side of a thread, as the list and the header show it."""
    picture = None
    if getattr(user, 'profile_picture', None):
        picture = (request.build_absolute_uri(user.profile_picture.url) if request
                   else user.profile_picture.url)
    return {'id': user.pk, 'first_name': user.first_name, 'last_name': user.last_name,
            'profile_picture': picture}


def conversation_card(conversation, *, user, request, last_message, unread_count,
                      refusal):
    """One row of the conversation list.

    `can_send` and `refusal` are here so the app can explain a closed composer without
    first posting a message and reading a 403 — a frozen coaching is readable, and the
    screen should say why it cannot be written to.
    """
    other = (conversation.client if user.pk == conversation.trainer_id
             else conversation.trainer)
    preview = None
    if last_message is not None:
        preview = {
            'id': last_message.pk,
            'sender': last_message.sender_id,
            # What the last message IS, so the row can say "Photo" or "Voice note"
            # under an empty caption. Never its media: the list is not the thread.
            'kind': last_message.kind,
            'body': '' if last_message.deleted_at else last_message.body[:200],
            'deleted': last_message.deleted_at is not None,
            'created_at': last_message.created_at,
        }
    return {
        'id': conversation.pk,
        'other': person_card(other, request),
        'last_message': preview,
        'last_message_at': conversation.last_message_at,
        'unread_count': unread_count,
        'can_send': refusal is None,
        'refusal': refusal,
        'created_at': conversation.created_at,
    }


def conversation_cards(conversations, *, user, request):
    """A whole page of rows, in a fixed number of queries whatever the page size.

    The two things a row needs beyond the conversation itself — the last message and
    whether the composer is open — are each read ONCE for the page: the messages by id
    (the ids come back annotated on the page query) and the refusals in bulk
    (`chat/permissions.py`). Nothing in here touches the database per row.
    """
    from .permissions import refusals_for_conversations

    rows = list(conversations)
    refusals = refusals_for_conversations(user, rows)
    wanted = [c.last_message_id for c in rows if getattr(c, 'last_message_id', None)]
    messages = {m.pk: m for m in Message.objects.filter(pk__in=wanted)} if wanted else {}
    return [
        conversation_card(
            c, user=user, request=request,
            last_message=messages.get(getattr(c, 'last_message_id', None)),
            unread_count=getattr(c, 'unread_count', 0),
            refusal=refusals.get(c.pk),
        )
        for c in rows
    ]


def conversation_queryset(user):
    """This caller's conversations, annotated with everything a row needs.

    Both annotations are SUBQUERIES rather than a join with a filtered aggregate: an
    aggregate would put the outer query under a GROUP BY that the last-message subquery
    then has to appear in, and the count would be paid for on rows the page never shows.
    As subqueries the page costs one query whatever its size.

    A message the sender took back is not unread — nobody should be asked to go and read
    a tombstone — and neither is one's own message.
    """
    from django.db.models import Count, IntegerField, OuterRef, Subquery, Value
    from django.db.models.functions import Coalesce

    mine = 'trainer' if user.is_trainer else 'client'
    newest = (Message.objects.filter(conversation=OuterRef('pk'))
              .order_by('-created_at', '-id').values('pk')[:1])
    unread = (Message.objects
              .filter(conversation=OuterRef('pk'), deleted_at__isnull=True,
                      created_at__gt=OuterRef(f'{mine}_last_read_at'))
              .exclude(sender=user)
              .order_by().values('conversation').annotate(n=Count('pk')).values('n'))
    return (Conversation.objects
            .filter(**{mine: user})
            .select_related('trainer', 'client')
            .annotate(
                last_message_id=Subquery(newest),
                unread_count=Coalesce(Subquery(unread, output_field=IntegerField()),
                                      Value(0)),
            ))
