"""The review queue: money that is almost right is held for a person, never dropped
(the spec, section 7; BACKEND_BUGS B-051).

"Right amount with wrong or missing reference, or right reference with wrong amount,
goes to NEEDS_REVIEW with the evidence attached, never silently dropped." And: "the
same ShamCash transaction cannot confirm two sessions."
"""
import json
from datetime import timedelta
from decimal import Decimal

import pytest
from django.utils import timezone

from tests.test_backend_bugs_app_0923b import _plan
from tests.test_fx_subs import WEBHOOK_SECRET, _checkout, _person, _post, _subscribe, shamcash  # noqa: F401

pytestmark = pytest.mark.django_db

D = Decimal


def _account(monkeypatch, txns):
    """The merchant account holds exactly `txns`."""
    from subscription.gateways.shamcash import ShamCashGateway

    monkeypatch.setattr(ShamCashGateway, "_get",
                        lambda self, path, params: {"data": list(txns)})


def _txn(tx_id, amount, *, note="", currency="SYP", sender="shamcash:0933000000"):
    return {"id": tx_id, "amount": str(amount), "currency": currency, "note": note,
            "sender": sender, "occurred_at": timezone.now().isoformat()}


def _open_checkout(api, make_user, name, plan, *, age=timedelta(minutes=1)):
    """A customer with an open checkout for `plan`, `age` old (past the poll grace)."""
    from subscription.models import Payment

    user = _person(make_user, name)
    c, sub = _subscribe(api, user, plan)
    assert _checkout(c, sub).status_code == 200
    payment = Payment.objects.get(subscription=sub)
    Payment.objects.filter(pk=payment.pk).update(created_at=timezone.now() - age)
    payment.refresh_from_db()
    return c, sub, payment


def _poll():
    from subscription.tasks import poll_open_payments
    return poll_open_payments()


def _sweep():
    from subscription.tasks import sweep_unmatched_transfers
    return sweep_unmatched_transfers()


# ------------------------------------------------------------------ near misses
def test_the_reference_with_another_amount_is_held_not_dropped(
        make_user, api, shamcash, monkeypatch, django_capture_on_commit_callbacks):
    from notifications.models import Notification
    from subscription.models import Payment, TransferReview

    plan = _plan("rv1", "10000.00")
    c, sub, payment = _open_checkout(api, make_user, "rv_near", plan)
    ref = payment.gateway_transaction_reference
    _account(monkeypatch, [_txn("tx-near-1", "9000.00", note=f"pay {ref}")])

    with django_capture_on_commit_callbacks(execute=True):
        assert _poll()["held"] == 1

    payment.refresh_from_db()
    review = TransferReview.objects.get(transaction_id="tx-near-1")
    assert payment.status == "needs_review"
    assert (review.reason, review.amount, review.payment_id, review.status) == \
        ("amount_differs", D("9000.00"), payment.id, "open")
    assert review.note == f"pay {ref}" and review.sender == "shamcash:0933000000"
    assert Notification.objects.filter(recipient=sub.user,
                                       event_type="payment_needs_review").exists()

    status = c.get(f"/api/subscription/v1/payments/{payment.id}/status/").json()
    assert status["status"] == "needs_review" and status["expires_at"] is None
    assert status["review"]["reason"] == "amount_differs"
    assert "9000" not in json.dumps(status["review"]), "the transfer stays in the console"

    # Hours later it is still held: money was seen, a person decides, not the clock.
    Payment.objects.filter(pk=payment.pk).update(created_at=timezone.now() - timedelta(days=2))
    _poll()
    payment.refresh_from_db()
    assert payment.status == "needs_review"
    assert TransferReview.objects.count() == 1, "the same transfer is queued once"


def test_an_exact_transfer_that_arrives_meanwhile_completes_it(
        make_user, api, shamcash, monkeypatch, django_capture_on_commit_callbacks):
    from subscription.models import Payment

    plan = _plan("rv2", "10000.00")
    c, sub, payment = _open_checkout(api, make_user, "rv_exact", plan)
    ref = payment.gateway_transaction_reference
    _account(monkeypatch, [_txn("tx-e1", "9000.00", note=ref)])
    with django_capture_on_commit_callbacks(execute=True):
        _poll()
    _account(monkeypatch, [_txn("tx-e1", "9000.00", note=ref),
                           _txn("tx-e2", "10000.00", note=ref)])
    with django_capture_on_commit_callbacks(execute=True):
        assert _poll()["completed"] == 1
    payment.refresh_from_db()
    assert (payment.status, payment.transaction_id) == ("completed", "tx-e2")


