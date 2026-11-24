"""What the adversarial review of B-047 (change-plan) found, 2026-09-23.

Three reviewers read the merged money-screen fixes (9a79874) through different lenses and
one verifier reproduced every finding it could not refute. Each test here is one of those
reproductions — it failed against 9a79874 for the reason its finding gives — except the
two at the end, which pin the other half of a rule the fixes introduced. They are named
for the finding (F1..F16); the fixes are described where they live.
"""
import json
from datetime import timedelta
from decimal import Decimal

import pytest
from django.utils import timezone

from tests.test_backend_bugs_app_0923b import _change, _complete, _lapse, _plan  # noqa: F401
from tests.test_fx_subs import (  # noqa: F401
    _checkout, _person, _post, _rate, _subscribe, dollars_chargeable, shamcash)

pytestmark = pytest.mark.django_db

D = Decimal
SUBS = "/api/subscription/v1/subscriptions/"


def _rows(resp):
    rows = resp.json()
    return rows.get("results", rows) if isinstance(rows, dict) else rows


# ---------------------------------------------------------------- F1 / F7 / F14
def test_f1_change_plan_prices_the_plan_in_the_readers_currency(make_user, api):
    _rate("13000")
    basic = _plan("rv f1 basic", "10000.00")
    premium = _plan("rv f1 premium", "20000.00")
    user = _person(make_user, "rv_f1", currency="USD")
    c, sub = _subscribe(api, user, basic)

    resp = _change(c, sub, premium.id)
    assert resp.status_code == 200, resp.content
    changed = resp.json()["subscription"]["plan"]
    got = c.get(f"{SUBS}{sub.id}/").json()["plan"]
    assert got["currency"] == "USD"
    assert (changed["currency"], changed["price"]) == (got["currency"], got["price"]), \
        (changed, got)


def test_f1_change_plan_on_a_store_build_withholds_the_price(make_user, api):
    basic = _plan("rv f1s basic", "10000.00")
    free = _plan("rv f1s free", "0.00")
    user = _person(make_user, "rv_f1s")
    c, sub = _subscribe(api, user, basic)

    resp = _change(c, sub, free.id, HTTP_X_APP_CHANNEL="play")
    assert resp.status_code == 200, resp.content
    plan = resp.json()["subscription"]["plan"]
    got = c.get(f"{SUBS}{sub.id}/", HTTP_X_APP_CHANNEL="play").json()["plan"]
    assert "price" not in got, got
    assert "price" not in plan, plan


# ---------------------------------------------------------------- F2 / F17
def test_f2_refunding_the_premium_payment_leaves_basic_cover_on_basic(
        make_user, api, shamcash, django_capture_on_commit_callbacks):
    from subscription.models import Payment, Subscription
    from subscription.services.payment_service import PaymentService

    basic = _plan("rv f2 basic", "10000.00", days=10)
    premium = _plan("rv f2 premium", "20000.00", days=60)
    user = _person(make_user, "rv_f2")
    c, sub = _subscribe(api, user, basic)
    assert _checkout(c, sub).status_code == 200
    late = Payment.objects.get(subscription=sub)
    PaymentService.expire_payment(late.id, "window closed")
    assert _change(c, sub, premium.id).status_code == 200
    assert _checkout(c, sub).status_code == 200
    p2 = Payment.objects.get(subscription=sub, status="pending")
    _complete(p2, django_capture_on_commit_callbacks)
    _complete(Payment.objects.get(pk=late.pk), django_capture_on_commit_callbacks)
    assert Subscription.objects.get(pk=sub.pk).plan_id == premium.id

    with django_capture_on_commit_callbacks(execute=True):
        PaymentService.refund_payment(p2.id, "chargeback")

    sub = Subscription.objects.get(pk=sub.pk)
    assert sub.is_active, "the Basic period is still paid for"
    assert sub.plan_id == basic.id, "only Basic's money is left"


