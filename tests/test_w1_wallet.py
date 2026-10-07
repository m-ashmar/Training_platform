"""Wave 1 of the owner's fix run (2026-09-28): the wallet rows.

One test per row, each written against the journey that went wrong. Every test here
fails on 554c604.

| Test | Row |
|---|---|
| `test_s237_*` | the generic admin reversal took one leg of a program sale |
| `test_b145_*` | ensure/ from another phone was answered "already paired" |
| `test_b144_*`, `test_b146_*`, `test_b148_*` | agent/me/: currency, the server's day, the status docstring |
| `test_b147_*` | a reversal row of the agent ledger did not name the top-up it took back |
| `test_b163_*` | the transfer's note was dropped; the transfer is now a coaching payment (owner) |
"""
from decimal import Decimal

import pytest

from tests.test_fx_subs import shamcash  # noqa: F401
from tests.test_hub_checkout import _product_session
from tests.test_routine_products import _buy, _fund, _published

pytestmark = pytest.mark.django_db

JSON = "application/json"
D = Decimal
REVERSAL = "/api/wallet/admin/reversal/"


def _code(resp):
    return resp.status_code, resp.json().get("code")


# ===========================================================================
# S-237: the generic admin reversal refuses every leg of a program sale
# ===========================================================================

def test_s237_no_leg_of_a_program_sale_is_reversed_on_its_own(
        make_user, api, settings, shamcash, django_capture_on_commit_callbacks):
    """Reversing a checkout's mint leg (None -> escrow) burned the shared per-currency
    escrow, which backs other clients' coaching holds, while the purchase stayed
    confirmed and the trainer's and the platform's shares stayed paid; the proper refund
    then refused. Every leg of a sale — the checkout's and the wallet sale's — is now
    refused with its own code, naming the purchase, and the refund still works."""
    from routine import products
    from routine.models import RoutinePurchase
    from subscription.services.payment_service import PaymentService
    from wallet.models import Transaction, Wallet
    from wallet.utils import get_escrow_wallet

    settings.COACHING_COMMISSION_RATE = D("0.20")
    operator = make_user("w1s237_op", user_type="admin")

    # Other clients' holds in escrow: the money the mint's reversal used to burn.
    escrow = get_escrow_wallet("SYP")
    Wallet.objects.filter(pk=escrow.pk).update(balance=escrow.balance + D("5000.00"))
    escrow.refresh_from_db()
    before = escrow.balance

    # A checkout's sale: None -> escrow 2,500, escrow -> trainer 2,000, -> platform 500.
    _trainer, buyer, _c, product_id, body = _product_session(make_user, api, "w1s237")
    with django_capture_on_commit_callbacks(execute=True):
        PaymentService.complete_payment(body["id"], {
            "amount": "2500.00", "currency": "SYP", "status": "completed",
            "transaction_id": "tx-w1s237"})
    checkout = RoutinePurchase.objects.get(buyer=buyer, product_id=product_id)

    # A wallet sale: buyer -> escrow, escrow -> trainer, escrow -> platform.
    _t2, _tpl, second_id = _published(make_user, api, "w1s237w")
    wallet_buyer = make_user("w1s237_wb")
    _fund(wallet_buyer, "10000.00")
    assert _buy(api, wallet_buyer, second_id, key="w1s237-buy").status_code == 200
    sale = RoutinePurchase.objects.get(buyer=wallet_buyer, product_id=second_id)

    for purchase in (checkout, sale):
        legs = list(Transaction.objects.filter(metadata__purchase_id=purchase.pk)
                    .order_by("id"))
        assert len(legs) == 3
        for n, leg in enumerate(legs):
            resp = api(operator).post(REVERSAL, {"reference_id": leg.reference_id,
                                                 "idempotency_key": f"w1s237-{purchase.pk}-{n}"},
                                      content_type=JSON)
            assert _code(resp) == (409, "routine_product_leg"), (leg.metadata, resp.content)
            assert resp.json()["purchase"] == {"id": purchase.pk, "status": "confirmed",
                                               "paid_with": purchase.paid_with}
            assert not Transaction.objects.filter(reverses=leg).exists()
        purchase.refresh_from_db()
        assert purchase.status == "confirmed"
    escrow.refresh_from_db()
    assert escrow.balance == before, "the shared escrow is untouched"

    # The door the refusal points to still works: every leg goes back together.
    products.refund(sale.pk, operator, "لم يطابق الوصف")
    sale.refresh_from_db()
    assert sale.status == "refunded"
    assert Transaction.objects.filter(metadata__purchase_id=sale.pk,
                                      tx_type="reversal").count() == 3


