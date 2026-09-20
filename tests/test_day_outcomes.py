"""The missed work is kept, and a day closed short breaks the streak (the owner, 2026-09-20).

Closing a training day writes a `routine.DayOutcome`: what the day planned against what
was done. The streak counts only days with nothing missing and points at the day that
broke it; the home screen, the trainer's screens and the AI assistant read the same rows.
Reps must be entered, or taken from the coach with `as_prescribed`.
See routine/outcomes.py.
"""
from datetime import timedelta

import pytest
from django.utils import timezone

pytestmark = pytest.mark.django_db

JSON = "application/json"


def _program(make_user, tag, *, sets=3, reps=10, weight=None, exercises=2, days=1):
    """A client with a routine of `exercises` exercises on day 1, and their trainer."""
    from routine.models import Exercise, Routine, RoutineExercise

    trainer = make_user(f"do_t_{tag}", user_type="trainer")
    client = make_user(f"do_c_{tag}")
    routine = Routine.objects.create(name=f"do {tag}", created_by=trainer, days=days)
    rows = []
    for index in range(exercises):
        exercise = Exercise.objects.create(name=f"do ex {tag} {index}", created_by=trainer)
        rows.append(RoutineExercise.objects.create(routine=routine, exercise=exercise, day=1,
                                                   order=index + 1, sets=sets, reps=reps,
                                                   weight=weight))
    routine.assigned_to.add(client)
    return trainer, client, routine, rows


def _log_sets(client, api, routine, row, count, *, reps=10, weight=20, date=None,
              session=None, commit=None, start=1):
    """Log `count` sets. `commit` is `django_capture_on_commit_callbacks`: the
    recalculation that can complete a day runs on commit, which a test must execute."""
    import contextlib

    from routine.models import UserExerciseProgress

    date = date or timezone.localdate()
    progress, _created = UserExerciseProgress.objects.get_or_create(
        user=client, exercise=row.exercise, date=date,
        defaults={'target_sets': row.sets})
    for number in range(start, start + count):
        body = {"user_exercise_progress": progress.pk, "set_number": number,
                "weight": weight, "reps": reps, "date": date.isoformat()}
        if session is not None:
            body["workout_session"] = session
        with (commit(execute=True) if commit else contextlib.nullcontext()):
            resp = api(client).post("/api/routine/set-logs/", body, content_type=JSON)
        assert resp.status_code in (200, 201), resp.content
    return progress


def _close(api, client, routine, *, day=1, confirm=True, status="completed"):
    return api(client).post(f"/api/routine/routines/{routine.pk}/update_progress/",
                            {"day": day, "status": status, "confirm_unlogged": confirm},
                            content_type=JSON)


# ------------------------------------------------------------------ the snapshot
def test_a_day_closed_short_keeps_what_was_missed(make_user, api):
    from routine.models import DayOutcome

    trainer, client, routine, rows = _program(make_user, "short", sets=3, reps=10)
    _log_sets(client, api, routine, rows[0], 2)          # 2 of 3, second exercise untouched

    warned = _close(api, client, routine, confirm=False)
    assert (warned.status_code, warned.json()["code"]) == (409, "unlogged_sets")
    assert (warned.json()["prescribed"], warned.json()["logged"]) == (6, 2)

    closed = _close(api, client, routine)
    assert closed.status_code == 200, closed.content
    outcome = closed.json()["outcome"]
    assert (outcome["planned_sets"], outcome["logged_sets"], outcome["missed_sets"]) == (6, 2, 4)
    assert (outcome["complete"], outcome["confirmed_unlogged"]) == (False, True)
    assert (outcome["planned_reps"], outcome["logged_reps"], outcome["missed_reps"]) == (60, 20, 40)
    assert (outcome["exercises_full"], outcome["exercises_partial"],
            outcome["exercises_untouched"]) == (0, 1, 1)

    row = DayOutcome.objects.get(user=client, routine=routine, day=1)
    assert (row.via, row.complete, row.missed_sets) == ("confirmed_unlogged", False, 4)


