"""An upgrade paid from the wallet under real concurrency (committed rows, real threads):
a double tap with one key, and two keys racing for the same upgrade.

In a `test_zz_` file because `django_db(transaction=True)` truncates every table when it
finishes; see the note at the top of tests/test_zz_concurrency.py. No row seeded by a
migration is relied on: the plans, the person and the wallet are made here, and the
platform's revenue wallet is made on first use.
"""
from datetime import timedelta
from decimal import Decimal
from unittest import mock

import pytest
from django.utils import timezone

from tests.test_zz_road10_s213_wallet_plan import _race


def _setup(make_user, tag):
    from subscription.models import Subscription, SubscriptionPlan
    from subscription.services.payment_service import PaymentService
    from wallet.models import Wallet, set_currency_for

    basic = SubscriptionPlan.objects.create(
        name=f"wuz {tag} basic", plan_type="premium", description="d",
        price=Decimal("10000.00"), currency="SYP", duration_days=30, is_active=True)
    premium = SubscriptionPlan.objects.create(
        name=f"wuz {tag} premium", plan_type="premium", description="d",
        price=Decimal("20000.00"), currency="SYP", duration_days=30, is_active=True)
    user = make_user(f"wuz_{tag}")
    set_currency_for(user, "SYP", "client")
    Wallet.objects.filter(owner=user).update(balance=Decimal("50000.00"))
    sub = Subscription.objects.create(user=user, plan=basic, status="pending",
                                      start_date=timezone.now(),
                                      end_date=timezone.now() + timedelta(days=30))
    PaymentService.pay_from_wallet(sub, idempotency_key=f"wuz-{tag}-basic")
    Subscription.objects.filter(pk=sub.pk).update(
        end_date=timezone.now() + timedelta(days=20, hours=1))
    return user, Subscription.objects.get(pk=sub.pk), basic, premium


def _upgrade(sub, premium, basic, key):
    from subscription.services.payment_service import PaymentService

    return lambda: PaymentService.pay_upgrade_from_wallet(
        sub, premium, idempotency_key=key, quote=(Decimal("13333.33"), "SYP"),
        expected_from_plan_id=basic.pk)


@pytest.mark.django_db(transaction=True)
def test_a_double_tap_with_one_key_pays_once(make_user):
    from subscription.models import Payment, Subscription
    from wallet.models import Transaction, Wallet

    user, sub, basic, premium = _setup(make_user, "tap")
    pay = _upgrade(sub, premium, basic, "one-key")
    outcomes = _race(("a", pay), ("b", pay))
    assert all(isinstance(o, Payment) for o in outcomes.values()), outcomes
    assert outcomes["a"].pk == outcomes["b"].pk
    assert sorted(o.replayed for o in outcomes.values()) == [False, True]
    assert Payment.objects.filter(subscription=sub, metadata__kind="upgrade").count() == 1
    assert Transaction.objects.filter(
        metadata__original_payment_id=str(outcomes["a"].pk)).count() == 1
    assert Wallet.objects.get(owner=user).balance == Decimal("50000.00") - Decimal(
        "10000.00") - Decimal("13333.33")
    assert Subscription.objects.get(pk=sub.pk).plan_id == premium.pk


@pytest.mark.django_db(transaction=True)
def test_two_taps_with_fresh_keys_charge_once(make_user):
    """Both read the subscription on Basic; the subscription lock serialises them, the
    first switches the plan and the second finds it moved: refused, nothing charged."""
    from subscription.models import Payment
    from subscription.services.payment_service import PlanChanged
    from wallet.models import Wallet

    user, sub, basic, premium = _setup(make_user, "two")
    outcomes = _race(("a", _upgrade(sub, premium, basic, "a")),
                     ("b", _upgrade(sub, premium, basic, "b")))
    kinds = sorted(type(o).__name__ for o in outcomes.values())
    assert kinds == ["Payment", "PlanChanged"], outcomes
    assert any(isinstance(o, PlanChanged) for o in outcomes.values())
    assert Payment.objects.filter(subscription=sub, metadata__kind="upgrade").count() == 1
    assert Wallet.objects.get(owner=user).balance == Decimal("50000.00") - Decimal(
        "10000.00") - Decimal("13333.33")


def _hold_and_switch(sub, plan, started, hold=1.5):
    """Lock the subscription, switch its plan, keep the lock `hold` seconds, commit: what
    an upgrade does to a lock that waits on it."""
    import time

    from django.db import connections, transaction

    from subscription.models import Subscription

    def run():
        try:
            with transaction.atomic():
                Subscription.objects.select_for_update().get(pk=sub.pk)
                Subscription.objects.filter(pk=sub.pk).update(plan=plan)
                started.set()
                time.sleep(hold)
        finally:
            connections.close_all()
    return run


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("site", ["start_renewal", "lock_for_completion", "change_plan",
                                  "wallet_renewal"])
def test_a_lock_that_waited_on_a_plan_switch_still_finds_the_subscription(make_user, site):
    """Every lock of a subscription waited on an upgrade and woke to `DoesNotExist`: with
    the plan joined, PostgreSQL re-checks the row against the plan read before the wait,
    and the switched plan fails the join. Locked alone, it finds the row and its new plan."""
    import threading

    from django.db import transaction

    from subscription.models import Payment
    from subscription.services.payment_service import PaymentService
    from subscription.views import _locked_subscription

    user, sub, basic, premium = _setup(make_user, f"lock_{site}")
    first = Payment.objects.get(subscription=sub, status="completed")
    # Outside the wallet's ten-minute repeat window, so the renewal is not refused as a
    # second tap (that refusal is not what this test asks about).
    Payment.objects.filter(pk=first.pk).update(
        completed_at=timezone.now() - timedelta(minutes=11))

    def wake():
        if site == "start_renewal":
            return PaymentService.start_renewal(sub).subscription
        if site == "lock_for_completion":
            with transaction.atomic():
                return PaymentService._lock_subscription_and_payment(first.pk)[0]
        if site == "change_plan":
            with transaction.atomic():
                return _locked_subscription(sub.pk)
        return PaymentService.pay_from_wallet(sub, idempotency_key="after-switch").subscription

    started = threading.Event()
    holder = threading.Thread(target=_hold_and_switch(sub, premium, started))
    with mock.patch("notifications.domain.dispatcher.emit_event"), \
            mock.patch("notifications.channels.email.queue_receipt"):
        holder.start()
        assert started.wait(timeout=30)
        try:
            woke = wake()
        finally:
            holder.join(timeout=60)
    assert woke.pk == sub.pk and woke.plan_id == premium.pk
