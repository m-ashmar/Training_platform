"""SIDE_BUGS S-180 (PAY-2): a currency switch cannot slip between a checkout's pricing
and its commit.

`_checkout_under_lock` priced the checkout from an unlocked read of the buyer's wallet
currency, and `set_currency_for` refuses a switch while a checkout is open — but only one
that has committed. A `PUT /api/wallet/currency/` landing in between left a payment
priced in the old currency against a wallet in the new one. The checkout now reads the
currency FOR UPDATE while it can still change (`wallet.models.checkout_currency_for`):
a switch either lands first and the checkout is priced in it, or waits and is refused.

In a `test_zz_` file: real threads on committed rows (`transaction=True` truncates every
table when it ends; see tests/test_zz_concurrency.py). Nothing seeded by a migration is
relied on.
"""
import threading
import time
from datetime import timedelta
from decimal import Decimal

import pytest
from django.utils import timezone

from tests.test_c9_payments import shamcash  # noqa: F401  (the fixture)

APK = {"HTTP_X_APP_CHANNEL": "apk"}


def _buyer(make_user, tag):
    from subscription.models import Subscription, SubscriptionPlan
    from wallet.models import Wallet, set_currency_for, wallet_is_untouched

    user = make_user(f"s180_{tag}")
    set_currency_for(user, "SYP", "client")
    assert wallet_is_untouched(Wallet.objects.get(owner=user)), "its currency can change"
    plan = SubscriptionPlan.objects.create(
        name=f"s180 {tag}", plan_type="premium", description="d", price=Decimal("130000.00"),
        currency="SYP", duration_days=30, is_active=True)
    sub = Subscription.objects.create(user=user, plan=plan, status="pending",
                                      start_date=timezone.now())
    return user, sub


@pytest.mark.django_db(transaction=True)
def test_a_switch_that_lands_first_prices_the_checkout(make_user, api, shamcash,
                                                       monkeypatch):
    """The switch holds the wallet row when the checkout reaches it: the checkout waits,
    then prices in the new currency — never a payment in the old one."""
    from django.db import connections, transaction

    from subscription.models import Payment
    from subscription.settings import gateway_config
    from wallet.models import ExchangeRate, Wallet, set_currency_for

    limits = dict(gateway_config.GATEWAY_REGISTRY["shamcash"]["amount_limits"])
    # 130,000 SYP is 10.00 USD at the rate below: inside these bounds.
    limits["USD"] = {"min": Decimal("1"), "max": Decimal("5000")}
    monkeypatch.setitem(gateway_config.GATEWAY_REGISTRY["shamcash"], "amount_limits", limits)
    ExchangeRate.objects.create(base="USD", quote="SYP", rate=Decimal("13000.000000"),
                                effective_from=timezone.now() - timedelta(hours=1),
                                note="s180")
    user, sub = _buyer(make_user, "first")
    started, outcome = threading.Event(), {}

    def switch():
        try:
            with transaction.atomic():
                set_currency_for(user, "USD")
                started.set()
                time.sleep(1.5)
        except Exception as exc:                     # recorded for the assertion
            outcome["switch"] = exc
        finally:
            connections.close_all()

    holder = threading.Thread(target=switch)
    holder.start()
    assert started.wait(timeout=30)
    try:
        resp = api(user).post("/api/subscription/v1/gateways/",
                              {"subscription_id": str(sub.pk), "gateway": "shamcash"},
                              content_type="application/json", **APK)
    finally:
        holder.join(timeout=60)
    assert "switch" not in outcome, outcome
    assert resp.status_code == 200, resp.content
    wallet = Wallet.objects.get(owner=user)
    (payment,) = Payment.objects.filter(subscription=sub)
    assert (wallet.currency, payment.currency) == ("USD", "USD")


@pytest.mark.django_db(transaction=True)
def test_a_switch_that_comes_second_waits_and_is_refused(make_user):
    """The checkout holds the wallet row while it opens its payment: the switch waits,
    then finds the checkout open and is refused (`CurrencyChangeBlocked`)."""
    from django.db import connections, transaction

    from subscription.models import Payment
    from wallet.models import (CurrencyChangeBlocked, Wallet, checkout_currency_for,
                               set_currency_for)

    user, sub = _buyer(make_user, "second")
    outcome = {}

    def switch():
        try:
            outcome["switch"] = set_currency_for(user, "USD")
        except Exception as exc:
            outcome["switch"] = exc
        finally:
            connections.close_all()

    switcher = threading.Thread(target=switch)
    with transaction.atomic():
        currency = checkout_currency_for(user)       # what the checkout prices in
        switcher.start()
        time.sleep(1.0)                              # the switch is waiting on the row
        assert "switch" not in outcome, "the switch did not wait for the checkout"
        Payment.objects.create(subscription=sub, amount=Decimal("130000.00"),
                               currency=currency, status="pending",
                               payment_method="shamcash")
    switcher.join(timeout=60)
    assert isinstance(outcome["switch"], CurrencyChangeBlocked), outcome
    assert Wallet.objects.get(owner=user).currency == "SYP" == currency