# ===========================================================================
# B-145: one agent, one device — ensure/ from another phone is refused
# ===========================================================================

def test_b145_ensure_from_another_phone_is_refused_and_create_moves_the_key(make_user, api):
    """ensure/ recorded a device name only on a key that had none, so a second phone
    was answered "already paired" and shown the first phone's name. The owner
    (2026-09-28): another device is refused with `key_paired_elsewhere`, naming the
    device the key is on; `create/` moves the key on purpose."""
    from wallet.models import AgentAPIKey, ensure_agent_profile

    agent = make_user("w1b145_a", user_type="agent")
    profile = ensure_agent_profile(agent)
    ensure = "/api/wallet/agent/apikey/ensure/"

    first = api(agent).post(ensure, {"device_name": "Samsung A54"}, content_type=JSON)
    assert first.status_code == 201, first.content
    key = AgentAPIKey.objects.get(agent=profile, is_active=True)

    other = api(agent).post(ensure, {"device_name": "iPhone 15"}, content_type=JSON)
    assert _code(other) == (409, "key_paired_elsewhere"), other.content
    assert other.json()["device_name"] == "Samsung A54"
    assert other.json()["fingerprint"] == first.json()["fingerprint"]
    assert other.json()["paired_at"] == first.json()["paired_at"]
    status = api(agent).get("/api/wallet/agent/apikey/status/").json()
    assert (status["key_id"], status["device_name"]) == (key.key_id, "Samsung A54")
    assert AgentAPIKey.objects.filter(agent=profile, is_active=True).count() == 1

    # The paired phone, or a caller that names no device, is answered as before.
    same = api(agent).post(ensure, {"device_name": "samsung a54"}, content_type=JSON)
    assert (same.status_code, same.json()["created"], same.json()["key_id"]) == \
        (200, False, key.key_id)
    bare = api(agent).post(ensure, {}, content_type=JSON)
    assert (bare.status_code, bare.json()["key_id"]) == (200, key.key_id)

    # Moving the key is create/, on purpose; then the new phone is the paired one.
    moved = api(agent).post("/api/wallet/agent/apikey/create/",
                            {"device_name": "iPhone 15"}, content_type=JSON)
    assert moved.status_code == 201, moved.content
    again = api(agent).post(ensure, {"device_name": "iPhone 15"}, content_type=JSON)
    assert (again.status_code, again.json()["key_id"], again.json()["device_name"]) == \
        (200, moved.json()["key_id"], "iPhone 15")
    assert _code(api(agent).post(ensure, {"device_name": "Samsung A54"},
                                 content_type=JSON)) == (409, "key_paired_elsewhere")


# ===========================================================================
# B-144, B-146, B-148: GET /api/wallet/agent/me/
# ===========================================================================
ME = "/api/wallet/agent/me/"


def _agent(make_user, name, **fields):
    from wallet.models import ensure_agent_profile

    agent = make_user(name, user_type="agent")
    profile = ensure_agent_profile(agent)
    profile.status = "active"
    for key, value in fields.items():
        setattr(profile, key, value)
    profile.save()
    return agent, profile


