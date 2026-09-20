"""The home screen is decided by the server (BACKEND_DELTA item 9).

`GET /api/v1/home` answers an ORDERED list of typed sections and the app renders what it
is handed. What is protected here: a person with nothing still gets a usable screen, a
full one gets every section in the documented order, a frozen coaching keeps its day
visible and unstartable (the owner, 2026-09-19), a build that may not sell is never sent
a section that sells, the cache separates builds and languages and dies when a day
closes, and one home load costs a bounded number of queries.

See home/views.py — its docstring is the contract.
"""
import uuid
from datetime import timedelta
from unittest import mock

import pytest
from django.utils import timezone

from tests.test_c9_payments import _plan
from tests.test_coaching_months import OWNER_RATE, _accepted, _end_month

pytestmark = pytest.mark.django_db

HOME = "/api/v1/home"
JSON = "application/json"

#: What one cold home load may cost. Measured, then rounded up a little: 15 queries for
#: a client with nothing, 28 for one with a coach, a closed day, a plan and a badge, 34
#: for the same person with six exercises and three days of history — the strength
#: headline reads one query per exercise it ranks (`routine/recent.py STRENGTH_LIMIT`),
#: which is the only part that grows and is bounded at six. A ceiling, not a target; the
#: test beside it also grows the data and asserts the count does not follow.
QUERY_CEILING = 36


@pytest.fixture(autouse=True)
def owners_rules(settings):
    settings.COACHING_COMMISSION_RATE = OWNER_RATE
    settings.COACHING_PERIOD_DAYS = 30
    settings.COACHING_RENEWAL_REMINDER_DAYS = 3
    settings.COACHING_FROZEN_REMINDER_DAYS = (3, 7, 14, 30)
    settings.SUBSCRIPTION_RENEWAL_NOTICE_DAYS = 3


@pytest.fixture(autouse=True)
def a_cold_cache():
    """The private cache lives for the whole session and the home payload is cached for
    120 s: every test here starts and ends from a cold one, as a first request would."""
    from training_platform.cache import private_cache

    private_cache().clear()
    yield
    private_cache().clear()


# --------------------------------------------------------------------------- reading
def _home(api, person, **extra):
    resp = api(person).get(HOME, **extra)
    assert resp.status_code == 200, resp.content
    return resp.json()


def _types(body):
    return [section["type"] for section in body["sections"]]


def _data(body, kind):
    for section in body["sections"]:
        if section["type"] == kind:
            return section["data"]
    return None


def _strings_and_keys(node, seen=None):
    """Every key and every string in a payload, flattened — for asserting an absence."""
    seen = seen if seen is not None else []
    if isinstance(node, dict):
        for key, value in node.items():
            seen.append(str(key))
            _strings_and_keys(value, seen)
    elif isinstance(node, list):
        for value in node:
            _strings_and_keys(value, seen)
    elif isinstance(node, str):
        seen.append(node)
    return seen


# --------------------------------------------------------------------------- building
def _program(make_user, tag, *, trainer=None, client=None, exercises=1, sets=3, reps=10,
             days=1, weight=20, assign=True):
    """A client with an active routine of `exercises` exercises on day 1.

    `assign=False` leaves the routine unassigned for a caller that will deliver it
    through `assign_to_client/`, which is also what settles a coaching's first month.
    """
    from routine.models import Exercise, Routine, RoutineExercise

    trainer = trainer or make_user(f"hm_t_{tag}", user_type="trainer")
    client = client or make_user(f"hm_c_{tag}")
    routine = Routine.objects.create(name=f"home {tag}", created_by=trainer, days=days)
    rows = []
    for index in range(exercises):
        exercise = Exercise.objects.create(name=f"home ex {tag} {index}", created_by=trainer)
        rows.append(RoutineExercise.objects.create(routine=routine, exercise=exercise, day=1,
                                                   order=index + 1, sets=sets, reps=reps,
                                                   weight=weight))
    if assign:
        routine.assigned_to.add(client)
    return trainer, client, routine, rows


