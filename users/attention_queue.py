"""The trainer's home is a QUEUE (BACKEND_BUGS B-091; design `12-trainer-shell/TrainerHome`).

"Not a dashboard: a queue. The server returns an ordered list of things needing
attention (new request, form-check video, idle client, program ending), each with its
own actions." Until this module the app composed that list itself from four reads —
`auth/trainer/pending-requests/`, `routine/trainer/client-progress/attention/`, the
roster and every routine's `end_date` — and form checks did not exist. Those four reads
are unchanged; this is one more, built from the same definitions:

    new_request      a pending request the CLIENT sent (`TrainerClientRelation`)
    form_check       a client's video in the chat that names an exercise
                     (`chat.Message`, B-091 part 1), sent since the CURRENT coaching
                     began, until the trainer writes after it
    idle_client      `routine.outcomes.attention` says `quiet` (nothing trained for
                     `QUIET_DAYS`, or never) or `never_started` (assigned a routine
                     and never trained)
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
asked at the moment of the call: the instant the chat's `may_message` asks, not the
whole day `attention` judges a date by) contributes NOTHING except a request: the chat
refuses both sides while the coaching is paused, so a form check could never be
answered, and the freeze rule says their routines cannot be used; the old attention
read still lists them, with the reason.
"""
from __future__ import annotations

import math
from datetime import datetime, time, timedelta

from django.db.models import Exists, F, OuterRef, Subquery
from django.db.models.functions import Coalesce
from django.utils import timezone
from django.utils.translation import gettext as _
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

#: The one priority order, first to last. Within a kind, see `_sort_key`.
KIND_ORDER = ('new_request', 'form_check', 'unanswered_message', 'awaiting_first_program',
              'idle_client', 'slipping_client', 'program_ending')

#: A program counts as ending from this many days before its `end_date`. The app's own
#: composer used 7 (`QueueComposer.endingWithinDays`) before this endpoint existed, so
#: moving the queue to the server changes nothing a trainer sees. The owner's 3-day
#: reminder is the CLIENT's notice that a paid month ends; a coach needs longer to write
#: the next programme.
PROGRAM_ENDING_WITHIN_DAYS = 7


