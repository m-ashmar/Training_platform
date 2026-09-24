"""The payouts review of 2026-09-24: three findings a reviewer and a verifier both
confirmed (the money and the wallet-read dimensions).

| Test | Finding |
|---|---|
| `test_support_cannot_reverse_any_leg_of_a_withdrawal` | [HIGH] support's reversal endpoint put a withdrawal's ledger leg back, and cancel / fail / confirm then paid out of payout clearing a second time |
| `test_a_withdrawal_whose_request_leg_was_reversed_never_pays_out_again` | the same, the backstop in `payouts/services.py` for a reversal written some other way |
| `test_an_operator_cannot_approve_or_send_to_a_trainer_no_longer_verified` | [MEDIUM] operator transitions never re-checked that the trainer is still verified |
| `test_an_operator_cannot_approve_or_send_to_a_suspended_trainer` | the same, for a suspended account |
| `test_a_transfer_already_sent_is_still_confirmed` | the decision for `confirm` |
| `test_the_console_shows_a_suspended_trainer_and_its_bulk_approve_refuses` | the console half of the same finding |
| `test_a_first_month_share_owed_for_a_stale_rate_is_on_the_trainers_read` | [MEDIUM] a trainer's share left OWED in escrow vanished from every figure |
| `test_a_renewal_share_owed_for_a_stale_rate_is_on_the_trainers_read` | the same, for a renewal |
| `test_the_owed_read_costs_the_same_queries_for_many_months` | the same read, bounded |

Every withdrawal here is made through the real endpoints, every coaching month by the
real request / approve / first-program endpoints, and every renewal by `renew`.
"""
from __future__ import annotations

import uuid
from datetime import timedelta
from decimal import Decimal

import pytest
from django.utils import timezone

pytestmark = pytest.mark.django_db

JSON = "application/json"
D = Decimal
WITHDRAWALS = "/api/v1/trainer/withdrawals/"
METHODS = "/api/v1/trainer/payout-methods/"
REVERSAL = "/api/wallet/admin/reversal/"
ADDRESS = "0a3f 91c2 7be4 55d0"
#: 1 USD = 13,000 SYP, round so every figure below can be read.
RATE = D("13000.000000")


# ------------------------------------------------------------------ helpers
def _tag(prefix):
    return f"{prefix}_{uuid.uuid4().hex[:8]}"


def _ready(make_user, api, amount="100000.00"):
    """A verified trainer with `amount` to withdraw and a ShamCash method."""
    from wallet.models import Wallet

    trainer = make_user(_tag("rp_t"), user_type="trainer", trainer_is_verified=True,
                        first_name="Rami")
    wallet, _created = Wallet.objects.get_or_create(owner=trainer,
                                                    defaults={"owner_type": "trainer"})
    wallet.balance = D(amount)
    wallet.save()
    added = api(trainer).post(METHODS, {"kind": "shamcash", "address": ADDRESS,
                                        "address_confirm": ADDRESS,
                                        "holder_name": "رامي حداد"}, content_type=JSON)
    assert added.status_code == 201, added.content
    return trainer


def _request(api, trainer, amount):
    made = api(trainer).post(WITHDRAWALS, {"amount": str(amount),
                                           "idempotency_key": uuid.uuid4().hex},
                             content_type=JSON)
    assert made.status_code == 201, made.content
    return made.json()


def _operator(make_user):
    return make_user(_tag("rp_op"), user_type="admin")


def _balance(user):
    from wallet.models import Wallet

    return Wallet.objects.get(owner=user).balance


def _clearing(currency="SYP"):
    from wallet.utils import get_payout_clearing_wallet

    return get_payout_clearing_wallet(currency).balance


def _code(resp):
    return resp.status_code, resp.json().get("code")


def _withdrawal(pk):
    from payouts.models import Withdrawal

    return Withdrawal.objects.get(pk=pk)


def _reverse(api, operator, reference):
    return api(operator).post(REVERSAL, {"reference_id": reference,
                                         "idempotency_key": uuid.uuid4().hex,
                                         "reason": "support"}, content_type=JSON)


def _reverse_directly(reference):
    """A reversal written straight to the ledger — the way the endpoint wrote one before
    this fix, and the way a support shell still could."""
    from wallet.models import Transaction, move_funds_atomic

    original = Transaction.objects.get(reference_id=reference)
    return move_funds_atomic(original.destination_wallet, original.source_wallet,
                             original.amount, tx_type="reversal",
                             money_type=original.money_type,
                             metadata={"original_reference": reference}, reverses=original)