def _log_sets(client, api, row, count, *, reps=10, weight=20, date=None, start=1):
    from routine.models import UserExerciseProgress

    date = date or timezone.localdate()
    progress, _created = UserExerciseProgress.objects.get_or_create(
        user=client, exercise=row.exercise, date=date, defaults={"target_sets": row.sets})
    for number in range(start, start + count):
        resp = api(client).post("/api/routine/set-logs/",
                                {"user_exercise_progress": progress.pk, "set_number": number,
                                 "weight": weight, "reps": reps, "date": date.isoformat()},
                                content_type=JSON)
        assert resp.status_code in (200, 201), resp.content
    return progress


def _close_day(api, client, routine, *, day=1):
    return api(client).post(f"/api/routine/routines/{routine.pk}/update_progress/",
                            {"day": day, "status": "completed", "confirm_unlogged": True},
                            content_type=JSON)


def _subscribe(client, tag, *, ends_in=timedelta(days=20), status="active"):
    from subscription.models import Subscription

    plan = _plan(f"home {tag}", has_diet_access=True)
    sub = Subscription.objects.create(user=client, plan=plan, status="active",
                                      source="shamcash")
    Subscription.objects.filter(pk=sub.pk).update(
        status=status, start_date=timezone.now() - timedelta(days=30),
        end_date=timezone.now() + ends_in)
    return Subscription.objects.get(pk=sub.pk)


def _diet_day(client, *, calories=2000.0, eaten=800.0):
    from diet.models import DailyProgress, DietPlan

    today = timezone.localdate()
    plan = DietPlan.objects.create(user=client, goal="Maintain", daily_calories=calories,
                                   target_protein=150.0, target_carbs=200.0, target_fat=60.0,
                                   start_date=today - timedelta(days=1),
                                   end_date=today + timedelta(days=5), duration_weeks=1)
    DailyProgress.objects.create(user=client, diet_plan=plan, date=today,
                                 meals_completed=1, total_meals=4,
                                 calories_consumed=eaten, protein_consumed=60.0,
                                 carbs_consumed=80.0, fat_consumed=20.0,
                                 target_calories=calories)
    return plan


def _award(client, tag, *, read=False):
    """An achievement earned, with the notification that makes it "new"."""
    from achievements.models import Achievement, UserAchievement
    from notifications.models import Notification

    badge = Achievement.objects.create(key=f"home_{tag}", name=f"Home {tag}",
                                       description="earned in a test", category="milestone",
                                       points=25)
    UserAchievement.objects.create(user=client, achievement=badge)
    Notification.objects.create(recipient=client, event_type="achievement_awarded",
                                related_object_id=f"achievements:{badge.pk}",
                                is_read=read, event_id=uuid.uuid4(),
                                deduplication_key=uuid.uuid4().hex)
    return badge


def _freeze(relation, **ago):
    """The month ended and the hourly run could not charge the next one."""
    from users.coaching import renew

    first = _end_month(relation, **ago)
    with mock.patch("notifications.domain.dispatcher.emit_event"):
        assert renew(relation.pk) == "frozen"
    type(first).objects.filter(pk=first.pk).update(
        frozen_at=min(timezone.now(), first.ends_at + timedelta(minutes=5)))
    first.refresh_from_db()
    return first


def _coached(make_user, api, tag, *, funds="1000.00"):
    """A client whose month 1 was paid by the arrival of their first program."""
    trainer, client, relation = _accepted(make_user, api, f"home_{tag}", funds=funds)
    trainer, client, routine, rows = _program(make_user, tag, trainer=trainer, client=client,
                                              assign=False)
    assigned = api(trainer).post(f"/api/routine/routines/{routine.pk}/assign_to_client/",
                                 {"client_id": client.pk}, content_type=JSON)
    assert assigned.status_code == 200, assigned.content
    relation.refresh_from_db()
    return trainer, client, relation, routine, rows


# ----------------------------------------------------------------- the empty screen
def test_a_client_with_nothing_gets_a_home_that_works(make_user, api):
    """No routine, no coach, no plan, no history — and still a screen, with no empty card."""
    person = make_user("home_empty")

    body = _home(api, person)

    assert set(body) == {"as_of", "refresh_after_seconds", "sections"}
    assert body["refresh_after_seconds"] == 120
    for section in body["sections"]:
        assert set(section) == {"type", "data"}, section
        assert section["data"], f"{section['type']} was sent empty"
    # Who they are, and the way to find a coach. Nothing else has anything to say.
    assert _types(body) == ["greeting", "coach"]
    assert _data(body, "greeting")["streak"] == {"current": 0, "longest": 0}
    assert "last_break" not in _data(body, "greeting")
    assert _data(body, "coach") == {"state": "none"}


