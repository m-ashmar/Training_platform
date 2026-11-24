"""SIDE_BUGS S-262 (Road to 10 phase 0 review, 2026-11-23; the manager's ruling the same
day): a bought programme plans its sessions a week, not seven dates.

A buyer's copy numbers SESSIONS (`template.days` a week, no dates). Home's week strip and
`week_stats.workouts.planned` — and the weekly workout goal's target, which reads the
same function by the owner's decision 5 — mapped its days onto dates by the calendar
cycle, so a 3-a-week programme planned all seven days of a week. Now `planned` is the
programme's sessions a week inside its window, capped in its last week at the sessions
left; the strip keeps `done` and `today`, draws `planned` only on the next dates still
owed this week and `rest` elsewhere. Dated programmes and a trainer's cycles are
counted by the calendar as before. The bought-programme tests fail on the code before
the fix; the two guards pass on both.
"""
from datetime import timedelta

import pytest
from django.utils import timezone

from tests.test_road10_review_s01_bought_week import _bought
from tests.test_road10_s01_schedule import _close, _routine

pytestmark = pytest.mark.django_db

HOME = "/api/v1/home"


def _home(api, person):
    from training_platform.cache import private_cache

    private_cache().clear()
    body = api(person).get(HOME, HTTP_X_APP_CHANNEL="apk").json()
    return {s["type"]: s["data"] for s in body["sections"]}


def _week_start():
    from home.sections import week_start

    return week_start(timezone.localdate())


def _since_week_start(days_back=0):
    """Days from the start of this week (Saturday) to today, plus `days_back`."""
    return (timezone.localdate() - _week_start()).days + days_back


def _strip_states(home):
    return [entry["state"] for entry in home["workout_hero"]["week_strip"]]


def _after_today(states):
    today = states.index("today")
    return states[:today], states[today + 1:]


def test_a_bought_programme_plans_three_a_week_and_the_goal_agrees(make_user, api):
    from analytics.goals import FROM_PROGRAM, weekly_target

    client, routine = _bought(make_user, api, "s262three", bought_ago=_since_week_start(7))
    _close(client, routine, 1, 0)                    # today's session, done

    home = _home(api, client)
    assert home["week_stats"]["workouts"] == {"done": 1, "planned": 3}
    assert weekly_target(client.pk) == (3, FROM_PROGRAM)

    before, after = _after_today(_strip_states(home))
    assert "planned" not in before                   # a missed date is not owed
    owed = min(2, len(after))                        # 3 planned, today's done
    assert after == ["planned"] * owed + ["rest"] * (len(after) - owed)


def test_today_takes_one_of_the_weeks_sessions_until_it_is_done(make_user, api):
    client, routine = _bought(make_user, api, "s262today", bought_ago=_since_week_start(7))

    states = _strip_states(_home(api, client))
    _before, after = _after_today(states)
    owed = min(2, len(after))                        # 3 planned, one of them today
    assert after == ["planned"] * owed + ["rest"] * (len(after) - owed)
    assert states.count("planned") + 1 <= 3


def test_the_last_week_plans_no_more_than_the_sessions_left(make_user, api):
    from analytics.goals import weekly_target

    client, routine = _bought(make_user, api, "s262last", bought_ago=60)
    for day in range(1, 12):                         # 11 of 12 sessions, long ago
        _close(client, routine, day, 60 - day)
    _close(client, routine, 12, 0)                   # the last one, today

    home = _home(api, client)
    assert home["week_stats"]["workouts"]["planned"] == 1
    assert weekly_target(client.pk)[0] == 1
    _before, after = _after_today(_strip_states(home))
    assert "planned" not in after


# ---------------------------------------------------------------- unchanged
def test_a_dated_programme_still_plans_its_dates(make_user, api):
    """28 days that ARE its dates, two workouts in each seven: 2 a week, by the calendar."""
    from analytics.goals import weekly_target

    workouts = [1, 3, 8, 10, 15, 17, 22, 24]
    _t, client, routine = _routine(make_user, "s262dated", workouts=workouts, days=28,
                                   start_ago=7, end_after=27)
    _close(client, routine, 1, 0)
    home = _home(api, client)
    assert home["week_stats"]["workouts"]["planned"] == 2
    assert weekly_target(client.pk)[0] == 2


def test_a_trainers_cycle_still_plans_by_the_calendar(make_user, api):
    """A 3-day cycle with work on days 1 and 2, no end: every date whose day of the cycle
    has work is planned, as before."""
    from analytics.goals import weekly_target

    _t, client, routine = _routine(make_user, "s262cycle", workouts=[1, 2], days=3,
                                   start_ago=20)
    _close(client, routine, 1, 0)
    start = _week_start()
    expected = sum(1 for offset in range(7)
                   if ((start + timedelta(days=offset) - routine.start_date).days % 3) + 1
                   in (1, 2))
    home = _home(api, client)
    assert home["week_stats"]["workouts"]["planned"] == expected
    assert weekly_target(client.pk)[0] == expected
