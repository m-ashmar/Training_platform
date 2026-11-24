"""Trainer withdrawals and payout methods (BACKEND_BUGS B-097 and B-098; plan step 6,
second half; the spec, section 8 "Trainer payouts").

B-097: the server released a coaching payout straight into the trainer's spendable
balance, and neither the wallet read nor the ledger row said which released money was
still inside the hold period, so the app guessed it from `created_at`. B-098: there was
no way to ask for a withdrawal, read one, cancel one, or save where the money should go.

What is asserted here, in order:

  * the settling window: on the wallet read (`available`, `settling`, `settling_until`,
    `in_withdrawal`) and on each payout row (`available_at`, `settling`), from a REAL
    coaching payout (request, approval, first program), including a reversed one and
    the hold switched off;
  * payout methods: the address typed twice with the holder's name, encrypted at rest,
    never a password; cash at a listed agent; one default; duplicates; the cap; delete;
  * a withdrawal: only verified trainers; the money leaves for payout clearing at once;
    WD- numbers; idempotent; every guardrail and its code; the build check;
  * the life of a withdrawal: cancel while requested; approve, sent (a unique ShamCash
    transaction id), confirmed; failed returns the money; every transition an immutable
    event, which the database also refuses to change;
  * the three pushes, as `Notification` rows;
  * the console: operator-only, the queue, a decision from the page.
"""
import re
import uuid
from datetime import timedelta
from decimal import Decimal

import pytest
from django.utils import timezone
from tests.routine_rows import deliverable_routine

pytestmark = pytest.mark.django_db

JSON = "application/json"
WITHDRAWALS = "/api/v1/trainer/withdrawals/"
METHODS = "/api/v1/trainer/payout-methods/"
ADDRESS = "0a3f 91c2 7be4 55d0"


def _tag(prefix):
    return f"{prefix}_{uuid.uuid4().hex[:8]}"


def _trainer(make_user, verified=True, **kw):
    return make_user(_tag("wd_t"), user_type="trainer", trainer_is_verified=verified,
                     first_name="Rami", **kw)


def _fund(user, amount, owner_type):
    from wallet.models import Wallet

    wallet, _created = Wallet.objects.get_or_create(owner=user,
                                                    defaults={"owner_type": owner_type})
    wallet.balance = Decimal(amount)
    wallet.save()
    return wallet


def _balance(user):
    from wallet.models import Wallet

    return Wallet.objects.get(owner=user).balance


def _clearing(currency="SYP"):
    from wallet.utils import get_payout_clearing_wallet

    return get_payout_clearing_wallet(currency).balance


def _shamcash(api, trainer, address=ADDRESS, **extra):
    resp = api(trainer).post(METHODS, {"kind": "shamcash", "address": address,
                                       "address_confirm": address,
                                       "holder_name": "رامي حداد", **extra},
                             content_type=JSON)
    assert resp.status_code == 201, resp.content
    return resp.json()


def _ready(make_user, api, amount="100000.00"):
    """A verified trainer with `amount` to withdraw and a ShamCash method."""
    trainer = _trainer(make_user)
    _fund(trainer, amount, "trainer")
    method = _shamcash(api, trainer)
    return trainer, method


def _request(api, trainer, amount, key=None, **extra):
    return api(trainer).post(WITHDRAWALS, {"amount": str(amount),
                                           "idempotency_key": key or uuid.uuid4().hex,
                                           **extra}, content_type=JSON)


def _code(resp):
    return resp.status_code, resp.json().get("code")


def _operator(make_user):
    return make_user(_tag("wd_op"), user_type="admin")


def _paid_coaching(make_user, api, *, rate="30000.00", funds="50000.00"):
    """A REAL coaching payout: a client asks, the trainer accepts (month 1 held), the
    first program arrives (month 1 paid out). The commission is 0 in the shared setup,
    so the trainer is paid the whole rate."""
    from routine.models import Routine
    from users.models import TrainerClientRelation

    trainer = _trainer(make_user, trainer_hourly_rate=Decimal(rate))
    client = make_user(_tag("wd_c"))
    _fund(client, funds, "client")
    assert api(client).post("/api/auth/client/request-trainer/", {"trainer_id": trainer.pk},
                            content_type=JSON).status_code == 200
    relation = TrainerClientRelation.objects.get(trainer=trainer, client=client)
    answered = api(trainer).post("/api/auth/trainer/respond-to-request/",
                                 {"request_id": relation.pk, "action": "approve"},
                                 content_type=JSON)
    assert answered.status_code == 200, answered.content
    # G5 (2026-11-24): an empty routine no longer pays month 1, so the first
    # programme has an exercise, as a real one does (tests/routine_rows.py).
    routine = deliverable_routine(trainer)
    delivered = api(trainer).post(f"/api/routine/routines/{routine.pk}/assign_to_client/",
                                  {"client_id": client.pk}, content_type=JSON)
    assert delivered.status_code == 200, delivered.content
    return trainer, client


