"""An upgrade's difference paid from the wallet (the owner, 2026-11-24; apk and web only).

`GET subscription/v1/subscriptions/{id}/upgrade/?plan_id=&gateway=wallet` quotes it by the
upgrade rule a ShamCash upgrade uses (`subscription.upgrade.quote_upgrade`), with no
gateway minimum; `POST .../upgrade/pay-from-wallet/` takes exactly that difference out of
the payer's wallet for the platform (100%) and switches the plan at once through the
same `_apply_upgrade`. A retry never charges twice, a refund puts back exactly what was
paid, and `play`/`ios` are refused by the check that locks every plan purchase there.
See `subscription/services/payment_service.py` `pay_upgrade_from_wallet`.
"""
from datetime import timedelta
from decimal import Decimal

import pytest
from django.utils import timezone

from tests.test_road10_s213_wallet_plan import (  # noqa: F401
    _balance, _legs, _person, _plan, _platform, _subscription, rate)

pytestmark = pytest.mark.django_db

JSON = "application/json"
D = Decimal
SUBS = "/api/subscription/v1/subscriptions/"


def _running(make_user, name, basic, *, funds="40000.00", days_left=20, currency="SYP"):
    """A subscriber on `basic`, paid from the wallet, `days_left` whole days and an hour
    from the end of that period."""
    from subscription.models import Subscription
    from subscription.services.payment_service import PaymentService

    user = _person(make_user, name, currency=currency, funds=funds)
    sub = _subscription(user, basic)
    first = PaymentService.pay_from_wallet(sub, idempotency_key=f"{name}-basic")
    assert first.status == "completed"
    if days_left is not None:
        Subscription.objects.filter(pk=sub.pk).update(
            end_date=timezone.now() + timedelta(days=days_left, hours=1))
    return user, Subscription.objects.get(pk=sub.pk), first


def _quote(api, user, sub, plan, gateway="wallet", **headers):
    return api(user).get(f"{SUBS}{sub.pk}/upgrade/",
                         {"plan_id": plan.pk, "gateway": gateway}, **headers)


def _upgrade(api, user, sub, plan, key="up-1", amount="13333.33", currency="SYP",
             **headers):
    return api(user).post(f"{SUBS}{sub.pk}/upgrade/pay-from-wallet/",
                          {"plan_id": plan.pk, "idempotency_key": key, "amount": amount,
                           "currency": currency}, content_type=JSON, **headers)


def _plans(tag, basic="10000.00", premium="20000.00", premium_days=45, **premium_kw):
    return (_plan(f"{tag} basic", price=basic, has_ai_advice=False),
            _plan(f"{tag} premium", price=premium, duration_days=premium_days,
                  has_ai_advice=True, **premium_kw))