def test_the_home_is_the_clients_screen(make_user, api):
    trainer = make_user("home_trainer", user_type="trainer")
    refused = api(trainer).get(HOME)
    assert (refused.status_code, refused.json()["code"]) == (403, "clients_only")
    assert api().get(HOME).status_code == 401

    # The one sentence this endpoint answers is translated, and compiled.
    arabic = api(trainer).get(HOME, HTTP_ACCEPT_LANGUAGE="ar")
    assert any("؀" <= ch <= "ۿ" for ch in arabic.json()["detail"]), arabic.content


# ------------------------------------------------------------------ the full screen
def test_a_full_client_gets_every_section_in_a_sensible_order(make_user, api,
                                                              django_capture_on_commit_callbacks):
    trainer, client, relation, routine, rows = _coached(make_user, api, "full")
    _subscribe(client, "full", ends_in=timedelta(days=20))
    _diet_day(client)
    _award(client, "full")
    _log_sets(client, api, rows[0], 2)
    with django_capture_on_commit_callbacks(execute=True):
        assert _close_day(api, client, routine).status_code == 200

    body = _home(api, client)

    assert _types(body) == ["greeting", "today", "plan", "week", "strength",
                            "achievements", "coach"]

    today = _data(body, "today")
    assert (today["routine_id"], today["day"], today["state"]) == (routine.pk, 1, "prescribed")
    assert today["sets"] == {"prescribed": 3, "logged": 2, "unlogged": 1}
    assert today["exercises"]["planned"] == 1
    assert (today["closed"], today["outcome"]["missed_sets"]) == (True, 1)
    assert today["can_start"] is True and "refusal" not in today
    assert "session" not in today

    week = _data(body, "week")
    assert len(week["days"]) == 7
    assert week["adherence"]["sets"] == {"planned": 3, "logged": 2, "missed": 1,
                                         "done_percent": 66.7}
    # The same numbers as the endpoint the app already knows, from the same entry.
    endpoint = api(client).get("/api/routine/v1/analytics/recent-progress/").json()
    assert (week["days"], week["adherence"]) == (endpoint["recent_progress"],
                                                 endpoint["adherence"])
    assert _data(body, "strength")["headline"] == endpoint["strength"]["headline"]
    assert _data(body, "strength")["headline"]["kind"] != "no_data"

    plan = _data(body, "plan")
    assert plan["targets"] == {"calories": 2000.0, "protein_g": 150.0,
                               "carbs_g": 200.0, "fat_g": 60.0}
    assert plan["eaten"]["calories"] == 800.0
    assert (plan["meals"], plan["readable_only"]) == ({"completed": 1, "total": 4}, False)

    coach = _data(body, "coach")
    assert (coach["state"], coach["trainer_id"], coach["status"]) == \
        ("assigned", trainer.pk, "active")

    earned = _data(body, "achievements")
    assert (earned["count"], earned["items"][0]["points"]) == (1, 25)
    assert earned["items"][0]["name"] == "Home full"


def test_a_rest_day_says_so_and_cannot_be_started(make_user, api):
    """The routine's cycle lands on a day the coach prescribed nothing for."""
    from routine.models import Routine

    trainer, client, routine, rows = _program(make_user, "rest", days=2)
    # Day 1 has the exercises; start the routine yesterday so today is day 2.
    Routine.objects.filter(pk=routine.pk).update(
        start_date=timezone.localdate() - timedelta(days=1))

    today = _data(_home(api, client), "today")
    assert (today["day"], today["state"], today["can_start"]) == (2, "rest", False)
    assert "sets" not in today


def test_an_open_session_is_shown_instead_of_a_way_to_start_another(make_user, api):
    trainer, client, routine, rows = _program(make_user, "open")
    started = api(client).post("/api/routine/workout-sessions/",
                               {"routine": routine.pk, "day": 1}, content_type=JSON)
    assert started.status_code in (200, 201), started.content

    today = _data(_home(api, client), "today")
    assert today["session"]["routine_id"] == routine.pk
    assert today["can_start"] is False