# ---------------------------------------------------------------- F3
def test_f3_a_late_cheaper_transfer_after_a_currency_switch_keeps_premium(
        make_user, api, shamcash, dollars_chargeable, django_capture_on_commit_callbacks):
    from subscription.models import Payment, Subscription
    from subscription.services.payment_service import PaymentService
    from wallet.models import set_currency_for

    _rate("13000")
    basic = _plan("rv f3 basic", "10000.00", days=10)
    premium = _plan("rv f3 premium", "20000.00", days=60)
    user = _person(make_user, "rv_f3")
    c, sub = _subscribe(api, user, basic)
    assert _checkout(c, sub).status_code == 200
    late = Payment.objects.get(subscription=sub)
    assert late.currency == "SYP"
    PaymentService.expire_payment(late.id, "window closed")

    set_currency_for(user, "USD", "client")   # expired checkout is not money on its way
    assert _change(c, sub, premium.id).status_code == 200
    pay = _checkout(c, sub)
    assert pay.status_code == 200, pay.content
    p2 = Payment.objects.get(subscription=sub, status="pending")
    assert p2.currency == "USD"
    _complete(p2, django_capture_on_commit_callbacks)
    assert Subscription.objects.get(pk=sub.pk).plan_id == premium.id

    _complete(Payment.objects.get(pk=late.pk), django_capture_on_commit_callbacks)

    sub = Subscription.objects.get(pk=sub.pk)
    assert sub.plan_id == premium.id, "a 10000 SYP Basic transfer downgraded paid Premium"


# ---------------------------------------------------------------- F4 / F11 / F15
def test_f4_history_and_receipt_name_the_plan_in_the_readers_language(
        make_user, api, shamcash, django_capture_on_commit_callbacks):
    from subscription.models import Payment

    plan = _plan("rv f4", "10000.00")
    plan.name_ar = "خطة مميزة"
    plan.save()
    user = _person(make_user, "rv_f4")
    c, sub = _subscribe(api, user, plan)
    assert _checkout(c, sub, HTTP_ACCEPT_LANGUAGE="en").status_code == 200
    paid = Payment.objects.get(subscription=sub)
    _complete(paid, django_capture_on_commit_callbacks)

    row = next(r for r in _rows(c.get("/api/subscription/v1/payments/",
                                      HTTP_ACCEPT_LANGUAGE="ar"))
               if r["id"] == str(paid.id))
    assert row["item"]["plan_name"] == "خطة مميزة", row["item"]
    receipt = c.get(f"/api/subscription/v1/payments/{paid.id}/receipt/",
                    HTTP_ACCEPT_LANGUAGE="ar").json()["receipt"]
    assert receipt["item"]["plan_name"] == "خطة مميزة", receipt["item"]


# ---------------------------------------------------------------- F5 / F12 / F18
def test_f5_a_payment_keeps_naming_its_plan_after_a_cancel_and_a_move(
        make_user, api, shamcash, django_capture_on_commit_callbacks):
    from subscription.models import Payment

    basic = _plan("rv f5 basic", "10000.00")
    premium = _plan("rv f5 premium", "20000.00")
    user = _person(make_user, "rv_f5")
    c, sub = _subscribe(api, user, basic)
    assert _checkout(c, sub).status_code == 200
    _complete(Payment.objects.get(subscription=sub), django_capture_on_commit_callbacks)

    resp = _post(c, f"{SUBS}{sub.id}/cancel/", {"reason": "moving", "immediate": True})
    assert resp.status_code == 200, resp.content
    assert _change(c, sub, premium.id).status_code == 200

    # CONTRACT CHANGE (SIDE_BUGS S-184, S-252; Road to 10 PAY-2, 2026-11-24): a
    # cancellation writes no payment row any more. It was a zero-amount 'cancelled'
    # Payment written outside PaymentService, which the app filtered out of its history.
    # What F5 protects holds for the rows that remain: the payment made on Basic keeps
    # naming Basic after the subscription moved to Premium.
    assert not Payment.objects.filter(subscription=sub, status="cancelled").exists()
    paid = Payment.objects.get(subscription=sub, status="completed")
    row = next(r for r in _rows(c.get("/api/subscription/v1/payments/"))
               if r["id"] == str(paid.id))
    assert row["item"]["plan_id"] == basic.id, row["item"]


