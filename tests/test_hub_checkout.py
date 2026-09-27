"""Checkout sessions: how the web payment hub buys (the payments spec, section 5 "Checkout
session", section 6 "the payment step is an adapter", section 7 the lifecycle).

`POST /api/v1/checkout/sessions/ {money_type, item_id}` opens ONE session per user per
item — asking again answers the same session and reference — for a plan (PLATFORM_SUB)
or a routine product (ROUTINE_PRODUCT), on the builds that may buy them. A session IS a
`subscription.Payment`: a plan's is the subscription's one checkout, opened by the Pay /
Renew buttons' own service calls; a product's is confirmed by the pollers, "check my
payment" and the review queue like a plan's, and delivers the buyer's copy with the
wallet sale's split. See `subscription/checkout_views.py`, `routine/products.py`.

On 88c4100 none of it exists: `/api/v1/checkout/` answers 404, `subscription.
payment_step` does not import, a Payment cannot be written without a subscription, and a
routine product can only be bought from the wallet. Every test below fails there.
"""
import json
from datetime import timedelta
from decimal import Decimal

import pytest
from django.db import IntegrityError, transaction
from django.utils import timezone

from tests.test_fx_subs import _checkout, _paid, _person, _plan, shamcash  # noqa: F401
from tests.test_payment_review import _account, _txn
from tests.test_routine_products import _buy, _console, _fund, _operator, _published

pytestmark = pytest.mark.django_db

JSON = "application/json"
D = Decimal
SESSIONS = "/api/v1/checkout/sessions/"
WEB = {"HTTP_X_APP_CHANNEL": "web"}


@pytest.fixture(autouse=True)
def owners_rate(settings):
    """The owner's 20% (tests/conftest.py sets the coaching rate to 0 per test)."""
    settings.COACHING_COMMISSION_RATE = D("0.20")


def _session(c, money_type, item_id, **headers):
    return c.post(SESSIONS, data=json.dumps({"money_type": money_type, "item_id": item_id}),
                  content_type=JSON, **{**WEB, **headers})


def _aged(session_id, age=timedelta(minutes=2)):
    """Past the poller's grace (or its window), as a real checkout would be by then."""
    from subscription.models import Payment

    Payment.objects.filter(pk=session_id).update(created_at=timezone.now() - age)


def _poll():
    from subscription.tasks import poll_open_payments

    return poll_open_payments()


def _wallet(user):
    from wallet.models import Wallet

    wallet = Wallet.objects.filter(owner=user).first()
    return wallet.balance if wallet else D("0.00")


def _ledger():
    """(escrow, platform revenue) in SYP."""
    from wallet.utils import get_escrow_wallet, get_platform_revenue_wallet

    return get_escrow_wallet("SYP").balance, get_platform_revenue_wallet("SYP").balance


def _steps(payment_id):
    from subscription.models import PaymentEvent

    return [(e.from_status, e.to_status, e.actor_role)
            for e in PaymentEvent.objects.filter(payment_id=payment_id)]


def _product_session(make_user, api, tag):
    """A published product (2,500 SYP), a buyer, and the buyer's open session for it."""
    trainer, _template, product_id = _published(make_user, api, tag)
    buyer = _person(make_user, f"hub_b_{tag}")
    c = api(buyer)
    resp = _session(c, "ROUTINE_PRODUCT", product_id)
    assert resp.status_code == 201, resp.content
    return trainer, buyer, c, product_id, resp.json()