def test_a_session_nobody_ever_closed_does_not_block_tomorrow(make_user, api):
    """Nothing abandons a forgotten session, and one may not last longer than 12 hours:
    past that it is not "open", or the button would never come back."""
    from routine.models import WorkoutSession

    trainer, client, routine, rows = _program(make_user, "forgotten")
    session = WorkoutSession.objects.create(user=client, routine=routine, day=1)
    WorkoutSession.objects.filter(pk=session.pk).update(
        start_time=timezone.now() - timedelta(hours=13))

    today = _data(_home(api, client), "today")
    assert "session" not in today
    assert today["can_start"] is True


# ----------------------------------------------------------------------- the freeze
def test_a_frozen_client_still_sees_the_day_and_cannot_start_it(make_user, api):
    """The owner (2026-09-19): never deleted, readable, and it cannot be used."""
    trainer, client, relation, routine, rows = _coached(make_user, api, "frozen",
                                                        funds="100.00")
    _freeze(relation, ago=timedelta(minutes=1))

    body = _home(api, client)

    today = _data(body, "today")
    assert today is not None, "a frozen coaching keeps the day visible"
    assert (today["state"], today["refusal"], today["can_start"]) == \
        ("prescribed", "coaching_frozen", False)
    # The refusal is the code the training writes answer, not a sentence to display.
    write = api(client).post("/api/routine/workout-sessions/",
                             {"routine": routine.pk, "day": 1}, content_type=JSON)
    assert (write.status_code, write.json()["code"]) == (403, today["refusal"])

    notices = _data(body, "notices")["items"]
    frozen = [item for item in notices if item["code"] == "coaching_frozen"]
    assert frozen and frozen[0]["urgent"] is True
    assert frozen[0]["trainer_id"] == trainer.pk
    assert _data(body, "coach")["status"] == "frozen"


def test_a_lapsed_subscription_is_a_notice_and_the_plan_stays_readable(make_user, api):
    trainer, client, routine, rows = _program(make_user, "lapsed")
    _subscribe(client, "lapsed", ends_in=-timedelta(hours=2), status="expired")
    _diet_day(client)

    body = _home(api, client)

    plan = _data(body, "plan")
    assert plan is not None and plan["readable_only"] is True
    codes = {item["code"]: item for item in _data(body, "notices")["items"]}
    assert codes["subscription_ended"]["urgent"] is True


def test_a_plan_about_to_end_is_the_three_day_reminder(make_user, api):
    client = make_user("home_ending")
    _subscribe(client, "ending", ends_in=timedelta(days=2))

    codes = {item["code"]: item for item in _data(_home(api, client), "notices")["items"]}
    assert codes["subscription_ending_soon"]["urgent"] is False
    assert codes["subscription_ending_soon"]["days_left"] <= 3


def test_a_client_with_no_diet_entitlement_loses_the_section_not_the_screen(make_user, api):
    trainer, client, routine, rows = _program(make_user, "nodiet")
    _diet_day(client)                      # a plan exists, but nothing paid for it

    body = _home(api, client)
    assert _data(body, "plan") is None
    assert "today" in _types(body), "the rest of the screen is unaffected"


# ------------------------------------------------------------------------- the build
@pytest.mark.parametrize("channel", ["play", "ios", ""])
def test_a_store_build_is_sent_nothing_that_sells(make_user, api, channel,
                                                  django_capture_on_commit_callbacks):
    trainer, client, relation, routine, rows = _coached(make_user, api, f"sell{channel or 'none'}")
    _subscribe(client, f"sell{channel or 'none'}")
    _diet_day(client)
    _log_sets(client, api, rows[0], 1)

    body = _home(api, client, HTTP_X_APP_CHANNEL=channel)

    words = " ".join(_strings_and_keys(body)).lower()
    for forbidden in ("price", "amount", "currency", "checkout", "money_type", "hub"):
        assert forbidden not in words, (forbidden, body)


@pytest.mark.parametrize("channel,prompted", [("apk", True), ("play", True),
                                              ("ios", False), ("", False)])
def test_only_a_build_that_may_pay_for_coaching_is_invited_to_hire_one(make_user, api,
                                                                       channel, prompted):
    """A prompt to find a trainer is a sale, and `ios` may not open a coaching checkout
    today. It depends on the BUILD and on nothing else."""
    person = make_user(f"home_hire_{channel or 'none'}")

    body = _home(api, person, HTTP_X_APP_CHANNEL=channel)
    assert (_data(body, "coach") == {"state": "none"}) is prompted


