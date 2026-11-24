"""Cycle 9: payments, on real threads.

`django_db(transaction=True)`, because two requests only see each other's work once it is
COMMITTED — which is exactly where every one of these defects lived. See
`tests/test_zz_concurrency.py` for why these files sort last.
"""
import threading
import time
from datetime import timedelta
from decimal import Decimal

import pytest
from django.utils import timezone

from tests.test_c9_payments import (  # noqa: F401  (`shamcash` is a fixture)
    _client_and_trainer, _legs, _plan, _post, _subscribe, shamcash,
)


def _on_threads(calls, barrier=True):
    """Run each callable on its own thread, released together; return what each gave."""
    from django.db import connections

    results = [None] * len(calls)
    gate = threading.Barrier(len(calls)) if barrier else None

    def run(i, fn):
        try:
            if gate:
                gate.wait(timeout=30)
            results[i] = fn()
        except Exception as exc:          # a lost race must be visible, not a hang
            results[i] = exc
        finally:
            connections.close_all()

    threads = [threading.Thread(target=run, args=(i, fn)) for i, fn in enumerate(calls)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=120)
    return results


def _active_subscription(make_user, tag, days_left=10):
    from subscription.models import Subscription

    plan = _plan(tag)
    user = make_user(f"c9zz_{tag}", phone_number="0933100000")
    sub = Subscription.objects.create(user=user, plan=plan, status="active")
    Subscription.objects.filter(pk=sub.pk).update(
        end_date=timezone.now() + timedelta(days=days_left))
    sub.refresh_from_db()
    return user, sub


def _open_payments(sub):
    from subscription.models import Payment

    return list(Payment.objects.filter(
        subscription=sub, status__in=("pending", "processing", "authorized"))
        .values_list("gateway_transaction_reference", flat=True))


def _answer(resp):
    body = resp.json()
    return resp.status_code, body.get("payment_id"), body.get("reference"), body.get("payment_url")


# ------------------------------------------------------------ one checkout, one reference
@pytest.mark.parametrize("taps", [2, 6])
@pytest.mark.django_db(transaction=True)
def test_a_double_tapped_renew_hands_out_the_reference_it_stores(taps, make_user, api, shamcash):
    """`start_renewal` locked the subscription only while it created the pending payment,
    and committed before the view generated a reference and saved it. Every tap waiting
    on that lock was released straight into the gap, found the payment with no reference,
    generated its own and saved over the others. Measured: 30 of 30 double taps handed the
    customer a reference that matched no payment; with six taps, up to three distinct
    references for one payment. A transfer quoting a lost reference was answered 400 by
    the webhook on every redelivery and the renewal was never granted."""
    user, sub = _active_subscription(make_user, f"renew{taps}")
    clients = [api(user) for _ in range(taps)]

    answers = _on_threads([
        (lambda c=c: _answer(_post(c, f"/api/subscription/v1/subscriptions/{sub.id}/renew/",
                                   {"gateway": "shamcash"})))
        for c in clients])

    assert all(not isinstance(a, Exception) and a[0] == 200 for a in answers), answers
    handed_out = {a[2] for a in answers}
    stored = _open_payments(sub)
    assert len(stored) == 1, stored
    assert handed_out == set(stored), (
        f"references handed to the customer {sorted(handed_out)}, stored {stored}")
    assert len({a[1] for a in answers}) == 1


@pytest.mark.django_db(transaction=True)
def test_a_double_tap_inside_a_hosted_gateway_round_trip_opens_one_session(
        make_user, api, shamcash, monkeypatch):
    """In hosted mode the gap between reserving the renewal and saving its reference was
    the whole gateway round trip, so ANY double tap inside it lost a reference: 15 of 15
    at a 150 ms stagger against a 300 ms gateway, and every tap opened its own live
    session at ShamCash."""
    from subscription.gateways.shamcash import ShamCashGateway

    shamcash["initiate_path"] = "/c9/initiate"
    # Hosted mode needs the API's base URL too, which has no default since the third
    # party's host left the gateway (the owner, 2026-11-24); this test is about the
    # round trip, not about which host it goes to.
    shamcash["api_url"] = "https://shamcash.test/v1"
    sessions = []

    def slow_gateway(self, path, body):
        sessions.append(body["reference"])
        time.sleep(0.3)
        return {"data": {"payment_url": f"https://pay.c9.test/{body['reference']}"}}

    monkeypatch.setattr(ShamCashGateway, "_post", slow_gateway)
    user, sub = _active_subscription(make_user, "hosted")
    first, second = api(user), api(user)

    def tap(c, delay):
        time.sleep(delay)
        return _answer(_post(c, f"/api/subscription/v1/subscriptions/{sub.id}/renew/",
                             {"gateway": "shamcash"}))

    answers = _on_threads([lambda: tap(first, 0), lambda: tap(second, 0.15)])

    assert all(not isinstance(a, Exception) and a[0] == 200 for a in answers), answers
    assert len(sessions) == 1, f"{len(sessions)} gateway sessions opened for one renewal"
    assert answers[0][2:] == answers[1][2:], answers
    assert _open_payments(sub) == [sessions[0]]


