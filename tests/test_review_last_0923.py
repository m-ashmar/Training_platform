"""What the last adversarial review of 2026-09-23 found (L1..L15), in the payment review
queue (B-051), the reworked mid-period upgrade and foods' fibre.

Three reviewers read the combined branch (7479a05), one per change; one verifier
reproduced each finding. Every test here failed there for the reason its name gives.
"""
import json
from datetime import datetime, timedelta
from decimal import Decimal

import pytest
from django.utils import timezone

from tests.test_backend_bugs_app_0923b import _complete, _plan
from tests.test_fx_subs import (  # noqa: F401
    _checkout, _person, _post, _rate, _subscribe, dollars_chargeable, shamcash)
from tests.test_payment_review import _account, _open_checkout, _poll, _sweep, _txn
from tests.test_plan_upgrade import _active_on, _buy, _quote
from tests.test_plan_upgrade_review import _refund, _renew, _upgrade_paid

pytestmark = pytest.mark.django_db

D = Decimal


def _windowed_account(monkeypatch, txns):
    """The merchant account holds `txns`; each lookup answers only what falls in its
    window, at most PAGE_LIMIT rows, as the real one would."""
    from subscription.gateways.shamcash import ShamCashGateway

    def _get(self, path, params):
        lo = datetime.fromisoformat(params["start_at"])
        hi = datetime.fromisoformat(params["end_at"])
        rows = [t for t in txns
                if lo <= datetime.fromisoformat(t["occurred_at"]) <= hi]
        return {"data": rows[:self.PAGE_LIMIT]}

    monkeypatch.setattr(ShamCashGateway, "_get", _get)


def _hold(payment, tx_id, amount, *, note=None):
    from subscription.services.payment_service import PaymentService

    return PaymentService.hold_for_review(payment.id, {
        "transaction_id": tx_id, "amount": str(amount), "currency": payment.currency,
        "note": note if note is not None else payment.gateway_transaction_reference,
        "sender": "shamcash:0933000000"}, reason="amount_differs")


# ------------------------------------------------------------------ L1 / L11
def test_l1_a_held_upgrade_checkout_is_not_handed_back_as_payable(
        make_user, api, shamcash, django_capture_on_commit_callbacks):
    from subscription.models import Payment

    cap = django_capture_on_commit_callbacks
    basic = _plan("l1 basic", "10000.00", days=30)
    premium = _plan("l1 premium", "20000.00", days=30)
    c, sub = _active_on(api, make_user, "l1_up", basic, cap, days_left=20)
    first = _buy(c, sub, premium, amount="13333.33", currency="SYP")
    assert first.status_code == 200, first.content
    up = Payment.objects.get(pk=first.json()["payment_id"])
    with cap(execute=True):
        _hold(up, "tx-l1", "13000.00")
    up.refresh_from_db()
    assert up.status == "needs_review"

    again = _buy(c, sub, premium, amount="13333.33", currency="SYP")
    body = again.json()
    assert again.status_code == 409 and body.get("code") == "checkout_open", \
        (again.status_code, body)


# ------------------------------------------------------------------ L2 (a)
def test_l2a_a_transfer_that_paid_the_held_checkout_is_not_left_open_in_the_queue(
        make_user, api, shamcash, monkeypatch, django_capture_on_commit_callbacks):
    from subscription.models import Payment, TransferReview

    plan = _plan("l2a", "10000.00")
    c, sub, payment = _open_checkout(api, make_user, "l2a_user", plan)
    ref = payment.gateway_transaction_reference
    _account(monkeypatch, [_txn("tx-l2a-1", "9000.00", note=ref)])
    with django_capture_on_commit_callbacks(execute=True):
        _poll()
    payment.refresh_from_db()
    assert payment.status == "needs_review"

    _account(monkeypatch, [_txn("tx-l2a-1", "9000.00", note=ref),
                           _txn("tx-l2a-2", "10000.00", note=ref)])
    with django_capture_on_commit_callbacks(execute=True):
        _sweep()            # the ten-minute sweep lands first
        _poll()             # then the minute poller matches the exact transfer
    payment.refresh_from_db()
    assert (payment.status, payment.transaction_id) == ("completed", "tx-l2a-2")
    assert not TransferReview.objects.filter(transaction_id="tx-l2a-2",
                                             status="open").exists(), \
        list(TransferReview.objects.values_list("transaction_id", "reason", "status"))