def test_a_client_with_a_coach_sees_them_on_every_build(make_user, api):
    trainer, client, relation, routine, rows = _coached(make_user, api, "everywhere")

    for channel in ("apk", "play", "ios", ""):
        coach = _data(_home(api, client, HTTP_X_APP_CHANNEL=channel), "coach")
        assert coach["trainer_id"] == trainer.pk, channel
        assert "coaching_price" not in coach and "price" not in coach


# -------------------------------------------------------------------------- the cache
def test_the_cache_does_not_leak_between_builds(make_user, api):
    person = make_user("home_build_cache")

    warm = _home(api, person, HTTP_X_APP_CHANNEL="apk")
    store = _home(api, person, HTTP_X_APP_CHANNEL="ios")

    assert _data(warm, "coach") == {"state": "none"}
    assert _data(store, "coach") is None, "the apk answer was served to a store build"
    assert _data(_home(api, person, HTTP_X_APP_CHANNEL="apk"), "coach") == {"state": "none"}


def test_the_cache_does_not_leak_between_languages(make_user, api):
    from routine.models import Routine

    trainer, client, routine, rows = _program(make_user, "lang")
    Routine.objects.filter(pk=routine.pk).update(translations={"ar": {"name": "قوة"}})

    english = _home(api, client, HTTP_ACCEPT_LANGUAGE="en")
    arabic = _home(api, client, HTTP_ACCEPT_LANGUAGE="ar")

    assert _data(english, "today")["routine_name"] == "home lang"
    assert _data(arabic, "today")["routine_name"] == "قوة"


def test_closing_a_day_shows_on_the_home_at_once(make_user, api,
                                                 django_capture_on_commit_callbacks):
    """The payload is cached for 120 s; the day that closes must not wait for it."""
    trainer, client, routine, rows = _program(make_user, "bust", sets=2)
    _log_sets(client, api, rows[0], 2)

    before = _data(_home(api, client), "today")
    assert before["closed"] is False

    with django_capture_on_commit_callbacks(execute=True):
        assert _close_day(api, client, routine).status_code == 200

    after = _data(_home(api, client), "today")
    assert after["closed"] is True, "the home was served from a stale cache"
    assert after["outcome"]["complete"] is True
    assert _data(_home(api, client), "greeting")["streak"]["current"] == 1


def test_a_second_load_is_served_from_the_cache(make_user, api):
    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    trainer, client, routine, rows = _program(make_user, "warm")
    _home(api, client)

    with CaptureQueriesContext(connection) as ctx:
        assert api(client).get(HOME).status_code == 200
    # Only what authentication itself costs: the screen is not rebuilt.
    assert len(ctx.captured_queries) <= 3, ctx.captured_queries


# -------------------------------------------------------------------------- the cost
def test_one_home_costs_a_bounded_number_of_queries(make_user, api,
                                                    django_capture_on_commit_callbacks):
    """A ceiling, and — the part that matters — a count that does not follow the data."""
    from django.db import connection
    from django.test.utils import CaptureQueriesContext
    from training_platform.cache import private_cache

    def cost(person):
        private_cache().clear()
        with CaptureQueriesContext(connection) as ctx:
            assert api(person).get(HOME).status_code == 200
        return len(ctx.captured_queries)

    trainer, small, relation, routine, rows = _coached(make_user, api, "cost_small")
    _subscribe(small, "cost_small")
    _diet_day(small)
    _award(small, "cost_small")
    _log_sets(small, api, rows[0], 2)
    with django_capture_on_commit_callbacks(execute=True):
        _close_day(api, small, routine)
    little = cost(small)
    assert little <= QUERY_CEILING, (little, QUERY_CEILING)

    # The same person, with six times the training and three more days of it behind them.
    _trainer, _client, big_routine, big_rows = _program(
        make_user, "cost_big", trainer=trainer, client=small, exercises=6, sets=2)
    for row in big_rows:
        for day_ago in range(3):
            _log_sets(small, api, row, 2,
                      date=timezone.localdate() - timedelta(days=day_ago),
                      weight=20 + day_ago)
    for day_ago in range(3):
        _award(small, f"cost_big_{day_ago}")
    big = cost(small)

    assert big <= little + 8, (little, big)
    assert big <= QUERY_CEILING, (big, QUERY_CEILING)
