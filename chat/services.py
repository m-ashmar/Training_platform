"""Everything that CHANGES a conversation, in one module.

The REST routes and the WebSocket consumer are two doors into these functions. Neither
one implements a rule: the pair check, the idempotent replay, the fan-out and the push
all live here, so a message sent over a socket and a message sent over HTTP are the same
event with the same side effects. A photo, voice note or video (`send_media_message`,
B-091) runs the same checks in the same order and ends in the same `_announce`, and so
does a trainer's PROGRAM CARD (`kind: "program"` on the text send, `_send_program`,
2026-09-27): one send path, one replay, one pair rule, one fan-out.
"""
import logging
import uuid as _uuid

from django.db import IntegrityError, transaction
from django.utils import timezone
from django.utils.translation import gettext as _
from rest_framework.exceptions import PermissionDenied, ValidationError

from .events import broadcast, forget_preview, notify_new_message
from .exceptions import IdempotencyConflict, MessagingRefused
from .models import Conversation, Message
from .permissions import may_message, pair_of
from .serializers import MessageSerializer

logger = logging.getLogger(__name__)

#: How much history one page may ask for.
PAGE_DEFAULT, PAGE_MAX = 30, 100

#: What `may_message` answers that is a coaching state, and is raised as the same 403
#: the training endpoints raise — one vocabulary for the app, not two.
COACHING_REFUSALS = ('coaching_frozen', 'coaching_unpaid', 'coaching_ended')


def require_may_message(sender, recipient):
    """Raise the 403 `chat.permissions.may_message` finds; nothing when it allows."""
    from users.coaching import raise_refusal

    code = may_message(sender, recipient)
    if code is None:
        return
    if code in COACHING_REFUSALS:
        pair = pair_of(sender, recipient)
        raise_refusal(code, pair[0].pk if pair else None)
    raise MessagingRefused(code)


def open_conversation(trainer, client) -> Conversation:
    """The pair's one conversation, created if this is the first time.

    Race-safe by the unique constraint rather than by get_or_create's read: two phones
    tapping "message" at once produce one row, and the loser reads it back.
    """
    conversation, _created = Conversation.objects.get_or_create(
        trainer=trainer, client=client)
    return conversation


def replay(conversation, sender, client_uuid):
    """The message this id already wrote, or None.

    A retry whose first attempt DID land (the answer was lost) must not post twice. An
    id already used by the OTHER person in this thread is a 409 and says nothing about
    their message — the same shape as the routine set-log replay.
    """
    if client_uuid is None:
        return None
    found = Message.objects.filter(conversation=conversation,
                                   client_uuid=client_uuid).first()
    if found is None:
        return None
    if found.sender_id != sender.pk:
        raise IdempotencyConflict()
    return found


def parse_client_uuid(raw):
    """The app's id as DRF's UUIDField reads it, or None. A malformed one is a 400
    whose top-level `code` is `invalid_client_uuid`, with the same code on
    `field_errors.client_uuid`.

    DRF's ValidationError, because the socket catches `APIException` and nothing else
    (`chat/consumers.py`). DRF's alone reached the response as `code:
    validation_error` — the exception handler names a DRF ValidationError by its
    `service_code` — so the code the contract documents never reached the app on the
    text route or the media route (review 2026-09-24). `service_code` carries it.
    """
    if raw in (None, ''):
        return None
    try:
        return (_uuid.UUID(int=raw) if isinstance(raw, int) and not isinstance(raw, bool)
                else _uuid.UUID(str(raw)))
    except (ValueError, TypeError, AttributeError):
        refused = ValidationError({'client_uuid': [_('Not a valid UUID.')]},
                                  code='invalid_client_uuid')
        refused.service_code = 'invalid_client_uuid'
        raise refused


def _refused(code, message, field):
    """A 400 whose top-level `code` is `code`, with the same code on
    `field_errors.<field>`: DRF's ValidationError, because the socket catches
    `APIException` and nothing else, carrying `service_code` so the code reaches the
    envelope (see `parse_client_uuid`)."""
    refused = ValidationError({field: [message]}, code=code)
    refused.service_code = code
    return refused