# --------------------------------------------------------------------------- rows
def _request_row(relation, request) -> dict:
    """A `new_request` item's `new_request` object: exactly the row
    `GET /api/auth/trainer/pending-requests/` answers for it — one builder,
    `users/trainer_requests.py pending_request_row` (the quoted fee and the trainer's net,
    null on a build that may not show a coaching fee; the client's v2 answers; never a
    health note before acceptance)."""
    from .trainer_requests import pending_request_row

    return pending_request_row(relation, request)


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

    A bounded number of queries whatever the roster's size (about twenty, measured
    2026-09-24): nothing below reads per client or per item.
    """
    from chat.media import payload as media_payload
    from chat.models import KIND_VIDEO, Message
    from moderation.services import blocked_ids
    from routine.models import Routine, WorkoutSession
    from routine.outcomes import QUIET_DAYS, attention
    from routine.permissions import accessible_user_ids

    from .coaching import refusals_for
    from .models import CustomUser, TrainerClientRelation
    from .visibility import public_q

    now = timezone.now()
    today = today or timezone.localdate(now)
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
            'new_request': _request_row(relation, request),
        })

    # --- the roster: approved (the one predicate), active, not blocked.
    ids = accessible_user_ids(trainer) or set()
    ids = {i for i in ids if i != trainer.pk} - hidden
    clients = {c.pk: c for c in (CustomUser.objects
                                 .filter(pk__in=ids, user_type='client')
                                 .filter(public_q()))}
    # Refused NOW — the instant the chat's `may_message` asks, not the whole day that
    # `attention` judges a date by. A month frozen at 09:00 still "covers" today as a
    # date, so reading the refusal from `attention` queued a form check until midnight
    # whose reply the chat already answered 403 `coaching_frozen`.
    refused = {client_id for client_id, refusal
               in refusals_for(trainer.pk, list(clients)).items() if refusal}
    usable = sorted(set(clients) - refused)
    # `attention` only for what it alone decides: who is idle, who is slipping.
    rows = attention(trainer, usable, today=today) if usable else []
    by_client = {row['client_id']: row for row in rows}

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

    # --- form checks: a usable client's video naming a move, sent during THIS coaching,
    # not taken back, with no message from the trainer AFTER it in that thread (a reply
    # the trainer took back does not count: the client has no answer).
    answered = Message.objects.filter(conversation=OuterRef('conversation'),
                                      sender_id=trainer.pk, deleted_at__isnull=True,
                                      pk__gt=OuterRef('pk'))
    # When the current approved relation began. A pair has one conversation for life, so
    # a video left unanswered when an EARLIER coaching ended (the chat then refused the
    # trainer) would come back as the oldest form check the day the client returns.
    began = (TrainerClientRelation.objects
             .filter(trainer=trainer, client_id=OuterRef('conversation__client_id'),
                     status='approved')
             .annotate(began=Coalesce('responded_at', 'created_at'))
             .order_by('-id')
             .values('began')[:1])
    checks = (Message.objects
              .filter(conversation__trainer=trainer,
                      conversation__client_id__in=usable,
                      kind=KIND_VIDEO, exercise__isnull=False, deleted_at__isnull=True,
                      sender_id=F('conversation__client_id'),
                      created_at__gte=Subquery(began))
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

    # --- unanswered messages (Road to 10 S2.3): a usable client's thread in which the
    # client wrote during THIS coaching and the trainer has not written since — the same
    # `answered` and `began` the form checks use. A form-check video is its own card
    # above and is not counted again; a message the client took back is not waiting.
    waiting = (Message.objects
               .filter(conversation__trainer=trainer,
                       conversation__client_id__in=usable,
                       deleted_at__isnull=True,
                       sender_id=F('conversation__client_id'),
                       created_at__gte=Subquery(began))
               .filter(~Exists(answered))
               .exclude(kind=KIND_VIDEO, exercise__isnull=False)
               .order_by('conversation_id', 'created_at', 'id')
               .values('conversation_id', 'sender_id', 'pk', 'created_at'))
    threads = {}
    for row in waiting:
        thread = threads.setdefault(row['conversation_id'], {'first': row, 'count': 0})
        thread['count'] += 1
    for conversation_id, thread in threads.items():
        first = thread['first']
        items.append({
            'kind': 'unanswered_message',
            'id': f"unanswered_message:{conversation_id}:{first['pk']}",
            'client': _client_card(clients[first['sender_id']], request),
            'at': first['created_at'],
            'unanswered_message': {
                'conversation_id': conversation_id,
                'message_id': first['pk'],
                'count': thread['count'],
                'sent_at': first['created_at'],
            },
        })

    # --- awaiting a first programme (S2.3): the coaching's month 1 is HELD until the
    # first programme arrives (`users/coaching.py`: accept holds it, the first programme
    # pays it out). That client is not idle: the next move is the trainer's. The same
    # rule `coaching_status` answers `awaiting_first_program` by.
    from .models import CoachingPeriod
    from .trainer_directory import CoachingPriceBook

    relations = {relation.client_id: relation for relation in
                 TrainerClientRelation.objects.filter(trainer=trainer, status='approved',
                                                      client_id__in=usable)}
    latest = {}
    for period in (CoachingPeriod.objects
                   .filter(relation_id__in=[r.pk for r in relations.values()])
                   .order_by('relation_id', '-number')
                   .distinct('relation_id')):
        latest[period.relation_id] = period
    awaiting = {}
    for client_id, relation in relations.items():
        period = latest.get(relation.pk)
        if period is not None and period.status == 'held':
            awaiting[client_id] = (relation, period.price, period.currency)
        elif period is None and relation.hold_reference:
            awaiting[client_id] = (relation, relation.agreed_rate,
                                   relation.agreed_currency or None)
    book = CoachingPriceBook(request) if awaiting else None
    for client_id, (relation, amount, currency) in awaiting.items():
        held, held_currency = (book.price(amount, currency) if currency
                               else (None, None))
        items.append({
            'kind': 'awaiting_first_program',
            'id': f'awaiting_first_program:{relation.pk}',
            'client': _client_card(clients[client_id], request),
            'at': relation.responded_at or relation.created_at,
            'awaiting_first_program': {
                'relation_id': relation.pk,
                'since': relation.responded_at or relation.created_at,
                'held_amount': held,
                'held_currency': held_currency,
            },
        })

    # --- idle and slipping: one card per client, idle first when both are true. A client
    # awaiting their first programme is that card instead, never idle.
    idle, slipping = [], []
    for client_id in usable:
        row = by_client.get(client_id)
        if row is None or client_id in awaiting:
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
                                  # The session review opens from it (S2.3).
                                  'session_id': session.pk,
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

    # `at` is when an item BECAME due: never later than the moment of this answer,
    # whatever a date-based rule above computes.
    for item in items:
        item['at'] = min(item['at'], now)
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
    if kind == 'unanswered_message':
        return (rank, at, item['unanswered_message']['message_id'], 0)
    if kind == 'awaiting_first_program':
        return (rank, at, item['awaiting_first_program']['relation_id'], 0)
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
    `403 {"code": "not_a_trainer"}`. No query parameters, no body.

    WHAT DIFFERS BY BUILD (`X-App-Channel`, `X-App-Version`): only the quote on a
    `new_request` row. `agreed_rate`, `agreed_currency`, `trainer_net` and
    `commission_rate` are null — the keys stay — on a build that may not show a coaching
    fee (`policy.services.can_show_price` for `coaching_1to1`: `ios` today, and a request
    that declares no channel). Every other field, every item and the order are the same
    on every build: the queue sells nothing.

    200::

        {"count": 4,
         "items": [
           {"kind": "new_request" | "form_check" | "unanswered_message"
                    | "awaiting_first_program" | "idle_client" | "slipping_client"
                    | "program_ending",
            "id": "form_check:812",          stable per item: kind + the row's own id(s)
            "client": {"id", "name", "first_name", "last_name", "profile_picture"},
            "at": datetime,                  when the item became due (below); never
                                             later than the response
            "<kind>": {...}}                 the kind's own fields, under its own name
         ]}

    Per kind — `at`, and the object under the kind's name:

    | kind | `at` | fields |
    |---|---|---|
    | `new_request` | the request was sent | the pending-requests row, exactly as `GET /api/auth/trainer/pending-requests/` answers a request the CLIENT sent (one builder, `users.trainer_requests.pending_request_row`): `request_id` (what `trainer/respond-to-request/` takes), `client_id`, `client_name`, `client_username`, `initiated_by` (always `client` here), `status` (`pending`), `created_at`, `requested_at`, `note` (the client's own words), `client_email`, `client_profile_picture`, `client_age`, `client_gender`, `client_goals`, `client_activity_level`, the v2 answers `training_goal`, `training_place`, `training_days_per_week` (a code, or "" / null while unanswered), and the QUOTE: `agreed_rate` + `agreed_currency` (the fee quoted with the request, in the trainer's currency when it converts, the unit always beside it), `trainer_net` (that fee less the platform's commission), `commission_rate` (that commission as a fraction, `0.2`) — the four null on a build that may not show a coaching fee (above), the first three also when no fee was quoted. Never a health note: those come after acceptance |
    | `form_check` | the video was sent | `message_id`, `conversation_id`, `exercise` `{id, name}`, `caption`, `duration_seconds` (null when the container declared none), `sent_at`, `media` (the thread's signed, expiring `{url, ...}`) |
    | `unanswered_message` | the first message waiting | (S2.3, 2026-11-22) a thread where the client wrote during this coaching and the trainer has not written since: `conversation_id`, `message_id` (the first one waiting), `count` (messages waiting), `sent_at`. A form-check video is its own `form_check` card and is not counted here |
    | `awaiting_first_program` | the coaching began | (S2.3) month 1 is held until the first programme arrives: `relation_id`, `since`, `held_amount` + `held_currency` (the held month, in the reader's currency when it converts; both null on a build that may not show a coaching fee). Such a client is never `idle_client` |
    | `idle_client` | the day they became quiet (last trained + `QUIET_DAYS`), or when the coaching began for a client who never trained | `reason` (`quiet` / `never_started`), `days` (null = never trained), `last_trained_on`, `last_workout` `{at, session_id, day, routine: {id, name}}` or null (`session_id` S2.3), `program` `{id, name}` or null. `never_started` only for a client who has never trained (`days` null); a client who has trained is idle only after `QUIET_DAYS` without, as `quiet` |
    | `slipping_client` | today | `this_week_percent`, `usual_percent` (their own four-week share of planned sets), `planned_sets`, `missed_sets`, `last_trained_on`, `program` |
    | `program_ending` | `PROGRAM_ENDING_WITHIN_DAYS` (7) before `end_date` | `routine_id`, `name`, `week`, `weeks` (week N of M, from `start_date` / `end_date`), `end_date`, `days_left` |

    THE ORDER — one, total: `new_request`, `form_check`, `unanswered_message`,
    `awaiting_first_program`, `idle_client`, `slipping_client`, `program_ending`; within a
    kind the most urgent first — the longest-waiting request, the oldest unanswered video,
    the oldest unanswered message, the longest-held first month, the idle client with the
    highest `attention` urgency score (then the longest overdue), the biggest drop
    against their own average, the programme with the fewest days left — and every tie
    broken by a unique id (`_sort_key`).

    WHO appears: this trainer's APPROVED clients (`accessible_user_ids`), never a
    suspended account (`users.visibility`), never a pair where either has blocked the
    other; a request from any active client not blocked. A client whose coaching is
    frozen, unpaid or ended at the moment of the call appears in none of the kinds —
    from the minute it froze, the same instant the chat starts refusing both sides
    (`chat.permissions.may_message`), not from the next day — and their routines cannot
    be used; `GET /api/routine/trainer/client-progress/attention/` still lists them,
    with the reason. A client is idle OR slipping, never both: idle wins. `idle` and
    `slipping` are `routine.outcomes.attention`'s own `quiet` / `never_started` and
    `slipping`, with its threshold (`QUIET_DAYS`, 5), so the two reads cannot disagree.

    A FORM CHECK is a video the CLIENT sent in the chat with an `exercise_id`
    (`POST /api/v1/chat/media/`) since the CURRENT coaching began (the approved
    relation's `responded_at`, else its `created_at`): a pair keeps one conversation
    for life, and a video left from an earlier coaching is not asked of this one. It
    leaves the queue as soon as the trainer sends any message in that conversation after
    it; a reply the trainer then took back does not count. A video the client took back
    leaves with it.

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
