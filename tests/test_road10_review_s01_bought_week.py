"""Road to 10 S0.1, review of phase 0 (2026-11-23): the week of a BOUGHT programme.

A buyer's copy numbers SESSIONS, not dates (`template.days` a week, `template.weeks`
weeks, no end date). Its schedule drew the slots from the sessions — today's block of
`template.days` — but still counted `week` from the purchase date on the calendar. One
answer then contradicted itself: three weeks after buying, with two sessions done,
`week` was 3 while `slots_week` was 1 and the slots were week 1's sessions, and the hero
said «الأسبوع 3 من 4» over the first week's workouts. The owner's D15 is that a missed
day does not move the programme on; the week is where the person is in it. These fail
on the code before the fix (`week` was the calendar's).
"""
from datetime import timedelta

import pytest
from django.utils import timezone

from tests.test_road10_s01_schedule import _close, _hero, _schedule

pytestmark = pytest.mark.django_db


def _bought(make_user, api, tag, *, bought_ago):
    """A 3-a-week, 4-week programme bought `bought_ago` days ago."""
    from routine.models import Routine
    from tests.test_routine_products import _buy, _client, _published

    _trainer, _template, product_id = _published(make_user, api, tag)
    client = _client(make_user, tag)
    resp = _buy(api, client, product_id, HTTP_X_APP_CHANNEL="apk")
    assert resp.status_code in (200, 201), resp.content
    routine = Routine.objects.get(assigned_to=client)
    Routine.objects.filter(pk=routine.pk).update(
        start_date=timezone.localdate() - timedelta(days=bought_ago))
    routine.refresh_from_db()
    assert (routine.days, routine.end_date) == (12, None)
    return client, routine


def test_a_bought_programme_behind_the_calendar_is_still_in_its_first_week(make_user, api):
    client, routine = _bought(make_user, api, "bwbehind", bought_ago=20)
    _close(client, routine, 1, 19)
    _close(client, routine, 2, 17)

    body = _schedule(api, client, routine)
    assert body["today"] == {"day": 3, "state": "today"}
    assert [s["day"] for s in body["slots"]] == [1, 2, 3]
    assert (body["week"], body["slots_week"], body["weeks_total"]) == (1, 1, 4)
    hero = _hero(api, client)
    assert (hero["day"], hero["week"], hero["weeks_total"]) == (3, 1, 4)


def test_a_bought_programme_ahead_of_the_calendar_is_in_the_week_it_reached(make_user, api):
    client, routine = _bought(make_user, api, "bwahead", bought_ago=4)
    for day, ago in ((1, 4), (2, 3), (3, 2), (4, 1)):
        _close(client, routine, day, ago)

    body = _schedule(api, client, routine)
    assert body["today"] == {"day": 5, "state": "today"}
    assert (body["week"], body["slots_week"]) == (2, 2)
    assert [s["day"] for s in body["slots"]] == [4, 5, 6]


def test_the_week_of_a_bought_programme_never_passes_its_weeks(make_user, api):
    client, routine = _bought(make_user, api, "bwlast", bought_ago=200)
    for day in range(1, 13):
        _close(client, routine, day, 200 - day)

    body = _schedule(api, client, routine)
    assert body["finished"] is True
    assert (body["week"], body["weeks_total"]) == (4, 4)


def test_a_dated_programme_still_counts_its_weeks_on_the_calendar(make_user, api):
    """Unchanged for a programme whose days are dates: its week is the calendar's."""
    from tests.test_road10_s01_schedule import _routine

    _t, client, routine = _routine(make_user, "bwdated", workouts=[1, 2], days=28,
                                   start_ago=15, end_after=27)
    body = _schedule(api, client, routine)
    assert (body["week"], body["weeks_total"]) == (3, 4)
