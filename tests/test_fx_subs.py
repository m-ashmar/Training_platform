"""The subscription and payment rows of SIDE_BUGS, closed on `fx/subs` (2026-09-23).

One test per defect, each named for its row. Every one of them failed against the code
it fixes, for the reason the row gives — that is how each was checked before the fix
landed. They are journeys where the defect is a journey (a checkout, a poll, a cache
miss) and a reading of the contract where the defect is the contract: the mobile
session builds from the views' and serializers' docstrings, so a docstring that leaves
out a key or a code is a defect in what the app is told.
"""
import hashlib
import hmac
import json
import time
from datetime import timedelta
from decimal import Decimal

import pytest
from django.utils import timezone

pytestmark = pytest.mark.django_db

JSON = "application/json"
D = Decimal
WEBHOOK_SECRET = "fx-subs-webhook-secret-0123456789"


# --------------------------------------------------------------------- helpers
@pytest.fixture
def shamcash():
    """ShamCash in transfer/reconcile mode with an account and a webhook secret. The
    registry is read from the environment at import and the tests set none of it."""
    from subscription.settings import gateway_config

    config = gateway_config.GATEWAY_REGISTRY["shamcash"]["config"][gateway_config.GATEWAY_MODE]
    before = dict(config)
    config["merchant_id"] = "FX-SUBS-ACCOUNT"
    config["webhook_secret"] = WEBHOOK_SECRET
    config["initiate_path"] = ""
    try:
        yield config
    finally:
        config.clear()
        config.update(before)


@pytest.fixture
def dollars_chargeable():
    """The owner's USD bounds, as SHAMCASH_USD_MIN_AMOUNT/_MAX_AMOUNT would set them.
    The numbers are the test's, not a proposal: the owner has not given any."""
    from subscription.settings import gateway_config

    limits = gateway_config.GATEWAY_REGISTRY["shamcash"]["amount_limits"]
    before = dict(limits)
    limits["USD"] = {"min": D("0.50"), "max": D("5000")}
    try:
        yield
    finally:
        limits.clear()
        limits.update(before)


@pytest.fixture
def dollars_not_chargeable():
    """As deployed today: no USD bounds, whatever the environment of this machine."""
    from subscription.settings import gateway_config

    limits = gateway_config.GATEWAY_REGISTRY["shamcash"]["amount_limits"]
    before = dict(limits)
    limits.pop("USD", None)
    try:
        yield
    finally:
        limits.clear()
        limits.update(before)


def _rate(value, *, ago=timedelta(hours=1)):
    from wallet.models import ExchangeRate

    return ExchangeRate.objects.create(base="USD", quote="SYP", rate=D(value),
                                       effective_from=timezone.now() - ago, note="fx-subs")


def _plan(tag, price="13000.00", currency="SYP", **flags):
    from django.utils import translation

    from subscription.models import SubscriptionPlan

    with translation.override("en"):
        return SubscriptionPlan.objects.create(
            name=f"fx-subs {tag}", plan_type="premium", description="fx",
            price=D(price), currency=currency, duration_days=30, is_active=True, **flags)


def _person(make_user, name, currency="SYP", **kw):
    from wallet.models import set_currency_for

    kw.setdefault("phone_number", "0933777000")
    user = make_user(name, **kw)
    set_currency_for(user, currency, "client")
    return user


def _post(client, url, payload=None, **extra):
    return client.post(url, data=json.dumps(payload or {}), content_type=JSON, **extra)


def _subscribe(api, user, plan):
    from subscription.models import Subscription

    c = api(user)
    resp = _post(c, "/api/subscription/v1/subscriptions/", {"plan_id": plan.id})
    assert resp.status_code == 201, resp.content
    return c, Subscription.objects.get(user=user)


def _checkout(c, sub, **payload):
    return _post(c, "/api/subscription/v1/gateways/",
                 {"gateway": "shamcash", "subscription_id": str(sub.id), **payload})


