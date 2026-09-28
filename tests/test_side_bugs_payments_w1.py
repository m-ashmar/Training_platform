"""SIDE_BUGS rows of the payments area, wave 1 of the owner's fix run (2026-09-28).

* S-234 (b): one checkout the pollers or the sweep cannot process — any exception, not
  only the refusals they expected — no longer aborts the whole run. Rows are read
  oldest-first (the open poller) or newest-first (the late poller), so one such row
  stopped every checkout of every customer behind it from being confirmed or expired
  for the whole 72 h lookback. The key collision that made a real row raise is part (a)
  of the row, closed in the routine products area; here the row's undeliverable program
  is simulated at the one call that raised (`routine.products.grant_checkout`), because
  what is proved is that NO row, for any reason, stops the others.
* S-238: every row of the poller's held pass is touched once it has been asked about,
  whatever the answer, so a row re-held on every ask cannot pin the front of the pass.

Every test here fails on 554c604.
"""
from datetime import timedelta

import pytest
from django.db import IntegrityError
from django.utils import timezone

from tests.test_fx_subs import _person, _plan, shamcash  # noqa: F401
from tests.test_hub_checkout import _aged, _product_session, owners_rate  # noqa: F401
from tests.test_payment_review import _account, _open_checkout, _poll, _sweep, _txn
from tests.test_routine_products import _buy, _fund

pytestmark = pytest.mark.django_db


def _undeliverable(monkeypatch, payment_id):
    """The program of `payment_id`'s checkout cannot be written: its delivery raises the
    IntegrityError the purchase-key collision raised (SIDE_BUGS S-234 (a))."""
    import routine.products as products

    real = products.grant_checkout

    def grant(payment):
        if str(payment.pk) == str(payment_id):
            raise IntegrityError('duplicate key value violates unique constraint '
                                 '"routine_purchase_key_per_buyer"')
        return real(payment)

    monkeypatch.setattr(products, "grant_checkout", grant)


def _status(payment_id):
    from subscription.models import Payment

    return Payment.objects.get(pk=payment_id).status


# ============================================================ S-234 (b)
def test_s234_one_row_that_cannot_be_delivered_never_stops_the_open_poller(
        make_user, api, shamcash, monkeypatch, django_capture_on_commit_callbacks):
    _trainer, _buyer, _c, _product_id, body = _product_session(make_user, api, "s234o")
    _aged(body["id"], timedelta(minutes=5))          # the oldest: read first
    _c2, _sub, other = _open_checkout(api, make_user, "s234o_other",
                                      _plan("s234o", "13000.00"))
    _undeliverable(monkeypatch, body["id"])
    _account(monkeypatch, [
        _txn("tx-s234o-x", "2500.00", note=body["reference"]),
        _txn("tx-s234o-y", "13000.00", note=other.gateway_transaction_reference)])

    with django_capture_on_commit_callbacks(execute=True):
        result = _poll()

    assert _status(other.pk) == "completed", "the row behind the broken one was confirmed"
    assert _status(body["id"]) == "pending", "the broken row is left for the next run"
    assert (result["checked"], result["completed"]) == (2, 1)


def test_s234_one_row_that_cannot_be_delivered_never_stops_the_late_poller(
        make_user, api, shamcash, monkeypatch, django_capture_on_commit_callbacks):
    from subscription.services.payment_service import PaymentService
    from subscription.tasks import poll_expired_checkouts

    _c, _sub, other = _open_checkout(api, make_user, "s234l_other",
                                     _plan("s234l", "13000.00"), age=timedelta(hours=3))
    _trainer, _buyer, _c2, _product_id, body = _product_session(make_user, api, "s234l")
    _aged(body["id"], timedelta(hours=2))            # the newest: read first
    PaymentService.expire_payment(other.pk, "window closed")
    PaymentService.expire_payment(body["id"], "window closed")
    _undeliverable(monkeypatch, body["id"])
    _account(monkeypatch, [
        _txn("tx-s234l-x", "2500.00", note=body["reference"]),
        _txn("tx-s234l-y", "13000.00", note=other.gateway_transaction_reference)])

    with django_capture_on_commit_callbacks(execute=True):
        result = poll_expired_checkouts()

    assert _status(other.pk) == "completed"
    assert _status(body["id"]) == "expired"
    assert (result["checked"], result["completed"]) == (2, 1)


