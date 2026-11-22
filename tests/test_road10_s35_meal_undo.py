"""Road to 10 S3.5 (the owner's D11): a meal confirmed today can be undone the same day.

`POST /api/diet/v1/client/meals/{id}/undo/` reopens the meal: its ticks, the day's
totals, the funnel event, the analytics row (achievement `meal_count`) and the AI coach's
behaviour rows all follow, and home's cached page is dropped. A meal confirmed on an
earlier day answers 409 `undo_window_closed`; undoing twice is harmless. On the code
before this change the route does not exist.
"""
import uuid
from datetime import timedelta

import pytest
from django.utils import timezone

pytestmark = pytest.mark.django_db

JSON = "application/json"


def _url(meal, verb):
    return f"/api/diet/v1/client/meals/{meal.pk}/{verb}/"


@pytest.fixture
def fresh_cache():
    from training_platform.cache import private_cache

    private_cache().clear()
    yield
    private_cache().clear()


def _day(make_user, tag, *, plan_days_back=0):
    """A subscribed client with a plan covering today and one lunch of two foods."""
    from diet.models import DietPlan, FoodCategory, FoodItem, Meal, MealComponent
    from tests.test_home import _subscribe

    client = make_user(f"s35_{tag}")
    _subscribe(client, f"s35_{tag}")
    today = timezone.localdate()
    cat = FoodCategory.objects.create(name=f"s35-{uuid.uuid4().hex[:6]}")
    foods = [FoodItem.objects.create(
        name=n, name_en=n, category=cat, api_id=f"s35-{uuid.uuid4().hex[:8]}",
        calories=100, protein=10, carbs=10, fat=1, serving_size="100g",
        allergens=[], allergen_source="verified") for n in (f"Rice {tag}", f"Beans {tag}")]
    plan = DietPlan.objects.create(user=client, goal="Maintain", daily_calories=2000,
                                   start_date=today - timedelta(days=plan_days_back),
                                   end_date=today + timedelta(days=3))
    meal = Meal.objects.create(diet_plan=plan, date=today, meal_type="Lunch",
                               scheduled_time="12:00")
    for food in foods:
        MealComponent.objects.create(meal=meal, food=food, quantity=150.0)
    return client, plan, meal


def _confirm(api, client, meal, capture):
    with capture(execute=True):
        resp = api(client).post(_url(meal, "complete"), {"action": "complete_meal"},
                                content_type=JSON)
    assert resp.status_code == 200, resp.content
    return resp


def _undo(api, client, meal, capture):
    with capture(execute=True):
        return api(client).post(_url(meal, "undo"), {}, content_type=JSON)


def test_a_meal_confirmed_today_is_reopened_and_everything_derived_follows(
        make_user, api, django_capture_on_commit_callbacks):
    from ai_assistant.models import UserBehaviorEvent
    from analytics.models import UserActivity
    from diet.models import DailyProgress, MealComponent, MealEvent

    client, plan, meal = _day(make_user, "all")
    _confirm(api, client, meal, django_capture_on_commit_callbacks)
    row = DailyProgress.objects.get(user=client, diet_plan=plan, date=meal.date)
    assert row.meals_completed == 1 and row.calories_consumed == 300.0
    assert MealEvent.objects.filter(meal=meal, kind=MealEvent.COMPLETED).exists()
    assert UserActivity.objects.filter(user=client, activity_type="meal_completed").exists()
    assert UserBehaviorEvent.objects.filter(user=client, event_type="meal_completed").count() == 2

    resp = _undo(api, client, meal, django_capture_on_commit_callbacks)
    assert resp.status_code == 200, resp.content
    body = resp.json()
    assert body["reopened"] is True
    assert body["components_reopened"] == 2
    assert body["meal_is_completed"] is False
    assert body["progress"]["meals_completed"] == 0
    assert body["progress"]["calories_consumed"] == 0
    assert body["progress"]["is_day_completed"] is False

    meal.refresh_from_db()
    assert meal.is_completed is False
    assert not MealComponent.objects.filter(meal=meal, is_completed=True).exists()
    assert not MealComponent.objects.filter(meal=meal, completed_at__isnull=False).exists()
    row.refresh_from_db()
    assert row.meals_completed == 0 and row.calories_consumed == 0
    assert not MealEvent.objects.filter(meal=meal, kind=MealEvent.COMPLETED).exists()
    assert not UserActivity.objects.filter(user=client, activity_type="meal_completed").exists()
    assert not UserBehaviorEvent.objects.filter(user=client, event_type="meal_completed").exists()

    # The client's own progress read follows.
    progress = api(client).get("/api/diet/v1/client/progress/").json()
    assert progress["meals_completed"] == 0
    assert progress["calories_consumed"] == 0


def test_the_amount_said_is_forgotten_and_a_partly_ticked_meal_is_reopened(
        make_user, api, django_capture_on_commit_callbacks):
    from diet.models import MealComponent

    client, _plan, meal = _day(make_user, "part")
    first = MealComponent.objects.filter(meal=meal).order_by("id").first()
    with django_capture_on_commit_callbacks(execute=True):
        resp = api(client).post(_url(meal, "complete"),
                                {"action": "complete_component", "component_id": first.pk,
                                 "actual_quantity": 80}, content_type=JSON)
    assert resp.status_code == 200, resp.content

    resp = _undo(api, client, meal, django_capture_on_commit_callbacks)
    assert resp.status_code == 200, resp.content
    assert resp.json()["components_reopened"] == 1
    first.refresh_from_db()
    assert first.is_completed is False
    assert first.actual_quantity_consumed is None