# ------------------------------------------------------------------ the upgrade
def test_the_difference_is_paid_from_the_wallet_and_the_new_plan_starts_at_once(
        make_user, api, django_capture_on_commit_callbacks):
    from notifications.models import Notification
    from subscription.models import Payment, PaymentEvent

    basic, premium = _plans("wu pay")
    user, sub, first = _running(make_user, "wu_pay", basic)
    balance_before, platform_before = _balance(user), _platform()

    quoted = _quote(api, user, sub, premium)
    assert quoted.status_code == 200, quoted.content
    q = quoted.json()
    # 10000 paid for 30 days; 20 whole days left (the hour under way is used).
    assert (q["credit"], q["new_price"], q["to_pay"], q["days_left"]) == \
        ("6666.67", "20000.00", "13333.33", 20)

    with django_capture_on_commit_callbacks(execute=True):
        resp = _upgrade(api, user, sub, premium, amount=q["to_pay"])
    assert resp.status_code == 200, resp.content
    body = resp.json()
    assert (body["status"], body["replayed"], body["kind"], body["payment_method"],
            body["money_type"], body["amount"], body["currency"]) == \
        ("completed", False, "upgrade", "wallet", "PLATFORM_SUB", "13333.33", "SYP")
    assert body["upgrade"]["credit"] == "6666.67" and body["upgrade"]["to_pay"] == "13333.33"
    assert body["exchange_rate"] is None and body["invoice_number"].startswith("INV-")
    assert body["balance"] == str(balance_before - D("13333.33"))

    # Exactly the difference, to the platform, in one ledger row; nothing minted.
    payment = Payment.objects.get(pk=body["payment_id"])
    assert _balance(user) == balance_before - D("13333.33")
    assert _platform() - platform_before == D("13333.33")
    (leg,) = _legs(payment.pk)
    assert (leg.tx_type, leg.amount, leg.currency, leg.money_type) == \
        ("transfer", D("13333.33"), "SYP", "PLATFORM_SUB")
    assert body["wallet_reference"] == leg.reference_id
    assert payment.payout_settled_at is not None

    # The plan switched now, for the new plan's FULL length, as a gateway upgrade does.
    sub.refresh_from_db()
    assert (sub.plan_id, sub.has_ai_advice, sub.status, sub.source) == \
        (premium.pk, True, "active", "agent_wallet")
    assert payment.covers_until - payment.covers_from == timedelta(days=45)
    assert body["subscription"]["plan_id"] == premium.pk
    # The price of record is the new plan's; the credited payment left the run.
    assert (payment.price_amount, payment.price_currency) == (D("20000.00"), "SYP")
    first.refresh_from_db()
    assert first.metadata.get("credited_into") == str(payment.pk)
    assert payment.metadata["upgrade"]["switch"]["credit_applied"] == "6666.67"

    steps = list(PaymentEvent.objects.filter(payment=payment).order_by("id")
                 .values_list("from_status", "to_status"))
    assert steps == [("", "pending"), ("pending", "completed")]
    receipt = api(user).get(body["receipt_url"])
    assert receipt.status_code == 200, receipt.content
    assert (receipt.json()["receipt"]["method"]["code"],
            receipt.json()["receipt"]["amount"]) == ("wallet", "13333.33")
    assert Notification.objects.filter(recipient=user, event_type="payment_confirmed",
                                       related_object_id=str(payment.pk)).exists()


def test_the_wallet_quote_is_the_gateway_upgrades_rule_without_its_minimum(make_user, api):
    """The same credit and the same `to_pay` as the ShamCash quote; only the gateway's
    floor (100 SYP) is not a wallet's. Two Basic months stacked leave 59 whole days."""
    from subscription.models import Payment, Subscription
    from subscription.services.payment_service import PaymentService

    basic, premium = _plans("wu rule", premium="19700.00", premium_days=30)
    user, sub, first = _running(make_user, "wu_rule", basic, days_left=None)
    # A renewal stacked behind it (the repeat window is about taps, not this test).
    Payment.objects.filter(pk=first.pk).update(
        completed_at=timezone.now() - timedelta(minutes=11))
    PaymentService.pay_from_wallet(Subscription.objects.get(pk=sub.pk),
                                   idempotency_key="wu_rule-renewal")
    sub = Subscription.objects.get(pk=sub.pk)

    by_wallet = _quote(api, user, sub, premium).json()
    assert (by_wallet["credit"], by_wallet["to_pay"]) == ("19666.67", "33.33"), by_wallet
    by_gateway = _quote(api, user, sub, premium, gateway="shamcash")
    assert (by_gateway.status_code, by_gateway.json()["code"],
            by_gateway.json()["to_pay"], by_gateway.json()["minimum"]) == \
        (409, "upgrade_not_payable", "33.33", "100")

    resp = _upgrade(api, user, sub, premium, amount="33.33")
    assert resp.status_code == 200, resp.content
    assert Subscription.objects.get(pk=sub.pk).plan_id == premium.pk


def test_a_credit_that_covers_the_new_price_is_not_payable_from_the_wallet_either(
        make_user, api):
    from subscription.models import Payment, Subscription
    from subscription.services.payment_service import PaymentService

    basic, premium = _plans("wu cover", premium="15000.00", premium_days=30)
    user, sub, first = _running(make_user, "wu_cover", basic, days_left=None)
    Payment.objects.filter(pk=first.pk).update(
        completed_at=timezone.now() - timedelta(minutes=11))
    PaymentService.pay_from_wallet(Subscription.objects.get(pk=sub.pk),
                                   idempotency_key="wu_cover-renewal")
    balance = _balance(user)
    for resp in (_quote(api, user, sub, premium),
                 _upgrade(api, user, sub, premium, amount="0.00")):
        assert (resp.status_code, resp.json()["code"], resp.json()["to_pay"],
                resp.json()["minimum"]) == (409, "upgrade_not_payable", "0.00", None)
    assert _balance(user) == balance


