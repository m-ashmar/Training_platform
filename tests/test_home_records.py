"""The home's day timeline, its ready programmes, the coach's voice note, and the records
of a finished workout (BACKEND_DELTA items 9 and 12, the remainders; build/home-records).

What is protected here:

* ``nutrition_today.timeline`` — the artboard's «يومك» (`screens/03-home-trainers/
  Home.png`): the day's meals with today's workout placed among them by time, built from
  the same day and routine rules as the hero, with the meal's own state and the hero's
  own refusal. Additive: the section list and every key the card had are unchanged.
* ``ready_programs`` — the routine products on sale are the cards, each the catalogue's
  own row plus its length, equipment and free-preview flag; a price only on a build
  that may show one, and the whole section still absent on `play`, `ios` and a caller
  with no channel (BuildGuide's store rule). The cost does not follow the cards.
* ``trainer_message`` of kind ``voice`` — `audio_url` (signed, and it plays),
  `duration_seconds` and `state`, on that kind only.
* ``personal_records`` on the answer that finishes a workout, and only on it — the
  verdicts the logging answers gave, one per exercise, with the gain over the best that
  stood before the session; and the analytics `prs` count agrees.

On 9fb4525 every test here fails: there is no `timeline`, the cards are public
templates with no product keys, a voice note carries no `audio_url`, and the finishing
PATCH carries no `personal_records`.

See home/views.py (its docstring is the home's contract) and routine/views.py
`WorkoutSessionViewSet` (the records).
"""
from datetime import time, timedelta

import pytest
from django.utils import timezone

from tests.test_chat_media import voice
from tests.test_home import (JSON, SECTION_TYPES, _close_day, _coached, _data, _freeze,
                             _home, _log_sets, _message, _program, _subscribe, _types)
from tests.test_home import a_cold_cache, owners_rules  # noqa: F401 — autouse here too

pytestmark = pytest.mark.django_db

SESSIONS = "/api/routine/workout-sessions/"
SET_LOGS = "/api/routine/set-logs/"


@pytest.fixture
def media_root(settings, tmp_path):
    """Uploads land in a temporary directory, signed exactly as in production."""
    settings.MEDIA_ROOT = str(tmp_path)
    settings.MEDIA_URL_SIGNING = True
    return tmp_path


# ============================================================ 1. the day timeline
def _meals(client, *, start=None):
    """A plan covering today: breakfast at midnight (eaten), dinner at 23:59, and a snack
    with no time. Midnight and 23:59 keep the states the same whenever the suite runs."""
    from diet.models import DietPlan, Meal

    today = timezone.localdate()
    plan = DietPlan.objects.create(user=client, goal="Maintain", daily_calories=2300.0,
                                   target_protein=160.0, target_carbs=240.0,
                                   target_fat=70.0, start_date=start or today,
                                   end_date=today + timedelta(days=5), duration_weeks=1)
    breakfast = Meal.objects.create(diet_plan=plan, date=today, meal_type="Breakfast",
                                    name="Oats with milk", scheduled_time=time(0, 0),
                                    is_completed=True)
    dinner = Meal.objects.create(diet_plan=plan, date=today, meal_type="Dinner",
                                 name="Grilled chicken", scheduled_time=time(23, 59))
    snack = Meal.objects.create(diet_plan=plan, date=today, meal_type="Snack",
                                name="Dates", scheduled_time=None)
    return plan, breakfast, dinner, snack


def _reminder_at(person, hour):
    person.workout_reminder_hour = hour
    person.save(update_fields=["workout_reminder_hour"])


def _timeline(body):
    nutrition = _data(body, "nutrition_today")
    assert nutrition is not None, _types(body)
    return nutrition["timeline"]


def _workout(timeline):
    found = [entry for entry in timeline if entry["kind"] == "workout"]
    assert len(found) <= 1, timeline
    return found[0] if found else None