# ------------------------------------------------------------------ L2 (b)
def test_l2b_a_checkout_closed_for_good_is_left_as_it_is(
        make_user, api, shamcash, monkeypatch, django_capture_on_commit_callbacks):
    from subscription.models import Payment, TransferReview
    from subscription.services.payment_service import PaymentService

    plan = _plan("l2b", "10000.00")
    c, sub, payment = _open_checkout(api, make_user, "l2b_user", plan)
    PaymentService.expire_payment(payment.id, "window closed")
    Payment.objects.filter(pk=payment.pk).update(created_at=timezone.now() - timedelta(hours=100))
    ref = payment.gateway_transaction_reference
    _account(monkeypatch, [_txn("tx-l2b", "10000.00", note=ref)])
    with django_capture_on_commit_callbacks(execute=True):
        _sweep()
    payment.refresh_from_db()
    review = TransferReview.objects.get(transaction_id="tx-l2b")
    assert review.reason == "checkout_closed"
    assert payment.status == "expired", payment.status


# ------------------------------------------------------------------ L3
def test_l3_a_reference_in_another_case_is_surfaced(
        make_user, api, shamcash, monkeypatch, django_capture_on_commit_callbacks):
    from subscription.models import Payment, TransferReview
    from subscription.services.payment_service import PaymentService
    from subscription.tasks import poll_expired_checkouts, sweep_unmatched_transfers

    plan = _plan("l3", "10000.00")
    c, sub, payment = _open_checkout(api, make_user, "l3_user", plan,
                                     age=timedelta(minutes=10))
    ref = payment.gateway_transaction_reference
    assert ref.startswith("SHAMCASH_")
    typed = "Shamcash_" + ref[len("SHAMCASH_"):]
    occurred = payment.created_at + timedelta(minutes=3)
    txn = {"id": "tx-l3", "amount": "10000.00", "currency": "SYP", "note": typed,
           "sender": "shamcash:0933000000", "occurred_at": occurred.isoformat()}
    _windowed_account(monkeypatch, [txn])

    with django_capture_on_commit_callbacks(execute=True):
        _poll()
        _sweep()
        # Time passing: the checkout closes — unless the transfer already settled it,
        # which, matched in any case, it now does within the minute.
        if Payment.objects.get(pk=payment.pk).status == "pending":
            PaymentService.expire_payment(payment.id, "window closed")
        poll_expired_checkouts()
        sweep_unmatched_transfers(now=payment.created_at + timedelta(hours=72, minutes=-5))
        sweep_unmatched_transfers(now=payment.created_at + timedelta(hours=72, minutes=5))
    payment.refresh_from_db()
    surfaced = (payment.status in ("completed", "needs_review")
                or TransferReview.objects.filter(transaction_id="tx-l3").exists())
    assert surfaced, payment.status


# ------------------------------------------------------------------ L4
def test_l4_the_export_does_not_hand_a_customer_another_senders_account(
        make_user, api, shamcash, monkeypatch, django_capture_on_commit_callbacks):
    from subscription.models import TransferReview
    from training_platform.privacy.registry import export_user_data

    plan = _plan("l4", "5000.00")
    c, sub, payment = _open_checkout(api, make_user, "l4_b", plan)
    _account(monkeypatch, [_txn("tx-l4", "5000.00", note="rent",
                                sender="shamcash:0999111222")])
    with django_capture_on_commit_callbacks(execute=True):
        _sweep()
    assert TransferReview.objects.get(transaction_id="tx-l4").payment_id == payment.id
    out = json.dumps(export_user_data(sub.user), default=str)
    assert "0999111222" not in out