def test_the_price_of_record_and_the_rate_are_snapshotted(make_user, api, rate):
    from subscription.models import Payment

    basic = _plan("wu fx basic", price="10000.00")
    premium = _plan("wu fx premium", price="2.00", currency="USD", duration_days=30)
    user, sub, _first = _running(make_user, "wu_fx", basic)
    q = _quote(api, user, sub, premium).json()
    # 2 USD at 13000 = 26000 SYP, less the 6666.67 credit.
    assert (q["new_price"], q["to_pay"], q["currency"]) == ("26000.00", "19333.33", "SYP")
    body = _upgrade(api, user, sub, premium, amount="19333.33").json()
    assert (body["amount"], body["price_of_record"], body["price_of_record_currency"],
            body["exchange_rate"]) == ("19333.33", "2.00", "USD", "13000.000000"), body
    payment = Payment.objects.get(pk=body["payment_id"])
    assert payment.exchange_rate_id == rate.pk


# ------------------------------------------------------------------ retries
def test_a_retry_with_the_same_key_charges_once(make_user, api):
    from subscription.models import Payment

    basic, premium = _plans("wu retry")
    user, sub, _first = _running(make_user, "wu_retry", basic)
    first = _upgrade(api, user, sub, premium)
    balance = _balance(user)
    again = _upgrade(api, user, sub, premium)
    assert first.status_code == again.status_code == 200, (first.content, again.content)
    assert again.json()["payment_id"] == first.json()["payment_id"]
    assert (first.json()["replayed"], again.json()["replayed"]) == (False, True)
    assert again.json()["upgrade"]["to_pay"] == "13333.33"
    assert _balance(user) == balance
    assert len(_legs(first.json()["payment_id"])) == 1
    assert Payment.objects.filter(subscription=sub, metadata__kind="upgrade").count() == 1


def test_a_second_tap_with_a_fresh_key_finds_the_plan_switched(make_user, api):
    basic, premium = _plans("wu fresh")
    user, sub, _first = _running(make_user, "wu_fresh", basic)
    assert _upgrade(api, user, sub, premium, key="a").status_code == 200
    balance = _balance(user)
    second = _upgrade(api, user, sub, premium, key="b")
    assert (second.status_code, second.json()["code"]) == (409, "not_an_upgrade")
    assert _balance(user) == balance


def test_a_key_that_paid_something_else_is_refused_not_replayed(make_user, api):
    from subscription.models import Subscription

    basic, premium = _plans("wu reuse")
    user, sub, first = _running(make_user, "wu_reuse", basic)
    balance = _balance(user)
    resp = _upgrade(api, user, sub, premium, key="wu_reuse-basic")   # the Pay tap's key
    assert (resp.status_code, resp.json()["code"], resp.json()["payment_id"]) == \
        (422, "idempotency_key_reused", str(first.pk))
    assert _balance(user) == balance
    assert Subscription.objects.get(pk=sub.pk).plan_id == basic.pk


# ------------------------------------------------------------------ refusals
@pytest.mark.parametrize("channel", ["play", "ios"])
def test_a_store_build_is_refused_before_anything_moves(make_user, api, channel):
    basic, premium = _plans(f"wu {channel}")
    user, sub, _first = _running(make_user, f"wu_{channel}", basic)
    balance = _balance(user)
    for resp in (_quote(api, user, sub, premium, HTTP_X_APP_CHANNEL=channel),
                 _upgrade(api, user, sub, premium, HTTP_X_APP_CHANNEL=channel)):
        assert (resp.status_code, resp.json()["code"]) == (403, "not_available_on_channel")
    assert _balance(user) == balance
    listed = api(user).get("/api/subscription/v1/gateways/",
                           HTTP_X_APP_CHANNEL=channel).json()
    assert listed["wallet"] is None