# ============================================================ a plan's session
def test_a_plan_session_is_the_plan_checkout_and_asking_again_returns_it(
        make_user, api, shamcash):
    from subscription.models import Payment, Subscription

    plan = _plan("hub1", "13000.00")
    user = _person(make_user, "hub_plan")
    c = api(user)

    first = _session(c, "PLATFORM_SUB", plan.id)
    assert first.status_code == 201, first.content
    body = first.json()
    assert body["already_open"] is False
    assert (body["money_type"], body["kind"], body["status"], body["payment_status"]) == \
        ("PLATFORM_SUB", "initial", "open", "pending")
    assert (body["amount"], body["currency"]) == ("13000.00", "SYP")
    assert body["item"] == {"type": "plan", "id": plan.id, "name": plan.name,
                            "duration_days": 30}
    assert body["reference"].startswith("SHAMCASH_") and body["expires_at"]
    assert body["payment_target"] == {
        "adapter": "manual_transfer", "gateway": "shamcash", "account_id": "FX-SUBS-ACCOUNT",
        "amount": "13000.00", "currency": "SYP", "note": body["reference"],
        "payment_url": None}
    assert (body["review"], body["result"]) == (None, None)

    # The session IS the payment row — a pending subscription was chosen for it, as
    # `POST v1/subscriptions/` chooses one — and its opening is its first event.
    payment = Payment.objects.get(pk=body["id"])
    sub = Subscription.objects.get(user=user)
    assert (payment.subscription_id, payment.money_type, payment.gateway_transaction_reference,
            sub.plan_id, sub.status) == (sub.pk, "PLATFORM_SUB", body["reference"], plan.id,
                                         "pending")
    (opened,) = payment.events.all()
    assert (opened.to_status, opened.actor_id, opened.evidence["channel"]) == \
        ("pending", user.pk, "web")

    # One way to open a plan checkout: the app's Pay button hands back THIS checkout.
    pay = _checkout(c, sub).json()
    assert (pay["payment_id"], pay["reference"]) == (body["id"], body["reference"])

    # Asking again (a double tap) answers the same session and reference.
    again = _session(c, "PLATFORM_SUB", plan.id)
    assert again.status_code == 200, again.content
    assert (again.json()["id"], again.json()["reference"], again.json()["already_open"]) == \
        (body["id"], body["reference"], True)
    assert Payment.objects.filter(subscription=sub).count() == 1
    assert len(_steps(body["id"])) == 1, "asking again opened nothing"


def test_a_plan_session_renews_running_cover_and_moves_a_lapsed_plan(
        make_user, api, shamcash, django_capture_on_commit_callbacks):
    from subscription.models import Payment, Subscription
    from subscription.services.payment_service import PaymentService

    basic, premium = _plan("hub2b", "13000.00"), _plan("hub2p", "26000.00")
    user = _person(make_user, "hub_renew")
    c = api(user)
    first = _session(c, "PLATFORM_SUB", basic.id).json()
    with django_capture_on_commit_callbacks(execute=True):
        PaymentService.complete_payment(first["id"], {
            "amount": "13000.00", "currency": "SYP", "status": "completed",
            "transaction_id": "tx-hub2"})
    assert Subscription.objects.get(user=user).is_active

    # Cover running on this plan: the session is its renewal, as the Renew button opens.
    renewal = _session(c, "PLATFORM_SUB", basic.id)
    assert renewal.status_code == 201, renewal.content
    assert (renewal.json()["kind"], renewal.json()["amount"]) == ("renewal", "13000.00")
    assert renewal.json()["id"] != first["id"]
    # Another plan while cover runs: the existing refusal (a dearer plan is upgrade/).
    moved = _session(c, "PLATFORM_SUB", premium.id)
    assert (moved.status_code, moved.json()["code"]) == (409, "plan_change_while_active")

    # A lapsed customer picks another plan: moved to it, as change-plan/ does, and paid.
    lapsed = _person(make_user, "hub_lapsed")
    lc = api(lapsed)
    old = _session(lc, "PLATFORM_SUB", basic.id).json()
    blocked = _session(lc, "PLATFORM_SUB", premium.id)
    assert (blocked.status_code, blocked.json()["code"], blocked.json()["payment_id"]) == \
        (409, "checkout_open", old["id"]), "an open checkout of another plan blocks it"
    PaymentService.expire_payment(old["id"], "window closed")
    fresh = _session(lc, "PLATFORM_SUB", premium.id)
    assert fresh.status_code == 201, fresh.content
    assert (fresh.json()["item"]["id"], fresh.json()["amount"]) == (premium.id, "26000.00")
    assert Subscription.objects.get(user=lapsed).plan_id == premium.id
    assert Payment.objects.get(pk=old["id"]).status == "expired"