# ---------------------------------------------------------------- F6 / F19
def test_f6_the_period_granted_is_the_period_receipted(
        make_user, api, shamcash, django_capture_on_commit_callbacks):
    from subscription.models import Payment

    plan = _plan("rv f6", "10000.00", days=30)
    user = _person(make_user, "rv_f6")
    c, sub = _subscribe(api, user, plan)
    assert _checkout(c, sub).status_code == 200
    payment = Payment.objects.get(subscription=sub)
    plan.duration_days = 15            # operator edit while the transfer is in flight
    plan.save()
    _complete(payment, django_capture_on_commit_callbacks)

    payment.refresh_from_db()
    granted = (payment.covers_until - payment.covers_from).days
    receipt = c.get(f"/api/subscription/v1/payments/{payment.id}/receipt/").json()["receipt"]
    assert receipt["plan_duration_days"] == granted, (receipt["plan_duration_days"], granted)


# ---------------------------------------------------------------- F8
def test_f8_a_float_plan_id_is_refused(make_user, api):
    basic = _plan("rv f8 basic", "10000.00")
    premium = _plan("rv f8 premium", "20000.00")
    user = _person(make_user, "rv_f8")
    c, sub = _subscribe(api, user, basic)

    resp = _change(c, sub, premium.id + 0.9)
    assert (resp.status_code, resp.json().get("code")) == (400, "invalid_plan"), resp.content


def test_f8_an_overflowing_plan_id_is_not_a_500(make_user, api):
    basic = _plan("rv f8o basic", "10000.00")
    user = _person(make_user, "rv_f8o")
    c, sub = _subscribe(api, user, basic)
    c.raise_request_exception = False
    resp = c.post(f"{SUBS}{sub.id}/change-plan/", data='{"plan_id": 1e999}',
                  content_type="application/json")
    assert resp.status_code == 400, (resp.status_code, resp.content[:300])


# ---------------------------------------------------------------- F9
def test_f9_reconcile_on_a_completed_payment_answers_payment_id(
        make_user, api, shamcash, django_capture_on_commit_callbacks):
    from subscription.models import Payment

    plan = _plan("rv f9", "10000.00")
    user = _person(make_user, "rv_f9")
    c, sub = _subscribe(api, user, plan)
    assert _checkout(c, sub).status_code == 200
    payment = Payment.objects.get(subscription=sub)
    _complete(payment, django_capture_on_commit_callbacks)

    resp = _post(c, f"/api/subscription/v1/payments/{payment.id}/reconcile/")
    assert resp.status_code == 200, resp.content
    assert resp.json().get("payment_id") == str(payment.id), resp.json()


# ---------------------------------------------------------------- F13 / F20
def test_f13_a_plan_change_between_quote_and_lock_is_price_changed(
        make_user, api, shamcash, monkeypatch):
    from subscription.models import Payment, Subscription
    from subscription.services.payment_service import PaymentService

    basic = _plan("rv f13 basic", "10000.00")
    premium = _plan("rv f13 premium", "20000.00")
    user = _person(make_user, "rv_f13")
    c, sub = _subscribe(api, user, basic)

    original = PaymentService.start_payment.__func__

    def interleaved(cls, subscription, initiate, **kwargs):
        # change-plan from the customer's other device commits here
        Subscription.objects.filter(pk=subscription.pk).update(plan=premium)
        return original(cls, subscription, initiate, **kwargs)

    monkeypatch.setattr(PaymentService, "start_payment", classmethod(interleaved))
    resp = _checkout(c, sub, amount="10000.00", currency="SYP")
    body = resp.json()
    assert resp.status_code == 409 and body.get("code") == "price_changed", \
        (resp.status_code, body, list(Payment.objects.filter(subscription=sub)
                                      .values_list("amount", flat=True)))
    assert (body["expected_amount"], body["expected_currency"]) == ("20000.00", "SYP")
    assert not Payment.objects.filter(subscription=sub).exists(), "no checkout was opened"