# ------------------------------------------------------------------ decisions
def test_approving_accepts_what_arrived_and_completes_the_checkout(
        make_user, api, shamcash, monkeypatch, django_capture_on_commit_callbacks):
    from subscription.models import Payment, Subscription, TransferReview
    from subscription.services.payment_service import PaymentService

    plan = _plan("rv3", "10000.00")
    c, sub, payment = _open_checkout(api, make_user, "rv_approve", plan)
    _account(monkeypatch, [_txn("tx-a1", "9500.00",
                                note=payment.gateway_transaction_reference)])
    with django_capture_on_commit_callbacks(execute=True):
        _poll()
    operator = make_user("rv_operator", user_type="admin")
    review = TransferReview.objects.get(transaction_id="tx-a1")

    with django_capture_on_commit_callbacks(execute=True):
        PaymentService.approve_review(review.id, operator, "customer short by 500, accepted")

    payment.refresh_from_db()
    review.refresh_from_db()
    assert (payment.status, payment.amount, payment.transaction_id) == \
        ("completed", D("9500.00"), "tx-a1")
    assert payment.metadata["review"]["price"] == "10000.00"
    assert (review.status, review.resolved_by_id) == ("approved", operator.id)
    assert Subscription.objects.get(pk=sub.pk).is_active
    receipt = c.get(f"/api/subscription/v1/payments/{payment.id}/receipt/").json()["receipt"]
    assert receipt["amount"] == "9500.00", "the receipt says what was received"


def test_rejecting_frees_the_customer_to_pay_again_and_tells_them(
        make_user, api, shamcash, monkeypatch, django_capture_on_commit_callbacks):
    from notifications.models import Notification
    from subscription.models import Payment, TransferReview
    from subscription.services.payment_service import PaymentService

    plan = _plan("rv4", "10000.00")
    c, sub, payment = _open_checkout(api, make_user, "rv_reject", plan)
    _account(monkeypatch, [_txn("tx-r1", "3.00", note=payment.gateway_transaction_reference)])
    with django_capture_on_commit_callbacks(execute=True):
        _poll()
    review = TransferReview.objects.get(transaction_id="tx-r1")
    operator = make_user("rv_op4", user_type="admin")

    with django_capture_on_commit_callbacks(execute=True):
        PaymentService.reject_review(review.id, operator, "returned to sender by hand")

    payment.refresh_from_db()
    assert payment.status == "rejected"
    assert Notification.objects.filter(recipient=sub.user, event_type="payment_rejected").exists()
    again = _checkout(c, sub)
    assert again.status_code == 200 and again.json()["payment_id"] != str(payment.id)
    status = c.get(f"/api/subscription/v1/payments/{payment.id}/status/").json()
    assert status["review"]["reason"] == "amount_differs"


def test_a_transfer_in_another_currency_cannot_be_approved(
        make_user, api, shamcash, monkeypatch, django_capture_on_commit_callbacks):
    from subscription.models import TransferReview
    from subscription.services.payment_service import PaymentService, ReviewConflict

    plan = _plan("rv5", "10000.00")
    c, sub, payment = _open_checkout(api, make_user, "rv_cur", plan)
    _account(monkeypatch, [_txn("tx-c1", "10000.00", currency="USD",
                                note=payment.gateway_transaction_reference)])
    with django_capture_on_commit_callbacks(execute=True):
        _poll()
    review = TransferReview.objects.get(transaction_id="tx-c1")
    assert review.reason == "currency_differs"
    with pytest.raises(ReviewConflict):
        PaymentService.approve_review(review.id, make_user("rv_op5", user_type="admin"))


# ------------------------------------------------------------------ the sweep
def test_the_right_amount_with_no_reference_holds_the_one_checkout_it_fits(
        make_user, api, shamcash, monkeypatch, django_capture_on_commit_callbacks):
    from subscription.models import Payment, TransferReview

    plan = _plan("rv6", "12345.00")
    c, sub, payment = _open_checkout(api, make_user, "rv_noref", plan)
    _account(monkeypatch, [_txn("tx-n1", "12345.00", note="for my subscription")])

    with django_capture_on_commit_callbacks(execute=True):
        assert _sweep()["held"] == 1

    payment.refresh_from_db()
    review = TransferReview.objects.get(transaction_id="tx-n1")
    assert payment.status == "needs_review"
    assert (review.reason, review.payment_id) == ("reference_missing", payment.id)


