"""Road to 10 S2.13, the owner's D19 (2026-11-22): a plan paid from the wallet.

"users can charge their wallets and use them to pay to subscribe in app features", never
inside a store build. `POST subscription/v1/subscriptions/{id}/pay-from-wallet/` takes the
plan's price in the payer's own currency out of their wallet for the platform (100%, the
owner's rule of 2026-09-19) and completes the payment at once: a receipt like any plan
payment, a refund that puts back exactly what was taken, a retry that never charges
twice. `play` and `ios` are refused by the check that already locks plan purchases there.
`GET v1/gateways/` names the wallet as a way to pay where it is one.
See `subscription/services/payment_service.py` `pay_from_wallet`.
"""
from datetime import timedelta
from decimal import Decimal

import pytest
from django.utils import timezone

pytestmark = pytest.mark.django_db

JSON = "application/json"
D = Decimal
GATEWAYS = "/api/subscription/v1/gateways/"


@pytest.fixture
def rate(db):
    from wallet.models import ExchangeRate

    return ExchangeRate.objects.create(base="USD", quote="SYP", rate=D("13000.000000"),
                                       effective_from=timezone.now() - timedelta(hours=1),
                                       note="s213")


def _plan(tag, price="2500.00", currency="SYP", **kw):
    from django.utils import translation

    from subscription.models import SubscriptionPlan

    defaults = dict(plan_type="premium", description="d", price=D(price), currency=currency,
                    duration_days=30, has_diet_access=True, has_ai_advice=True,
                    max_meals_per_day=5, max_routines=10, is_active=True)
    defaults.update(kw)
    with translation.override("en"):
        return SubscriptionPlan.objects.create(name=f"s213 {tag}", **defaults)


def _person(make_user, name, currency="SYP", funds="10000.00"):
    from wallet.models import Wallet, set_currency_for

    user = make_user(name)
    set_currency_for(user, currency, "client")
    wallet = Wallet.objects.get(owner=user)
    wallet.balance = D(funds)
    wallet.save(update_fields=["balance"])
    return user


def _subscription(user, plan, *, status="pending", ends_in=timedelta(days=30), **kw):
    from subscription.models import Subscription

    end = timezone.now() + ends_in
    return Subscription.objects.create(user=user, plan=plan, status=status,
                                       start_date=min(timezone.now(), end - timedelta(days=30)),
                                       end_date=end, **kw)


def _pay(api, user, sub, key="k-1", amount="2500.00", currency="SYP", **body):
    """The Review screen's Pay: its key and the exact figure it showed."""
    headers = {k: body.pop(k) for k in list(body) if k.startswith("HTTP_")}
    return api(user).post(f"/api/subscription/v1/subscriptions/{sub.pk}/pay-from-wallet/",
                          {"idempotency_key": key, "amount": amount, "currency": currency,
                           **body}, content_type=JSON, **headers)


def _balance(user):
    from wallet.models import Wallet

    return Wallet.objects.get(owner=user).balance


def _platform(currency="SYP"):
    from wallet.utils import get_platform_revenue_wallet

    return get_platform_revenue_wallet(currency).balance


def _legs(payment_id):
    from wallet.models import Transaction

    return list(Transaction.objects.filter(metadata__original_payment_id=str(payment_id))
                .order_by("id"))


# ------------------------------------------------------------------ the payment
def test_a_plan_is_paid_from_the_wallet_at_once(make_user, api,
                                               django_capture_on_commit_callbacks):
    from notifications.models import Notification
    from subscription.models import Payment, PaymentEvent

    user = _person(make_user, "s213_pay")
    sub = _subscription(user, _plan("pay"))
    platform_before = _platform()
    with django_capture_on_commit_callbacks(execute=True):
        resp = _pay(api, user, sub)
    assert resp.status_code == 200, resp.content
    body = resp.json()
    assert (body["status"], body["replayed"], body["kind"], body["payment_method"],
            body["money_type"], body["amount"], body["currency"], body["balance"]) == \
        ("completed", False, "initial", "wallet", "PLATFORM_SUB", "2500.00", "SYP", "7500.00")
    assert body["exchange_rate"] is None and body["invoice_number"].startswith("INV-")

    payment = Payment.objects.get(pk=body["payment_id"])
    sub.refresh_from_db()
    assert (sub.status, sub.source, sub.is_active) == ("active", "agent_wallet", True)
    assert payment.covers_until - payment.covers_from == timedelta(days=30)
    assert payment.payout_settled_at is not None and payment.completed_at is not None
    # The whole price to the platform, in one ledger row; nothing minted.
    assert _balance(user) == D("7500.00")
    assert _platform() - platform_before == D("2500.00")
    (leg,) = _legs(payment.pk)
    assert (leg.tx_type, leg.amount, leg.currency, leg.money_type) == \
        ("transfer", D("2500.00"), "SYP", "PLATFORM_SUB")
    assert leg.metadata["purpose"] == "subscription_revenue"
    assert body["wallet_reference"] == leg.reference_id
    steps = list(PaymentEvent.objects.filter(payment=payment).order_by("id")
                 .values_list("from_status", "to_status"))
    assert steps == [("", "pending"), ("pending", "completed")]
    # A receipt like any plan payment, and the notice.
    receipt = api(user).get(body["receipt_url"])
    assert receipt.status_code == 200, receipt.content
    assert (receipt.json()["receipt"]["method"]["code"],
            receipt.json()["receipt"]["amount"]) == ("wallet", "2500.00")
    assert Notification.objects.filter(recipient=user, event_type="payment_confirmed").exists()


