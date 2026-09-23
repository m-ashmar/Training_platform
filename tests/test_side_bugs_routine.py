"""The `routine` app's `SIDE_BUGS.md` rows, one test per defect.

Each test fails against the behaviour that was there, for the reason named:

    S-022  `WorkoutSession.status` had an `abandoned` value nothing ever wrote, so a
           workout the client walked away from read `active` for ever and the home
           screen bounded it at twelve hours on its own
    S-032  `set-logs/my-progress/` answered a bare array of every set a person had
           ever logged, and `workout-sessions/` answered a bad `?page=` with 404
           where the rest of the app answers 400
    S-030  `RoutineExerciseViewSet` had no `filterset_fields`, so `?routine=&day=`
           did nothing and one day of a routine could not be asked for
    S-007  `ExerciseSetLogViewSet` was registered under two prefixes
    S-029  two different classes were both called `RoutineTemplateExerciseSerializer`
    S-036  `exercises/create-with-image/` returned `str(exception)` to the client
    S-039  `add-media/`'s 207 `errors[]` were English in an Arabic response
    S-037  `DetailedClientProgressSerializer` was imported and used by nothing
"""
import re
from datetime import timedelta
from pathlib import Path

import pytest
from django.utils import timezone

pytestmark = pytest.mark.django_db

JSON = "application/json"
REPO_ROOT = Path(__file__).resolve().parent.parent


# ------------------------------------------------------------------------ helpers
def _program(make_user, tag, *, days=1, sets=3, reps=10):
    """A trainer, an approved client, and a routine of `days` days the client has."""
    from routine.models import Exercise, Routine, RoutineExercise
    from users.models import TrainerClientRelation

    trainer = make_user(f"rt_t_{tag}", user_type="trainer")
    client = make_user(f"rt_c_{tag}")
    TrainerClientRelation.objects.create(trainer=trainer, client=client,
                                         status="approved", initiated_by="client")
    routine = Routine.objects.create(name=f"rt {tag}", created_by=trainer, days=days)
    exercises = []
    for day in range(1, days + 1):
        exercise = Exercise.objects.create(name=f"rt ex {tag} d{day}", created_by=trainer)
        RoutineExercise.objects.create(routine=routine, exercise=exercise, day=day,
                                       order=1, sets=sets, reps=reps)
        exercises.append(exercise)
    routine.assigned_to.add(client)
    return trainer, client, routine, exercises


def _logged_set(client, exercise, session, *, weight=40.0, reps=8):
    """One real set inside `session`, with the progress row that owns its day."""
    from routine.models import ExerciseSetLog, UserExerciseProgress

    day = timezone.localdate(session.start_time)
    progress = UserExerciseProgress.objects.create(
        user=client, exercise=exercise, date=day, target_sets=3)
    log = ExerciseSetLog.objects.create(
        user_exercise_progress=progress, workout_session=session,
        set_number=1, weight=weight, reps=reps, date=day)
    return progress, log


# ------------------------------------------ S-022: a forgotten workout is not running
def test_s022_a_forgotten_session_is_abandoned_and_its_sets_survive(make_user, api):
    """Nothing wrote `abandoned`, so a client who started a workout and closed the app
    left a row every "is a workout running" reader answered yes to, for ever.

    Three things have to hold at once: the sweep closes the forgotten one, it does NOT
    touch a workout still in progress, and the work logged inside the abandoned session
    is exactly where it was — the sets, and every figure computed from them.
    """
    from django.conf import settings

    from routine.models import ExerciseSetLog, UserExerciseProgress, WorkoutSession
    from routine.tasks import abandon_forgotten_sessions
    from training_platform.celery import app

    trainer, client, routine, exercises = _program(make_user, "s022")

    forgotten = WorkoutSession.objects.create(user=client, routine=routine, day=1)
    WorkoutSession.objects.filter(pk=forgotten.pk).update(
        start_time=timezone.now() - timedelta(hours=13))
    forgotten.refresh_from_db()
    running = WorkoutSession.objects.create(user=client, routine=routine, day=1)
    finished = WorkoutSession.objects.create(user=client, routine=routine, day=1,
                                             status="completed")
    WorkoutSession.objects.filter(pk=finished.pk).update(
        start_time=timezone.now() - timedelta(hours=20))

    progress, log = _logged_set(client, exercises[0], forgotten)
    progress.refresh_from_db()
    completed_before = progress.completed_sets

    # The endpoint answers the rule BEFORE the sweep has run: a session forgotten an
    # hour ago must not hold the start button until tonight.
    listed = api(client).get("/api/routine/workout-sessions/?status=active").json()
    assert [row["id"] for row in listed["results"]] == [running.pk], listed

    result = abandon_forgotten_sessions()

    assert result == {"abandoned": 1, "capped": False}, result
    forgotten.refresh_from_db()
    running.refresh_from_db()
    finished.refresh_from_db()
    assert forgotten.status == "abandoned"
    assert forgotten.end_time is None, "nobody knows when they stopped; do not invent it"
    assert running.status == "active", "a workout in progress is not forgotten"
    assert finished.status == "completed", "a finished workout is never overwritten"

    # The work is untouched: the set, its progress row and the total computed from it.
    assert ExerciseSetLog.objects.filter(pk=log.pk).exists()
    assert (UserExerciseProgress.objects.get(pk=progress.pk).completed_sets
            == completed_before)
    assert ExerciseSetLog.objects.get(pk=log.pk).workout_session_id == forgotten.pk

    # Idempotent, and the beat entry names a task that is really registered.
    assert abandon_forgotten_sessions()["abandoned"] == 0
    entry = settings.CELERY_BEAT_SCHEDULE["abandon-forgotten-sessions"]
    app.loader.import_default_modules()
    assert entry["task"] in app.tasks, entry["task"]