def test_b144_agent_me_names_the_agents_currency(make_user, api):
    """The answer carried caps per currency and never said which one is the agent's
    own, so the phone read `wallet/balance/` to know which object to draw."""
    from wallet.models import set_currency_for

    dollar, _p = _agent(make_user, "w1b144_usd")
    set_currency_for(dollar, "USD", "agent")
    body = api(dollar).get(ME).json()
    assert body["currency"] == "USD"
    assert body["currencies"]["USD"]["currency"] == "USD"

    pound, _p = _agent(make_user, "w1b144_syp")
    assert api(pound).get(ME).json()["currency"] == "SYP"


def test_b146_agent_me_says_the_servers_day_and_the_ledger_reads_in_it(
        make_user, api, settings, monkeypatch):
    """The caps' "today" and `since`/`until` are the server's day (Asia/Damascus), and
    nothing told the phone which day or zone that is; the agent ledger's `created_at`
    was written in UTC, unlike `wallet/transactions/`."""
    import datetime

    from django.utils import timezone

    from wallet.models import AgentReceivableEntry

    agent, profile = _agent(make_user, "w1b146_a", daily_limit=D("100000"),
                            monthly_limit=D("100000"), receivable_ceiling=D("100000"))
    client = make_user("w1b146_c")
    top = api(agent).post("/api/wallet/agent/topup/proxy/", {
        "client_identifier": str(client.id), "amount": "400.00", "currency": "SYP",
        "idempotency_key": "w1b146-1"}, content_type=JSON)
    assert top.status_code == 200, top.content
    (row,) = api(agent).get("/api/wallet/agent/ledger/").json()["results"]
    entry = AgentReceivableEntry.objects.get(agent=profile)
    assert row["created_at"] == timezone.localtime(entry.created_at).isoformat()
    receipt = api(agent).get(f"/api/wallet/agent/receipts/{top.json()['reference_id']}/")
    assert receipt.json()["created_at"] == \
        timezone.localtime(entry.transaction.created_at).isoformat()

    # 22:30 UTC is 01:30 the next day in Damascus: the server's day is the next one.
    late = datetime.datetime(2026, 9, 27, 22, 30, tzinfo=datetime.timezone.utc)
    monkeypatch.setattr(timezone, "now", lambda: late)
    body = api(agent).get(ME).json()
    assert body["time_zone"] == settings.TIME_ZONE
    assert body["today"] == timezone.localdate(late).isoformat()
    if settings.TIME_ZONE == "Asia/Damascus":
        assert body["today"] == "2026-09-28"


def test_b148_agent_me_docstring_names_the_profile_status():
    """The docstring said the top-level `status` was the account's; it is the agent's
    own `AgentProfile.status`, the same value as `profile.status`."""
    from wallet.views import AgentMeView

    doc = AgentMeView.__doc__
    assert "`status` is the account's" not in doc
    assert "AgentProfile.status" in doc


# ===========================================================================
# B-147: a reversal row of the agent ledger names the top-up it took back
# ===========================================================================
LEDGER = "/api/wallet/agent/ledger/"