def kind_of(data):
    """What the text send is asked to write: `text` (the default, and what an absent or
    empty `kind` means) or `program`. Anything else is 400 `invalid_kind` — a photo,
    voice note or video has its own route (`POST /api/v1/chat/media/`)."""
    from .models import KIND_PROGRAM, KIND_TEXT

    raw = data.get('kind') if hasattr(data, 'get') else None
    if raw in (None, '', KIND_TEXT):
        return KIND_TEXT
    if raw == KIND_PROGRAM:
        return KIND_PROGRAM
    raise _refused('invalid_kind',
                   _('This route sends text and program cards. Send photos, voice notes '
                     'and videos to /api/v1/chat/media/.'), 'kind')


def send_message(conversation, sender, data):
    """Write one message and tell everyone. Returns `(message, created)`.

    `data` is the raw request body: `{body, client_uuid?}` for text, or `{kind:
    "program", routine_id | template_id, day?, body?, client_uuid?}` for a trainer's
    program card (`_send_program`). Validation, the pair check and the replay all happen
    here, the replay first — an idempotent retry of a message that landed while the
    coaching was still paid is ANSWERED, not refused, exactly as a replayed set log is.
    """
    from .models import KIND_PROGRAM

    recipient_id = conversation.other_id(sender.pk)
    if recipient_id is None:
        # Not a member. Callers 404 before this; belt and braces for the socket.
        raise PermissionDenied(_('This conversation is not yours.'), code='not_found')

    client_uuid = parse_client_uuid(
        data.get('client_uuid') if hasattr(data, 'get') else None)
    replayed = replay(conversation, sender, client_uuid)
    if replayed is not None:
        return replayed, False

    if kind_of(data) == KIND_PROGRAM:
        return _send_program(conversation, sender, data, client_uuid)

    serializer = MessageSerializer(data=data)
    serializer.is_valid(raise_exception=True)

    recipient = (conversation.client if sender.pk == conversation.trainer_id
                 else conversation.trainer)
    require_may_message(sender, recipient)

    try:
        with transaction.atomic():
            message = Message.objects.create(
                conversation=conversation, sender=sender,
                body=serializer.validated_data['body'], client_uuid=client_uuid)
            _moved(conversation, message)
    except IntegrityError:
        # Two attempts raced and the partial unique index let one through.
        first = replay(conversation, sender, client_uuid)
        if first is None:
            raise
        return first, False

    _announce(conversation, message)
    return message, True


def _send_program(conversation, sender, data, client_uuid):
    """Write a trainer's program card and tell everyone. Returns `(message, True)`, or
    the first card on a lost-answer race of the same `client_uuid`.

    In the order it is decided, after the membership check, the `client_uuid` and the
    replay that `send_message` ran:

        403 `program_trainer_only`         the sender is the thread's client
        400 `validation_error`             a field is malformed (`field_errors` names it)
        400 `program_target_required`      neither or both of `routine_id`, `template_id`
        400 `unknown_program`              no routine / template of THIS trainer's by
                                           that id (someone else's answers the same)
        400 `program_not_for_this_client`  the trainer's routine, but this client is not
                                           on it; or the trainer's template, and this
                                           client has no routine written from it
        400 `day_out_of_range`             `day` is past the routine's `days`
        403 the pair rule                  `no_relation`, `blocked`, `coaching_frozen`,
                                           `coaching_unpaid`, `coaching_ended` — exactly
                                           as a text send
    """
    from .models import KIND_PROGRAM
    from .serializers import ProgramCardSerializer

    if sender.pk != conversation.trainer_id:
        raise PermissionDenied(_('Only the trainer sends program cards.'),
                               code='program_trainer_only')
    form = ProgramCardSerializer(data=data)
    form.is_valid(raise_exception=True)
    values = form.validated_data
    if (values.get('routine_id') is None) == (values.get('template_id') is None):
        raise _refused('program_target_required',
                       _('Name exactly one of routine_id or template_id.'), 'routine_id')
    client = conversation.client
    routine = resolve_program(sender, client, routine_id=values.get('routine_id'),
                              template_id=values.get('template_id'))
    day = values.get('day')
    if day is not None and day > routine.days:
        raise _refused('day_out_of_range',
                       _('This routine has %(days)s days.') % {'days': routine.days},
                       'day')

    require_may_message(sender, client)

    try:
        with transaction.atomic():
            message = Message.objects.create(
                conversation=conversation, sender=sender, kind=KIND_PROGRAM,
                body=values.get('body') or '', client_uuid=client_uuid,
                routine=routine, program_day=day,
                program_changes=program_summary(conversation, routine, day))
            _moved(conversation, message)
    except IntegrityError:
        first = replay(conversation, sender, client_uuid)
        if first is None:
            raise
        return first, False

    _announce(conversation, message)
    return message, True