def _payout_row(trainer):
    from wallet.models import Transaction

    return Transaction.objects.get(destination_wallet__owner=trainer,
                                   metadata__purpose="trainer_assignment_settlement")


def _age(tx, days):
    # Shared setup, changed deliberately: the ledger refuses UPDATE at the database
    # since wallet/0021, so the back-dating goes through the one test-only escape.
    from tests.ledger_rows import backdate

    backdate(tx.pk, tx.created_at - timedelta(days=days))


# ===========================================================================
# 1. B-097: the settling window, on the wire
# ===========================================================================

def test_the_design_numbers_are_the_defaults(settings):
    """«يتاح خلال 3 أيام» and «الحد الأدنى 10,000» (15-trainer-money)."""
    assert settings.PAYOUT_HOLD_DAYS == 3
    assert settings.PAYOUT_MIN_AMOUNT["SYP"] == Decimal("10000")
    assert set(settings.PAYOUT_DAILY_CAP) == {"SYP", "USD"}


def test_a_fresh_coaching_payout_is_settling_not_available(make_user, api):
    trainer, _client = _paid_coaching(make_user, api)
    payout = _payout_row(trainer)

    body = api(trainer).get("/api/wallet/balance/").json()
    assert body["balance"] == "30000.00"
    assert body["settling"] == "30000.00"
    assert body["available"] == "0.00"
    assert body["in_withdrawal"] == "0.00"
    assert body["settling_until"] == (payout.created_at + timedelta(days=3)).isoformat()

    _age(payout, 4)
    body = api(trainer).get("/api/wallet/balance/").json()
    assert (body["available"], body["settling"], body["settling_until"]) == \
        ("30000.00", "0.00", None)


def test_each_payout_row_says_when_it_becomes_available(make_user, api):
    trainer, _client = _paid_coaching(make_user, api)
    payout = _payout_row(trainer)

    rows = api(trainer).get("/api/wallet/transactions/").json()["results"]
    row = next(r for r in rows if r["reference_id"] == payout.reference_id)
    assert row["kind"] == "trainer_fee_payout"
    assert row["available_at"] == (payout.created_at + timedelta(days=3)).isoformat()
    assert row["settling"] is True

    detail = api(trainer).get(f"/api/wallet/transactions/{payout.reference_id}/").json()
    assert detail["settling"] is True and detail["available_at"] == row["available_at"]

    _age(payout, 4)
    row = next(r for r in api(trainer).get("/api/wallet/transactions/").json()["results"]
               if r["reference_id"] == payout.reference_id)
    assert row["settling"] is False and row["available_at"] is not None


def test_rows_that_are_not_payouts_carry_no_window(make_user, api):
    trainer, client = _paid_coaching(make_user, api)
    rows = api(client).get("/api/wallet/transactions/").json()["results"]
    assert rows and all(r["available_at"] is None and r["settling"] is False for r in rows)
    body = api(client).get("/api/wallet/balance/").json()
    assert body["available"] == body["balance"] and body["settling"] == "0.00"
    assert body["settling_until"] is None


def test_a_reversed_payout_is_not_settling(make_user, api):
    trainer, _client = _paid_coaching(make_user, api)
    payout = _payout_row(trainer)
    reversed_ = api(_operator(make_user)).post(
        "/api/wallet/admin/reversal/", {"reference_id": payout.reference_id,
                                        "idempotency_key": uuid.uuid4().hex},
        content_type=JSON)
    assert reversed_.status_code == 200, reversed_.content

    body = api(trainer).get("/api/wallet/balance/").json()
    assert (body["balance"], body["settling"], body["available"]) == \
        ("0.00", "0.00", "0.00")
    row = next(r for r in api(trainer).get("/api/wallet/transactions/").json()["results"]
               if r["reference_id"] == payout.reference_id)
    assert row["settling"] is False


def test_no_hold_when_the_hold_is_zero_days(make_user, api, settings):
    settings.PAYOUT_HOLD_DAYS = 0
    trainer, _client = _paid_coaching(make_user, api)
    body = api(trainer).get("/api/wallet/balance/").json()
    assert (body["available"], body["settling"]) == ("30000.00", "0.00")


