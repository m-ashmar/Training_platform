"""The trainer's home queue: `GET /api/v1/trainer/attention/` (BACKEND_BUGS B-091).

The design (`12-trainer-shell/TrainerHome`) draws ONE ordered list — a new request, a
form-check video, an idle client, a programme ending — and the app used to compose it
from four reads. Each test pins one promise of `users/attention_queue.py`:

    every kind              each with the fields its card draws
    one total order         kinds in the design's order, the most urgent first within
    a form check            stays until the trainer writes after it; a reply taken back
                            does not count; the client taking it back removes it
    who may appear          approved, active, unblocked; frozen coaching contributes
                            nothing; a trainer's own invitation is not a request
    not a trainer           the existing `not_a_trainer`
    the query bound         the same count for one client and for ten

Every row is seeded the way the product writes it: real `DayOutcome`s and sessions for
`routine.outcomes.attention`, real `CoachingPeriod`s for the freeze — nothing mocked.
"""
import uuid
from datetime import timedelta
from decimal import Decimal

import pytest
from django.utils import timezone

pytestmark = pytest.mark.django_db

QUEUE = '/api/v1/trainer/attention/'


# ----------------------------------------------------------------------- seeding
@pytest.fixture
def trainer(make_user):
    return make_user("aq_trainer", user_type="trainer", first_name="Rami")


def _approved(make_user, trainer, name, **kw):
    from users.models import TrainerClientRelation

    client = make_user(name, user_type="client", **kw)
    TrainerClientRelation.objects.create(trainer=trainer, client=client,
                                         status="approved", initiated_by="client",
                                         responded_at=timezone.now() - timedelta(days=60))
    return client


def _routine(trainer, client, *, name="Strength", start=None, end=None):
    from routine.models import Routine

    today = timezone.localdate()
    routine = Routine.objects.create(name=name, created_by=trainer, days=4,
                                     start_date=start or today - timedelta(days=30),
                                     end_date=end)
    routine.assigned_to.set([client])
    return routine


def _trained(client, routine, days_ago, *, planned=10, logged=10, missed=0, day=1):
    """A closed training day, as `routine.outcomes.record_day` writes one."""
    from routine.models import DayOutcome

    return DayOutcome.objects.create(
        user=client, routine=routine, day=day,
        date=timezone.localdate() - timedelta(days=days_ago), via='completed',
        complete=logged >= planned, planned_sets=planned, logged_sets=logged,
        missed_sets=missed)


def _conversation(trainer, client):
    from chat.services import open_conversation

    return open_conversation(trainer, client)


def _form_check(trainer, client, exercise, *, body="back straight?", seconds=18.0,
                minutes_ago=40):
    """A client's video naming the move — the row `POST /api/v1/chat/media/` writes."""
    from chat.models import Message

    message = Message.objects.create(
        conversation=_conversation(trainer, client), sender=client, kind='video',
        exercise=exercise, body=body, duration_seconds=seconds)
    Message.objects.filter(pk=message.pk).update(
        created_at=timezone.now() - timedelta(minutes=minutes_ago))
    message.refresh_from_db()
    return message


def _reply(trainer, client, body="lower the hips"):
    from chat.models import Message

    return Message.objects.create(conversation=_conversation(trainer, client),
                                  sender=trainer, body=body)


def _request(make_user, trainer, name, *, hours_ago=2):
    from users.models import TrainerClientRelation

    client = make_user(name, user_type="client")
    relation = TrainerClientRelation.objects.create(
        trainer=trainer, client=client, status="pending", initiated_by="client",
        client_note="I want to build muscle")
    sent = timezone.now() - timedelta(hours=hours_ago)
    TrainerClientRelation.objects.filter(pk=relation.pk).update(created_at=sent,
                                                               requested_at=sent)
    relation.refresh_from_db()
    return client, relation


