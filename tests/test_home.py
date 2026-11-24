"""The home screen is decided by the server (BACKEND_DELTA item 9, BuildGuide layer 4).

`GET /api/v1/home` answers an ORDERED list of typed sections from the design canvas's
closed set, and the app renders what it is handed. What is protected here: a person
with nothing still gets a usable screen, a full one gets every section in the
documented order, a frozen coaching keeps its day visible and unstartable (the owner,
2026-09-19), a build that may not sell never receives the two sections that sell, a
role without a designed shell gets an empty list rather than someone else's screen,
the cache separates builds and languages and dies when a day closes or a message
arrives, and one home load costs a bounded number of queries.

See home/views.py — its docstring is the contract.
"""
import uuid
from datetime import time, timedelta

import pytest
from django.utils import timezone

from tests.test_c9_payments import _plan
from tests.test_coaching_months import OWNER_RATE, _accepted, _end_month

pytestmark = pytest.mark.django_db

HOME = "/api/v1/home"
JSON = "application/json"

#: What one cold home load may cost. Measured on this fixture, then rounded up a
#: little: 24 queries for a client with nothing, 29 for the first-day list (the trainer
#: matcher is the expensive part), 38 for a full active client — coach, closed day,
#: plan, meals, message, challenge and smart note — and 43 for that same person with
#: six times the training, five more meals and five more messages. A ceiling, not a
#: target: the test beside it also grows the data and asserts the count does not
#: follow, which is the part that catches an N+1. The only thing that grows with data
#: at all is the strength headline, one query per exercise it ranks, bounded at six
#: (`routine/recent.py STRENGTH_LIMIT`).
#:
#: Re-recorded 48 -> 49 (2026-11-24, the owner's ruling on SIDE_BUGS S-292): the build's
#: flags are now read fresh on every load (`policy_channelcapabilities` is on
#: CACHALOT_UNCACHABLE_TABLES), so the one flags read that cachalot used to answer from
#: its cache after the first request is now always a query. Measured on the test's own
#: data: the large load is 48 with the flags cached and 49 with them fresh, exactly one
#: `policy_channelcapabilities` read; nothing else moved.
QUERY_CEILING = 49

#: The minutes of `_program`'s day (one exercise, 3 sets of 10, the model's 60 s rest):
#: 3 x 10 x 4 s + 2 x 60 s = 240 s. Deliberate contract change (Road to 10 S0.2,
#: BACKEND_BUGS B-196, 2026-11-22): the hero's `estimated_minutes` is the DAY's, by the
#: routine's own per-set formula (`routine/schedule.py day_minutes`), the number Train and
#: the Day screen show; it was the whole routine's `estimated_duration` (60).
ONE_DAY_MINUTES = 4

#: The first-day list: the sections only a client with NO coach receives.
#: Deliberate contract change (the owner, 2026-09-28, BACKEND_BUGS B-182):
#: `ready_programs` left this list — a coached client is also shown the programmes on
#: sale, by the same build rule (absent where prices may not show). The test that reads
#: this still asserts what it was written for: a client with a coach gets none of the
#: first-day sections, and the banner stays for everyone.
FIRST_DAY_SECTIONS = ("find_trainer_hero", "setup_checklist", "featured_trainers")

#: The canvas's closed set of section types (BuildGuide layer 4) the home may answer.
#: Deliberate contract change (the owner, 2026-09-28, BACKEND_BUGS B-176): a 12th type,
#: `day_timeline` — the «يومك» rail as a section of its own, so the server's order can
#: be the artboard's (rail, then the week tiles, then the calories card). Additive:
#: `nutrition_today.timeline` stays, and an installed build skips a type it does not
#: know. The tests that read this still assert that no type outside the set is sent.
SECTION_TYPES = frozenset({
    "workout_hero", "find_trainer_hero", "setup_checklist", "trainer_message",
    "day_timeline", "nutrition_today", "week_stats", "ai_advice", "challenge_rank",
    "featured_trainers", "ready_programs", "media_banner"})

