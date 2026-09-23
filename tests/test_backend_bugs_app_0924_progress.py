"""BACKEND_BUGS B-066..B-073: what the app session found wiring the progress screens —
goals, analytics and workout sessions (2026-09-24). One test per row."""
import json
from datetime import datetime, time, timedelta

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from tests.test_home import _log_sets, _program

pytestmark = pytest.mark.django_db

JSON = "application/json"


def _post(api, user, url, body):
    return api(user).post(url, data=json.dumps(body), content_type=JSON)


def _this_week_begins():
    from home.sections import week_start

    return timezone.make_aware(datetime.combine(week_start(timezone.localdate()), time.min))


def _session(user, routine, start, *, minutes=45, status="completed", day=1):
    from routine.models import WorkoutSession

    return WorkoutSession.objects.create(
        user=user, routine=routine, day=day, start_time=start, status=status,
        end_time=start + timedelta(minutes=minutes) if status == "completed" else None)


# ------------------------------------------------------------------------ B-066
def test_b066_a_goal_is_measured_from_its_start_in_its_own_direction(make_user, api):
    """`new_value / target_value`: a weight goal 82 -> 75 read 100% at 82 and was marked
    completed by its first reading; a lift 72.5 -> 100 read 82.5% at 82.5 kg."""
    from analytics.models import UserGoal

    user = make_user("b066")
    url = "/api/analytics/goals/{}/update_progress/"

    # With no start, a loss is measured from the first reading — never from 0 kg.
    cut = UserGoal.objects.create(user=user, goal_type="weight_loss", title="cut",
                                  target_value=75)
    first = _post(api, user, url.format(cut.pk), {"new_value": 82}).json()
    assert first["status"] == "active", "82 kg is not a 75 kg target reached"
    assert first["completed_at"] is None
    assert first["progress_percentage"] == 0
    halfway = _post(api, user, url.format(cut.pk), {"new_value": 78.5}).json()
    assert halfway["progress_percentage"] == pytest.approx(50.0)

    # With a start: the share of the way covered, downward.
    down = UserGoal.objects.create(user=user, goal_type="custom", title="down",
                                   target_value=75, start_value=82, current_value=82)
    body = _post(api, user, url.format(down.pk), {"new_value": 80}).json()
    assert body["progress_percentage"] == pytest.approx(200 / 7, abs=0.01)
    assert body["status"] == "active"
    worse = _post(api, user, url.format(down.pk), {"new_value": 85}).json()
    assert worse["progress_percentage"] == 0, "a reading the wrong way is 0, not negative"
    done = _post(api, user, url.format(down.pk), {"new_value": 74.8}).json()
    assert (done["progress_percentage"], done["status"]) == (100.0, "completed")
    assert done["completed_at"] is not None

    # Upward: 72.5 -> 100 at 82.5 is 36% of the way, not 82.5%.
    lift = UserGoal.objects.create(user=user, goal_type="strength", title="bench",
                                   target_value=100, start_value=72.5, current_value=72.5)
    body = _post(api, user, url.format(lift.pk), {"new_value": 82.5}).json()
    assert body["progress_percentage"] == pytest.approx(36.36, abs=0.01)
    assert body["status"] == "active"


# ------------------------------------------------------------------------ B-067
def test_b067_the_workouts_list_is_newest_first_by_a_total_order(make_user, api):
    """The list rested on `Meta.ordering`; the view now says its order itself, and two
    sessions begun in the same second keep their places across pages."""
    from rest_framework.request import Request
    from rest_framework.test import APIRequestFactory

    from routine.views import WorkoutSessionViewSet

    trainer, client, routine, rows = _program(make_user, "b067")
    same = timezone.now() - timedelta(hours=3)
    tied = [_session(client, routine, same) for _ in range(3)]
    older = _session(client, routine, same - timedelta(days=1))
    newest = _session(client, routine, same + timedelta(hours=1))

    seen = []
    page = "/api/routine/workout-sessions/?page_size=2"
    while page:
        body = api(client).get(page).json()
        seen += [row["id"] for row in body["results"]]
        page = body["next"]
    expected = [newest.pk] + sorted((s.pk for s in tied), reverse=True) + [older.pk]
    assert seen == expected, "newest first, ties broken by id, nothing repeated or lost"

    request = Request(APIRequestFactory().get("/api/routine/workout-sessions/"))
    request.user = client
    view = WorkoutSessionViewSet(request=request, action="list", kwargs={}, format_kwarg=None)
    assert view.get_queryset().query.order_by == ("-start_time", "-id"), \
        "the order the (user, -start_time, -id) index serves, said by the view"


