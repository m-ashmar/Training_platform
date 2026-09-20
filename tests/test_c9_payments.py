"""Cycle 9: payments — what a payment buys, how many checkouts a subscription can have
open, what a refund takes back, and who gets paid when a receiver fails once.

Each test is written from a reproduction against the code before the fix. The measured
failure is in the docstring; the assertion is the behaviour a paying customer is owed.
Tests that need real threads live in `tests/test_zz_c9_payments.py`.
"""
import hashlib
import hmac
import json
import time
from datetime import timedelta
from decimal import Decimal

import pytest
from django.utils import timezone


# --------------------------------------------------------------------------- helpers
WEBHOOK_SECRET = "c9-payments-webhook-secret-0123456789"


@pytest.fixture
def shamcash():
    """ShamCash in its default transfer/reconcile mode, with a webhook secret.

    The registry is built from the environment at import and the test environment sets
    none of it, so without this a checkout is (correctly) refused and every webhook is
    rejected as unsigned.
    """
    from subscription.settings import gateway_config

    config = gateway_config.GATEWAY_REGISTRY["shamcash"]["config"][gateway_config.GATEWAY_MODE]
    before = dict(config)
    config["merchant_id"] = "C9-ACCOUNT-1"
    config["webhook_secret"] = WEBHOOK_SECRET
    config["initiate_path"] = ""
    try:
        yield config
    finally:
        config.clear()
        config.update(before)


def _plan(tag, price="5000.00", duration_days=30, **flags):
    from django.utils import translation

    from subscription.models import SubscriptionPlan

    with translation.override("en"):
        return SubscriptionPlan.objects.create(
            name=f"c9pay {tag}", plan_type="premium", description="c9",
            price=Decimal(price), currency="SYP", duration_days=duration_days,
            is_active=True, **flags)


def _client_and_trainer(make_user, tag):
    """A client with an approved trainer. The trainer is created FIRST, so the trainer's
    wallet has the lower id — the ordinary case, and the one that deadlocked."""
    from users.models import TrainerClientRelation

    trainer = make_user(f"c9pay_{tag}_t", user_type="trainer")
    client = make_user(f"c9pay_{tag}_c", user_type="client", phone_number="0933000000")
    TrainerClientRelation.objects.create(trainer=trainer, client=client, status="approved")
    return client, trainer


def _signed_webhook(reference, status="completed", amount="5000.00", currency="SYP",
                    event_id=None):
    from django.test import Client

    body = json.dumps({"data": {
        "status": status, "amount": amount, "currency": currency,
        "reference": reference, "transaction_id": event_id or f"tx-{reference}-{status}",
        "event_id": event_id or f"ev-{reference}-{status}",
    }}).encode()
    ts = str(int(time.time()))
    signature = hmac.new(WEBHOOK_SECRET.encode(), f"{ts}.".encode() + body,
                         hashlib.sha256).hexdigest()
    return Client().post(
        "/api/subscription/webhook/shamcash/", data=body, content_type="application/json",
        HTTP_X_SHAMCASH_SIGNATURE=signature, HTTP_X_SHAMCASH_TIMESTAMP=ts)


def _post(client, url, payload=None):
    return client.post(url, data=json.dumps(payload or {}), content_type="application/json")


def _subscribe(api, user, plan):
    """The real journey: POST /subscriptions/, then the Pay button."""
    from subscription.models import Subscription

    c = api(user)
    resp = _post(c, "/api/subscription/v1/subscriptions/", {"plan_id": plan.id})
    assert resp.status_code == 201, resp.content
    return c, Subscription.objects.get(user=user)


def _checkout(c, sub):
    resp = _post(c, "/api/subscription/v1/gateways/",
                 {"gateway": "shamcash", "subscription_id": str(sub.id)})
    assert resp.status_code == 200, resp.content
    return resp.json()


def _renew(c, sub):
    resp = _post(c, f"/api/subscription/v1/subscriptions/{sub.id}/renew/",
                 {"gateway": "shamcash"})
    assert resp.status_code == 200, resp.content
    return resp.json()


def _paid_and_lapsed(api, user, plan, capture):
    """A subscriber who paid once, and whose cover then ran out and was swept.

    Forty-one days pass: the subscription's window and the window its payment recorded
    move back together, as they would have if the payment had really cleared then."""
    from django.db.models import F

    from subscription.models import Payment, Subscription
    from subscription.tasks import expire_lapsed_subscriptions

    c, sub = _subscribe(api, user, plan)
    first = _checkout(c, sub)
    with capture(execute=True):
        assert _signed_webhook(first["reference"]).status_code == 200
    ago = timedelta(days=41)
    Subscription.objects.filter(pk=sub.pk).update(
        start_date=F("start_date") - ago, end_date=F("end_date") - ago)
    Payment.objects.filter(subscription=sub, status="completed").update(
        covers_from=F("covers_from") - ago, covers_until=F("covers_until") - ago)
    assert expire_lapsed_subscriptions() >= 1
    sub.refresh_from_db()
    assert sub.status == "expired" and not sub.is_active
    return c, sub


def _days_of_cover(sub):
    sub.refresh_from_db()
    return (sub.end_date - timezone.now()).total_seconds() / 86400