def test_a_day_with_nothing_missing_is_complete(make_user, api):
    trainer, client, routine, rows = _program(make_user, "full", sets=2, reps=8, exercises=1)
    _log_sets(client, api, routine, rows[0], 2)
    closed = _close(api, client, routine, confirm=False)
    assert closed.status_code == 200, closed.content
    outcome = closed.json()["outcome"]
    assert (outcome["complete"], outcome["missed_sets"], outcome["via"]) == (True, 0, "completed")


def test_the_coachs_later_edit_does_not_rewrite_a_closed_day(make_user, api):
    """The prescription is a snapshot: editing the routine must not move last week."""
    from routine.models import DayOutcome, RoutineExercise

    trainer, client, routine, rows = _program(make_user, "snapshot", sets=2, reps=10, exercises=1)
    _log_sets(client, api, routine, rows[0], 2)
    _close(api, client, routine, confirm=False)

    RoutineExercise.objects.filter(pk=rows[0].pk).update(sets=8, reps=20)
    outcome = DayOutcome.objects.get(user=client, routine=routine, day=1)
    assert (outcome.planned_sets, outcome.planned_reps) == (2, 20)
    assert outcome.complete is True


def test_sets_synced_after_the_close_still_count(make_user, api,
                                                 django_capture_on_commit_callbacks):
    from routine.models import DayOutcome

    trainer, client, routine, rows = _program(make_user, "late", sets=3, reps=10, exercises=1)
    _log_sets(client, api, routine, rows[0], 1)
    _close(api, client, routine)
    assert DayOutcome.objects.get(user=client, routine=routine).missed_sets == 2

    _log_sets(client, api, routine, rows[0], 3, commit=django_capture_on_commit_callbacks)
    outcome = DayOutcome.objects.get(user=client, routine=routine)
    assert (outcome.logged_sets, outcome.missed_sets, outcome.complete) == (3, 0, True)
    assert outcome.via == "confirmed_unlogged", "the reason the day was closed is kept"


def test_logging_a_whole_day_records_it_with_reps_unknown(make_user, api):
    from routine.models import DayOutcome

    trainer, client, routine, rows = _program(make_user, "bulk", sets=3, reps=10, exercises=1)
    resp = api(client).post("/api/routine/user-exercise-progress/bulk-complete/", {
        "routine_id": routine.pk, "day": 1, "date": timezone.localdate().isoformat(),
        "completed_sets": 3}, content_type=JSON)
    assert resp.status_code == 200, resp.content
    outcome = DayOutcome.objects.get(user=client, routine=routine)
    assert (outcome.via, outcome.complete, outcome.reps_known) == ("bulk_complete", True, False)


def test_a_day_that_completes_itself_is_recorded_too(make_user, api,
                                                    django_capture_on_commit_callbacks):
    """No button is pressed: every exercise reaches its target and the recalculation
    completes the day."""
    from routine.models import DayOutcome

    trainer, client, routine, rows = _program(make_user, "auto", sets=2, reps=6, exercises=1)
    _log_sets(client, api, routine, rows[0], 2, commit=django_capture_on_commit_callbacks)
    outcome = DayOutcome.objects.filter(user=client, routine=routine).first()
    assert outcome is not None and outcome.via == "auto" and outcome.complete


def test_a_skipped_day_is_a_break_not_a_training_day(make_user, api):
    from routine.models import DayOutcome

    trainer, client, routine, rows = _program(make_user, "skip", exercises=1)
    resp = _close(api, client, routine, status="skipped")
    assert resp.status_code == 200, resp.content
    outcome = DayOutcome.objects.get(user=client, routine=routine)
    assert (outcome.via, outcome.complete) == ("skipped", False)


