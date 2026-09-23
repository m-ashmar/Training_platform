"""BACKEND_BUGS B-047..B-052: what the app session found wiring the money screens
(2026-09-23), one test per row the backend closed. B-051 (a needs-review payment state)
is plan step 5's checkout-session work and has no test here.

B-047 is the only one that moves money: a subscription can now change plan, so every
checkout records the plan it was priced for, and a late transfer against an expired
checkout grants what it paid for rather than whatever plan the row is on by then.
"""
import json
from datetime import timedelta
from decimal import Decimal

import pytest
from django.utils import timezone

from tests.test_fx_subs import _checkout, _person, _post, _subscribe, shamcash  # noqa: F401

pytestmark = pytest.mark.django_db

D = Decimal
SUBS = "/api/subscription/v1/subscriptions/"


def _plan(tag, price, days=30, **flags):
    from django.utils import translation

    from subscription.models import SubscriptionPlan

    with translation.override("en"):
        return SubscriptionPlan.objects.create(
            name=f"b047 {tag}", plan_type="premium", description="b047",
            price=D(price), currency="SYP", duration_days=days, is_active=True, **flags)


def _change(c, sub, plan_id, **extra):
    return _post(c, f"{SUBS}{sub.id}/change-plan/", {"plan_id": plan_id}, **extra)


def _complete(payment, django_capture_on_commit_callbacks):
    from subscription.services.payment_service import PaymentService

    with django_capture_on_commit_callbacks(execute=True):
        return PaymentService.complete_payment(payment.id, {
            "amount": str(payment.amount), "currency": payment.currency,
            "status": "completed", "transaction_id": f"tx-{payment.id}"})


def _lapse(sub):
    from subscription.models import Subscription

    now = timezone.now()
    Subscription.objects.filter(pk=sub.pk).update(
        start_date=now - timedelta(days=31), end_date=now - timedelta(days=1))


# ------------------------------------------------------------------------ B-047
def test_a_pending_subscription_moves_to_the_plan_the_customer_wants(
        make_user, api, shamcash):
    """The row made on the wrong plan was a dead end: a second POST is refused and
    renew only re-charges the plan the row has. The move charges nothing; the Pay
    button then prices the NEW plan, and the row takes the new plan's features."""
    from subscription.models import Payment

    basic = _plan("basic", "10000.00", has_ai_advice=False)
    premium = _plan("premium", "20000.00", has_ai_advice=True)
    user = _person(make_user, "b047_move")
    c, sub = _subscribe(api, user, basic)

    resp = _change(c, sub, premium.id)
    assert resp.status_code == 200, resp.content
    body = resp.json()
    assert body["changed"] is True
    assert body["subscription"]["plan"]["id"] == premium.id
    assert body["subscription"]["has_ai_advice"] is True, "the new plan's features"
    assert not Payment.objects.filter(subscription=sub).exists(), "nothing was charged"

    pay = _checkout(c, sub)
    assert pay.status_code == 200, pay.content
    assert (pay.json()["amount"], pay.json()["currency"]) == ("20000.00", "SYP")

    back = _change(c, sub, basic.id)
    assert back.status_code == 409 and back.json()["code"] == "checkout_open", \
        "changing again while the Premium checkout is open"


def test_the_same_plan_is_a_no_op_and_an_unknown_one_is_refused(make_user, api):
    basic = _plan("same", "10000.00")
    user = _person(make_user, "b047_same")
    c, sub = _subscribe(api, user, basic)

    same = _change(c, sub, basic.id)
    assert same.status_code == 200 and same.json()["changed"] is False

    for bad in (987654321, "x", None):
        resp = _change(c, sub, bad)
        assert (resp.status_code, resp.json()["code"]) == (400, "invalid_plan"), bad

    retired = _plan("retired", "5000.00")
    retired.is_active = False
    retired.save()
    resp = _change(c, sub, retired.id)
    assert (resp.status_code, resp.json()["code"]) == (400, "invalid_plan")


def test_a_store_build_may_not_change_to_a_paid_plan(make_user, api):
    """Choosing a paid plan is the first step of buying it (`create`'s rule)."""
    from subscription.models import Subscription

    basic = _plan("store basic", "10000.00")
    premium = _plan("store premium", "20000.00")
    user = _person(make_user, "b047_store")
    c, sub = _subscribe(api, user, basic)

    for channel in ("play", "ios"):
        resp = _change(c, sub, premium.id, HTTP_X_APP_CHANNEL=channel)
        assert resp.status_code == 403, (channel, resp.content)
        assert resp.json()["code"] == "not_available_on_channel"
    assert Subscription.objects.get(pk=sub.pk).plan_id == basic.id


