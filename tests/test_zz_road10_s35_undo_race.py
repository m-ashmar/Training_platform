"""Road to 10 S3.5 under real concurrency: an undo of a meal sent while its confirm is
still running (two devices, or a tap on «تراجع» before the confirm's answer arrived).

The confirm ticked the foods in one transaction and wrote the meal's flag after it,
unlocked. An undo that ran in that gap reopened the foods, and the confirm then marked
the meal completed: a meal counted eaten with none of its foods eaten (the day's
`meals_completed` 1 at 0 kcal, home and the trainer's week agreeing with neither). The
confirm now takes the meal's row lock first, the one the undo takes, so the two run one
after the other and the meal ends one way or the other, whole.

The gap is held open on purpose: the confirm's thread stops right after its first
`transaction.atomic()` block in `diet/trainer_services.py` closes, and waits for the
undo to finish (or to block, which is the fix). What this proves is that the confirm's
writes (ticks, flag, day, funnel event) are ONE transaction the undo cannot land
inside. That the meal's lock is taken BEFORE any food is written, the order that keeps
a confirm, an undo, a swap and a trainer's edit from deadlocking, is
tests/test_road10_s35_lock_order.py's to prove (the second review). In a `test_zz_` file because
`django_db(transaction=True)` truncates every table when it finishes; nothing seeded by
a migration is relied on.
"""
import contextlib
import threading

import pytest

from tests.test_road10_s35_meal_undo import JSON, _day, _url


@pytest.mark.django_db(transaction=True)
def test_an_undo_inside_a_confirm_leaves_the_meal_whole_one_way_or_the_other(
        make_user, api, monkeypatch):
    from django.db import connections
    from django.db import transaction as real_tx

    from diet import trainer_services
    from diet.models import DailyProgress, MealComponent, MealEvent

    client, plan, meal = _day(make_user, "race")
    confirming = {}
    foods_ticked, undo_done = threading.Event(), threading.Event()

    class _HoldTheGap:
        """`transaction` as the service sees it: the confirm's thread pauses after its
        first atomic block closes, until the undo is done or 3 s have passed."""

        def atomic(self, *args, **kwargs):
            @contextlib.contextmanager
            def block():
                with real_tx.atomic(*args, **kwargs):
                    yield
                if (threading.get_ident() == confirming.get("thread")
                        and not foods_ticked.is_set()):
                    foods_ticked.set()
                    undo_done.wait(timeout=3)
            return block()

        def __getattr__(self, name):
            return getattr(real_tx, name)

    monkeypatch.setattr(trainer_services, "transaction", _HoldTheGap())
    answers = {}

    def confirm():
        confirming["thread"] = threading.get_ident()
        try:
            answers["confirm"] = api(client).post(
                _url(meal, "complete"), {"action": "complete_meal"},
                content_type=JSON).status_code
        finally:
            foods_ticked.set()
            connections.close_all()

    def undo():
        try:
            foods_ticked.wait(timeout=30)
            answers["undo"] = api(client).post(_url(meal, "undo"), {},
                                               content_type=JSON).status_code
        finally:
            undo_done.set()
            connections.close_all()

    threads = [threading.Thread(target=confirm), threading.Thread(target=undo)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    assert answers == {"confirm": 200, "undo": 200}, answers

    meal.refresh_from_db()
    eaten = MealComponent.objects.filter(meal=meal, is_completed=True).count()
    row = DailyProgress.objects.get(user=client, diet_plan=plan, date=meal.date)
    # Whole one way or the other: completed with both foods, or open with neither.
    assert (meal.is_completed, eaten) in ((True, 2), (False, 0)), (meal.is_completed, eaten)
    assert row.meals_completed == (1 if meal.is_completed else 0)
    assert row.calories_consumed == (300.0 if meal.is_completed else 0)
    assert MealEvent.objects.filter(
        meal=meal, kind=MealEvent.COMPLETED).exists() is meal.is_completed