def test_s022_one_rule_decides_how_long_a_workout_may_run(make_user, api):
    """Home had its own twelve hours, the serializer its own, and the sweep would have
    been a third. They are one constant now, and home reads the shared predicate."""
    import home.sections as sections
    from routine.serializers import WorkoutSessionSerializer
    from routine.sessions import SESSION_MAX_SECONDS, running

    # A deliberate change (SIDE_BUGS S-098), not an assertion bent to fit: this pinned
    # `sections.LONGEST_SESSION`, an alias home kept of the shared constant after its
    # own query moved to `routine.sessions.running`, and which nothing then read. The
    # alias is gone; what this test protects — home holds no second copy of the rule —
    # is asserted directly instead, beside the source check below.
    assert not hasattr(sections, "LONGEST_SESSION"), "home kept a second name for the rule"
    assert sections.running_sessions is running, "home asks the shared predicate"
    assert WorkoutSessionSerializer.MAX_SESSION_SECONDS is SESSION_MAX_SECONDS
    # Not merely equal numbers written out twice: the source says so.
    source = (REPO_ROOT / "home" / "sections.py").read_text()
    assert "12 * 3600" not in source, "home is inventing the bound again"


# ------------------------------------------------------- S-032: unbounded list bodies
def test_s032_my_progress_is_paginated_in_both_shapes(make_user, api):
    """It returned every set log the person had ever written, unpaginated, in one
    body — and built it by serialising all of them into memory first."""
    trainer, client, routine, exercises = _program(make_user, "s032")
    from routine.models import WorkoutSession

    session = WorkoutSession.objects.create(user=client, routine=routine, day=1)
    from routine.models import ExerciseSetLog, UserExerciseProgress

    today = timezone.localdate()
    progress = UserExerciseProgress.objects.create(
        user=client, exercise=exercises[0], date=today, target_sets=5)
    for number in range(1, 4):
        ExerciseSetLog.objects.create(user_exercise_progress=progress,
                                      workout_session=session, set_number=number,
                                      weight=30 + number, reps=8, date=today)

    body = api(client).get("/api/routine/set-logs/my-progress/?page_size=2").json()
    assert isinstance(body, dict), f"a bare array is an unbounded response: {type(body)}"
    assert set(body) >= {"count", "next", "previous", "results"}, body.keys()
    assert body["count"] == 3 and len(body["results"]) == 2, body

    grouped = api(client).get(
        "/api/routine/set-logs/my-progress/?group_by=exercise").json()
    assert isinstance(grouped, dict), "the grouped shape pages the same way"
    assert grouped["count"] == 1, grouped
    assert grouped["results"][0]["sets_completed"] == 3, grouped


def test_s032_a_bad_page_on_the_session_list_is_a_400_naming_it(make_user, api):
    """`workout-sessions/` paginated only by the project default, whose bad-page answer
    is a 404 — indistinguishable, on a list, from "the thing you asked about is gone".
    Every other list in this app answers 400 naming the parameter."""
    trainer, client, routine, exercises = _program(make_user, "s032p")

    refused = api(client).get("/api/routine/workout-sessions/?page=abc")

    assert refused.status_code == 400, refused.content
    assert "page" in str(refused.json()).lower(), refused.json()


# ----------------------------------------------- S-030: one day of a routine, asked for
def test_s030_a_client_can_ask_for_one_day_of_a_routine(make_user, api):
    """`?routine=&day=` was accepted and ignored, so the only way to read day 2 was to
    pull the whole routine. The template equivalent could already be filtered."""
    from routine.models import RoutineExercise

    trainer, client, routine, exercises = _program(make_user, "s030", days=3)

    body = api(client).get(
        f"/api/routine/routine-exercises/?routine={routine.pk}&day=2").json()

    assert body["count"] == 1, body
    assert {row["day"] for row in body["results"]} == {2}, body

    # And the filter narrows what the caller may see; it never widens it. Another
    # trainer's routine is not readable by naming its id.
    other_trainer, other_client, other_routine, _rows = _program(make_user, "s030b")
    hidden = api(client).get(
        f"/api/routine/routine-exercises/?routine={other_routine.pk}").json()
    assert hidden["count"] == 0, hidden
    assert RoutineExercise.objects.filter(routine=other_routine).exists(), \
        "the rows exist — the caller simply may not read them"