# ------------------------------------------------------------------ L5
def test_l5_held_rows_do_not_starve_new_checkouts(
        make_user, api, shamcash, monkeypatch, settings, django_capture_on_commit_callbacks):
    from subscription.models import Payment

    settings.PAYMENT_POLL_BATCH = 2
    plan = _plan("l5", "10000.00")
    held = []
    for i in range(2):
        _c, _s, p = _open_checkout(api, make_user, f"l5_held{i}", plan,
                                   age=timedelta(minutes=30 + i))
        with django_capture_on_commit_callbacks(execute=True):
            _hold(p, f"tx-l5-h{i}", "9000.00")
        held.append(p)
    c, sub, fresh = _open_checkout(api, make_user, "l5_new", plan)
    _account(monkeypatch, [_txn("tx-l5-new", "10000.00",
                                note=fresh.gateway_transaction_reference)])
    with django_capture_on_commit_callbacks(execute=True):
        _poll()
    fresh.refresh_from_db()
    assert fresh.status == "completed", fresh.status


# ------------------------------------------------------------------ L6
def test_l6_a_reject_racing_an_attach_does_not_strand_the_checkout(
        make_user, api, shamcash, monkeypatch, django_capture_on_commit_callbacks):
    import subscription.services.payment_service as ps
    from subscription.models import Payment, TransferReview
    from subscription.services.payment_service import PaymentService, ReviewConflict

    plan = _plan("l6", "5000.00")
    c, sub, payment = _open_checkout(api, make_user, "l6_user", plan)
    _account(monkeypatch, [_txn("tx-l6", "4321.00", note="yalla")])
    with django_capture_on_commit_callbacks(execute=True):
        _sweep()
    review = TransferReview.objects.get(transaction_id="tx-l6")
    assert review.payment_id is None
    op_a = make_user("l6_opa", user_type="admin")
    op_b = make_user("l6_opb", user_type="admin")

    real = ps.transaction

    class _Interleave:
        fired = False

        def __getattr__(self, name):
            return getattr(real, name)

        def atomic(self, *a, **k):
            # B has read payment_id (None); A's attach commits before B's lock.
            if not _Interleave.fired:
                _Interleave.fired = True
                PaymentService.attach_review(review.id, payment.id, op_a)
            return real.atomic(*a, **k)

    monkeypatch.setattr(ps, "transaction", _Interleave())
    with django_capture_on_commit_callbacks(execute=True):
        try:
            PaymentService.reject_review(review.id, op_b, "not ours")
        except ReviewConflict:
            pass
    monkeypatch.setattr(ps, "transaction", real)
    payment.refresh_from_db()
    stranded = (payment.status == "needs_review"
                and not TransferReview.objects.filter(payment=payment, status="open").exists())
    assert not stranded, (payment.status, list(
        TransferReview.objects.values_list("status", "payment_id")))


# ------------------------------------------------------------------ L7
def test_l7_view_only_staff_cannot_approve(
        make_user, api, shamcash, monkeypatch, django_capture_on_commit_callbacks):
    from django.contrib.auth.models import Permission
    from django.test import Client

    from subscription.models import Payment, TransferReview

    plan = _plan("l7", "10000.00")
    c, sub, payment = _open_checkout(api, make_user, "l7_user", plan)
    _account(monkeypatch, [_txn("tx-l7", "9900.00", note=payment.gateway_transaction_reference)])
    with django_capture_on_commit_callbacks(execute=True):
        _poll()
    review = TransferReview.objects.get(transaction_id="tx-l7")
    viewer = make_user("l7_viewer", user_type="client", is_staff=True)
    viewer.user_permissions.add(Permission.objects.get(
        codename="view_transferreview", content_type__app_label="subscription"))
    console = Client()
    console.force_login(viewer)
    assert console.get("/dj-admin/subscription/transferreview/").status_code == 200
    with django_capture_on_commit_callbacks(execute=True):
        console.post("/dj-admin/subscription/transferreview/",
                     {"action": "approve", "_selected_action": [str(review.id)]})
    payment.refresh_from_db()
    review.refresh_from_db()
    assert (payment.status, review.status) == ("needs_review", "open"), \
        (payment.status, review.status)


