"""What the adversarial review of the mid-period upgrade found (U1..U15, 2026-09-23).

Three reviewers read the first build (9f54239); one verifier reproduced each finding.
Every test here failed on that build for the reason its name gives. The fix is one
rule: the switch records who paid for what, and a refund takes back exactly that
(`PaymentService._apply_upgrade`, `_upgrade_share`, `upgrade.credit_for_run`).
"""
import re
from datetime import timedelta
from decimal import Decimal

import pytest
from django.utils import timezone

from tests.test_backend_bugs_app_0923b import _complete, _plan
from tests.test_fx_subs import (  # noqa: F401
    _checkout, _person, _post, _rate, _subscribe, dollars_chargeable, shamcash)
from tests.test_plan_upgrade import _active_on, _buy, _quote, _upgrade_url

pytestmark = pytest.mark.django_db

D = Decimal
SUBS = "/api/subscription/v1/subscriptions/"


def _renew(c, sub, capture):
    from subscription.models import Payment

    r = _post(c, f"{SUBS}{sub.id}/renew/", {"gateway": "shamcash"})
    assert r.status_code == 200, r.content
    p = Payment.objects.get(pk=r.json()["payment_id"])
    _complete(p, capture)
    return Payment.objects.get(pk=p.pk)


def _upgrade_paid(c, sub, plan, capture):
    from subscription.models import Payment

    body = _buy(c, sub, plan).json()
    assert "payment_id" in body, body
    p = Payment.objects.get(pk=body["payment_id"])
    _complete(p, capture)
    return Payment.objects.get(pk=p.pk)


def _refund(pid, capture):
    from subscription.services.payment_service import PaymentService

    with capture(execute=True):
        PaymentService.refund_payment(pid, "chargeback")


# ---------------------------------------------------------------- U1 / U17
def test_u1_refunding_the_replaced_plan_payment_takes_its_credited_share_back(
        make_user, api, shamcash, django_capture_on_commit_callbacks):
    from subscription.models import Payment, Subscription

    cap = django_capture_on_commit_callbacks
    basic = _plan("rv1 basic", "10000.00", days=30)
    premium = _plan("rv1 premium", "20000.00", days=30)
    c, sub = _active_on(api, make_user, "rv_u1", basic, cap, days_left=20)
    p1 = Payment.objects.get(subscription=sub, status="completed")
    _upgrade_paid(c, sub, premium, cap)
    end_after = Subscription.objects.get(pk=sub.pk).end_date

    _refund(p1.id, cap)
    sub = Subscription.objects.get(pk=sub.pk)
    # P1's 10000 came back; the 6666.67 credit it funded (10 Premium days) must go too.
    assert sub.end_date <= end_after - timedelta(days=9), (sub.end_date, end_after)


# ---------------------------------------------------------------- U2 / U16
def test_u2_refunding_an_upgrade_over_a_stacked_renewal_keeps_the_basic_days(
        make_user, api, shamcash, django_capture_on_commit_callbacks):
    from subscription.models import Subscription

    cap = django_capture_on_commit_callbacks
    basic = _plan("rv2 basic", "10000.00", days=30)
    premium = _plan("rv2 premium", "20000.00", days=30)
    c, sub = _active_on(api, make_user, "rv_u2a", basic, cap)
    _renew(c, sub, cap)                              # R: stacked, covers day 30..60
    up = _upgrade_paid(c, sub, premium, cap)          # 59 days credited, pays 333.33
    assert up.amount == D("333.33"), up.amount

    _refund(up.id, cap)
    sub = Subscription.objects.get(pk=sub.pk)
    # The 59 credited Basic days (19,666.67) bought 29.5 of the 30 Premium days; the
    # refunded 333.33 bought the other half day. The credit stays as Premium days (the
    # rule U7's fix states); the defect was that the whole cover was cancelled.
    assert sub.is_active, (sub.status, sub.end_date)
    left = sub.end_date - up.covers_from
    assert timedelta(days=29, hours=11) <= left <= timedelta(days=29, hours=13), left


def test_u2_refunding_the_stacked_renewal_after_an_upgrade_leaves_cover(
        make_user, api, shamcash, django_capture_on_commit_callbacks):
    from subscription.models import Subscription

    cap = django_capture_on_commit_callbacks
    basic = _plan("rv2b basic", "10000.00", days=30)
    premium = _plan("rv2b premium", "20000.00", days=30)
    c, sub = _active_on(api, make_user, "rv_u2b", basic, cap)
    r = _renew(c, sub, cap)
    _upgrade_paid(c, sub, premium, cap)
    sub = Subscription.objects.get(pk=sub.pk)
    assert not sub.current_run_payments().filter(pk=r.pk).exists(), \
        "a credited renewal is spent, not part of the new run"

    _refund(r.id, cap)
    sub = Subscription.objects.get(pk=sub.pk)
    # R contributed 10000 of credit (15 Premium days); the P1 credit and the cash remain.
    assert sub.is_active, (sub.status, sub.end_date)


