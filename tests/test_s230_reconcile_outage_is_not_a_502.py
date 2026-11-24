"""SIDE_BUGS S-230 (PAY-2): what `payments/{id}/reconcile/` answers while ShamCash is down,
as its docstring and API_CONTRACT.md now say.

The docstring listed `502 gateway_unreachable` as "the provider could not be asked". But
the ShamCash adapter turns a lookup it could not make into the status `unknown`, which
raises nothing: an outage is a 200 with the payment's status unchanged and, once the
failures have lasted, `gateway_state: degraded` (the spec, section 6: nothing is refused
because of it). The 502 is a gateway whose adapter raises.
"""
from datetime import timedelta
from decimal import Decimal

import pytest
from django.utils import timezone

from tests.test_c9_payments import shamcash  # noqa: F401  (the fixture)
from tests.test_road10_s213_wallet_plan import _person, _plan, _subscription

pytestmark = pytest.mark.django_db

JSON = "application/json"


@pytest.fixture
def shamcash_down(shamcash, monkeypatch, settings):
    """ShamCash set up to be read, and every request to it failing."""
    import requests

    settings.PAYMENT_GATEWAY_DEGRADED_AFTER_FAILURES = 1
    settings.PAYMENT_GATEWAY_DEGRADED_AFTER_SECONDS = 0
    shamcash.update(api_key="s230-token", api_url="https://shamcash.test/v1")

    def down(*args, **kwargs):
        raise requests.ConnectionError("ShamCash is not answering")

    monkeypatch.setattr(requests, "get", down)
    monkeypatch.setattr(requests, "post", down)
    monkeypatch.setattr(requests.sessions.Session, "request", down)
    return shamcash


def _pending(make_user, tag):
    from subscription.models import Payment

    user = _person(make_user, tag)
    sub = _subscription(user, _plan(tag))
    payment = Payment.objects.create(subscription=sub, amount=Decimal("2500.00"),
                                     currency="SYP", payment_method="shamcash",
                                     status="pending",
                                     gateway_transaction_reference=f"SHAMCASH_1_{tag[:8]:0<8}")
    Payment.objects.filter(pk=payment.pk).update(
        created_at=timezone.now() - timedelta(minutes=5))
    return user, payment


def test_an_outage_is_a_200_with_the_payment_unchanged(make_user, api, shamcash_down):
    user, payment = _pending(make_user, "s230out")
    resp = api(user).post(f"/api/subscription/v1/payments/{payment.pk}/reconcile/",
                          content_type=JSON)
    assert resp.status_code == 200, resp.content
    body = resp.json()
    assert body["status"] == "pending"
    assert body["gateway_state"]["state"] == "degraded"
    payment.refresh_from_db()
    assert payment.status == "pending"


def test_the_502_is_an_adapter_that_raises(make_user, api, shamcash, monkeypatch):
    from subscription.gateways.shamcash import ShamCashGateway

    def broken(self, *args, **kwargs):
        raise RuntimeError("a programming fault in the adapter")

    monkeypatch.setattr(ShamCashGateway, "fetch_payment_status", broken)
    user, payment = _pending(make_user, "s230bad")
    resp = api(user).post(f"/api/subscription/v1/payments/{payment.pk}/reconcile/",
                          content_type=JSON)
    assert (resp.status_code, resp.json()["code"]) == (502, "gateway_unreachable")
    payment.refresh_from_db()
    assert payment.status == "pending"