def test_a_session_finished_on_a_day_records_it(make_user, api):
    from routine.models import DayOutcome

    trainer, client, routine, rows = _program(make_user, "session", sets=1, reps=5, exercises=1)
    opened = api(client).post("/api/routine/workout-sessions/",
                              {"routine": routine.pk, "day": 1}, content_type=JSON)
    assert opened.status_code == 201, opened.content
    finished = api(client).patch(f"/api/routine/workout-sessions/{opened.json()['id']}/",
                                 {"status": "completed"}, content_type=JSON)
    assert finished.status_code == 200, finished.content
    outcome = DayOutcome.objects.get(user=client, routine=routine)
    assert outcome.via in ("session", "auto") and outcome.planned_sets == 1


# ------------------------------------------------------------------ the streak
def test_a_day_closed_short_breaks_the_streak_and_is_pointed_at(make_user, api):
    """The owner (2026-09-20): a day with sets missing does not count, and the app is
    told which day broke the streak."""
    from routine.models import DayOutcome

    trainer, client, routine, rows = _program(make_user, "streak", sets=2, reps=10, exercises=1)
    from routine.outcomes import record_day

    today = timezone.localdate()
    # Two perfect days before yesterday, then yesterday closed one set short.
    for days_ago, logged in ((3, 2), (2, 2), (1, 1)):
        date = today - timedelta(days=days_ago)
        _log_sets(client, api, routine, rows[0], logged, date=date)
        record_day(client, routine, 1, date,
                   via='completed' if logged == 2 else 'confirmed_unlogged',
                   confirmed_unlogged=logged < 2)
    assert DayOutcome.objects.filter(user=client).count() == 3

    shown = api(client).get("/api/routine/analytics/streaks/").json()
    assert shown["current_streak"] == 0, "a short day breaks it"
    assert shown["max_streak"] == 2
    assert shown["last_break"]["date"] == (today - timedelta(days=1)).isoformat()
    assert shown["last_break"]["missed_sets"] == 1
    assert shown["last_break"]["reason"] == "sets_missed"


def test_days_closed_before_outcomes_existed_still_count(make_user, api):
    from routine.models import RoutineProgress

    trainer, client, routine, rows = _program(make_user, "legacy", exercises=1)
    today = timezone.localdate()
    for days_ago in (1, 0):
        RoutineProgress.objects.update_or_create(
            user=client, routine=routine, day=1, date=today - timedelta(days=days_ago),
            defaults={"status": "completed"})
    shown = api(client).get("/api/routine/analytics/streaks/").json()
    assert (shown["current_streak"], shown["approximate"]) == (2, True)


def test_the_achievement_streak_also_stops_at_a_short_day(make_user, api):
    from achievements.engine import AchievementEngine
    from routine.outcomes import record_day

    trainer, client, routine, rows = _program(make_user, "badge", sets=2, reps=10, exercises=1)
    today = timezone.localdate()
    _log_sets(client, api, routine, rows[0], 1, date=today)
    record_day(client, routine, 1, today, via='confirmed_unlogged', confirmed_unlogged=True)
    assert AchievementEngine._calculate_workout_streak(client) == 0


# ------------------------------------------------------------------ the screens
def test_the_home_screen_and_the_adherence_route_show_the_plan(make_user, api):
    trainer, client, routine, rows = _program(make_user, "home", sets=3, reps=10,
                                              weight=20, exercises=1)
    _log_sets(client, api, routine, rows[0], 2, reps=10, weight=20)
    _close(api, client, routine)

    # The home payload is cached per user for 120 s and the test cache is shared
    # across the run: start from a clean one, as a real first request would.
    from training_platform.cache import private_cache
    private_cache().delete(f"recent_progress:{client.pk}")

    home = api(client).get("/api/routine/v1/analytics/recent-progress/").json()
    assert home["adherence"]["sets"] == {"planned": 3, "logged": 2, "missed": 1,
                                         "done_percent": 66.7}
    assert home["adherence"]["perfect_days"] == 0

    week = api(client).get("/api/routine/analytics/adherence/?period=week").json()
    assert week["sets"]["missed"] == 1 and week["days_closed"] == 1
    assert week["reps"] == {"planned": 30, "logged": 20, "missed": 10}
    assert week["volume_kg"]["planned"] == 600.0 and week["volume_kg"]["logged"] == 400.0
    assert len(week["by_day"]) == 1 and week["by_day"][0]["missed_sets"] == 1
    assert api(client).get("/api/routine/analytics/adherence/?period=decade").status_code == 400


