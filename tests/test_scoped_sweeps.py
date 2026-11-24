"""The three sweeps a caller may scope to its own rows (the framework owner's review of
the walk seed, 2026-11-24): `users.coaching.remind_upcoming(relation_ids=)`,
`subscription.tasks.expire_lapsed_subscriptions(subscription_ids=)` and
`send_renewal_reminders(subscription_ids=)`. Scoped, each touches only the rows named;
called as the beat task calls it, with no argument, each still sweeps every row, as
before.
"""
from datetime import timedelta
from decimal import Decimal
from unittest import mock

import pytest
from django.utils import timezone

from tests.test_coaching_months import (  # noqa: F401  (owners_rate is an autouse fixture)
    _accepted, _deliver_first_program, owners_rate,
)

pytestmark = pytest.mark.django_db


def _month_ending_soon(make_user, api, tag):
    """A paid month that ends in 2 days and 20 hours."""
    trainer, client, relation = _accepted(make_user, api, tag)
    _deliver_first_program(api, trainer, client)
    period = relation.periods.order_by("-number").first()
    shift = period.ends_at - (timezone.now() + timedelta(days=2, hours=20))
    type(period).objects.filter(pk=period.pk).update(starts_at=period.starts_at - shift,
                                                     ends_at=period.ends_at - shift)
    return relation


def _reminded(relation):
    return relation.periods.order_by("-number").first().renewal_reminder_sent_at is not None


def test_coaching_reminders_scoped_and_not(make_user, api):
    from users.coaching import remind_upcoming

    mine, theirs = (_month_ending_soon(make_user, api, "sw_a"),
                    _month_ending_soon(make_user, api, "sw_b"))
    with mock.patch("notifications.domain.dispatcher.emit_event"):
        assert remind_upcoming(relation_ids=[mine.pk]) == 1
        assert (_reminded(mine), _reminded(theirs)) == (True, False)
        assert remind_upcoming() == 1                      # the beat task's call
    assert _reminded(theirs)


def _plan(tag):
    from subscription.models import SubscriptionPlan

    return SubscriptionPlan.objects.create(
        name=f"sweep {tag}", plan_type="premium", description="d", price=Decimal("2500"),
        currency="SYP", duration_days=30, is_active=True, has_diet_access=True)


def _subscription(make_user, tag, *, ends_in):
    from subscription.models import Subscription

    user = make_user(f"sw_{tag}")
    sub = Subscription.objects.create(user=user, plan=_plan(tag), status="active",
                                      source="shamcash")
    end = timezone.now() + ends_in
    Subscription.objects.filter(pk=sub.pk).update(status="active", end_date=end,
                                                  start_date=end - timedelta(days=30))
    return Subscription.objects.get(pk=sub.pk)


def test_the_plan_lapse_scoped_and_not(make_user):
    from subscription.models import Subscription
    from subscription.tasks import expire_lapsed_subscriptions

    mine = _subscription(make_user, "lapse_a", ends_in=-timedelta(hours=2))
    theirs = _subscription(make_user, "lapse_b", ends_in=-timedelta(hours=2))
    assert expire_lapsed_subscriptions(subscription_ids=[mine.pk]) == 1
    assert Subscription.objects.get(pk=mine.pk).status == "expired"
    assert Subscription.objects.get(pk=theirs.pk).status == "active"
    assert expire_lapsed_subscriptions() >= 1                # the beat task's call
    assert Subscription.objects.get(pk=theirs.pk).status == "expired"


def test_the_plan_reminders_scoped_and_not(make_user, django_capture_on_commit_callbacks):
    from notifications.models import Notification
    from subscription.tasks import send_renewal_reminders

    mine = _subscription(make_user, "soon_a", ends_in=timedelta(days=2))
    theirs = _subscription(make_user, "soon_b", ends_in=timedelta(days=2))

    def told(sub):
        return Notification.objects.filter(recipient=sub.user,
                                           event_type="subscription_expiring").exists()

    with django_capture_on_commit_callbacks(execute=True):
        send_renewal_reminders(subscription_ids=[mine.pk])
    assert (told(mine), told(theirs)) == (True, False)
    with django_capture_on_commit_callbacks(execute=True):
        send_renewal_reminders()                             # the beat task's call
    assert told(theirs)
