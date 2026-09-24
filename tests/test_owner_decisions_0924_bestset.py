"""«أفضل مجموعة» — the best set (the owner, F3, 2026-09-24).

"We must have the best set, which is the highest volume set in a session (weight x
reps)". Among equal volumes the heavier weight wins. It is `top_set` on every point of
`analytics/strength/?exercise_id=` and on every overview row (the window's best, with
its date). `best`, `best_weight` and `best_reps` keep their meanings.
"""
from datetime import timedelta

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

pytestmark = pytest.mark.django_db

URL = "/api/routine/analytics/strength/"


def _exercise(trainer, name):
    from routine.models import Exercise

    return Exercise.objects.create(name=name, created_by=trainer)


def _day(user, exercise, sets, *, days_ago=0):
    """One training day: `sets` = [(weight, reps), ...] in set order."""
    from routine.models import ExerciseSetLog, UserExerciseProgress

    date = timezone.localdate() - timedelta(days=days_ago)
    progress = UserExerciseProgress.objects.create(user=user, exercise=exercise, date=date,
                                                   target_sets=len(sets))
    for number, (weight, reps) in enumerate(sets, start=1):
        ExerciseSetLog.objects.create(user_exercise_progress=progress, set_number=number,
                                      weight=weight, reps=reps, date=date)
    return date


def _people(make_user, tag):
    trainer = make_user(f"bs_t_{tag}", user_type="trainer")
    client = make_user(f"bs_c_{tag}")
    return trainer, client


def test_a_lighter_set_of_ten_beats_a_heavy_single(make_user, api):
    trainer, client = _people(make_user, "single")
    squat = _exercise(trainer, "bs squat")
    _day(client, squat, [(140, 1), (100, 10), (110, 5)])

    body = api(client).get(f"{URL}?exercise_id={squat.pk}").json()
    point = body["points"][0]
    assert point["top_set"] == {"weight": 100.0, "reps": 10, "volume_kg": 1000.0}
    # The one-rep-max fields keep their meaning: the day's strength is still decided by
    # the estimate, not by the volume.
    assert point["best_weight"] == 140.0 and point["best_reps"] == 1
    assert body["best"]["weight"] == 140.0, "`best` is still the heaviest set"
    assert body["top_set"]["volume_kg"] == 1000.0
    assert body["top_set"]["date"] == timezone.localdate().isoformat()


def test_equal_volume_goes_to_the_heavier_weight(make_user, api):
    trainer, client = _people(make_user, "tie")
    bench = _exercise(trainer, "bs bench")
    _day(client, bench, [(50, 12), (60, 10), (100, 6), (75, 8)])     # all 600 kg

    point = api(client).get(f"{URL}?exercise_id={bench.pk}").json()["points"][0]
    assert point["top_set"] == {"weight": 100.0, "reps": 6, "volume_kg": 600.0}


def test_sets_without_weight_or_reps_never_compete(make_user, api):
    from routine.models import ExerciseSetLog, UserExerciseProgress

    trainer, client = _people(make_user, "empty")
    row = _exercise(trainer, "bs row")
    date = _day(client, row, [(40, 8)])
    progress = UserExerciseProgress.objects.get(user=client, exercise=row, date=date)
    ExerciseSetLog.objects.create(user_exercise_progress=progress, set_number=2,
                                  weight=None, reps=50, date=date)
    ExerciseSetLog.objects.create(user_exercise_progress=progress, set_number=3,
                                  weight=0, reps=40, date=date)
    ExerciseSetLog.objects.create(user_exercise_progress=progress, set_number=4,
                                  weight=500, reps=0, date=date)

    body = api(client).get(f"{URL}?exercise_id={row.pk}").json()
    assert body["points"][0]["top_set"] == {"weight": 40.0, "reps": 8, "volume_kg": 320.0}