# ---------------------------------------------------------------- U3 / U18
def test_u3_a_second_upgrade_credits_the_plan_price_of_the_first(
        make_user, api, shamcash, django_capture_on_commit_callbacks):
    cap = django_capture_on_commit_callbacks
    basic = _plan("rv3 basic", "10000.00", days=30)
    premium = _plan("rv3 premium", "20000.00", days=30)
    gold = _plan("rv3 gold", "40000.00", days=30)
    c, sub = _active_on(api, make_user, "rv_u3", basic, cap, days_left=20)
    up = _upgrade_paid(c, sub, premium, cap)
    assert up.amount == D("13333.33")

    q = _quote(c, sub, gold).json()
    assert q["days_left"] == 29, q
    # 29 of 30 Premium days left, at Premium's price.
    assert q["credit"] == "19333.33", q


# ---------------------------------------------------------------- U4 / U10 / U19
def test_u4_a_late_upgrade_transfer_does_not_erase_a_renewal_paid_after_the_quote(
        make_user, api, shamcash, django_capture_on_commit_callbacks):
    from subscription.models import Payment, Subscription
    from subscription.services.payment_service import PaymentService

    cap = django_capture_on_commit_callbacks
    basic = _plan("rv4 basic", "10000.00", days=30)
    premium = _plan("rv4 premium", "20000.00", days=30)
    c, sub = _active_on(api, make_user, "rv_u4", basic, cap, days_left=9)
    body = _buy(c, sub, premium).json()
    u = Payment.objects.get(pk=body["payment_id"])
    assert u.amount == D("17000.00")
    PaymentService.expire_payment(u.id, "window closed")
    r = _renew(c, sub, cap)                           # 39 days of Basic now
    assert r.status == "completed"

    _complete(Payment.objects.get(pk=u.pk), cap)
    sub = Subscription.objects.get(pk=sub.pk)
    # The renewal's 30 Basic days were paid for and never credited.
    assert sub.end_date > timezone.now() + timedelta(days=31), (sub.plan_id, sub.end_date)


def test_u4c_a_late_upgrade_while_the_old_plan_runs_does_not_credit_used_days(
        make_user, api, shamcash, django_capture_on_commit_callbacks):
    from subscription.models import Payment, Subscription
    from subscription.services.payment_service import PaymentService

    cap = django_capture_on_commit_callbacks
    basic = _plan("rv4c basic", "10000.00", days=30)
    premium = _plan("rv4c premium", "20000.00", days=30)
    c, sub = _active_on(api, make_user, "rv_u4c", basic, cap, days_left=20)
    body = _buy(c, sub, premium).json()
    u = Payment.objects.get(pk=body["payment_id"])
    assert u.amount == D("13333.33")
    PaymentService.expire_payment(u.id, "window closed")
    # Three days pass with Basic still running.
    Subscription.objects.filter(pk=sub.pk).update(
        end_date=timezone.now() + timedelta(days=17, hours=1))

    _complete(Payment.objects.get(pk=u.pk), cap)
    u.refresh_from_db()
    # Only 17 days were left to credit (5666.67); 13333.33 does not buy a full Premium
    # month at 20000 - 5666.67 = 14333.33.
    assert u.covers_until - u.covers_from < timedelta(days=30), \
        (u.covers_from, u.covers_until)


# ---------------------------------------------------------------- U5 / U11 / U20
def test_u5_the_open_checkout_comes_back_for_its_own_amount_after_a_day_ticks(
        make_user, api, shamcash, django_capture_on_commit_callbacks):
    from subscription.models import Subscription

    cap = django_capture_on_commit_callbacks
    basic = _plan("rv5 basic", "10000.00", days=30)
    premium = _plan("rv5 premium", "20000.00", days=30)
    c, sub = _active_on(api, make_user, "rv_u5", basic, cap, days_left=20)
    first = _buy(c, sub, premium, amount="13333.33", currency="SYP")
    assert first.status_code == 200, first.content
    pid = first.json()["payment_id"]
    # The whole-day count ticks while the checkout is open.
    Subscription.objects.filter(pk=sub.pk).update(
        end_date=timezone.now() + timedelta(days=19, hours=1))

    a = _buy(c, sub, premium, amount="13333.33", currency="SYP")
    b = _buy(c, sub, premium, amount="13666.67", currency="SYP")
    answers = [(a.status_code, a.json()), (b.status_code, b.json())]
    assert any(s == 200 and j.get("payment_id") == pid for s, j in answers), answers