def test_a_retry_with_the_same_key_charges_once(make_user, api):
    from subscription.models import Payment

    user = _person(make_user, "s213_retry")
    sub = _subscription(user, _plan("retry"))
    first = _pay(api, user, sub, key="tap-1")
    again = _pay(api, user, sub, key="tap-1")
    assert (first.status_code, again.status_code) == (200, 200)
    assert again.json()["replayed"] is True
    assert again.json()["payment_id"] == first.json()["payment_id"]
    assert _balance(user) == D("7500.00")
    assert Payment.objects.filter(subscription=sub).count() == 1
    assert len(_legs(first.json()["payment_id"])) == 1


def test_a_renewal_follows_the_cover_already_held(make_user, api):
    user = _person(make_user, "s213_renew")
    sub = _subscription(user, _plan("renew"), status="active", ends_in=timedelta(days=10))
    end_before = sub.end_date
    resp = _pay(api, user, sub)
    assert resp.status_code == 200, resp.content
    assert resp.json()["kind"] == "renewal"
    sub.refresh_from_db()
    assert sub.end_date == end_before + timedelta(days=30)


# ------------------------------------------------------------------ builds
@pytest.mark.parametrize("channel", ["play", "ios"])
def test_a_store_build_is_refused_before_anything_moves(make_user, api, channel):
    from subscription.models import Payment

    user = _person(make_user, f"s213_{channel}")
    sub = _subscription(user, _plan(channel))
    resp = _pay(api, user, sub, HTTP_X_APP_CHANNEL=channel)
    assert (resp.status_code, resp.json()["code"]) == (403, "not_available_on_channel")
    assert _balance(user) == D("10000.00")
    assert not Payment.objects.filter(subscription=sub).exists()
    listed = api(user).get(GATEWAYS, HTTP_X_APP_CHANNEL=channel).json()
    assert (listed["can_pay_from_wallet"], listed["wallet"], listed["gateways"]) == \
        (False, None, [])


def test_no_channel_is_refused(make_user, api):
    user = _person(make_user, "s213_nochan")
    sub = _subscription(user, _plan("nochan"))
    resp = _pay(api, user, sub, HTTP_X_APP_CHANNEL="")
    assert (resp.status_code, resp.json()["code"]) == (400, "app_channel_required")
    assert _balance(user) == D("10000.00")


@pytest.mark.parametrize("channel", ["apk", "web"])
def test_the_gateway_list_names_the_wallet_where_it_is_a_way_to_pay(make_user, api, channel):
    user = _person(make_user, f"s213_gw_{channel}", funds="1234.00")
    listed = api(user).get(GATEWAYS, HTTP_X_APP_CHANNEL=channel).json()
    assert listed["can_pay_from_wallet"] is True
    wallet = listed["wallet"]
    assert (wallet["name"], wallet["kind"], wallet["available"], wallet["balance"],
            wallet["currency"]) == ("wallet", "wallet", True, "1234.00", "SYP")
    assert wallet["pay_path"].endswith("/pay-from-wallet/")
    assert "wallet" not in [g["name"] for g in listed["gateways"]]


