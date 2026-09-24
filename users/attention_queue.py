"""The trainer's home is a QUEUE (BACKEND_BUGS B-091; design `12-trainer-shell/TrainerHome`).

"Not a dashboard: a queue. The server returns an ordered list of things needing
attention (new request, form-check video, idle client, program ending), each with its
own actions." Until this module the app composed that list itself from four reads —
`auth/trainer/pending-requests/`, `routine/trainer/client-progress/attention/`, the
roster and every routine's `end_date` — and form checks did not exist. Those four reads
are unchanged; this is one more, built from the same definitions:

    new_request      a pending request the CLIENT sent (`TrainerClientRelation`)
    form_check       a client's video in the chat that names an exercise
                     (`chat.Message`, B-091 part 1), until the trainer writes after it
    idle_client      `routine.outcomes.attention` says `quiet` (nothing trained for
                     `QUIET_DAYS`, or never) or `never_started`
    slipping_client  `attention` says `slipping` — this week well under their own
                     four-week average — and the client is not idle
    program_ending   an active routine this trainer wrote for the client ends within
                     `PROGRAM_ENDING_WITHIN_DAYS`

Who can appear: an APPROVED client of this trainer (`routine.permissions.
accessible_user_ids`, the one predicate — never the `assigned_trainer` pointer), whose
account is active (`users.visibility.public_q`: a suspended account is not shown) and
with no block between the two (`moderation.services.blocked_ids`, either direction).
A request needs only the last two: its client is not approved yet. A client whose
coaching the platform refuses (frozen, unpaid, ended — `users.coaching.refusals_for`,
read inside `attention`) contributes NOTHING except a request: the chat refuses both
sides while the coaching is paused, so a form check could never be answered, and the
freeze rule says their routines cannot be used; the old attention read still lists
them, with the reason.
"""
from __future__ import annotations

import math
from datetime import datetime, time, timedelta

from django.db.models import Exists, F, OuterRef
from django.utils import timezone
from django.utils.translation import gettext as _
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

#: The one priority order, first to last. Within a kind, see `_sort_key`.
KIND_ORDER = ('new_request', 'form_check', 'idle_client', 'slipping_client',
              'program_ending')

#: A program counts as ending from this many days before its `end_date`. The app's own
#: composer used 7 (`QueueComposer.endingWithinDays`) before this endpoint existed, so
#: moving the queue to the server changes nothing a trainer sees. The owner's 3-day
#: reminder is the CLIENT's notice that a paid month ends; a coach needs longer to write
#: the next programme.
PROGRAM_ENDING_WITHIN_DAYS = 7


# --------------------------------------------------------------------------- rows
def _request_row(relation) -> dict:
    """A `new_request` item's `new_request` object: the pending-requests row.

    MERGE NOTE: `TrainerPendingRequestsView` (`users/views.py`) builds this row inline
    today, and a parallel branch (`fx/trainer-rows`) extracts it as
    `users.trainer_requests.pending_request_row(relation, request)` and adds the
    client's v2 answers and the quoted fee to it. Point this one adapter at that
    function and both land together; nothing else here builds a request row.

    This reproduces the view's row for a request the CLIENT sent to a trainer neither
    of them has blocked — the only kind the queue shows — which is the long shape.
    """
    client = relation.client
    return {
        'request_id': relation.id,
        'client_id': client.id,
        'client_name': client.full_name or client.username,
        'client_username': client.username,
        'initiated_by': relation.initiated_by,
        'created_at': relation.created_at,
        'client_email': client.email,
        'client_profile_picture': (client.profile_picture.url
                                   if client.profile_picture else None),
        'client_age': client.age,
        'client_gender': client.gender,
        'client_goals': client.client_goals,
        'client_activity_level': client.activity_level,
        'requested_at': relation.requested_at or relation.created_at,
        'status': relation.status,
        'note': relation.client_note,
    }


def _client_card(user, request) -> dict:
    """The person on a queue card: the chat's person card plus the display name every
    trainer read uses (`full_name`, else `username`). `profile_picture` is absolute."""
    from chat.serializers import person_card

    return {**person_card(user, request), 'name': user.full_name or user.username}


def _program(routine, reader) -> dict | None:
    if routine is None:
        return None
    from routine.services import routine_name_for

    return {'id': routine.pk, 'name': routine_name_for(routine, reader)}


