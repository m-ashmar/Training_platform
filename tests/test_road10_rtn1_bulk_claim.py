"""The fix run's RTN-1, S-175 (2026-11-24): a set logged after "log a whole day" does not
erase the day's claim. Bulk-complete writes `completed_sets` with no set log behind it;
the recalculation (`_recalc_locked`) rewrote that column from the set logs alone, so one
set logged — or deleted — that day replaced a claim of 3 with 1, `day_work` read the 1,
and the day's outcome turned short: out of adherence, the streak broken.

The claim now has its own column (`UserExerciseProgress.claimed_sets`, written by
bulk-complete alone) and `completed_sets` is the larger of the logged sets and the claim.
Migration 0038 fills the column for every row that still claims more than its logs.

The first two tests fail on the code before; the others hold what stays.
"""
import pytest
from django.utils import timezone

from tests.test_day_outcomes import _log_sets, _program

pytestmark = pytest.mark.django_db

JSON = "application/json"


def _log_whole_day(api, client, routine, sets):
    resp = api(client).post("/api/routine/user-exercise-progress/bulk-complete/", {
        "routine_id": routine.pk, "day": 1, "date": timezone.localdate().isoformat(),
        "completed_sets": sets}, content_type=JSON)
    assert resp.status_code == 200, resp.content


def _state(client, routine, row):
    from routine.models import DayOutcome, UserExerciseProgress

    progress = UserExerciseProgress.objects.get(user=client, exercise=row.exercise,
                                                date=timezone.localdate())
    outcome = DayOutcome.objects.get(user=client, routine=routine, day=1)
    return progress.completed_sets, outcome.complete


def test_a_set_logged_after_the_whole_day_keeps_the_claim(
        make_user, api, django_capture_on_commit_callbacks):
    _trainer, client, routine, (row,) = _program(make_user, "s175a", sets=3, exercises=1)
    _log_whole_day(api, client, routine, 3)
    assert _state(client, routine, row) == (3, True)

    _log_sets(client, api, routine, row, 1, commit=django_capture_on_commit_callbacks)
    assert _state(client, routine, row) == (3, True)


def test_a_set_deleted_after_the_whole_day_keeps_the_claim(
        make_user, api, django_capture_on_commit_callbacks):
    from routine.models import ExerciseSetLog

    _trainer, client, routine, (row,) = _program(make_user, "s175b", sets=3, exercises=1)
    _log_whole_day(api, client, routine, 3)
    progress = _log_sets(client, api, routine, row, 1,
                         commit=django_capture_on_commit_callbacks)
    log = ExerciseSetLog.objects.get(user_exercise_progress=progress)
    with django_capture_on_commit_callbacks(execute=True):
        gone = api(client).delete(f"/api/routine/set-logs/{log.pk}/")
    assert gone.status_code == 204, gone.content
    assert _state(client, routine, row) == (3, True)


def test_more_sets_logged_than_claimed_count_in_full(
        make_user, api, django_capture_on_commit_callbacks):
    _trainer, client, routine, (row,) = _program(make_user, "s175c", sets=3, exercises=1)
    _log_whole_day(api, client, routine, 2)
    _log_sets(client, api, routine, row, 3, commit=django_capture_on_commit_callbacks)
    completed, complete = _state(client, routine, row)
    assert completed == 3 and complete


def test_without_a_claim_the_logs_alone_still_decide(
        make_user, api, django_capture_on_commit_callbacks):
    """No bulk claim: deleting a logged set lowers the count, as it always did."""
    from routine.models import ExerciseSetLog, UserExerciseProgress

    _trainer, client, _routine, (row,) = _program(make_user, "s175d", sets=3, exercises=1)
    progress = _log_sets(client, api, _routine, row, 2,
                         commit=django_capture_on_commit_callbacks)
    log = ExerciseSetLog.objects.filter(user_exercise_progress=progress).first()
    with django_capture_on_commit_callbacks(execute=True):
        assert api(client).delete(f"/api/routine/set-logs/{log.pk}/").status_code == 204
    progress = UserExerciseProgress.objects.get(pk=progress.pk)
    assert (progress.completed_sets, progress.claimed_sets) == (1, 0)


def test_the_migration_keeps_every_standing_claim(make_user):
    """0038's fill: a row claiming more sets than its logs hold keeps the claim; a row
    whose count is its logs gets none."""
    import importlib

    from django.apps import apps

    from routine.models import ExerciseSetLog, UserExerciseProgress

    _trainer, client, _routine, (claimed, logged) = _program(make_user, "s175e", sets=3,
                                                              exercises=2)
    today = timezone.localdate()
    a = UserExerciseProgress.objects.create(user=client, exercise=claimed.exercise,
                                            date=today, target_sets=3)
    b = UserExerciseProgress.objects.create(user=client, exercise=logged.exercise,
                                            date=today, target_sets=3)
    for number in (1, 2):
        ExerciseSetLog.objects.create(user_exercise_progress=b, set_number=number,
                                      date=today, reps=10, weight=20)
    # As the column stood before 0038: a bare claim on one, the log count on the other.
    UserExerciseProgress.objects.filter(pk=a.pk).update(completed_sets=3, claimed_sets=0)
    UserExerciseProgress.objects.filter(pk=b.pk).update(completed_sets=2, claimed_sets=0)

    migration = importlib.import_module("routine.migrations.0038_days_bounds_claimed_sets")
    migration.keep_standing_claims(apps, None)
    assert UserExerciseProgress.objects.get(pk=a.pk).claimed_sets == 3
    assert UserExerciseProgress.objects.get(pk=b.pk).claimed_sets == 0