def test_the_build_is_checked_before_the_body(make_user, api):
    basic, _premium = _plans("wu body")
    user, sub, _first = _running(make_user, "wu_body", basic)
    resp = api(user).post(f"{SUBS}{sub.pk}/upgrade/pay-from-wallet/", {},
                          content_type=JSON, HTTP_X_APP_CHANNEL="play")
    assert (resp.status_code, resp.json()["code"]) == (403, "not_available_on_channel")
    missing = api(user).post(f"{SUBS}{sub.pk}/upgrade/pay-from-wallet/", {},
                             content_type=JSON)
    assert (missing.status_code, missing.json()["code"]) == (400, "validation_error")


def test_a_figure_nobody_saw_is_refused_and_nothing_moves(make_user, api):
    basic, premium = _plans("wu quote")
    user, sub, _first = _running(make_user, "wu_quote", basic)
    balance = _balance(user)
    for amount, currency in (("13000.00", "SYP"), ("13333.33", "USD")):
        resp = _upgrade(api, user, sub, premium, amount=amount, currency=currency)
        assert (resp.status_code, resp.json()["code"], resp.json()["expected_amount"],
                resp.json()["expected_currency"]) == \
            (409, "price_changed", "13333.33", "SYP")
    assert _balance(user) == balance


def test_a_short_wallet_says_what_is_required_and_writes_nothing(make_user, api):
    from subscription.models import Payment

    basic, premium = _plans("wu short")
    user, sub, _first = _running(make_user, "wu_short", basic, funds="12000.00")
    resp = _upgrade(api, user, sub, premium)
    assert (resp.status_code, resp.json()["code"], resp.json()["required"],
            resp.json()["balance"], resp.json()["currency"]) == \
        (402, "insufficient_balance", "13333.33", "2000.00", "SYP")
    assert not Payment.objects.filter(subscription=sub, metadata__kind="upgrade").exists()


def test_an_open_shamcash_checkout_blocks_paying_from_the_wallet(make_user, api):
    from subscription.models import Payment

    basic, premium = _plans("wu open")
    user, sub, _first = _running(make_user, "wu_open", basic)
    open_one = Payment.objects.create(subscription=sub, amount=D("13333.33"), currency="SYP",
                                      payment_method="shamcash", status="pending",
                                      gateway_transaction_reference="YG-WU-OPEN",
                                      metadata={"kind": "upgrade"})
    balance = _balance(user)
    resp = _upgrade(api, user, sub, premium)
    assert (resp.status_code, resp.json()["code"], resp.json()["payment_id"]) == \
        (409, "checkout_open", str(open_one.pk))
    assert _balance(user) == balance


def test_the_upgrade_rules_refusals_name_their_codes(make_user, api):
    basic, premium = _plans("wu codes")
    cheaper = _plan("wu codes cheaper", price="5000.00")
    user, sub, _first = _running(make_user, "wu_codes", basic)
    down = _upgrade(api, user, sub, cheaper, amount="0.01")
    assert (down.status_code, down.json()["code"]) == (409, "not_an_upgrade")
    waiting = _person(make_user, "wu_codes_waiting")
    idle = _subscription(waiting, basic)       # pending: nothing running
    none = _upgrade(api, waiting, idle, premium)
    assert (none.status_code, none.json()["code"]) == (409, "no_running_cover")
    gone = api(user).post(f"{SUBS}{sub.pk}/upgrade/pay-from-wallet/",
                          {"plan_id": 999999, "idempotency_key": "x", "amount": "1.00",
                           "currency": "SYP"}, content_type=JSON)
    assert (gone.status_code, gone.json()["code"]) == (400, "invalid_plan")
    other = _person(make_user, "wu_codes_other")
    assert _upgrade(api, other, sub, premium).status_code == 404


def test_the_gateway_list_says_where_an_upgrade_is_paid_from_the_wallet(make_user, api):
    user = _person(make_user, "wu_list")
    wallet = api(user).get("/api/subscription/v1/gateways/", HTTP_X_APP_CHANNEL="apk") \
        .json()["wallet"]
    assert wallet["upgrade_path"] == \
        "/api/subscription/v1/subscriptions/{id}/upgrade/pay-from-wallet/"