# ------------------------------------------------------------------ L8
def test_l8_mark_as_completed_does_not_complete_a_held_payment(
        make_user, api, shamcash, monkeypatch, django_capture_on_commit_callbacks):
    from django.test import Client

    from subscription.models import Payment, TransferReview

    plan = _plan("l8", "10000.00")
    c, sub, payment = _open_checkout(api, make_user, "l8_user", plan)
    _account(monkeypatch, [_txn("tx-l8", "9000.00", note=payment.gateway_transaction_reference)])
    with django_capture_on_commit_callbacks(execute=True):
        _poll()
    payment.refresh_from_db()
    assert payment.status == "needs_review"
    operator = make_user("l8_op", user_type="admin")
    console = Client()
    console.force_login(operator)
    with django_capture_on_commit_callbacks(execute=True):
        console.post("/dj-admin/subscription/payment/",
                     {"action": "mark_as_completed", "_selected_action": [str(payment.id)]})
    payment.refresh_from_db()
    assert payment.status == "needs_review", (payment.status, payment.amount)


# ------------------------------------------------------------------ L9
def test_l9_a_busy_account_still_shows_the_sweep_the_newest_transfer(
        make_user, api, shamcash, monkeypatch, django_capture_on_commit_callbacks):
    from subscription.models import Payment, TransferReview

    plan = _plan("l9", "12345.00")
    c, sub, payment = _open_checkout(api, make_user, "l9_user", plan,
                                     age=timedelta(minutes=5))
    now = timezone.now()
    start = now - timedelta(hours=72)
    # 3000 transfers spread over the window (no id: the sweep skips them, but they fill
    # pages), then the one that matters, two minutes ago.
    noise = [{"amount": "1.00", "currency": "SYP", "note": "",
              "occurred_at": (start + timedelta(seconds=60 + i * 86)).isoformat()}
             for i in range(3000)]
    wanted = {"id": "tx-l9", "amount": "12345.00", "currency": "SYP", "note": "sub",
              "sender": "shamcash:0933000000",
              "occurred_at": (now - timedelta(minutes=2)).isoformat()}
    _windowed_account(monkeypatch, noise + [wanted])
    with django_capture_on_commit_callbacks(execute=True):
        _sweep()
    payment.refresh_from_db()
    assert TransferReview.objects.filter(transaction_id="tx-l9").exists(), payment.status


# ------------------------------------------------------------------ L10
def test_l10_refunding_the_first_upgrade_after_a_second_takes_something_back(
        make_user, api, shamcash, django_capture_on_commit_callbacks):
    from subscription.models import Subscription

    cap = django_capture_on_commit_callbacks
    basic = _plan("l10 basic", "10000.00", days=30)
    premium = _plan("l10 premium", "20000.00", days=30)
    gold = _plan("l10 gold", "40000.00", days=30)
    c, sub = _active_on(api, make_user, "l10_user", basic, cap, days_left=20)
    u1 = _upgrade_paid(c, sub, premium, cap)
    assert u1.amount == D("13333.33")
    u2 = _upgrade_paid(c, sub, gold, cap)
    assert u2.amount == D("20666.67"), u2.amount
    before = Subscription.objects.get(pk=sub.pk).end_date

    _refund(u1.id, cap)
    after = Subscription.objects.get(pk=sub.pk).end_date
    assert after < before - timedelta(days=1), (before, after)


# ------------------------------------------------------------------ L12
def test_l12_days_the_credit_bought_are_credited_after_the_upgrade_is_refunded(
        make_user, api, shamcash, django_capture_on_commit_callbacks):
    from subscription.models import Subscription

    cap = django_capture_on_commit_callbacks
    basic = _plan("l12 basic", "10000.00", days=30)
    premium = _plan("l12 premium", "20000.00", days=30)
    gold = _plan("l12 gold", "40000.00", days=30)
    c, sub = _active_on(api, make_user, "l12_user", basic, cap, days_left=20)
    u = _upgrade_paid(c, sub, premium, cap)
    _refund(u.id, cap)
    sub = Subscription.objects.get(pk=sub.pk)
    assert sub.is_active and sub.plan_id == premium.id
    left = sub.end_date - timezone.now()
    assert timedelta(days=9) < left < timedelta(days=11), left

    q = _quote(c, sub, gold)
    assert q.status_code == 200, q.content
    assert D(q.json()["credit"]) > 0, q.json()


