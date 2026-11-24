"""The fix run's RTN-1, the trend row (2026-11-24): "a session PATCHed `completed` alone
leaves no completed `RoutineProgress` row, so `analytics/trends/` does not count it".

It stays, deliberately. `trends/`'s `completion_trend.completed` counts days whose
`RoutineProgress` is `completed`: days every set completed (`auto`), or days the PERSON
closed as done — "complete", "complete anyway" after the unlogged-sets warning
(`confirm_unlogged`), "log the whole day" (`routine.models.PERSON_CLOSED_VIAS`, SIDE_BUGS
S-110). Finishing a workout is neither: with nothing logged it closes the day SHORT
(`via: session`, `complete: false`, every planned set missed), the owner's rule of
2026-09-20, and nobody said the work was done. Counting it would put a day in the trend
that the client never claimed and the streak calls a break. A client who did the work
without logging it says so with `routines/{id}/update_progress/` and `confirm_unlogged`;
the trend then counts it (the streak still does not: sets were missed).

These tests hold what each reader counts.
"""
import pytest
from django.utils import timezone

from tests.test_day_outcomes import _close, _log_sets, _program

pytestmark = pytest.mark.django_db

JSON = "application/json"


def _finish_session(api, client, routine, commit, *, sets=0, row=None):
    opened = api(client).post("/api/routine/workout-sessions/",
                              {"routine": routine.pk, "day": 1}, content_type=JSON)
    assert opened.status_code == 201, opened.content
    session = opened.json()["id"]
    if sets:
        _log_sets(client, api, routine, row, sets, session=session, commit=commit)
    with commit(execute=True):
        done = api(client).patch(f"/api/routine/workout-sessions/{session}/",
                                 {"status": "completed"}, content_type=JSON)
    assert done.status_code == 200, done.content


def _readers(api, client):
    today = timezone.localdate().isoformat()
    trend = api(client).get("/api/routine/analytics/trends/?period=day").json()
    completed = sum(row["completed"] for row in trend["completion_trend"]
                    if str(row["period"])[:10] == today)
    streak = api(client).get("/api/routine/analytics/streaks/").json()["current_streak"]
    return completed, streak


def test_a_session_finished_with_nothing_logged_is_not_a_day_done(
        make_user, api, django_capture_on_commit_callbacks):
    from routine.models import DayOutcome

    _trainer, client, routine, _rows = _program(make_user, "trend0", sets=3, exercises=1)
    _finish_session(api, client, routine, django_capture_on_commit_callbacks)
    outcome = DayOutcome.objects.get(user=client, routine=routine, day=1)
    assert (outcome.via, outcome.complete, outcome.missed_sets) == ("session", False, 3)
    assert _readers(api, client) == (0, 0)


def test_a_session_finished_with_every_set_logged_counts_everywhere(
        make_user, api, django_capture_on_commit_callbacks):
    _trainer, client, routine, (row,) = _program(make_user, "trend3", sets=3, exercises=1)
    _finish_session(api, client, routine, django_capture_on_commit_callbacks, sets=3,
                    row=row)
    assert _readers(api, client) == (1, 1)


def test_work_confirmed_without_logs_is_in_the_trend_not_the_streak(make_user, api):
    _trainer, client, routine, _rows = _program(make_user, "trendc", sets=3, exercises=1)
    closed = _close(api, client, routine, confirm=True)
    assert closed.status_code == 200, closed.content
    assert _readers(api, client) == (1, 0)