def test_a_trainer_reads_the_same_numbers_for_their_client(make_user, api):
    from users.models import TrainerClientRelation

    trainer, client, routine, rows = _program(make_user, "coach", sets=4, reps=10, exercises=1)
    TrainerClientRelation.objects.create(trainer=trainer, client=client, status="approved")
    _log_sets(client, api, routine, rows[0], 1)
    _close(api, client, routine)

    one = api(trainer).get(f"/api/routine/trainer/client-progress/{client.pk}/").json()
    assert one["weekly_stats"]["missed_sets"] == 3
    assert one["weekly_stats"]["perfect_days"] == 0

    roster = api(trainer).get("/api/routine/trainer/client-progress/recent/").json()
    mine = [row for row in roster["recent_activity"]
            if row["client_info"]["id"] == client.pk]
    assert mine and mine[0]["this_week"]["missed_sets"] == 3
    assert mine[0]["this_week"]["set_completion_percent"] == 25.0

    seen = api(trainer).get(f"/api/routine/analytics/adherence/?user_id={client.pk}")
    assert seen.status_code == 200 and seen.json()["sets"]["missed"] == 3
    stranger = make_user("do_stranger", user_type="trainer")
    assert api(stranger).get(
        f"/api/routine/analytics/adherence/?user_id={client.pk}").status_code == 403


def test_the_day_row_carries_its_outcome(make_user, api):
    trainer, client, routine, rows = _program(make_user, "rows", sets=2, reps=10, exercises=1)
    _log_sets(client, api, routine, rows[0], 1)
    _close(api, client, routine)
    listed = api(client).get("/api/routine/routine-progress/").json()
    row = [r for r in listed["results"] if r["day"] == 1][0]
    assert row["outcome"]["missed_sets"] == 1 and row["outcome"]["complete"] is False


# ------------------------------------------------------------------ reps
def test_a_set_without_reps_is_refused(make_user, api):
    from routine.models import UserExerciseProgress

    trainer, client, routine, rows = _program(make_user, "reps", sets=3, reps=12, exercises=1)
    progress = UserExerciseProgress.objects.create(user=client, exercise=rows[0].exercise,
                                                   date=timezone.localdate(), target_sets=3)
    resp = api(client).post("/api/routine/set-logs/",
                            {"user_exercise_progress": progress.pk, "set_number": 1,
                             "weight": 30}, content_type=JSON)
    assert resp.status_code == 400
    assert resp.json()["field_errors"]["reps"][0]["code"] == "reps_required"


def test_as_prescribed_fills_in_the_coachs_reps(make_user, api):
    from routine.models import ExerciseSetLog, UserExerciseProgress

    trainer, client, routine, rows = _program(make_user, "asked", sets=3, reps=12,
                                              weight=40, exercises=1)
    progress = UserExerciseProgress.objects.create(user=client, exercise=rows[0].exercise,
                                                   date=timezone.localdate(), target_sets=3)
    resp = api(client).post("/api/routine/set-logs/",
                            {"user_exercise_progress": progress.pk, "set_number": 1,
                             "as_prescribed": True, "routine": routine.pk, "day": 1},
                            content_type=JSON)
    assert resp.status_code == 201, resp.content
    log = ExerciseSetLog.objects.get(pk=resp.json()["id"])
    assert (log.reps, log.weight) == (12, 40.0)

    # Through the session, with no routine or day in the body.
    opened = api(client).post("/api/routine/workout-sessions/",
                              {"routine": routine.pk, "day": 1}, content_type=JSON)
    second = api(client).post("/api/routine/set-logs/",
                              {"user_exercise_progress": progress.pk, "set_number": 2,
                               "workout_session": opened.json()["id"], "as_prescribed": True},
                              content_type=JSON)
    assert second.status_code == 201, second.content
    assert ExerciseSetLog.objects.get(pk=second.json()["id"]).reps == 12