# ------------------------------------------------------- the first payment's period
def test_a_payment_that_clears_late_still_buys_the_whole_period(
        make_user, api, shamcash, django_capture_on_commit_callbacks):
    """`Subscription.save()` derives `end_date = start_date + duration` when the PENDING
    row is created at checkout, and activation kept that date whenever it was still in
    the future. So the clock ran from the moment the customer opened the checkout, not
    from the moment the money cleared. Measured: a checkout left ten days before paying
    delivered 20.00 days of a 30-day plan (`days_remaining` 19); paid on day 29 it
    delivered one day for the full 5,000 SYP.

    The pending payment that POST /subscriptions/ writes is also the one the Pay button
    charges now. It had no reference, nothing could ever complete it, and every
    subscriber's history carried an orphan 'pending' row beside the payment that cleared.
    """
    from subscription.models import Payment, Subscription

    plan = _plan("late")
    user = make_user("c9pay_late", phone_number="0933000001")
    c, sub = _subscribe(api, user, plan)

    # The checkout sat for ten days before the customer transferred the money.
    Subscription.objects.filter(pk=sub.pk).update(
        start_date=timezone.now() - timedelta(days=10),
        end_date=timezone.now() + timedelta(days=20),
        created_at=timezone.now() - timedelta(days=10))

    checkout = _checkout(c, sub)
    with django_capture_on_commit_callbacks(execute=True):
        assert _signed_webhook(checkout["reference"]).status_code == 200

    sub.refresh_from_db()
    assert sub.status == "active" and sub.is_active
    assert _days_of_cover(sub) > 29.99, f"paid for 30 days, got {_days_of_cover(sub):.2f}"
    assert abs((sub.start_date - timezone.now()).total_seconds()) < 60, \
        "the paid period is dated from the checkout, not from the payment"
    current = c.get("/api/subscription/v1/subscriptions/current/").json()
    assert current["days_remaining"] >= 29, current

    payments = list(Payment.objects.filter(subscription=sub).values_list("status", flat=True))
    assert payments == ["completed"], f"an orphan checkout row was left behind: {payments}"


def test_a_trial_that_converts_to_paid_is_entitled(make_user, shamcash):
    """Activation never cleared `trial_end_date`, and `is_active` refuses any row whose
    trial has ended — so a trial user who paid after the trial got `status='active'` and
    no access at all."""
    from subscription.models import Payment, Subscription
    from subscription.services.payment_service import PaymentService

    plan = _plan("trial")
    user = make_user("c9pay_trial")
    sub = Subscription.objects.create(user=user, plan=plan, status="trial",
                                      trial_end_date=timezone.now() + timedelta(days=7))
    Subscription.objects.filter(pk=sub.pk).update(
        start_date=timezone.now() - timedelta(days=9),
        trial_end_date=timezone.now() - timedelta(days=2))

    payment = Payment.objects.create(subscription=sub, amount=plan.price, currency="SYP",
                                     status="pending", payment_method="manual")
    PaymentService.complete_payment(payment.id, {
        "amount": "5000.00", "currency": "SYP", "status": "completed",
        "event_id": "c9-trial-convert"})

    sub.refresh_from_db()
    assert sub.status == "active"
    assert sub.is_active, (sub.trial_end_date, sub.end_date)
    assert _days_of_cover(sub) > 29.99


# ----------------------------------------------------- one checkout per subscription
def test_after_a_lapse_renew_and_pay_open_one_checkout(
        make_user, api, shamcash, django_capture_on_commit_callbacks):
    """`renew` deduplicated only `metadata.kind='renewal'` payments and the Pay button
    deduplicated nothing, refusing only while the subscription was active. After a lapse
    the app offered both, each handed out its own reference, and paying both charged
    10,000 SYP: whichever cleared second was marked completed, paid out to the trainer,
    and added 0 days."""
    from subscription.models import Payment

    plan = _plan("lapse")
    client_user, _trainer = _client_and_trainer(make_user, "lapse")
    c, sub = _paid_and_lapsed(api, client_user, plan, django_capture_on_commit_callbacks)

    renewal = _renew(c, sub)
    pay = _checkout(c, sub)
    again = _renew(c, sub)

    assert pay["reference"] == renewal["reference"], (renewal, pay)
    assert pay["payment_id"] == renewal["payment_id"] == again["payment_id"]
    open_payments = Payment.objects.filter(
        subscription=sub, status__in=("pending", "processing", "authorized"))
    assert open_payments.count() == 1, list(open_payments.values_list(
        "description", "gateway_transaction_reference"))


def test_a_double_tapped_pay_button_hands_out_one_reference(make_user, api, shamcash):
    """Two taps on Pay for a subscription that is not yet active each created a Payment
    with its own reference — both payable, both completable, and the second one bought
    nothing."""
    from subscription.models import Payment

    plan = _plan("paytwice")
    user = make_user("c9pay_paytwice", phone_number="0933000002")
    c, sub = _subscribe(api, user, plan)

    first, second = _checkout(c, sub), _checkout(c, sub)
    assert first["reference"] == second["reference"], (first, second)
    assert first["payment_id"] == second["payment_id"]
    assert first["instructions"] == second["instructions"]
    assert Payment.objects.filter(subscription=sub).exclude(
        gateway_transaction_reference=None).count() == 1


def test_an_active_subscription_is_still_sent_to_renew(make_user, api, shamcash,
                                                        django_capture_on_commit_callbacks):
    """The Pay button refuses an active subscription — that rule moved under the lock and
    must still hold."""
    plan = _plan("active409")
    user = make_user("c9pay_active409", phone_number="0933000003")
    c, sub = _subscribe(api, user, plan)
    checkout = _checkout(c, sub)
    with django_capture_on_commit_callbacks(execute=True):
        assert _signed_webhook(checkout["reference"]).status_code == 200

    resp = _post(c, "/api/subscription/v1/gateways/",
                 {"gateway": "shamcash", "subscription_id": str(sub.id)})
    assert resp.status_code == 409, resp.content


def test_a_gateway_that_refuses_leaves_no_checkout_behind(make_user, api, shamcash):
    """The gateway is called while the subscription is locked, so a refusal now rolls the
    whole checkout back. The old renew path created the payment first and marked it
    'failed' when initiation failed, which left a row in the customer's history for a
    checkout that never opened; the Pay path left nothing but also told nobody."""
    from subscription.models import Payment, Subscription

    plan = _plan("gwdown")
    paying = make_user("c9pay_gwdown_new", phone_number="0933000009")
    c_pay, pending_sub = _subscribe(api, paying, plan)
    renewing = make_user("c9pay_gwdown_old", phone_number="0933000010")
    active_sub = Subscription.objects.create(user=renewing, plan=plan, status="active")
    Subscription.objects.filter(pk=active_sub.pk).update(
        end_date=timezone.now() + timedelta(days=5))
    shamcash["merchant_id"] = ""          # a deploy with no ShamCash account configured

    renew = _post(api(renewing), f"/api/subscription/v1/subscriptions/{active_sub.id}/renew/",
                  {"gateway": "shamcash"})
    pay = _post(c_pay, "/api/subscription/v1/gateways/",
                {"gateway": "shamcash", "subscription_id": str(pending_sub.id)})

    assert renew.status_code == 400, renew.content
    assert pay.status_code == 503, pay.content
    assert not Payment.objects.exclude(gateway_transaction_reference=None).exists()
    assert not Payment.objects.filter(subscription=active_sub).exists()