def test_settling_money_cannot_be_withdrawn(make_user, api):
    trainer, _client = _paid_coaching(make_user, api)
    _shamcash(api, trainer)
    from wallet.models import Wallet
    Wallet.objects.filter(owner=trainer).update(balance=Decimal("45000.00"))

    refused = _request(api, trainer, "20000")
    assert _code(refused) == (409, "amount_exceeds_available")
    assert refused.json()["available"] == "15000.00"
    assert refused.json()["settling"] == "30000.00"
    assert refused.json()["settling_until"]
    assert _balance(trainer) == Decimal("45000.00")

    assert _request(api, trainer, "15000").status_code == 201


# ===========================================================================
# 2. Payout methods
# ===========================================================================

def test_a_shamcash_address_is_typed_twice_with_the_holders_name(make_user, api):
    trainer = _trainer(make_user)
    client = api(trainer)

    def post(**body):
        return client.post(METHODS, {"kind": "shamcash", **body}, content_type=JSON)

    assert _code(post(address="", address_confirm="", holder_name="x")) == \
        (400, "payout_address_required")
    assert _code(post(address="12", address_confirm="12", holder_name="x")) == \
        (400, "payout_address_invalid")
    assert _code(post(address=ADDRESS, address_confirm="0a3f 91c2 7be4 55d1",
                      holder_name="x")) == (400, "payout_address_mismatch")
    assert _code(post(address=ADDRESS, address_confirm=ADDRESS, holder_name=" ")) == \
        (400, "payout_holder_name_required")
    assert _code(post(address=ADDRESS, address_confirm=ADDRESS, holder_name="x" * 121)) == \
        (400, "payout_holder_name_too_long")
    assert _code(client.post(METHODS, {"kind": "paypal"}, content_type=JSON)) == \
        (400, "payout_method_kind_invalid")

    # Retyped the way people retype it: other grouping, capitals.
    made = post(address=ADDRESS, address_confirm="0A3F-91C2-7BE4-55D0",
                holder_name="رامي حداد", password="never-read")
    assert made.status_code == 201, made.content
    body = made.json()
    assert body["kind"] == "shamcash" and body["address"] == ADDRESS
    assert body["holder_name"] == "رامي حداد" and body["agent"] is None
    assert body["is_default"] is True and body["verified"] is False
    assert "password" not in body

    assert _code(post(address="0a3f91c27be455d0", address_confirm=ADDRESS,
                      holder_name="x")) == (409, "payout_method_exists")


def test_the_address_and_the_name_are_encrypted_at_rest(make_user, api):
    from django.db import connection

    trainer = _trainer(make_user)
    made = _shamcash(api, trainer)
    with connection.cursor() as cur:
        cur.execute("SELECT shamcash_address, holder_name FROM payouts_payoutmethod "
                    "WHERE id = %s", [made["id"]])
        address, holder = cur.fetchone()
    assert "0a3f91c27be455d0" not in address and "رامي" not in holder


def test_cash_is_collected_from_a_listed_active_agent(make_user, api):
    from wallet.models import AgentProfile

    trainer = _trainer(make_user)
    agent_user = make_user(_tag("wd_agent"), user_type="agent")
    agent = AgentProfile.objects.create(user=agent_user, display_name="مكتب الفرات",
                                        city="دمشق", area="المزة", is_listed=False)
    client = api(trainer)
    body = {"kind": "agent_cash", "agent_id": agent.pk}
    assert _code(client.post(METHODS, body, content_type=JSON)) == \
        (400, "payout_agent_unavailable")
    AgentProfile.objects.filter(pk=agent.pk).update(is_listed=True)
    made = client.post(METHODS, body, content_type=JSON)
    assert made.status_code == 201, made.content
    assert made.json()["agent"]["name"] == "مكتب الفرات"
    assert made.json()["address"] is None
    assert _code(client.post(METHODS, body, content_type=JSON)) == \
        (409, "payout_method_exists")


