"""SIDE_BUGS S-289, what was left (Road to 10 round 4): a set that names its workout
session and sends NO date is dated on that session's start day (Asia/Damascus), not the
server's today. A workout begun at 23:40 whose sets arrive after midnight is then one
workout on its start day in every challenge. A date the client sends is always kept
(the app sends one), and an undated set with no session is still today's.

The session here began at 23:40 two days ago and ended at 00:30: the server's today is
not its start day, which is exactly the case after midnight, with no clock to move.
"""
import uuid
from datetime import datetime, time, timedelta

import pytest
from django.utils import timezone

pytestmark = pytest.mark.django_db

JSON = "application/json"
SYNC = "/api/routine/set-logs/sync/"


def _late_workout(make_user, tag):
    from routine.models import WorkoutSession
    from tests.test_home import _program

    trainer, client, routine, rows = _program(make_user, f"s289{tag}", sets=5, reps=10,
                                              weight=40)
    eve = timezone.localdate() - timedelta(days=2)
    start = datetime.combine(eve, time(23, 40), tzinfo=timezone.get_current_timezone())
    session = WorkoutSession.objects.create(user=client, routine=routine, day=1,
                                            status="completed", start_time=start,
                                            end_time=start + timedelta(minutes=50))
    return client, rows[0], session, eve


def _sync(api, client, exercise_id, session, *, date=None, count=4):
    sets = []
    for n in range(1, count + 1):
        row = {"client_uuid": str(uuid.uuid4()), "exercise_id": exercise_id,
               "set_number": n, "reps": 10, "weight": 40}
        if session is not None:
            row["workout_session_id"] = session.pk
        if date is not None:
            row["date"] = date.isoformat()
        sets.append(row)
    resp = api(client).post(SYNC, {"sets": sets}, content_type=JSON)
    assert resp.status_code in (200, 201, 207), resp.content
    return resp.json()


def _days(session=None, user=None):
    from routine.models import ExerciseSetLog

    rows = ExerciseSetLog.objects.all()
    rows = rows.filter(workout_session=session) if session is not None else rows.filter(
        user_exercise_progress__user=user)
    return sorted(set(rows.values_list("date", flat=True)))


def _workouts(user):
    from social.challenge_progress import measure_for_review

    window = (timezone.now() - timedelta(days=4), timezone.now() + timedelta(days=1))
    return measure_for_review("workouts", [user.pk], *window)[0].get(user.pk, 0.0)


def test_undated_sets_of_a_late_workout_are_its_start_days_and_it_counts(
        make_user, api, django_capture_on_commit_callbacks):
    client, row, session, eve = _late_workout(make_user, "sync")
    with django_capture_on_commit_callbacks(execute=True):
        _sync(api, client, row.exercise_id, session)
    assert _days(session) == [eve]
    assert _workouts(client) == 1.0


def test_a_sent_date_is_kept(make_user, api, django_capture_on_commit_callbacks):
    client, row, session, eve = _late_workout(make_user, "sent")
    after_midnight = eve + timedelta(days=1)
    with django_capture_on_commit_callbacks(execute=True):
        _sync(api, client, row.exercise_id, session, date=after_midnight)
    assert _days(session) == [after_midnight]


def test_an_undated_set_with_no_session_is_still_today(make_user, api,
                                                       django_capture_on_commit_callbacks):
    client, row, _session, _eve = _late_workout(make_user, "none")
    with django_capture_on_commit_callbacks(execute=True):
        _sync(api, client, row.exercise_id, None, count=1)
    assert _days(user=client) == [timezone.localdate()]


def test_the_single_post_dates_an_undated_session_set_on_the_sessions_day(make_user,
                                                                          api):
    """`POST set-logs/` refused it: an omitted date was today, and today was not the
    day of the session's progress row (400 `set_date_mismatch`)."""
    from routine.models import UserExerciseProgress

    client, row, session, eve = _late_workout(make_user, "single")
    progress = UserExerciseProgress.objects.create(user=client, exercise=row.exercise,
                                                   date=eve, target_sets=row.sets)
    resp = api(client).post("/api/routine/set-logs/", {
        "user_exercise_progress": progress.pk, "workout_session": session.pk,
        "set_number": 1, "weight": 40, "reps": 10}, content_type=JSON)
    assert resp.status_code == 201, resp.content
    assert _days(session) == [eve]


def test_the_single_post_without_a_session_is_still_today(make_user, api):
    """The freeze rule stays: an undated set on an OLDER progress row with no session is
    today's and is refused against that row (tests/test_coaching_freeze.py)."""
    from routine.models import UserExerciseProgress

    client, row, _session, eve = _late_workout(make_user, "single_none")
    progress = UserExerciseProgress.objects.create(user=client, exercise=row.exercise,
                                                   date=eve, target_sets=row.sets)
    resp = api(client).post("/api/routine/set-logs/", {
        "user_exercise_progress": progress.pk, "set_number": 1, "weight": 40, "reps": 10},
        content_type=JSON)
    assert resp.status_code == 400, resp.content
