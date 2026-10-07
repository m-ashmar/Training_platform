"""The wave-1 review of the payments area, R-0: an overrun ends the run.

`SoftTimeLimitExceeded` — what Celery raises INSIDE a task that overruns
`CELERY_TASK_SOFT_TIME_LIMIT` — is an `Exception`. The per-row catch-alls wave 1 added
to the pollers (SIDE_BUGS S-234), the older catch around the gateway lookup, and
ShamCash's own read all swallowed it, so an overrunning run went on asking the gateway,
row after row, until the hard limit SIGKILLed the child, possibly between a COMMIT
and its on-commit notice. An overrun now ends the run where it lands, through
`no_retry_on_timeout` (an `Ignore`: not retried), and the rows not reached are asked
next run. The sweep, which had no `no_retry_on_timeout`, gets it, so its overrun ends
it once instead of being autoretried three times.

Every test here fails on 39ae624.
"""
from datetime import timedelta

import pytest
from django.utils import timezone

from tests.test_fx_subs import _plan, shamcash  # noqa: F401
from tests.test_payment_review import _account, _open_checkout, _txn

pytestmark = pytest.mark.django_db


def _status(payment_id):
    from subscription.models import Payment

    return Payment.objects.get(pk=payment_id).status


def _overrun_completing(monkeypatch, payment_id):
    """`PaymentService.complete_payment` overruns the slot for `payment_id` only."""
    from celery.exceptions import SoftTimeLimitExceeded

    from subscription.services import payment_service as ps

    real = ps.PaymentService.complete_payment.__func__

    def complete(cls, pk, *args, **kwargs):
        if str(pk) == str(payment_id):
            raise SoftTimeLimitExceeded()
        return real(cls, pk, *args, **kwargs)

    monkeypatch.setattr(ps.PaymentService, "complete_payment", classmethod(complete))


# ============================================================ the open poller
def test_r0_an_overrun_completing_one_row_ends_the_open_poller(
        make_user, api, shamcash, monkeypatch, django_capture_on_commit_callbacks):
    from celery.exceptions import Ignore

    from subscription.tasks import poll_open_payments

    _c1, _s1, first = _open_checkout(api, make_user, "w1r_po_a", _plan("w1rpoa", "13000.00"),
                                     age=timedelta(minutes=3))       # oldest: read first
    _c2, _s2, second = _open_checkout(api, make_user, "w1r_po_b", _plan("w1rpob", "13000.00"))
    _overrun_completing(monkeypatch, first.pk)
    _account(monkeypatch, [
        _txn("tx-w1r-po-a", "13000.00", note=first.gateway_transaction_reference),
        _txn("tx-w1r-po-b", "13000.00", note=second.gateway_transaction_reference)])

    with django_capture_on_commit_callbacks(execute=True):
        with pytest.raises(Ignore):
            poll_open_payments()

    assert _status(second.pk) == "pending", \
        "the run ended at the overrun; the row behind it waits for the next minute"
    assert _status(first.pk) == "pending"


def test_r0_an_overrun_while_shamcash_answers_ends_the_open_poller_and_is_not_its_failure(
        make_user, api, shamcash, monkeypatch, django_capture_on_commit_callbacks):
    """The likeliest place for the limit to land: inside the HTTP read. ShamCash's own
    read caught it as a failed lookup, counted it against the gateway's health, and the
    poller treated the row as 'unknown' and went on."""
    from celery.exceptions import Ignore, SoftTimeLimitExceeded

    from subscription.gateways.shamcash import ShamCashGateway
    from subscription.models import GatewayHealth
    from subscription.tasks import poll_open_payments

    _c1, _s1, first = _open_checkout(api, make_user, "w1r_pg_a", _plan("w1rpga", "13000.00"),
                                     age=timedelta(minutes=3))
    _c2, _s2, second = _open_checkout(api, make_user, "w1r_pg_b", _plan("w1rpgb", "13000.00"))
    account = [_txn("tx-w1r-pg-a", "13000.00", note=first.gateway_transaction_reference),
               _txn("tx-w1r-pg-b", "13000.00", note=second.gateway_transaction_reference)]
    calls = []

    def read(self, path, params):
        calls.append(path)
        if len(calls) == 1:
            raise SoftTimeLimitExceeded()
        return {"data": list(account)}

    monkeypatch.setattr(ShamCashGateway, "_get", read)

    with django_capture_on_commit_callbacks(execute=True):
        with pytest.raises(Ignore):
            poll_open_payments()

    assert len(calls) == 1, "no further read after the overrun"
    assert (_status(first.pk), _status(second.pk)) == ("pending", "pending")
    assert not GatewayHealth.objects.filter(gateway="shamcash",
                                            last_failure_at__isnull=False).exists(), \
        "the task's own time limit is not a failure of ShamCash"


