"""Upgrading while a plan runs (the owner, 2026-09-23).

An upgrade is allowed while a plan runs and a downgrade is not; the days left are
credited at the price paid × days left ÷ the period's length; the customer pays the new
price minus that credit, sees it before paying, and the new plan starts — for its full
length — when that payment clears. `GET`/`POST v1/subscriptions/{id}/upgrade/`,
`subscription.upgrade.quote_upgrade`, `PaymentService.start_upgrade` / `_apply_upgrade`
/ `_undo_upgrade`.
"""
from datetime import timedelta
from decimal import Decimal

import pytest
from django.utils import timezone

from tests.test_backend_bugs_app_0923b import _complete, _plan
from tests.test_fx_subs import (  # noqa: F401
    _checkout, _person, _post, _rate, _subscribe, dollars_chargeable, shamcash)

pytestmark = pytest.mark.django_db

D = Decimal
SUBS = "/api/subscription/v1/subscriptions/"


def _upgrade_url(sub):
    return f"{SUBS}{sub.id}/upgrade/"


def _quote(c, sub, plan, **extra):
    return c.get(_upgrade_url(sub), {"plan_id": plan.id}, **extra)


def _buy(c, sub, plan, **payload):
    return _post(c, _upgrade_url(sub), {"plan_id": plan.id, **payload})


def _active_on(api, make_user, name, plan, capture, *, days_left=None, currency="SYP"):
    """A subscriber whose paid period on `plan` is running, `days_left` whole days and
    an hour from its end."""
    from subscription.models import Payment, Subscription

    user = _person(make_user, name, currency)
    c, sub = _subscribe(api, user, plan)
    assert _checkout(c, sub).status_code == 200
    _complete(Payment.objects.get(subscription=sub), capture)
    if days_left is not None:
        Subscription.objects.filter(pk=sub.pk).update(
            end_date=timezone.now() + timedelta(days=days_left, hours=1))
    return c, Subscription.objects.get(pk=sub.pk)


# ------------------------------------------------------------------- the quote
def test_the_quote_credits_the_whole_days_left_and_charges_nothing(
        make_user, api, shamcash, django_capture_on_commit_callbacks):
    from subscription.models import Payment

    basic = _plan("up basic", "10000.00", days=30)
    premium = _plan("up premium", "20000.00", days=30)
    c, sub = _active_on(api, make_user, "up_quote", basic,
                        django_capture_on_commit_callbacks, days_left=20)
    before = Payment.objects.filter(subscription=sub).count()

    resp = _quote(c, sub, premium)
    assert resp.status_code == 200, resp.content
    q = resp.json()
    # 10000 paid for 30 days; 20 whole days left (the hour under way is used).
    assert (q["credit"], q["new_price"], q["to_pay"]) == ("6666.67", "20000.00", "13333.33")
    assert (q["days_left"], q["currency"]) == (20, "SYP")
    assert (q["from_plan"]["id"], q["to_plan"]["id"], q["starts"]) == \
        (basic.id, premium.id, "when_paid")
    assert Payment.objects.filter(subscription=sub).count() == before, "a quote charges nothing"


def test_paying_the_difference_starts_the_new_plan_for_its_full_length(
        make_user, api, shamcash, django_capture_on_commit_callbacks):
    from subscription.models import Payment, Subscription

    basic = _plan("up2 basic", "10000.00", days=30, has_ai_advice=False)
    premium = _plan("up2 premium", "20000.00", days=45, has_ai_advice=True)
    c, sub = _active_on(api, make_user, "up_pay", basic,
                        django_capture_on_commit_callbacks, days_left=20)
    old = Payment.objects.get(subscription=sub, status="completed")

    resp = _buy(c, sub, premium, amount="13333.33", currency="SYP")
    assert resp.status_code == 200, resp.content
    body = resp.json()
    assert (body["amount"], body["currency"]) == ("13333.33", "SYP")
    assert body["upgrade"]["credit"] == "6666.67" and body["reference"]
    assert Subscription.objects.get(pk=sub.pk).plan_id == basic.id, "nothing moves until paid"

    payment = Payment.objects.get(pk=body["payment_id"])
    _complete(payment, django_capture_on_commit_callbacks)

    sub = Subscription.objects.get(pk=sub.pk)
    payment.refresh_from_db()
    assert sub.plan_id == premium.id and sub.has_ai_advice is True
    assert payment.covers_until - payment.covers_from == timedelta(days=45), "full length"
    assert abs((sub.end_date - timezone.now()) - timedelta(days=45)) < timedelta(minutes=1)
    assert sub.start_date == payment.covers_from, "a new run began at the switch"
    assert not sub.current_run_payments().filter(pk=old.pk).exists(), \
        "the replaced plan's payment no longer counts as this run's"


