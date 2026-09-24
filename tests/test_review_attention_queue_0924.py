"""The review of `GET /api/v1/trainer/attention/` (users/attention_queue.py), 2026-09-24.

Four findings, each confirmed by a reviewer and a verifier; one test (or two) each:

| Finding | Defect | Proved by |
|---|---|---|
| frozen today | a coaching frozen after midnight still queued its form checks, idle and ending cards until midnight, while the chat already answered `coaching_frozen` | `test_a_coaching_frozen_today_leaves_the_queue_at_once` |
| day-less training | a client training through sessions with no day was idle `never_started`, with an `at` days in the future | `test_a_client_who_trains_without_a_day_is_not_never_started`, `test_no_item_is_due_in_the_future` |
| a returning client | a video left unanswered under an EARLIER coaching came back as the oldest form check | `test_a_video_from_an_earlier_coaching_is_not_a_form_check` |
| the contract | the view's docstring promised a fee the row did not name, and said nothing differs by build | `test_the_view_contract_names_every_request_field`, `test_the_request_rows_fee_depends_on_the_build` |

Plus the query bound with those rosters in it: `test_the_bound_holds_with_frozen_and_returning_clients`.

Every row is seeded the way the product writes it — a real `CoachingPeriod` linked to its
relation, a real `WorkoutSession`, the relation deleted by `users.coaching.end_coaching` —
nothing mocked.
"""
import uuid
from datetime import datetime, timedelta
from decimal import Decimal

import pytest
from django.db import transaction
from django.utils import timezone

pytestmark = pytest.mark.django_db

QUEUE = '/api/v1/trainer/attention/'
#: A build that may show a coaching fee, and one that may not (BACKEND_DELTA decision 4).
SHOWN = {"HTTP_X_APP_CHANNEL": "apk"}
HIDDEN = {"HTTP_X_APP_CHANNEL": "ios"}
FEE = ('agreed_rate', 'agreed_currency', 'trainer_net', 'commission_rate')


# ----------------------------------------------------------------------- seeding
@pytest.fixture
def trainer(make_user):
    return make_user("rq_trainer", user_type="trainer", first_name="Rami",
                     trainer_hourly_rate=Decimal("12000"))


def _approved(make_user, trainer, name, *, since_days=60, client=None):
    from users.models import TrainerClientRelation

    client = client or make_user(name, user_type="client")
    TrainerClientRelation.objects.create(
        trainer=trainer, client=client, status="approved", initiated_by="client",
        responded_at=timezone.now() - timedelta(days=since_days))
    return client


def _routine(trainer, client, *, end=None):
    from routine.models import Routine

    routine = Routine.objects.create(name="Strength", created_by=trainer, days=4,
                                     start_date=timezone.localdate() - timedelta(days=30),
                                     end_date=end)
    routine.assigned_to.set([client])
    return routine


def _trained(client, routine, days_ago, *, day=1):
    """A closed training day, as `routine.outcomes.record_day` writes one."""
    from routine.models import DayOutcome

    return DayOutcome.objects.create(
        user=client, routine=routine, day=day,
        date=timezone.localdate() - timedelta(days=days_ago), via='completed',
        complete=True, planned_sets=10, logged_sets=10, missed_sets=0)


def _dayless_session(client, routine, days_ago):
    """A finished workout opened WITHOUT a day (`WorkoutSession.day` is nullable, and
    `routine/views.py` writes no `DayOutcome` for one)."""
    from routine.models import WorkoutSession

    ended = timezone.now() - timedelta(days=days_ago)
    return WorkoutSession.objects.create(user=client, routine=routine, day=None,
                                         status='completed',
                                         start_time=ended - timedelta(hours=1),
                                         end_time=ended)


def _video(trainer, client, exercise, *, at):
    """A client's video naming the move — the row `POST /api/v1/chat/media/` writes."""
    from chat.models import Message
    from chat.services import open_conversation

    message = Message.objects.create(
        conversation=open_conversation(trainer, client), sender=client, kind='video',
        exercise=exercise, body="form?", duration_seconds=12.0)
    Message.objects.filter(pk=message.pk).update(created_at=at)
    message.refresh_from_db()
    return message