def test_one_default_the_cap_and_delete(make_user, api, settings):
    settings.PAYOUT_METHODS_MAX = 2
    trainer = _trainer(make_user)
    first = _shamcash(api, trainer, "aaaa bbbb cccc dddd")
    second = _shamcash(api, trainer, "aaaa bbbb cccc eeee", is_default=True)
    listed = api(trainer).get(METHODS).json()["results"]
    assert [m["id"] for m in listed] == [second["id"], first["id"]]
    assert [m["is_default"] for m in listed] == [True, False]

    resp = api(trainer).post(METHODS, {"kind": "shamcash", "address": "aaaabbbbccccffff",
                                       "address_confirm": "aaaabbbbccccffff",
                                       "holder_name": "x"}, content_type=JSON)
    assert _code(resp) == (409, "payout_methods_limit") and resp.json()["max"] == 2

    made_default = api(trainer).post(f"{METHODS}{first['id']}/default/")
    assert made_default.status_code == 200 and made_default.json()["is_default"] is True
    assert api(trainer).delete(f"{METHODS}{first['id']}/").status_code == 204
    assert [(m["id"], m["is_default"]) for m in api(trainer).get(METHODS).json()["results"]] \
        == [(second["id"], True)]

    other = _trainer(make_user)
    assert _code(api(other).delete(f"{METHODS}{second['id']}/")) == \
        (404, "payout_method_not_found")
    assert _code(api(other).post(f"{METHODS}{second['id']}/default/")) == \
        (404, "payout_method_not_found")


def test_every_route_is_for_trainers(make_user, api):
    client = make_user(_tag("wd_client"))
    for method, path in (("get", METHODS), ("post", METHODS), ("get", WITHDRAWALS),
                         ("post", WITHDRAWALS), ("get", f"{WITHDRAWALS}setup/"),
                         ("get", f"{WITHDRAWALS}1/"), ("post", f"{WITHDRAWALS}1/cancel/"),
                         ("delete", f"{METHODS}1/"), ("post", f"{METHODS}1/default/")):
        resp = getattr(api(client), method)(path, {}, content_type=JSON)
        assert _code(resp) == (403, "not_a_trainer"), (method, path, resp.content)


# ===========================================================================
# 3. Asking for a withdrawal
# ===========================================================================

def test_only_a_verified_trainer_can_withdraw(make_user, api):
    trainer = _trainer(make_user, verified=False)
    _fund(trainer, "100000", "trainer")
    _shamcash(api, trainer)
    assert api(trainer).get(f"{WITHDRAWALS}setup/").json()["refusal"] == \
        "trainer_not_verified"
    assert _code(_request(api, trainer, "20000")) == (403, "trainer_not_verified")
    assert _balance(trainer) == Decimal("100000.00")


def test_the_money_leaves_for_payout_clearing_at_once(
        make_user, api, django_capture_on_commit_callbacks):
    from notifications.models import Notification
    from payouts.models import Withdrawal
    from policy.money import MoneyType
    from wallet.models import Transaction

    trainer, method = _ready(make_user, api)
    clearing_before = _clearing()
    with django_capture_on_commit_callbacks(execute=True):
        resp = _request(api, trainer, "80000")
    assert resp.status_code == 201, resp.content
    body = resp.json()
    today = timezone.localdate()
    assert re.fullmatch(rf"WD-{today:%Y-%m-%d}-\d{{3}}", body["number"]), body["number"]
    assert body["state"] == "requested" and body["can_cancel"] is True
    assert body["amount"] == "80000.00" and body["currency"] == "SYP"
    assert body["destination"] == {"kind": "shamcash", "address": ADDRESS,
                                   "holder_name": "رامي حداد", "agent": None}
    assert body["method_id"] == method["id"]
    assert [step["state"] for step in body["timeline"]] == ["requested"]

    assert _balance(trainer) == Decimal("20000.00")
    assert _clearing() == clearing_before + Decimal("80000.00")
    withdrawal = Withdrawal.objects.get(pk=body["id"])
    leg = Transaction.objects.get(reference_id=withdrawal.hold_reference)
    assert leg.money_type == MoneyType.WALLET_MOVE
    assert leg.source_wallet.owner == trainer and leg.amount == Decimal("80000.00")

    # The trainer's ledger reads it as a withdrawal, with nobody named as the other side.
    row = next(r for r in api(trainer).get("/api/wallet/transactions/").json()["results"]
               if r["reference_id"] == leg.reference_id)
    assert (row["kind"], row["direction"], row["counterparty"]) == \
        ("withdrawal", "out", None)
    wallet = api(trainer).get("/api/wallet/balance/").json()
    assert (wallet["balance"], wallet["available"], wallet["in_withdrawal"]) == \
        ("20000.00", "20000.00", "80000.00")

    push = Notification.objects.get(recipient=trainer, event_type="withdrawal_requested")
    assert push.metadata["data"] == {"type": "withdrawal_requested",
                                     "withdrawal_id": str(withdrawal.pk),
                                     "number": withdrawal.number}

    second = _trainer(make_user)
    _fund(second, "50000", "trainer")
    _shamcash(api, second)
    number = _request(api, second, "10000").json()["number"]
    assert int(number[-3:]) == int(body["number"][-3:]) + 1