# ---------------------------------------------------------------- U6
def test_u6_undo_does_not_restore_a_plan_whose_payment_was_refunded(
        make_user, api, shamcash, django_capture_on_commit_callbacks):
    from subscription.models import Payment, Subscription

    cap = django_capture_on_commit_callbacks
    basic = _plan("rv6 basic", "10000.00", days=30)
    premium = _plan("rv6 premium", "20000.00", days=30)
    c, sub = _active_on(api, make_user, "rv_u6", basic, cap, days_left=20)
    p1 = Payment.objects.get(subscription=sub, status="completed")
    up = _upgrade_paid(c, sub, premium, cap)
    _refund(p1.id, cap)
    _refund(up.id, cap)
    sub = Subscription.objects.get(pk=sub.pk)
    assert not sub.is_active, (sub.status, sub.plan_id, sub.end_date)


def test_u6_undo_keeps_a_cancelled_row_cancelled(
        make_user, api, shamcash, django_capture_on_commit_callbacks):
    from subscription.models import Subscription

    cap = django_capture_on_commit_callbacks
    basic = _plan("rv6b basic", "10000.00", days=30)
    premium = _plan("rv6b premium", "20000.00", days=30)
    c, sub = _active_on(api, make_user, "rv_u6b", basic, cap, days_left=20)
    up = _upgrade_paid(c, sub, premium, cap)
    r = _post(c, f"{SUBS}{sub.id}/cancel/", {"immediate": True, "reason": "x"})
    assert r.status_code == 200, r.content
    assert Subscription.objects.get(pk=sub.pk).status == "cancelled"
    _refund(up.id, cap)
    sub = Subscription.objects.get(pk=sub.pk)
    assert sub.status == "cancelled", (sub.status, sub.plan_id)


# ---------------------------------------------------------------- U7
def test_u7_refunding_an_upgrade_with_a_renewal_behind_it_keeps_the_credited_days(
        make_user, api, shamcash, django_capture_on_commit_callbacks):
    from subscription.models import Subscription

    cap = django_capture_on_commit_callbacks
    basic = _plan("rv7 basic", "10000.00", days=30)
    premium = _plan("rv7 premium", "20000.00", days=30)
    c, sub = _active_on(api, make_user, "rv_u7", basic, cap, days_left=20)
    up = _upgrade_paid(c, sub, premium, cap)
    switch = up.covers_from
    _renew(c, sub, cap)                              # Premium P3 stacked: switch+60
    assert Subscription.objects.get(pk=sub.pk).end_date - switch == timedelta(days=60)

    _refund(up.id, cap)
    sub = Subscription.objects.get(pk=sub.pk)
    # The upgrade's cash bought 20 of its 30 days; the credit's 10 stay.
    assert sub.end_date - switch >= timedelta(days=39), sub.end_date - switch


# ---------------------------------------------------------------- U8
def test_u8_each_running_payment_is_credited_at_its_own_price(
        make_user, api, shamcash, django_capture_on_commit_callbacks):
    from subscription.models import Payment, Subscription

    cap = django_capture_on_commit_callbacks
    basic = _plan("rv8 basic", "10000.00", days=30)
    gold = _plan("rv8 gold", "40000.00", days=30)
    c, sub = _active_on(api, make_user, "rv_u8", basic, cap)
    p1 = Payment.objects.get(subscription=sub, status="completed")
    basic.price = D("16000.00")
    basic.save()
    p2 = _renew(c, sub, cap)
    assert p2.amount == D("16000.00")
    shift = timedelta(days=10)
    for p in (p1, p2):
        Payment.objects.filter(pk=p.pk).update(covers_from=p.covers_from - shift,
                                               covers_until=p.covers_until - shift)
    s = Subscription.objects.get(pk=sub.pk)
    Subscription.objects.filter(pk=sub.pk).update(
        start_date=p1.covers_from - shift, end_date=s.end_date - shift + timedelta(hours=1))

    q = _quote(c, sub, gold).json()
    assert q["days_left"] == 50, q
    # 20 days of P1 at 10000/30 + 30 days of P2 at 16000/30.
    assert q["credit"] == "22666.67", q