#: What the full client of `test_a_full_client_gets_every_section_in_a_sensible_order`
#: is answered, in order. Same deliberate change as `SECTION_TYPES` (B-176): the rail
#: (`day_timeline`) comes after the coach's message, then `week_stats`, then
#: `nutrition_today` — `Home.html`'s «يومك», «هذا الأسبوع», «سعرات اليوم». The test still
#: asserts every section of a full client, in one documented order.
FULL_CLIENT_SECTIONS = ["workout_hero", "trainer_message", "day_timeline", "week_stats",
                        "nutrition_today", "ai_advice", "challenge_rank"]


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
        exercise = Exercise.objects.create(name=f"home ex {tag} {index}",
                                           created_by=trainer, target_muscle="Triceps")
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


def _subscribe(client, tag, *, ends_in=timedelta(days=20), status="active", ai=True):
    from subscription.models import Subscription

    plan = _plan(f"home {tag}", has_diet_access=True, has_ai_advice=ai)
    sub = Subscription.objects.create(user=client, plan=plan, status="active",
                                      source="shamcash")
    Subscription.objects.filter(pk=sub.pk).update(
        status=status, start_date=timezone.now() - timedelta(days=30),
        end_date=timezone.now() + ends_in)
    return Subscription.objects.get(pk=sub.pk)


def _diet_day(client, *, calories=2300.0, eaten=1420.0, meals=2, days_back=6):
    """A plan covering today, two meals, and a week of tracked days behind it."""
    from diet.models import DailyProgress, DietPlan, Meal

    today = timezone.localdate()
    plan = DietPlan.objects.create(user=client, goal="Maintain", daily_calories=calories,
                                   target_protein=160.0, target_carbs=240.0, target_fat=70.0,
                                   start_date=today - timedelta(days=days_back),
                                   end_date=today + timedelta(days=5), duration_weeks=1)
    Meal.objects.create(diet_plan=plan, date=today, meal_type="Breakfast",
                        name="Oats with milk", description="oats, milk, banana",
                        scheduled_time=time(8, 0), is_completed=True)
    Meal.objects.create(diet_plan=plan, date=today, meal_type="Dinner",
                        name="Grilled chicken", description="chicken and freekeh",
                        scheduled_time=time(23, 59))
    for back in range(days_back + 1):
        DailyProgress.objects.create(
            user=client, diet_plan=plan, date=today - timedelta(days=back),
            meals_completed=meals, total_meals=4,
            calories_consumed=eaten, protein_consumed=96.0,
            carbs_consumed=150.0, fat_consumed=41.0,
            target_calories=calories, target_protein=160.0,
            target_carbs=240.0, target_fat=70.0)
    return plan


def _consent(client):
    from moderation.models import AIConsent

    return AIConsent.objects.create(user=client, granted_at=timezone.now(),
                                    policy_version="1")


def _message(trainer, client, body="Great week. Add 2.5 kg on the press today."):
    from chat.models import Conversation, Message

    thread, _created = Conversation.objects.get_or_create(trainer=trainer, client=client)
    return Message.objects.create(conversation=thread, sender=trainer, body=body)


def _challenge(client, tag, *, others=2, value=12.0):
    """A running challenge this client is in, with `others` people ahead of them."""
    from social.models import Challenge, ChallengeParticipation

    now = timezone.now()
    challenge = Challenge.objects.create(
        title=f"home {tag}", description="a test challenge", challenge_type="workout_count",
        creator=client, start_date=now - timedelta(days=2), end_date=now + timedelta(days=5),
        status="active", target_value=20.0, unit="workouts", participants_count=others + 1)
    ChallengeParticipation.objects.create(user=client, challenge=challenge,
                                          current_value=value, progress_percentage=60.0)
    for index in range(others):
        rival = type(client).objects.create_user(
            email=f"rival_{tag}_{index}@gate.test", username=f"rival_{tag}_{index}",
            password="Xx!23456")
        ChallengeParticipation.objects.create(user=rival, challenge=challenge,
                                              current_value=value + index + 1)
    return challenge