# ============================================================ who may open one
def test_store_builds_and_other_kinds_of_money_are_refused_with_the_existing_codes(
        make_user, api, shamcash):
    from subscription.models import Payment, Subscription

    plan = _plan("hub3", "13000.00")
    _trainer, _template, product_id = _published(make_user, api, "hub3")
    user = _person(make_user, "hub_refused")
    c = api(user)

    for channel in ("play", "ios"):
        for money_type, item in (("PLATFORM_SUB", plan.id), ("ROUTINE_PRODUCT", product_id)):
            resp = _session(c, money_type, item, HTTP_X_APP_CHANNEL=channel)
            assert (resp.status_code, resp.json()["code"]) == \
                (403, "not_available_on_channel"), (channel, money_type)
    resp = _session(c, "PLATFORM_SUB", plan.id, HTTP_X_APP_CHANNEL="")
    assert (resp.status_code, resp.json()["code"]) == (400, "app_channel_required")
    resp = _session(c, "COACHING_1TO1", plan.id, HTTP_X_APP_CHANNEL="")
    assert (resp.status_code, resp.json()["code"]) == (400, "app_channel_required")
    # Coaching and wallet money are paid from the wallet, never through a checkout.
    for money_type in ("COACHING_1TO1", "WALLET_MOVE", "SOMETHING"):
        resp = _session(c, money_type, plan.id)
        assert (resp.status_code, resp.json()["code"]) == (400, "validation_error")
        assert resp.json()["field_errors"]["money_type"][0]["code"] == "invalid_choice"
    resp = c.post(SESSIONS, data=json.dumps({"money_type": "PLATFORM_SUB"}),
                  content_type=JSON, **WEB)
    assert resp.json()["field_errors"]["item_id"][0]["code"] == "required"
    resp = _session(c, "PLATFORM_SUB", 999999)
    assert (resp.status_code, resp.json()["code"]) == (400, "invalid_plan")
    free = _plan("hub3free", "0.00")
    resp = _session(c, "PLATFORM_SUB", free.id)
    assert (resp.status_code, resp.json()["code"]) == (400, "payment_not_required")

    assert not Payment.objects.filter(subscription__user=user).exists()
    assert not Payment.objects.filter(buyer=user).exists()
    assert not Subscription.objects.filter(user=user).exists(), "a refused build chose nothing"


def test_a_product_session_is_refused_as_the_wallet_refuses_the_same_facts(
        make_user, api, shamcash):
    trainer, _template, product_id = _published(make_user, api, "hub3p")
    resp = _session(api(trainer), "ROUTINE_PRODUCT", product_id)
    assert (resp.status_code, resp.json()["code"]) == (403, "own_product")
    resp = _session(api(_person(make_user, "hub3p_x")), "ROUTINE_PRODUCT", 999999)
    assert (resp.status_code, resp.json()["code"]) == (404, "not_found")

    owner = _person(make_user, "hub3p_owner")
    _fund(owner, "10000.00")
    assert _buy(api, owner, product_id).status_code == 200
    resp = _session(api(owner), "ROUTINE_PRODUCT", product_id)
    assert (resp.status_code, resp.json()["code"]) == (409, "already_owned")