def test_paying_the_same_upgrade_twice_is_one_checkout(
        make_user, api, shamcash, django_capture_on_commit_callbacks):
    from subscription.models import Payment

    basic = _plan("up3 basic", "10000.00")
    premium = _plan("up3 premium", "20000.00")
    c, sub = _active_on(api, make_user, "up_twice", basic, django_capture_on_commit_callbacks,
                        days_left=10)
    first = _buy(c, sub, premium)
    second = _buy(c, sub, premium)
    assert first.status_code == second.status_code == 200, (first.content, second.content)
    assert first.json()["payment_id"] == second.json()["payment_id"]
    assert Payment.objects.filter(subscription=sub, status="pending").count() == 1


# ------------------------------------------------------------------- refusals
def test_a_downgrade_waits_until_the_period_ends(make_user, api, shamcash,
                                                 django_capture_on_commit_callbacks):
    cheap = _plan("up4 cheap", "10000.00")
    same = _plan("up4 same", "20000.00")
    premium = _plan("up4 premium", "20000.00")
    c, sub = _active_on(api, make_user, "up_down", premium, django_capture_on_commit_callbacks)
    for plan in (cheap, same, premium):
        for resp in (_quote(c, sub, plan), _buy(c, sub, plan)):
            assert (resp.status_code, resp.json()["code"]) == (409, "not_an_upgrade"), plan


def test_nothing_running_is_change_plan_not_upgrade(make_user, api):
    basic = _plan("up5 basic", "10000.00")
    premium = _plan("up5 premium", "20000.00")
    user = _person(make_user, "up_none")
    c, sub = _subscribe(api, user, basic)
    resp = _quote(c, sub, premium)
    assert (resp.status_code, resp.json()["code"]) == (409, "no_running_cover")


def test_a_credit_that_covers_the_new_price_is_not_payable(
        make_user, api, shamcash, django_capture_on_commit_callbacks):
    """Two Basic months paid ahead leave 59 whole days: a credit of 19,666.67 against a
    15,000 Premium. The owner declined both extra days and wallet credit for leftover
    value, so this upgrade waits; nothing to pay is shown as 0.00, never negative."""
    from subscription.models import Payment

    basic = _plan("up6 basic", "10000.00", days=30)
    premium = _plan("up6 premium", "15000.00", days=30)
    c, sub = _active_on(api, make_user, "up_cover", basic, django_capture_on_commit_callbacks)
    renew = _post(c, f"{SUBS}{sub.id}/renew/", {"gateway": "shamcash"})
    assert renew.status_code == 200, renew.content
    _complete(Payment.objects.get(pk=renew.json()["payment_id"]),
              django_capture_on_commit_callbacks)

    for resp in (_quote(c, sub, premium), _buy(c, sub, premium)):
        body = resp.json()
        assert (resp.status_code, body["code"]) == (409, "upgrade_not_payable"), body
        assert (body["to_pay"], body["minimum"]) == ("0.00", "100")
    assert not Payment.objects.filter(subscription=sub, status="pending").exists()


def test_a_leftover_below_the_gateway_minimum_is_not_payable(
        make_user, api, shamcash, django_capture_on_commit_callbacks):
    basic = _plan("up7 basic", "10000.00", days=30)
    premium = _plan("up7 premium", "10020.00", days=30)
    c, sub = _active_on(api, make_user, "up_min", basic, django_capture_on_commit_callbacks,
                        days_left=29)
    # 10020 - 10000 × 29/30 = 353.33: payable. With 30 whole days left it would be 20.
    from subscription.models import Subscription

    Subscription.objects.filter(pk=sub.pk).update(
        end_date=timezone.now() + timedelta(days=30, hours=1))
    resp = _quote(c, sub, premium)
    body = resp.json()
    assert (resp.status_code, body["code"]) == (409, "upgrade_not_payable"), body
    assert (body["to_pay"], body["minimum"]) == ("20.00", "100")