def _frozen_today(trainer, client):
    """The paid month ended AND was frozen today, after midnight and before now — the
    hourly renewal failing at, say, 09:00 (users/coaching.py writes `frozen_at`)."""
    from users.models import CoachingPeriod, TrainerClientRelation

    now = timezone.now()
    midnight = timezone.make_aware(datetime.combine(timezone.localdate(), datetime.min.time()))
    ended = midnight + (now - midnight) / 2
    assert midnight < ended < now
    return CoachingPeriod.objects.create(
        relation=TrainerClientRelation.objects.get(trainer=trainer, client=client),
        trainer=trainer, client=client, number=1, price=Decimal("100.00"),
        commission_rate=Decimal("0.2000"), status='settled',
        hold_reference=str(uuid.uuid4()),
        starts_at=ended - timedelta(days=30), ends_at=ended, frozen_at=ended)


def _returning(make_user, trainer, name, exercise):
    """A former client who comes back: a video left unanswered when the first coaching
    ended (`end_coaching` deletes the relation; the conversation, one per pair, keeps
    its history), then a new approved relation and a new video."""
    from users.coaching import end_coaching
    from users.models import TrainerClientRelation

    client = _approved(make_user, trainer, name, since_days=270)
    old = _video(trainer, client, exercise, at=timezone.now() - timedelta(days=180))
    relation = TrainerClientRelation.objects.get(trainer=trainer, client=client)
    with transaction.atomic():
        end_coaching(relation, reason='finished')
    assert not TrainerClientRelation.objects.filter(trainer=trainer, client=client).exists()
    _approved(make_user, trainer, name, since_days=10, client=client)
    new = _video(trainer, client, exercise, at=timezone.now() - timedelta(minutes=40))
    return client, old, new


def _queue(api, user, **headers):
    response = api(user).get(QUEUE, **headers)
    assert response.status_code == 200, response.content
    body = response.json()
    assert body['count'] == len(body['items'])
    return body['items']


# ------------------------------------------------------------ 1. frozen today
def test_a_coaching_frozen_today_leaves_the_queue_at_once(api, make_user, trainer):
    """The queue asks the coaching rule at the same instant the chat does. It asked it
    for the whole DAY, so a month frozen at 09:00 still "covered" today until midnight:
    the trainer was offered a form check whose reply the chat refused."""
    from chat.permissions import may_message
    from routine.models import Exercise

    squat = Exercise.objects.create(name="Squat")
    frozen = _approved(make_user, trainer, "rq_frozen")
    routine = _routine(trainer, frozen, end=timezone.localdate() + timedelta(days=3))
    _trained(frozen, routine, 8)                                  # idle
    _video(trainer, frozen, squat, at=timezone.now() - timedelta(hours=1))
    _frozen_today(trainer, frozen)

    fine = _approved(make_user, trainer, "rq_fine")
    _trained(fine, _routine(trainer, fine, end=timezone.localdate() + timedelta(days=3)), 8)

    assert may_message(trainer, frozen) == 'coaching_frozen'
    items = _queue(api, trainer)
    theirs = [item['id'] for item in items if item['client']['id'] == frozen.pk]
    assert theirs == [], f"a coaching the chat refuses is still queued: {theirs}"
    assert {item['kind'] for item in items if item['client']['id'] == fine.pk} == {
        'idle_client', 'program_ending'}


