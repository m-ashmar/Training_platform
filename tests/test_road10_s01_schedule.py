"""Road to 10 S0.1 and S0.2 (BACKEND_BUGS B-197, B-196): ONE rule for today's programme
day, a read that carries it, and the hero's minutes are the day's.

The owner's D15 (2026-11-22): today is the next day not done, not the calendar cycle; a
programme past its dates says it is finished. Home's hero named the day by the cycle from
`start_date`, the week with no upper bound (week 12 of a nine-week programme), and the
minutes of the whole routine on a one-day card. Every test here fails on the code before
`routine/schedule.py`: the route did not exist, and the hero answered the calendar's day,
an unbounded week and `Routine.estimated_duration`.
"""
from datetime import timedelta

import pytest
from django.utils import timezone

pytestmark = pytest.mark.django_db

JSON = "application/json"
HOME = "/api/v1/home"


def _routine(make_user, tag, *, workouts, days, start_ago=0, end_after=None, sets=3,
             client=None, trainer=None, names=None):
    """A routine of `days` days with one exercise on each day in `workouts`, assigned to
    a client. `start_ago` days before today it starts; `end_after` days after its start
    it ends (None: open)."""
    from routine.models import Exercise, Routine, RoutineExercise

    trainer = trainer or make_user(f"sch_t_{tag}", user_type="trainer")
    client = client or make_user(f"sch_c_{tag}")
    today = timezone.localdate()
    start = today - timedelta(days=start_ago)
    routine = Routine.objects.create(
        name=f"sch {tag}", created_by=trainer, days=days, start_date=start,
        end_date=(start + timedelta(days=end_after)) if end_after is not None else None,
        day_names=names or {})
    muscle = {1: "Upper Chest", 2: "Upper Back", 3: "Biceps"}
    for day in workouts:
        exercise = Exercise.objects.create(name=f"sch ex {tag} {day}", created_by=trainer,
                                           target_muscle=muscle.get(day, "Triceps"))
        RoutineExercise.objects.create(routine=routine, exercise=exercise, day=day,
                                       order=1, sets=sets, reps=10)
    routine.assigned_to.add(client)
    return trainer, client, routine


def _close(client, routine, day, days_ago, *, complete=True):
    """A day outcome, as every closing path writes one (`routine/outcomes.py`)."""
    from routine.models import DayOutcome

    return DayOutcome.objects.create(
        user=client, routine=routine, day=day,
        date=timezone.localdate() - timedelta(days=days_ago),
        via="completed" if complete else "skipped", complete=complete,
        planned_sets=3, logged_sets=3 if complete else 0,
        missed_sets=0 if complete else 3)


def _schedule(api, client, routine):
    resp = api(client).get(f"/api/routine/routines/{routine.pk}/schedule/")
    assert resp.status_code == 200, resp.content
    return resp.json()


def _hero(api, client):
    from training_platform.cache import private_cache

    private_cache().clear()
    body = api(client).get(HOME, HTTP_X_APP_CHANNEL="apk").json()
    return next(s["data"] for s in body["sections"] if s["type"] == "workout_hero")


# ------------------------------------------------------------------------ the rule
def test_a_missed_day_does_not_skip_the_workout(make_user, api):
    """Day 1 done four days ago, nothing since. The calendar cycle (5 % 3 + 1) said day 3;
    the next day not done is day 2 — and Home says the same."""
    _t, client, routine = _routine(make_user, "missed", workouts=[1, 2, 3], days=3,
                                   start_ago=5)
    _close(client, routine, 1, 4)

    body = _schedule(api, client, routine)
    assert body["today"] == {"day": 2, "state": "today"}
    assert body["next_day"] == 2
    assert [s["state"] for s in body["slots"]] == ["done", "today", "upcoming"]
    assert body["slots"][0]["closed_on"] == (timezone.localdate()
                                             - timedelta(days=4)).isoformat()

    hero = _hero(api, client)
    assert (hero["day"], hero["cta"]["state"]) == (2, "start")


def test_rest_days_are_kept_and_never_owed(make_user, api):
    """Push, rest, pull. Push closed yesterday: today is the rest day. Push closed three
    days ago and nothing since: the rest was taken, today is pull (the workout waits)."""
    from routine.schedule import build

    _t, client, routine = _routine(make_user, "rest", workouts=[1, 3], days=3,
                                   start_ago=3)
    _close(client, routine, 1, 1)
    today = timezone.localdate()

    body = _schedule(api, client, routine)
    assert body["today"] == {"day": 2, "state": "rest"}
    assert [s["state"] for s in body["slots"]] == ["done", "rest", "upcoming"]
    hero = _hero(api, client)
    assert (hero["day"], hero["cta"]) == (2, {"state": "rest"})

    later = build(client, routine, today + timedelta(days=2))
    assert later.today == {"day": 3, "state": "today"}


def test_a_day_closed_today_is_today_and_a_short_one_is_never_offered_again(make_user, api):
    _t, client, routine = _routine(make_user, "short", workouts=[1, 2], days=2)
    _close(client, routine, 1, 0, complete=False)

    body = _schedule(api, client, routine)
    assert body["today"] == {"day": 1, "state": "short"}
    assert body["next_day"] == 2
    assert [s["state"] for s in body["slots"]] == ["short", "upcoming"]