def test_a_store_build_may_not_upgrade(make_user, api, shamcash,
                                       django_capture_on_commit_callbacks):
    basic = _plan("up8 basic", "10000.00")
    premium = _plan("up8 premium", "20000.00")
    c, sub = _active_on(api, make_user, "up_store", basic, django_capture_on_commit_callbacks)
    for channel in ("play", "ios"):
        for resp in (_quote(c, sub, premium, HTTP_X_APP_CHANNEL=channel),
                     _post(c, _upgrade_url(sub), {"plan_id": premium.id},
                           HTTP_X_APP_CHANNEL=channel)):
            assert (resp.status_code, resp.json()["code"]) == \
                (403, "not_available_on_channel"), channel


def test_a_figure_the_customer_was_not_shown_is_price_changed(
        make_user, api, shamcash, django_capture_on_commit_callbacks):
    from subscription.models import Payment

    basic = _plan("up9 basic", "10000.00", days=30)
    premium = _plan("up9 premium", "20000.00", days=30)
    c, sub = _active_on(api, make_user, "up_price", basic, django_capture_on_commit_callbacks,
                        days_left=20)
    resp = _buy(c, sub, premium, amount="12000.00")
    body = resp.json()
    assert (resp.status_code, body["code"]) == (409, "price_changed"), body
    assert (body["expected_amount"], body["expected_currency"]) == ("13333.33", "SYP")
    assert not Payment.objects.filter(subscription=sub, status="pending").exists()


def test_another_open_checkout_blocks_the_upgrade(make_user, api, shamcash,
                                                  django_capture_on_commit_callbacks):
    basic = _plan("up10 basic", "10000.00")
    premium = _plan("up10 premium", "20000.00")
    c, sub = _active_on(api, make_user, "up_open", basic, django_capture_on_commit_callbacks)
    renew = _post(c, f"{SUBS}{sub.id}/renew/", {"gateway": "shamcash"})
    assert renew.status_code == 200, renew.content
    resp = _buy(c, sub, premium)
    body = resp.json()
    assert (resp.status_code, body["code"]) == (409, "checkout_open"), body
    assert body["payment_id"] == renew.json()["payment_id"]


def test_an_invalid_plan_is_invalid_plan(make_user, api, shamcash,
                                         django_capture_on_commit_callbacks):
    basic = _plan("up11 basic", "10000.00")
    c, sub = _active_on(api, make_user, "up_bad", basic, django_capture_on_commit_callbacks)
    for bad in ("x", 2.5, None, 987654321):
        resp = c.get(_upgrade_url(sub), {} if bad is None else {"plan_id": bad})
        assert (resp.status_code, resp.json()["code"]) == (400, "invalid_plan"), bad


# ------------------------------------------------------------------- late, refund
def test_an_upgrade_paid_after_its_credit_ran_out_grants_only_what_it_bought(
        make_user, api, shamcash, django_capture_on_commit_callbacks):
    """The checkout expired, the old plan ended, then the transfer cleared. The credited
    days were used, so the money buys new_length × to_pay ÷ new_price days — and the
    receipt says that many."""
    from subscription.models import Payment, Subscription
    from subscription.services.payment_service import PaymentService

    basic = _plan("up12 basic", "10000.00", days=30)
    premium = _plan("up12 premium", "20000.00", days=30)
    c, sub = _active_on(api, make_user, "up_late", basic, django_capture_on_commit_callbacks,
                        days_left=15)
    body = _buy(c, sub, premium).json()
    payment = Payment.objects.get(pk=body["payment_id"])
    assert payment.amount == D("15000.00")
    PaymentService.expire_payment(payment.id, "window closed")
    now = timezone.now()
    Subscription.objects.filter(pk=sub.pk).update(
        start_date=now - timedelta(days=31), end_date=now - timedelta(days=1))

    _complete(Payment.objects.get(pk=payment.pk), django_capture_on_commit_callbacks)

    sub = Subscription.objects.get(pk=sub.pk)
    payment.refresh_from_db()
    granted = payment.covers_until - payment.covers_from
    assert sub.plan_id == premium.id and sub.is_active
    assert granted == timedelta(days=23), "30 × 15000 ÷ 20000 = 22.5, rounded half up"
    receipt = c.get(f"/api/subscription/v1/payments/{payment.id}/receipt/").json()["receipt"]
    assert receipt["plan_duration_days"] == 23 and receipt["upgrade"]["late"] is True
    assert (receipt["upgrade"]["credit"], receipt["upgrade"]["days_left"]) == (None, None), \
        "no cover was left to credit"