# ============================================================ a product's session
def test_a_product_session_is_one_per_buyer_and_program_and_prices_it_now(
        make_user, api, shamcash):
    from policy.money import MoneyType
    from routine.models import RoutinePurchase
    from subscription.models import Payment

    trainer, buyer, c, product_id, body = _product_session(make_user, api, "hub4")
    assert (body["money_type"], body["kind"], body["status"], body["already_open"]) == \
        ("ROUTINE_PRODUCT", "routine_product", "open", False)
    assert body["item"] == {"type": "routine_product", "id": product_id,
                            "name": "Shoulder rehab in 4 weeks"}
    assert (body["amount"], body["currency"], body["result"]) == ("2500.00", "SYP", None)
    assert body["payment_target"]["note"] == body["reference"]

    payment = Payment.objects.get(pk=body["id"])
    assert (payment.buyer_id, payment.routine_product_id, payment.subscription_id,
            payment.money_type) == (buyer.pk, product_id, None, "ROUTINE_PRODUCT")
    # The commission is fixed when the price is locked (the spec, section 8).
    assert payment.metadata["product"]["commission_rate"] == "0.20"
    assert _steps(body["id"]) == [("", "pending", "customer")]

    again = _session(c, "ROUTINE_PRODUCT", product_id)
    assert again.status_code == 200, again.content
    assert (again.json()["id"], again.json()["reference"], again.json()["already_open"]) == \
        (body["id"], body["reference"], True)
    assert Payment.objects.filter(buyer=buyer).count() == 1
    assert not RoutinePurchase.objects.filter(buyer=buyer).exists(), "nothing is delivered unpaid"

    # The wallet cannot sell it to them as well while the checkout is open.
    _fund(buyer, "10000.00")
    bought = _buy(api, buyer, product_id)
    assert (bought.status_code, bought.json()["code"], bought.json()["payment_id"]) == \
        (409, "checkout_open", body["id"])
    assert _wallet(buyer) == D("10000.00")

    listed = c.get("/api/v1/checkout/sessions", **WEB).json()["results"]
    assert [s["id"] for s in listed] == [body["id"]]

    # The database's backstop for one open checkout per buyer per product.
    with pytest.raises(IntegrityError):
        with transaction.atomic():
            Payment.objects.create(buyer=buyer, routine_product_id=product_id,
                                   money_type=MoneyType.ROUTINE_PRODUCT, amount=D("2500.00"),
                                   currency="SYP", status="pending")


def test_the_poller_confirms_a_product_session_and_it_delivers_like_a_wallet_sale(
        make_user, api, shamcash, monkeypatch, django_capture_on_commit_callbacks):
    from notifications.models import Notification
    from routine.models import RoutinePurchase
    from subscription.models import Payment
    from wallet.models import Transaction

    trainer, buyer, c, product_id, body = _product_session(make_user, api, "hub5")
    _aged(body["id"])
    escrow, platform = _ledger()
    _account(monkeypatch, [_txn("tx-hub5", "2500.00", note=f"yalla {body['reference']}")])

    with django_capture_on_commit_callbacks(execute=True):
        assert _poll()["completed"] == 1

    payment = Payment.objects.get(pk=body["id"])
    assert (payment.status, payment.transaction_id) == ("completed", "tx-hub5")
    assert payment.invoice_number and payment.payout_settled_at is not None, \
        "delivered with the completion: nothing is left owed"
    purchase = RoutinePurchase.objects.get(buyer=buyer, product_id=product_id)
    assert (purchase.status, purchase.paid_with, purchase.checkout_reference,
            purchase.money_type) == ("confirmed", "shamcash", body["reference"],
                                     "ROUTINE_PRODUCT")
    assert (purchase.amount, purchase.commission_rate, purchase.commission,
            purchase.trainer_share, purchase.trainer_amount) == \
        (D("2500.00"), D("0.2000"), D("500.00"), D("2000.00"), D("2000.00"))
    copy = purchase.routine
    assert copy.created_by_id == buyer.pk and list(copy.assigned_to.all()) == [buyer]

    legs = Transaction.objects.filter(metadata__purchase_id=purchase.pk).order_by("id")
    assert [(leg.metadata["purpose"], leg.amount, leg.money_type) for leg in legs] == [
        ("routine_product_checkout", D("2500.00"), "ROUTINE_PRODUCT"),
        ("routine_product_sale", D("2000.00"), "ROUTINE_PRODUCT"),
        ("routine_product_commission", D("500.00"), "ROUTINE_PRODUCT")]
    assert _wallet(trainer) == D("2000.00")
    assert _ledger() == (escrow, platform + D("500.00")), "escrow nets to zero"
    assert _wallet(buyer) == D("0.00"), "the money came from ShamCash, not the wallet"

    session = c.get(f"{SESSIONS}{body['id']}/", **WEB).json()
    assert (session["status"], session["expires_at"]) == ("confirmed", None)
    assert session["result"] == {"purchase_id": purchase.pk, "purchase_status": "confirmed",
                                 "routine_id": copy.pk}
    assert _steps(body["id"]) == [("", "pending", "customer"),
                                  ("pending", "completed", "poller")]
    assert Notification.objects.filter(recipient=buyer,
                                       event_type="routine_product_purchased").exists()
    assert Notification.objects.filter(recipient=trainer,
                                       event_type="routine_product_sold").exists()
    assert c.get(SESSIONS, **WEB).json()["results"] == [], "a confirmed session is not open"

    # The same transfer read again by the sweep of unclaimed money confirms nothing twice.
    from subscription.tasks import sweep_unmatched_transfers

    with django_capture_on_commit_callbacks(execute=True):
        assert sweep_unmatched_transfers()["seen"] == 1
    assert RoutinePurchase.objects.filter(buyer=buyer, product_id=product_id).count() == 1
    assert _wallet(trainer) == D("2000.00")