def test_b147_a_reversal_row_names_the_top_up_it_took_back(make_user, api):
    """A `reversal` row carried the REVERSAL's reference, whose receipt answers 404, and
    nothing linked it to the top-up; the top-up row never said it had been taken back.
    Now the reversal row names the top-up (whose receipt opens) and the top-up row says
    `reversed`, and the page still costs the same number of queries."""
    from cachalot.api import cachalot_disabled
    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    agent, _profile = _agent(make_user, "w1b147_a", daily_limit=D("100000"),
                             monthly_limit=D("100000"), receivable_ceiling=D("100000"))
    client = make_user("w1b147_c")
    operator = make_user("w1b147_op", user_type="admin")

    def top_up(key, amount="300.00"):
        resp = api(agent).post("/api/wallet/agent/topup/proxy/", {
            "client_identifier": str(client.id), "amount": amount, "currency": "SYP",
            "idempotency_key": key}, content_type=JSON)
        assert resp.status_code == 200, resp.content
        return resp.json()["reference_id"]

    def reverse(ref, key):
        resp = api(operator).post(REVERSAL, {"reference_id": ref, "idempotency_key": key},
                                  content_type=JSON)
        assert resp.status_code == 200, resp.content
        return resp.json()

    kept = top_up("w1b147-1")
    taken = top_up("w1b147-2", "500.00")
    reverse(taken, "w1b147-r2")

    rows = api(agent).get(LEDGER).json()["results"]
    assert [r["kind"] for r in rows] == ["reversal", "topup", "topup"]
    back, taken_row, kept_row = rows
    assert back["reverses_reference_id"] == taken
    assert back["reference_id"] != taken
    assert (taken_row["reference_id"], taken_row["reversed"]) == (taken, True)
    assert (kept_row["reference_id"], kept_row["reversed"]) == (kept, False)
    assert taken_row["reverses_reference_id"] is None
    assert back["reversed"] is None
    receipt = api(agent).get(f"/api/wallet/agent/receipts/{back['reverses_reference_id']}/")
    assert receipt.status_code == 200, receipt.content
    assert receipt.json()["reversed"] is True

    def cost():
        with cachalot_disabled(), CaptureQueriesContext(connection) as queries:
            assert api(agent).get(LEDGER).status_code == 200
        return len(queries)

    cost()      # warm per-process caches
    small = cost()
    for n in range(3):
        reverse(top_up(f"w1b147-more-{n}"), f"w1b147-more-r{n}")
    assert len(api(agent).get(LEDGER).json()["results"]) == 9
    assert cost() == small, "the link between a top-up and its reversal costs a query a row"


# ===========================================================================
# B-163: a client's payment to a trainer carries a note and is a coaching payment
# ===========================================================================
TRANSFER = "/api/wallet/client/transfer/"
TRANSACTIONS = "/api/wallet/transactions/"
NOTE = "جلسة الجمعة الإضافية"


def _pair(make_user, name, balance="8000.00"):
    from tests.test_routine_products import _fund

    client = make_user(f"{name}_c")
    trainer = make_user(f"{name}_t", user_type="trainer")
    _fund(client, balance, "client")
    _fund(trainer, "0.00", "trainer")
    return client, trainer


def _send(api, client, trainer, key, amount="5000.00", **extra):
    return api(client).post(TRANSFER, {"trainer_id": trainer.id, "amount": amount,
                                       "idempotency_key": key, **extra},
                            content_type=JSON)


def _row(api, user, reference_id=None):
    rows = api(user).get(TRANSACTIONS).json()["results"]
    if reference_id is None:
        return rows
    (row,) = [r for r in rows if r["reference_id"] == reference_id]
    return row