def test_a_retry_moves_the_money_once(make_user, api):
    trainer, _method = _ready(make_user, api)
    first = _request(api, trainer, "20000", key="wd-key-1")
    again = _request(api, trainer, "20000", key="wd-key-1")
    assert (first.status_code, again.status_code) == (201, 200)
    assert again.json()["id"] == first.json()["id"]
    assert _balance(trainer) == Decimal("80000.00")

    reused = _request(api, trainer, "30000", key="wd-key-1")
    assert _code(reused) == (422, "idempotency_key_reused")
    assert _balance(trainer) == Decimal("80000.00")


def test_every_guardrail_names_its_code(make_user, api, settings):
    from payouts import services
    from wallet.models import Wallet

    trainer, _method = _ready(make_user, api, amount="700000")
    below = _request(api, trainer, "9999.99")
    assert _code(below) == (400, "withdrawal_below_minimum")
    assert (below.json()["minimum"], below.json()["currency"]) == ("10000.00", "SYP")
    over_cap = _request(api, trainer, "500000.01")
    assert _code(over_cap) == (429, "daily_limit_exceeded")
    assert over_cap.json()["daily_cap_left"] == "500000.00"

    Wallet.objects.filter(owner=trainer).update(balance=Decimal("20000.00"))
    assert _code(_request(api, trainer, "20000.01")) == (402, "insufficient_balance")

    open_one = _request(api, trainer, "10000")
    assert open_one.status_code == 201
    second = _request(api, trainer, "10000")
    assert _code(second) == (409, "withdrawal_already_open")
    assert second.json()["withdrawal"]["number"] == open_one.json()["number"]
    assert _balance(trainer) == Decimal("10000.00")

    # Today's cap counts what was asked and paid, not what came back.
    settings.PAYOUT_DAILY_CAP = {"SYP": Decimal("25000")}
    assert api(trainer).post(f"{WITHDRAWALS}{open_one.json()['id']}/cancel/").status_code == 200
    Wallet.objects.filter(owner=trainer).update(balance=Decimal("100000.00"))
    paid = _request(api, trainer, "20000").json()
    operator = _operator(make_user)
    services.approve(paid["id"], operator)
    services.mark_sent(paid["id"], operator, "SC-CAP-1")
    services.confirm(paid["id"], operator)
    capped = _request(api, trainer, "10000")
    assert _code(capped) == (429, "daily_limit_exceeded")
    assert capped.json()["daily_cap_left"] == "5000.00"
    setup = api(trainer).get(f"{WITHDRAWALS}setup/").json()
    assert setup["refusal"] == "daily_limit_exceeded"

    # A currency nobody set a cap for withdraws nothing: fail closed.
    settings.PAYOUT_DAILY_CAP = {}
    assert _code(_request(api, trainer, "10000")) == (429, "daily_limit_exceeded")


def test_the_method_must_be_the_trainers_and_there_must_be_one(make_user, api):
    from wallet.models import AgentProfile

    trainer = _trainer(make_user)
    _fund(trainer, "100000", "trainer")
    assert _code(_request(api, trainer, "10000")) == (409, "payout_method_required")
    other = _trainer(make_user)
    theirs = _shamcash(api, other)
    assert _code(_request(api, trainer, "10000", method_id=theirs["id"])) == \
        (404, "payout_method_not_found")

    agent = AgentProfile.objects.create(user=make_user(_tag("wd_ag"), user_type="agent"),
                                        display_name="Shop", is_listed=True)
    cash = api(trainer).post(METHODS, {"kind": "agent_cash", "agent_id": agent.pk},
                             content_type=JSON).json()
    AgentProfile.objects.filter(pk=agent.pk).update(status="suspended")
    assert _code(_request(api, trainer, "10000", method_id=cash["id"])) == \
        (409, "payout_agent_unavailable")


