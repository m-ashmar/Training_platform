"""SIDE_BUGS S-228 and S-229 (PAY-2): two details of a ShamCash checkout.

S-228: the transfer instructions' `amount` was built as `str(float(Decimal))`, so Pay,
Renew and Upgrade answered "13000.0" there while the checkout's own `amount` read
"13000.00". The manager now passes the Decimal through and the adapter writes two
decimals.

S-229: the request validator refused a blank phone (400 `gateway_error`) though no
adapter reads the caller's contact details — the transfer is to the merchant account
with the reference in its note. A phone an operator blanked no longer stops a payment.
"""
import pytest

from tests.test_c9_payments import shamcash  # noqa: F401  (the fixture)
from tests.test_road10_s213_wallet_plan import _person, _plan, _subscription

pytestmark = pytest.mark.django_db

APK = {"HTTP_X_APP_CHANNEL": "apk"}


def _checkout(api, user, sub):
    return api(user).post("/api/subscription/v1/gateways/",
                          {"subscription_id": str(sub.pk), "gateway": "shamcash"},
                          content_type="application/json", **APK)


def test_the_instructions_amount_has_two_decimals(make_user, api, shamcash):
    user = _person(make_user, "s228")
    sub = _subscription(user, _plan("s228", price="13000.00"))
    resp = _checkout(api, user, sub)
    assert resp.status_code == 200, resp.content
    body = resp.json()
    assert body["instructions"]["amount"] == "13000.00", body["instructions"]
    assert body["instructions"]["amount"] == body["amount"]


def test_the_adapter_writes_two_decimals_whatever_it_is_handed():
    from decimal import Decimal

    from subscription.gateways.shamcash import ShamCashGateway

    gateway = ShamCashGateway({"merchant_id": "S228-ACCOUNT"})
    for handed in (Decimal("13000"), Decimal("13000.0"), 13000.0, "13000.5"):
        opened = gateway.initiate_payment({"reference": "SHAMCASH_1_abcdef12",
                                           "amount": handed, "currency": "SYP"})
        assert opened["instructions"]["amount"] in ("13000.00", "13000.50"), handed


def test_a_blank_phone_does_not_stop_a_payment(make_user, api, shamcash):
    from django.contrib.auth import get_user_model

    user = _person(make_user, "s229")
    get_user_model().objects.filter(pk=user.pk).update(phone_number="")
    user.refresh_from_db()
    sub = _subscription(user, _plan("s229", price="5000.00"))
    resp = _checkout(api, user, sub)
    assert resp.status_code == 200, resp.content
    assert resp.json()["instructions"]["account_id"] == "C9-ACCOUNT-1"