def test_undoing_twice_is_harmless(make_user, api, django_capture_on_commit_callbacks):
    from diet.models import DailyProgress

    client, plan, meal = _day(make_user, "twice")
    _confirm(api, client, meal, django_capture_on_commit_callbacks)
    first = _undo(api, client, meal, django_capture_on_commit_callbacks)
    assert first.status_code == 200, first.content
    rows_before = DailyProgress.objects.filter(user=client).count()

    second = _undo(api, client, meal, django_capture_on_commit_callbacks)
    assert second.status_code == 200, second.content
    assert second.json()["reopened"] is False
    assert second.json()["components_reopened"] == 0
    assert second.json()["progress"]["meals_completed"] == 0
    assert DailyProgress.objects.filter(user=client).count() == rows_before


def test_confirm_again_after_an_undo_counts_the_meal_once(
        make_user, api, django_capture_on_commit_callbacks):
    """The achievement count reads the analytics row: after confirm, undo, confirm it
    is one meal, not two and not none."""
    from achievements.engine import AchievementEngine

    client, _plan, meal = _day(make_user, "again")
    _confirm(api, client, meal, django_capture_on_commit_callbacks)
    assert AchievementEngine._get_metric_value(client, "meal_count", {}) == 1
    assert _undo(api, client, meal, django_capture_on_commit_callbacks).status_code == 200
    assert AchievementEngine._get_metric_value(client, "meal_count", {}) == 0
    _confirm(api, client, meal, django_capture_on_commit_callbacks)
    assert AchievementEngine._get_metric_value(client, "meal_count", {}) == 1


def test_a_meal_confirmed_on_an_earlier_day_is_refused_with_its_own_code(
        make_user, api, django_capture_on_commit_callbacks):
    from diet.models import Meal, MealComponent

    client, plan, _meal = _day(make_user, "late", plan_days_back=2)
    yesterday = timezone.localdate() - timedelta(days=1)
    meal = Meal.objects.create(diet_plan=plan, date=yesterday, meal_type="Dinner",
                               scheduled_time="20:00")
    food = MealComponent.objects.filter(meal__diet_plan=plan).first().food
    MealComponent.objects.create(meal=meal, food=food, quantity=100.0)
    _confirm(api, client, meal, django_capture_on_commit_callbacks)
    when = timezone.now() - timedelta(days=1)
    MealComponent.objects.filter(meal=meal).update(completed_at=when)

    resp = _undo(api, client, meal, django_capture_on_commit_callbacks)
    assert resp.status_code == 409, resp.content
    assert resp.json()["code"] == "undo_window_closed"
    assert resp.json()["confirmed_on"] == timezone.localtime(when).date().isoformat()
    meal.refresh_from_db()
    assert meal.is_completed is True
    assert MealComponent.objects.filter(meal=meal, is_completed=True).count() == 1


def test_a_tick_with_no_time_is_refused_as_not_today(make_user, api,
                                                    django_capture_on_commit_callbacks):
    from diet.models import MealComponent

    client, _plan, meal = _day(make_user, "notime")
    _confirm(api, client, meal, django_capture_on_commit_callbacks)
    MealComponent.objects.filter(meal=meal).update(completed_at=None)

    resp = _undo(api, client, meal, django_capture_on_commit_callbacks)
    assert resp.status_code == 409, resp.content
    assert resp.json()["code"] == "undo_window_closed"
    assert resp.json()["confirmed_on"] is None


def test_the_undo_drops_homes_cached_page(make_user, api, fresh_cache,
                                          django_capture_on_commit_callbacks):
    from home.views import _stamp

    client, _plan, meal = _day(make_user, "home")
    _confirm(api, client, meal, django_capture_on_commit_callbacks)
    before = _stamp(client.pk)
    assert _undo(api, client, meal, django_capture_on_commit_callbacks).status_code == 200
    assert _stamp(client.pk) != before


def test_who_may_undo(make_user, api, django_capture_on_commit_callbacks):
    """A trainer is told `clients_only`; another client's meal is `not_found`; a meal on
    a replaced plan is `plan_superseded` — and the owner's own undo still works."""
    from diet.models import DietPlan

    from tests.test_home import _subscribe

    client, plan, meal = _day(make_user, "who")
    _confirm(api, client, meal, django_capture_on_commit_callbacks)
    # With diet access, so the answer is the role's, not the entitlement's.
    trainer = make_user("s35_who_t", user_type="trainer")
    _subscribe(trainer, "s35_who_t")
    stranger, _p, _m = _day(make_user, "who_other")

    resp = api(trainer).post(_url(meal, "undo"), {}, content_type=JSON)
    assert resp.status_code == 403 and resp.json()["code"] == "clients_only", resp.content
    resp = api(stranger).post(_url(meal, "undo"), {}, content_type=JSON)
    assert resp.status_code == 404 and resp.json()["code"] == "not_found", resp.content

    assert _undo(api, client, meal, django_capture_on_commit_callbacks).status_code == 200

    DietPlan.objects.filter(pk=plan.pk).update(is_active=False)
    resp = api(client).post(_url(meal, "undo"), {}, content_type=JSON)
    assert resp.status_code == 409 and resp.json()["code"] == "plan_superseded", resp.content