def test_the_commission_is_the_one_the_session_opened_with(
        make_user, api, shamcash, settings, django_capture_on_commit_callbacks):
    from routine.models import RoutinePurchase
    from subscription.services.payment_service import PaymentService

    _trainer, buyer, _c, product_id, body = _product_session(make_user, api, "hub6")
    settings.COACHING_COMMISSION_RATE = D("0.30")
    with django_capture_on_commit_callbacks(execute=True):
        PaymentService.complete_payment(body["id"], {
            "amount": "2500.00", "currency": "SYP", "status": "completed",
            "transaction_id": "tx-hub6"})
    purchase = RoutinePurchase.objects.get(buyer=buyer, product_id=product_id)
    assert (purchase.commission_rate, purchase.commission) == (D("0.2000"), D("500.00"))


def test_i_paid_confirms_a_product_session_through_the_payment_routes(
        make_user, api, shamcash, monkeypatch, django_capture_on_commit_callbacks):
    from routine.models import RoutinePurchase

    _trainer, buyer, c, product_id, body = _product_session(make_user, api, "hub9")
    _paid(monkeypatch, body["reference"], "2500.00")
    with django_capture_on_commit_callbacks(execute=True):
        resp = c.post(f"/api/subscription/v1/payments/{body['id']}/reconcile/", **WEB)
    assert (resp.status_code, resp.json()["status"]) == (200, "completed"), resp.content
    assert RoutinePurchase.objects.filter(buyer=buyer, product_id=product_id,
                                          paid_with="shamcash").exists()
    status = c.get(f"/api/subscription/v1/payments/{body['id']}/status/", **WEB)
    assert (status.status_code, status.json()["status"]) == (200, "completed")
    assert status.json()["events"][-1]["by"] == "customer"
    # Nobody else's.
    other = api(_person(make_user, "hub9_other"))
    assert other.get(f"{SESSIONS}{body['id']}/", **WEB).status_code == 404
    assert other.post(f"/api/subscription/v1/payments/{body['id']}/reconcile/",
                      **WEB).status_code == 404


def test_a_program_taken_off_sale_after_its_session_opened_is_still_delivered(
        make_user, api, shamcash, django_capture_on_commit_callbacks):
    """The buyer was offered it at that price and pays it: the open session stays theirs
    (asking again answers it) and its money delivers the copy. Nobody new can open one."""
    from routine import products
    from routine.models import RoutinePurchase
    from subscription.services.payment_service import PaymentService

    trainer, buyer, c, product_id, body = _product_session(make_user, api, "hub14")
    products.retire(trainer, product_id)
    again = _session(c, "ROUTINE_PRODUCT", product_id)
    assert (again.status_code, again.json()["id"]) == (200, body["id"]), again.content
    with django_capture_on_commit_callbacks(execute=True):
        PaymentService.complete_payment(body["id"], {
            "amount": "2500.00", "currency": "SYP", "status": "completed",
            "transaction_id": "tx-hub14"})
    assert RoutinePurchase.objects.filter(buyer=buyer, product_id=product_id,
                                          status="confirmed").exists()
    late = _session(api(_person(make_user, "hub14_late")), "ROUTINE_PRODUCT", product_id)
    assert (late.status_code, late.json()["code"]) == (404, "not_found")