def test_as_prescribed_says_so_when_the_coach_set_no_reps(make_user, api):
    from routine.models import RoutineExercise, UserExerciseProgress

    trainer, client, routine, rows = _program(make_user, "noreps", sets=2, reps=10, exercises=1)
    RoutineExercise.objects.filter(pk=rows[0].pk).update(reps=None)
    progress = UserExerciseProgress.objects.create(user=client, exercise=rows[0].exercise,
                                                   date=timezone.localdate(), target_sets=2)
    resp = api(client).post("/api/routine/set-logs/",
                            {"user_exercise_progress": progress.pk, "set_number": 1,
                             "as_prescribed": True, "routine": routine.pk, "day": 1},
                            content_type=JSON)
    assert resp.status_code == 400
    assert resp.json()["field_errors"]["reps"][0]["code"] == "no_prescribed_reps"


def test_bulk_logging_needs_reps_or_the_coachs_number(make_user, api):
    from routine.models import ExerciseSetLog

    trainer, client, routine, rows = _program(make_user, "bulkreps", sets=2, reps=15,
                                              weight=25, exercises=1)
    today = timezone.localdate().isoformat()
    refused = api(client).post("/api/routine/set-logs/bulk-create/", {
        "routine_id": routine.pk, "day": 1, "date": today, "sets": 2, "weight": 10},
        content_type=JSON)
    assert (refused.status_code, refused.json()["code"]) == (400, "reps_required")

    asked = api(client).post("/api/routine/set-logs/bulk-create/", {
        "routine_id": routine.pk, "day": 1, "date": today, "sets": 2,
        "as_prescribed": True}, content_type=JSON)
    assert asked.status_code == 200, asked.content
    logs = ExerciseSetLog.objects.filter(user_exercise_progress__user=client)
    assert logs.count() == 2 and {log.reps for log in logs} == {15}
    assert {log.weight for log in logs} == {25.0}


# ------------------------------------------------------------------ the sweep
def test_a_day_left_open_is_closed_by_the_nightly_sweep(make_user, api):
    from routine.models import DayOutcome, RoutineProgress
    from routine.tasks import close_open_training_days

    trainer, client, routine, rows = _program(make_user, "sweep", sets=3, reps=10, exercises=1)
    yesterday = timezone.localdate() - timedelta(days=1)
    _log_sets(client, api, routine, rows[0], 1, date=yesterday)
    RoutineProgress.objects.update_or_create(user=client, routine=routine, day=1,
                                             date=yesterday,
                                             defaults={"status": "in_progress"})
    DayOutcome.objects.filter(user=client).delete()

    assert close_open_training_days()["closed"] == 1
    outcome = DayOutcome.objects.get(user=client, routine=routine, date=yesterday)
    assert (outcome.via, outcome.missed_sets, outcome.complete) == ("unclosed", 2, False)
    # Idempotent: a second night changes nothing.
    assert close_open_training_days()["closed"] == 0


def test_the_sweep_leaves_a_frozen_coaching_alone(make_user, api):
    """A day nobody was allowed to train is not missed work (the owner's freeze)."""
    from unittest import mock

    from routine.models import DayOutcome, RoutineProgress
    from routine.tasks import close_open_training_days

    trainer, client, routine, rows = _program(make_user, "frozen_sweep", exercises=1)
    yesterday = timezone.localdate() - timedelta(days=1)
    RoutineProgress.objects.create(user=client, routine=routine, day=1, date=yesterday,
                                   status="in_progress")
    with mock.patch("users.coaching.training_refusal", return_value="coaching_frozen"):
        assert close_open_training_days() == {"closed": 0, "skipped": 1}
    assert not DayOutcome.objects.filter(user=client).exists()


