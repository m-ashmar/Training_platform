"""Road to 10 S2.13 under real concurrency (committed rows, real threads): a plan paid
from the wallet, double-tapped with one key, and two keys racing for a balance that
covers one.

In a `test_zz_` file because `django_db(transaction=True)` truncates every table when it
finishes; see the note at the top of tests/test_zz_concurrency.py. No row seeded by a
migration is relied on: the plan, the people and their wallets are made here, and the
platform's revenue wallet is made on first use.
"""
import threading
from datetime import timedelta
from decimal import Decimal
from unittest import mock

import pytest
from django.utils import timezone


def _setup(make_user, tag, funds):
    from subscription.models import Subscription, SubscriptionPlan
    from wallet.models import Wallet, set_currency_for

    plan = SubscriptionPlan.objects.create(
        name=f"s213z {tag}", plan_type="premium", description="d", price=Decimal("2500.00"),
        currency="SYP", duration_days=30, is_active=True)
    user = make_user(f"s213z_{tag}")
    set_currency_for(user, "SYP", "client")
    Wallet.objects.filter(owner=user).update(balance=Decimal(funds))
    sub = Subscription.objects.create(user=user, plan=plan, status="pending",
                                      start_date=timezone.now(),
                                      end_date=timezone.now() + timedelta(days=30))
    return user, sub


def _race(*calls):
    from django.db import connections

    barrier = threading.Barrier(len(calls))
    outcomes = {}

    def wrap(name, fn):
        def run():
            try:
                barrier.wait(timeout=30)
                outcomes[name] = fn()
            except Exception as exc:          # recorded, so the assertion shows it
                outcomes[name] = exc
            finally:
                connections.close_all()
        return run

    with mock.patch("notifications.domain.dispatcher.emit_event"), \
            mock.patch("notifications.channels.email.queue_receipt"):
        threads = [threading.Thread(target=wrap(name, fn)) for name, fn in calls]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=120)
    return outcomes


@pytest.mark.django_db(transaction=True)
def test_a_double_tap_with_one_key_pays_once(make_user):
    from subscription.models import Payment
    from subscription.services.payment_service import PaymentService
    from wallet.models import Transaction, Wallet

    user, sub = _setup(make_user, "tap", "10000.00")
    pay = lambda: PaymentService.pay_from_wallet(sub, idempotency_key="one-key")  # noqa: E731
    outcomes = _race(("a", pay), ("b", pay))
    assert all(isinstance(o, Payment) for o in outcomes.values()), outcomes
    assert outcomes["a"].pk == outcomes["b"].pk
    assert sorted(o.replayed for o in outcomes.values()) == [False, True]
    assert Payment.objects.filter(subscription=sub).count() == 1
    assert Transaction.objects.filter(
        metadata__original_payment_id=str(outcomes["a"].pk)).count() == 1
    assert Wallet.objects.get(owner=user).balance == Decimal("7500.00")


@pytest.mark.django_db(transaction=True)
def test_two_taps_with_fresh_keys_charge_once(make_user):
    """The balance covers two periods; two taps that each made a key race. The
    subscription lock serialises them and the second finds the first's payment: refused
    as a repeat, nothing charged."""
    from subscription.models import Payment
    from subscription.services.payment_service import AlreadyPaid, PaymentService
    from wallet.models import Wallet

    user, sub = _setup(make_user, "two", "5000.00")
    outcomes = _race(("a", lambda: PaymentService.pay_from_wallet(sub, idempotency_key="a")),
                     ("b", lambda: PaymentService.pay_from_wallet(sub, idempotency_key="b")))
    kinds = sorted(type(o).__name__ for o in outcomes.values())
    assert kinds == ["AlreadyPaid", "Payment"], outcomes
    paid = next(o for o in outcomes.values() if isinstance(o, Payment))
    repeat = next(o for o in outcomes.values() if isinstance(o, AlreadyPaid))
    assert repeat.payment.pk == paid.pk
    assert Payment.objects.filter(subscription=sub).count() == 1
    assert Wallet.objects.get(owner=user).balance == Decimal("2500.00")