def test_an_amount_several_checkouts_fit_is_queued_alone_with_its_candidates(
        make_user, api, shamcash, monkeypatch, django_capture_on_commit_callbacks):
    from subscription.models import Payment, TransferReview

    plan = _plan("rv7", "7777.00")
    _c1, _s1, first = _open_checkout(api, make_user, "rv_two_a", plan)
    _c2, _s2, second = _open_checkout(api, make_user, "rv_two_b", plan)
    _account(monkeypatch, [_txn("tx-s1", "7777.00"), _txn("tx-s2", "1.23")])

    with django_capture_on_commit_callbacks(execute=True):
        assert _sweep()["queued"] == 2

    several = TransferReview.objects.get(transaction_id="tx-s1")
    assert (several.reason, several.payment_id) == ("several_candidates", None)
    assert set(several.candidate_payment_ids) == {str(first.id), str(second.id)}
    assert TransferReview.objects.get(transaction_id="tx-s2").reason == "no_candidate"
    assert set(Payment.objects.filter(pk__in=[first.pk, second.pk])
               .values_list("status", flat=True)) == {"pending"}, "nobody told a guess"


def test_a_second_transfer_for_a_paid_checkout_is_queued_as_money_to_return(
        make_user, api, shamcash, monkeypatch, django_capture_on_commit_callbacks):
    from subscription.models import Payment, TransferReview

    plan = _plan("rv8", "10000.00")
    c, sub, payment = _open_checkout(api, make_user, "rv_dup", plan)
    ref = payment.gateway_transaction_reference
    _account(monkeypatch, [_txn("tx-d1", "10000.00", note=ref)])
    with django_capture_on_commit_callbacks(execute=True):
        _poll()
    payment.refresh_from_db()
    assert payment.status == "completed"

    _account(monkeypatch, [_txn("tx-d1", "10000.00", note=ref),
                           _txn("tx-d2", "10000.00", note=ref.lower())])
    with django_capture_on_commit_callbacks(execute=True):
        first = _sweep()
        second = _sweep()
    assert (first["queued"], second["queued"]) == (1, 0), "queued once, never twice"
    review = TransferReview.objects.get()
    assert (review.transaction_id, review.reason, review.payment_id) == \
        ("tx-d2", "duplicate_transfer", payment.id)
    payment.refresh_from_db()
    assert payment.status == "completed", "a paid checkout is left as it is"


def test_a_queued_transfer_is_attached_then_approved(
        make_user, api, shamcash, monkeypatch, django_capture_on_commit_callbacks):
    from subscription.models import Payment, TransferReview
    from subscription.services.payment_service import PaymentService

    plan = _plan("rv9", "5000.00")
    c, sub, payment = _open_checkout(api, make_user, "rv_attach", plan)
    _account(monkeypatch, [_txn("tx-q1", "5000.00", note="yalla")])
    Payment.objects.filter(pk=payment.pk).update(amount=D("5001.00"))  # no amount match
    with django_capture_on_commit_callbacks(execute=True):
        _sweep()
    review = TransferReview.objects.get(transaction_id="tx-q1")
    assert (review.reason, review.payment_id) == ("no_candidate", None)
    operator = make_user("rv_op9", user_type="admin")

    with django_capture_on_commit_callbacks(execute=True):
        PaymentService.attach_review(review.id, payment.id, operator)
        PaymentService.approve_review(review.id, operator, "confirmed by phone")
    payment.refresh_from_db()
    assert (payment.status, payment.amount) == ("completed", D("5000.00"))


def test_a_transfer_holding_a_checkout_cannot_be_dismissed(
        make_user, api, shamcash, monkeypatch, django_capture_on_commit_callbacks):
    from subscription.models import TransferReview
    from subscription.services.payment_service import PaymentService, ReviewConflict

    plan = _plan("rv10", "10000.00")
    c, sub, payment = _open_checkout(api, make_user, "rv_dismiss", plan)
    _account(monkeypatch, [_txn("tx-x1", "1.00", note=payment.gateway_transaction_reference),
                           _txn("tx-x2", "42.00", note="not ours")])
    with django_capture_on_commit_callbacks(execute=True):
        _poll()
        _sweep()
    operator = make_user("rv_op10", user_type="admin")
    with pytest.raises(ReviewConflict):
        PaymentService.dismiss_review(
            TransferReview.objects.get(transaction_id="tx-x1").id, operator)
    alone = PaymentService.dismiss_review(
        TransferReview.objects.get(transaction_id="tx-x2").id, operator, "a supplier refund")
    assert alone.status == "dismissed"