def test_the_workout_takes_its_place_among_the_meals(make_user, api):
    trainer, client, routine, rows = _program(make_user, "tl_order", sets=3)
    _subscribe(client, "tl_order")
    plan, breakfast, dinner, snack = _meals(client)
    _reminder_at(client, 0)

    body = _home(api, client)
    timeline = _timeline(body)

    assert [entry["kind"] for entry in timeline] == ["meal", "workout", "meal", "meal"]
    assert [entry.get("meal_id") for entry in timeline] == \
        [breakfast.pk, None, dinner.pk, snack.pk], "a meal with no time sorts last"
    meal = timeline[0]
    assert meal == {"kind": "meal", "at": "00:00:00", "state": "done",
                    "meal_id": breakfast.pk, "meal_type": "Breakfast",
                    "name": "Oats with milk", "calories": meal["calories"]}
    assert [entry["state"] for entry in timeline] == ["done", "now", "next", "now"]

    workout = timeline[1]
    hero = _data(body, "workout_hero")
    assert workout == {
        "kind": "workout", "at": "00:00:00", "at_source": "reminder_hour",
        "state": "now", "routine_id": routine.pk, "routine_name": hero["routine_name"],
        "day": hero["day"], "muscles": hero["muscles"],
        "exercises": hero["counts"]["exercises"], "sets": hero["counts"]["sets"],
        "estimated_minutes": hero["counts"]["estimated_minutes"]}

    # Each meal's timeline state is its card's own, renamed — never a second rule.
    renamed = {"eaten": "done", "now": "now", "upcoming": "next"}
    cards = {m["id"]: m["state"] for m in _data(body, "nutrition_today")["meals"]}
    for entry in timeline:
        if entry["kind"] == "meal":
            assert entry["state"] == renamed[cards[entry["meal_id"]]], entry


def test_the_workout_waits_for_its_hour(make_user, api):
    """Before the reminder hour the workout is `next`; the hour itself decides."""
    trainer, client, routine, rows = _program(make_user, "tl_hour")
    _subscribe(client, "tl_hour")
    _meals(client)
    _reminder_at(client, 23)
    before = timezone.localtime().time()

    workout = _workout(_timeline(_home(api, client)))
    assert (workout["at"], workout["at_source"]) == ("23:00:00", "reminder_hour")
    assert workout["state"] == ("now" if time(23, 0) <= before else "next"), before


def test_an_open_session_puts_the_workout_at_its_start(make_user, api):
    from routine.models import WorkoutSession

    trainer, client, routine, rows = _program(make_user, "tl_open")
    _subscribe(client, "tl_open")
    _meals(client)
    _reminder_at(client, 23)
    started = api(client).post(SESSIONS, {"routine": routine.pk, "day": 1},
                               content_type=JSON)
    assert started.status_code == 201, started.content
    session = WorkoutSession.objects.get(pk=started.json()["id"])

    workout = _workout(_timeline(_home(api, client)))
    at = timezone.localtime(session.start_time).time().replace(second=0, microsecond=0)
    assert (workout["at"], workout["at_source"]) == (at.isoformat(), "session")
    assert (workout["state"], workout["session_id"]) == ("now", session.pk), \
        "a workout under way is now, whatever the hour"


def test_a_closed_day_is_done_on_the_timeline(make_user, api,
                                              django_capture_on_commit_callbacks):
    trainer, client, routine, rows = _program(make_user, "tl_done", sets=2)
    _subscribe(client, "tl_done")
    _meals(client)
    _reminder_at(client, 23)
    _log_sets(client, api, rows[0], 2)
    assert _workout(_timeline(_home(api, client)))["state"] == "now", \
        "a set already logged means the workout is under way"

    with django_capture_on_commit_callbacks(execute=True):
        assert _close_day(api, client, routine).status_code == 200

    body = _home(api, client)
    assert "outcome" in _data(body, "workout_hero")
    assert _workout(_timeline(body))["state"] == "done", "the timeline was served stale"


def test_a_rest_day_has_no_workout_on_the_timeline(make_user, api):
    from routine.models import Routine

    trainer, client, routine, rows = _program(make_user, "tl_rest", days=2)
    Routine.objects.filter(pk=routine.pk).update(
        start_date=timezone.localdate() - timedelta(days=1))     # today is day 2: rest
    _subscribe(client, "tl_rest")
    _meals(client)

    timeline = _timeline(_home(api, client))
    assert _workout(timeline) is None
    assert [entry["kind"] for entry in timeline] == ["meal", "meal", "meal"]


def test_a_program_that_has_not_started_puts_nothing_on_the_day(make_user, api):
    from routine.models import Routine

    trainer, client, routine, rows = _program(make_user, "tl_later")
    Routine.objects.filter(pk=routine.pk).update(
        start_date=timezone.localdate() + timedelta(days=3))
    _subscribe(client, "tl_later")
    _meals(client)

    assert _workout(_timeline(_home(api, client))) is None