def resolve_program(trainer, client, *, routine_id=None, template_id=None):
    """The routine a program card names: one `trainer` wrote that `client` is on.

    By `routine_id`: that routine, if the trainer wrote it (`created_by`) and the client
    is on it (`assigned_to`) — the client's private copy (`routine/assignment.py`).

    By `template_id`: a template the trainer OWNS, resolved to the client's own routine
    written from it (`source_template`), the newest active one first. The card always
    opens a ROUTINE the client reads; a template is only a way for the trainer to name
    it. A template the client has no copy of is `program_not_for_this_client`: the
    trainer delivers it first (`templates/{id}/generate/`).

    A routine or template of another trainer's answers `unknown_program`, like an id
    that does not exist. A routine product the client bought is theirs, not the
    trainer's (`Routine.purchase`), so it cannot be named either.
    """
    from routine.models import Routine, RoutineTemplate

    if routine_id is not None:
        # The whole row: the card served right after the send reads its name, day
        # names and template, and a deferred field is a query each.
        routine = Routine.objects.filter(pk=routine_id, created_by=trainer).first()
        if routine is None:
            raise _refused('unknown_program', _('There is no such program of yours.'),
                           'routine_id')
        if not routine.assigned_to.filter(pk=client.pk).exists():
            raise _refused('program_not_for_this_client',
                           _('This client is not on that routine.'), 'routine_id')
        return routine

    if not RoutineTemplate.objects.filter(pk=template_id, created_by=trainer).exists():
        raise _refused('unknown_program', _('There is no such program of yours.'),
                       'template_id')
    routine = (Routine.objects
               .filter(source_template_id=template_id, created_by=trainer,
                       assigned_to=client)
               .order_by('-is_active', '-created_at', '-id').first())
    if routine is None:
        raise _refused('program_not_for_this_client',
                       _('This client has no routine written from that template yet.'),
                       'template_id')
    return routine


def program_summary(conversation, routine, day):
    """What a card announces about `routine` (its `day`, or the whole routine when
    `day` is None), written once when it is sent:

        {"exercises": 6,                     exercise rows on that day now
         "changes": null | {                 since the previous card for this routine
             "since": "<that card's time>",  in this thread (a taken-back one does not
             "added": 1,                     count): rows written after it,
             "edited": 2,                    rows edited after it,
             "removed": 1 | null}}           rows gone since (null when the two cards
                                             are about different days)

    `changes` is null on the first card for a routine. Every routine-exercise write
    goes through `save()`, which stamps `created_at` / `updated_at`, so the two counts
    are exact; a removal leaves no row, so `removed` is worked out from the two cards'
    `exercises` (a row moved onto the day counts as edited, one moved off as removed).
    Two queries, both on indexes: the previous card, and one aggregate.
    """
    from django.db.models import Count, Q

    from routine.models import RoutineExercise

    from .models import KIND_PROGRAM

    rows = RoutineExercise.objects.filter(routine=routine)
    if day is not None:
        rows = rows.filter(day=day)
    previous = (Message.objects
                .filter(conversation=conversation, kind=KIND_PROGRAM, routine=routine,
                        deleted_at__isnull=True)
                .order_by('-id')
                .values('created_at', 'program_day', 'program_changes').first())
    if previous is None:
        return {'exercises': rows.count(), 'changes': None}
    since = previous['created_at']
    counts = rows.aggregate(
        exercises=Count('pk'),
        added=Count('pk', filter=Q(created_at__gt=since)),
        edited=Count('pk', filter=Q(created_at__lte=since, updated_at__gt=since)))
    before = (previous['program_changes'] or {}).get('exercises')
    removed = None
    if previous['program_day'] == day and isinstance(before, int):
        removed = max(0, before + counts['added'] - counts['exercises'])
    return {'exercises': counts['exercises'],
            'changes': {'since': since.isoformat(), 'added': counts['added'],
                        'edited': counts['edited'], 'removed': removed}}