def test_b163_the_transfer_note_reaches_the_trainers_row(make_user, api, settings):
    """SendMoney's note ('جلسة الجمعة الإضافية') was dropped by the serializer, and a
    note holding a banned term was answered 200. Now it is shown to both people on their
    own row, a banned one moves nothing, and — the owner, 2026-09-28 — the payment is a
    coaching payment: the platform keeps its 20% and the trainer gets the rest."""
    from wallet.models import CoachingTransfer, Transaction, Wallet
    from wallet.utils import get_escrow_wallet, get_platform_revenue_wallet

    settings.COACHING_COMMISSION_RATE = D("0.20")
    client, trainer = _pair(make_user, "w1b163")
    escrow, platform = get_escrow_wallet("SYP"), get_platform_revenue_wallet("SYP")
    escrow_before, platform_before = escrow.balance, platform.balance

    sent = _send(api, client, trainer, "w1b163-1", note=NOTE)
    assert sent.status_code == 200, sent.content
    ref = sent.json()["reference_id"]
    assert sent.json()["client_balance"] == "3000.00"

    # The money: the client paid 5,000; the trainer got 4,000, the platform 1,000.
    assert Wallet.objects.get(owner=trainer).balance == D("4000.00")
    platform.refresh_from_db()
    escrow.refresh_from_db()
    assert platform.balance - platform_before == D("1000.00")
    assert escrow.balance == escrow_before, "escrow nets to zero inside the transfer"
    record = CoachingTransfer.objects.get(charge_reference=ref)
    assert (record.amount, record.commission_rate, record.commission, record.trainer_share,
            record.note) == (D("5000.00"), D("0.2000"), D("1000.00"), D("4000.00"), NOTE)
    legs = {tx.reference_id: tx for tx in Transaction.objects.filter(reference_id__in=[
        record.charge_reference, record.payout_reference, record.commission_reference])}
    assert len(legs) == 3
    assert {tx.money_type for tx in legs.values()} == {"COACHING_1TO1"}
    fee = legs[record.commission_reference]
    assert (fee.metadata["purpose"], fee.metadata["rate"]) == ("coaching_commission", "0.20")
    assert all(NOTE not in str(tx.metadata) for tx in legs.values()), \
        "the note never enters the append-only ledger"

    # Both people read the note on their own row; the kinds are what they were.
    mine = _row(api, client, ref)
    assert (mine["kind"], mine["amount"], mine["note"], mine["counterparty"]["id"]) == \
        ("trainer_transfer", "5000.00", NOTE, trainer.id)
    (theirs,) = _row(api, trainer)
    assert (theirs["kind"], theirs["amount"], theirs["note"], theirs["counterparty"]["id"],
            theirs["settling"]) == ("transfer_received", "4000.00", NOTE, client.id, False)
    assert api(client).get(f"{TRANSACTIONS}{ref}/").json()["note"] == NOTE
    assert api(trainer).get(f"{TRANSACTIONS}{theirs['reference_id']}/").json()["note"] == NOTE

    # A note with a banned term is refused before anything moves, under its own code.
    spam = _send(api, client, trainer, "w1b163-2", amount="100.00",
                 note="كلمني على wa.me/963900000000")
    assert _code(spam) == (400, "content_not_allowed"), spam.content
    assert "note" in spam.json()["field_errors"]
    assert Wallet.objects.get(owner=client).balance == D("3000.00")
    assert CoachingTransfer.objects.filter(client=client).count() == 1
    # Too long is a validation error; another note under a spent key is another request.
    assert _code(_send(api, client, trainer, "w1b163-3", amount="100.00",
                       note="x" * 141)) == (400, "validation_error")
    assert _code(_send(api, client, trainer, "w1b163-1", note="غير ذلك")) == \
        (422, "idempotency_key_reused")
    # Without a note: null on both rows, and nothing else changes.
    bare = _send(api, client, trainer, "w1b163-4", amount="100.00")
    assert bare.status_code == 200, bare.content
    assert _row(api, client, bare.json()["reference_id"])["note"] is None


def test_b163_a_page_of_transfers_reads_its_notes_in_one_query(make_user, api, settings):
    """The note is joined to the rows the way the withdrawal block is: one query per
    page, whatever the number of transfers on it."""
    from cachalot.api import cachalot_disabled
    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    settings.COACHING_COMMISSION_RATE = D("0.20")
    client, trainer = _pair(make_user, "w1b163q", balance="10000.00")

    def cost(user):
        with cachalot_disabled(), CaptureQueriesContext(connection) as queries:
            assert api(user).get(TRANSACTIONS).status_code == 200
        return len(queries)

    assert _send(api, client, trainer, "w1b163q-0", amount="100.00",
                 note="حصة 0").status_code == 200
    for user in (client, trainer):
        cost(user)
    small = {user.pk: cost(user) for user in (client, trainer)}
    for n in range(1, 5):
        assert _send(api, client, trainer, f"w1b163q-{n}", amount="100.00",
                     note=f"حصة {n}").status_code == 200
    assert [r["note"] for r in _row(api, trainer)] == [f"حصة {n}" for n in range(4, -1, -1)]
    for user in (client, trainer):
        assert cost(user) == small[user.pk], "reading notes grew with the page"