# ---------------------------------------------------------------- U9 / U13 / U21
def test_u9_an_unknown_gateway_is_gateway_unavailable(
        make_user, api, shamcash, django_capture_on_commit_callbacks):
    cap = django_capture_on_commit_callbacks
    basic = _plan("rv9 basic", "10000.00", days=30)
    premium = _plan("rv9 premium", "20000.00", days=30)
    c, sub = _active_on(api, make_user, "rv_u9", basic, cap, days_left=20)
    try:
        c.raise_request_exception = False
    except Exception:
        pass
    resp = c.get(_upgrade_url(sub), {"plan_id": premium.id, "gateway": "stripe"})
    assert resp.status_code == 503, (resp.status_code, resp.content[:300])
    resp = _buy(c, sub, premium, gateway="stripe")
    assert resp.status_code == 503, (resp.status_code, resp.content[:300])


# ---------------------------------------------------------------- U12
def test_u12_renew_does_not_pass_an_upgrade_checkout_off_as_a_renewal(
        make_user, api, shamcash, django_capture_on_commit_callbacks):
    cap = django_capture_on_commit_callbacks
    basic = _plan("rv12 basic", "10000.00", days=30)
    premium = _plan("rv12 premium", "20000.00", days=30)
    c, sub = _active_on(api, make_user, "rv_u12", basic, cap, days_left=20)
    up = _buy(c, sub, premium).json()
    r = _post(c, f"{SUBS}{sub.id}/renew/", {"gateway": "shamcash"})
    body = r.json()
    assert r.status_code == 409 or "upgrade" in body or "kind" in body, (r.status_code, body,
                                                                         up["payment_id"])


# ---------------------------------------------------------------- U14
def test_u14_price_changed_after_the_plan_moved_carries_the_fresh_figure(
        make_user, api, shamcash, django_capture_on_commit_callbacks, monkeypatch):
    from subscription import views as sviews
    from subscription.models import Payment, Subscription
    from subscription.services.payment_service import PaymentService
    from subscription.upgrade import quote_upgrade as real_quote

    cap = django_capture_on_commit_callbacks
    basic = _plan("rv14 basic", "10000.00", days=30)
    premium = _plan("rv14 premium", "20000.00", days=30)
    gold = _plan("rv14 gold", "40000.00", days=30)
    c, sub = _active_on(api, make_user, "rv_u14", basic, cap, days_left=20)
    p_up = Payment.objects.get(pk=_buy(c, sub, premium).json()["payment_id"])
    fired = []

    def racing_quote(*a, **kw):
        q = real_quote(*a, **kw)
        if not fired:
            fired.append(1)
            # The other device's Premium upgrade clears between quote and lock.
            PaymentService.complete_payment(p_up.id, {
                "amount": str(p_up.amount), "currency": p_up.currency,
                "status": "completed", "transaction_id": f"tx-{p_up.id}"})
        return q

    monkeypatch.setattr(sviews, "quote_upgrade", racing_quote)
    resp = _buy(c, sub, gold)
    body = resp.json()
    assert (resp.status_code, body.get("code")) == (409, "price_changed"), body
    now_sub = Subscription.objects.get(pk=sub.pk)
    assert now_sub.plan_id == premium.id
    fresh = real_quote(now_sub, gold, buyer_currency="SYP")
    assert body["expected_amount"] == str(fresh.to_pay), (body, fresh.to_pay)


# ---------------------------------------------------------------- U15
def test_u15_a_late_receipt_does_not_claim_a_credit_the_contract_never_explains(
        make_user, api, shamcash, django_capture_on_commit_callbacks):
    from subscription.models import Payment, Subscription
    from subscription.services.payment_service import PaymentService
    from subscription.views import PaymentReceiptView

    cap = django_capture_on_commit_callbacks
    basic = _plan("rv15 basic", "10000.00", days=30)
    premium = _plan("rv15 premium", "20000.00", days=30)
    c, sub = _active_on(api, make_user, "rv_u15", basic, cap, days_left=15)
    payment = Payment.objects.get(pk=_buy(c, sub, premium).json()["payment_id"])
    PaymentService.expire_payment(payment.id, "window closed")
    now = timezone.now()
    Subscription.objects.filter(pk=sub.pk).update(
        start_date=now - timedelta(days=31), end_date=now - timedelta(days=1))
    _complete(Payment.objects.get(pk=payment.pk), cap)

    up = c.get(f"/api/subscription/v1/payments/{payment.id}/receipt/").json()[
        "receipt"]["upgrade"]
    assert up["late"] is True
    doc = PaymentReceiptView.__doc__ or ""
    for m in ("get", "post"):
        doc += getattr(getattr(PaymentReceiptView, m, None), "__doc__", "") or ""
    explained = re.search(r"late[^\n]{0,60}(true|no credit|not applied)", doc, re.I)
    assert up["credit"] is None or explained, (up, "late:true meaning not in the contract")