# ------------------------------------------------------------------ refusals
def test_a_short_wallet_says_what_is_required_and_writes_nothing(make_user, api):
    from subscription.models import Payment
    from wallet.models import Wallet

    user = _person(make_user, "s213_short", funds="1000.00")
    sub = _subscription(user, _plan("short"))
    resp = _pay(api, user, sub, key="short-1")
    assert resp.status_code == 402, resp.content
    assert (resp.json()["code"], resp.json()["required"], resp.json()["balance"],
            resp.json()["currency"]) == ("insufficient_balance", "2500.00", "1000.00", "SYP")
    assert not Payment.objects.filter(subscription=sub).exists()
    assert _balance(user) == D("1000.00")
    # Topped up, the same key now pays: a refusal spends no key.
    Wallet.objects.filter(owner=user).update(balance=D("3000.00"))
    resp = _pay(api, user, sub, key="short-1")
    assert (resp.status_code, resp.json()["replayed"], resp.json()["balance"]) == \
        (200, False, "500.00")


def test_a_quote_that_differs_is_refused_and_nothing_moves(make_user, api):
    user = _person(make_user, "s213_quote")
    sub = _subscription(user, _plan("quote"))
    resp = _pay(api, user, sub, amount="2400.00", currency="SYP")
    assert resp.status_code == 409, resp.content
    assert (resp.json()["code"], resp.json()["expected_amount"],
            resp.json()["expected_currency"]) == ("price_changed", "2500.00", "SYP")
    assert _balance(user) == D("10000.00")
    assert _pay(api, user, sub, amount="2500.00", currency="syp").status_code == 200


def test_an_open_shamcash_checkout_blocks_a_second_way_of_paying(make_user, api):
    from subscription.models import Payment

    user = _person(make_user, "s213_open")
    sub = _subscription(user, _plan("open"))
    open_one = Payment.objects.create(subscription=sub, amount=D("2500.00"), currency="SYP",
                                      payment_method="shamcash", status="pending",
                                      gateway_transaction_reference="YG-OPEN-1")
    resp = _pay(api, user, sub)
    assert (resp.status_code, resp.json()["code"], resp.json()["payment_id"]) == \
        (409, "checkout_open", str(open_one.pk))
    assert _balance(user) == D("10000.00")


def test_the_other_refusals_name_their_codes(make_user, api):
    user = _person(make_user, "s213_codes")
    free = _subscription(user, _plan("free", price="0.00"))
    resp = _pay(api, user, free)
    assert (resp.status_code, resp.json()["code"]) == (400, "payment_not_required")

    other = _person(make_user, "s213_codes2")
    resp = _pay(api, other, free)
    assert (resp.status_code, resp.json()["code"]) == (404, "not_found")

    for body in ({}, {"idempotency_key": "k"}, {"idempotency_key": "k", "amount": "1"}):
        resp = api(user).post(f"/api/subscription/v1/subscriptions/{free.pk}/pay-from-wallet/",
                              body, content_type=JSON)
        assert (resp.status_code, resp.json()["code"]) == (400, "validation_error"), body

    trial_user = _person(make_user, "s213_trial")
    trial = _subscription(trial_user, _plan("trial"), status="trial",
                          trial_end_date=timezone.now() + timedelta(days=3))
    resp = _pay(api, trial_user, trial)
    assert (resp.status_code, resp.json()["code"]) == (409, "subscription_active")
    assert _balance(trial_user) == D("10000.00")


# ------------------------------------------------------------------ currency
def test_the_price_is_converted_once_and_the_rate_snapshotted(make_user, api, rate):
    from subscription.models import Payment

    user = _person(make_user, "s213_usd", currency="USD", funds="10.00")
    sub = _subscription(user, _plan("usd", price="26000.00", currency="SYP"))
    usd_before, syp_before = _platform("USD"), _platform("SYP")
    resp = _pay(api, user, sub, amount="2.00", currency="USD")
    assert resp.status_code == 200, resp.content
    body = resp.json()
    assert (body["amount"], body["currency"], body["price_of_record"],
            body["price_of_record_currency"], body["exchange_rate"]) == \
        ("2.00", "USD", "26000.00", "SYP", "13000.000000")
    payment = Payment.objects.get(pk=body["payment_id"])
    assert (payment.exchange_rate_id, payment.exchange_rate_value) == (rate.pk, rate.rate)
    assert _balance(user) == D("8.00")
    assert (_platform("USD") - usd_before, _platform("SYP") - syp_before) == (D("2.00"), 0)


def test_no_rate_no_sale(make_user, api):
    user = _person(make_user, "s213_norate", currency="USD", funds="10.00")
    sub = _subscription(user, _plan("norate", price="26000.00", currency="SYP"))
    resp = _pay(api, user, sub, amount="2.00", currency="USD")
    assert (resp.status_code, resp.json()["code"]) == (409, "exchange_rate_missing")
    assert _balance(user) == D("10.00")


