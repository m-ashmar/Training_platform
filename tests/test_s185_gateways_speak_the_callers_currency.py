"""SIDE_BUGS S-185 (PAY-2): `GET v1/gateways/` offers a gateway only in a currency it can
charge.

It copied the registry's `supported_currencies` (['SYP', 'USD']) verbatim, although a
checkout charges only in a currency with configured bounds (`amount_limits`), and USD has
none until the owner sets them (S-052); and `can_pay` ignored the caller's currency. So a
USD buyer was offered a Pay whose checkout answered 503 `currency_not_chargeable`. Now
`supported_currencies` lists the chargeable currencies, `available` needs the caller's
own currency among them, and a gateway set up only for others leaves `can_pay` false with
`reason: "currency_not_chargeable"`.
"""
from decimal import Decimal

import pytest

from tests.test_c9_payments import shamcash  # noqa: F401  (the fixture)
from tests.test_road10_s213_wallet_plan import _person, _plan, _subscription

pytestmark = pytest.mark.django_db

APK = {"HTTP_X_APP_CHANNEL": "apk"}


def _listed(api, user):
    body = api(user).get("/api/subscription/v1/gateways/", **APK).json()
    (gateway,) = body["gateways"]
    return body, gateway


def test_a_syp_buyer_is_offered_the_gateway_in_syp_only(make_user, api, shamcash):
    body, gateway = _listed(api, _person(make_user, "s185_syp"))
    assert gateway["supported_currencies"] == ["SYP"]
    assert set(gateway["amount_limits"]) == {"SYP"}
    assert (gateway["available"], gateway["enabled"], gateway["kind"]) == \
        (True, True, "reviewed")
    assert (body["can_pay"], body["reason"]) == (True, None)


def test_a_usd_buyer_is_not_offered_a_pay_the_checkout_refuses(make_user, api, shamcash):
    buyer = _person(make_user, "s185_usd", currency="USD", funds="0.00")
    body, gateway = _listed(api, buyer)
    assert gateway["supported_currencies"] == ["SYP"]
    assert (gateway["available"], gateway["enabled"]) == (False, False)
    assert gateway["kind"] == "reviewed", "how it is set up does not change"
    assert (body["can_pay"], body["reason"]) == (False, "currency_not_chargeable")

    # ... which is what the checkout itself answers.
    sub = _subscription(buyer, _plan("s185 usd", price="10.00", currency="USD"))
    resp = api(buyer).post("/api/subscription/v1/gateways/",
                           {"subscription_id": str(sub.pk), "gateway": "shamcash"},
                           content_type="application/json", **APK)
    assert (resp.status_code, resp.json()["code"]) == (503, "currency_not_chargeable"), \
        resp.content


def test_once_usd_has_bounds_the_usd_buyer_is_offered_it(make_user, api, shamcash,
                                                          monkeypatch):
    from subscription.settings import gateway_config

    limits = dict(gateway_config.GATEWAY_REGISTRY["shamcash"]["amount_limits"])
    limits["USD"] = {"min": Decimal("1"), "max": Decimal("500")}
    monkeypatch.setitem(gateway_config.GATEWAY_REGISTRY["shamcash"], "amount_limits", limits)

    body, gateway = _listed(api, _person(make_user, "s185_bounds", currency="USD"))
    assert gateway["supported_currencies"] == ["SYP", "USD"]
    assert gateway["available"] is True
    assert (body["can_pay"], body["reason"]) == (True, None)


def test_with_no_gateway_set_up_the_reason_stays_no_gateway(make_user, api):
    body, gateway = _listed(api, _person(make_user, "s185_none", currency="USD"))
    assert (gateway["kind"], gateway["available"]) == (None, False)
    assert (body["can_pay"], body["reason"]) == (False, "no_gateway")