def _banner():
    from mediakit.models import MediaAsset
    from mediakit.slots import APP_OWNER_ID, OwnerType

    return MediaAsset.objects.create(
        owner_type=OwnerType.APP, owner_id=APP_OWNER_ID, slot="app.banner",
        original="mediakit/banner.jpg", file_1x="mediakit/banner_1x.webp",
        file_2x="mediakit/banner_2x.webp", alt_ar="لافتة", alt_en="banner",
        width=1440, height=617, processing_state="ready")


def _public_template(trainer, tag):
    """A ready programme exists: a public template, LISTED FOR SALE.

    Deliberate contract change (build/home-records, BACKEND_DELTA item 9): the
    `ready_programs` cards are the routine products on sale (plan step 10), not public
    templates, so "a ready programme exists" now means a published product. The tests
    that call this still assert exactly what they were written to protect — which
    builds receive the section, that no price reaches a store build, that the cache
    separates builds, and that a coached client gets no first-day list.
    """
    from decimal import Decimal

    from routine.models import (Exercise, RoutineProduct, RoutineTemplate,
                                RoutineTemplateExercise)

    # A code: the model takes only the closed goal list (wave-1 review R-5, 2026-10-07).
    template = RoutineTemplate.objects.create(
        name=f"ready {tag}", description="a ready programme", goal="strength",
        days=3, created_by=trainer, is_public=True)
    exercise = Exercise.objects.create(name=f"tmpl ex {tag}", created_by=trainer)
    RoutineTemplateExercise.objects.create(template=template, exercise=exercise,
                                           day=1, order=1, sets=3, reps=10)
    # On sale needs a verified trainer and the template version the review saw
    # (`RoutineProduct.is_on_sale`); the review itself is `routine/products.py`'s test.
    type(trainer).objects.filter(pk=trainer.pk).update(trainer_is_verified=True)
    template.refresh_from_db()
    RoutineProduct.objects.create(
        trainer=trainer, source_template=template, title_en=f"ready {tag}",
        price=Decimal("2500.00"), currency="SYP", status="published",
        template_version=template.updated_at, published_at=timezone.now())
    return template


def _freeze(relation, **ago):
    """The month ended and the hourly run could not charge the next one."""
    from unittest import mock

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

    assert set(body) == {"role", "as_of", "refresh_after_seconds", "sections"}
    assert (body["role"], body["refresh_after_seconds"]) == ("client", 120)
    for section in body["sections"]:
        assert set(section) == {"type", "data"}, section
        assert section["data"], f"{section['type']} was sent empty"
    # The only thing the server can honestly say to someone with nothing.
    assert _types(body) == ["setup_checklist"]
    checklist = _data(body, "setup_checklist")
    assert checklist["current"] == "profile"
    assert [step["state"] for step in checklist["steps"]] == ["current", "todo", "todo"]


@pytest.mark.parametrize("role", ["trainer", "agent", "admin"])
def test_a_role_without_a_designed_shell_gets_an_empty_list(make_user, api, role):
    """Their shells are later batches; the endpoint is theirs and says so, rather than
    refusing or handing them a client's screen."""
    person = make_user(f"home_role_{role}", user_type=role)

    body = _home(api, person)
    assert (body["role"], body["sections"]) == (role, [])
    assert api().get(HOME).status_code == 401


