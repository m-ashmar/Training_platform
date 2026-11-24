"""Which finish leaves the row the month trend counts (the manager's question from the
app's walk of `walk.client_progress`, 2026-11-24).

`GET /api/routine/analytics/trends/?period=month` counts, per month of the TRAINING date,
the `RoutineProgress` rows with `status='completed'` (`routine/views.py`, `trends`). For a
real client finishing through the API, each path:

  | the finish                                          | completed row on the date? |
  |-----------------------------------------------------|----------------------------|
  | the workout session PATCHed `completed`, alone      | no (a `DayOutcome` only)   |
  |   ... with every set of the day logged              | yes (the recalculation)    |
  | `routines/{id}/update_progress/` `completed`        | yes                        |
  |   (the app's `closeDay`, the end of every workout)  |                            |
  | `user-exercise-progress/bulk-complete/`             | yes                        |

So a client who finishes the way the app does is counted: the trend's zeros on
`walk.client_progress` were the seed's (it wrote the sessions and one exercise's sets
directly, and never closed the day), not the product's.
"""
from datetime import timedelta

import pytest
from django.utils import timezone

from tests.test_day_outcomes import JSON, _close, _log_sets, _program

pytestmark = pytest.mark.django_db

TRENDS = "/api/routine/analytics/trends/"


def _completed_on(client, routine, date):
    from routine.models import RoutineProgress

    return RoutineProgress.objects.filter(user=client, routine=routine, date=date,
                                          status="completed").exists()


def _month_completed(api, client):
    body = api(client).get(TRENDS, {"period": "month"}).json()
    return sum(row["completed"] for row in body["completion_trend"])


def _session(api, client, routine):
    started = api(client).post("/api/routine/workout-sessions/",
                               {"routine": routine.pk, "day": 1}, content_type=JSON)
    assert started.status_code in (200, 201), started.content
    return started.json()["id"]


def _finish(api, client, session):
    done = api(client).patch(f"/api/routine/workout-sessions/{session}/",
                             {"status": "completed"}, content_type=JSON)
    assert done.status_code == 200, done.content


def test_a_session_finished_alone_leaves_no_completed_row(make_user, api,
                                                          django_capture_on_commit_callbacks):
    _trainer, client, routine, rows = _program(make_user, "tf_alone")
    session = _session(api, client, routine)
    _log_sets(client, api, routine, rows[0], 1, session=session,
              commit=django_capture_on_commit_callbacks)
    _finish(api, client, session)
    assert not _completed_on(client, routine, timezone.localdate())
    assert _month_completed(api, client) == 0


def test_the_apps_close_of_the_day_leaves_it(make_user, api,
                                             django_capture_on_commit_callbacks):
    _trainer, client, routine, rows = _program(make_user, "tf_close")
    session = _session(api, client, routine)
    _log_sets(client, api, routine, rows[0], 1, session=session,
              commit=django_capture_on_commit_callbacks)
    _finish(api, client, session)
    closed = _close(api, client, routine)
    assert closed.status_code == 200, closed.content
    assert _completed_on(client, routine, timezone.localdate())
    assert _month_completed(api, client) == 1


def test_a_day_logged_in_full_completes_itself(make_user, api,
                                               django_capture_on_commit_callbacks):
    _trainer, client, routine, rows = _program(make_user, "tf_full")
    session = _session(api, client, routine)
    for row in rows:
        _log_sets(client, api, routine, row, row.sets, session=session,
                  commit=django_capture_on_commit_callbacks)
    _finish(api, client, session)
    assert _completed_on(client, routine, timezone.localdate())
    assert _month_completed(api, client) == 1


def test_bulk_complete_leaves_it_on_its_own_date(make_user, api,
                                                 django_capture_on_commit_callbacks):
    _trainer, client, routine, _rows = _program(make_user, "tf_bulk")
    day = timezone.localdate() - timedelta(days=2)
    with django_capture_on_commit_callbacks(execute=True):
        resp = api(client).post("/api/routine/user-exercise-progress/bulk-complete/",
                                {"routine_id": routine.pk, "day": 1,
                                 "date": day.isoformat(), "completed_sets": 3,
                                 "skipped": False}, content_type=JSON)
    assert resp.status_code in (200, 201), resp.content
    assert _completed_on(client, routine, day)