def test_a_build_that_may_not_move_wallet_money_cannot_ask(make_user, api):
    trainer, _method = _ready(make_user, api)
    ios = api(trainer).post(WITHDRAWALS, {"amount": "20000", "idempotency_key": "k"},
                            content_type=JSON, HTTP_X_APP_CHANNEL="ios")
    assert _code(ios) == (403, "not_available_on_channel")
    none = api(trainer).post(WITHDRAWALS, {"amount": "20000", "idempotency_key": "k"},
                             content_type=JSON, HTTP_X_APP_CHANNEL="")
    assert _code(none) == (400, "app_channel_required")
    assert _balance(trainer) == Decimal("100000.00")

    assert api(trainer).get(f"{WITHDRAWALS}setup/", HTTP_X_APP_CHANNEL="ios").json()[
        "refusal"] == "not_available_on_channel"
    made = _request(api, trainer, "20000")
    cancel = api(trainer).post(f"{WITHDRAWALS}{made.json()['id']}/cancel/",
                               HTTP_X_APP_CHANNEL="ios")
    assert _code(cancel) == (403, "not_available_on_channel")


def test_the_setup_is_what_the_withdraw_screen_draws(make_user, api):
    trainer, method = _ready(make_user, api, amount="92400")
    setup = api(trainer).get(f"{WITHDRAWALS}setup/").json()
    assert setup["currency"] == "SYP"
    assert (setup["balance"], setup["available"], setup["settling"]) == \
        ("92400.00", "92400.00", "0.00")
    assert (setup["minimum"], setup["daily_cap"], setup["daily_cap_left"], setup["fee"]) == \
        ("10000.00", "500000.00", "500000.00", "0.00")
    assert setup["hold_days"] == 3 and setup["verified"] is True
    assert setup["open_withdrawal"] is None and setup["refusal"] is None
    assert [m["id"] for m in setup["methods"]] == [method["id"]]

    made = _request(api, trainer, "80000").json()
    setup = api(trainer).get(f"{WITHDRAWALS}setup/").json()
    assert setup["open_withdrawal"]["id"] == made["id"]
    assert setup["refusal"] == "withdrawal_already_open"
    assert (setup["available"], setup["in_withdrawal"], setup["daily_cap_left"]) == \
        ("12400.00", "80000.00", "420000.00")


def test_the_list_and_one_withdrawal(make_user, api):
    trainer, _method = _ready(make_user, api)
    first = _request(api, trainer, "10000").json()
    api(trainer).post(f"{WITHDRAWALS}{first['id']}/cancel/")
    second = _request(api, trainer, "20000").json()
    page = api(trainer).get(WITHDRAWALS).json()
    assert page["count"] == 2
    assert [w["id"] for w in page["results"]] == [second["id"], first["id"]]
    assert api(trainer).get(f"{WITHDRAWALS}{first['id']}/").json()["state"] == "cancelled"
    other = _trainer(make_user)
    assert _code(api(other).get(f"{WITHDRAWALS}{first['id']}/")) == \
        (404, "withdrawal_not_found")
    assert _code(api(other).post(f"{WITHDRAWALS}{first['id']}/cancel/")) == \
        (404, "withdrawal_not_found")


# ===========================================================================
# 4. The life of a withdrawal
# ===========================================================================

def test_cancel_while_requested_returns_the_money(make_user, api):
    from payouts.models import Withdrawal
    from wallet.models import Transaction

    trainer, _method = _ready(make_user, api)
    clearing_before = _clearing()
    made = _request(api, trainer, "30000").json()
    cancelled = api(trainer).post(f"{WITHDRAWALS}{made['id']}/cancel/")
    assert cancelled.status_code == 200
    assert cancelled.json()["state"] == "cancelled" and cancelled.json()["can_cancel"] is False
    assert _balance(trainer) == Decimal("100000.00") and _clearing() == clearing_before
    again = api(trainer).post(f"{WITHDRAWALS}{made['id']}/cancel/")
    assert again.status_code == 200 and _balance(trainer) == Decimal("100000.00")

    withdrawal = Withdrawal.objects.get(pk=made["id"])
    back = Transaction.objects.get(reference_id=withdrawal.return_reference)
    assert back.metadata["purpose"] == "withdrawal_returned"
    assert [(e.from_state, e.to_state) for e in withdrawal.events.all()] == \
        [("", "requested"), ("requested", "cancelled")]


def test_approved_cannot_be_cancelled(make_user, api):
    from payouts import services

    trainer, _method = _ready(make_user, api)
    made = _request(api, trainer, "30000").json()
    services.approve(made["id"], _operator(make_user))
    refused = api(trainer).post(f"{WITHDRAWALS}{made['id']}/cancel/")
    assert _code(refused) == (409, "withdrawal_not_cancellable")
    assert refused.json()["state"] == "approved"
    assert _balance(trainer) == Decimal("70000.00")