def _no_transactions(monkeypatch):
    from subscription.gateways.shamcash import ShamCashGateway

    monkeypatch.setattr(ShamCashGateway, "_get", lambda self, path, params: {"data": []})


def _paid(monkeypatch, reference, amount, currency="SYP"):
    from subscription.gateways.shamcash import ShamCashGateway

    monkeypatch.setattr(ShamCashGateway, "_get", lambda self, path, params: {"data": [{
        "id": f"txn-{reference}", "amount": str(amount), "currency": currency,
        "note": reference}]})


def _plan_row(c, plan, **extra):
    rows = c.get("/api/subscription/v1/plans/", **extra).json()
    rows = rows.get("results", rows) if isinstance(rows, dict) else rows
    return next(r for r in rows if r["id"] == plan.id)


# ============================================================== S-049 overrides
def test_s049_an_operators_grant_survives_the_next_save(make_user, django_capture_on_commit_callbacks):
    """`Subscription.save()` copied all five flags from the plan on EVERY save, so an
    operator's per-subscriber grant was reverted by the next save of the row — here, the
    activation a payment makes. Moving the row onto another plan still re-inherits."""
    from subscription.models import Payment, Subscription
    from subscription.services.payment_service import PaymentService

    plan = _plan("s049", has_diet_access=True, has_ai_advice=False)
    user = _person(make_user, "fx_s049")
    sub = Subscription.objects.create(user=user, plan=plan, status="pending",
                                      end_date=timezone.now() + timedelta(days=30))
    assert sub.has_diet_access is True and sub.has_ai_advice is False, "inherited on create"

    # The operator grants the AI coach to this one subscriber (the admin's change form).
    granted = Subscription.objects.get(pk=sub.pk)
    granted.has_ai_advice = True
    granted.save()
    assert Subscription.objects.get(pk=sub.pk).has_ai_advice is True, \
        "the grant did not survive its own save"

    # The next save of the row is an ordinary one: the customer pays.
    payment = Payment.objects.create(subscription=sub, amount=plan.price, currency="SYP",
                                     status="pending", gateway_transaction_reference="fx-s049")
    with django_capture_on_commit_callbacks(execute=True):
        PaymentService.complete_payment(payment.id, {
            "amount": str(plan.price), "currency": "SYP", "status": "completed",
            "transaction_id": "fx-s049-tx"})
    sub.refresh_from_db()
    assert sub.status == "active"
    assert sub.has_ai_advice is True, "activating the subscription reverted the grant"

    # Moving the subscriber to another plan IS a reason to take that plan's features.
    other = _plan("s049 other", has_diet_access=False, has_ai_advice=False)
    moved = Subscription.objects.get(pk=sub.pk)
    moved.plan = other
    moved.save()
    moved.refresh_from_db()
    assert (moved.has_diet_access, moved.has_ai_advice) == (False, False)


# ======================================================= S-052 dollars at the gateway
def test_s052_a_dollar_checkout_is_refused_with_a_code_until_the_owner_sets_bounds(
        make_user, api, shamcash, dollars_not_chargeable):
    """ShamCash has bounds for SYP only, and a currency with none is refused — fail
    closed by design. But it was refused as a bare 400 "request could not be
    completed", which the app cannot tell from any other fault. Now: 503
    `currency_not_chargeable`, and nothing is left open behind it."""
    from subscription.models import Payment

    _rate("13000")
    plan = _plan("s052")
    user = _person(make_user, "fx_s052", "USD")
    c, sub = _subscribe(api, user, plan)

    resp = _checkout(c, sub)
    assert resp.status_code == 503, resp.content
    assert resp.json()["code"] == "currency_not_chargeable", resp.content
    assert not Payment.objects.filter(subscription=sub).exists(), "a checkout was left open"