# ------------------------------------------------------------------ L13
def test_l13_refunding_a_running_payment_the_credit_skipped_takes_something_back(
        make_user, api, shamcash, django_capture_on_commit_callbacks):
    from subscription.models import Payment, Subscription

    cap = django_capture_on_commit_callbacks
    basic = _plan("l13 basic", "10000.00", days=30)
    premium = _plan("l13 premium", "20000.00", days=30)
    c, sub = _active_on(api, make_user, "l13_user", basic, cap)
    p1 = Payment.objects.get(subscription=sub, status="completed")
    p2 = _renew(c, sub, cap)
    p3 = _renew(c, sub, cap)
    _refund(p1.id, cap)
    # Forty days pass.
    shift = timedelta(days=40)
    s = Subscription.objects.get(pk=sub.pk)
    Subscription.objects.filter(pk=sub.pk).update(start_date=s.start_date - shift,
                                                  end_date=s.end_date - shift)
    for p in Payment.objects.filter(subscription=sub):
        Payment.objects.filter(pk=p.pk).update(
            covers_from=p.covers_from - shift if p.covers_from else None,
            covers_until=p.covers_until - shift if p.covers_until else None)
    sub = Subscription.objects.get(pk=sub.pk)
    assert sub.is_active

    u = _upgrade_paid(c, sub, premium, cap)
    parts = [x["payment_id"] for x in u.metadata["upgrade"]["switch"]["credited"]]
    p3.refresh_from_db()
    before = Subscription.objects.get(pk=sub.pk).end_date
    _refund(p3.id, cap)
    after = Subscription.objects.get(pk=sub.pk).end_date
    assert after < before - timedelta(days=1), (parts, str(p3.id), before, after)


# ------------------------------------------------------------------ L14
def test_l14_the_upgrade_receipt_reconciles_when_the_credit_is_re_read(
        make_user, api, shamcash, django_capture_on_commit_callbacks):
    from subscription.models import Payment, Subscription
    from subscription.receipts import receipt_data

    cap = django_capture_on_commit_callbacks
    basic = _plan("l14 basic", "10000.00", days=30)
    premium = _plan("l14 premium", "20000.00", days=30)
    c, sub = _active_on(api, make_user, "l14_user", basic, cap, days_left=20)
    body = _buy(c, sub, premium, amount="13333.33", currency="SYP").json()
    u = Payment.objects.get(pk=body["payment_id"])
    Subscription.objects.filter(pk=sub.pk).update(
        end_date=timezone.now() + timedelta(days=19, hours=1))
    _complete(u, cap)
    u = Payment.objects.get(pk=u.pk)
    data = receipt_data(u)
    up = data["upgrade"]
    assert D(up["new_price"]) - D(up["credit"]) == D(data["amount"]), (up, data["amount"])


# ------------------------------------------------------------------ L15
@pytest.mark.parametrize("now_g,offered_g", [(1.3, 3.3), (0.3, 2.3), (6.2, 8.2)])
def test_l15_an_exact_two_gram_fibre_gain_is_more_fiber(now_g, offered_g):
    from diet.views import _swap_tag

    current = {"calories": 100.0, "protein": 5.0, "fiber": now_g}
    offered = {"calories": 100.0, "protein": 5.0, "fiber": offered_g}
    assert _swap_tag(current, offered) == ("more_fiber", 2.0)


def test_l9_control_a_quiet_account_shows_the_sweep_the_same_transfer(
        make_user, api, shamcash, monkeypatch, django_capture_on_commit_callbacks):
    """Control for L9: the same transfer on a quiet account is held (so L9 fails on
    the truncated read, not on the setup)."""
    from subscription.models import TransferReview

    plan = _plan("l9c", "12345.00")
    c, sub, payment = _open_checkout(api, make_user, "l9c_user", plan,
                                     age=timedelta(minutes=5))
    now = timezone.now()
    wanted = {"id": "tx-l9c", "amount": "12345.00", "currency": "SYP", "note": "sub",
              "sender": "shamcash:0933000000",
              "occurred_at": (now - timedelta(minutes=2)).isoformat()}
    _windowed_account(monkeypatch, [wanted])
    with django_capture_on_commit_callbacks(execute=True):
        _sweep()
    assert TransferReview.objects.filter(transaction_id="tx-l9c").exists()