# ---------------------------------------------------------------- F16
def test_f16_choosing_a_plan_without_diet_keeps_the_frozen_diet_read(
        make_user, api, shamcash, django_capture_on_commit_callbacks):
    from subscription.models import Payment, Subscription

    premium = _plan("rv f16 premium", "20000.00", has_diet_access=True)
    basic = _plan("rv f16 basic", "10000.00", has_diet_access=False)
    user = _person(make_user, "rv_f16")
    c, sub = _subscribe(api, user, premium)
    assert _checkout(c, sub).status_code == 200
    _complete(Payment.objects.get(subscription=sub), django_capture_on_commit_callbacks)
    _lapse(sub)
    assert Subscription.objects.get(pk=sub.pk).is_frozen
    assert c.get("/api/diet/v1/my/diet-plans/").status_code == 200, "frozen read before"

    assert _change(c, sub, basic.id).status_code == 200
    resp = c.get("/api/diet/v1/my/diet-plans/")
    assert resp.status_code == 200, (resp.status_code, resp.content[:200])


# ---------------------------------------------------------------- F2, the mirror
def test_f2_refunding_the_late_basic_payment_keeps_premium(
        make_user, api, shamcash, django_capture_on_commit_callbacks):
    """The other half of the rule: refunding the payment that did NOT decide the plan
    leaves the plan where the remaining paid period puts it."""
    from subscription.models import Payment, Subscription
    from subscription.services.payment_service import PaymentService

    basic = _plan("rv f2m basic", "10000.00", days=10)
    premium = _plan("rv f2m premium", "20000.00", days=60)
    user = _person(make_user, "rv_f2m")
    c, sub = _subscribe(api, user, basic)
    assert _checkout(c, sub).status_code == 200
    late = Payment.objects.get(subscription=sub)
    PaymentService.expire_payment(late.id, "window closed")
    assert _change(c, sub, premium.id).status_code == 200
    assert _checkout(c, sub).status_code == 200
    _complete(Payment.objects.get(subscription=sub, status="pending"),
              django_capture_on_commit_callbacks)
    _complete(Payment.objects.get(pk=late.pk), django_capture_on_commit_callbacks)

    with django_capture_on_commit_callbacks(execute=True):
        PaymentService.refund_payment(late.id, "chargeback")

    sub = Subscription.objects.get(pk=sub.pk)
    assert sub.is_active and sub.plan_id == premium.id


# ---------------------------------------------------------------- F4, the snapshot
def test_f4_a_receipt_keeps_the_name_it_was_sold_under(
        make_user, api, shamcash, django_capture_on_commit_callbacks):
    """The snapshot keeps every language's name, so renaming the plan later does not
    rewrite what an earlier receipt says was bought."""
    from subscription.models import Payment

    plan = _plan("rv f4r", "10000.00")
    plan.name_ar = "خطة قديمة"
    plan.save()
    user = _person(make_user, "rv_f4r")
    c, sub = _subscribe(api, user, plan)
    assert _checkout(c, sub).status_code == 200
    paid = Payment.objects.get(subscription=sub)
    _complete(paid, django_capture_on_commit_callbacks)
    plan.name_ar = "اسم جديد"
    plan.save()

    receipt = c.get(f"/api/subscription/v1/payments/{paid.id}/receipt/",
                    HTTP_ACCEPT_LANGUAGE="ar").json()["receipt"]
    assert receipt["item"]["plan_name"] == "خطة قديمة"