def test_s052_bounds_come_from_the_environment_and_never_from_a_guess(monkeypatch):
    from subscription.settings.gateway_config import _amount_limits_from_env

    monkeypatch.delenv("FXTEST_USD_MIN_AMOUNT", raising=False)
    monkeypatch.delenv("FXTEST_USD_MAX_AMOUNT", raising=False)
    assert _amount_limits_from_env("FXTEST", "USD") is None, "no numbers, no bounds"

    monkeypatch.setenv("FXTEST_USD_MIN_AMOUNT", "1")
    assert _amount_limits_from_env("FXTEST", "USD") is None, "half a range is no range"

    monkeypatch.setenv("FXTEST_USD_MAX_AMOUNT", "400")
    assert _amount_limits_from_env("FXTEST", "USD") == {"min": D("1"), "max": D("400")}

    for low, high in (("abc", "400"), ("0", "400"), ("500", "400")):
        monkeypatch.setenv("FXTEST_USD_MIN_AMOUNT", low)
        monkeypatch.setenv("FXTEST_USD_MAX_AMOUNT", high)
        assert _amount_limits_from_env("FXTEST", "USD") is None, (low, high)


# ============================================================ S-074 plan currency
def test_s074_both_plan_admins_price_a_plan_in_a_currency(make_user, rf):
    """Neither plan admin exposed `currency`, so every plan an operator made was SYP,
    and the console let `price` be typed into the list with no unit beside it."""
    from django.contrib import admin as dj_admin

    from admin_dashboard.admin import admin_site
    from subscription.models import SubscriptionPlan

    request = rf.get("/")
    request.user = make_user("fx_s074_op", user_type="admin")
    for site in (dj_admin.site, admin_site):
        model_admin = site._registry[SubscriptionPlan]
        assert "currency" in model_admin.get_form(request).base_fields, site.name
        assert "currency" in model_admin.list_display, site.name
        assert "price" not in (model_admin.list_editable or ()), \
            f"{site.name}: a bare price is editable in the list"


# ================================================ S-075 the window of an adopted row
def test_s075_a_checkout_opened_a_day_after_the_plan_was_chosen_is_not_born_expired(
        make_user, api, shamcash, monkeypatch):
    """The checkout adopts the reference-less row written when the plan was chosen and
    kept its `created_at`, which is what the window is measured from. Opened a day
    later, it was born expired and the poller closed it inside a minute."""
    from subscription.models import Payment, Subscription
    from subscription.tasks import poll_open_payments

    plan = _plan("s075")
    user = _person(make_user, "fx_s075")
    sub = Subscription.objects.create(user=user, plan=plan, status="pending",
                                      end_date=timezone.now() + timedelta(days=30))
    # The row POST /subscriptions/ used to write, the day before.
    chosen = Payment.objects.create(subscription=sub, amount=plan.price, currency="SYP",
                                    status="pending")
    Payment.objects.filter(pk=chosen.pk).update(created_at=timezone.now() - timedelta(days=1))

    resp = _checkout(api(user), sub)
    assert resp.status_code == 200, resp.content
    body = resp.json()
    assert body["payment_id"] == str(chosen.pk), "the orphan row was not the one adopted"
    from django.utils.dateparse import parse_datetime

    expires = parse_datetime(body["expires_at"])
    assert expires > timezone.now() + timedelta(minutes=50), \
        f"the checkout was born closing at {expires}"

    # Five minutes later nobody has paid yet: the poller must leave it open.
    _no_transactions(monkeypatch)
    result = poll_open_payments(now=timezone.now() + timedelta(minutes=5))
    assert result["checked"] == 1
    assert Payment.objects.get(pk=chosen.pk).status == "pending", \
        "a checkout five minutes old was expired"


# ============================================================ S-076 the payout sweep
def test_s076_the_owed_payout_sweep_is_scheduled():
    """A registered task in no schedule never runs, and this one is the backstop every
    payout path names."""
    from celery.schedules import crontab
    from django.conf import settings

    entries = [e for e in settings.CELERY_BEAT_SCHEDULE.values()
               if e["task"] == "subscription.settle_owed_payouts"]
    assert entries, "nothing schedules subscription.settle_owed_payouts"
    assert isinstance(entries[0]["schedule"], crontab)