def test_a_share_that_crosses_currencies_is_exchanged_at_the_rate_the_price_was_locked_at(
        make_user, api, shamcash, django_capture_on_commit_callbacks):
    """A dollar buyer, a pound trainer: the price is converted when the session opens
    (1 USD for 13,000 SYP) and the trainer's share goes back across at THAT rate, not at
    the one an operator sets while the transfer is on its way."""
    from routine.models import RoutinePurchase
    from subscription.services.payment_service import PaymentService
    from subscription.settings import gateway_config
    from tests.test_fx_subs import _rate
    from wallet.utils import get_escrow_wallet

    limits = gateway_config.GATEWAY_REGISTRY["shamcash"]["amount_limits"]
    before = dict(limits)
    limits["USD"] = {"min": D("0.50"), "max": D("5000")}
    try:
        _rate("13000.000000")
        trainer, _template, product_id = _published(make_user, api, "hub15", price="13000.00")
        buyer = _person(make_user, "hub15_b", currency="USD")
        opened = _session(api(buyer), "ROUTINE_PRODUCT", product_id)
        assert opened.status_code == 201, opened.content
        body = opened.json()
        assert (body["amount"], body["currency"]) == ("1.00", "USD")
        escrow = get_escrow_wallet("USD").balance

        _rate("20000.000000", ago=timedelta(seconds=0))
        with django_capture_on_commit_callbacks(execute=True):
            PaymentService.complete_payment(body["id"], {
                "amount": "1.00", "currency": "USD", "status": "completed",
                "transaction_id": "tx-hub15"})
    finally:
        limits.clear()
        limits.update(before)
    purchase = RoutinePurchase.objects.get(buyer=buyer, product_id=product_id)
    assert (purchase.amount, purchase.currency, purchase.commission, purchase.trainer_share) \
        == (D("1.00"), "USD", D("0.20"), D("0.80"))
    assert (purchase.trainer_amount, purchase.trainer_currency) == (D("10400.00"), "SYP")
    assert _wallet(trainer) == D("10400.00")
    assert get_escrow_wallet("USD").balance == escrow


# ============================================================ grants nothing
def test_a_product_session_that_expires_or_is_rejected_delivers_nothing(
        make_user, api, shamcash, monkeypatch, django_capture_on_commit_callbacks):
    from notifications.models import Notification
    from routine.models import RoutinePurchase
    from subscription.models import Payment, TransferReview
    from subscription.services.payment_service import PaymentService

    trainer, buyer, c, product_id, body = _product_session(make_user, api, "hub7")
    _aged(body["id"], timedelta(hours=2))
    _account(monkeypatch, [])
    with django_capture_on_commit_callbacks(execute=True):
        assert _poll()["expired"] == 1
    expired = c.get(f"{SESSIONS}{body['id']}/", **WEB).json()
    assert (expired["status"], expired["expires_at"]) == ("expired", None)
    assert Notification.objects.filter(recipient=buyer, event_type="checkout_expired",
                                       related_object_id=body["id"]).exists()

    # A new session once the old one closed; this time the money is not what it asked.
    second = _session(c, "ROUTINE_PRODUCT", product_id)
    assert second.status_code == 201 and second.json()["id"] != body["id"]
    second = second.json()
    _aged(second["id"])
    _account(monkeypatch, [_txn("tx-hub7", "900.00", note=second["reference"])])
    with django_capture_on_commit_callbacks(execute=True):
        assert _poll()["held"] == 1
    held = c.get(f"{SESSIONS}{second['id']}/", **WEB).json()
    assert (held["status"], held["review"]["reason"]) == ("needs_review", "amount_differs")
    review = TransferReview.objects.get(transaction_id="tx-hub7")
    with django_capture_on_commit_callbacks(execute=True):
        PaymentService.reject_review(review.pk, _operator(make_user, "hub7_op"),
                                     "returned by hand")
    assert c.get(f"{SESSIONS}{second['id']}/", **WEB).json()["status"] == "rejected"
    assert Notification.objects.filter(recipient=buyer, event_type="payment_rejected").exists()

    assert not RoutinePurchase.objects.filter(buyer=buyer).exists()
    assert _wallet(trainer) == D("0.00")
    assert set(Payment.objects.filter(buyer=buyer).values_list("status", flat=True)) == \
        {"expired", "rejected"}