def test_running_cover_is_not_changed(make_user, api, shamcash,
                                      django_capture_on_commit_callbacks):
    """How a mid-period upgrade is priced is the owner's decision; until then it is
    refused, and nothing on the row moves."""
    from subscription.models import Payment, Subscription

    basic = _plan("active basic", "10000.00")
    premium = _plan("active premium", "20000.00")
    user = _person(make_user, "b047_active")
    c, sub = _subscribe(api, user, basic)
    assert _checkout(c, sub).status_code == 200
    _complete(Payment.objects.get(subscription=sub), django_capture_on_commit_callbacks)

    resp = _change(c, sub, premium.id)
    assert (resp.status_code, resp.json()["code"]) == (409, "plan_change_while_active")
    assert Subscription.objects.get(pk=sub.pk).plan_id == basic.id

    _lapse(sub)
    assert _change(c, sub, premium.id).status_code == 200, "once the period has ended"


def test_an_open_checkout_blocks_the_change_and_says_when_it_closes(
        make_user, api, shamcash):
    """The customer may be transferring against the reference right now; a transfer
    that cleared after the change would buy the new plan at the old price."""
    from subscription.models import Payment, Subscription

    basic = _plan("open basic", "10000.00")
    premium = _plan("open premium", "20000.00")
    user = _person(make_user, "b047_open")
    c, sub = _subscribe(api, user, basic)
    assert _checkout(c, sub).status_code == 200
    payment = Payment.objects.get(subscription=sub)

    resp = _change(c, sub, premium.id)
    assert resp.status_code == 409, resp.content
    body = resp.json()
    assert body["code"] == "checkout_open"
    assert body["payment_id"] == str(payment.id)
    assert body["expires_at"], "the app shows when it can try again"
    assert Subscription.objects.get(pk=sub.pk).plan_id == basic.id


def test_earlier_payments_keep_naming_the_plan_they_bought(
        make_user, api, shamcash, django_capture_on_commit_callbacks):
    """History and receipts read the plan through the subscription, so a plan change
    would have renamed every earlier payment to the new plan."""
    from subscription.models import Payment

    basic = _plan("history basic", "10000.00", days=30)
    premium = _plan("history premium", "20000.00", days=60)
    user = _person(make_user, "b047_history")
    c, sub = _subscribe(api, user, basic)
    assert _checkout(c, sub).status_code == 200
    paid = Payment.objects.get(subscription=sub)
    _complete(paid, django_capture_on_commit_callbacks)
    _lapse(sub)
    assert _change(c, sub, premium.id).status_code == 200

    rows = c.get("/api/subscription/v1/payments/").json()
    rows = rows.get("results", rows) if isinstance(rows, dict) else rows
    row = next(r for r in rows if r["id"] == str(paid.id))
    assert (row["item"]["plan_id"], row["item"]["plan_name"],
            row["item"]["duration_days"]) == (basic.id, basic.name, 30)

    receipt = c.get(f"/api/subscription/v1/payments/{paid.id}/receipt/").json()["receipt"]
    assert receipt["item"]["plan_name"] == basic.name
    assert receipt["plan_duration_days"] == 30


def test_a_late_transfer_buys_the_plan_it_paid_for(
        make_user, api, shamcash, django_capture_on_commit_callbacks):
    """`expired -> completed` is legal: the money is real. A Basic checkout that expired,
    then a change to Premium, then the Basic transfer arriving: activation read the
    row's plan, so Basic's price bought Premium's features and Premium's length."""
    from subscription.models import Payment, Subscription
    from subscription.services.payment_service import PaymentService

    basic = _plan("late basic", "10000.00", days=10, has_ai_advice=False)
    premium = _plan("late premium", "20000.00", days=60, has_ai_advice=True)
    user = _person(make_user, "b047_late")
    c, sub = _subscribe(api, user, basic)
    assert _checkout(c, sub).status_code == 200
    late = Payment.objects.get(subscription=sub)
    PaymentService.expire_payment(late.id, "window closed")
    assert _change(c, sub, premium.id).status_code == 200

    _complete(Payment.objects.get(pk=late.pk), django_capture_on_commit_callbacks)

    sub = Subscription.objects.get(pk=sub.pk)
    late.refresh_from_db()
    assert sub.plan_id == basic.id, "the money bought Basic"
    assert sub.has_ai_advice is False, "and Basic's features"
    assert late.covers_until - late.covers_from == timedelta(days=10)


def test_a_late_cheaper_transfer_does_not_downgrade_paid_cover(
        make_user, api, shamcash, django_capture_on_commit_callbacks):
    """The customer paid Premium after the change, then the old Basic transfer cleared.
    Moving them to Basic would take what they paid for; they keep Premium and the Basic
    period is added."""
    from subscription.models import Payment, Subscription
    from subscription.services.payment_service import PaymentService

    basic = _plan("paid basic", "10000.00", days=10)
    premium = _plan("paid premium", "20000.00", days=60)
    user = _person(make_user, "b047_paid")
    c, sub = _subscribe(api, user, basic)
    assert _checkout(c, sub).status_code == 200
    late = Payment.objects.get(subscription=sub)
    PaymentService.expire_payment(late.id, "window closed")
    assert _change(c, sub, premium.id).status_code == 200
    assert _checkout(c, sub).status_code == 200
    _complete(Payment.objects.get(subscription=sub, status="pending"),
              django_capture_on_commit_callbacks)
    before = Subscription.objects.get(pk=sub.pk).end_date

    _complete(Payment.objects.get(pk=late.pk), django_capture_on_commit_callbacks)

    sub = Subscription.objects.get(pk=sub.pk)
    assert sub.plan_id == premium.id
    assert sub.end_date - before == timedelta(days=10), "Basic's period, after Premium's"