def test_refunding_an_upgrade_takes_back_what_its_money_bought(
        make_user, api, shamcash, django_capture_on_commit_callbacks):
    """The upgrade's 30 Premium days were bought by 13,333.33 of money and 6,666.67 of
    credit for 20 Basic days. Refunding the money takes back its 20 days; the 10 the
    credit bought stay — they were the customer's already (the review of 2026-09-23)."""
    from subscription.models import Payment, Subscription
    from subscription.services.payment_service import PaymentService

    basic = _plan("up13 basic", "10000.00", days=30, has_ai_advice=False)
    premium = _plan("up13 premium", "20000.00", days=30, has_ai_advice=True)
    c, sub = _active_on(api, make_user, "up_refund", basic,
                        django_capture_on_commit_callbacks, days_left=20)
    body = _buy(c, sub, premium).json()
    upgrade = Payment.objects.get(pk=body["payment_id"])
    _complete(upgrade, django_capture_on_commit_callbacks)
    upgrade.refresh_from_db()
    assert Subscription.objects.get(pk=sub.pk).plan_id == premium.id

    with django_capture_on_commit_callbacks(execute=True):
        PaymentService.refund_payment(upgrade.id, "chargeback")

    sub = Subscription.objects.get(pk=sub.pk)
    assert (sub.plan_id, sub.status, sub.is_active) == (premium.id, "active", True)
    left = sub.end_date - upgrade.covers_from
    assert timedelta(days=9, hours=23) <= left <= timedelta(days=10, hours=1), left


# ------------------------------------------------------------------- receipt, currency
def test_the_receipt_says_where_the_credit_went(make_user, api, shamcash,
                                                django_capture_on_commit_callbacks):
    from django.utils import translation

    from subscription.models import Payment
    from subscription.receipts import missing_glyphs

    basic = _plan("up14 basic", "10000.00", days=30)
    premium = _plan("up14 premium", "20000.00", days=30)
    c, sub = _active_on(api, make_user, "up_receipt", basic,
                        django_capture_on_commit_callbacks, days_left=20)
    payment = Payment.objects.get(pk=_buy(c, sub, premium).json()["payment_id"])
    _complete(payment, django_capture_on_commit_callbacks)

    receipt = c.get(f"/api/subscription/v1/payments/{payment.id}/receipt/").json()["receipt"]
    assert receipt["amount"] == "13333.33"
    assert receipt["upgrade"] == {
        "from_plan_id": basic.id, "from_plan_name": basic.name, "new_price": "20000.00",
        "credit": "6666.67", "days_left": 20, "granted_days": 30,
        "period_price": "20000.00", "currency": "SYP", "late": False}
    pdf = c.get(f"/api/subscription/v1/payments/{payment.id}/receipt/?format=pdf",
                HTTP_ACCEPT_LANGUAGE="ar")
    assert pdf.status_code == 200 and pdf.content[:4] == b"%PDF"
    with translation.override("ar"):
        for label in ("Upgraded from", "Plan price", "Credit for days left"):
            assert not missing_glyphs(translation.gettext(label)), label

    other = Payment.objects.filter(subscription=sub, status="completed").exclude(
        pk=payment.pk).first()
    assert c.get(f"/api/subscription/v1/payments/{other.id}/receipt/").json()[
        "receipt"]["upgrade"] is None


def test_a_dollar_buyer_is_quoted_and_charged_in_dollars(
        make_user, api, shamcash, dollars_chargeable, django_capture_on_commit_callbacks):
    _rate("13000")
    basic = _plan("up15 basic", "13000.00", days=30)        # 1.00 USD
    premium = _plan("up15 premium", "130000.00", days=30)   # 10.00 USD
    c, sub = _active_on(api, make_user, "up_usd", basic, django_capture_on_commit_callbacks,
                        days_left=15, currency="USD")
    q = _quote(c, sub, premium).json()
    assert (q["currency"], q["new_price"], q["credit"], q["to_pay"]) == \
        ("USD", "10.00", "0.50", "9.50"), q
    body = _buy(c, sub, premium, amount="9.50", currency="USD").json()
    assert (body["amount"], body["currency"]) == ("9.50", "USD"), body


def test_the_upgrade_contract_lists_every_code():
    from subscription.views import SubscriptionViewSet

    doc = SubscriptionViewSet.upgrade.__doc__
    for code in ("app_channel_required", "not_available_on_channel", "not_found",
                 "invalid_plan", "no_running_cover", "not_an_upgrade", "upgrade_not_payable",
                 "checkout_open", "price_changed", "exchange_rate_stale",
                 "exchange_rate_missing", "invalid_amount", "gateway_unavailable",
                 "currency_not_chargeable", "gateway_error", "validation_error",
                 "server_error"):
        assert f"`{code}`" in doc, code