# ------------------------------------------------ what a payment buys, what a refund takes
def _two_completed_payments(sub, tag):
    """Two payments for one subscription that both cleared — what a double charge from
    before this fix, or an administrator settling a transfer by hand, leaves behind."""
    from subscription.models import Payment
    from subscription.services.payment_service import PaymentService

    payments = []
    for i in range(2):
        payment = Payment.objects.create(
            subscription=sub, amount=sub.plan.price, currency=sub.plan.currency,
            status="pending", payment_method="manual")
        PaymentService.complete_payment(payment.id, {
            "amount": str(sub.plan.price), "currency": sub.plan.currency,
            "status": "completed", "event_id": f"c9-{tag}-{i}"})
        payments.append(payment)
    return payments


def test_every_payment_that_clears_buys_its_own_period(
        make_user, api, shamcash, django_capture_on_commit_callbacks):
    """A non-renewal payment that cleared while cover was already running added nothing,
    yet was marked completed, invoiced and paid out. Measured: two 5,000 SYP payments on
    a lapsed subscription left 30.0 days of cover and 10,000 in the trainer's wallet."""
    plan = _plan("eachperiod")
    user = make_user("c9pay_eachperiod", phone_number="0933000004")
    _c, sub = _paid_and_lapsed(api, user, plan, django_capture_on_commit_callbacks)

    _two_completed_payments(sub, "eachperiod")
    assert _days_of_cover(sub) > 59.99, f"two payments bought {_days_of_cover(sub):.2f} days"


def test_refunding_a_duplicate_keeps_the_period_still_paid_for(
        make_user, api, shamcash, django_capture_on_commit_callbacks):
    """`refund_payment` took `plan.duration_days` off whenever another completed payment
    existed, with no idea what the refunded payment had added. Refunding the duplicate —
    the normal fix for a double charge — landed `end_date` on `now` and cancelled a
    subscription the customer had still paid a month for. Measured: status cancelled,
    `is_active` False, trainer 5,000."""
    from subscription.models import Payment

    plan = _plan("dupold")
    client_user, trainer = _client_and_trainer(make_user, "dupold")
    _c, sub = _paid_and_lapsed(api, client_user, plan, django_capture_on_commit_callbacks)

    with django_capture_on_commit_callbacks(execute=True):
        _first, duplicate = _two_completed_payments(sub, "dupold")
    Payment.objects.filter(pk=duplicate.pk).update(gateway_transaction_reference="C9-DUP-OLD")

    with django_capture_on_commit_callbacks(execute=True):
        resp = _signed_webhook("C9-DUP-OLD", status="refunded", event_id="c9-dupold-refund")
    assert resp.status_code == 200

    duplicate.refresh_from_db()
    assert duplicate.status == "refunded"
    sub.refresh_from_db()
    assert sub.status == "active" and sub.is_active, sub.status
    assert 29.99 < _days_of_cover(sub) < 30.01, _days_of_cover(sub)


@pytest.mark.parametrize("window", ["recorded", "never recorded"])
def test_refunding_a_payment_from_before_a_lapse_leaves_the_period_paid_since(
        window, make_user, api, shamcash, django_capture_on_commit_callbacks):
    """A refund took a whole period off `end_date` whenever another completed payment
    existed — including when the refunded payment's period had run out long before, and
    the subscription had lapsed and been paid for again since. A chargeback on January's
    payment, landing in March, cancelled the March month the customer had just paid for.
    Measured on the recorded-window version too: `end_date - 30d` landed on `now` and the
    subscription was cancelled with a fresh 5,000 SYP payment behind it.

    'never recorded' is a row that completed before the window was stored — the ledger
    already holds those. It has only its own `created_at` to say when its cover began,
    and that is still before the restart."""
    from subscription.models import Payment

    plan = _plan(f"oldrefund {window}")
    client_user, _trainer = _client_and_trainer(make_user, f"oldrefund_{window}")
    c, sub = _paid_and_lapsed(api, client_user, plan, django_capture_on_commit_callbacks)
    january = Payment.objects.get(subscription=sub, status="completed")
    if window == "never recorded":
        Payment.objects.filter(pk=january.pk).update(covers_from=None, covers_until=None)
        january.refresh_from_db()

    march = _checkout(c, sub)
    with django_capture_on_commit_callbacks(execute=True):
        assert _signed_webhook(march["reference"]).status_code == 200
    assert 29.99 < _days_of_cover(sub) < 30.01

    with django_capture_on_commit_callbacks(execute=True):
        assert _signed_webhook(january.gateway_transaction_reference, status="chargeback",
                               event_id=f"c9-oldrefund-cb-{window}").status_code == 200

    january.refresh_from_db()
    sub.refresh_from_db()
    assert january.status == "refunded"
    assert sub.status == "active" and sub.is_active, sub.status
    assert 29.99 < _days_of_cover(sub) < 30.01, _days_of_cover(sub)


