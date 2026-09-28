"""What wiring the home found on 2026-09-28 (BACKEND_BUGS B-175, B-176, B-178, B-182,
B-179; SIDE_BUGS S-193), and the owner's decisions of that day on the home's heroes.

| Row | Finding | Tests |
|---|---|---|
| B-182 | an uncoached programme buyer got BOTH heroes (the day's and the first day's) | `test_b182_*` |
| B-175 | a coach's program card on the home did not name its routine | `test_b175_*` |
| B-176 | the artboard's order (rail, week, calories) could not be answered | `test_b176_*` |
| B-178 | the contract gave the «الآن» pill to an untimed meal over a timed workout | `test_b178_*` |
| B-179 | a meal due inside the 120 s cache window stayed `upcoming` until it expired | `test_b179_*` |
| S-193 | `challenge_rank.participants` was said to count dropped participants | `test_s193_*` |

The owner's decisions (2026-09-28), which the B-182 tests pin: an uncoached programme
buyer gets the day hero alone; a former client whose coaching ended and whose day is
refused is led by the find-a-trainer hero; a coached client is also shown the programmes
on sale (`ready_programs`, same build gating, an owned one `owned: true`,
`can_buy: false`).

See home/views.py — its docstring is the contract.
"""
from datetime import datetime, time, timedelta
from unittest import mock

import pytest
from django.utils import timezone

from tests.test_home import (JSON, _coached, _data, _freeze, _home, _program,
                             _public_template, _subscribe, _types)
from tests.test_home import a_cold_cache, owners_rules  # noqa: F401 — autouse here too

pytestmark = pytest.mark.django_db

CHAT = "/api/v1/chat"


def _bust():
    from training_platform.cache import private_cache

    private_cache().clear()


# ============================================================================ B-182
def test_b182_an_uncoached_buyer_gets_the_day_hero_alone(make_user, api):
    """The two artboards never stack two heroes: a client who bought a programme is
    training, so the day hero is THE hero. The find-a-trainer call to action stays on
    the home through the checklist's `coach` step and the suggested trainers."""
    from tests.test_review_products_0924 import _bought

    _trainer, buyer, _product_id, _purchase = _bought(make_user, api, "b182buy")
    type(buyer).objects.filter(pk=buyer.pk).update(training_goal="build_muscle")
    buyer.refresh_from_db()
    _bust()

    types = _types(_home(api, buyer))
    assert "workout_hero" in types, types
    assert "find_trainer_hero" not in types, types
    checklist = _data(_home(api, buyer), "setup_checklist")
    assert checklist is not None, types
    coach = [step for step in checklist["steps"] if step["code"] == "coach"][0]
    assert coach["done"] is False


def test_b182_a_former_client_with_a_refused_day_is_led_by_the_find_trainer_hero(
        make_user, api):
    """The owner (2026-09-28): the coaching ended and the coach's routine stays readable
    and unstartable (`refused`); the find-a-trainer hero leads, the day stays readable
    behind it."""
    trainer, client, relation, routine, rows = _coached(make_user, api, "b182left",
                                                        funds="100.00")
    _freeze(relation, ago=timedelta(days=2))
    left = api(client).post("/api/auth/client/unassign-trainer/", {}, content_type=JSON)
    assert left.status_code == 200, left.content
    type(client).objects.filter(pk=client.pk).update(training_goal="build_muscle")
    _bust()

    body = _home(api, client)
    types = _types(body)
    assert types[:2] == ["find_trainer_hero", "workout_hero"], types
    assert _data(body, "workout_hero")["cta"] == {"state": "refused",
                                                  "refusal": "coaching_ended"}


def test_b182_a_coached_client_is_also_shown_the_programmes_on_sale(make_user, api):
    """The owner (2026-09-28): the programmes on sale are for a coached client too, by
    the same build rule — absent where prices may not show — and in the same place."""
    from home.views import SECTION_ORDER
    from tests.test_routine_products import _buy, _published

    trainer, client, relation, routine, rows = _coached(make_user, api, "b182rp",
                                                        funds="10000.00")
    _seller, _template, bought_id = _published(make_user, api, "b182rp")
    listed = _public_template(make_user("b182rp_t2", user_type="trainer"), "b182rp2")
    assert _buy(api, client, bought_id).status_code in (200, 201)
    _bust()

    body = _home(api, client, HTTP_X_APP_CHANNEL="apk")
    types = _types(body)
    assert "ready_programs" in types, types
    assert types == sorted(types, key=SECTION_ORDER.index), "not where the order puts it"
    cards = {card["id"]: card for card in _data(body, "ready_programs")["programs"]}
    assert (cards[bought_id]["owned"], cards[bought_id]["can_buy"]) == (True, False)
    other = [card for pk, card in cards.items() if pk != bought_id]
    assert other and other[0]["title"] == listed.name
    assert (other[0]["owned"], other[0]["can_buy"]) == (False, True)
    for first_day_only in ("find_trainer_hero", "setup_checklist", "featured_trainers"):
        assert first_day_only not in types, types

    for channel in ("play", "ios", ""):
        assert "ready_programs" not in _types(_home(api, client,
                                                    HTTP_X_APP_CHANNEL=channel)), channel