def _moved(conversation, message):
    """The list is ordered by `last_message_at`, so it moves with every message —
    including one that is later taken back: the thread DID have activity."""
    Conversation.objects.filter(pk=conversation.pk).update(
        last_message_at=message.created_at)
    conversation.last_message_at = message.created_at


def frame_payload(message, *, checked=False) -> dict:
    """The `message` object of a socket frame — `message.new` and `message.updated`
    alike: the object REST returns, `media`, `exercise` and `program` included.

    Serialized without a request, so on the filesystem backend its `media.url` is
    relative to the API host (a GCS URL is absolute either way). Both people in the
    group are the conversation's members, checked at connect, and ONE frame serves them
    both, so a program card's `program` is decided for the pair — never left to
    `get_program`'s answer for "no reader", which is null (BACKEND_BUGS B-150: every
    `message.updated` of a card said «لم يعد متاحاً» until the next read):

      * `checked=True` — the send that wrote the card has just checked that the trainer
        wrote the routine and the client is on it (`resolve_program`), so both may read
        it and nothing is asked (`_announce`, `message.new`);
      * otherwise — a frame about a message sent earlier (`message.updated`: a poster
        cut, a duration measured) — the routine is asked of BOTH members through
        `readable_routine_ids`, the rule a read of the thread applies, and the card
        rides along only if both may still read it. A routine taken off the client
        since then leaves this frame's card null for both, as the client's own read
        does; the trainer's next read draws it again. Three small queries, and only for
        a program card: a photo, voice note or video costs none.
    """
    from .serializers import readable_routine_ids

    context = {}
    if message.routine_id is not None:
        if checked:
            readable = {message.routine_id}
        else:
            pair = (Conversation.objects.select_related('trainer', 'client')
                    .filter(pk=message.conversation_id).first())
            readable = (readable_routine_ids(pair.trainer, [message])
                        & readable_routine_ids(pair.client, [message])
                        if pair is not None else set())
        context['readable_routines'] = readable
    return dict(MessageSerializer(message, context=context).data)


def _announce(conversation, message):
    """The socket fan-out and the push, for any kind of message. Same object as REST
    (`frame_payload`): a program card goes out WITH its card, which the send has just
    checked both people in the group may read."""
    broadcast(conversation.pk, 'message.new',
              {'message': frame_payload(message, checked=True)})
    notify_new_message(message)