def test_a_frozen_coaching_keeps_the_workout_readable_with_its_refusal(make_user, api):
    """The owner (2026-09-19): readable, never startable — the hero's own code."""
    trainer, client, relation, routine, rows = _coached(make_user, api, "tl_frozen",
                                                        funds="100.00")
    _freeze(relation, ago=timedelta(minutes=1))
    _subscribe(client, "tl_frozen")
    _meals(client)

    body = _home(api, client)
    workout = _workout(_timeline(body))
    assert workout is not None, "a frozen coaching keeps the day visible"
    assert workout["refusal"] == _data(body, "workout_hero")["cta"]["refusal"] == \
        "coaching_frozen"


def test_the_timeline_is_additive(make_user, api):
    """No new section type, no key of the card moved, and no diet section, no timeline."""
    trainer, client, routine, rows = _program(make_user, "tl_additive")
    _subscribe(client, "tl_additive")
    _meals(client)

    body = _home(api, client)
    # The canvas's closed set of types — shared setup (`tests/test_home.py
    # SECTION_TYPES`), which the owner's `day_timeline` joined on 2026-09-28 (B-176).
    assert set(_types(body)) <= SECTION_TYPES, _types(body)
    nutrition = _data(body, "nutrition_today")
    assert set(nutrition) == {"diet_plan_id", "date", "goal", "meals", "totals",
                              "readable_only", "timeline"}

    # Without a diet entitlement the section is gone, and the timeline with it.
    bare = make_user("hr_tl_nodiet")
    _program(make_user, "tl_nodiet", client=bare)
    _meals(bare)
    body = _home(api, bare)
    assert "workout_hero" in _types(body)
    assert "nutrition_today" not in _types(body)
    assert "timeline" not in str(body)


# ======================================================= 2. the ready programmes
def _product(make_user, api, tag, **fields):
    from tests.test_routine_products import _published

    return _published(make_user, api, f"hr{tag}", **fields)


def test_a_product_on_sale_is_a_card_with_its_length_equipment_and_preview(make_user,
                                                                          api):
    from routine.models import Exercise

    trainer, template, product_id = _product(make_user, api, "card", preview_days=1)
    Exercise.objects.filter(templates=template).update(equipment="dumbbell")
    person = make_user("hr_rp_card")

    programs = _data(_home(api, person, HTTP_X_APP_CHANNEL="apk"),
                     "ready_programs")["programs"]
    assert [card["id"] for card in programs] == [product_id]
    card = programs[0]
    # The keys the section always had, from the product now.
    assert (card["name"], card["goal"], card["days_per_cycle"], card["exercises"]) == \
        ("Shoulder rehab in 4 weeks", "rehab", 3, 3)
    assert card["description"] == "For a mildly sore shoulder."
    # What the programme is: its length, what it needs, and whether it can be tried.
    assert (card["weeks"], card["session_count"], card["preview_days"]) == (4, 12, 1)
    assert card["trial"] is True
    assert (card["equipment"], card["no_equipment"]) == (["dumbbell"], False)
    # The price this build may show, with its kind of money, and the buy action.
    assert (card["price"], card["currency"], card["money_type"]) == \
        ("2500.00", "SYP", "ROUTINE_PRODUCT")
    assert card["can_buy"] is True and card["owned"] is False


def test_the_card_is_the_catalogue_row(make_user, api):
    """One definition of a programme on sale: the home card carries every key of the
    catalogue's row, with the same values."""
    trainer, template, product_id = _product(make_user, api, "same")
    person = make_user("hr_rp_same")

    card = _data(_home(api, person, HTTP_X_APP_CHANNEL="web"), "ready_programs")[
        "programs"][0]
    row = api(person).get("/api/v1/products/", HTTP_X_APP_CHANNEL="web").json()[
        "results"][0]
    for key, value in row.items():
        if key == "cover":
            continue        # a signed URL is minted per answer
        assert card[key] == value, (key, card[key], value)