def _set(user, **fields):
    """What `trainer_verification.services.revoke` (the flag) or a suspension (the
    account) writes on the trainer's row, and nothing else."""
    from users.models import CustomUser

    CustomUser.objects.filter(pk=user.pk).update(**fields)


def _console(user):
    from django.test import Client

    client = Client()
    client.force_login(user)
    return client


# ===========================================================================
# [HIGH] A withdrawal's ledger legs are not support's to reverse
# ===========================================================================

def test_support_cannot_reverse_any_leg_of_a_withdrawal(make_user, api):
    """Two trainers each withdraw 50,000, so clearing holds 100,000. Reversing A's
    request leg answered 200 and moved clearing -> A; A then cancelled and was paid the
    same 50,000 again, and B's withdrawal could no longer be confirmed. Every leg —
    the request, the return, the payout — is refused, naming the withdrawal, and
    nothing moves."""
    from payouts import services
    from wallet.models import Transaction

    a, b = _ready(make_user, api), _ready(make_user, api)
    operator = _operator(make_user)
    base = _clearing()
    wa, wb = _request(api, a, "50000"), _request(api, b, "50000")
    assert _clearing() == base + D("100000.00")

    refused = _reverse(api, operator, _withdrawal(wa["id"]).hold_reference)
    assert _code(refused) == (409, "withdrawal_leg"), refused.content
    assert refused.json()["withdrawal"] == {"id": wa["id"], "number": wa["number"],
                                            "state": "requested"}
    assert refused.json()["error"]
    assert not Transaction.objects.filter(tx_type="reversal").exists()
    assert _balance(a) == D("50000.00") and _clearing() == base + D("100000.00")

    # A cancels: the return leg. Reversing it stranded A's money in clearing.
    assert api(a).post(f"{WITHDRAWALS}{wa['id']}/cancel/").status_code == 200
    back = _reverse(api, operator, _withdrawal(wa["id"]).return_reference)
    assert _code(back) == (409, "withdrawal_leg")
    assert back.json()["withdrawal"]["state"] == "cancelled"

    # B is paid: reversing the leg that took it off the platform minted into clearing.
    services.approve(wb["id"], operator)
    services.mark_sent(wb["id"], operator, "SC-RV-PAID")
    services.confirm(wb["id"], operator)
    out = _reverse(api, operator, _withdrawal(wb["id"]).payout_reference)
    assert _code(out) == (409, "withdrawal_leg")
    assert out.json()["withdrawal"] == {"id": wb["id"], "number": wb["number"],
                                        "state": "confirmed"}

    assert not Transaction.objects.filter(tx_type="reversal").exists()
    assert (_balance(a), _balance(b), _clearing()) == (D("100000.00"), D("50000.00"), base)


def test_a_withdrawal_whose_request_leg_was_reversed_never_pays_out_again(make_user, api):
    """The backstop, mirroring `_move_hold`'s `reverses=held` check: a request leg that
    was reversed some other way is found, under the wallet locks, before anything moves.
    Nothing may send, return or pay out money that has already gone back."""
    from payouts import services

    a, b = _ready(make_user, api), _ready(make_user, api)
    operator = _operator(make_user)
    base = _clearing()
    wa, wb = _request(api, a, "50000"), _request(api, b, "50000")
    _reverse_directly(_withdrawal(wa["id"]).hold_reference)
    assert _balance(a) == D("100000.00") and _clearing() == base + D("50000.00")

    # The trainer's cancel paid the 50,000 a second time (150,000 from 100,000).
    refused = api(a).post(f"{WITHDRAWALS}{wa['id']}/cancel/")
    assert _code(refused) == (409, "withdrawal_hold_reversed"), refused.content
    assert refused.json()["error"]
    # So did the operator's fail.
    with pytest.raises(services.TransitionRefused) as failed:
        services.fail(wa["id"], operator, "support returned it")
    assert failed.value.code == "ledger_refused"
    # And the money must not be sent off the platform either.
    with pytest.raises(services.TransitionRefused) as approved:
        services.approve(wa["id"], operator)
    assert approved.value.code == "ledger_refused"
    assert _withdrawal(wa["id"]).state == "requested"
    assert _balance(a) == D("100000.00") and _clearing() == base + D("50000.00")

    # B's withdrawal is whole: clearing still holds exactly its money.
    services.approve(wb["id"], operator)
    services.mark_sent(wb["id"], operator, "SC-RV-B")
    services.confirm(wb["id"], operator)
    assert _withdrawal(wb["id"]).state == "confirmed" and _clearing() == base