def test_refunds_out_of_a_run_of_renewals_each_take_one_period(make_user, api, shamcash):
    """A refund out of the middle of a run moves every later period earlier, so their
    recorded dates stop matching the row. What comes off is each payment's LENGTH; a rule
    reading the dates would take nothing for the last payment once the middle one had
    gone."""
    from subscription.services.payment_service import PaymentService

    plan = _plan("run")
    user = make_user("c9pay_run", phone_number="0933000007")
    _c, sub = _subscribe(api, user, plan)
    payments = _two_completed_payments(sub, "run-a")
    assert 59.99 < _days_of_cover(sub) < 60.01
    payments.append(PaymentService.complete_payment(
        PaymentService.start_renewal(sub).id,
        {"amount": "5000.00", "currency": "SYP", "status": "completed", "event_id": "c9-run-c"}))
    assert 89.99 < _days_of_cover(sub) < 90.01

    PaymentService.refund_payment(payments[1].id, "c9 middle")
    assert 59.99 < _days_of_cover(sub) < 60.01
    PaymentService.refund_payment(payments[2].id, "c9 last")
    assert 29.99 < _days_of_cover(sub) < 30.01
    sub.refresh_from_db()
    assert sub.status == "active"


def test_paying_again_after_an_immediate_cancel_buys_a_whole_period(
        make_user, api, shamcash, django_capture_on_commit_callbacks):
    """Activation kept whatever `end_date` a cancelled row still carried when it was in
    the future. Measured: paid on day 0, cancelled with `immediate` on day 5, paid 5,000
    SYP again — 25.00 days of cover, and the second payment added nothing."""
    from django.db.models import F

    from subscription.models import Payment, Subscription

    plan = _plan("recancel")
    user = make_user("c9pay_recancel", phone_number="0933000008")
    c, sub = _subscribe(api, user, plan)
    with django_capture_on_commit_callbacks(execute=True):
        assert _signed_webhook(_checkout(c, sub)["reference"]).status_code == 200
    five_days = timedelta(days=5)
    Subscription.objects.filter(pk=sub.pk).update(
        start_date=F("start_date") - five_days, end_date=F("end_date") - five_days)
    Payment.objects.filter(subscription=sub, status="completed").update(
        covers_from=F("covers_from") - five_days, covers_until=F("covers_until") - five_days)

    resp = _post(c, f"/api/subscription/v1/subscriptions/{sub.id}/cancel/", {"immediate": True})
    assert resp.status_code == 200, resp.content
    again = _checkout(c, sub)
    with django_capture_on_commit_callbacks(execute=True):
        assert _signed_webhook(again["reference"]).status_code == 200

    sub.refresh_from_db()
    assert sub.status == "active" and sub.is_active
    assert _days_of_cover(sub) > 29.99, _days_of_cover(sub)


def test_refunding_a_new_subscribers_duplicate_is_applied_not_crashed(
        make_user, api, shamcash, django_capture_on_commit_callbacks):
    """For a new subscriber the same subtraction landed exactly on `start_date`, and
    `Subscription.save()` -> `validate_row()` raised "End date must be after start date".
    Nothing caught it: the refund webhook answered 500 on every redelivery, the admin
    'Mark as refunded' action raised, the payment stayed completed and the trainer kept
    the double payout."""
    from django.contrib.admin.sites import AdminSite

    from subscription.admin import PaymentAdmin
    from subscription.models import Payment

    plan = _plan("dupnew")
    user = make_user("c9pay_dupnew", phone_number="0933000005")
    _c, sub = _subscribe(api, user, plan)
    first, duplicate = _two_completed_payments(sub, "dupnew")

    admin = PaymentAdmin(Payment, AdminSite())
    admin.message_user = lambda *a, **k: None
    admin.mark_as_refunded(None, Payment.objects.filter(pk=duplicate.pk))
    duplicate.refresh_from_db()
    assert duplicate.status == "refunded"
    assert sub.__class__.objects.get(pk=sub.pk).is_active
    assert 29.99 < _days_of_cover(sub) < 30.01

    # And the last paid period can be refunded too: the subscription ends, it does not
    # crash on its own start date.
    admin.mark_as_refunded(None, Payment.objects.filter(pk=first.pk))
    first.refresh_from_db()
    sub.refresh_from_db()
    assert first.status == "refunded"
    assert sub.status == "cancelled" and not sub.is_active


# ------------------------------------------------------------ the payout after a failure
@pytest.fixture
def payout_fails_once(monkeypatch):
    """One transient fault inside the payout — the deadlock, the connection drop."""
    from django.db import OperationalError

    import wallet.signals as wallet_signals

    real = wallet_signals.move_funds_atomic
    state = {"failed": False}

    def flaky(*args, **kwargs):
        if not state["failed"]:
            state["failed"] = True
            raise OperationalError("deadlock detected")
        return real(*args, **kwargs)

    monkeypatch.setattr(wallet_signals, "move_funds_atomic", flaky)
    return state


def _legs(payment):
    from wallet.models import Transaction

    return sorted(Transaction.objects.filter(
        metadata__original_payment_id=str(payment.id)).values_list("tx_type", flat=True))


def _platform_balance():
    """What the platform earned. A plan payment stays 100% with the platform whether or
    not the client has a trainer (the owner, 2026-09-19; plan step 4), so these tests ask
    the platform's revenue wallet where the money went — they used to ask the trainer's,
    which is the forward that decision removed. What they are ABOUT is unchanged: that
    the payout happens exactly once, however the retry arrives."""
    from wallet.utils import get_platform_revenue_wallet

    return get_platform_revenue_wallet().balance


def _stub_transactions(monkeypatch, reference, amount="5000.00"):
    from subscription.gateways.shamcash import ShamCashGateway

    monkeypatch.setattr(ShamCashGateway, "_get", lambda self, path, params: {"data": [{
        "id": f"txn-{reference}", "amount": amount, "currency": "SYP", "note": reference}]})