# ------------------------------------------------------------------------ B-068
def test_b068_a_first_window_has_no_volume_change(make_user, api):
    """With nothing in the previous window the change was the constant 100.0 — a first
    month read exactly like a doubling."""
    trainer, client, routine, rows = _program(make_user, "b068", sets=3)
    today = timezone.localdate()
    _log_sets(client, api, rows[0], 2, reps=10, weight=40, date=today)

    first = api(client).get("/api/routine/analytics/summary/?period=week").json()
    assert first["week_volume"] == 800
    assert first["volume_change_percent"] is None, "nothing to compare with"

    _log_sets(client, api, rows[0], 1, reps=10, weight=40, date=today - timedelta(days=10))
    second = api(client).get("/api/routine/analytics/summary/?period=week").json()
    assert second["volume_change_percent"] == 100.0, "400 kg -> 800 kg is a real doubling"


# ------------------------------------------------------------------------ B-069
def test_b069_a_goal_is_created_at_its_start_in_one_request(make_user, api):
    """`current_value` was read-only and the start ignored, so every goal was stored at
    0 / 0% and the start needed a second, non-atomic `update_progress`."""
    from analytics.models import UserGoal

    user = make_user("b069")
    made = _post(api, user, "/api/analytics/goals/", {
        "goal_type": "weight_loss", "title": "cut", "target_value": 75, "start_value": 82})
    assert made.status_code == 201, made.content
    body = made.json()
    assert (body["start_value"], body["current_value"], body["progress_percentage"]) == \
        (82.0, 82.0, 0.0)
    row = UserGoal.objects.get(pk=body["id"])
    assert (row.start_value, row.current_value, row.status) == (82.0, 82.0, "active")

    moved = _post(api, user, f"/api/analytics/goals/{row.pk}/update_progress/",
                  {"new_value": 80}).json()
    assert moved["progress_percentage"] == pytest.approx(28.57, abs=0.01)

    # Where the app kept the start until now.
    kept = _post(api, user, "/api/analytics/goals/", {
        "goal_type": "strength", "title": "bench", "target_value": 100,
        "metadata": {"start_value": 72.5}}).json()
    assert (kept["start_value"], kept["current_value"]) == (72.5, 72.5)

    refused = _post(api, user, "/api/analytics/goals/", {
        "goal_type": "custom", "title": "same", "target_value": 10, "start_value": 10})
    assert refused.status_code == 400
    assert refused.json()["field_errors"]["start_value"][0]["code"] == "start_reaches_target"

    # Fixed at creation: a PATCH does not move it.
    api(user).patch(f"/api/analytics/goals/{row.pk}/", data=json.dumps({"start_value": 90}),
                    content_type=JSON)
    row.refresh_from_db()
    assert row.start_value == 82.0

    # The rows written before the column existed (migration analytics/0005).
    from importlib import import_module

    from django.apps import apps as registry

    backfill = import_module("analytics.migrations.0005_usergoal_start_value").backfill
    wrongly_done = UserGoal.objects.create(
        user=user, goal_type="weight_loss", title="old cut", target_value=75,
        current_value=80, progress_percentage=100, status="completed",
        completed_at=timezone.now(), metadata={"start_value": 82})
    never_moved = UserGoal.objects.create(
        user=user, goal_type="strength", title="old bench", target_value=100,
        metadata={"start_value": 72.5})
    weekly = UserGoal.objects.create(
        user=user, goal_type="workout_frequency", title="old weekly", target_value=4,
        current_value=4, progress_percentage=100, status="completed",
        completed_at=timezone.now())
    untouched = UserGoal.objects.create(user=user, goal_type="custom", title="plain",
                                        target_value=10, current_value=3,
                                        progress_percentage=30)
    backfill(registry, None)
    for goal in (wrongly_done, never_moved, weekly, untouched):
        goal.refresh_from_db()
    assert (wrongly_done.start_value, wrongly_done.status, wrongly_done.completed_at) == \
        (82.0, "active", None)
    assert wrongly_done.progress_percentage == pytest.approx(28.57, abs=0.01)
    assert (never_moved.start_value, never_moved.current_value,
            never_moved.progress_percentage) == (72.5, 72.5, 0.0)
    assert (weekly.status, weekly.completed_at) == ("active", None)
    assert (untouched.start_value, untouched.progress_percentage) == (None, 30)