def test_s234_one_transfer_that_cannot_be_delivered_never_stops_the_sweep(
        make_user, api, shamcash, monkeypatch, django_capture_on_commit_callbacks):
    from subscription.models import TransferReview
    from subscription.services.payment_service import PaymentService

    _trainer, _buyer, _c, _product_id, body = _product_session(make_user, api, "s234s")
    _aged(body["id"], timedelta(hours=2))
    PaymentService.expire_payment(body["id"], "window closed")
    _c2, _sub, other = _open_checkout(api, make_user, "s234s_other",
                                      _plan("s234s", "13131.00"))
    _undeliverable(monkeypatch, body["id"])
    # The closed checkout's own exact transfer first (the sweep settles it), then a
    # transfer with no reference whose amount fits exactly one open checkout.
    _account(monkeypatch, [_txn("tx-s234s-x", "2500.00", note=body["reference"]),
                           _txn("tx-s234s-y", "13131.00", note="")])

    with django_capture_on_commit_callbacks(execute=True):
        result = _sweep()

    assert _status(other.pk) == "needs_review", "the transfer after the broken one held"
    assert TransferReview.objects.get(transaction_id="tx-s234s-y").payment_id == other.pk
    assert _status(body["id"]) == "expired"
    assert result["held"] == 1


# ============================================================ S-238
def _held(api, make_user, name, price):
    """An open plan checkout, a minute old (a near-miss transfer the test lists then
    holds it for review)."""
    _c, _sub, payment = _open_checkout(api, make_user, name, _plan(name, price))
    return payment


def test_s238_a_held_row_whose_near_miss_is_still_on_the_account_does_not_pin_the_pass(
        make_user, api, shamcash, monkeypatch, settings,
        django_capture_on_commit_callbacks):
    """Held batch of one. A is held for amount_differs and its near-miss transfer stays
    on the account; B is held too, then its exact transfer arrives. A was never touched
    by the needs_review branch, so it stayed first in the pass and B never completed."""
    from subscription.models import Payment

    settings.PAYMENT_POLL_HELD_BATCH = 1
    a = _held(api, make_user, "s238a_a", "10000.00")
    b = _held(api, make_user, "s238a_b", "20000.00")
    near = [_txn("tx-s238a-a", "9000.00", note=a.gateway_transaction_reference),
            _txn("tx-s238a-b", "19000.00", note=b.gateway_transaction_reference)]
    _account(monkeypatch, near)
    with django_capture_on_commit_callbacks(execute=True):
        _poll()
    assert (_status(a.pk), _status(b.pk)) == ("needs_review", "needs_review")

    long_ago = timezone.now() - timedelta(hours=1)
    Payment.objects.filter(pk=a.pk).update(updated_at=long_ago)
    Payment.objects.filter(pk=b.pk).update(updated_at=long_ago + timedelta(minutes=1))
    _account(monkeypatch, [*near, _txn("tx-s238a-b-exact", "20000.00",
                                       note=b.gateway_transaction_reference)])

    with django_capture_on_commit_callbacks(execute=True):
        _poll()                                        # asks about A, and touches it
    assert Payment.objects.get(pk=a.pk).updated_at > long_ago + timedelta(minutes=1)
    with django_capture_on_commit_callbacks(execute=True):
        _poll()                                        # asks about B
    assert _status(b.pk) == "completed"
    assert _status(a.pk) == "needs_review"


def test_s238_a_held_row_its_completion_re_holds_does_not_pin_the_pass(
        make_user, api, shamcash, monkeypatch, settings,
        django_capture_on_commit_callbacks):
    """A product checkout expired, its buyer bought the program from the wallet, and
    the late exact transfer held it (`already_owned`). Every ask re-runs the refusal and
    re-holds it without saving the row; it stayed first in the held pass."""
    from subscription.models import Payment
    from subscription.tasks import poll_expired_checkouts

    settings.PAYMENT_POLL_HELD_BATCH = 1
    _trainer, buyer, _c, product_id, body = _product_session(make_user, api, "s238o")
    _aged(body["id"], timedelta(hours=2))
    _account(monkeypatch, [])
    with django_capture_on_commit_callbacks(execute=True):
        _poll()
    assert _status(body["id"]) == "expired"
    _fund(buyer, "10000.00")
    assert _buy(api, buyer, product_id).status_code == 200
    owned = [_txn("tx-s238o", "2500.00", note=body["reference"])]
    _account(monkeypatch, owned)
    with django_capture_on_commit_callbacks(execute=True):
        poll_expired_checkouts()
    assert _status(body["id"]) == "needs_review"

    b = _held(api, make_user, "s238o_b", "20000.00")
    near_b = _txn("tx-s238o-b", "19000.00", note=b.gateway_transaction_reference)
    _account(monkeypatch, [*owned, near_b])
    with django_capture_on_commit_callbacks(execute=True):
        _poll()                         # B held in the open pass (A may be asked too)
    assert _status(b.pk) == "needs_review"

    long_ago = timezone.now() - timedelta(hours=1)
    Payment.objects.filter(pk=body["id"]).update(updated_at=long_ago)
    Payment.objects.filter(pk=b.pk).update(updated_at=long_ago + timedelta(minutes=1))
    _account(monkeypatch, [*owned, near_b, _txn("tx-s238o-b-exact", "20000.00",
                                                note=b.gateway_transaction_reference)])
    for _run in range(2):
        with django_capture_on_commit_callbacks(execute=True):
            _poll()
    assert _status(b.pk) == "completed"
    assert _status(body["id"]) == "needs_review", "an operator still decides the owned one"