# ------------------------------------------------------------------ the full screen
def test_a_full_client_gets_every_section_in_a_sensible_order(make_user, api,
                                                              django_capture_on_commit_callbacks):
    from home.sections import week_start
    from routine.models import Routine

    trainer, client, relation, routine, rows = _coached(make_user, api, "full")
    # A full client's program was already running when this week began. `start_date`
    # defaults to today, and since BACKEND_BUGS B-045 no day before a program starts is
    # planned — so the `planned: 7` below held only when this ran on the week's first day
    # (a Saturday). Setup, not the assertion: the start stays inside this week, so the
    # hero's week 1 / day 1 are unchanged.
    Routine.objects.filter(pk=routine.pk).update(
        start_date=week_start(timezone.localdate()))
    _subscribe(client, "full")
    _diet_day(client)
    _consent(client)
    _message(trainer, client)
    _challenge(client, "full")
    _log_sets(client, api, rows[0], 3)          # the whole day, so it counts
    with django_capture_on_commit_callbacks(execute=True):
        assert _close_day(api, client, routine).status_code == 200

    body = _home(api, client)

    assert _types(body) == FULL_CLIENT_SECTIONS

    hero = _data(body, "workout_hero")
    assert (hero["routine_id"], hero["week"], hero["day"], hero["days_in_cycle"]) == \
        (routine.pk, 1, 1, 1)
    assert hero["muscles"] == ["Triceps"]
    assert hero["counts"] == {"exercises": 1, "sets": 3,
                              "estimated_minutes": ONE_DAY_MINUTES}
    assert hero["sets"] == {"prescribed": 3, "logged": 3, "unlogged": 0}
    assert (hero["outcome"]["complete"], hero["outcome"]["missed_sets"]) == (True, 0)
    assert hero["cta"] == {"state": "continue"}
    assert len(hero["week_strip"]) == 7
    today = timezone.localdate().isoformat()
    assert [day for day in hero["week_strip"] if day["date"] == today][0]["state"] == "today"

    message = _data(body, "trainer_message")
    assert (message["from"]["id"], message["from_coach"]) == (trainer.pk, True)
    assert message["message"]["kind"] == "text"
    assert message["unread_count"] == 1

    nutrition = _data(body, "nutrition_today")
    assert [meal["state"] for meal in nutrition["meals"]] == ["eaten", "upcoming"]
    assert nutrition["meals"][0]["name"] == "Oats with milk"
    assert nutrition["totals"]["targets"] == {"calories": 2300.0, "protein_g": 160.0,
                                              "carbs_g": 240.0, "fat_g": 70.0}
    assert nutrition["totals"]["eaten"]["calories"] == 1420.0
    assert nutrition["readable_only"] is False

    week = _data(body, "week_stats")
    assert week["workouts"] == {"done": 1, "planned": 7}
    assert week["streak"]["current"] == 1
    # The same volume figure the week endpoint answers, from the same entry.
    endpoint = api(client).get("/api/routine/v1/analytics/recent-progress/").json()
    assert week["volume_kg"]["logged"] == endpoint["adherence"]["volume_kg"]["logged"]

    advice = _data(body, "ai_advice")
    assert advice["code"] == "protein_short"
    assert advice["numbers"]["gap_g"] == 64.0 and advice["numbers"]["days_tracked"] == 7
    assert advice["coach"] == {"available": True, "reason": None}

    rank = _data(body, "challenge_rank")
    assert (rank["rank"], rank["participants"]) == (3, 3)


def test_a_rest_day_says_so_and_cannot_be_started(make_user, api):
    """The routine's cycle lands on a day the coach prescribed nothing for."""
    from routine.models import Routine

    from routine.models import DayOutcome

    trainer, client, routine, rows = _program(make_user, "rest", days=2)
    # Day 1 has the exercises; start the routine yesterday so today is day 2.
    Routine.objects.filter(pk=routine.pk).update(
        start_date=timezone.localdate() - timedelta(days=1))
    # Deliberate contract change (the owner's D15, 2026-11-22, BACKEND_BUGS B-197):
    # today is the next day not done, never the calendar cycle, so a day 1 nobody trained
    # yesterday is still today's workout. Day 2 is the rest day once day 1 is DONE, as
    # here: the rest that follows a closed day is kept (`routine/schedule.py`).
    DayOutcome.objects.create(user=client, routine=routine, day=1,
                              date=timezone.localdate() - timedelta(days=1),
                              via="completed", complete=True, planned_sets=3,
                              logged_sets=3)

    hero = _data(_home(api, client), "workout_hero")
    assert (hero["day"], hero["cta"]) == (2, {"state": "rest"})
    assert "sets" not in hero
    assert hero["counts"]["exercises"] == 0