# ------------------------------------------------------------------------ B-070
def test_b070_a_weekly_goal_counts_this_weeks_workouts(make_user, api):
    """Nothing moved a `workout_frequency` goal when workouts were logged and nothing
    reset it: '4 a week' moved only by hand and, once at 4, stayed completed."""
    from analytics.models import UserGoal
    from home.sections import week_start

    trainer, client, routine, rows = _program(make_user, "b070")
    other = make_user("b070_other")
    begins = _this_week_begins()
    _session(client, routine, begins + timedelta(minutes=1))
    _session(client, routine, begins + timedelta(minutes=2))
    _session(client, routine, begins - timedelta(hours=1))               # last week
    _session(client, routine, begins + timedelta(minutes=3), status="abandoned")
    _session(other, routine, begins + timedelta(minutes=4))               # not theirs

    goal = UserGoal.objects.create(user=client, goal_type="workout_frequency",
                                   title="4 a week", target_value=4)
    body = api(client).get(f"/api/analytics/goals/{goal.pk}/").json()
    assert (body["current_value"], body["progress_percentage"]) == (2.0, 50.0)
    first = week_start(timezone.localdate())
    assert body["week_start"] == first.isoformat()
    assert body["week_end"] == (first + timedelta(days=6)).isoformat()
    assert body["status"] == "active"

    refused = _post(api, client, f"/api/analytics/goals/{goal.pk}/update_progress/",
                    {"new_value": 4})
    assert refused.status_code == 400
    assert refused.json()["code"] == "goal_counted_from_workouts"

    # A list costs the same however many counted goals it holds.
    def listed():
        with CaptureQueriesContext(connection) as ctx:
            resp = api(client).get("/api/analytics/goals/")
        assert resp.status_code == 200
        return len(ctx.captured_queries), resp.json()["results"]

    few, results = listed()
    assert {row["current_value"] for row in results} == {2.0}
    for index in range(3):
        UserGoal.objects.create(user=client, goal_type="workout_frequency",
                                title=f"g{index}", target_value=3)
    many, _results = listed()
    assert many == few


# ------------------------------------------------------------------------ B-071
def test_b071_one_activity_per_workout_and_per_meal_and_a_type_filter(
        make_user, api, django_capture_on_commit_callbacks):
    """One `exercise_completed` row per logged SET and one untitled `meal_completed` row
    per FOOD; and no way to ask the log for one kind."""
    from achievements.engine import AchievementEngine
    from analytics.models import UserActivity
    from diet.models import DietPlan, FoodItem, Meal, MealComponent
    from diet.trainer_services import ClientProgressService

    trainer, client, routine, rows = _program(make_user, "b071", sets=3)
    with django_capture_on_commit_callbacks(execute=True):
        _log_sets(client, api, rows[0], 3, reps=10, weight=40)
        session = _session(client, routine, timezone.now() - timedelta(minutes=40))
    assert not UserActivity.objects.filter(user=client,
                                           activity_type="exercise_completed").exists()
    assert UserActivity.objects.filter(user=client,
                                       activity_type="routine_completed").count() == 1

    today = timezone.localdate()
    plan = DietPlan.objects.create(user=client, goal="Maintain", daily_calories=2200,
                                   target_protein=150, target_carbs=220, target_fat=70,
                                   start_date=today, end_date=today, duration_weeks=1)
    meal = Meal.objects.create(diet_plan=plan, date=today, meal_type="Lunch",
                               name="Chicken freekeh", name_ar="فريكة بالدجاج",
                               scheduled_time=time(13, 0))
    rice = FoodItem.objects.create(name="B071 Freekeh", name_en="B071 Freekeh",
                                   calories=200, protein=8, carbs=40, fat=2,
                                   serving_size="100g", serving_size_grams=100)
    chicken = FoodItem.objects.create(name="B071 Chicken", name_en="B071 Chicken",
                                      calories=100, protein=20, carbs=0, fat=2,
                                      serving_size="100g", serving_size_grams=100)
    MealComponent.objects.create(meal=meal, food=rice, quantity=150)
    MealComponent.objects.create(meal=meal, food=chicken, quantity=50)
    with django_capture_on_commit_callbacks(execute=True):
        ClientProgressService(client).complete_entire_meal(meal)

    meals = UserActivity.objects.filter(user=client, activity_type="meal_completed")
    assert meals.count() == 1, "one row for the meal, not one per food"
    recorded = meals.get().metadata
    assert recorded["meal_id"] == meal.pk
    assert (recorded["name"], recorded["name_ar"]) == ("Chicken freekeh", "فريكة بالدجاج")
    assert recorded["calories"] == 350.0, "150 g at 200 kcal + 50 g at 100 kcal"
    assert AchievementEngine._get_metric_value(client, "meal_count", {}) == 1

    # Saving an eaten food again (a rating) logs nothing more — nor for a meal finished
    # before this change, which already has its old one-row-per-food entries.
    earlier = Meal.objects.create(diet_plan=plan, date=today, meal_type="Dinner",
                                  name="Old dinner", scheduled_time=time(20, 0))
    old_food = MealComponent.objects.create(meal=earlier, food=chicken, quantity=100,
                                            is_completed=True, completed_at=timezone.now())
    UserActivity.objects.create(user=client, activity_type="meal_completed",
                                session_id=f"meal-component-{old_food.pk}",
                                user_agent="server", metadata={"component_id": old_food.pk})
    with django_capture_on_commit_callbacks(execute=True):
        for component in MealComponent.objects.filter(meal__in=[meal, earlier]):
            component.is_liked = True
            component.save()
    assert meals.count() == 2, "the meal's row and the old dinner's per-food row, no more"

    workouts = api(client).get(
        "/api/analytics/activities/recent_activities/?activity_type=routine_completed").json()
    assert [row["activity_type"] for row in workouts["results"]] == ["routine_completed"]
    both = api(client).get("/api/analytics/activities/recent_activities/"
                           "?activity_type=routine_completed,meal_completed").json()
    assert both["count"] == 3, "the workout, the meal, and the old dinner's per-food row"
    bad = api(client).get("/api/analytics/activities/recent_activities/?activity_type=nope")
    assert bad.status_code == 400
    assert bad.json()["code"] == "bad_activity_type"
    assert session.pk