@pytest.mark.parametrize("retry", ["redelivery", "new_event", "reconcile", "admin"])
def test_a_payout_that_failed_once_is_paid_by_the_retry(
        retry, make_user, api, shamcash, payout_fails_once, monkeypatch,
        django_capture_on_commit_callbacks):
    """The payout runs on commit, after the payment row is already 'completed', and every
    retry decided "done" from that status: the event-id replay check, the
    already-completed no-op, the reconcile view's early return and the admin action.
    Measured: delivery 1 -> 500 with no payout legs; the gateway's redelivery -> 200,
    legs []; a new event id -> 200, legs []; reconcile -> 200, legs []; trainer 0.00. The
    gateway stops retrying on that 200, and the trainer is never paid."""
    from django.contrib.admin.sites import AdminSite
    from django.db import OperationalError

    from subscription.admin import PaymentAdmin
    from subscription.models import Payment
    from wallet.models import Wallet

    plan = _plan(f"payout {retry}")
    client_user, trainer = _client_and_trainer(make_user, f"payout_{retry}")
    c, sub = _subscribe(api, client_user, plan)
    reference = _checkout(c, sub)["reference"]
    payment = Payment.objects.get(gateway_transaction_reference=reference)

    with pytest.raises(OperationalError):
        with django_capture_on_commit_callbacks(execute=True):
            assert _signed_webhook(reference, event_id=f"ev-{retry}").status_code == 200
    payment.refresh_from_db()
    assert payment.status == "completed" and _legs(payment) == []
    assert payment.payout_settled_at is None, "a payout that never happened is recorded as done"

    with django_capture_on_commit_callbacks(execute=True):
        if retry == "redelivery":
            assert _signed_webhook(reference, event_id=f"ev-{retry}").status_code == 200
        elif retry == "new_event":
            assert _signed_webhook(reference, event_id=f"ev-{retry}-2").status_code == 200
        elif retry == "reconcile":
            _stub_transactions(monkeypatch, reference)
            resp = _post(c, f"/api/subscription/v1/payments/{payment.id}/reconcile/")
            assert resp.status_code == 200 and resp.json()["status"] == "completed"
        else:
            admin = PaymentAdmin(Payment, AdminSite())
            admin.message_user = lambda *a, **k: None
            admin.mark_as_completed(None, Payment.objects.filter(pk=payment.pk))

    payment.refresh_from_db()
    assert _legs(payment) == ["topup", "transfer"], _legs(payment)
    assert _platform_balance() == Decimal("5000.00")
    assert Wallet.objects.get(owner=trainer).balance == Decimal("0.00"), \
        "a plan payment is not the trainer's"
    assert payment.payout_settled_at is not None

    # And a further retry pays nothing more.
    with django_capture_on_commit_callbacks(execute=True):
        assert _signed_webhook(reference, event_id=f"ev-{retry}").status_code == 200
    assert _legs(payment) == ["topup", "transfer"]
    assert _platform_balance() == Decimal("5000.00")


def test_owed_payouts_are_swept(make_user, api, shamcash, payout_fails_once,
                                django_capture_on_commit_callbacks):
    """When the gateway never redelivers — or completion came through a reconcile the
    customer never retried — something still has to pay the trainer."""
    from django.db import OperationalError

    from subscription.models import Payment
    from subscription.tasks import settle_owed_payouts
    from wallet.models import Wallet

    plan = _plan("sweep")
    client_user, trainer = _client_and_trainer(make_user, "sweep")
    c, sub = _subscribe(api, client_user, plan)
    reference = _checkout(c, sub)["reference"]
    with pytest.raises(OperationalError):
        with django_capture_on_commit_callbacks(execute=True):
            _signed_webhook(reference)
    payment = Payment.objects.get(gateway_transaction_reference=reference)
    assert _legs(payment) == []

    # Too fresh: the on-commit payout may still be running in another process.
    with django_capture_on_commit_callbacks(execute=True):
        assert settle_owed_payouts() == 0
    Payment.objects.filter(pk=payment.pk).update(
        updated_at=timezone.now() - timedelta(minutes=30))
    with django_capture_on_commit_callbacks(execute=True):
        assert settle_owed_payouts() == 1
    assert _legs(payment) == ["topup", "transfer"]
    assert _platform_balance() == Decimal("5000.00")
    assert Wallet.objects.get(owner=trainer).balance == Decimal("0.00")
    with django_capture_on_commit_callbacks(execute=True):
        assert settle_owed_payouts() == 0


def test_payments_completed_before_the_payout_was_recorded_are_not_paid_again(make_user):
    """The receiver pays whoever is the client's trainer AT PAYOUT TIME. Left unmarked,
    every payment completed before `payout_settled_at` existed would read as owed, and the
    first sweep would pay today's trainer for the history of a client who had none then."""
    import importlib

    from django.apps import apps
    from django.db.models import F

    from subscription.models import Payment, Subscription
    from subscription.tasks import settle_owed_payouts

    migration = importlib.import_module(
        "subscription.migrations.0011_payment_cover_window_and_payout_state")
    plan = _plan("history")
    user = make_user("c9pay_history")
    sub = Subscription.objects.create(user=user, plan=plan, status="active")
    old = Payment.objects.create(subscription=sub, amount=plan.price, currency="SYP",
                                 status="completed", payment_method="manual")
    Payment.objects.filter(pk=old.pk).update(updated_at=timezone.now() - timedelta(days=90))

    migration.mark_history_settled(apps, None)

    assert Payment.objects.filter(pk=old.pk, payout_settled_at=F("updated_at")).exists()
    assert settle_owed_payouts() == 0


def test_a_payment_with_no_trainer_owes_no_payout(make_user, api, shamcash,
                                                  django_capture_on_commit_callbacks):
    """The sweep must not re-examine a client with no trainer for ever.

    It is still recorded: a plan payment goes to the platform whether or not anyone
    coaches the client (the owner, 2026-09-19; plan step 4), and writing nothing at all —
    which is what this case used to do — left the platform's largest revenue line absent
    from its own ledger."""
    from subscription.models import Payment

    plan = _plan("notrainer")
    user = make_user("c9pay_notrainer", phone_number="0933000006")
    c, sub = _subscribe(api, user, plan)
    reference = _checkout(c, sub)["reference"]
    before = _platform_balance()
    with django_capture_on_commit_callbacks(execute=True):
        assert _signed_webhook(reference).status_code == 200
    payment = Payment.objects.get(gateway_transaction_reference=reference)
    assert payment.payout_settled_at is not None
    assert _legs(payment) == ["topup", "transfer"]
    assert _platform_balance() - before == payment.amount