def test_the_change_plan_contract_lists_every_code():
    from subscription.views import SubscriptionViewSet

    doc = SubscriptionViewSet.change_plan.__doc__
    for code in ("app_channel_required", "not_available_on_channel", "not_found",
                 "invalid_plan", "plan_change_while_active", "checkout_open"):
        assert f"`{code}`" in doc, code


# ------------------------------------------------------------------------ B-048
def test_status_and_reconcile_refusals_carry_a_code(make_user, api, shamcash, monkeypatch):
    import uuid

    from subscription.models import Payment
    from subscription.services.payment_gateways import (
        PaymentGatewayError, PaymentGatewayService)

    plan = _plan("b048", "10000.00")
    user = _person(make_user, "b048")
    c, sub = _subscribe(api, user, plan)

    missing = uuid.uuid4()
    resp = c.get(f"/api/subscription/v1/payments/{missing}/status/")
    assert (resp.status_code, resp.json()["code"]) == (404, "not_found")
    resp = _post(c, f"/api/subscription/v1/payments/{missing}/reconcile/")
    assert (resp.status_code, resp.json()["code"]) == (404, "not_found")

    bare = Payment.objects.create(subscription=sub, amount=plan.price, currency="SYP",
                                  status="pending")
    resp = _post(c, f"/api/subscription/v1/payments/{bare.id}/reconcile/")
    assert (resp.status_code, resp.json()["code"]) == (400, "no_gateway_reference")
    bare.delete()

    assert _checkout(c, sub).status_code == 200
    open_one = Payment.objects.get(subscription=sub)

    def unreachable(self, *args, **kwargs):
        raise PaymentGatewayError("provider timed out")

    monkeypatch.setattr(PaymentGatewayService, "fetch_payment_status", unreachable)
    resp = _post(c, f"/api/subscription/v1/payments/{open_one.id}/reconcile/")
    assert (resp.status_code, resp.json()["code"]) == (502, "gateway_unreachable")

    monkeypatch.setattr(PaymentGatewayService, "fetch_payment_status",
                        lambda self, *a, **k: {"status": "completed", "amount": "1.00",
                                               "currency": "SYP"})
    resp = _post(c, f"/api/subscription/v1/payments/{open_one.id}/reconcile/")
    assert (resp.status_code, resp.json()["code"]) == (400, "reconcile_rejected")
    open_one.refresh_from_db()
    assert open_one.status == "pending", "a mismatched amount changed nothing"


# ------------------------------------------------------------------------ B-049
def test_a_pdf_receipt_refusal_is_json_with_its_code(make_user, api):
    import uuid

    from subscription.models import Payment

    plan = _plan("b049", "10000.00")
    user = _person(make_user, "b049")
    c, sub = _subscribe(api, user, plan)
    unpaid = Payment.objects.create(subscription=sub, amount=plan.price, currency="SYP",
                                    status="pending")

    for pid, code in ((unpaid.id, "receipt_not_available"), (uuid.uuid4(), "not_found")):
        resp = c.get(f"/api/subscription/v1/payments/{pid}/receipt/?format=pdf")
        assert resp.status_code == 404
        assert resp["Content-Type"].startswith("application/json"), resp["Content-Type"]
        assert json.loads(resp.content)["code"] == code, resp.content


# ------------------------------------------------------------------------ B-050
def test_the_current_summary_names_the_plan_by_id(make_user, api):
    plan = _plan("b050", "10000.00")
    user = _person(make_user, "b050")
    c, sub = _subscribe(api, user, plan)

    body = c.get(f"{SUBS}current/").json()
    summary = body.get("subscription", body)
    assert summary["plan_id"] == plan.id, body


# ------------------------------------------------------------------------ B-052
def test_renew_says_when_its_checkout_closes(make_user, api, shamcash,
                                             django_capture_on_commit_callbacks):
    from subscription.models import Payment

    plan = _plan("b052", "10000.00")
    user = _person(make_user, "b052")
    c, sub = _subscribe(api, user, plan)
    assert _checkout(c, sub).status_code == 200
    _complete(Payment.objects.get(subscription=sub), django_capture_on_commit_callbacks)

    resp = _post(c, f"{SUBS}{sub.id}/renew/", {"gateway": "shamcash"})
    assert resp.status_code == 200, resp.content
    assert resp.json()["expires_at"], "the Pay screen's countdown"