@pytest.fixture
def board(make_user, trainer):
    """One of every kind, plus a second request and a second form check to order."""
    from routine.models import Exercise, WorkoutSession

    today = timezone.localdate()
    deadlift = Exercise.objects.create(name="Deadlift")
    squat = Exercise.objects.create(name="Squat")

    samer, older_request = _request(make_user, trainer, "aq_samer", hours_ago=5)
    nour, newer_request = _request(make_user, trainer, "aq_nour", hours_ago=2)

    laith = _approved(make_user, trainer, "aq_laith")
    laith_routine = _routine(trainer, laith, name="Pull")
    _trained(laith, laith_routine, 1)
    rana = _approved(make_user, trainer, "aq_rana")
    rana_routine = _routine(trainer, rana)
    _trained(rana, rana_routine, 1)
    newer_check = _form_check(trainer, laith, deadlift, minutes_ago=40)
    older_check = _form_check(trainer, rana, squat, minutes_ago=90, body="")

    heba = _approved(make_user, trainer, "aq_heba")
    heba_routine = _routine(trainer, heba, name="Fitness")
    _trained(heba, heba_routine, 8, day=3)
    WorkoutSession.objects.create(
        user=heba, routine=heba_routine, day=3, status='completed',
        start_time=timezone.now() - timedelta(days=8, hours=1),
        end_time=timezone.now() - timedelta(days=8))

    sami = _approved(make_user, trainer, "aq_sami")
    sami_routine = _routine(trainer, sami, name="Hypertrophy")
    for days_ago in (10, 13, 17):
        _trained(sami, sami_routine, days_ago, day=days_ago % 4 + 1)
    _trained(sami, sami_routine, 1, planned=10, logged=2, missed=8, day=2)

    omar = _approved(make_user, trainer, "aq_omar")
    omar_routine = _routine(trainer, omar, name="Beginner Strength",
                            start=today - timedelta(days=52),
                            end=today + timedelta(days=3))
    _trained(omar, omar_routine, 1)

    return {
        'requests': (older_request, newer_request),
        'checks': (older_check, newer_check),
        'heba': heba, 'heba_routine': heba_routine,
        'sami': sami, 'omar': omar, 'omar_routine': omar_routine,
        'laith': laith, 'rana': rana, 'samer': samer,
        'deadlift': deadlift,
    }


def _queue(api, user):
    response = api(user).get(QUEUE)
    assert response.status_code == 200, response.content
    body = response.json()
    assert body['count'] == len(body['items'])
    return body['items']


# ------------------------------------------------------------------ every kind
def test_every_kind_is_one_list_in_the_designs_order(api, trainer, board):
    items = _queue(api, trainer)
    assert [item['kind'] for item in items] == [
        'new_request', 'new_request', 'form_check', 'form_check', 'idle_client',
        'slipping_client', 'program_ending'], [(i['kind'], i['id']) for i in items]

    # Within a kind, the most urgent first: the longest-waiting request, the oldest
    # unanswered video.
    older_request, newer_request = board['requests']
    assert [i['new_request']['request_id'] for i in items[:2]] == [
        older_request.pk, newer_request.pk]
    older_check, newer_check = board['checks']
    assert [i['form_check']['message_id'] for i in items[2:4]] == [
        older_check.pk, newer_check.pk]

    # Ids are stable and unique.
    assert len({item['id'] for item in items}) == len(items)
    assert items[2]['id'] == f"form_check:{older_check.pk}"
    assert _queue(api, trainer) == items, "two calls on the same data disagree"


def test_each_card_carries_what_the_design_draws(api, trainer, board):
    items = {item['kind']: item for item in reversed(_queue(api, trainer))}

    request = items['new_request']
    assert request['client']['id'] == board['samer'].pk
    assert set(request['client']) >= {'id', 'name', 'first_name', 'last_name',
                                      'profile_picture'}
    assert request['new_request']['note'] == "I want to build muscle"
    assert request['new_request']['initiated_by'] == 'client'

    check = items['form_check']['form_check']
    newest = board['checks'][1]
    first_check = _queue(api, trainer)[3]['form_check']
    assert first_check['exercise'] == {'id': board['deadlift'].pk, 'name': "Deadlift"}
    assert first_check['caption'] == "back straight?"
    assert first_check['duration_seconds'] == 18.0
    assert first_check['conversation_id'] == newest.conversation_id
    assert check['sent_at']

    idle = items['idle_client']
    assert idle['client']['id'] == board['heba'].pk
    assert idle['idle_client']['reason'] == 'quiet'
    assert idle['idle_client']['days'] == 8
    assert idle['idle_client']['last_workout']['day'] == 3
    assert idle['idle_client']['program'] == {'id': board['heba_routine'].pk,
                                              'name': "Fitness"}
    from routine.outcomes import QUIET_DAYS
    due = timezone.localdate() - timedelta(days=8) + timedelta(days=QUIET_DAYS)
    assert idle['at'].startswith(due.isoformat())

    slipping = items['slipping_client']
    assert slipping['client']['id'] == board['sami'].pk
    assert slipping['slipping_client']['this_week_percent'] == 20.0
    assert slipping['slipping_client']['usual_percent'] > 50
    assert slipping['slipping_client']['missed_sets'] == 8

    ending = items['program_ending']
    assert ending['client']['id'] == board['omar'].pk
    assert ending['program_ending'] == {
        'routine_id': board['omar_routine'].pk, 'name': "Beginner Strength",
        'week': 8, 'weeks': 8,
        'end_date': (timezone.localdate() + timedelta(days=3)).isoformat(),
        'days_left': 3}