# ------------------------------------------------ the customer's own writes to the row
def _active_with_pending_renewal(make_user, tag):
    from subscription.models import Payment, Subscription

    plan = _plan(tag)
    user = make_user(f"c9pay_{tag}")
    sub = Subscription.objects.create(user=user, plan=plan, status="active",
                                      auto_renew=True)
    Subscription.objects.filter(pk=sub.pk).update(end_date=timezone.now() + timedelta(days=10))
    renewal = Payment.objects.create(
        subscription=sub, amount=plan.price, currency="SYP", status="pending",
        payment_method="shamcash", gateway_transaction_reference=f"C9-RENEW-{tag}",
        metadata={"kind": "renewal"}, description="Renewal")
    sub.refresh_from_db()
    return user, sub, renewal


# `patch_auto_renew` was a third case here until plan step 5 made `auto_renew`
# read-only. The race it reproduced — a renewal clearing between the view's read and its
# write — cannot happen through a request that never writes, and the simulation ran the
# renewal inside the view's own transaction, which a refusal rolls back. The refusal has
# its own test below; the lock path is still covered by both cancel cases, which do write.
@pytest.mark.parametrize("request_kind", ["cancel", "cancel_immediate"])
def test_turning_auto_renew_off_while_a_renewal_clears_keeps_the_paid_days(
        request_kind, make_user, api, monkeypatch):
    """`cancel` loaded the row with `get_object()` and no lock, then `subscription.save()`
    wrote EVERY column back from that copy; PATCH through `SubscriptionUpdateSerializer`
    did the same. A renewal that cleared between the read and the write had its new
    `end_date` overwritten with the old one. Measured with the webhook and the tap on two
    threads: 16 of 20 paid renewals lost their 30 days, both calls answering 200.

    The interleaving is reproduced here in one thread: the renewal clears exactly between
    the view's read and its write.
    """
    from subscription import serializers as sub_serializers
    from subscription.services.payment_service import PaymentService

    user, sub, renewal = _active_with_pending_renewal(make_user, f"lost_{request_kind}")
    paid_until_before = sub.end_date

    def renewal_clears_now():
        PaymentService.complete_payment(renewal.id, {
            "amount": "5000.00", "currency": "SYP", "status": "completed",
            "event_id": f"c9-lost-{request_kind}"})

    target = sub_serializers.SubscriptionCancelSerializer
    real_validate = target.validate

    def validate_then_clear(self, data):
        out = real_validate(self, data)
        renewal_clears_now()
        return out

    monkeypatch.setattr(target, "validate", validate_then_clear)

    c = api(user)
    resp = _post(c, f"/api/subscription/v1/subscriptions/{sub.id}/cancel/",
                 {"reason": "c9", "immediate": request_kind == "cancel_immediate"})
    assert resp.status_code == 200, resp.content

    sub.refresh_from_db()
    renewal.refresh_from_db()
    assert renewal.status == "completed"
    added = (sub.end_date - paid_until_before).days
    assert added == 30, f"the paid renewal added {added} days"
    assert sub.auto_renew is False
    if request_kind == "cancel_immediate":
        assert sub.status == "cancelled"


def test_auto_renew_is_answered_but_never_taken(make_user, api):
    """Plan step 5. Nothing charges a plan automatically — `send_renewal_reminders`
    reminds every subscriber "whatever `auto_renew` says" — so the switch promised a
    renewal that does not exist, and a store build may not offer one at all. The key
    stays in the response, because the app reads it; the write is refused rather than
    accepted and dropped."""
    user, sub, _renewal = _active_with_pending_renewal(make_user, "readonly_ar")
    c = api(user)

    read = c.get(f"/api/subscription/v1/subscriptions/{sub.id}/")
    assert read.status_code == 200
    assert "auto_renew" in read.json(), "the key is still answered"

    refused = c.patch(f"/api/subscription/v1/subscriptions/{sub.id}/",
                      data=json.dumps({"auto_renew": False}),
                      content_type="application/json")
    assert refused.status_code == 400, refused.content
    assert refused.json()["field_errors"]["auto_renew"][0]["code"] == "auto_renew_read_only"
    sub.refresh_from_db()
    assert sub.auto_renew is True, "nothing was written"


def test_a_renewal_clearing_does_not_turn_auto_renew_back_on(make_user, api):
    """Activation wrote `auto_renew = True` on every completion, so a customer who turned
    auto-renew off and whose renewal then cleared found it silently back on."""
    from subscription.services.payment_service import PaymentService

    user, sub, renewal = _active_with_pending_renewal(make_user, "optout")
    resp = _post(api(user), f"/api/subscription/v1/subscriptions/{sub.id}/cancel/", {})
    assert resp.status_code == 200, resp.content

    PaymentService.complete_payment(renewal.id, {
        "amount": "5000.00", "currency": "SYP", "status": "completed",
        "event_id": "c9-optout"})
    sub.refresh_from_db()
    assert sub.auto_renew is False
    assert _days_of_cover(sub) > 39.9