def test_a_cycle_begins_again_once_every_day_is_done(make_user, api):
    _t, client, routine = _routine(make_user, "cycle", workouts=[1, 2], days=2,
                                   start_ago=4)
    _close(client, routine, 1, 3)
    _close(client, routine, 2, 2)

    body = _schedule(api, client, routine)
    assert (body["today"], body["finished"]) == ({"day": 1, "state": "today"}, False)
    assert [s["state"] for s in body["slots"]] == ["today", "upcoming"]


def test_a_programme_past_its_dates_is_finished_and_its_week_is_bounded(make_user, api):
    """A nine-week programme (63 days) that started 80 days ago: the hero said week 12 and
    offered a workout. It is finished, week 9 of 9, and the days it ended without are
    `missed`."""
    _t, client, routine = _routine(make_user, "past", workouts=[1, 2], days=63,
                                   start_ago=80, end_after=62)
    _close(client, routine, 1, 79)

    body = _schedule(api, client, routine)
    assert (body["finished"], body["week"], body["weeks_total"]) == (True, 9, 9)
    assert (body["today"], body["next_day"]) == (None, None)
    assert [s["state"] for s in body["slots"]][:2] == ["done", "missed"]

    hero = _hero(api, client)
    assert (hero["week"], hero["weeks_total"], hero["finished"]) == (9, 9, True)
    assert hero["cta"] == {"state": "finished"}


def test_a_programme_written_out_in_full_finishes_when_every_day_is_done(make_user, api):
    _t, client, routine = _routine(make_user, "full", workouts=[1, 8], days=8,
                                   start_ago=9)
    _close(client, routine, 1, 8)
    _close(client, routine, 8, 1)

    body = _schedule(api, client, routine)
    assert body["finished"] is True
    assert body["today"] is None
    assert body["slots_week"] == 2
    assert [s["day"] for s in body["slots"]] == [8]
    assert body["slots"][0]["state"] == "done"


def test_a_dated_programme_dates_its_slots_and_says_when_it_starts(make_user, api):
    _t, client, routine = _routine(make_user, "dated", workouts=[1, 3], days=14,
                                   start_ago=-3, end_after=13, names={"1": "Push"})
    body = _schedule(api, client, routine)
    start = timezone.localdate() + timedelta(days=3)
    assert body["today"] == {"day": 1, "state": "not_started"}
    assert body["starts_on"] == start.isoformat()
    assert (body["week"], body["weeks_total"], body["days_in_cycle"]) == (1, 2, 14)
    assert [s["day"] for s in body["slots"]] == list(range(1, 8))
    assert body["slots"][0]["date"] == start.isoformat()
    assert body["slots"][2]["date"] == (start + timedelta(days=2)).isoformat()
    assert (body["slots"][0]["name"], body["slots"][1]["name"]) == ("Push", None)
    assert body["slots"][0]["muscles"] == ["Upper Chest"]
    assert body["slots"][1]["state"] == "rest"


# ---------------------------------------------------------------------- the minutes
def test_the_heros_minutes_are_the_days_the_schedules_number(make_user, api):
    """BACKEND_BUGS B-196: the hero answered `Routine.estimated_duration` (60) for one
    day. It is the routine's own formula on that day's rows, the schedule's slot."""
    from routine.models import Exercise, RoutineExercise
    from routine.schedule import day_minutes

    trainer, client, routine = _routine(make_user, "minutes", workouts=[1], days=1)
    for index, sets in enumerate((4, 3, 3), start=2):
        exercise = Exercise.objects.create(name=f"sch min {index}", created_by=trainer,
                                           target_muscle="Upper Back")
        RoutineExercise.objects.create(routine=routine, exercise=exercise, day=1,
                                       order=index, sets=sets, reps=10, rest_time=60)
    rows = list(RoutineExercise.objects.filter(routine=routine, day=1)
                .values("sets", "reps", "rest_time"))
    # 13 sets x 10 reps x 4 s + 9 rests x 60 s + 3 changes x 120 s = 1420 s.
    assert day_minutes(rows) == 24

    slot = _schedule(api, client, routine)["slots"][0]
    hero = _hero(api, client)
    assert slot["counts"] == {"exercises": 4, "sets": 13, "estimated_minutes": 24}
    assert hero["counts"] == slot["counts"]
    assert hero["counts"]["estimated_minutes"] != routine.estimated_duration


# ------------------------------------------------------------------------ who reads
def test_only_the_person_on_the_routine_reads_its_schedule(make_user, api):
    trainer, client, routine = _routine(make_user, "who", workouts=[1], days=1)
    stranger = make_user("sch_who_other")
    url = f"/api/routine/routines/{routine.pk}/schedule/"

    assert api().get(url).status_code == 401
    for caller in (trainer, stranger):
        resp = api(caller).get(url)
        assert resp.status_code == 404, (caller, resp.content)
        assert resp.json()["code"] == "not_found"
    assert api(client).get(url).json()["refusal"] is None