# ------------------------------------------------------------------------ B-072
def test_b072_a_finished_workout_says_its_day_duration_and_volume(
        make_user, api, django_capture_on_commit_callbacks):
    """The `routine_completed` activity carried only `routine_id` and `session_id`, so
    the log could not say «47 دقيقة · 5,480 كغ»."""
    from analytics.models import UserActivity
    from routine.models import ExerciseSetLog, UserExerciseProgress

    trainer, client, routine, rows = _program(make_user, "b072", sets=3)
    today = timezone.localdate()
    progress, _created = UserExerciseProgress.objects.get_or_create(
        user=client, exercise=rows[0].exercise, date=today, defaults={"target_sets": 3})
    start = timezone.now() - timedelta(minutes=50)
    session = _session(client, routine, start, status="active", day=1)
    for number in (1, 2, 3):
        ExerciseSetLog.objects.create(user_exercise_progress=progress,
                                      workout_session=session, set_number=number,
                                      weight=40, reps=10, date=today)
    with django_capture_on_commit_callbacks(execute=True):
        session.status = "completed"
        session.end_time = start + timedelta(minutes=47)
        session.save()

    feed = api(client).get("/api/analytics/activities/recent_activities/"
                           "?activity_type=routine_completed").json()["results"]
    assert len(feed) == 1
    assert feed[0]["workout"] == {"session_id": session.pk, "routine_id": routine.pk,
                                  "day": 1, "duration_minutes": 47, "volume_kg": 1200.0,
                                  "sets": 3}

    # One query for the page, however many workouts it holds.
    def cost():
        with CaptureQueriesContext(connection) as ctx:
            assert api(client).get(
                "/api/analytics/activities/recent_activities/").status_code == 200
        return len(ctx.captured_queries)

    one = cost()
    for minutes in (200, 300, 400):
        other = _session(client, routine, timezone.now() - timedelta(minutes=minutes))
        UserActivity.objects.create(user=client, activity_type="routine_completed",
                                    session_id=str(other.pk), user_agent="server")
    assert cost() == one


# ------------------------------------------------------------------------ B-073
def test_b073_an_exercise_id_that_is_sent_is_read(make_user, api):
    """`if exercise_id:` read `?exercise_id=0` as absent: 200 with the whole overview."""
    trainer, client, routine, rows = _program(make_user, "b073")
    url = "/api/routine/analytics/strength/?exercise_id="

    for sent in ("0", "-3"):
        resp = api(client).get(url + sent)
        assert resp.status_code == 404, (sent, resp.status_code)
        assert resp.json()["code"] == "not_found"

    junk = api(client).get(url + "abc")
    assert junk.status_code == 400
    assert junk.json()["code"] == "bad_exercise_id"

    overview = api(client).get(url)
    assert overview.status_code == 200 and "exercises" in overview.json()