def test_a_program_with_no_equipment_says_so(make_user, api):
    from routine.models import Exercise

    trainer, template, product_id = _product(make_user, api, "bw", preview_days=0)
    exercises = Exercise.objects.filter(templates=template)
    exercises.update(equipment="bodyweight")
    person = make_user("hr_rp_bw")

    card = _data(_home(api, person), "ready_programs")["programs"][0]
    assert (card["equipment"], card["no_equipment"], card["trial"]) == \
        (["bodyweight"], True, False)

    # One exercise that says nothing is not evidence of no equipment.
    Exercise.objects.filter(pk=exercises.first().pk).update(equipment=None)
    from training_platform.cache import private_cache
    private_cache().clear()
    card = _data(_home(api, person), "ready_programs")["programs"][0]
    assert (card["equipment"], card["no_equipment"]) == (["bodyweight"], False)


def test_only_what_is_on_sale_is_a_card(make_user, api):
    from routine import products
    from routine.models import RoutineProduct, RoutineTemplate
    from tests.test_home import _public_template

    person = make_user("hr_rp_only")
    # A public template that is not listed, a draft, a retired product, and a product
    # whose program changed after its review: none of them is on sale.
    lister = make_user("hr_rp_only_t", user_type="trainer", trainer_is_verified=True)
    # A code: the model takes only the closed goal list (wave-1 review R-5, 2026-10-07).
    RoutineTemplate.objects.create(name="just public", goal="strength", days=3,
                                   created_by=lister, is_public=True)
    _t1, _tpl1, draft_id = _product(make_user, api, "draft")
    RoutineProduct.objects.filter(pk=draft_id).update(status="draft")
    t2, _tpl2, retired_id = _product(make_user, api, "retired")
    products.retire(t2, retired_id)
    _t3, changed_template, changed_id = _product(make_user, api, "changed")
    RoutineTemplate.objects.filter(pk=changed_template.pk).update(
        updated_at=timezone.now() + timedelta(minutes=5))

    assert "ready_programs" not in _types(_home(api, person)), \
        "nothing on sale is not an empty row"

    listed = _public_template(make_user("hr_rp_only_t2", user_type="trainer"), "hr_only")
    on_sale = RoutineProduct.objects.get(source_template=listed)
    from training_platform.cache import private_cache
    private_cache().clear()
    programs = _data(_home(api, person), "ready_programs")["programs"]
    assert [card["id"] for card in programs] == [on_sale.pk]


def test_a_programme_just_bought_is_owned_on_the_card_at_once(
        make_user, api, django_capture_on_commit_callbacks):
    """The home is cached for 120 s; a purchase must not leave the card offering to sell
    the buyer what they have just bought."""
    from tests.test_routine_products import _buy, _client

    trainer, template, product_id = _product(make_user, api, "bought")
    buyer = _client(make_user, "hr_bought")
    before = _data(_home(api, buyer), "ready_programs")["programs"][0]
    assert (before["owned"], before["can_buy"]) == (False, True)

    with django_capture_on_commit_callbacks(execute=True):
        bought = _buy(api, buyer, product_id)
    assert bought.status_code in (200, 201), bought.content

    after = _data(_home(api, buyer), "ready_programs")["programs"][0]
    assert (after["owned"], after["can_buy"]) == (True, False), "served from a stale cache"
    assert after["routine_id"] == bought.json()["purchase"]["routine_id"]


@pytest.mark.parametrize("channel", ["play", "ios", ""])
def test_a_store_build_gets_no_programme_and_no_price(make_user, api, channel):
    """BuildGuide: the section is filtered by the same flags as the banner — absent, not
    emptied. Nothing of the product reaches the payload, its title included."""
    trainer, template, product_id = _product(make_user, api, f"store{channel or 'x'}")
    person = make_user(f"hr_rp_store_{channel or 'none'}")

    body = _home(api, person, HTTP_X_APP_CHANNEL=channel)
    assert "ready_programs" not in _types(body)
    assert "Shoulder rehab in 4 weeks" not in str(body)
    assert "ROUTINE_PRODUCT" not in str(body)


