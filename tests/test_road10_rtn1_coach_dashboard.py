"""The fix run's RTN-1, `GET /api/routine/analytics/admin_dashboard/` (2026-11-24):

S-172  a trainer was a row of their own client list: the clients came from
       `accessible_user_ids`, which always holds the caller's own id. Now only clients:
       a trainer's approved ones, every one for an admin.
S-173  `completion_rate` divided completed rows by every `RoutineProgress` row, the
       stranded `not_started` scaffolds included, so a client who trained every day read
       50%. Now distinct days per routine, as `analytics/completion` counts them.

The first test of each fails on the code before.
"""
from datetime import timedelta

import pytest
from django.utils import timezone

from tests.test_fx_rttr import _program

pytestmark = pytest.mark.django_db

DASHBOARD = "/api/routine/analytics/admin_dashboard/"


def _rows(api, who):
    resp = api(who).get(DASHBOARD)
    assert resp.status_code == 200, resp.content
    return {row["client_id"]: row for row in resp.json()["dashboard"]}


def _train(client, routine, day, date):
    """A day completed at its real training date — not on its scaffold's date."""
    from routine.models import RoutineProgress

    RoutineProgress.objects.create(user=client, routine=routine, day=day, date=date,
                                   status="completed", exercises_completed=1,
                                   total_exercises=1)


def test_s172_the_trainer_is_not_a_row_of_their_own_client_list(make_user, api):
    trainer, client, _routine, _rows_ = _program(make_user, "s172", coached=True)
    rows = _rows(api, trainer)
    assert trainer.pk not in rows
    assert set(rows) == {client.pk}


def test_s172_an_admin_still_reads_every_client_and_no_staff(make_user, api):
    _trainer, client, _routine, _rows_ = _program(make_user, "s172a", coached=True)
    admin = make_user("s172_admin", user_type="admin")
    resp = api(admin).get(f"{DASHBOARD}?page_size=100")
    ids = {row["client_id"] for row in resp.json()["dashboard"]}
    assert client.pk in ids and admin.pk not in ids and _trainer.pk not in ids


def test_s173_every_day_trained_reads_100_percent(make_user, api):
    trainer, client, routine, _rows_ = _program(make_user, "s173", days=2, coached=True)
    today = timezone.localdate()
    _train(client, routine, 1, today - timedelta(days=2))
    _train(client, routine, 2, today - timedelta(days=1))
    assert _rows(api, trainer)[client.pk]["completion_rate"] == 100


def test_s173_half_the_days_read_50_percent_across_routines(make_user, api):
    from routine.models import Routine, RoutineExercise

    trainer, client, first, rows = _program(make_user, "s173b", days=2, coached=True)
    second = Routine.objects.create(name="s173b two", created_by=trainer, days=2,
                                    start_date=timezone.localdate())
    for day in (1, 2):
        RoutineExercise.objects.create(routine=second, exercise=rows[day - 1].exercise,
                                       day=day, order=1, sets=3, reps=10)
    second.assigned_to.add(client)
    yesterday = timezone.localdate() - timedelta(days=1)
    _train(client, first, 1, yesterday)
    _train(client, second, 1, yesterday)
    # Day 1 of each routine of two days: 2 of 4 days, never "1 distinct day of 2".
    assert _rows(api, trainer)[client.pk]["completion_rate"] == 50