def test_no_set_with_both_answers_null(make_user, api):
    trainer, client = _people(make_user, "none")
    plank = _exercise(trainer, "bs plank")
    _day(client, plank, [(None, 1), (0, 60)])                        # held, not lifted
    body = api(client).get(f"{URL}?exercise_id={plank.pk}").json()
    assert body["points"] == []
    assert body["top_set"] is None


def test_a_long_set_competes_for_volume_but_not_for_the_one_rep_max(make_user, api):
    trainer, client = _people(make_user, "long")
    press = _exercise(trainer, "bs press")
    _day(client, press, [(30, 25), (50, 5)])                         # 750 vs 250

    point = api(client).get(f"{URL}?exercise_id={press.pk}").json()["points"][0]
    assert point["top_set"] == {"weight": 30.0, "reps": 25, "volume_kg": 750.0}
    assert point["best_weight"] == 50.0 and point["best_reps"] == 5


def test_the_overview_row_carries_the_windows_best_with_its_date(make_user, api):
    from routine.strength import TREND_WEEKS

    trainer, client = _people(make_user, "window")
    dead = _exercise(trainer, "bs deadlift")
    too_old = TREND_WEEKS * 7 + 3
    _day(client, dead, [(200, 10)], days_ago=too_old)                # outside the window
    peak = _day(client, dead, [(120, 8), (100, 10)], days_ago=14)    # 1000 is the best
    _day(client, dead, [(130, 5)], days_ago=7)
    _day(client, dead, [(125, 6)], days_ago=0)
    # A day of nothing but long sets has no strength point, but its volume is real.
    long_only = _exercise(trainer, "bs endurance")
    _day(client, long_only, [(20, 20)], days_ago=3)
    _day(client, long_only, [(40, 5)], days_ago=1)

    body = api(client).get(URL).json()
    rows = {row["exercise_name"]: row for row in body["exercises"]}
    assert rows["bs deadlift"]["top_set"] == {"weight": 100.0, "reps": 10,
                                              "volume_kg": 1000.0, "date": peak.isoformat()}
    assert rows["bs endurance"]["top_set"]["volume_kg"] == 400.0
    assert "points" not in rows["bs deadlift"]


def test_the_same_best_set_twice_names_the_most_recent_day(make_user, api):
    trainer, client = _people(make_user, "again")
    curl = _exercise(trainer, "bs curl")
    _day(client, curl, [(20, 10)], days_ago=10)
    latest = _day(client, curl, [(20, 10)], days_ago=2)
    body = api(client).get(f"{URL}?exercise_id={curl.pk}").json()
    assert body["top_set"]["date"] == latest.isoformat()


def test_a_duplicate_numbered_set_is_collapsed_before_it_competes(make_user, api):
    from routine.models import ExerciseSetLog, UserExerciseProgress

    trainer, client = _people(make_user, "dup")
    lunge = _exercise(trainer, "bs lunge")
    date = _day(client, lunge, [(30, 10)])
    progress = UserExerciseProgress.objects.get(user=client, exercise=lunge, date=date)
    ExerciseSetLog.objects.create(user_exercise_progress=progress, set_number=1,
                                  weight=32, reps=10, date=date)     # the correction
    point = api(client).get(f"{URL}?exercise_id={lunge.pk}").json()["points"][0]
    assert point["top_set"]["weight"] == 32.0
    assert point["sets"] == 1


def test_top_set_adds_no_query_per_point_or_per_exercise(make_user, api):
    trainer, client = _people(make_user, "queries")
    few = _exercise(trainer, "bs few")
    many = _exercise(trainer, "bs many")
    _day(client, few, [(50, 5)])
    for days_ago in range(0, 42, 3):
        _day(client, many, [(50, 5), (55, 5), (60, 3)], days_ago=days_ago)

    caller = api(client)
    caller.get(URL)             # the first request of a client warms per-user caches

    def count(url):
        with CaptureQueriesContext(connection) as queries:
            assert caller.get(url).status_code == 200
        return len(queries)

    assert count(f"{URL}?exercise_id={few.pk}") == count(f"{URL}?exercise_id={many.pk}")