def test_the_cost_of_the_programmes_does_not_follow_the_cards(make_user, api):
    """One card or four: the same queries (the catalogue's, and one for every card's
    exercises and equipment)."""
    from django.db import connection
    from django.test.client import RequestFactory
    from django.test.utils import CaptureQueriesContext

    from home import first_day
    from policy.services import flags_for

    person = make_user("hr_rp_cost")
    request = RequestFactory().get("/api/v1/home", HTTP_X_APP_CHANNEL="apk")
    request.user = person

    def cost():
        if hasattr(request, "_product_price_memo"):
            del request._product_price_memo
        with CaptureQueriesContext(connection) as ctx:
            section = first_day.ready_programs(person, request=request,
                                               flags=flags_for("apk"))
        return len(ctx.captured_queries), len(section["programs"])

    _product(make_user, api, "cost0")
    little, one = cost()
    for index in range(1, 5):
        _product(make_user, api, f"cost{index}")
    big, four = cost()

    assert (one, four) == (1, 4)
    assert big <= little, (little, big)
    assert big <= 9, (little, big)


# ============================================================= 3. the voice note
def _voice_from_coach(make_user, api, tag, seconds=24.0):
    from chat.services import open_conversation

    trainer, client, relation, routine, rows = _coached(make_user, api, tag)
    thread = open_conversation(trainer, client)
    sent = api(trainer).post("/api/v1/chat/media/",
                             {"conversation": thread.pk, "kind": "voice",
                              "file": voice(seconds)})
    assert sent.status_code == 201, sent.content
    return trainer, client, sent.json()


def test_a_voice_note_from_the_coach_plays_on_the_card(make_user, api, media_root):
    trainer, client, sent = _voice_from_coach(make_user, api, "hr_voice")

    message = _data(_home(api, client), "trainer_message")["message"]
    assert (message["id"], message["kind"], message["deleted"]) == \
        (sent["id"], "voice", False)
    assert (message["state"], message["duration_seconds"]) == ("ready", 24.0)
    assert message["audio_url"], message
    assert api().get(message["audio_url"]).status_code == 200, \
        "the signed URL on the card does not play"


def test_a_voice_note_taken_back_has_nothing_to_play(make_user, api, media_root,
                                                     django_capture_on_commit_callbacks):
    trainer, client, sent = _voice_from_coach(make_user, api, "hr_voice_back")
    with django_capture_on_commit_callbacks(execute=True):
        gone = api(trainer).delete(f"/api/v1/chat/messages/{sent['id']}/")
    assert gone.status_code in (200, 204), gone.content

    message = _data(_home(api, client), "trainer_message")["message"]
    assert (message["kind"], message["deleted"], message["state"]) == \
        ("voice", True, "unavailable")
    assert (message["audio_url"], message["duration_seconds"]) == (None, None)


def test_a_text_message_carries_no_voice_keys(make_user, api):
    """Additive to kind `text`: the three keys belong to a voice note alone."""
    trainer, client, relation, routine, rows = _coached(make_user, api, "hr_text")
    _message(trainer, client, body="Rest today.")

    message = _data(_home(api, client), "trainer_message")["message"]
    assert set(message) == {"id", "kind", "body", "deleted", "sent_at"}
    assert message["kind"] == "text"


# ========================================================= 4. the records of a workout
def _baseline(client, exercise, *, weight, reps, days_ago=10):
    """A set trained before the window, written as history (no verdict of its own)."""
    from routine.models import ExerciseSetLog, UserExerciseProgress

    day = timezone.localdate() - timedelta(days=days_ago)
    progress = UserExerciseProgress.objects.create(user=client, exercise=exercise, date=day)
    ExerciseSetLog.objects.create(user_exercise_progress=progress, set_number=1,
                                  weight=weight, reps=reps, date=day)


def _open(api, client, routine):
    started = api(client).post(SESSIONS, {"routine": routine.pk, "day": 1},
                               content_type=JSON)
    assert started.status_code == 201, started.content
    return started.json()["id"]


def _set(api, client, row, session_id, number, weight, reps):
    from routine.models import UserExerciseProgress

    today = timezone.localdate()
    progress, _created = UserExerciseProgress.objects.get_or_create(
        user=client, exercise=row.exercise, date=today, defaults={"target_sets": row.sets})
    logged = api(client).post(SET_LOGS, {
        "user_exercise_progress": progress.pk, "workout_session": session_id,
        "set_number": number, "weight": weight, "reps": reps,
        "date": today.isoformat()}, content_type=JSON)
    assert logged.status_code in (200, 201), logged.content
    return logged.json()


def _finish(api, client, session_id):
    return api(client).patch(f"{SESSIONS}{session_id}/", {"status": "completed"},
                             content_type=JSON)


