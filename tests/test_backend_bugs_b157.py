"""BACKEND_BUGS B-157: a quick-logged day could not be told from a live one, and could
only be closed for TODAY.

LogDay («سجّل دفعة واحدة») logs real set rows through `set-logs/`, then closes the day
with `routines/{id}/update_progress/`. That close recorded `via = completed` — the same
as a live session — and `record_day` keeps the strongest `via`, so a later plain close
could never be told apart either. And `update_progress` had no date: a LogDay for
yesterday closed TODAY, answered 409 `unlogged_sets` (today had no sets) and, once
confirmed, wrote a false 0-of-N day today.

Now: `quick_log: true` is kept on the day's outcome (`DayOutcome.quick_log`, OR-ed like
`confirmed_unlogged`, so no later close erases it; `bulk-complete` sets it too), and
`date` closes the day it names, inside the training window.
"""
from datetime import timedelta

import pytest
from django.utils import timezone

pytestmark = pytest.mark.django_db

JSON = "application/json"


def _program(make_user, tag):
    """A client on a 1-day routine of 2 exercises x 3 sets, started ten days ago."""
    from routine.models import Exercise, Routine, RoutineExercise

    trainer = make_user(f"b157_t_{tag}", user_type="trainer")
    client = make_user(f"b157_c_{tag}")
    routine = Routine.objects.create(name=f"b157 {tag}", created_by=trainer, days=1,
                                     start_date=timezone.localdate() - timedelta(days=10))
    rows = [RoutineExercise.objects.create(
        routine=routine, day=1, order=i + 1, sets=3, reps=10,
        exercise=Exercise.objects.create(name=f"b157 ex {tag} {i}", created_by=trainer))
        for i in range(2)]
    routine.assigned_to.add(client)
    return client, routine, rows


def _log_every_set(api, client, rows, date):
    from routine.models import UserExerciseProgress

    for row in rows:
        progress, _ = UserExerciseProgress.objects.get_or_create(
            user=client, exercise=row.exercise, date=date,
            defaults={"target_sets": row.sets})
        for number in range(1, row.sets + 1):
            resp = api(client).post("/api/routine/set-logs/", {
                "user_exercise_progress": progress.pk, "set_number": number,
                "weight": 20, "reps": 10, "date": date.isoformat()}, content_type=JSON)
            assert resp.status_code in (200, 201), resp.content


def _close(api, client, routine, **body):
    return api(client).post(f"/api/routine/routines/{routine.pk}/update_progress/",
                            {"day": 1, "status": "completed", **body}, content_type=JSON)


def test_quick_log_is_kept_on_the_outcome(make_user, api):
    from routine.models import DayOutcome

    client, routine, rows = _program(make_user, "kept")
    today = timezone.localdate()
    _log_every_set(api, client, rows, today)

    quick = _close(api, client, routine, quick_log=True)
    assert quick.status_code == 200, quick.content
    assert quick.json()["outcome"]["quick_log"] is True
    assert quick.json()["outcome"]["complete"] is True
    assert DayOutcome.objects.get(user=client, routine=routine, day=1, date=today).quick_log

    plain = _close(api, client, routine)
    assert plain.status_code == 200, plain.content
    assert plain.json()["outcome"]["quick_log"] is True, "a later close never erases it"
    assert DayOutcome.objects.get(user=client, routine=routine, day=1, date=today).quick_log


def test_a_live_close_is_not_a_quick_log_and_bulk_complete_is(make_user, api):
    from routine.models import DayOutcome

    client, routine, rows = _program(make_user, "live")
    today = timezone.localdate()
    _log_every_set(api, client, rows, today)
    live = _close(api, client, routine)
    assert (live.status_code, live.json()["outcome"]["quick_log"]) == (200, False)

    other_client, other, _rows = _program(make_user, "bulk")
    resp = api(other_client).post("/api/routine/user-exercise-progress/bulk-complete/", {
        "routine_id": other.pk, "day": 1, "date": today.isoformat(), "completed_sets": 3},
        content_type=JSON)
    assert resp.status_code == 200, resp.content
    assert DayOutcome.objects.get(user=other_client, routine=other, day=1,
                                  date=today).quick_log


def test_quick_log_for_yesterday_closes_yesterday(make_user, api):
    from routine.models import DayOutcome, RoutineProgress

    client, routine, rows = _program(make_user, "yday")
    today = timezone.localdate()
    yesterday = today - timedelta(days=1)
    _log_every_set(api, client, rows, yesterday)

    resp = _close(api, client, routine, date=yesterday.isoformat(), quick_log=True)
    assert resp.status_code == 200, resp.content
    outcome = resp.json()["outcome"]
    assert outcome["date"] == yesterday.isoformat()
    assert (outcome["planned_sets"], outcome["logged_sets"], outcome["missed_sets"]) == (6, 6, 0)
    assert outcome["quick_log"] is True
    assert RoutineProgress.objects.get(user=client, routine=routine, day=1,
                                       date=yesterday).status == "completed"
    assert not DayOutcome.objects.filter(user=client, routine=routine, date=today).exists()
    assert not RoutineProgress.objects.filter(user=client, routine=routine,
                                              date=today).exists()


def test_the_close_date_is_held_to_the_training_window(make_user, api):
    client, routine, _rows = _program(make_user, "window")
    today = timezone.localdate()
    for sent, code in ((today + timedelta(days=1), "date_in_future"),
                       (today - timedelta(days=400), "date_too_old"),
                       ("yesterday", "bad_date")):
        sent = sent if isinstance(sent, str) else sent.isoformat()
        resp = _close(api, client, routine, date=sent, confirm_unlogged=True)
        assert (resp.status_code, resp.json()["code"]) == (400, code), resp.content