# ======================================================= S-077 late money, no webhook
def test_s077_money_that_lands_after_the_checkout_closed_is_found_without_a_webhook(
        make_user, api, shamcash, monkeypatch, django_capture_on_commit_callbacks):
    """Reconcile mode has no webhook, and the poller only looks at OPEN checkouts: once
    one expired, nothing asked about its reference again, and a customer who paid late
    kept 'expired' and no access."""
    from django.conf import settings

    from subscription.models import Payment, Subscription
    from subscription.tasks import poll_expired_checkouts, poll_open_payments

    plan = _plan("s077")
    user = _person(make_user, "fx_s077")
    c, sub = _subscribe(api, user, plan)
    reference = _checkout(c, sub).json()["reference"]
    payment = Payment.objects.get(gateway_transaction_reference=reference)
    Payment.objects.filter(pk=payment.pk).update(created_at=timezone.now() - timedelta(hours=3))
    _no_transactions(monkeypatch)
    poll_open_payments()
    assert Payment.objects.get(pk=payment.pk).status == "expired"

    # The transfer posts to the merchant account after the window closed.
    _paid(monkeypatch, reference, plan.price)
    with django_capture_on_commit_callbacks(execute=True):
        result = poll_expired_checkouts()
    assert result["completed"] == 1, result
    assert Payment.objects.get(pk=payment.pk).status == "completed"
    assert Subscription.objects.get(pk=sub.pk).is_active, "paid late and got nothing"

    assert any(e["task"] == "subscription.poll_expired_checkouts"
               for e in settings.CELERY_BEAT_SCHEDULE.values()), "the late sweep never runs"


# ============================================================ S-128 payment admins
def test_s128_neither_admin_can_write_a_payments_state(make_user, rf):
    """The change form left `status`, `amount`, `currency` and `subscription` open (and
    the console every column): a payment's state written with no PaymentService."""
    from django.contrib import admin as dj_admin

    from admin_dashboard.admin import admin_site
    from subscription.models import Payment, Subscription

    plan = _plan("s128")
    user = _person(make_user, "fx_s128")
    sub = Subscription.objects.create(user=user, plan=plan, status="pending",
                                      end_date=timezone.now() + timedelta(days=30))
    payment = Payment.objects.create(subscription=sub, amount=plan.price, currency="SYP",
                                     status="pending")
    request = rf.get("/")
    request.user = make_user("fx_s128_op", user_type="admin")

    owned = {"subscription", "amount", "currency", "status", "price_amount",
             "price_currency", "exchange_rate", "exchange_rate_value", "payout_settled_at",
             "completed_at", "invoice_number"}
    for site in (dj_admin.site, admin_site):
        model_admin = site._registry[Payment]
        editable = set(model_admin.get_form(request, payment).base_fields)
        assert not (editable & owned), (site.name, sorted(editable & owned))
        assert not model_admin.has_add_permission(request), \
            f"{site.name}: a payment can be written by hand in any status"
        # The state still moves — through the service, as an action.
        assert "mark_as_completed" in model_admin.get_actions(request), site.name


# ============================================================ S-129 a full page
def _account(transactions):
    """A merchant account that answers `/transactions` the way a paged API does: the
    newest `limit` rows inside [start_at, end_at]."""
    from datetime import datetime

    def _get(self, path, params):
        low = datetime.fromisoformat(params["start_at"])
        high = datetime.fromisoformat(params["end_at"])
        rows = sorted((t for t in transactions if low <= t["at"] <= high),
                      key=lambda t: t["at"], reverse=True)[:params["limit"]]
        return {"data": [{k: v for k, v in t.items() if k != "at"} for t in rows]}
    return _get