def test_the_home_week_answers_at_all(make_user, api):
    """`GET v1/analytics/recent-progress/` annotated `day` over `WorkoutSession.day`, so
    Django refused the query and the client's home week answered 500 to everyone from the
    day that column was added."""
    from routine.models import WorkoutSession

    trainer, client, routine, rows = _program(make_user, "home500", exercises=1)
    now = timezone.now()
    WorkoutSession.objects.create(user=client, routine=routine, day=1, status="completed",
                                  start_time=now - timedelta(hours=1), end_time=now)
    from training_platform.cache import private_cache
    private_cache().delete(f"recent_progress:{client.pk}")

    resp = api(client).get("/api/routine/v1/analytics/recent-progress/")
    assert resp.status_code == 200, resp.content
    week = resp.json()["recent_progress"]
    assert len(week) == 7 and sum(day["duration"] for day in week) == 3600


# ------------------------------------- what the review of 2026-09-20 found
def test_one_exercise_twice_in_a_day_is_counted_once(make_user, api):
    """A warm-up row and a top set of the same exercise: the day's logs are one pool,
    filled into the rows in order. Counting them per row marked a finished day short."""
    from routine.models import DayOutcome, RoutineExercise

    trainer, client, routine, rows = _program(make_user, "twice", sets=4, reps=10, exercises=1)
    RoutineExercise.objects.create(routine=routine, exercise=rows[0].exercise, day=1,
                                   order=2, sets=1, reps=3)
    _log_sets(client, api, routine, rows[0], 5)          # 4 + 1 = the whole day

    closed = _close(api, client, routine, confirm=False)
    assert closed.status_code == 200, closed.content
    outcome = DayOutcome.objects.get(user=client, routine=routine)
    assert (outcome.planned_sets, outcome.logged_sets, outcome.complete) == (5, 5, True)
    assert outcome.exercises_full == 2


def test_a_late_set_never_rewrites_the_plan_of_a_closed_day(make_user, api,
                                                            django_capture_on_commit_callbacks):
    """The snapshot must survive a coach's edit even when a later set refreshes the day."""
    from routine.models import DayOutcome, RoutineExercise

    trainer, client, routine, rows = _program(make_user, "frozen", sets=3, reps=10, exercises=1)
    _log_sets(client, api, routine, rows[0], 3)
    _close(api, client, routine, confirm=False)

    RoutineExercise.objects.filter(pk=rows[0].pk).update(sets=8, reps=20)
    _log_sets(client, api, routine, rows[0], 1, commit=django_capture_on_commit_callbacks)

    outcome = DayOutcome.objects.get(user=client, routine=routine)
    assert (outcome.planned_sets, outcome.planned_reps) == (3, 30)
    assert (outcome.logged_sets, outcome.missed_sets, outcome.complete) == (3, 0, True)


def test_sets_that_arrive_late_lower_a_short_days_miss(make_user, api,
                                                       django_capture_on_commit_callbacks):
    from routine.models import DayOutcome

    trainer, client, routine, rows = _program(make_user, "catchup", sets=4, reps=10, exercises=1)
    _log_sets(client, api, routine, rows[0], 1)
    _close(api, client, routine)
    assert DayOutcome.objects.get(user=client, routine=routine).missed_sets == 3

    _log_sets(client, api, routine, rows[0], 2, start=2,
               commit=django_capture_on_commit_callbacks)
    outcome = DayOutcome.objects.get(user=client, routine=routine)
    assert (outcome.logged_sets, outcome.missed_sets, outcome.complete) == (3, 1, False)


def test_an_exercise_without_prescribed_reps_records_no_phantom_work(make_user, api):
    """Bulk logging wrote the claimed set count before refusing the exercise, so a day
    counted work that has no set behind it."""
    from routine.models import ExerciseSetLog, RoutineExercise, UserExerciseProgress

    trainer, client, routine, rows = _program(make_user, "phantom", sets=2, reps=10, exercises=2)
    RoutineExercise.objects.filter(pk=rows[1].pk).update(reps=None)
    resp = api(client).post("/api/routine/set-logs/bulk-create/", {
        "routine_id": routine.pk, "day": 1, "date": timezone.localdate().isoformat(),
        "sets": 2, "as_prescribed": True}, content_type=JSON)
    assert resp.status_code == 200, resp.content
    assert [e["code"] for e in resp.json()["errors"]] == ["no_prescribed_reps"]
    assert not UserExerciseProgress.objects.filter(user=client,
                                                   exercise=rows[1].exercise).exists()
    assert ExerciseSetLog.objects.filter(user_exercise_progress__user=client).count() == 2

    closed = _close(api, client, routine, confirm=False)
    assert (closed.status_code, closed.json()["code"]) == (409, "unlogged_sets")
    assert closed.json()["unlogged"] == 2, "the second exercise is still owed"