def test_approve_send_confirm(make_user, api, django_capture_on_commit_callbacks):
    from notifications.models import Notification
    from payouts import services
    from payouts.models import PayoutMethod, Withdrawal
    from wallet.models import Transaction

    trainer, method = _ready(make_user, api)
    operator = _operator(make_user)
    made = _request(api, trainer, "40000").json()
    clearing_open = _clearing()

    with pytest.raises(services.TransitionRefused):
        services.confirm(made["id"], operator)             # not sent yet
    services.approve(made["id"], operator)
    services.approve(made["id"], operator)                  # idempotent
    with pytest.raises(services.TransitionRefused) as needs_id:
        services.mark_sent(made["id"], operator, "")
    assert needs_id.value.code == "transaction_id_required"
    services.mark_sent(made["id"], operator, "SC-TX-778899")
    services.mark_sent(made["id"], operator, "SC-TX-778899")   # idempotent
    with pytest.raises(services.TransitionRefused):
        services.mark_sent(made["id"], operator, "SC-TX-other")

    shown = api(trainer).get(f"{WITHDRAWALS}{made['id']}/").json()
    assert shown["state"] == "sent" and shown["transfer_reference"] == "SC-TX-778899"

    with django_capture_on_commit_callbacks(execute=True):
        services.confirm(made["id"], operator)
    services.confirm(made["id"], operator)                  # idempotent
    withdrawal = Withdrawal.objects.get(pk=made["id"])
    assert withdrawal.state == "confirmed"
    assert _clearing() == clearing_open - Decimal("40000.00")
    assert _balance(trainer) == Decimal("60000.00")
    out = Transaction.objects.get(reference_id=withdrawal.payout_reference)
    assert out.destination_wallet is None and out.money_type == "WALLET_MOVE"
    assert [(e.to_state, e.actor_role) for e in withdrawal.events.all()] == \
        [("requested", "trainer"), ("approved", "operator"), ("sent", "operator"),
         ("confirmed", "operator")]
    assert withdrawal.events.get(to_state="sent").evidence["shamcash_transaction_id"] == \
        "SC-TX-778899"
    assert PayoutMethod.objects.get(pk=method["id"]).verified_at is not None
    assert api(trainer).get(METHODS).json()["results"][0]["verified"] is True
    assert [s["state"] for s in api(trainer).get(f"{WITHDRAWALS}{made['id']}/").json()[
        "timeline"]] == ["requested", "approved", "sent", "confirmed"]

    push = Notification.objects.get(recipient=trainer, event_type="withdrawal_paid")
    assert push.metadata["data"]["number"] == withdrawal.number

    # One ShamCash transfer pays one withdrawal.
    again = _request(api, trainer, "10000").json()
    services.approve(again["id"], operator)
    with pytest.raises(services.TransitionRefused) as taken:
        services.mark_sent(again["id"], operator, "SC-TX-778899")
    assert taken.value.code == "transaction_id_in_use"


def test_a_failed_withdrawal_returns_the_money(
        make_user, api, django_capture_on_commit_callbacks):
    from notifications.models import Notification
    from payouts import services

    trainer, _method = _ready(make_user, api)
    operator = _operator(make_user)
    clearing_before = _clearing()
    made = _request(api, trainer, "25000").json()
    services.approve(made["id"], operator)
    services.mark_sent(made["id"], operator, "SC-TX-bounced")
    with pytest.raises(services.TransitionRefused) as no_reason:
        services.fail(made["id"], operator, "  ")
    assert no_reason.value.code == "reason_required"
    with django_capture_on_commit_callbacks(execute=True):
        services.fail(made["id"], operator, "الحساب غير صحيح")
    services.fail(made["id"], operator, "again")            # idempotent, nothing moves
    assert _balance(trainer) == Decimal("100000.00") and _clearing() == clearing_before
    shown = api(trainer).get(f"{WITHDRAWALS}{made['id']}/").json()
    assert shown["state"] == "failed" and shown["failure_reason"] == "الحساب غير صحيح"
    push = Notification.objects.get(recipient=trainer, event_type="withdrawal_failed")
    assert push.metadata["data"]["reason"] == "الحساب غير صحيح"
    with pytest.raises(services.TransitionRefused):
        services.confirm(made["id"], operator)


def test_the_destination_is_copied_when_asked(make_user, api):
    trainer, method = _ready(make_user, api)
    made = _request(api, trainer, "20000").json()
    assert api(trainer).delete(f"{METHODS}{method['id']}/").status_code == 204
    shown = api(trainer).get(f"{WITHDRAWALS}{made['id']}/").json()
    assert shown["method_id"] is None
    assert shown["destination"]["address"] == ADDRESS