def test_a_confirm_after_the_request_leg_was_reversed_is_refused(make_user, api):
    """Reversed after it was sent: confirming it took B's money out of clearing."""
    from payouts import services

    a, b = _ready(make_user, api), _ready(make_user, api)
    operator = _operator(make_user)
    base = _clearing()
    wa, wb = _request(api, a, "50000"), _request(api, b, "50000")
    services.approve(wa["id"], operator)
    services.mark_sent(wa["id"], operator, "SC-RV-A")
    _reverse_directly(_withdrawal(wa["id"]).hold_reference)

    with pytest.raises(services.TransitionRefused) as confirmed:
        services.confirm(wa["id"], operator)
    assert confirmed.value.code == "ledger_refused"
    assert _withdrawal(wa["id"]).state == "sent"
    assert _clearing() == base + D("50000.00")
    services.approve(wb["id"], operator)
    services.mark_sent(wb["id"], operator, "SC-RV-B2")
    services.confirm(wb["id"], operator)
    assert _clearing() == base


# ===========================================================================
# [MEDIUM] Only a verified, active trainer is approved or sent money
# ===========================================================================

def test_an_operator_cannot_approve_or_send_to_a_trainer_no_longer_verified(make_user, api):
    """Spec section 8, "Only verified trainers can withdraw": a verification revoked
    after the request (a forged ID found later) used to leave the withdrawal to be
    approved, sent and confirmed. Fail stays the operator's move."""
    from payouts import services

    trainer = _ready(make_user, api)
    operator = _operator(make_user)
    made = _request(api, trainer, "30000")
    _set(trainer, trainer_is_verified=False)

    with pytest.raises(services.TransitionRefused) as refused:
        services.approve(made["id"], operator)
    assert refused.value.code == "trainer_not_verified"
    assert _withdrawal(made["id"]).state == "requested"

    _set(trainer, trainer_is_verified=True)
    services.approve(made["id"], operator)
    _set(trainer, trainer_is_verified=False)
    with pytest.raises(services.TransitionRefused) as unsent:
        services.mark_sent(made["id"], operator, "SC-VER-1")
    assert unsent.value.code == "trainer_not_verified"
    withdrawal = _withdrawal(made["id"])
    assert (withdrawal.state, withdrawal.shamcash_transaction_id) == ("approved", None)

    services.fail(made["id"], operator, "Verification withdrawn")
    assert _withdrawal(made["id"]).state == "failed"
    assert _balance(trainer) == D("100000.00")


def test_an_operator_cannot_approve_or_send_to_a_suspended_trainer(make_user, api):
    from payouts import services

    trainer = _ready(make_user, api)
    operator = _operator(make_user)
    made = _request(api, trainer, "30000")
    _set(trainer, is_active=False)

    with pytest.raises(services.TransitionRefused) as refused:
        services.approve(made["id"], operator)
    assert refused.value.code == "trainer_inactive"

    _set(trainer, is_active=True)
    services.approve(made["id"], operator)
    _set(trainer, is_active=False)
    with pytest.raises(services.TransitionRefused) as unsent:
        services.mark_sent(made["id"], operator, "SC-SUS-1")
    assert unsent.value.code == "trainer_inactive"
    assert _withdrawal(made["id"]).state == "approved"

    services.fail(made["id"], operator, "Account suspended")
    assert _balance(trainer) == D("100000.00")


def test_a_transfer_already_sent_is_still_confirmed(make_user, api):
    """`confirm` records that money which already left the platform arrived. Refusing
    it cannot bring the money back; it would only leave clearing holding money that is
    gone, or push the operator to 'fail' a transfer that arrived and pay it twice."""
    from payouts import services

    trainer = _ready(make_user, api)
    operator = _operator(make_user)
    base = _clearing()
    made = _request(api, trainer, "30000")
    services.approve(made["id"], operator)
    services.mark_sent(made["id"], operator, "SC-GONE-1")
    _set(trainer, trainer_is_verified=False, is_active=False)

    services.confirm(made["id"], operator)
    assert _withdrawal(made["id"]).state == "confirmed"
    assert _clearing() == base and _balance(trainer) == D("70000.00")