def test_an_operator_accepts_a_near_miss_and_the_split_is_of_what_arrived(
        make_user, api, shamcash, monkeypatch, django_capture_on_commit_callbacks):
    from routine.models import RoutinePurchase
    from subscription.models import TransferReview
    from subscription.services.payment_service import PaymentService

    trainer, buyer, c, product_id, body = _product_session(make_user, api, "hub8")
    _aged(body["id"])
    _account(monkeypatch, [_txn("tx-hub8", "2400.00", note=body["reference"])])
    with django_capture_on_commit_callbacks(execute=True):
        _poll()
    # Held, it is still the open session: asking again answers it.
    again = _session(c, "ROUTINE_PRODUCT", product_id)
    assert (again.status_code, again.json()["id"], again.json()["status"]) == \
        (200, body["id"], "needs_review")

    review = TransferReview.objects.get(transaction_id="tx-hub8")
    with django_capture_on_commit_callbacks(execute=True):
        PaymentService.approve_review(review.pk, _operator(make_user, "hub8_op"),
                                      "short by 100, accepted")
    purchase = RoutinePurchase.objects.get(buyer=buyer, product_id=product_id)
    assert (purchase.amount, purchase.commission, purchase.trainer_amount) == \
        (D("2400.00"), D("480.00"), D("1920.00"))
    assert _wallet(trainer) == D("1920.00")
    assert _steps(body["id"])[-1] == ("needs_review", "completed", "operator")


def test_money_for_a_program_already_owned_is_held_never_delivered_twice(
        make_user, api, shamcash, monkeypatch, django_capture_on_commit_callbacks):
    from routine.models import RoutinePurchase
    from subscription.models import Payment, TransferReview
    from subscription.services.payment_service import PaymentService, ReviewConflict
    from subscription.tasks import poll_expired_checkouts

    trainer, buyer, c, product_id, body = _product_session(make_user, api, "hub11")
    _aged(body["id"], timedelta(hours=2))
    _account(monkeypatch, [])
    with django_capture_on_commit_callbacks(execute=True):
        _poll()
    assert Payment.objects.get(pk=body["id"]).status == "expired"
    # Closed, so the wallet may sell it; then the transfer for the old checkout lands.
    _fund(buyer, "10000.00")
    assert _buy(api, buyer, product_id).status_code == 200
    trainer_after_sale = _wallet(trainer)
    _account(monkeypatch, [_txn("tx-hub11", "2500.00", note=body["reference"])])
    with django_capture_on_commit_callbacks(execute=True):
        poll_expired_checkouts()

    payment = Payment.objects.get(pk=body["id"])
    review = TransferReview.objects.get(transaction_id="tx-hub11")
    assert (payment.status, review.reason, review.payment_id) == \
        ("needs_review", "already_owned", payment.pk)
    assert RoutinePurchase.objects.filter(buyer=buyer, product_id=product_id).count() == 1
    assert _wallet(trainer) == trainer_after_sale
    with pytest.raises(ReviewConflict):
        PaymentService.approve_review(review.pk, _operator(make_user, "hub11_op"), "")
    assert RoutinePurchase.objects.filter(buyer=buyer, product_id=product_id).count() == 1