def test_idle_uses_the_attention_reads_own_threshold(api, make_user, trainer):
    """One number for "idle": a client quiet for QUIET_DAYS - 1 is not idle, at
    QUIET_DAYS they are — the same answer the older attention read gives."""
    from routine.outcomes import QUIET_DAYS

    almost = _approved(make_user, trainer, "aq_almost")
    _trained(almost, _routine(trainer, almost), QUIET_DAYS - 1)
    quiet = _approved(make_user, trainer, "aq_quiet")
    _trained(quiet, _routine(trainer, quiet), QUIET_DAYS)

    idle = [i['client']['id'] for i in _queue(api, trainer) if i['kind'] == 'idle_client']
    assert idle == [quiet.pk]

    old = api(trainer).get('/api/routine/trainer/client-progress/attention/').json()
    quiet_in_old = {row['client_id'] for row in old['clients']
                    if any(r['code'] == 'quiet' for r in row['reasons'])}
    assert quiet_in_old == {quiet.pk}, "the two reads disagree about who is idle"


# --------------------------------------------------------------- form checks
def test_a_form_check_leaves_when_the_trainer_writes_after_it(api, trainer, board):
    from chat.models import Message

    laith = board['laith']
    newer = board['checks'][1]

    def checks():
        return [i['form_check']['message_id'] for i in _queue(api, trainer)
                if i['kind'] == 'form_check']

    assert newer.pk in checks()
    reply = _reply(trainer, laith)
    assert newer.pk not in checks(), "a replied form check is still in the queue"

    # A reply the trainer took back is no answer: the video is back.
    Message.objects.filter(pk=reply.pk).update(deleted_at=timezone.now())
    assert newer.pk in checks()

    # A message from the CLIENT after it is not an answer either.
    Message.objects.create(conversation=newer.conversation, sender=laith, body="??")
    assert newer.pk in checks()

    # The client taking the video back takes it out of the queue.
    Message.objects.filter(pk=newer.pk).update(deleted_at=timezone.now())
    assert newer.pk not in checks()


def test_only_a_clients_video_that_names_a_move_is_a_form_check(api, trainer, board):
    from chat.models import Message

    laith = board['laith']
    conversation = board['checks'][1].conversation
    # The trainer's own demonstration, a client video with no exercise, a photo.
    Message.objects.create(conversation=conversation, sender=laith, kind='video',
                           body="no move named")
    Message.objects.create(conversation=conversation, sender=laith, kind='image',
                           exercise=board['deadlift'])
    ids = {i['form_check']['message_id'] for i in _queue(api, trainer)
           if i['kind'] == 'form_check'}
    assert ids == {m.pk for m in board['checks']}


def test_a_form_check_sent_through_the_route_reaches_the_queue_with_its_media(
        api, trainer, make_user, settings, tmp_path):
    """End to end: the chat route writes it, the queue serves the trainer the same
    signed URL the thread would."""
    import struct

    from django.core.files.uploadedfile import SimpleUploadedFile

    from routine.models import Exercise

    settings.MEDIA_ROOT = str(tmp_path)
    settings.MEDIA_URL_SIGNING = True
    client = _approved(make_user, trainer, "aq_route")
    _trained(client, _routine(trainer, client), 1)
    conversation = _conversation(trainer, client)
    bench = Exercise.objects.create(name="Bench Press")

    def box(kind, payload):
        return struct.pack(">I", len(payload) + 8) + kind + payload

    mvhd = box(b"mvhd", b"\x00" * 12 + struct.pack(">I", 1000)
               + struct.pack(">I", 12000) + b"\x00" * 80)
    clip = (box(b"ftyp", b"isom" + struct.pack(">I", 512) + b"isommp42")
            + box(b"moov", mvhd) + box(b"mdat", b"\x00" * 1024))
    sent = api(client).post('/api/v1/chat/media/', {
        'conversation': conversation.pk, 'kind': 'video', 'exercise_id': bench.pk,
        'file': SimpleUploadedFile("c.mp4", clip, content_type="video/mp4")})
    assert sent.status_code == 201, sent.content

    [item] = [i for i in _queue(api, trainer) if i['kind'] == 'form_check']
    assert item['form_check']['message_id'] == sent.json()['id']
    assert item['form_check']['duration_seconds'] == 12.0
    assert api().get(item['form_check']['media']['url']).status_code == 200