def test_s129_a_paid_transfer_behind_a_full_page_is_still_found(monkeypatch):
    """One page of 200 from the last 72 h, and 'pending' when the reference was not on
    it — which the poller reads as "nobody paid" and expires."""
    from subscription.gateways.shamcash import ShamCashGateway

    now = timezone.now()
    busy = [{"id": f"t{i}", "amount": "700.00", "currency": "SYP", "note": f"other {i}",
             "at": now - timedelta(minutes=8 * i)} for i in range(1, 450)]
    busy.append({"id": "mine", "amount": "13000.00", "currency": "SYP",
                 "note": "SHAMCASH_REF_S129", "at": now - timedelta(hours=50)})
    monkeypatch.setattr(ShamCashGateway, "_get", _account(busy))

    gateway = ShamCashGateway({"merchant_id": "acct"})
    result = gateway.fetch_payment_status(
        "SHAMCASH_REF_S129", expected={"amount": D("13000.00"), "currency": "SYP"})
    assert result["status"] == "completed", result


def test_s129_a_window_that_cannot_be_read_in_full_is_unknown_not_pending(monkeypatch):
    """Evidence of nothing closes nothing: the poller leaves 'unknown' alone."""
    from subscription.gateways.shamcash import ShamCashGateway

    now = timezone.now()
    flood = [{"id": f"t{i}", "amount": "700.00", "currency": "SYP", "note": f"other {i}",
              "at": now - timedelta(seconds=40 * i)} for i in range(1, 6000)]
    monkeypatch.setattr(ShamCashGateway, "_get", _account(flood))

    gateway = ShamCashGateway({"merchant_id": "acct"})
    result = gateway.fetch_payment_status(
        "SHAMCASH_REF_ABSENT", expected={"amount": D("13000.00"), "currency": "SYP"})
    assert result["status"] == "unknown", result


# ============================================================ S-130 the plan list
def test_s130_pricing_the_plan_list_costs_the_same_for_one_plan_or_many(make_user, rf):
    """Each row read the reader's wallet and, across currencies, the rate — two queries
    per plan on the Plans screen and on every cache miss."""
    from cachalot.api import cachalot_disabled
    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    from subscription.models import SubscriptionPlan
    from subscription.serializers import SubscriptionPlanListSerializer

    _rate("13000")
    reader = _person(make_user, "fx_s130", "USD")
    first = _plan("s130 a")
    others = [_plan(f"s130 {tag}") for tag in "bcde"]

    def queries(plans):
        request = rf.get("/api/subscription/v1/plans/", HTTP_X_APP_CHANNEL="apk")
        request.user = reader
        with cachalot_disabled(), CaptureQueriesContext(connection) as ctx:
            data = SubscriptionPlanListSerializer(
                plans, many=True, context={"request": request}).data
        assert all(row["currency"] == "USD" for row in data), data
        return len(ctx.captured_queries)

    one = queries(list(SubscriptionPlan.objects.filter(pk=first.pk)))
    five = queries(list(SubscriptionPlan.objects.filter(
        pk__in=[first.pk, *[p.pk for p in others]])))
    assert five == one, f"{one} queries for one plan, {five} for five"


# ============================================================ S-131 the top-level code
def test_s131_a_patch_carrying_auto_renew_answers_its_code_at_the_top(make_user, api):
    from subscription.models import Subscription

    plan = _plan("s131")
    user = _person(make_user, "fx_s131")
    sub = Subscription.objects.create(user=user, plan=plan, status="active",
                                      end_date=timezone.now() + timedelta(days=30))
    resp = api(user).patch(f"/api/subscription/v1/subscriptions/{sub.id}/",
                           data=json.dumps({"auto_renew": False}), content_type=JSON)
    assert resp.status_code == 400, resp.content
    body = resp.json()
    assert body["code"] == "auto_renew_read_only", body
    assert body["field_errors"]["auto_renew"][0]["code"] == "auto_renew_read_only", body
    assert Subscription.objects.get(pk=sub.pk).auto_renew is True