def test_an_open_session_is_continued_not_started_again(make_user, api):
    trainer, client, routine, rows = _program(make_user, "open")
    started = api(client).post("/api/routine/workout-sessions/",
                               {"routine": routine.pk, "day": 1}, content_type=JSON)
    assert started.status_code in (200, 201), started.content

    hero = _data(_home(api, client), "workout_hero")
    assert hero["session"]["routine_id"] == routine.pk
    assert hero["cta"]["state"] == "continue" and hero["cta"]["session_id"]


def test_a_session_nobody_ever_closed_does_not_block_tomorrow(make_user, api):
    """Nothing abandons a forgotten session, and one may not last longer than 12 hours:
    past that it is not "open", or the button would never come back."""
    from routine.models import WorkoutSession

    trainer, client, routine, rows = _program(make_user, "forgotten")
    session = WorkoutSession.objects.create(user=client, routine=routine, day=1)
    WorkoutSession.objects.filter(pk=session.pk).update(
        start_time=timezone.now() - timedelta(hours=13))

    hero = _data(_home(api, client), "workout_hero")
    assert "session" not in hero
    assert hero["cta"] == {"state": "start"}


# ----------------------------------------------------------------------- the freeze
def test_a_frozen_client_still_sees_the_day_and_cannot_start_it(make_user, api):
    """The owner (2026-09-19): never deleted, readable, and it cannot be used."""
    trainer, client, relation, routine, rows = _coached(make_user, api, "frozen",
                                                        funds="100.00")
    _freeze(relation, ago=timedelta(minutes=1))

    hero = _data(_home(api, client), "workout_hero")
    assert hero is not None, "a frozen coaching keeps the day visible"
    assert hero["cta"] == {"state": "refused", "refusal": "coaching_frozen"}
    assert hero["sets"]["prescribed"] == 3, "and readable"
    # The refusal is the code the training writes answer, not a sentence to display.
    write = api(client).post("/api/routine/workout-sessions/",
                             {"routine": routine.pk, "day": 1}, content_type=JSON)
    assert (write.status_code, write.json()["code"]) == (403, hero["cta"]["refusal"])


def test_a_lapsed_subscription_leaves_the_day_readable(make_user, api):
    trainer, client, routine, rows = _program(make_user, "lapsed")
    _subscribe(client, "lapsed", ends_in=-timedelta(hours=2), status="expired")
    _diet_day(client)

    nutrition = _data(_home(api, client), "nutrition_today")
    assert nutrition is not None and nutrition["readable_only"] is True


def test_a_client_with_no_diet_entitlement_loses_the_section_not_the_screen(make_user, api):
    trainer, client, routine, rows = _program(make_user, "nodiet")
    _diet_day(client)                      # a plan exists, but nothing paid for it

    body = _home(api, client)
    assert _data(body, "nutrition_today") is None
    assert "workout_hero" in _types(body), "the rest of the screen is unaffected"


def test_no_smart_note_before_consent(make_user, api):
    trainer, client, routine, rows = _program(make_user, "noconsent")
    _subscribe(client, "noconsent")
    _diet_day(client)

    assert _data(_home(api, client), "ai_advice") is None
    _consent(client)
    from training_platform.cache import private_cache
    private_cache().clear()
    assert _data(_home(api, client), "ai_advice")["code"] == "protein_short"


# ------------------------------------------------------------------------- the build
@pytest.mark.parametrize("channel,sells", [("apk", True), ("web", True),
                                           ("play", False), ("ios", False), ("", False)])
