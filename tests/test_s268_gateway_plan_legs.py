"""SIDE_BUGS S-268 (PAY-1): no row of a plan paid through a gateway is reversed alone.

`POST wallet/admin/reversal/` refused the one row of a plan paid from the wallet
(`plan_payment_leg`, Road to 10 S2.13) but not the rows a GATEWAY payment writes
(`wallet.signals.handle_payment_completed`): the mint into the client's wallet, the
trainer's share and the platform's revenue, each tagged `original_payment_id` and none
`paid_with`. Reversing the revenue alone put the price back in the client's wallet while
the payment stayed `completed` and the plan active: a free plan, and a refund outside
`refund_payment`. Every such row is now `409 plan_payment_leg` with `payment_id`;
undoing the payment is its refund, which reverses every leg together.
"""
from decimal import Decimal

import pytest

from tests.test_road10_s213_wallet_plan import _person, _plan, _subscription

pytestmark = pytest.mark.django_db

JSON = "application/json"
REVERSAL = "/api/wallet/admin/reversal/"


def _paid_through_a_gateway(make_user, tag, capture):
    from subscription.models import Payment
    from subscription.services.payment_service import PaymentService

    user = _person(make_user, tag, funds="0.00")
    plan = _plan(tag, price="2500.00")
    sub = _subscription(user, plan)
    payment = Payment.objects.create(subscription=sub, amount=plan.price, currency="SYP",
                                     status="pending", payment_method="manual")
    with capture(execute=True):
        PaymentService.complete_payment(payment.id, {
            "amount": "2500.00", "currency": "SYP", "status": "completed",
            "event_id": f"{tag}-paid"})
    payment.refresh_from_db()
    assert payment.status == "completed" and payment.payout_settled_at is not None
    return user, sub, payment


def _legs(payment):
    from wallet.models import Transaction

    return {(row.tx_type, (row.metadata or {}).get("purpose")): row
            for row in Transaction.objects.filter(
                metadata__original_payment_id=str(payment.id))}


def _ledger(user):
    from wallet.models import Transaction, Wallet
    from wallet.utils import get_platform_revenue_wallet

    return (Transaction.objects.count(), Wallet.objects.get(owner=user).balance,
            get_platform_revenue_wallet("SYP").balance)


@pytest.mark.parametrize("leg", ["revenue", "mint"])
def test_no_row_of_a_gateway_plan_payment_is_reversed_alone(
        make_user, api, django_capture_on_commit_callbacks, leg):
    from subscription.models import Subscription

    user, sub, payment = _paid_through_a_gateway(
        make_user, f"s268_{leg}", django_capture_on_commit_callbacks)
    legs = _legs(payment)
    row = legs[("transfer", "subscription_revenue")] if leg == "revenue" \
        else legs[("topup", None)]
    before = _ledger(user)

    admin = make_user(f"s268_{leg}_admin", user_type="admin")
    resp = api(admin).post(REVERSAL, {"reference_id": row.reference_id,
                                      "idempotency_key": f"s268-{leg}", "reason": "test"},
                           content_type=JSON)
    assert (resp.status_code, resp.json()["code"], resp.json()["payment_id"]) == \
        (409, "plan_payment_leg", str(payment.id)), resp.content
    assert resp.json()["error"]
    assert _ledger(user) == before, "nothing moved"
    payment.refresh_from_db()
    assert payment.status == "completed"
    assert Subscription.objects.get(pk=sub.pk).status == "active"


def test_the_refusal_names_a_gateway_payment_in_arabic(
        make_user, api, django_capture_on_commit_callbacks):
    user, _sub, payment = _paid_through_a_gateway(
        make_user, "s268_ar", django_capture_on_commit_callbacks)
    row = _legs(payment)[("transfer", "subscription_revenue")]
    admin = make_user("s268_ar_admin", user_type="admin")
    resp = api(admin).post(REVERSAL, {"reference_id": row.reference_id,
                                      "idempotency_key": "s268-ar"},
                           content_type=JSON, HTTP_ACCEPT_LANGUAGE="ar")
    assert resp.json()["error"] == \
        "هذا جزء من دفعة خطة ولا يمكن عكسه وحده. استرد قيمة الدفعة بدلاً من ذلك."


def test_the_payment_is_still_undone_whole_by_its_refund(
        make_user, api, django_capture_on_commit_callbacks):
    """The way the refusal points to: the refund reverses every leg together."""
    from subscription.services.payment_service import PaymentService
    from wallet.models import Transaction, Wallet
    from wallet.utils import get_platform_revenue_wallet

    user, _sub, payment = _paid_through_a_gateway(
        make_user, "s268_refund", django_capture_on_commit_callbacks)
    platform_before = get_platform_revenue_wallet("SYP").balance
    with django_capture_on_commit_callbacks(execute=True):
        PaymentService.refund_payment(payment.id, reason="test")
    assert get_platform_revenue_wallet("SYP").balance == platform_before - Decimal("2500.00")
    assert Wallet.objects.get(owner=user).balance == Decimal("0.00")
    assert Transaction.objects.filter(
        tx_type="reversal", metadata__original_payment_id=str(payment.id)).count() == 2