# ============================================================ S-132 the summary contract
def test_s132_the_summary_contract_names_every_price_key_it_sends_and_withholds():
    """Five price keys are sent and five withheld on a store build; the docstrings the
    app reads named one."""
    from subscription.serializers import UserSubscriptionSummarySerializer
    from subscription.views import SubscriptionViewSet

    for doc in (UserSubscriptionSummarySerializer.__doc__, SubscriptionViewSet.current.__doc__):
        for key in UserSubscriptionSummarySerializer.price_fields:
            assert key in doc, (key, doc[:80])
        assert "ABSENT" in doc, "does not say the keys vanish on a store build"


# ============================================================ S-133 the inbox row
def test_s133_a_muted_type_still_gets_its_inbox_row(make_user, api, shamcash, monkeypatch):
    """Preferences govern delivery (B-014), not whether the record exists — the poller
    skipped the whole call for a muted `checkout_expired`."""
    from notifications.models import Notification, UserNotificationPreference
    from subscription.models import Payment
    from subscription.tasks import poll_open_payments

    plan = _plan("s133")
    user = _person(make_user, "fx_s133")
    UserNotificationPreference.objects.create(user=user, event_type="checkout_expired",
                                              is_enabled=False)
    c, sub = _subscribe(api, user, plan)
    reference = _checkout(c, sub).json()["reference"]
    payment = Payment.objects.get(gateway_transaction_reference=reference)
    Payment.objects.filter(pk=payment.pk).update(created_at=timezone.now() - timedelta(hours=3))
    _no_transactions(monkeypatch)

    assert poll_open_payments()["expired"] == 1
    assert Notification.objects.filter(recipient=user, event_type="checkout_expired",
                                       related_object_id=str(payment.pk)).exists(), \
        "muting the push also erased the record"


# ============================================================ S-134 the orphan payment
def test_s134_choosing_a_plan_writes_no_payment_in_the_plans_currency(make_user, api):
    """POST /subscriptions/ wrote a pending payment at the plan's price in the PLAN'S
    currency, while a payment is charged in the buyer's."""
    from subscription.models import Payment
    from wallet.models import currency_for

    _rate("13000")
    plan = _plan("s134")
    user = _person(make_user, "fx_s134", "USD")
    _c, sub = _subscribe(api, user, plan)
    wrong = Payment.objects.filter(subscription=sub).exclude(currency=currency_for(user))
    assert not wrong.exists(), list(wrong.values_list("amount", "currency", "status"))


# ============================================================ S-135 the refusals
def test_s135_renew_and_pay_list_their_codes_and_answer_them(make_user, api):
    from subscription.models import Subscription
    from subscription.views import PaymentGatewayView, SubscriptionViewSet

    renew_doc = SubscriptionViewSet.renew.__doc__
    for code in ("app_channel_required", "not_available_on_channel", "not_renewable",
                 "exchange_rate_stale", "exchange_rate_missing", "currency_not_chargeable",
                 "gateway_error"):
        assert code in renew_doc, code
    pay_doc = PaymentGatewayView.post.__doc__
    for code in ("exchange_rate_stale", "exchange_rate_missing", "gateway_unavailable",
                 "currency_not_chargeable", "price_changed", "subscription_active",
                 "not_found", "payment_not_required"):
        assert code in pay_doc, code

    # And the refusal the doc names is the one the app gets.
    plan = _plan("s135")
    user = _person(make_user, "fx_s135")
    sub = Subscription.objects.create(user=user, plan=plan, status="pending",
                                      end_date=timezone.now() + timedelta(days=30))
    resp = _post(api(user), f"/api/subscription/v1/subscriptions/{sub.id}/renew/",
                 {"gateway": "shamcash"})
    assert resp.status_code == 400, resp.content
    assert resp.json().get("code") == "not_renewable", resp.content