def test_the_database_holds_one_open_withdrawal_per_trainer(make_user, api):
    from django.db import IntegrityError, transaction

    from payouts.models import Withdrawal

    trainer, _method = _ready(make_user, api)
    made = Withdrawal.objects.get(pk=_request(api, trainer, "20000").json()["id"])
    with pytest.raises(IntegrityError):
        with transaction.atomic():
            Withdrawal.objects.create(number="WD-test-x", trainer=trainer,
                                      amount=Decimal("1"), currency="SYP",
                                      destination_kind="shamcash")
    assert made.is_open


def test_an_event_is_never_changed(make_user, api):
    from django.db import DatabaseError, connection, transaction

    from payouts.models import ImmutableEvent, WithdrawalEvent

    trainer, _method = _ready(make_user, api)
    _request(api, trainer, "20000")
    event = WithdrawalEvent.objects.get(withdrawal__trainer=trainer)
    event.to_state = "confirmed"
    with pytest.raises(ImmutableEvent):
        event.save()
    with pytest.raises(ImmutableEvent):
        event.delete()
    if connection.vendor != "postgresql":
        pytest.skip("the append-only trigger is PostgreSQL-only")
    with pytest.raises(DatabaseError):
        with transaction.atomic():
            WithdrawalEvent.objects.filter(pk=event.pk).update(to_state="confirmed")
    with pytest.raises(DatabaseError):
        with transaction.atomic():
            WithdrawalEvent.objects.filter(pk=event.pk).delete()
    assert WithdrawalEvent.objects.get(pk=event.pk).to_state == "requested"


def test_the_export_carries_payouts_and_erasure_keeps_the_money_record(make_user, api):
    from payouts.models import PayoutMethod, Withdrawal
    from training_platform.privacy.registry import erase_user_data, export_user_data

    trainer, _method = _ready(make_user, api)
    made = _request(api, trainer, "20000").json()
    sections = export_user_data(trainer)["sections"]
    assert sections["payout_methods"][0]["shamcash_address"] == "0a3f91c27be455d0"
    assert sections["withdrawals"][0]["destination_address"] == "0a3f91c27be455d0"
    assert [e["to_state"] for e in sections["withdrawal_events"]] == ["requested"]
    assert "evidence" not in sections["withdrawal_events"][0]

    api(trainer).post(f"{WITHDRAWALS}{made['id']}/cancel/")
    report = erase_user_data(trainer)
    assert report["payout_methods"]["action"] == "delete"
    assert not PayoutMethod.objects.filter(trainer=trainer).exists()
    assert Withdrawal.objects.filter(pk=made["id"]).exists()   # a record of money


def test_payout_clearing_is_the_platform_not_a_person(db):
    from wallet.utils import (get_payout_clearing_wallet, is_reserved_username,
                              ledger_owner_emails)

    wallet = get_payout_clearing_wallet("USD")
    assert wallet.currency == "USD" and wallet.owner.email in ledger_owner_emails()
    assert not wallet.owner.is_active
    assert is_reserved_username("platform_payout_clearing_syp")


# ===========================================================================
# 5. The console
# ===========================================================================

def _console(user):
    from django.test import Client

    client = Client()
    client.force_login(user)
    return client


def test_the_console_queue_is_operator_only_and_decides_from_the_page(make_user, api):
    from payouts.models import Withdrawal

    trainer, _method = _ready(make_user, api)
    older = _request(api, trainer, "20000").json()
    Withdrawal.objects.filter(pk=older["id"]).update(
        requested_at=timezone.now() - timedelta(days=1))

    staff = make_user(_tag("wd_staff"), is_staff=True)
    assert _console(staff).get("/dj-admin/payouts/withdrawalqueueentry/").status_code == 403

    console = _console(_operator(make_user))
    page = console.get("/dj-admin/payouts/withdrawalqueueentry/")
    assert page.status_code == 200 and older["number"] in page.content.decode()

    url = f"/dj-admin/payouts/withdrawalqueueentry/{older['id']}/change/"
    assert console.get(url).status_code == 200
    assert console.post(url, {"decision": "approve", "note": ""}).status_code == 302
    assert console.post(url, {"decision": "sent", "transfer_id": "SC-PAGE-1",
                              "note": ""}).status_code == 302
    withdrawal = Withdrawal.objects.get(pk=older["id"])
    assert (withdrawal.state, withdrawal.shamcash_transaction_id) == ("sent", "SC-PAGE-1")
    assert withdrawal.events.get(to_state="sent").actor.user_type == "admin"