def send_media_message(sender, data, upload):
    """Write one photo, voice note or video and tell everyone. Returns
    `(message, created)`.

    `data` is the multipart form: `{conversation, kind, body?, exercise_id?,
    client_uuid?}`; `upload` is the file. The order is the text send's, with the file
    LAST: membership (404), the replay (a retry answers the first message and stores
    nothing), the form (400), the pair rule (403 — the same `may_message` as text), the
    sender's daily media quota (429 `media_quota_exceeded`, from the upload's size
    alone: `chat.media.require_media_quota`), and only then the bytes, so a refused
    sender costs no decode and no storage. The file and the row are written together;
    if the row cannot be written, the file is removed again (`chat.media.discard`), so
    a refusal or a race never leaves bytes that nothing points at. A video's poster
    frame is not cut here: it is queued for a worker once the row commits
    (`chat.media.queue_poster`, 2026-09-27), so the request never decodes a frame; nor
    is the length of a voice note or video whose container declared none — a worker
    measures it (`chat.media.queue_duration`, BACKEND_BUGS B-177).

    Raises DRF exceptions for the refusals the text send shares and for the quota, and
    Django's `ValidationError` (with its `code`) for the upload's own — the API's
    exception handler renders both, the latter keeping its code (BACKEND_BUGS B-149).
    """
    from rest_framework.exceptions import NotFound

    from . import media
    from .models import KIND_VIDEO, KIND_VOICE
    from .serializers import MediaMessageSerializer

    raw = data if hasattr(data, 'get') else {}
    try:
        conversation_id = int(raw.get('conversation'))
    except (TypeError, ValueError):
        conversation_id = None
    conversation = (Conversation.objects.select_related('trainer', 'client')
                    .filter(pk=conversation_id).first() if conversation_id else None)
    if conversation is None or conversation.other_id(sender.pk) is None:
        # The same 404 as the thread's own routes: a 403 would confirm it exists.
        raise NotFound(_('Conversation not found.'), code='not_found')

    client_uuid = parse_client_uuid(raw.get('client_uuid'))
    replayed = replay(conversation, sender, client_uuid)
    if replayed is not None:
        return replayed, False

    form = MediaMessageSerializer(data=raw)
    form.is_valid(raise_exception=True)
    kind = form.validated_data['kind']
    exercise = _exercise_for(sender, kind, form.validated_data.get('exercise_id'))

    recipient = (conversation.client if sender.pk == conversation.trainer_id
                 else conversation.trainer)
    require_may_message(sender, recipient)

    media.require_media_quota(sender, upload)
    prepared = media.prepare(kind, upload)
    message = Message(conversation=conversation, sender=sender, kind=kind,
                      body=form.validated_data.get('body') or '', exercise=exercise,
                      client_uuid=client_uuid)
    try:
        media.store(message, prepared)
        with transaction.atomic():
            message.save()
            _moved(conversation, message)
    except IntegrityError:
        media.discard(message)
        first = replay(conversation, sender, client_uuid)
        if first is None:
            raise
        return first, False
    except Exception:
        media.discard(message)
        raise

    _announce(conversation, message)
    message_id = message.pk
    if kind == KIND_VIDEO:
        # The poster frame is a worker's job, never this request's: queued once the
        # row is committed (at once here, where nothing wraps the send in a wider
        # transaction), after `message.new` went out with `poster_url: null`. The
        # worker announces the frame with `message.updated` (`chat.tasks`).
        transaction.on_commit(lambda: media.queue_poster(message_id))
    if kind in (KIND_VOICE, KIND_VIDEO) and message.duration_seconds is None:
        # A container that declared no length (a WebM recorded live): a worker measures
        # it with ffprobe once the row commits, and announces it the same way
        # (`chat.tasks.fill_media_duration`, BACKEND_BUGS B-177).
        transaction.on_commit(lambda: media.queue_duration(message_id))
    return message, True


def _exercise_for(sender, kind, exercise_id):
    """The exercise a video names, resolved as the SENDER may see it, or None.

    Only a video names a move (`exercise_only_on_video`), and only one the sender may
    read (`routine.permissions.visible_exercises_for`): otherwise a client could put a
    trainer's private exercise name in front of another trainer. An id the sender may
    not see answers exactly like one that does not exist (`unknown_exercise`).
    """
    from django.core.exceptions import ValidationError as DjangoValidationError

    from .models import KIND_VIDEO

    if exercise_id is None:
        return None
    if kind != KIND_VIDEO:
        raise DjangoValidationError(_('Only a video can name an exercise.'),
                                    code='exercise_only_on_video')
    from routine.permissions import visible_exercises_for

    exercise = visible_exercises_for(sender).filter(pk=exercise_id).first()
    if exercise is None:
        raise DjangoValidationError(_('There is no such exercise.'),
                                    code='unknown_exercise')
    return exercise