# ============================================================ S-136 the quoted price
def test_s136_the_quote_is_held_to_what_the_returned_checkout_charges(
        make_user, api, shamcash, dollars_chargeable):
    """The quote was compared with a price computed fresh, while an already-open
    checkout came back at the rate it was opened at: the app's figure passed and the
    reference it was handed charged another."""
    _rate("13000", ago=timedelta(hours=2))
    plan = _plan("s136")                                # 13,000 SYP = 1.00 USD today
    user = _person(make_user, "fx_s136", "USD")
    c, sub = _subscribe(api, user, plan)
    opened = _checkout(c, sub)
    assert opened.status_code == 200, opened.content
    assert (opened.json()["amount"], opened.json()["currency"]) == ("1.00", "USD")

    _rate("6500", ago=timedelta(minutes=1))              # the plan now shows 2.00 USD
    stale_quote = _checkout(c, sub, amount="2.00", currency="USD")
    assert stale_quote.status_code == 409, stale_quote.content
    assert stale_quote.json()["code"] == "price_changed"
    assert stale_quote.json()["expected_amount"] == "1.00", stale_quote.content

    honest = _checkout(c, sub, amount="1.00", currency="USD")
    assert honest.status_code == 200, honest.content
    assert honest.json()["reference"] == opened.json()["reference"]
    assert honest.json()["amount"] == "1.00"


# ============================================================ S-137 the receipt
def test_s137_the_receipt_contract_shows_the_conversion():
    from subscription.views import PaymentReceiptView

    doc = PaymentReceiptView.__doc__
    for key in ('"exchange"', "price_of_record", "price_of_record_currency", "rate_text"):
        assert key in doc, key


# ============================================================ S-078 a new rate
def test_s078_a_new_rate_reaches_the_plan_list_at_once(make_user, api):
    _rate("13000", ago=timedelta(hours=2))
    plan = _plan("s078")
    reader = _person(make_user, "fx_s078", "USD")
    c = api(reader)
    assert _plan_row(c, plan)["price"] == "1.00"

    _rate("6500", ago=timedelta(minutes=1))
    assert _plan_row(c, plan)["price"] == "2.00", "the list kept the old rate's price"


# ============================================================ S-139 a build's flags
def _apk_row():
    from policy.defaults import DEFAULT_FLAGS
    from policy.models import ChannelCapabilities

    row = ChannelCapabilities.objects.filter(channel="apk", min_version="0.0.0").first()
    if row is None:
        row = ChannelCapabilities(channel="apk", min_version="0.0.0", **DEFAULT_FLAGS["apk"])
    row.can_show_prices = True
    row.save()
    return row


def test_s139_withdrawing_prices_from_a_build_reaches_the_plan_list_at_once(make_user, api):
    plan = _plan("s139")
    reader = _person(make_user, "fx_s139")
    row = _apk_row()
    c = api(reader)
    assert "price" in _plan_row(c, plan, HTTP_X_APP_CHANNEL="apk")

    row.can_show_prices = False
    row.save()
    assert "price" not in _plan_row(c, plan, HTTP_X_APP_CHANNEL="apk"), \
        "the build kept being sent prices after they were withdrawn"


def test_s139_withdrawing_prices_from_a_build_reaches_its_home_at_once(make_user, api):
    from routine.models import Exercise, RoutineTemplate, RoutineTemplateExercise

    trainer = make_user("fx_s139_t", user_type="trainer")
    template = RoutineTemplate.objects.create(
        name="fx ready", description="ready", goal="Strength", days=3,
        created_by=trainer, is_public=True)
    RoutineTemplateExercise.objects.create(
        template=template, exercise=Exercise.objects.create(name="fx ex", created_by=trainer),
        day=1, order=1, sets=3, reps=10)
    person = _person(make_user, "fx_s139_home")
    row = _apk_row()
    c = api(person)

    def types():
        resp = c.get("/api/v1/home", HTTP_X_APP_CHANNEL="apk")
        assert resp.status_code == 200, resp.content
        return [s["type"] for s in resp.json()["sections"]]

    assert "ready_programs" in types()
    row.can_show_prices = False
    row.save()
    assert "ready_programs" not in types(), "home kept a selling section after prices went"