# ------------------------------------------------------- 2. day-less training
def test_a_client_who_trains_without_a_day_is_not_never_started(api, make_user, trainer):
    """`never_started` means never trained. A client whose workouts carry no day has no
    `DayOutcome`, and was called `never_started` — score 50, `at` four days ahead —
    though they trained yesterday. Idle is decided by the last training, one threshold
    (`QUIET_DAYS`), and the older attention read says the same."""
    from routine.outcomes import QUIET_DAYS

    yesterday = _approved(make_user, trainer, "rq_yesterday")
    _dayless_session(yesterday, _routine(trainer, yesterday), 1)
    long_ago = _approved(make_user, trainer, "rq_long_ago")
    _dayless_session(long_ago, _routine(trainer, long_ago), QUIET_DAYS + 2)
    never = _approved(make_user, trainer, "rq_never", since_days=3)
    _routine(trainer, never)

    idle = {item['client']['id']: item for item in _queue(api, trainer)
            if item['kind'] == 'idle_client'}
    assert yesterday.pk not in idle, idle.get(yesterday.pk)

    quiet = idle[long_ago.pk]
    assert quiet['idle_client']['reason'] == 'quiet'
    assert quiet['idle_client']['days'] == QUIET_DAYS + 2
    due = timezone.localdate() - timedelta(days=QUIET_DAYS + 2) + timedelta(days=QUIET_DAYS)
    assert quiet['at'].startswith(due.isoformat()), quiet['at']

    # Never trained at all: still `never_started`, due when the coaching began.
    assert idle[never.pk]['idle_client']['reason'] == 'never_started'
    assert idle[never.pk]['idle_client']['days'] is None

    old = api(trainer).get('/api/routine/trainer/client-progress/attention/').json()
    codes = {row['client_id']: {r['code'] for r in row['reasons']} for row in old['clients']}
    assert 'never_started' not in codes.get(yesterday.pk, set()), codes.get(yesterday.pk)
    assert codes[long_ago.pk] >= {'quiet'} and 'never_started' not in codes[long_ago.pk]
    assert 'never_started' in codes[never.pk]


def test_no_item_is_due_in_the_future(api, make_user, trainer):
    """`at` is when an item BECAME due, so it is never later than the response."""
    from django.utils.dateparse import parse_datetime

    from routine.models import Exercise

    squat = Exercise.objects.create(name="Squat")
    for n, days_ago in enumerate((0, 1, 2, 4)):
        client = _approved(make_user, trainer, f"rq_future_{n}")
        routine = _routine(trainer, client, end=timezone.localdate() + timedelta(days=6))
        _dayless_session(client, routine, days_ago)
        _video(trainer, client, squat, at=timezone.now() - timedelta(minutes=5))

    items = _queue(api, trainer)
    assert items
    now = timezone.now()
    late = [(item['id'], item['at']) for item in items if parse_datetime(item['at']) > now]
    assert late == [], f"due in the future: {late}"


# --------------------------------------------------------- 3. a returning client
def test_a_video_from_an_earlier_coaching_is_not_a_form_check(api, make_user, trainer):
    """The trainer could not answer it (the chat refused them once the coaching ended),
    and it is not about this coaching. Only videos sent since the CURRENT approved
    relation began count; the earlier one used to top the group as the oldest."""
    from routine.models import Exercise

    squat = Exercise.objects.create(name="Squat")
    client, old, new = _returning(make_user, trainer, "rq_back", squat)

    checks = [item['form_check']['message_id'] for item in _queue(api, trainer)
              if item['kind'] == 'form_check']
    assert checks == [new.pk], (checks, old.pk)


def test_a_relation_with_no_answer_time_counts_from_its_creation(api, make_user, trainer):
    """A row approved without `responded_at` (older rows, operator fixes) began when it
    was created — the same fallback the idle card's `at` uses."""
    from routine.models import Exercise
    from users.models import TrainerClientRelation

    squat = Exercise.objects.create(name="Squat")
    client = make_user("rq_no_answer", user_type="client")
    relation = TrainerClientRelation.objects.create(trainer=trainer, client=client,
                                                    status="approved", initiated_by="client")
    TrainerClientRelation.objects.filter(pk=relation.pk).update(
        created_at=timezone.now() - timedelta(days=20))
    before = _video(trainer, client, squat, at=timezone.now() - timedelta(days=30))
    after = _video(trainer, client, squat, at=timezone.now() - timedelta(days=2))

    checks = [item['form_check']['message_id'] for item in _queue(api, trainer)
              if item['kind'] == 'form_check']
    assert checks == [after.pk], (checks, before.pk)