def mark_read(conversation, reader):
    """Mark everything up to now read, and answer with what is left.

    Stamps the reader's own `..._last_read_at` (which is what the unread count is
    measured from) AND `read_at` on each message they had not read, which is what the
    other side's ticks are drawn from.
    """
    field = conversation.read_field_for(reader.pk)
    if field is None:
        raise PermissionDenied(_('This conversation is not yours.'), code='not_found')
    now = timezone.now()
    with transaction.atomic():
        Conversation.objects.filter(pk=conversation.pk).update(**{field: now})
        setattr(conversation, field, now)
        (Message.objects.filter(conversation=conversation, read_at__isnull=True)
         .exclude(sender=reader).update(read_at=now))
        # `.update()` fires no signal, and the home card carries this unread count.
        from training_platform.signals import bust_home_for_a_read
        bust_home_for_a_read(conversation, reader.pk)
    broadcast(conversation.pk, 'message.read',
              {'conversation': conversation.pk, 'reader': reader.pk,
               'read_at': now.isoformat()})
    # Anything that arrived while the update ran is genuinely still unread.
    remaining = (Message.objects
                 .filter(conversation=conversation, deleted_at__isnull=True,
                         created_at__gt=now)
                 .exclude(sender=reader).count())
    return remaining, now


def tombstone(message, actor):
    """The sender takes their message back. The row stays; the text stops being served.

    Only the sender (403 `not_your_message`), and taking back a message that is already
    gone changes nothing and answers the same tombstone — a retry of a delete is not an
    error. The recipient's inbox stops quoting it too (`forget_preview`); the listener
    that writes the inbox row checks the tombstone as well, for a push still queued.
    """
    if message.sender_id != actor.pk:
        raise PermissionDenied(_('You can only delete your own messages.'),
                               code='not_your_message')
    if message.deleted_at is None:
        message.deleted_at = timezone.now()
        message.save(update_fields=['deleted_at'])
        broadcast(message.conversation_id, 'message.deleted',
                  {'conversation': message.conversation_id, 'message': message.pk,
                   'deleted_at': message.deleted_at.isoformat()})
        forget_preview(message)
    return message


def history(conversation, *, before=None, limit=None):
    """A page of the thread, newest first. Returns `(rows, next_before)`.

    Cursored by id and not by time: two messages can share a timestamp to the
    microsecond, and a cursor on a non-unique column repeats and hides rows. Tombstones
    are RETURNED (with no text) so the app keeps a stable list rather than watching a
    gap appear under its scroll.

    `next_before` is the cursor for the next page, and None once the thread has been
    read back to its first message. One row beyond the page is fetched to decide that,
    so the app never asks for a page that turns out to be empty.
    """
    try:
        size = int(limit) if limit not in (None, '') else PAGE_DEFAULT
    except (TypeError, ValueError):
        raise ValidationError({'limit': [_('Not a number.')]}, code='invalid_limit')
    if size < 1 or size > PAGE_MAX:
        raise ValidationError({'limit': [_('Out of range.')]}, code='invalid_limit')

    # `exercise` joined: a form check names its move, and reading it per row would be a
    # query per video on the page. `routine` likewise: a program card names its routine.
    rows = (Message.objects.filter(conversation=conversation)
            .select_related('exercise', 'routine'))
    if before not in (None, ''):
        try:
            rows = rows.filter(pk__lt=int(before))
        except (TypeError, ValueError):
            raise ValidationError({'before': [_('Not a message id.')]},
                                  code='invalid_cursor')
    page = list(rows.order_by('-id')[:size + 1])
    return page[:size], (page[size - 1].pk if len(page) > size else None)