def test_the_two_selling_sections_reach_only_a_build_that_may_sell(make_user, api,
                                                                   channel, sells):
    """BuildGuide: "the media_banner and ready_programs sections are filtered by the
    same flags". Left out entirely — not emptied, not stripped."""
    trainer = make_user(f"hm_sell_t_{channel or 'none'}", user_type="trainer")
    person = make_user(f"hm_sell_c_{channel or 'none'}")
    _public_template(trainer, f"sell{channel or 'none'}")
    _banner()

    types = _types(_home(api, person, HTTP_X_APP_CHANNEL=channel))
    assert ("ready_programs" in types) is sells, types
    assert ("media_banner" in types) is sells, types


def test_a_build_that_may_show_no_fee_is_sent_no_price_at_all(make_user, api):
    """`ios` and a caller that declares no build may show neither a plan price nor a
    coaching fee, so nothing in the whole payload may carry one. (`play` MAY show a
    coaching fee — that is the test below.)"""
    trainer = make_user("hm_price_t", user_type="trainer",
                        trainer_is_available=True, email_verified=True,
                        trainer_hourly_rate="12000.00", trainer_goal_focus=["build_muscle"])
    person = make_user("hm_price_c", training_goal="build_muscle")
    _public_template(trainer, "price")
    _banner()

    for channel in ("ios", ""):
        body = _home(api, person, HTTP_X_APP_CHANNEL=channel)
        words = " ".join(_strings_and_keys(body)).lower()
        for forbidden in ("price", "money_type", "currency", "checkout", "hub"):
            assert forbidden not in words, (channel, forbidden)


def test_the_coaching_fee_follows_the_flag_that_governs_it(make_user, api):
    """A coach is a person-to-person service: the section stays on every build and only
    the fee comes and goes, by `can_show_coaching_prices`."""
    # Deliberate contract change (Road to 10 S0.3, BACKEND_BUGS B-198, 2026-11-22): a
    # trainer with no first name is not listed or matched, so a listed coach has one.
    trainer = make_user("hm_fee_t", user_type="trainer", trainer_is_available=True,
                        email_verified=True, trainer_hourly_rate="12000.00",
                        trainer_goal_focus=["build_muscle"], first_name="Rami")
    person = make_user("hm_fee_c", training_goal="build_muscle")

    shown = _data(_home(api, person, HTTP_X_APP_CHANNEL="play"), "featured_trainers")
    hidden = _data(_home(api, person, HTTP_X_APP_CHANNEL="ios"), "featured_trainers")
    assert shown["trainers"][0]["trainer"]["coaching_price"] == "12000.00"
    assert "coaching_price" not in hidden["trainers"][0]["trainer"]
    assert "money_type" not in hidden["trainers"][0]["trainer"]


def test_the_first_day_hero_counts_the_trainers_who_suit_the_goal(make_user, api):
    for index in range(2):
        # Named: a trainer with no first name is not listed (S0.3, B-198, 2026-11-22).
        make_user(f"hm_match_{index}", user_type="trainer", trainer_is_available=True,
                  email_verified=True, trainer_goal_focus=["build_muscle"],
                  first_name="Coach")
    make_user("hm_match_other", user_type="trainer", trainer_is_available=True,
              email_verified=True, trainer_goal_focus=["fat_loss"], first_name="Coach")
    person = make_user("hm_match_c", training_goal="build_muscle")

    body = _home(api, person)
    assert _data(body, "find_trainer_hero") == {"goal": "build_muscle", "matches": 2}
    assert _types(body)[:2] == ["find_trainer_hero", "setup_checklist"]


def test_a_client_with_a_coach_gets_no_first_day_list(make_user, api):
    trainer, client, relation, routine, rows = _coached(make_user, api, "nofirstday")
    _public_template(trainer, "nofirstday")
    _banner()

    types = _types(_home(api, client))
    for first_day_section in FIRST_DAY_SECTIONS:
        assert first_day_section not in types, types
    assert "media_banner" in types, "the banner is for everyone"