# ============================================================================ B-175
def test_b175_a_program_card_on_the_home_names_its_routine(make_user, api):
    """«حدّث برنامجك · <routine>»: the home card of a coach's program card carries the
    thread's own `program` for the same message, and only while the client may still
    read that routine — never the name of a routine taken off them."""
    from chat.services import open_conversation
    from tests.test_chat_cards_posters import _card, _history
    from tests.test_chat_cards_posters import _program as _card_program
    from users.models import TrainerClientRelation

    trainer = make_user("b175_t", user_type="trainer")
    client = make_user("b175_c")
    TrainerClientRelation.objects.create(trainer=trainer, client=client,
                                         status="approved", initiated_by="client")
    conversation = open_conversation(trainer, client)
    routine = _card_program(trainer, client)
    sent = _card(api, trainer, conversation, routine_id=routine.pk, day=2,
                 body="خففت وزن ضغط الأكتاف")
    assert sent.status_code == 201, sent.content
    _bust()

    message = _data(_home(api, client), "trainer_message")["message"]
    [row] = _history(api, client, conversation)
    assert (message["id"], message["kind"]) == (row["id"], "program")
    assert row["program"] is not None
    assert message["program"] == row["program"]
    assert (message["program"]["routine_id"], message["program"]["day"]) == (routine.pk, 2)

    routine.assigned_to.remove(client)
    _bust()
    message = _data(_home(api, client), "trainer_message")["message"]
    assert message["kind"] == "program"
    assert message["program"] is None, "the card named a routine the client lost"


def test_b175_a_program_card_taken_back_names_nothing(make_user, api):
    from chat.services import open_conversation
    from tests.test_chat_cards_posters import _card
    from tests.test_chat_cards_posters import _program as _card_program
    from users.models import TrainerClientRelation

    trainer = make_user("b175b_t", user_type="trainer")
    client = make_user("b175b_c")
    TrainerClientRelation.objects.create(trainer=trainer, client=client,
                                         status="approved", initiated_by="client")
    conversation = open_conversation(trainer, client)
    routine = _card_program(trainer, client)
    card = _card(api, trainer, conversation, routine_id=routine.pk).json()
    assert api(trainer).delete(f"{CHAT}/messages/{card['id']}/").status_code == 200
    _bust()

    message = _data(_home(api, client), "trainer_message")["message"]
    assert (message["kind"], message["deleted"], message["program"]) == \
        ("program", True, None)


# ============================================================================ B-176
def _full_client(make_user, api, tag, django_capture_on_commit_callbacks):
    """test_home.py's full client: a coach, a closed day, a plan with meals, a message,
    a challenge, a smart note and a week of history."""
    from home.sections import week_start
    from routine.models import Routine
    from tests.test_home import (_challenge, _close_day, _consent, _diet_day, _log_sets,
                                 _message)

    trainer, client, relation, routine, rows = _coached(make_user, api, tag)
    Routine.objects.filter(pk=routine.pk).update(
        start_date=week_start(timezone.localdate()))
    _subscribe(client, tag)
    _diet_day(client)
    _consent(client)
    _message(trainer, client)
    _challenge(client, tag)
    _log_sets(client, api, rows[0], 3)
    with django_capture_on_commit_callbacks(execute=True):
        assert _close_day(api, client, routine).status_code == 200
    return trainer, client, routine


def test_b176_the_home_answers_the_artboards_order(make_user, api,
                                                    django_capture_on_commit_callbacks):
    """`Home.html`: «يومك» (the rail), then «هذا الأسبوع» (the week tiles), then
    «سعرات اليوم» (the calories card). The rail is its own section now, so the order
    can be answered; `nutrition_today.timeline` stays for the builds already installed."""
    _t, client, routine = _full_client(make_user, api, "b176",
                                       django_capture_on_commit_callbacks)

    body = _home(api, client)
    types = _types(body)
    assert "day_timeline" in types, types
    assert types.index("day_timeline") < types.index("week_stats") < \
        types.index("nutrition_today"), types

    rail = _data(body, "day_timeline")
    nutrition = _data(body, "nutrition_today")
    assert set(rail) == {"date", "timeline"}
    assert rail["date"] == nutrition["date"] == timezone.localdate().isoformat()
    assert rail["timeline"] == nutrition["timeline"], "one rail, one builder"
    assert [entry["kind"] for entry in rail["timeline"]].count("workout") == 1