def test_the_console_shows_a_suspended_trainer_and_its_bulk_approve_refuses(make_user, api):
    """The queue showed only a Verified column, so a suspended account looked payable,
    and the bulk 'Approve the selected withdrawals' approved it."""
    trainer = _ready(make_user, api)
    made = _request(api, trainer, "20000")
    _set(trainer, is_active=False)

    console = _console(_operator(make_user))
    queue = "/dj-admin/payouts/withdrawalqueueentry/"
    page = console.get(queue)
    assert page.status_code == 200
    assert "Account active" in page.content.decode()
    change = console.get(f"{queue}{made['id']}/change/")
    assert "Account active" in change.content.decode()

    done = console.post(queue, {"action": "approve_selected",
                                "_selected_action": [made["id"]]}, follow=True)
    assert done.status_code == 200
    assert "suspended" in done.content.decode()
    assert _withdrawal(made["id"]).state == "requested"


# ===========================================================================
# [MEDIUM] A trainer's share owed in escrow is on the trainer's wallet read
# ===========================================================================

@pytest.fixture
def rate(db):
    from wallet.models import ExchangeRate

    return ExchangeRate.objects.create(base="USD", quote="SYP", rate=RATE,
                                       effective_from=timezone.now() - timedelta(hours=1),
                                       note="review-payouts")


def _make_stale(rate_row):
    """Age the live rate past the limit. `update()`: the model refuses an edit."""
    from wallet.models import ExchangeRate

    ExchangeRate.objects.filter(pk=rate_row.pk).update(
        effective_from=timezone.now() - timedelta(days=30))


def _fresh_rate():
    from wallet.models import ExchangeRate

    ExchangeRate.objects.create(base="USD", quote="SYP", rate=RATE,
                                effective_from=timezone.now() - timedelta(minutes=5),
                                note="review-payouts fresh")


def _person(make_user, name, currency, *, user_type="client", **kw):
    from wallet.models import set_currency_for

    user = make_user(name, user_type=user_type, **kw)
    set_currency_for(user, currency, user_type)
    return user


def _dollar_trainer(make_user):
    return _person(make_user, _tag("rp_usd"), "USD", user_type="trainer",
                   trainer_hourly_rate=D("10.00"), first_name="Rami", last_name="Haddad")


def _pound_client_of(make_user, api, trainer, *, first_name="Sam"):
    """A pound client who asked `trainer` and was accepted: month 1 is held."""
    from users.models import TrainerClientRelation
    from wallet.models import Wallet

    client = _person(make_user, _tag("rp_syp"), "SYP", first_name=first_name,
                     last_name="Client")
    Wallet.objects.filter(owner=client).update(balance=D("1000000.00"))
    assert api(client).post("/api/auth/client/request-trainer/", {"trainer_id": trainer.pk},
                            content_type=JSON).status_code == 200
    relation = TrainerClientRelation.objects.get(trainer=trainer, client=client)
    answered = api(trainer).post("/api/auth/trainer/respond-to-request/",
                                 {"request_id": relation.pk, "action": "approve"},
                                 content_type=JSON)
    assert answered.status_code == 200, answered.content
    relation.refresh_from_db()
    assert relation.hold_reference
    return client, relation


def _first_program(api, trainer, client):
    from routine.models import Routine

    routine = Routine.objects.create(name="Month one", created_by=trainer, days=3)
    answer = api(trainer).post(f"/api/routine/routines/{routine.pk}/assign_to_client/",
                               {"client_id": client.pk}, content_type=JSON)
    assert answer.status_code == 200, answer.content


def _read(api, user):
    response = api(user).get("/api/wallet/balance/")
    assert response.status_code == 200, response.content
    return response.json()