@pytest.mark.django_db(transaction=True)
def test_renew_and_pay_at_once_after_a_lapse_open_one_checkout(make_user, api, shamcash):
    """Renew deduplicated only renewals and Pay deduplicated nothing, so the two buttons
    the app shows after a lapse each handed out a live reference — and paying both
    charged twice for one period."""
    from subscription.models import Subscription

    user, sub = _active_subscription(make_user, "renewpay")
    Subscription.objects.filter(pk=sub.pk).update(
        status="expired", end_date=timezone.now() - timedelta(days=3))
    renew_client, pay_client = api(user), api(user)

    answers = _on_threads([
        lambda: _answer(_post(renew_client, f"/api/subscription/v1/subscriptions/{sub.id}/renew/",
                              {"gateway": "shamcash"})),
        lambda: _answer(_post(pay_client, "/api/subscription/v1/gateways/",
                              {"gateway": "shamcash", "subscription_id": str(sub.id)})),
    ])

    assert all(not isinstance(a, Exception) and a[0] == 200 for a in answers), answers
    assert answers[0][1:3] == answers[1][1:3], answers
    assert _open_payments(sub) == [answers[0][2]]


@pytest.mark.django_db(transaction=True)
def test_a_double_tapped_pay_button_opens_one_checkout(make_user, api, shamcash):
    """Two concurrent taps on Pay each created a payment with its own reference."""
    from subscription.models import Subscription

    plan = _plan("paytaps")
    user = make_user("c9zz_paytaps", phone_number="0933100001")
    c, sub = _subscribe(api, user, plan)
    assert Subscription.objects.get(pk=sub.pk).status == "pending"
    clients = [api(user) for _ in range(4)]

    answers = _on_threads([
        (lambda c=c: _answer(_post(c, "/api/subscription/v1/gateways/",
                                   {"gateway": "shamcash", "subscription_id": str(sub.id)})))
        for c in clients])

    assert all(not isinstance(a, Exception) and a[0] == 200 for a in answers), answers
    assert len({a[2] for a in answers}) == 1, answers
    assert _open_payments(sub) == [answers[0][2]]


# ------------------------------------------------- the customer's write vs the renewal
@pytest.mark.parametrize("request_kind", ["cancel", "patch_auto_renew"])
@pytest.mark.django_db(transaction=True)
def test_a_tap_while_the_renewal_commits_does_not_erase_the_paid_period(
        request_kind, make_user, api):
    """`cancel` and PATCH read the row with no lock and then saved EVERY column back.
    Forced order — the tap reads while the renewal is extending the row, and its UPDATE
    waits for that COMMIT — lost the paid 30 days 3 of 3 times; on a plain barrier 16 of
    20. The renewal payment stays 'completed', both calls answer 200."""
    from django.db import connections, transaction

    from subscription.models import Payment, Subscription
    from subscription.services.payment_service import PaymentService

    user, sub = _active_subscription(make_user, f"tap_{request_kind}")
    paid_until_before = sub.end_date
    renewal = Payment.objects.create(
        subscription=sub, amount=sub.plan.price, currency="SYP", status="pending",
        payment_method="shamcash", gateway_transaction_reference=f"C9ZZ-{request_kind}",
        metadata={"kind": "renewal"})
    extended = threading.Event()
    c = api(user)

    def renewal_clears():
        with transaction.atomic():
            PaymentService.complete_payment(renewal.id, {
                "amount": "5000.00", "currency": "SYP", "status": "completed",
                "event_id": f"c9zz-{request_kind}"})
            extended.set()
            time.sleep(0.5)        # the tap arrives inside this window
        return "committed"

    def tap():
        assert extended.wait(timeout=30)
        if request_kind == "cancel":
            return _post(c, f"/api/subscription/v1/subscriptions/{sub.id}/cancel/",
                         {"reason": "c9"}).status_code
        import json
        return c.patch(f"/api/subscription/v1/subscriptions/{sub.id}/",
                       data=json.dumps({"auto_renew": False}),
                       content_type="application/json").status_code

    results = _on_threads([renewal_clears, tap], barrier=False)
    connections.close_all()

    # The PATCH is refused since plan step 5 (`auto_renew` is read-only: nothing charges
    # a plan automatically). The race is still the subject — a request arriving inside
    # the renewal's window must not cost the days it just paid for, whether it goes on
    # to write or to refuse.
    expected = 400 if request_kind == "patch_auto_renew" else 200
    assert results == ["committed", expected], results
    sub = Subscription.objects.get(pk=sub.pk)
    added = (sub.end_date - paid_until_before).total_seconds() / 86400
    assert Payment.objects.get(pk=renewal.pk).status == "completed"
    assert 29.99 < added < 30.01, f"the paid renewal added {added:.2f} days"
    assert sub.auto_renew is (request_kind == "patch_auto_renew")