def test_b176_no_diet_section_no_rail_and_an_empty_day_is_not_a_section(make_user, api):
    from diet.models import DietPlan

    trainer, client, routine, rows = _program(make_user, "b176_bare")
    body = _home(api, client)                        # no plan at all
    assert "day_timeline" not in _types(body) and "nutrition_today" not in _types(body)

    # A plan covering today with no meal today, and no training today: nothing to say.
    person = make_user("b176_empty")
    _subscribe(person, "b176_empty")
    today = timezone.localdate()
    DietPlan.objects.create(user=person, goal="Maintain", daily_calories=2000.0,
                            start_date=today, end_date=today + timedelta(days=3),
                            duration_weeks=1)
    body = _home(api, person)
    assert _data(body, "nutrition_today")["timeline"] == []
    assert "day_timeline" not in _types(body), "an empty rail was sent as a section"


# ============================================================================ B-178
def test_b178_the_now_pill_goes_to_a_timed_entry(make_user, api):
    """A meal with no time is `now` all day and sorts last, so "the pill belongs to the
    LAST `now`" gave it to the untimed snack even while a timed workout was under way.
    The contract names the timed entry, which is what the app already draws."""
    from home.views import HomeView
    from tests.test_home_records import _meals, _reminder_at

    doc = " ".join((HomeView.__doc__ or "").split())
    assert ("the «الآن» pill belongs to the last `now` entry that has a time "
            "(`at` not null); only when no timed entry is `now`, to the last untimed "
            "`now`") in doc

    trainer, client, routine, rows = _program(make_user, "b178")
    _subscribe(client, "b178")
    _meals(client)
    _reminder_at(client, 0)

    timeline = _data(_home(api, client), "nutrition_today")["timeline"]
    nows = [entry for entry in timeline if entry["state"] == "now"]
    timed = [entry for entry in nows if entry["at"] is not None]
    assert timed and timed[-1]["kind"] == "workout", timeline
    assert (nows[-1]["kind"], nows[-1]["at"]) == ("meal", None), \
        "the case the rule exists for: the last `now` overall is the untimed snack"


# ============================================================================ B-179
def test_b179_a_meal_due_inside_the_window_shortens_the_refresh(make_user, api):
    """The states are read off the clock once, and the page was cached for 120 s
    whatever was due: a meal due in a minute stayed `upcoming` for two."""
    from diet.models import DietPlan, Meal

    moment = timezone.localtime()
    due = (moment + timedelta(seconds=60)).replace(microsecond=0)
    if moment.time() >= time(23, 57):
        pytest.skip("within 3 minutes of midnight the day itself turns")

    client = make_user("b179_c")
    _subscribe(client, "b179")
    today = timezone.localdate()
    plan = DietPlan.objects.create(user=client, goal="Maintain", daily_calories=2000.0,
                                   start_date=today, end_date=today + timedelta(days=3),
                                   duration_weeks=1)
    meal = Meal.objects.create(diet_plan=plan, date=today, meal_type="Lunch",
                               name="Lunch soon", scheduled_time=due.time())

    def state(body):
        return {m["id"]: m["state"] for m in _data(body, "nutrition_today")["meals"]}[
            meal.pk]

    first = _home(api, client)
    assert state(first) == "upcoming"
    assert 1 <= first["refresh_after_seconds"] <= 61, first["refresh_after_seconds"]

    later = timezone.now() + timedelta(seconds=62)
    with mock.patch("home.views.timezone.now", return_value=later):
        second = _home(api, client)
    assert second["as_of"] != first["as_of"], "the stale page was served past its life"
    assert state(second) == "now"


def test_b179_a_page_with_nothing_due_keeps_the_full_window(make_user, api):
    """Only a state that will change shortens the page's life: nothing due today (the
    last meal eaten, or none timed) keeps the 120 s, and a cached page still counts
    down from its own life."""
    from home import views as home_views

    trainer, client, routine, rows = _program(make_user, "b179_flat")
    first = _home(api, client)
    assert first["refresh_after_seconds"] == home_views.CACHE_SECONDS

    later = timezone.now() + timedelta(seconds=30)
    with mock.patch("home.views.timezone.now", return_value=later):
        second = _home(api, client)
    assert second["as_of"] == first["as_of"], "served from the cache"
    assert 1 <= second["refresh_after_seconds"] <= 90


# ============================================================================ S-193
def test_s193_a_dropped_participant_is_not_counted_in_participants(make_user, api):
    """`participants` is counted by the rule that ranks (`social/standing.py
    in_standing`), not `Challenge.participants_count`: someone who dropped out is
    neither ahead nor counted."""
    from social.models import ChallengeParticipation
    from tests.test_home import _challenge

    person = make_user("s193_c")
    challenge = _challenge(person, "s193", others=2)
    rival = (ChallengeParticipation.objects.filter(challenge=challenge)
             .exclude(user=person).order_by("-current_value", "id").first())
    ChallengeParticipation.objects.filter(pk=rival.pk).update(status="dropped")

    card = _data(_home(api, person), "challenge_rank")
    assert (card["rank"], card["participants"]) == (2, 2), card