def test_a_first_month_share_owed_for_a_stale_rate_is_on_the_trainers_read(
        make_user, api, settings, rate):
    """A dollar trainer quoting 10.00, a pound client. Month 1 is held (8.00 for the
    trainer), the rate goes stale, the first program arrives: `pay_out` leaves the
    share owed in escrow and the month runs. The next read said 0.00 everywhere —
    nothing held, nothing settling, no ledger row — for as long as the rate stayed
    stale. The owed share is its own figure; `held_for_trainer` keeps its meaning."""
    from users.coaching import settle_owed_payouts

    settings.COACHING_COMMISSION_RATE = D("0.20")
    trainer = _dollar_trainer(make_user)
    client, relation = _pound_client_of(make_user, api, trainer, first_name="Rana")
    before = _read(api, trainer)
    assert (before["held_for_trainer"], before["awaiting_first_program"]) == ("8.00", 1)
    assert (before["owed_to_trainer"], before["owed_to_trainer_items"]) == ("0.00", [])

    _make_stale(rate)
    _first_program(api, trainer, client)
    month = relation.periods.get()
    assert month.status == "settled" and month.payout_owed_since is not None

    body = _read(api, trainer)
    assert (body["held_for_trainer"], body["held_for_trainer_gross"],
            body["awaiting_first_program"], body["held_for_trainer_items"]) == \
        ("0.00", "0.00", 0, [])
    assert (body["balance"], body["settling"], body["available"]) == \
        ("0.00", "0.00", "0.00")
    assert body["owed_to_trainer"] == "8.00"
    (item,) = body["owed_to_trainer_items"]
    assert (item["period_id"], item["relation_id"], item["month"]) == \
        (month.pk, relation.pk, 1)
    assert item["client"] == {"id": client.pk, "name": "Rana Client", "role": "client"}
    assert (item["status"], item["kind"]) == ("payout_owed", "trainer_fee_payout")
    assert item["reason"]
    assert (item["amount"], item["commission"], item["net"], item["currency"]) == \
        ("10.00", "2.00", "8.00", "USD")
    assert item["commission_rate"] == "0.2"
    assert (item["charged_amount"], item["charged_currency"]) == ("130000.00", "SYP")
    assert item["estimated"] is True
    assert item["owed_since"] and item["starts_at"]
    # Not a ledger row: nothing is written for the trainer until it is paid.
    assert api(trainer).get("/api/wallet/transactions/").json()["count"] == 0
    # Anyone else reads the new keys empty.
    other = _read(api, client)
    assert (other["owed_to_trainer"], other["owed_to_trainer_items"]) == ("0.00", [])

    # The rate is fresh again: the hourly run pays it, and it is no longer owed.
    _fresh_rate()
    assert settle_owed_payouts()["paid"] == 1
    paid = _read(api, trainer)
    assert (paid["owed_to_trainer"], paid["owed_to_trainer_items"]) == ("0.00", [])
    assert paid["balance"] == "8.00"


def test_a_renewal_share_owed_for_a_stale_rate_is_on_the_trainers_read(
        make_user, api, settings, rate):
    """`renew` charges and pays out in one transaction, so a renewal is never HELD —
    but its trainer's share is OWED the same way when the rate is stale."""
    from users.coaching import renew, settle_owed_payouts

    settings.COACHING_COMMISSION_RATE = D("0.20")
    trainer = _dollar_trainer(make_user)
    client, relation = _pound_client_of(make_user, api, trainer)
    _first_program(api, trainer, client)
    first = relation.periods.get(number=1)
    assert first.payout_owed_since is None
    assert _read(api, trainer)["balance"] == "8.00"

    _make_stale(rate)
    assert renew(relation.pk, now=first.ends_at + timedelta(minutes=1)) == "renewed"
    second = relation.periods.get(number=2)
    assert second.payout_owed_since is not None

    body = _read(api, trainer)
    assert (body["held_for_trainer"], body["awaiting_first_program"]) == ("0.00", 0)
    assert body["owed_to_trainer"] == "8.00"
    (item,) = body["owed_to_trainer_items"]
    assert (item["period_id"], item["month"], item["status"]) == \
        (second.pk, 2, "payout_owed")
    assert (item["net"], item["currency"], item["estimated"]) == ("8.00", "USD", True)
    assert body["balance"] == "8.00"

    _fresh_rate()
    assert settle_owed_payouts()["paid"] == 1
    paid = _read(api, trainer)
    assert (paid["owed_to_trainer"], paid["owed_to_trainer_items"]) == ("0.00", [])
    assert paid["balance"] == "16.00"


def test_the_owed_read_costs_the_same_queries_for_many_months(make_user, api, settings, rate):
    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    settings.COACHING_COMMISSION_RATE = D("0.20")
    trainer = _dollar_trainer(make_user)
    clients = [_pound_client_of(make_user, api, trainer, first_name=f"C{n}")[0]
               for n in range(3)]
    _make_stale(rate)
    _first_program(api, trainer, clients[0])
    with CaptureQueriesContext(connection) as one:
        assert len(_read(api, trainer)["owed_to_trainer_items"]) == 1
    for client in clients[1:]:
        _first_program(api, trainer, client)
    with CaptureQueriesContext(connection) as three:
        body = _read(api, trainer)
    assert len(body["owed_to_trainer_items"]) == 3 and body["owed_to_trainer"] == "24.00"
    assert len(three) <= len(one), (len(one), len(three))
    owed_since = [i["owed_since"] for i in body["owed_to_trainer_items"]]
    assert owed_since == sorted(owed_since, reverse=True)