# ------------------------------------------------------------- 4. the contract
def _pending(make_user, trainer, name, **extra):
    """A request the CLIENT sent, quoted the way `client/request-trainer/` quotes it."""
    from users.models import TrainerClientRelation
    from users.trainer_relations import quote_agreed_price

    client = make_user(name, user_type="client", training_place="home",
                       training_days_per_week=3, **extra)
    relation = TrainerClientRelation(trainer=trainer, client=client, status="pending",
                                     initiated_by="client", requested_at=timezone.now(),
                                     client_note="Hi coach")
    quote_agreed_price(relation, trainer)
    relation.save()
    return relation


def test_the_view_contract_names_every_request_field(api, make_user, trainer):
    """The docstring is the contract the app reads. It said the request row carries "the
    quoted fee" without naming one field, and that nothing differs by build — while the
    fee is null on a build that may not show a coaching fee."""
    from users.attention_queue import AttentionQueueView

    _pending(make_user, trainer, "rq_asks")
    [item] = [i for i in _queue(api, trainer, **SHOWN) if i['kind'] == 'new_request']
    doc = AttentionQueueView.__doc__
    unnamed = sorted(key for key in item['new_request'] if f"`{key}`" not in doc)
    assert unnamed == [], f"served but not in the view's contract: {unnamed}"
    assert "Nothing here differs by build" not in doc
    assert "can_show_price" in doc, "the contract must say what differs by build"


def test_the_request_rows_fee_depends_on_the_build(api, make_user, trainer):
    """What the contract now says: the quote and the net on a build that may show a
    coaching fee; the same keys, null, on one that may not."""
    _pending(make_user, trainer, "rq_asks_build")

    [shown] = [i['new_request'] for i in _queue(api, trainer, **SHOWN)
               if i['kind'] == 'new_request']
    assert Decimal(str(shown['agreed_rate'])) == Decimal("12000")
    assert shown['agreed_currency'] and shown['trainer_net'] is not None
    assert shown['training_place'] == 'home' and shown['training_days_per_week'] == 3

    for headers in (HIDDEN, {}):
        [hidden] = [i['new_request'] for i in _queue(api, trainer, **headers)
                    if i['kind'] == 'new_request']
        assert {key: hidden[key] for key in FEE} == dict.fromkeys(FEE), headers


# ------------------------------------------------------------- the query bound
def test_the_bound_holds_with_frozen_and_returning_clients(
        api, make_user, trainer, django_assert_max_num_queries):
    """The same count for a small roster and a big one, with clients whose coaching
    froze today and a returning former client in both — and neither leaks through."""
    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    from routine.models import Exercise

    row = Exercise.objects.create(name="Row")
    frozen, stale = [], []

    def add(n):
        client = _approved(make_user, trainer, f"rq_bulk_{n}")
        routine = _routine(trainer, client, end=timezone.localdate() + timedelta(days=2))
        _trained(client, routine, 9 if n % 2 == 0 else 1)
        _video(trainer, client, row, at=timezone.now() - timedelta(minutes=30))
        cold = _approved(make_user, trainer, f"rq_bulk_frozen_{n}")
        cold_routine = _routine(trainer, cold, end=timezone.localdate() + timedelta(days=2))
        _trained(cold, cold_routine, 9)
        _video(trainer, cold, row, at=timezone.now() - timedelta(minutes=20))
        _frozen_today(trainer, cold)
        frozen.append(cold.pk)

    add(0)
    _client, old, _new = _returning(make_user, trainer, "rq_bulk_back", row)
    stale.append(old.pk)
    with CaptureQueriesContext(connection) as one:
        small = _queue(api, trainer)
    for n in range(1, 6):
        add(n)
    with django_assert_max_num_queries(len(one.captured_queries)):
        big = _queue(api, trainer)
    assert len(big) > len(small) >= 4

    for items in (small, big):
        assert not [i['id'] for i in items if i['client']['id'] in frozen]
        assert not [i['id'] for i in items
                    if i['kind'] == 'form_check' and i['form_check']['message_id'] in stale]
    with django_assert_max_num_queries(30):
        _queue(api, trainer)