# ============================================================ the late poller
def test_r0_an_overrun_completing_one_row_ends_the_late_poller(
        make_user, api, shamcash, monkeypatch, django_capture_on_commit_callbacks):
    from celery.exceptions import Ignore

    from subscription.services.payment_service import PaymentService
    from subscription.tasks import poll_expired_checkouts

    _c1, _s1, older = _open_checkout(api, make_user, "w1r_pl_a", _plan("w1rpla", "13000.00"),
                                     age=timedelta(hours=3))
    _c2, _s2, newer = _open_checkout(api, make_user, "w1r_pl_b", _plan("w1rplb", "13000.00"),
                                     age=timedelta(hours=2))        # newest: read first
    PaymentService.expire_payment(older.pk, "window closed")
    PaymentService.expire_payment(newer.pk, "window closed")
    _overrun_completing(monkeypatch, newer.pk)
    _account(monkeypatch, [
        _txn("tx-w1r-pl-a", "13000.00", note=older.gateway_transaction_reference),
        _txn("tx-w1r-pl-b", "13000.00", note=newer.gateway_transaction_reference)])

    with django_capture_on_commit_callbacks(execute=True):
        with pytest.raises(Ignore):
            poll_expired_checkouts()

    assert (_status(newer.pk), _status(older.pk)) == ("expired", "expired")


# ============================================================ the sweep
def _overrun_queueing(monkeypatch, transaction_id):
    """Queuing transfer `transaction_id` overruns the slot; the others queue as usual."""
    from celery.exceptions import SoftTimeLimitExceeded

    from subscription.services import payment_service as ps

    real = ps.PaymentService.queue_unclaimed_transfer.__func__

    def queue(cls, facts, *args, **kwargs):
        if facts.get("transaction_id") == transaction_id:
            raise SoftTimeLimitExceeded()
        return real(cls, facts, *args, **kwargs)

    monkeypatch.setattr(ps.PaymentService, "queue_unclaimed_transfer", classmethod(queue))


def test_r0_an_overrun_ends_the_sweep_once_and_it_is_not_retried(shamcash, monkeypatch):
    """The sweep had no `no_retry_on_timeout`: the per-row catch swallowed the overrun
    and the sweep went on; one that escaped it was autoretried up to three more slots.
    Run as the worker runs it (`apply`), it now ends IGNORED — not SUCCESS, not RETRY."""
    from subscription.models import TransferReview
    from subscription.tasks import sweep_unmatched_transfers

    # Two transfers no checkout claims: no reference, and an amount nobody asked for.
    _account(monkeypatch, [_txn("tx-w1r-sw-a", "4321.00"), _txn("tx-w1r-sw-b", "4322.00")])
    _overrun_queueing(monkeypatch, "tx-w1r-sw-a")

    result = sweep_unmatched_transfers.apply()

    assert result.state == "IGNORED", result.state
    assert not TransferReview.objects.filter(transaction_id="tx-w1r-sw-b").exists(), \
        "the sweep ended at the overrun; the next run queues the rest"


def test_r0_an_overrun_reading_the_account_ends_the_sweep(shamcash, monkeypatch):
    """The account read is where the sweep spends its time. ShamCash's read swallowed
    the limit as a failed lookup, and the sweep returned as if the account were down."""
    from celery.exceptions import SoftTimeLimitExceeded

    from subscription.gateways.shamcash import ShamCashGateway
    from subscription.models import GatewayHealth
    from subscription.tasks import sweep_unmatched_transfers

    def read(self, path, params):
        raise SoftTimeLimitExceeded()

    monkeypatch.setattr(ShamCashGateway, "_get", read)

    result = sweep_unmatched_transfers.apply()

    assert result.state == "IGNORED", result.state
    assert not GatewayHealth.objects.filter(gateway="shamcash",
                                            last_failure_at__isnull=False).exists()


# ============================================================ owed payouts
def test_r0_an_overrun_ends_the_owed_payout_sweep(make_user, monkeypatch):
    """Same catch-all, same swallow: the owed-payout sweep went on sending until the hard
    limit. It now ends at the overrun; the next run sends what is still owed."""
    from celery.exceptions import Ignore, SoftTimeLimitExceeded

    from subscription.models import Payment, Subscription
    from subscription.services import payment_service as ps
    from subscription.tasks import settle_owed_payouts

    plan = _plan("w1rowed", "13000.00")
    owed = []
    for n, age in enumerate((40, 30)):
        user = make_user(f"w1r_owed_{n}")
        sub = Subscription.objects.create(user=user, plan=plan, status="active")
        # Written directly (no events): a completed row whose payout never landed is the
        # state this sweep exists for, and only its selection matters here.
        payment = Payment.objects.create(subscription=sub, amount=plan.price, currency="SYP",
                                         status="completed", payment_method="manual")
        Payment.objects.filter(pk=payment.pk).update(
            updated_at=timezone.now() - timedelta(minutes=age))
        owed.append(payment.pk)
    sent = []

    def send(sender, payment, subscription):
        sent.append(payment.pk)
        if payment.pk == owed[0]:
            raise SoftTimeLimitExceeded()
        return []

    monkeypatch.setattr(ps.payment_completed, "send", send)

    with pytest.raises(Ignore):
        settle_owed_payouts()

    assert sent == [owed[0]], "nothing sent after the overrun"