# ------------------------------------------------------------- the payout's lock order
@pytest.mark.django_db(transaction=True)
def test_the_payout_and_the_clients_own_transfer_do_not_deadlock(make_user):
    """The payout locked the client's wallet for the top-up, then asked for both wallets
    sorted by id for the transfer — client, then trainer, whenever the trainer signed up
    first. A client's own transfer to that trainer (`move_funds_atomic` sorts too) took
    the trainer's wallet and waited for the client's: a Postgres deadlock cycle. When the
    payout was the victim it rolled back after the payment had committed — 13 lost
    payouts in 400 natural runs, each answered 500 to the gateway and 200 on retry.

    The interleaving is forced here: the transfer takes the trainer's wallet exactly
    after the top-up leg, which is where the cycle closed."""
    from django.db import transaction

    import wallet.signals as wallet_signals
    from subscription.models import Payment, Subscription
    from wallet.models import Wallet, wallet_for

    client_user, trainer = _client_and_trainer(make_user, "zzlock")
    plan = _plan("zzlock")
    sub = Subscription.objects.create(user=client_user, plan=plan, status="active")
    payment = Payment.objects.create(subscription=sub, amount=plan.price, currency="SYP",
                                     status="pending", payment_method="manual")
    # Completed without its payout — the moment between the COMMIT and the receiver.
    Payment.objects.filter(pk=payment.pk).update(status="completed")
    trainer_wallet = wallet_for(trainer, "trainer", "SYP")
    client_wallet = wallet_for(client_user, "client", "SYP")
    Wallet.objects.filter(pk=client_wallet.pk).update(balance=Decimal("100.00"))
    assert trainer_wallet.pk < client_wallet.pk, "the ordinary case: the trainer came first"

    topped_up, transfer_holds_trainer = threading.Event(), threading.Event()
    real_move = wallet_signals.move_funds_atomic

    def move_then_yield(*args, **kwargs):
        tx = real_move(*args, **kwargs)
        if kwargs.get("tx_type") == "topup":
            topped_up.set()
            transfer_holds_trainer.wait(timeout=1.5)
        return tx

    wallet_signals.move_funds_atomic = move_then_yield
    try:
        def payout():
            wallet_signals.handle_payment_completed(
                sender=None, payment=Payment.objects.get(pk=payment.pk), subscription=sub)
            return "paid"

        def clients_transfer():
            assert topped_up.wait(timeout=30)
            with transaction.atomic():
                Wallet.objects.select_for_update().get(pk=trainer_wallet.pk)
                transfer_holds_trainer.set()
                time.sleep(0.1)
                Wallet.objects.select_for_update().get(pk=client_wallet.pk)
            return "transferred"

        results = _on_threads([payout, clients_transfer], barrier=False)
    finally:
        wallet_signals.move_funds_atomic = real_move

    assert results == ["paid", "transferred"], results
    assert _legs(payment) == ["topup", "transfer"]
    assert Payment.objects.get(pk=payment.pk).payout_settled_at is not None