def test_as_prescribed_cannot_read_a_routine_that_is_not_yours(make_user, api):
    """The prescription is the coach's: `routine` in the body is honoured only for a
    routine the caller may train, or any trainer's numbers could be read by id."""
    from routine.models import UserExerciseProgress

    trainer, client, routine, rows = _program(make_user, "mine", sets=2, reps=9, exercises=1)
    other_trainer, other_client, other_routine, other_rows = _program(
        make_user, "theirs", sets=3, reps=77, exercises=1)

    progress = UserExerciseProgress.objects.create(user=client, exercise=other_rows[0].exercise,
                                                   date=timezone.localdate(), target_sets=3)
    resp = api(client).post("/api/routine/set-logs/", {
        "user_exercise_progress": progress.pk, "set_number": 1, "as_prescribed": True,
        "routine": other_routine.pk, "day": 1}, content_type=JSON)
    assert resp.status_code == 400
    assert resp.json()["field_errors"]["reps"][0]["code"] == "no_prescribed_reps"


def test_a_skipped_day_did_not_confirm_a_warning(make_user, api):
    from routine.models import DayOutcome

    trainer, client, routine, rows = _program(make_user, "skipflag", sets=3, reps=10, exercises=1)
    _close(api, client, routine, status="skipped")
    outcome = DayOutcome.objects.get(user=client, routine=routine)
    assert outcome.confirmed_unlogged is False, "nobody was warned about a skipped day"


def test_a_skipped_exercise_is_not_counted_as_work(make_user, api):
    from routine.models import DayOutcome, UserExerciseProgress

    trainer, client, routine, rows = _program(make_user, "skipex", sets=3, reps=10, exercises=1)
    UserExerciseProgress.objects.create(user=client, exercise=rows[0].exercise,
                                        date=timezone.localdate(), target_sets=3,
                                        completed_sets=3, skipped=True)
    _close(api, client, routine)
    outcome = DayOutcome.objects.get(user=client, routine=routine)
    assert (outcome.logged_sets, outcome.missed_sets, outcome.exercises_skipped) == (0, 3, 1)


def test_the_sweep_leaves_old_history_alone(make_user, api):
    from routine.models import DayOutcome, RoutineProgress
    from routine.tasks import SWEEP_WINDOW_DAYS, close_open_training_days

    trainer, client, routine, rows = _program(make_user, "oldsweep", exercises=1)
    ancient = timezone.localdate() - timedelta(days=SWEEP_WINDOW_DAYS + 5)
    RoutineProgress.objects.create(user=client, routine=routine, day=1, date=ancient,
                                   status="in_progress")
    assert close_open_training_days()["closed"] == 0
    assert not DayOutcome.objects.filter(user=client, date=ancient).exists()


def test_the_two_screens_answer_the_same_streak(make_user, api):
    from routine.models import RoutineProgress

    trainer, client, routine, rows = _program(make_user, "agree", exercises=1)
    today = timezone.localdate()
    for days_ago in (1, 0):
        RoutineProgress.objects.update_or_create(
            user=client, routine=routine, day=1, date=today - timedelta(days=days_ago),
            defaults={"status": "completed"})
    streaks = api(client).get("/api/routine/analytics/streaks/").json()
    week = api(client).get("/api/routine/analytics/adherence/?period=week").json()
    assert streaks["current_streak"] == week["perfect_streak"]["current"] == 2
    assert streaks["approximate"] and week["approximate"]