# ------------------------------------------------------------------ refunds
def test_a_refund_puts_back_exactly_what_was_paid(make_user, api, rate):
    from subscription.services.payment_service import PaymentService
    from wallet.models import Transaction

    user = _person(make_user, "s213_refund", currency="USD", funds="10.00")
    sub = _subscription(user, _plan("refund", price="26000.00", currency="SYP"))
    payment_id = _pay(api, user, sub, amount="2.00", currency="USD").json()["payment_id"]
    platform_after_sale = _platform("USD")
    PaymentService.refund_payment(payment_id, "support")
    PaymentService.refund_payment(payment_id, "support again")   # a repeat is a no-op
    assert _balance(user) == D("10.00")
    assert platform_after_sale - _platform("USD") == D("2.00")
    reversals = Transaction.objects.filter(tx_type="reversal",
                                           metadata__original_payment_id=payment_id)
    assert reversals.count() == 1 and reversals.get().currency == "USD"
    sub.refresh_from_db()
    assert sub.is_active is False


def test_nothing_is_minted_for_a_wallet_payment_whatever_sends_the_signal(make_user):
    """A gateway payment's completion mints its top-up (`wallet.signals`). A wallet
    payment's money moved out of the wallet itself, so the receiver must mint nothing for
    it — even for a row with no ledger leg at all, which the receiver's older check (a
    transfer naming the payment) would not catch."""
    from subscription.models import Payment
    from subscription.services.payment_service import PaymentService, payment_completed
    from wallet.models import Transaction

    user = _person(make_user, "s213_mint")
    sub = _subscription(user, _plan("mint"), status="active")
    payment = Payment.objects.create(subscription=sub, amount=D("2500.00"), currency="SYP",
                                     payment_method="wallet", status="completed",
                                     completed_at=timezone.now())
    before = Transaction.objects.count()
    payment_completed.send(sender=PaymentService, payment=payment, subscription=sub)
    assert Transaction.objects.count() == before, "nothing minted, nothing moved"
    assert _balance(user) == D("10000.00")
    payment.refresh_from_db()
    assert payment.payout_settled_at is not None


def test_a_second_tap_with_a_fresh_key_is_refused_not_charged(make_user, api):
    from subscription.models import Payment

    user = _person(make_user, "s213_twotaps")
    sub = _subscription(user, _plan("twotaps"))
    first = _pay(api, user, sub, key="tap-a")
    second = _pay(api, user, sub, key="tap-b")
    assert first.status_code == 200, first.content
    assert (second.status_code, second.json()["code"], second.json()["payment_id"]) == \
        (409, "already_paid", first.json()["payment_id"])
    assert second.json()["end_date"] is not None
    assert _balance(user) == D("7500.00")
    assert Payment.objects.filter(subscription=sub).count() == 1


def test_the_operator_cannot_reverse_the_payment_row_alone(make_user, api):
    from subscription.models import Payment

    user = _person(make_user, "s213_rev")
    admin = make_user("s213_rev_admin", user_type="admin")
    sub = _subscription(user, _plan("rev"))
    body = _pay(api, user, sub).json()
    resp = api(admin).post("/api/wallet/admin/reversal/",
                           {"reference_id": body["wallet_reference"],
                            "idempotency_key": "rev-1", "reason": "test"},
                           content_type=JSON)
    assert (resp.status_code, resp.json()["code"], resp.json()["payment_id"]) == \
        (409, "plan_payment_leg", body["payment_id"])
    assert _balance(user) == D("7500.00")
    assert Payment.objects.get(pk=body["payment_id"]).status == "completed"


def test_the_build_is_checked_before_the_body(make_user, api):
    user = _person(make_user, "s213_order")
    sub = _subscription(user, _plan("order"))
    resp = api(user).post(f"/api/subscription/v1/subscriptions/{sub.pk}/pay-from-wallet/",
                          {}, content_type=JSON, HTTP_X_APP_CHANNEL="play")
    assert (resp.status_code, resp.json()["code"]) == (403, "not_available_on_channel")


def test_a_lapsed_plan_paid_again_starts_now(make_user, api):
    user = _person(make_user, "s213_lapsed")
    sub = _subscription(user, _plan("lapsed"), status="expired", ends_in=-timedelta(days=3))
    resp = _pay(api, user, sub)
    assert resp.status_code == 200, resp.content
    assert resp.json()["kind"] == "renewal"
    sub.refresh_from_db()
    assert sub.is_active and sub.end_date - timezone.now() > timedelta(days=29)