# ---------------------------------------------- S-007: one viewset, one URL prefix
def test_s007_the_set_log_viewset_answers_on_one_prefix_only(make_user, api):
    """It was registered at `set-logs/` and `exercisesetlogs/`, so every action it
    gained doubled the route surface and had to be documented twice."""
    trainer, client, routine, exercises = _program(make_user, "s007")

    assert api(client).get("/api/routine/set-logs/").status_code == 200
    assert api(client).get("/api/routine/exercisesetlogs/").status_code == 404
    assert api(client).post("/api/routine/exercisesetlogs/bulk-create/", {},
                            content_type=JSON).status_code == 404

    contract = (REPO_ROOT / "API_CONTRACT.md").read_text()
    assert "`/api/routine/exercisesetlogs/`" not in contract
    assert "exercisesetlogs" in contract, \
        "a removal the app has to act on is written down, not silently dropped"


# ------------------------------------------------ S-029: two classes with one name
def test_s029_no_serializer_name_means_two_different_classes():
    """`RoutineTemplateExerciseSerializer` was defined in BOTH `routine/views.py` and
    `routine/serializers.py` with different fields, and views imported the second one
    and then rebound the name. The router used one and `RoutineTemplateSerializer` the
    other, so an edit could land in the wrong file without anything failing."""
    import inspect

    from routine import serializers as serializers_module
    from routine import views as views_module

    def defined_in(module):
        return {name: obj for name, obj in vars(module).items()
                if inspect.isclass(obj) and obj.__module__ == module.__name__}

    clash = sorted(set(defined_in(views_module)) & set(defined_in(serializers_module)))

    assert not clash, f"one name, two classes: {clash}"
    # And the two that clashed are still the two shapes they were, under both names.
    assert "exercise_details" in views_module.RoutineTemplateExerciseListSerializer().fields
    assert "exercise_id" in serializers_module.RoutineTemplateExerciseSerializer().fields


# ------------------------------------------ S-036: internal exception text to the client
def test_s036_a_failed_exercise_create_never_hands_back_the_exception(make_user, api,
                                                                     monkeypatch):
    """`f'Failed to create exercise: {str(e)}'` is a stack trace's worth of internals —
    file paths, bucket names, SQL — handed to whoever posted the form. The same defect
    the two media views closed as S-006."""
    from routine.models import Exercise

    trainer = make_user("rt_t_s036", user_type="trainer")
    secret = "/srv/secrets/db-primary.internal password=hunter2"

    def boom(*args, **kwargs):
        raise RuntimeError(secret)

    monkeypatch.setattr(Exercise.objects, "create", boom)

    broke = api(trainer).post("/api/routine/exercises/create-with-image/", {
        "name": "bench", "description": "chest", "target_muscle": "chest"})

    assert broke.status_code == 500, broke.content
    body = broke.json()
    assert body.get("code") == "exercise_create_failed", body
    assert secret not in str(broke.content.decode()), body
    assert "RuntimeError" not in str(broke.content.decode()), body


# ------------------------------------------------- S-039: untranslated English in a 207
def test_s039_the_207_from_add_media_is_arabic_for_an_arabic_client(make_user, api):
    """The reason was translated and the `Item 2: ` in front of it was not, so an
    Arabic user read half a sentence in English inside an Arabic screen."""
    from routine.models import Exercise

    trainer = make_user("rt_t_s039", user_type="trainer")
    exercise = Exercise.objects.create(name="rt ex s039", created_by=trainer)

    answer = api(trainer).post(
        f"/api/routine/exercises/{exercise.pk}/add-media/",
        {"media_items": [{"media_type": "text", "content": "keep your back straight"},
                         {"media_type": "hologram", "content": "x"}]},
        content_type=JSON, HTTP_ACCEPT_LANGUAGE="ar")

    assert answer.status_code == 207, answer.content
    body = answer.json()
    assert len(body["errors"]) == 1, body
    line = body["errors"][0]
    assert "Item" not in line, line
    assert re.search(r"[؀-ۿ]", line), line
    # The code is what the app branches on, and it does not move.
    assert body["errors_detail"][0]["code"] == "media_type_invalid", body


# ---------------------------------------------------------------- S-037: dead code
def test_s037_the_dead_client_progress_serializer_is_gone(make_user, api):
    """`DetailedClientProgressSerializer` was imported by `routine/views.py` and used
    by nothing, so the next reader of `trainer/client-progress/{id}/` would have found
    `get_client_info` and believed it was the live path. It is not, and the endpoint
    still answers what it always answered."""
    from routine import serializers as serializers_module
    from routine import views as views_module

    assert not hasattr(serializers_module, "DetailedClientProgressSerializer")
    assert not hasattr(views_module, "DetailedClientProgressSerializer")

    trainer, client, routine, exercises = _program(make_user, "s037")
    body = api(trainer).get(
        f"/api/routine/trainer/client-progress/{client.pk}/").json()
    assert {"client", "client_info", "weekly_stats", "recent_activity"} <= set(body), \
        body.keys()