# ============================================================ the refund
def test_either_operator_refund_reverses_the_checkout_sale_and_revokes_the_copy(
        make_user, api, client, shamcash, django_capture_on_commit_callbacks):
    from routine.models import Routine, RoutinePurchase
    from subscription.models import Payment
    from subscription.services.payment_service import By, PaymentService
    from wallet.models import Transaction

    escrow, platform = _ledger()
    operator = _operator(make_user, "hub10_op")
    for door, tag in (("purchase", "hub10a"), ("payment", "hub10b")):
        trainer, buyer, c, product_id, body = _product_session(make_user, api, tag)
        with django_capture_on_commit_callbacks(execute=True):
            PaymentService.complete_payment(body["id"], {
                "amount": "2500.00", "currency": "SYP", "status": "completed",
                "transaction_id": f"tx-{tag}"})
        purchase = RoutinePurchase.objects.get(buyer=buyer, product_id=product_id)
        with django_capture_on_commit_callbacks(execute=True):
            if door == "purchase":
                # The console's purchase refund, the existing operator door.
                resp = _console(client, operator, "/dj-admin/routine/routinepurchase/",
                                "refund", [purchase.pk], reason="لم يطابق الوصف")
                assert resp.status_code == 302
            else:
                PaymentService.refund_payment(body["id"], "chargeback", by=By.OPERATOR,
                                              actor=operator)
        purchase.refresh_from_db()
        payment = Payment.objects.get(pk=body["id"])
        assert (purchase.status, payment.status) == ("refunded", "refunded"), door
        assert _steps(body["id"])[-1] == ("completed", "refunded", "operator"), door
        copy = Routine.objects.get(purchase=purchase)
        assert copy.is_active is False and not copy.assigned_to.exists(), door
        reversals = Transaction.objects.filter(metadata__purchase_id=purchase.pk,
                                               tx_type="reversal")
        assert reversals.count() == 3, door
        assert set(reversals.values_list("money_type", flat=True)) == {"ROUTINE_PRODUCT"}
        assert _wallet(trainer) == D("0.00"), door
        assert _wallet(buyer) == D("0.00"), "the money goes back by hand, not to the wallet"
        assert c.get(f"{SESSIONS}{body['id']}/", **WEB).json()["status"] == "refunded"
    assert _ledger() == (escrow, platform), "every unit that came in went out again"


# ============================================================ reading
def test_sessions_are_the_callers_own_and_a_store_build_reads_no_price(
        make_user, api, shamcash):
    _trainer, buyer, c, _product_id, body = _product_session(make_user, api, "hub12")
    plan = _plan("hub12", "13000.00")
    plan_session = _session(c, "PLATFORM_SUB", plan.id).json()

    listed = c.get(SESSIONS, **WEB).json()["results"]
    assert [s["id"] for s in listed] == [plan_session["id"], body["id"]], "newest first"
    for channel in ("ios", "play", ""):
        detail = c.get(f"{SESSIONS}{body['id']}/", HTTP_X_APP_CHANNEL=channel)
        assert detail.status_code == 200
        assert detail.json()["status"] == "open"
        for key in ("amount", "reference", "payment_target"):
            assert key not in detail.json(), (channel, key)
        for row in c.get(SESSIONS, HTTP_X_APP_CHANNEL=channel).json()["results"]:
            assert not {"amount", "reference", "payment_target"} & set(row), channel

    stranger = api(_person(make_user, "hub12_other"))
    resp = stranger.get(f"{SESSIONS}{body['id']}/", **WEB)
    assert (resp.status_code, resp.json()["code"]) == (404, "not_found")
    assert stranger.get(SESSIONS, **WEB).json()["results"] == []


# ============================================================ the adapter
def test_every_checkout_opens_through_the_payment_step_adapter(
        make_user, api, shamcash, monkeypatch):
    from subscription import payment_step as step
    from subscription.models import Subscription

    assert isinstance(step.payment_step(), step.ManualTransferAdapter)
    assert set(step.ADAPTERS) == {"manual_transfer", "shamcash_hosted"}
    with pytest.raises(NotImplementedError):
        step.ShamCashHostedAdapter().opener(None, metadata={})

    opened = []
    real = step.ManualTransferAdapter.opener

    def spy(self, request, *, metadata, service=None):
        opened.append(dict(metadata))
        return real(self, request, metadata=metadata, service=service)

    monkeypatch.setattr(step.ManualTransferAdapter, "opener", spy)
    # The app's Pay button and the hub's product session both open through it.
    plan = _plan("hub13", "13000.00")
    user = _person(make_user, "hub13")
    c = api(user)
    assert c.post("/api/subscription/v1/subscriptions/", data=json.dumps(
        {"plan_id": plan.id}), content_type=JSON).status_code == 201
    assert _checkout(c, Subscription.objects.get(user=user)).status_code == 200
    _trainer, _buyer, _c, product_id, _body = _product_session(make_user, api, "hub13")
    assert [sorted(m) for m in opened] == [["subscription_id"], ["product_id"]]
    assert opened[1]["product_id"] == str(product_id)