def test_b163_the_note_goes_with_either_account_and_the_money_record_stays(
        make_user, api, settings, django_capture_on_commit_callbacks):
    """The note is user-written text: unlike the ledger, it is erased with the account
    of either person, while the record of the money stays."""
    from training_platform.privacy.registry import erase_user_data
    from wallet.models import CoachingTransfer, Transaction

    settings.COACHING_COMMISSION_RATE = D("0.20")
    client, trainer = _pair(make_user, "w1b163e")
    ref = _send(api, client, trainer, "w1b163e-1", note=NOTE).json()["reference_id"]

    with django_capture_on_commit_callbacks(execute=True):
        erase_user_data(client)
    record = CoachingTransfer.objects.get(charge_reference=ref)
    assert record.note == ""
    assert record.amount == D("5000.00")
    assert Transaction.objects.filter(reference_id=ref).exists()
    (theirs,) = _row(api, trainer)
    assert theirs["note"] is None


def test_b163_support_reverses_a_payment_to_a_trainer_whole(make_user, api, settings):
    """Three legs now: reversing the trainer's leg or the commission alone would leave
    escrow — which holds other clients' coaching money — paying for the other, so each
    is refused; the payment's own reference puts every leg back together."""
    from wallet.models import CoachingTransfer, Transaction, Wallet
    from wallet.utils import get_escrow_wallet, get_platform_revenue_wallet

    settings.COACHING_COMMISSION_RATE = D("0.20")
    operator = make_user("w1b163r_op", user_type="admin")
    client, trainer = _pair(make_user, "w1b163r")
    escrow, platform = get_escrow_wallet("SYP"), get_platform_revenue_wallet("SYP")
    escrow_before, platform_before = escrow.balance, platform.balance
    ref = _send(api, client, trainer, "w1b163r-1", note=NOTE).json()["reference_id"]
    record = CoachingTransfer.objects.get(charge_reference=ref)
    for n, leg in enumerate((record.payout_reference, record.commission_reference)):
        resp = api(operator).post(REVERSAL, {"reference_id": leg,
                                             "idempotency_key": f"w1b163r-leg-{n}"},
                                  content_type=JSON)
        assert _code(resp) == (409, "coaching_transfer_leg"), resp.content
        assert resp.json()["transfer_reference"] == ref
    assert not Transaction.objects.filter(tx_type="reversal",
                                          reverses__reference_id__in=[
                                              ref, record.payout_reference,
                                              record.commission_reference]).exists()

    whole = api(operator).post(REVERSAL, {"reference_id": ref, "idempotency_key": "w1b163r-all"},
                               content_type=JSON)
    assert whole.status_code == 200, whole.content
    assert Wallet.objects.get(owner=client).balance == D("8000.00")
    assert Wallet.objects.get(owner=trainer).balance == D("0.00")
    escrow.refresh_from_db()
    platform.refresh_from_db()
    assert (escrow.balance, platform.balance) == (escrow_before, platform_before)
    assert Transaction.objects.filter(tx_type="reversal", reverses__reference_id__in=[
        ref, record.payout_reference, record.commission_reference]).count() == 3
    assert _code(api(operator).post(REVERSAL, {"reference_id": ref,
                                               "idempotency_key": "w1b163r-again"},
                                    content_type=JSON)) == (409, "already_reversed")


def test_b163_a_build_that_may_not_pay_for_coaching_may_not_pay_a_trainer(make_user, api):
    """The money is coaching money now, so the build's coaching flag is asked as well as
    its wallet flag — and nothing but the build decides."""
    from policy.defaults import DEFAULT_FLAGS
    from policy.models import ChannelCapabilities
    from wallet.models import Wallet

    client, trainer = _pair(make_user, "w1b163p")
    flags = {**DEFAULT_FLAGS["apk"], "can_pay_coaching_in_app": False}
    rows = ChannelCapabilities.objects.filter(channel="apk")
    if rows.exists():
        rows.update(can_pay_coaching_in_app=False)
    else:
        ChannelCapabilities.objects.create(channel="apk", min_version="0.0.0", **flags)
    resp = _send(api, client, trainer, "w1b163p-1", amount="100.00")
    assert _code(resp) == (403, "not_available_on_channel"), resp.content
    assert Wallet.objects.get(owner=client).balance == D("8000.00")