def _midnight(day) -> datetime:
    """The start of `day` in the server's zone — when a date-based item became due."""
    return timezone.make_aware(datetime.combine(day, time.min))


# --------------------------------------------------------------------------- build
def build_queue(trainer, *, request=None, today=None) -> list[dict]:
    """Every item needing this trainer's attention, in the one order (see the view).

    A bounded number of queries whatever the roster's size (about seventeen): nothing
    below reads per client or per item.
    """
    from chat.media import payload as media_payload
    from chat.models import KIND_VIDEO, Message
    from moderation.services import blocked_ids
    from routine.models import Routine, WorkoutSession
    from routine.outcomes import QUIET_DAYS, attention
    from routine.permissions import accessible_user_ids

    from .models import CustomUser, TrainerClientRelation
    from .visibility import public_q

    today = today or timezone.localdate()
    hidden = blocked_ids(trainer)
    items = []

    # --- new requests: the client asked, is active, and no block stands between.
    pending = (TrainerClientRelation.objects
               .filter(trainer=trainer, status='pending', initiated_by='client')
               .filter(public_q('client'))
               .exclude(client_id__in=hidden)
               .select_related('client')
               .order_by('created_at', 'id'))
    for relation in pending:
        items.append({
            'kind': 'new_request',
            'id': f'new_request:{relation.pk}',
            'client': _client_card(relation.client, request),
            'at': relation.requested_at or relation.created_at,
            'new_request': _request_row(relation),
        })

    # --- the roster: approved (the one predicate), active, not blocked.
    ids = accessible_user_ids(trainer) or set()
    ids = {i for i in ids if i != trainer.pk} - hidden
    clients = {c.pk: c for c in (CustomUser.objects
                                 .filter(pk__in=ids, user_type='client')
                                 .filter(public_q()))}
    rows = attention(trainer, sorted(clients), today=today) if clients else []
    by_client = {row['client_id']: row for row in rows}
    refused = {row['client_id'] for row in rows
               if any(r['code'] in ('frozen', 'unpaid', 'ended') for r in row['reasons'])}
    usable = sorted(set(clients) - refused)

    # Every active routine this trainer wrote for a usable client, in one query: the
    # idle and slipping cards name the programme, and the ending ones are among these.
    through = Routine.assigned_to.through
    person = Routine.assigned_to.field.m2m_reverse_field_name()
    links = (through.objects
             .filter(**{f'{person}_id__in': usable})
             .filter(routine__created_by=trainer, routine__is_active=True)
             .select_related('routine')
             .order_by(f'{person}_id', '-routine__start_date', '-routine_id'))
    current, ending = {}, []
    horizon = today + timedelta(days=PROGRAM_ENDING_WITHIN_DAYS)
    for link in links:
        client_id, routine = getattr(link, f'{person}_id'), link.routine
        current.setdefault(client_id, routine)
        if routine.end_date and today <= routine.end_date <= horizon:
            ending.append((client_id, routine))

    # --- form checks: a usable client's video naming a move, not taken back, with no
    # message from the trainer AFTER it in that thread (a reply the trainer took back
    # does not count: the client has no answer).
    answered = Message.objects.filter(conversation=OuterRef('conversation'),
                                      sender_id=trainer.pk, deleted_at__isnull=True,
                                      pk__gt=OuterRef('pk'))
    checks = (Message.objects
              .filter(conversation__trainer=trainer,
                      conversation__client_id__in=usable,
                      kind=KIND_VIDEO, exercise__isnull=False, deleted_at__isnull=True,
                      sender_id=F('conversation__client_id'))
              .filter(~Exists(answered))
              .select_related('exercise')
              .order_by('created_at', 'id'))
    for message in checks:
        client = clients[message.sender_id]
        items.append({
            'kind': 'form_check',
            'id': f'form_check:{message.pk}',
            'client': _client_card(client, request),
            'at': message.created_at,
            'form_check': {
                'message_id': message.pk,
                'conversation_id': message.conversation_id,
                # The instance, not `.values()`: modeltranslation serves the name in the
                # reader's language only through the attribute.
                'exercise': {'id': message.exercise.pk, 'name': message.exercise.name},
                'caption': message.body,
                'duration_seconds': message.duration_seconds,
                'sent_at': message.created_at,
                # The trainer is one of the two people in this thread: the signed,
                # expiring URL the thread itself would hand them.
                'media': media_payload(message, request),
            },
        })

    # --- idle and slipping: one card per client, idle first when both are true.
    idle, slipping = [], []
    for client_id in usable:
        row = by_client.get(client_id)
        if row is None:
            continue
        codes = {r['code']: r for r in row['reasons']}
        if 'never_started' in codes or 'quiet' in codes:
            idle.append((client_id, row, codes))
        elif 'slipping' in codes:
            slipping.append((client_id, row, codes))

    last_sessions = {}
    if idle:
        # The newest finished session of each idle client, in one query (DISTINCT ON).
        for session in (WorkoutSession.objects
                        .filter(user_id__in=[c for c, _r, _c in idle], status='completed')
                        .select_related('routine')
                        .order_by('user_id', F('end_time').desc(nulls_last=True), '-id')
                        .distinct('user_id')):
            last_sessions[session.user_id] = session
    never = [c for c, row, _codes in idle if row['last_trained'] is None]
    began = {}
    if never:
        # When the coaching began: the moment a client who never trained became due.
        began = {rel['client_id']: rel['responded_at'] or rel['created_at']
                 for rel in (TrainerClientRelation.objects
                             .filter(trainer=trainer, status='approved',
                                     client_id__in=never)
                             .values('client_id', 'responded_at', 'created_at'))}

    for client_id, row, codes in idle:
        last_on = row['last_trained']
        session = last_sessions.get(client_id)
        items.append({
            'kind': 'idle_client',
            'id': f'idle_client:{client_id}',
            'client': _client_card(clients[client_id], request),
            'at': (_midnight(last_on + timedelta(days=QUIET_DAYS)) if last_on
                   else began.get(client_id) or _midnight(today)),
            'idle_client': {
                'reason': 'never_started' if 'never_started' in codes else 'quiet',
                'days': (today - last_on).days if last_on else None,
                'last_trained_on': last_on,
                'last_workout': ({'at': session.end_time or session.start_time,
                                  'day': session.day,
                                  'routine': _program(session.routine, trainer)}
                                 if session else None),
                'program': _program(current.get(client_id), trainer),
            },
            '_score': row['score'],
        })

    for client_id, row, codes in slipping:
        week = row['this_week']
        drop = codes['slipping']['usual'] - codes['slipping']['this_week']
        items.append({
            'kind': 'slipping_client',
            'id': f'slipping_client:{client_id}',
            'client': _client_card(clients[client_id], request),
            'at': _midnight(today),
            'slipping_client': {
                'this_week_percent': codes['slipping']['this_week'],
                'usual_percent': codes['slipping']['usual'],
                'planned_sets': week.get('planned_sets', 0),
                'missed_sets': week.get('missed_sets', 0),
                'last_trained_on': row['last_trained'],
                'program': _program(current.get(client_id), trainer),
            },
            '_drop': drop,
        })

    # --- programmes ending.
    for client_id, routine in ending:
        start, end = routine.start_date, routine.end_date
        weeks = max(1, math.ceil(((end - start).days + 1) / 7)) if start else None
        week = (min(weeks, max(1, (today - start).days // 7 + 1))
                if start and weeks else None)
        items.append({
            'kind': 'program_ending',
            'id': f'program_ending:{routine.pk}:{client_id}',
            'client': _client_card(clients[client_id], request),
            'at': _midnight(end - timedelta(days=PROGRAM_ENDING_WITHIN_DAYS)),
            'program_ending': {
                'routine_id': routine.pk,
                'name': _program(routine, trainer)['name'],
                'week': week,
                'weeks': weeks,
                'end_date': end,
                'days_left': (end - today).days,
            },
        })

    items.sort(key=_sort_key)
    for item in items:
        item.pop('_score', None)
        item.pop('_drop', None)
    return items


def _sort_key(item):
    """The one order. `KIND_ORDER` first; within a kind, the most urgent first:

        new_request      the longest-waiting request first (`at`, then its id)
        form_check       the oldest unanswered video first (`at`, then its id)
        idle_client      `attention`'s own urgency score, highest first — the number
                         the old attention read ranks by — then the longest overdue
                         (`at`), then the client id
        slipping_client  the biggest drop against their own average first, then the
                         client id
        program_ending   the fewest days left first, then the routine id, then the
                         client id

    Every branch ends in a unique id, so the order is total and two calls with the
    same data answer the same list.
    """
    rank = KIND_ORDER.index(item['kind'])
    at = item['at'].timestamp()
    kind = item['kind']
    if kind == 'new_request':
        return (rank, at, item['new_request']['request_id'], 0)
    if kind == 'form_check':
        return (rank, at, item['form_check']['message_id'], 0)
    if kind == 'idle_client':
        return (rank, -item['_score'], at, item['client']['id'])
    if kind == 'slipping_client':
        return (rank, -item['_drop'], item['client']['id'], 0)
    detail = item['program_ending']
    return (rank, detail['days_left'], detail['routine_id'], item['client']['id'])


# --------------------------------------------------------------------------- view
class AttentionQueueView(APIView):
    """GET /api/v1/trainer/attention/ — what needs this trainer, most urgent first.

    Trainers only: anyone else — a client, an agent, an admin — answers
    `403 {"code": "not_a_trainer"}`. No query parameters, no body. Nothing here differs
    by build (`X-App-Channel`): it sells nothing and shows no price of its own (a
    request row carries the quoted fee the pending-requests read carries).

    200::

        {"count": 4,
         "items": [
           {"kind": "new_request" | "form_check" | "idle_client" | "slipping_client"
                    | "program_ending",
            "id": "form_check:812",          stable per item: kind + the row's own id(s)
            "client": {"id", "name", "first_name", "last_name", "profile_picture"},
            "at": datetime,                  when the item became due (below)
            "<kind>": {...}}                 the kind's own fields, under its own name
         ]}

    Per kind — `at`, and the object under the kind's name:

    | kind | `at` | fields |
    |---|---|---|
    | `new_request` | the request was sent | the pending-requests row, exactly as `GET /api/auth/trainer/pending-requests/` answers it (`request_id` is what `trainer/respond-to-request/` takes) |
    | `form_check` | the video was sent | `message_id`, `conversation_id`, `exercise` `{id, name}`, `caption`, `duration_seconds` (null when the container declared none), `sent_at`, `media` (the thread's signed, expiring `{url, ...}`) |
    | `idle_client` | the day they became quiet (last trained + `QUIET_DAYS`), or when the coaching began for a client who never trained | `reason` (`quiet` / `never_started`), `days` (null = never trained), `last_trained_on`, `last_workout` `{at, day, routine: {id, name}}` or null, `program` `{id, name}` or null |
    | `slipping_client` | today | `this_week_percent`, `usual_percent` (their own four-week share of planned sets), `planned_sets`, `missed_sets`, `last_trained_on`, `program` |
    | `program_ending` | `PROGRAM_ENDING_WITHIN_DAYS` (7) before `end_date` | `routine_id`, `name`, `week`, `weeks` (week N of M, from `start_date` / `end_date`), `end_date`, `days_left` |

    THE ORDER — one, total: `new_request`, `form_check`, `idle_client`,
    `slipping_client`, `program_ending`; within a kind the most urgent first — the
    longest-waiting request, the oldest unanswered video, the idle client with the
    highest `attention` urgency score (then the longest overdue), the biggest drop
    against their own average, the programme with the fewest days left — and every tie
    broken by a unique id (`_sort_key`).

    WHO appears: this trainer's APPROVED clients (`accessible_user_ids`), never a
    suspended account (`users.visibility`), never a pair where either has blocked the
    other; a request from any active client not blocked. A client whose coaching is
    frozen, unpaid or ended appears in none of the kinds (the chat refuses both sides
    while it lasts, and their routines cannot be used); `GET
    /api/routine/trainer/client-progress/attention/` still lists them, with the reason.
    A client is idle OR slipping, never both: idle wins. `idle` and `slipping` are
    `routine.outcomes.attention`'s own `quiet` / `never_started` and `slipping`, with
    its threshold (`QUIET_DAYS`, 5), so the two reads cannot disagree.

    A FORM CHECK is a video the CLIENT sent in the chat with an `exercise_id`
    (`POST /api/v1/chat/media/`). It leaves the queue as soon as the trainer sends any
    message in that conversation after it; a reply the trainer then took back does not
    count. A video the client took back leaves with it.

    The four older reads (pending requests, attention, recent activity, routines) are
    unchanged. A bounded number of queries whatever the roster's size.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        if not request.user.is_trainer:
            return Response({'error': _('This endpoint is only for trainers'),
                             'code': 'not_a_trainer'},
                            status=status.HTTP_403_FORBIDDEN)
        items = build_queue(request.user, request=request)
        return Response({'count': len(items), 'items': items})