def test_the_finishing_patch_answers_the_records_of_the_session(make_user, api):
    trainer, client, routine, rows = _program(make_user, "hr_pr", exercises=2, sets=3,
                                              reps=8, weight=80)
    bench, row_press = rows[0].exercise, rows[1]
    _baseline(client, bench, weight=80, reps=8)
    session_id = _open(api, client, routine)

    first = _set(api, client, rows[0], session_id, 1, 80, 8)       # equal: no record
    second = _set(api, client, rows[0], session_id, 2, 82.5, 8)    # a record
    third = _set(api, client, rows[0], session_id, 3, 85, 5)       # another one
    assert [first["personal_record"], second["personal_record"],
            third["personal_record"]] == [False, True, True]
    _set(api, client, row_press, session_id, 1, 40, 10)            # a first set: a baseline

    finished = _finish(api, client, session_id)
    assert finished.status_code == 200, finished.content
    records = finished.json()["personal_records"]

    assert len(records) == 1, "one entry per exercise, and a first-ever set is no record"
    record = records[0]
    assert record["exercise"] == {"id": bench.pk, "name": bench.name}
    assert (record["set_log_id"], record["weight"], record["reps"]) == (third["id"], 85.0, 5)
    assert record["date"] == timezone.localdate().isoformat()
    # The best that stood before the session's records began — exactly what the first
    # record's own answer said it beat — so the card reads +5 kg, not +2.5.
    assert record["previous_best"] == second["previous_best"]
    assert record["previous_best"]["weight"] == 80.0
    assert record["gain_kg"] == 5.0

    # The analytics count agrees with the card: one exercise set a record this week.
    summary = api(client).get("/api/routine/analytics/summary/?period=week").json()
    assert summary["prs"] == len(records)


def test_only_the_finish_carries_the_records(make_user, api):
    trainer, client, routine, rows = _program(make_user, "hr_pr_once", weight=60)
    _baseline(client, rows[0].exercise, weight=60, reps=5)
    session_id = _open(api, client, routine)
    _set(api, client, rows[0], session_id, 1, 65, 5)

    opened = api(client).patch(f"{SESSIONS}{session_id}/", {"effort_rating": 3},
                               content_type=JSON)
    assert opened.status_code == 200, opened.content
    assert "personal_records" not in opened.json(), "a PATCH that finishes nothing"

    finished = _finish(api, client, session_id)
    assert [r["weight"] for r in finished.json()["personal_records"]] == [65.0]

    again = _finish(api, client, session_id)
    assert again.status_code == 200, again.content
    assert "personal_records" not in again.json(), "a re-sent finish is not a transition"
    assert "personal_records" not in api(client).get(f"{SESSIONS}{session_id}/").json()


def test_a_workout_with_no_record_answers_an_empty_list(make_user, api):
    trainer, client, routine, rows = _program(make_user, "hr_pr_none", weight=60)
    _baseline(client, rows[0].exercise, weight=100, reps=5)
    session_id = _open(api, client, routine)
    _set(api, client, rows[0], session_id, 1, 60, 5)

    finished = _finish(api, client, session_id)
    assert finished.json()["personal_records"] == [], "the card stays hidden"


def test_a_record_of_another_session_is_not_this_ones(make_user, api):
    trainer, client, routine, rows = _program(make_user, "hr_pr_other", weight=60)
    _baseline(client, rows[0].exercise, weight=60, reps=5)
    earlier = _open(api, client, routine)
    _set(api, client, rows[0], earlier, 1, 70, 5)                  # the record, there
    assert _finish(api, client, earlier).json()["personal_records"][0]["weight"] == 70.0

    later = _open(api, client, routine)
    _set(api, client, rows[0], later, 2, 65, 5)                    # lighter: none here
    assert _finish(api, client, later).json()["personal_records"] == []


def test_an_offline_workout_synced_finished_carries_the_key(make_user, api):
    """The POST of a session already completed is the finish (BACKEND_BUGS B-003)."""
    trainer, client, routine, rows = _program(make_user, "hr_pr_sync")
    created = api(client).post(SESSIONS, {
        "routine": routine.pk, "day": 1, "status": "completed",
        "start_time": (timezone.now() - timedelta(hours=2)).isoformat(),
        "end_time": (timezone.now() - timedelta(hours=1)).isoformat()},
        content_type=JSON)
    assert created.status_code == 201, created.content
    assert created.json()["personal_records"] == []