# -------------------------------------------------------------------------- the cache
def test_the_cache_does_not_leak_between_builds(make_user, api):
    trainer = make_user("hm_leak_t", user_type="trainer")
    person = make_user("hm_leak_c")
    _public_template(trainer, "leak")

    warm = _home(api, person, HTTP_X_APP_CHANNEL="apk")
    store = _home(api, person, HTTP_X_APP_CHANNEL="ios")

    assert "ready_programs" in _types(warm)
    assert "ready_programs" not in _types(store), "the apk answer reached a store build"
    assert "ready_programs" in _types(_home(api, person, HTTP_X_APP_CHANNEL="apk"))


def test_the_cache_does_not_leak_between_languages(make_user, api):
    from routine.models import Routine

    trainer, client, routine, rows = _program(make_user, "lang")
    Routine.objects.filter(pk=routine.pk).update(translations={"ar": {"name": "قوة"}})

    english = _home(api, client, HTTP_ACCEPT_LANGUAGE="en")
    arabic = _home(api, client, HTTP_ACCEPT_LANGUAGE="ar")

    assert _data(english, "workout_hero")["routine_name"] == "home lang"
    assert _data(arabic, "workout_hero")["routine_name"] == "قوة"


def test_closing_a_day_shows_on_the_home_at_once(make_user, api,
                                                 django_capture_on_commit_callbacks):
    """The payload is cached for 120 s; the day that closes must not wait for it."""
    trainer, client, routine, rows = _program(make_user, "bust", sets=2)
    _log_sets(client, api, rows[0], 2)

    before = _data(_home(api, client), "workout_hero")
    assert "outcome" not in before

    with django_capture_on_commit_callbacks(execute=True):
        assert _close_day(api, client, routine).status_code == 200

    after = _data(_home(api, client), "workout_hero")
    assert after["outcome"]["complete"] is True, "the home was served from a stale cache"
    assert _data(_home(api, client), "week_stats")["streak"]["current"] == 1


def test_a_message_from_the_coach_shows_on_the_home_at_once(make_user, api,
                                                            django_capture_on_commit_callbacks):
    trainer, client, relation, routine, rows = _coached(make_user, api, "msgbust")
    assert _data(_home(api, client), "trainer_message") is None

    with django_capture_on_commit_callbacks(execute=True):
        _message(trainer, client, body="Rest today.")

    message = _data(_home(api, client), "trainer_message")
    assert message is not None and message["message"]["body"] == "Rest today."


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
    _consent(small)
    _message(trainer, small)
    _challenge(small, "cost_small")
    _log_sets(small, api, rows[0], 2)
    with django_capture_on_commit_callbacks(execute=True):
        _close_day(api, small, routine)
    little = cost(small)
    assert little <= QUERY_CEILING, (little, QUERY_CEILING)

    # The same person, with six times the training, three more days of it, five more
    # meals today and five more messages from their coach.
    _t, _c, big_routine, big_rows = _program(
        make_user, "cost_big", trainer=trainer, client=small, exercises=6, sets=2)
    for row in big_rows:
        for day_ago in range(3):
            _log_sets(small, api, row, 2,
                      date=timezone.localdate() - timedelta(days=day_ago),
                      weight=20 + day_ago)
    from diet.models import DietPlan, Meal
    plan = DietPlan.objects.filter(user=small).first()
    for index in range(5):
        Meal.objects.create(diet_plan=plan, date=timezone.localdate(), meal_type="Snack",
                            name=f"snack {index}", scheduled_time=time(10, index))
    for index in range(5):
        _message(trainer, small, body=f"note {index}")
    big = cost(small)

    assert big <= little + 8, (little, big)
    assert big <= QUERY_CEILING, (big, QUERY_CEILING)