# ------------------------------------------------------------------ refunds
def test_a_refund_puts_back_exactly_what_was_paid(make_user, api):
    from subscription.models import Subscription
    from subscription.services.payment_service import PaymentService
    from wallet.models import Transaction

    basic, premium = _plans("wu refund")
    user, sub, _first = _running(make_user, "wu_refund", basic)
    body = _upgrade(api, user, sub, premium).json()
    after_sale, platform_after_sale = _balance(user), _platform()
    end_after_sale = Subscription.objects.get(pk=sub.pk).end_date

    PaymentService.refund_payment(body["payment_id"], "support")
    PaymentService.refund_payment(body["payment_id"], "support again")   # a no-op
    assert _balance(user) - after_sale == D("13333.33")
    assert platform_after_sale - _platform() == D("13333.33")
    reversal = Transaction.objects.get(tx_type="reversal",
                                       metadata__original_payment_id=body["payment_id"])
    assert (reversal.amount, reversal.currency) == (D("13333.33"), "SYP")
    # The money's share of the 45 days goes; the credited days stay (as for a gateway
    # upgrade): 13333.33 of the 20000.00 the period was worth.
    sub = Subscription.objects.get(pk=sub.pk)
    taken = end_after_sale - sub.end_date
    assert abs(taken - timedelta(days=45) * (13333.33 / 20000)) < timedelta(minutes=1), taken
    assert sub.is_active


# ------------------------------------------------------------------ the review's findings
def test_pay_from_wallet_does_not_replay_an_upgrades_key(make_user, api):
    """An upgrade's key sent to the Pay button would have been answered as a first
    payment or renewal that never happened."""
    basic, premium = _plans("wu key")
    user, sub, _first = _running(make_user, "wu_key", basic)
    upgrade = _upgrade(api, user, sub, premium, key="shared-key").json()
    balance = _balance(user)
    resp = api(user).post(f"{SUBS}{sub.pk}/pay-from-wallet/",
                          {"idempotency_key": "shared-key", "amount": "20000.00",
                           "currency": "SYP"}, content_type=JSON)
    assert (resp.status_code, resp.json()["code"], resp.json()["payment_id"]) == \
        (422, "idempotency_key_reused", upgrade["payment_id"])
    assert _balance(user) == balance


def test_a_paid_upgrade_is_still_answered_after_its_plan_leaves_sale(make_user, api):
    from subscription.models import SubscriptionPlan

    basic, premium = _plans("wu off")
    user, sub, _first = _running(make_user, "wu_off", basic)
    paid = _upgrade(api, user, sub, premium, key="k-off").json()
    SubscriptionPlan.objects.filter(pk=premium.pk).update(is_active=False)
    again = _upgrade(api, user, sub, premium, key="k-off")
    assert (again.status_code, again.json()["payment_id"], again.json()["replayed"]) == \
        (200, paid["payment_id"], True)
    other_basic, off_sale = _plans("wu off2")
    user2, sub2, _f2 = _running(make_user, "wu_off2", other_basic)
    SubscriptionPlan.objects.filter(pk=off_sale.pk).update(is_active=False)
    fresh = _upgrade(api, user2, sub2, off_sale, key="k-new")
    assert (fresh.status_code, fresh.json()["code"]) == (400, "invalid_plan")


def test_the_credited_payments_are_locked_before_the_wallets(make_user, api):
    """The platform's order is subscription -> payment -> user row -> wallets; the switch
    marks the credited payments, so they are locked before the wallets are."""
    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    basic, premium = _plans("wu order")
    user, sub, _first = _running(make_user, "wu_order", basic)
    with CaptureQueriesContext(connection) as queries:
        assert _upgrade(api, user, sub, premium).status_code == 200
    sqls = [q["sql"] for q in queries.captured_queries]
    locked = [i for i, sql in enumerate(sqls) if "FOR UPDATE" in sql]
    payment = next(i for i in locked if 'FROM "subscription_payment"' in sqls[i])
    wallet = next(i for i in locked if 'FROM "wallet_wallet"' in sqls[i])
    subscription = next(i for i in locked if 'FROM "subscription_subscription"' in sqls[i])
    assert subscription < payment < wallet, (sqls[subscription][:120], sqls[payment][:120],
                                             sqls[wallet][:120])
    assert 'JOIN "subscription_subscriptionplan"' not in sqls[subscription]