def test_one_transfer_confirms_one_payment(
        make_user, api, shamcash, monkeypatch, django_capture_on_commit_callbacks):
    """A transfer that confirmed a checkout is never queued; one queued against another
    checkout cannot be approved there once it confirmed the first."""
    from subscription.models import Payment, TransferReview
    from subscription.services.payment_service import PaymentService, ReviewConflict

    plan = _plan("rv11", "10000.00")
    c1, s1, first = _open_checkout(api, make_user, "rv_once_a", plan)
    c2, s2, second = _open_checkout(api, make_user, "rv_once_b", plan)
    _account(monkeypatch, [_txn("tx-o1", "9000.00", note=second.gateway_transaction_reference)])
    with django_capture_on_commit_callbacks(execute=True):
        _poll()
    review = TransferReview.objects.get(transaction_id="tx-o1")
    # The same transaction id turns up as the exact payment of the first checkout.
    with django_capture_on_commit_callbacks(execute=True):
        PaymentService.complete_payment(first.id, {
            "amount": "10000.00", "currency": "SYP", "status": "completed",
            "transaction_id": "tx-o1", "event_id": "tx-o1"})
    with pytest.raises(ReviewConflict):
        PaymentService.approve_review(review.id, make_user("rv_op11", user_type="admin"))
    assert Payment.objects.get(pk=second.pk).status == "needs_review"


# ------------------------------------------------------------------ webhook, reconcile
def test_a_webhook_with_another_amount_is_held_and_acknowledged(
        make_user, api, shamcash, django_capture_on_commit_callbacks):
    import hashlib
    import hmac
    import time

    from django.test import Client

    from subscription.models import Payment, TransferReview

    plan = _plan("rv12", "10000.00")
    c, sub, payment = _open_checkout(api, make_user, "rv_hook", plan)
    body = json.dumps({"reference": payment.gateway_transaction_reference, "status": "completed",
                       "amount": "8000.00", "currency": "SYP", "transaction_id": "tx-w1",
                       "event_id": "tx-w1"}).encode()
    ts = str(int(time.time()))
    sig = hmac.new(WEBHOOK_SECRET.encode(), f"{ts}.".encode() + body, hashlib.sha256).hexdigest()
    headers = {f"HTTP_{k.upper().replace('-', '_')}": v for k, v in {
        shamcash.get("signature_header", "X-ShamCash-Signature"): sig,
        shamcash.get("timestamp_header", "X-ShamCash-Timestamp"): ts}.items()}
    with django_capture_on_commit_callbacks(execute=True):
        resp = Client().post("/api/subscription/webhook/shamcash/", data=body,
                             content_type="application/json", **headers)
    assert resp.status_code == 200, resp.content
    payment.refresh_from_db()
    assert payment.status == "needs_review"
    assert TransferReview.objects.get(transaction_id="tx-w1").reason == "amount_differs"


def test_asking_again_answers_needs_review(
        make_user, api, shamcash, monkeypatch, django_capture_on_commit_callbacks):
    plan = _plan("rv13", "10000.00")
    c, sub, payment = _open_checkout(api, make_user, "rv_reconcile", plan)
    _account(monkeypatch, [_txn("tx-rc1", "9999.00", note=payment.gateway_transaction_reference)])
    with django_capture_on_commit_callbacks(execute=True):
        resp = _post(c, f"/api/subscription/v1/payments/{payment.id}/reconcile/")
    assert resp.status_code == 200 and resp.json()["status"] == "needs_review", resp.content


# ------------------------------------------------------------------ console, contract
def test_the_console_approves_through_the_payment_service(
        make_user, api, shamcash, monkeypatch, django_capture_on_commit_callbacks):
    from django.test import Client

    from subscription.models import Payment, TransferReview

    plan = _plan("rv14", "10000.00")
    c, sub, payment = _open_checkout(api, make_user, "rv_console", plan)
    _account(monkeypatch, [_txn("tx-k1", "9900.00", note=payment.gateway_transaction_reference)])
    with django_capture_on_commit_callbacks(execute=True):
        _poll()
    review = TransferReview.objects.get(transaction_id="tx-k1")
    operator = make_user("rv_op14", user_type="admin")
    console = Client()
    console.force_login(operator)
    with django_capture_on_commit_callbacks(execute=True):
        resp = console.post("/dj-admin/subscription/transferreview/",
                            {"action": "approve", "_selected_action": [str(review.id)]})
    assert resp.status_code in (200, 302), resp.status_code
    payment.refresh_from_db()
    review.refresh_from_db()
    assert (payment.status, review.status, review.resolved_by_id) == \
        ("completed", "approved", operator.id)


def test_the_status_contract_says_what_needs_review_means():
    from subscription.views import PaymentReconcileView, PaymentStatusView

    status_doc = PaymentStatusView.__doc__
    for word in ("needs_review", "rejected", "`review`", "reference_missing", "amount_differs"):
        assert word in status_doc, word
    assert "needs_review" in PaymentReconcileView.__doc__