@pytest.mark.parametrize("action,days_added,status", [
    ("extend_subscriptions", 60, "active"),
    ("cancel_subscriptions", 30, "cancelled"),
    ("activate_subscriptions", 30, "active"),
])
def test_an_administrator_action_while_a_renewal_clears_keeps_the_paid_period(
        action, days_added, status, make_user):
    """The bulk subscription actions saved every column of the row the changelist had
    read — the same stale full-row write as the customer's cancel. A renewal that cleared
    between the read and the save lost its 30 days: measured, extend moved the date by 30
    (the administrator's) instead of 60, and cancel and activate moved it by 0 with the
    renewal payment 'completed'."""
    from types import SimpleNamespace

    from subscription import admin as subscription_admin
    from subscription.models import Subscription
    from subscription.services.payment_service import PaymentService

    staff = make_user(f"c9pay_{action}_admin", user_type="admin")
    _user, sub, renewal = _active_with_pending_renewal(make_user, f"admin_{action}")
    paid_until_before = sub.end_date

    changelist = Subscription.objects.filter(pk=sub.pk)
    list(changelist)            # the admin has read the rows ...
    PaymentService.complete_payment(renewal.id, {   # ... the renewal clears ...
        "amount": "5000.00", "currency": "SYP", "status": "completed",
        "event_id": f"c9-{action}"})
    getattr(subscription_admin, action)(                              # ... and it acts.
        SimpleNamespace(message_user=lambda *a, **k: None),
        SimpleNamespace(user=staff), changelist)

    sub.refresh_from_db()
    assert (sub.end_date - paid_until_before).days == days_added, sub.end_date - paid_until_before
    assert sub.status == status


# ------------------------------------------- the plan flags the routine and challenge wiring reads
@pytest.mark.parametrize("who,expected", [
    ("never_subscribed", False),
    ("lapsed_with_flags", False),
    ("active_without_flags", False),
    ("active_with_flags", True),
    ("verified_trainer", True),
    ("unverified_trainer", False),
    ("staff", True),
])
def test_routine_and_challenge_access_follow_the_plan(who, expected, make_user):
    """`has_routine_access` and `has_challenges_access` gated nothing: no routine or
    challenge view attached `HasRoutineAccess` / `HasChallengesAccess`, so routines,
    workout sessions, set logs, analytics and challenge joins all answered 2xx before
    subscribing, after the subscription lapsed, and on a plan without the feature.

    Attaching them is a change to `routine/views.py` and `social/views.py`. This pins the
    decision those views will delegate to, for every kind of caller they serve."""
    from rest_framework.test import APIRequestFactory, force_authenticate
    from rest_framework.views import APIView

    from subscription.models import Subscription
    from subscription.permissions import HasChallengesAccess, HasRoutineAccess

    flags = {"has_routine_access": True, "has_challenges_access": True}
    no_flags = {"has_routine_access": False, "has_challenges_access": False}
    user_type = "trainer" if who.endswith("trainer") else (
        "admin" if who == "staff" else "client")
    user = make_user(f"c9pay_flags_{who}", user_type=user_type,
                     trainer_is_verified=(who == "verified_trainer"))

    if who in ("lapsed_with_flags", "active_with_flags", "active_without_flags"):
        plan = _plan(f"flags {who}", **(no_flags if who == "active_without_flags" else flags))
        sub = Subscription.objects.create(user=user, plan=plan, status="active")
        if who == "lapsed_with_flags":
            Subscription.objects.filter(pk=sub.pk).update(
                status="expired", end_date=timezone.now() - timedelta(days=1))
        # Re-read: `create()` leaves the subscription cached on the user object, and a
        # request carries a user loaded from the database, not one that made the row.
        user = type(user).objects.get(pk=user.pk)

    request = APIRequestFactory().post("/api/social/challenges/1/join/")
    force_authenticate(request, user=user)
    request = APIView().initialize_request(request)
    request.user = user

    for permission in (HasRoutineAccess(), HasChallengesAccess()):
        assert permission.has_permission(request, None) is expected, (who, permission)


# ------------------------------------------------------- step 4: where a plan's money goes
def test_a_coached_clients_plan_payment_goes_to_the_platform(make_user, api, shamcash,
                                                             django_capture_on_commit_callbacks):
    """The owner (2026-09-19): a plan payment stays 100% with the platform, whether or
    not the client has a trainer. It used to move the WHOLE price to whoever coached the
    client — no rate, no escrow, no agreed price — so the platform earned nothing from a
    plan sold to a coached client and the trainer was paid for someone else's product."""
    from subscription.models import Payment
    from wallet.models import Wallet

    plan = _plan("step4 platform")
    client_user, trainer = _client_and_trainer(make_user, "step4_platform")
    c, sub = _subscribe(api, client_user, plan)
    reference = _checkout(c, sub)["reference"]
    before = _platform_balance()

    with django_capture_on_commit_callbacks(execute=True):
        assert _signed_webhook(reference).status_code == 200

    payment = Payment.objects.get(gateway_transaction_reference=reference)
    assert _legs(payment) == ["topup", "transfer"]
    assert _platform_balance() - before == payment.amount
    assert Wallet.objects.get(owner=trainer).balance == Decimal("0.00")
    # And nothing is left sitting in the client's wallet as spendable balance.
    assert Wallet.objects.get(owner=client_user).balance == Decimal("0.00")


def test_the_trainers_share_of_a_plan_is_a_setting_the_owner_can_raise(
        make_user, api, shamcash, settings, django_capture_on_commit_callbacks):
    """The forward is priced at nothing, not deleted: `SUBSCRIPTION_TRAINER_SHARE` is
    kept so the owner can decide later to pay a trainer for a plan their client buys."""
    from subscription.models import Payment
    from wallet.models import Wallet

    settings.SUBSCRIPTION_TRAINER_SHARE = Decimal("0.30")
    plan = _plan("step4 share")
    client_user, trainer = _client_and_trainer(make_user, "step4_share")
    c, sub = _subscribe(api, client_user, plan)
    reference = _checkout(c, sub)["reference"]
    before = _platform_balance()

    with django_capture_on_commit_callbacks(execute=True):
        assert _signed_webhook(reference).status_code == 200

    payment = Payment.objects.get(gateway_transaction_reference=reference)
    assert Wallet.objects.get(owner=trainer).balance == (payment.amount * Decimal("0.30")
                                                         ).quantize(Decimal("0.01"))
    assert _platform_balance() - before == payment.amount - Wallet.objects.get(
        owner=trainer).balance
    assert Wallet.objects.get(owner=client_user).balance == Decimal("0.00"), \
        "the two shares add up to the price"