# --------------------------------------------------------------- who may appear
def test_blocked_pairs_and_suspended_accounts_never_appear(api, trainer, board):
    from moderation.models import Block

    Block.objects.create(blocker=board['samer'], blocked=trainer)    # a request
    Block.objects.create(blocker=trainer, blocked=board['laith'])    # a form check
    board['heba'].is_active = False                                  # idle, suspended
    board['heba'].save(update_fields=['is_active'])

    items = _queue(api, trainer)
    people = {item['client']['id'] for item in items}
    assert board['samer'].pk not in people
    assert board['laith'].pk not in people
    assert board['heba'].pk not in people
    assert board['rana'].pk in people and board['sami'].pk in people


def test_a_frozen_coaching_contributes_nothing(api, trainer, board):
    """The chat refuses both sides while a coaching is frozen, so its form check could
    never be answered, and the client's routines cannot be used."""
    from users.models import CoachingPeriod, TrainerClientRelation

    now = timezone.now()
    for client in (board['rana'], board['omar']):
        relation = TrainerClientRelation.objects.get(trainer=trainer, client=client)
        CoachingPeriod.objects.create(
            relation=relation, trainer=trainer, client=client, number=1,
            price=Decimal("100.00"), commission_rate=Decimal("0.2000"),
            status='settled', hold_reference=str(uuid.uuid4()),
            starts_at=now - timedelta(days=31), ends_at=now - timedelta(days=1),
            frozen_at=now - timedelta(days=1))

    people = {item['client']['id'] for item in _queue(api, trainer)}
    assert board['rana'].pk not in people, "a frozen client's form check is queued"
    assert board['omar'].pk not in people, "a frozen client's programme is queued"
    assert board['laith'].pk in people


def test_only_approved_clients_and_only_requests_the_client_sent(api, make_user,
                                                                 trainer, board):
    from chat.models import Message
    from users.models import TrainerClientRelation

    # The trainer's own invitation waits on the CLIENT: nothing for the trainer to do.
    invited = make_user("aq_invited", user_type="client")
    TrainerClientRelation.objects.create(trainer=trainer, client=invited,
                                         status="pending", initiated_by="trainer")
    # A former client whose relation is gone keeps their thread, not their place here.
    TrainerClientRelation.objects.filter(trainer=trainer, client=board['laith']).delete()
    # Another trainer's client with an unanswered form check to THAT trainer.
    other = make_user("aq_other_trainer", user_type="trainer")
    theirs = _approved(make_user, other, "aq_theirs")
    _form_check(other, theirs, board['deadlift'])

    items = _queue(api, trainer)
    people = {item['client']['id'] for item in items}
    assert invited.pk not in people
    assert board['laith'].pk not in people
    assert theirs.pk not in people
    assert Message.objects.filter(conversation__client=board['laith']).exists()


def test_a_non_trainer_gets_the_existing_code(api, make_user, board):
    for who in (board['laith'], make_user("aq_admin", user_type="admin"),
                make_user("aq_agent", user_type="agent")):
        refused = api(who).get(QUEUE)
        assert refused.status_code == 403
        assert refused.json()['code'] == 'not_a_trainer'
    assert api().get(QUEUE).status_code == 401


def test_the_older_reads_still_answer(api, trainer, board):
    """Additive: the four reads the app composed the queue from are untouched."""
    for path in ('/api/auth/trainer/pending-requests/',
                 '/api/routine/trainer/client-progress/attention/',
                 '/api/routine/trainer/client-progress/recent/',
                 '/api/routine/routines/'):
        assert api(trainer).get(path).status_code == 200, path


# --------------------------------------------------------------- the query bound
def test_the_queue_costs_the_same_for_one_client_and_for_ten(
        api, make_user, trainer, django_assert_max_num_queries):
    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    from routine.models import Exercise

    exercise = Exercise.objects.create(name="Row")

    def add(n):
        client = _approved(make_user, trainer, f"aq_bulk_{n}")
        routine = _routine(trainer, client, end=timezone.localdate() + timedelta(days=2))
        # Even ones idle, odd ones not: the first client alone already takes every
        # branch the bigger roster takes, so the counts are comparable.
        _trained(client, routine, 9 if n % 2 == 0 else 1)
        _form_check(trainer, client, exercise)
        _request(make_user, trainer, f"aq_bulk_req_{n}")

    add(0)
    with CaptureQueriesContext(connection) as one:
        small = _queue(api, trainer)
    for n in range(1, 10):
        add(n)
    with django_assert_max_num_queries(len(one.captured_queries)):
        big = _queue(api, trainer)
    assert len(big) > len(small) >= 3
    with django_assert_max_num_queries(30):
        _queue(api, trainer)