def test_a_refunded_plan_payment_takes_the_platforms_money_back(
        make_user, api, shamcash, django_capture_on_commit_callbacks):
    """Every leg reverses, including the new one: a refund must not leave the platform
    holding money the customer got back from their card."""
    from subscription.models import Payment
    from wallet.signals import unwind_payment

    plan = _plan("step4 refund")
    client_user, trainer = _client_and_trainer(make_user, "step4_refund")
    c, sub = _subscribe(api, client_user, plan)
    reference = _checkout(c, sub)["reference"]
    before = _platform_balance()
    with django_capture_on_commit_callbacks(execute=True):
        assert _signed_webhook(reference).status_code == 200
    payment = Payment.objects.get(gateway_transaction_reference=reference)
    assert _platform_balance() - before == payment.amount

    assert unwind_payment(payment) == 2
    assert _platform_balance() == before


# ------------------------------------------------- step 5: the checkout poller
def _no_transactions(monkeypatch):
    """The merchant account shows nothing: nobody has paid."""
    from subscription.gateways.shamcash import ShamCashGateway

    monkeypatch.setattr(ShamCashGateway, "_get", lambda self, path, params: {"data": []})


def test_the_poller_completes_a_checkout_the_webhook_never_told_us_about(
        make_user, api, shamcash, monkeypatch, django_capture_on_commit_callbacks):
    """A dropped webhook used to leave the row pending for ever — and `start_payment`
    hands the same open checkout back, so that customer could never buy again."""
    from subscription.models import Payment
    from subscription.tasks import poll_open_payments

    plan = _plan("poll done")
    user = make_user("c9poll_done", phone_number="0933001001")
    c, sub = _subscribe(api, user, plan)
    reference = _checkout(c, sub)["reference"]
    payment = Payment.objects.get(gateway_transaction_reference=reference)
    Payment.objects.filter(pk=payment.pk).update(
        created_at=timezone.now() - timedelta(minutes=2))

    _stub_transactions(monkeypatch, reference)
    with django_capture_on_commit_callbacks(execute=True):
        result = poll_open_payments()

    payment.refresh_from_db()
    assert payment.status == "completed"
    assert (result["completed"], result["expired"]) == (1, 0)
    assert _legs(payment) == ["topup", "transfer"]


def test_the_poller_leaves_a_fresh_checkout_to_the_webhook(make_user, api, shamcash,
                                                           monkeypatch):
    """It backs the webhook up; it does not race it."""
    from subscription.models import Payment
    from subscription.tasks import poll_open_payments

    plan = _plan("poll fresh")
    user = make_user("c9poll_fresh", phone_number="0933001002")
    c, sub = _subscribe(api, user, plan)
    reference = _checkout(c, sub)["reference"]
    _stub_transactions(monkeypatch, reference)

    assert poll_open_payments()["checked"] == 0
    assert Payment.objects.get(gateway_transaction_reference=reference).status == "pending"


def test_a_checkout_nobody_finished_is_closed_and_the_customer_told(
        make_user, api, shamcash, monkeypatch):
    """So they can start a new one: one open checkout at a time is the rule, and a
    customer who walked away was locked out of buying for ever."""
    from notifications.models import Notification
    from subscription.models import Payment
    from subscription.tasks import poll_open_payments

    plan = _plan("poll expire")
    user = make_user("c9poll_expire", phone_number="0933001003")
    c, sub = _subscribe(api, user, plan)
    reference = _checkout(c, sub)["reference"]
    payment = Payment.objects.get(gateway_transaction_reference=reference)
    Payment.objects.filter(pk=payment.pk).update(
        created_at=timezone.now() - timedelta(hours=3))
    _no_transactions(monkeypatch)

    result = poll_open_payments()
    payment.refresh_from_db()
    assert payment.status == "expired"
    assert result["expired"] == 1
    notice = Notification.objects.get(recipient=user, event_type="checkout_expired")
    assert notice.related_object_id == str(payment.id)
    # And the customer can open a new checkout at once.
    assert _checkout(c, sub)["reference"] != reference


def test_money_that_arrives_for_an_expired_checkout_is_still_honoured(
        make_user, api, shamcash, monkeypatch, django_capture_on_commit_callbacks):
    """Otherwise a customer pays and gets nothing, and the gateway retries for ever
    against a row that can never move."""
    from subscription.models import Payment
    from subscription.tasks import poll_open_payments

    plan = _plan("poll late")
    user = make_user("c9poll_late", phone_number="0933001004")
    c, sub = _subscribe(api, user, plan)
    reference = _checkout(c, sub)["reference"]
    payment = Payment.objects.get(gateway_transaction_reference=reference)
    Payment.objects.filter(pk=payment.pk).update(
        created_at=timezone.now() - timedelta(hours=3))
    _no_transactions(monkeypatch)
    poll_open_payments()
    payment.refresh_from_db()
    assert payment.status == "expired"

    with django_capture_on_commit_callbacks(execute=True):
        assert _signed_webhook(reference).status_code == 200
    payment.refresh_from_db()
    assert payment.status == "completed", "late money is still money"


def test_an_unreachable_gateway_leaves_every_checkout_as_it_was(make_user, api, shamcash,
                                                                monkeypatch):
    """One outage must not fail a queue or close a checkout somebody is paying."""
    from subscription.models import Payment
    from subscription.tasks import poll_open_payments

    from subscription.gateways.shamcash import ShamCashGateway

    plan = _plan("poll down")
    user = make_user("c9poll_down", phone_number="0933001005")
    c, sub = _subscribe(api, user, plan)
    reference = _checkout(c, sub)["reference"]
    Payment.objects.filter(gateway_transaction_reference=reference).update(
        created_at=timezone.now() - timedelta(hours=3))

    def boom(self, path, params):
        raise RuntimeError("gateway down")

    monkeypatch.setattr(ShamCashGateway, "_get", boom)
    result = poll_open_payments()
    assert (result["completed"], result["expired"], result["failed"]) == (0, 0, 0)
    assert Payment.objects.get(gateway_transaction_reference=reference).status == "pending"
