"""The agent shell: limits, the receivable, history and status (plan step 9; the spec,
section 8 "Agents"; BACKEND_BUGS B-113..B-118 and B-124).

The screens are `yalla_gym/design/yalla-gym-design/screens/16-agent-operator/`:
AgentHome (the daily gauge, today's counts, the cash held for the platform with its
ceiling and the next settlement), AgentHistory (per-transaction, daily and monthly
limits, then the ledger with settlements), AgentReceipt (a receipt by reference, "the
client got a notification"), AgentKey (pairing, fingerprint, last signature, revoke).

| Test | Row |
|---|---|
| `test_step9_*` | the spec: raise the defaults above the top plan |
| `test_b124_*` | 429 cap refusals say limit / used / remaining / currency; the per-transaction cap |
| `test_receivable_*` | every top-up is cash held for the platform; the ceiling; settlements |
| `test_b113_*` | `GET wallet/agent/me/` |
| `test_b114_*` | the agent's ledger, a receipt by reference, the settlement feed |
| `test_b115_*` | a top-up pushes the client (a real `Notification` row) |
| `test_b116_*` | `apikey/ensure/` answers `key_id` and the fingerprint |
| `test_b117_*` | `apikey/revoke/` and the pairing on `apikey/status/` |
| `test_b118_*` | a suspended or erased client is not credited, and not revealed |
"""
import datetime
import time
from decimal import Decimal as D

import pytest
from django.utils import timezone

JSON = "application/json"
PROXY = "/api/wallet/agent/topup/proxy/"
SIGNED = "/api/wallet/agent/topup/"
ME = "/api/wallet/agent/me/"
LEDGER = "/api/wallet/agent/ledger/"
SETTLEMENTS = "/api/wallet/agent/settlements/"

MONEY_FIELDS = ("daily_limit", "monthly_limit", "daily_limit_usd", "monthly_limit_usd",
                "per_transaction_limit", "per_transaction_limit_usd",
                "receivable_ceiling", "receivable_ceiling_usd")


def _code(resp):
    return resp.status_code, resp.json().get("code")


def _agent(make_user, name, **fields):
    from wallet.models import ensure_agent_profile

    agent = make_user(name, user_type="agent")
    profile = ensure_agent_profile(agent)
    profile.status = "active"
    for key, value in fields.items():
        setattr(profile, key, D(value) if key in MONEY_FIELDS and value is not None
                else value)
    profile.save()
    return agent, profile


def _topup(api, agent, client, amount, key, currency="SYP", identifier=None):
    return api(agent).post(PROXY, {
        "client_identifier": identifier if identifier is not None else str(client.id),
        "amount": amount, "currency": currency, "idempotency_key": key},
        content_type=JSON)


def _signed(api, agent, key_id, secret, identifier, amount, key, currency="SYP"):
    from wallet.security import compute_hmac_signature

    ts = int(time.time())
    sig = compute_hmac_signature(secret, f"{identifier}|{amount}|{ts}|{key}|{currency}")
    return api(agent).post(
        SIGNED, {"client_identifier": identifier, "amount": amount, "currency": currency,
                 "idempotency_key": key, "timestamp": ts, "signature": sig},
        content_type=JSON,
        HTTP_X_AGENT_AUTH=f"AgentAuth key_id={key_id},signature={sig},timestamp={ts}")


def _key(profile, key_id, secret="s" * 64):
    from wallet.models import AgentAPIKey
    from wallet.security import encrypt_secret

    return AgentAPIKey.objects.create(agent=profile, key_id=key_id, hashed_key="x",
                                      secret_ciphertext=encrypt_secret(secret),
                                      is_active=True)


def _balance(user):
    from wallet.models import Wallet

    wallet = Wallet.objects.filter(owner=user).first()
    return wallet.balance if wallet else D("0")


def _receivable(profile, currency="SYP"):
    from django.db.models import Sum

    from wallet.models import AgentReceivableEntry

    return (AgentReceivableEntry.objects.filter(agent=profile, currency=currency)
            .aggregate(total=Sum("amount"))["total"] or D("0"))


def _operator(make_user, name):
    return make_user(name, user_type="admin")


def _console(user):
    from django.test import Client

    client = Client()
    client.force_login(user)
    return client


# ===========================================================================
# The spec: "Raise the default limits. 200 SYP a day cannot cover one Basic plan at
# 1,000 SYP ... with the daily default above the price of the top plan."
# ===========================================================================

def test_step9_a_new_agent_can_top_up_the_price_of_the_top_plan_at_once(make_user, api,
                                                                       settings):
    from django.core.management import call_command

    from subscription.models import SubscriptionPlan

    call_command("setup_subscription_plans", verbosity=0)
    top = max(plan.price for plan in SubscriptionPlan.objects.filter(is_active=True)
              if (getattr(plan, "currency", "SYP") or "SYP") == "SYP")
    assert settings.AGENT_DEFAULT_DAILY_LIMIT > top
    assert settings.AGENT_DEFAULT_PER_TRANSACTION_LIMIT >= top
    assert settings.AGENT_DEFAULT_RECEIVABLE_CEILING >= settings.AGENT_DEFAULT_DAILY_LIMIT

    from wallet.models import ensure_agent_profile

    agent = make_user("as9_agent", user_type="agent")
    profile = ensure_agent_profile(agent)
    assert profile.daily_limit == settings.AGENT_DEFAULT_DAILY_LIMIT
    assert profile.per_transaction_limit == settings.AGENT_DEFAULT_PER_TRANSACTION_LIMIT
    assert profile.receivable_ceiling == settings.AGENT_DEFAULT_RECEIVABLE_CEILING
    client = make_user("as9_client")
    resp = _topup(api, agent, client, f"{top:.2f}", "as9-1")
    assert resp.status_code == 200, resp.content
    assert _balance(client) == top


def test_step9_an_agent_left_on_the_old_default_is_raised_and_a_chosen_cap_is_kept(
        make_user, settings):
    """The migration lifts only the pair nobody chose (200 / 5,000 SYP a day / month)."""
    import importlib

    from django.apps import apps

    from wallet.models import AgentProfile

    _a, old = _agent(make_user, "as9_old", daily_limit="200", monthly_limit="5000")
    _b, chosen = _agent(make_user, "as9_chosen", daily_limit="900", monthly_limit="5000")
    AgentProfile.objects.filter(pk__in=[old.pk, chosen.pk]).update(
        per_transaction_limit=None, receivable_ceiling=None)
    migration = importlib.import_module(
        "wallet.migrations.0019_agent_receivable_backfill_and_raised_caps")
    migration.raise_the_old_default_caps(apps, None)

    old.refresh_from_db()
    chosen.refresh_from_db()
    assert (old.daily_limit, old.monthly_limit, old.per_transaction_limit,
            old.receivable_ceiling) == (settings.AGENT_DEFAULT_DAILY_LIMIT,
                                        settings.AGENT_DEFAULT_MONTHLY_LIMIT,
                                        settings.AGENT_DEFAULT_PER_TRANSACTION_LIMIT,
                                        settings.AGENT_DEFAULT_RECEIVABLE_CEILING)
    assert (chosen.daily_limit, chosen.per_transaction_limit,
            chosen.receivable_ceiling) == (D("900"), None, None)


# ===========================================================================
# B-124: a cap refusal says the cap, what is used and what is left
# ===========================================================================

def test_b124_a_cap_refusal_says_the_cap_what_is_used_and_what_is_left(make_user, api):
    client = make_user("as124_c")
    agent, _p = _agent(make_user, "as124_a", daily_limit="1000", monthly_limit="100000",
                       per_transaction_limit="600", receivable_ceiling="100000")
    assert _topup(api, agent, client, "600.00", "as124-1").status_code == 200

    per_tx = _topup(api, agent, client, "700.00", "as124-2")
    assert _code(per_tx) == (429, "per_transaction_limit_exceeded"), per_tx.content
    assert {k: per_tx.json()[k] for k in ("limit", "used", "remaining", "currency")} == \
        {"limit": "600.00", "used": None, "remaining": "600.00", "currency": "SYP"}

    daily = _topup(api, agent, client, "500.00", "as124-3")
    assert _code(daily) == (429, "daily_limit_exceeded"), daily.content
    assert {k: daily.json()[k] for k in ("limit", "used", "remaining", "currency")} == \
        {"limit": "1000.00", "used": "600.00", "remaining": "400.00", "currency": "SYP"}
    assert daily.json()["error"]            # the message is kept beside the code

    monthly_agent, _p = _agent(make_user, "as124_m", daily_limit="100000",
                               monthly_limit="800", receivable_ceiling="100000")
    assert _topup(api, monthly_agent, client, "600.00", "as124-4").status_code == 200
    monthly = _topup(api, monthly_agent, client, "300.00", "as124-5")
    assert _code(monthly) == (429, "monthly_limit_exceeded"), monthly.content
    assert {k: monthly.json()[k] for k in ("limit", "used", "remaining", "currency")} == \
        {"limit": "800.00", "used": "600.00", "remaining": "200.00", "currency": "SYP"}
    assert _balance(client) == D("1200.00")


def test_b124_a_replayed_top_up_is_answered_even_once_the_cap_is_full(make_user, api):
    """A retry of a top-up that went through must get its receipt back, not a refusal
    written by the money it already moved."""
    client = make_user("as124r_c")
    agent, _p = _agent(make_user, "as124r_a", daily_limit="1000", monthly_limit="100000",
                       receivable_ceiling="100000")
    first = _topup(api, agent, client, "1000.00", "as124r-1")
    assert first.status_code == 200, first.content
    again = _topup(api, agent, client, "1000.00", "as124r-1")
    assert again.status_code == 200, again.content
    assert again.json() == first.json()
    assert _balance(client) == D("1000.00")


# ===========================================================================
# The receivable: every top-up is cash the agent holds for the platform
# ===========================================================================

def test_receivable_every_top_up_is_posted_once_on_both_paths(make_user, api):
    from django.db.models import Sum

    from policy.money import MoneyType
    from wallet.models import AgentReceivableEntry, Transaction

    client = make_user("asr_c")
    agent, profile = _agent(make_user, "asr_a", daily_limit="100000",
                            monthly_limit="100000", receivable_ceiling="100000")
    proxy = _topup(api, agent, client, "500.00", "asr-1")
    assert proxy.status_code == 200, proxy.content
    _key(profile, "asrsigned")
    signed = _signed(api, agent, "asrsigned", "s" * 64, str(client.id), "700.00", "asr-2")
    assert signed.status_code == 200, signed.content
    # A replay moves nothing and posts nothing.
    assert _topup(api, agent, client, "500.00", "asr-1").json() == proxy.json()

    entries = list(AgentReceivableEntry.objects.filter(agent=profile).order_by("id"))
    assert [(e.kind, e.amount, e.balance_after, e.currency) for e in entries] == [
        ("topup", D("500.00"), D("500.00"), "SYP"),
        ("topup", D("700.00"), D("1200.00"), "SYP")]
    assert [e.transaction.reference_id for e in entries] == \
        [proxy.json()["reference_id"], signed.json()["reference_id"]]
    assert {e.money_type for e in entries} == {MoneyType.WALLET_MOVE}
    # The books balance: what the agent credited is what they hold for the platform.
    credited = Transaction.objects.filter(actor=agent, tx_type="topup").aggregate(
        total=Sum("amount"))["total"]
    assert credited == _receivable(profile) == D("1200.00")


def test_receivable_the_ceiling_stops_top_ups_until_a_settlement(make_user, api):
    from wallet import agents

    client = make_user("asc_c")
    agent, profile = _agent(make_user, "asc_a", daily_limit="100000",
                            monthly_limit="100000", receivable_ceiling="1000")
    operator = _operator(make_user, "asc_op")
    assert _topup(api, agent, client, "800.00", "asc-1").status_code == 200

    over = _topup(api, agent, client, "300.00", "asc-2")
    assert _code(over) == (429, "receivable_ceiling_exceeded"), over.content
    assert {k: over.json()[k] for k in ("limit", "used", "remaining", "currency")} == \
        {"limit": "1000.00", "used": "800.00", "remaining": "200.00", "currency": "SYP"}
    assert _topup(api, agent, client, "200.00", "asc-3").status_code == 200
    assert _code(_topup(api, agent, client, "1.00", "asc-4"))[1] == \
        "receivable_ceiling_exceeded"

    agents.record_settlement(profile.pk, "SYP", D("1000.00"), operator,
                             note="Cash at the office", key="asc-settle-1")
    assert _receivable(profile) == D("0")
    assert _topup(api, agent, client, "300.00", "asc-5").status_code == 200
    assert _balance(client) == D("1300.00")


def test_receivable_an_operator_records_a_settlement_in_the_console_once(make_user, api):
    from wallet.models import AgentSettlement

    client = make_user("ascon_c")
    agent, profile = _agent(make_user, "ascon_a", daily_limit="100000",
                            monthly_limit="100000", receivable_ceiling="100000")
    assert _topup(api, agent, client, "5000.00", "ascon-1").status_code == 200

    staff = make_user("ascon_staff", is_staff=True)
    assert _console(staff).get("/dj-admin/wallet/agentreceivable/").status_code == 403

    operator = _operator(make_user, "ascon_op")
    console = _console(operator)
    listing = console.get("/dj-admin/wallet/agentreceivable/")
    assert listing.status_code == 200
    assert "5000.00" in listing.content.decode()

    url = f"/dj-admin/wallet/agentreceivable/{profile.pk}/change/"
    assert console.get(url).status_code == 200
    body = {"settle_currency": "SYP", "settle_amount": "3000.00",
            "settle_note": "Cash at the office", "settle_token": "ascon-tok-1"}
    assert console.post(url, body).status_code == 302
    assert console.post(url, body).status_code == 302          # the same form, twice
    (settlement,) = AgentSettlement.objects.filter(agent=profile)
    assert (settlement.amount, settlement.currency, settlement.recorded_by) == \
        (D("3000.00"), "SYP", operator)
    assert (settlement.receivable_before, settlement.receivable_after) == \
        (D("5000.00"), D("2000.00"))
    assert _receivable(profile) == D("2000.00")

    # More than the agent holds is refused on the page; nothing is written.
    too_much = console.post(url, {**body, "settle_amount": "2500.00",
                                  "settle_token": "ascon-tok-2"})
    assert too_much.status_code == 200
    assert AgentSettlement.objects.filter(agent=profile).count() == 1
    assert _receivable(profile) == D("2000.00")


def test_receivable_settlements_and_entries_are_immutable(make_user, api):
    from django.db import transaction

    from wallet import agents
    from wallet.models import AgentReceivableEntry, AgentSettlement

    client = make_user("asim_c")
    agent, profile = _agent(make_user, "asim_a", daily_limit="100000",
                            monthly_limit="100000", receivable_ceiling="100000")
    operator = _operator(make_user, "asim_op")
    assert _topup(api, agent, client, "900.00", "asim-1").status_code == 200
    settlement = agents.record_settlement(profile.pk, "SYP", D("400.00"), operator,
                                          note="", key="asim-settle")
    entry = AgentReceivableEntry.objects.filter(agent=profile, kind="topup").get()

    for row in (settlement, entry):
        with pytest.raises(Exception):
            row.save()
        with pytest.raises(Exception):
            row.delete()
    with pytest.raises(Exception), transaction.atomic():
        AgentSettlement.objects.filter(pk=settlement.pk).update(amount=D("1.00"))
    with pytest.raises(Exception), transaction.atomic():
        AgentReceivableEntry.objects.filter(pk=entry.pk).delete()
    assert _receivable(profile) == D("500.00")


def test_receivable_top_ups_made_before_it_existed_are_posted_once(make_user):
    """Migration 0019: every earlier AGENT top-up (and its reversal) is posted in the
    order it happened, dated as it happened; a plan payment's mint leg is not an agent's
    cash; running it twice posts nothing twice."""
    import importlib

    from django.apps import apps

    from wallet.models import (AgentReceivableEntry, Wallet, move_funds_atomic,
                               wallet_for)

    client = make_user("asbf_c")
    agent, profile = _agent(make_user, "asbf_a")
    wallet = wallet_for(client, "client")
    first = move_funds_atomic(None, wallet, D("300.00"), actor_id=agent.id,
                              tx_type="topup", currency="SYP",
                              money_type="WALLET_MOVE", metadata={"agent_id": agent.id})
    second = move_funds_atomic(None, wallet, D("200.00"), actor_id=agent.id,
                               tx_type="topup", currency="SYP",
                               money_type="WALLET_MOVE", metadata={"agent_id": agent.id})
    move_funds_atomic(None, wallet, D("999.00"), actor_id=client.id, tx_type="topup",
                      currency="SYP", money_type="PLATFORM_SUB",
                      metadata={"original_payment_id": "p-1"})
    back = move_funds_atomic(Wallet.objects.get(pk=wallet.pk), None, D("200.00"),
                             actor_id=agent.id, tx_type="reversal",
                             money_type="WALLET_MOVE", reverses=second)
    migration = importlib.import_module(
        "wallet.migrations.0019_agent_receivable_backfill_and_raised_caps")
    migration.backfill_the_receivable(apps, None)
    migration.backfill_the_receivable(apps, None)

    rows = list(AgentReceivableEntry.objects.filter(agent=profile).order_by("id"))
    assert [(r.kind, r.amount, r.balance_after, r.transaction_id) for r in rows] == [
        ("topup", D("300.00"), D("300.00"), first.pk),
        ("topup", D("200.00"), D("500.00"), second.pk),
        ("reversal", D("-200.00"), D("300.00"), back.pk)]
    assert rows[0].created_at == first.created_at
    assert _receivable(profile) == D("300.00")


def test_receivable_a_reversed_top_up_comes_off_it(make_user, api):
    client = make_user("asrev_c")
    agent, profile = _agent(make_user, "asrev_a", daily_limit="100000",
                            monthly_limit="100000", receivable_ceiling="100000")
    operator = _operator(make_user, "asrev_op")
    ref = _topup(api, agent, client, "1000.00", "asrev-1").json()["reference_id"]
    resp = api(operator).post("/api/wallet/admin/reversal/",
                              {"reference_id": ref, "idempotency_key": "asrev-r1"},
                              content_type=JSON)
    assert resp.status_code == 200, resp.content
    assert _receivable(profile) == D("0")
    kinds = list(profile.receivable_entries.order_by("id").values_list("kind", "amount"))
    assert kinds == [("topup", D("1000.00")), ("reversal", D("-1000.00"))]


# ===========================================================================
# B-113: GET /api/wallet/agent/me/
# ===========================================================================

def test_b113_the_agent_reads_its_own_account(make_user, api):
    client = make_user("as113_c")
    agent, profile = _agent(
        make_user, "as113_a", per_transaction_limit="50000", daily_limit="250000",
        monthly_limit="5000000", receivable_ceiling="600000",
        display_name="مكتب الفرات", city="دمشق", area="المزة", opening_hours="9 ص إلى 9 م",
        public_phone="0999111222", is_listed=True)
    assert _topup(api, agent, client, "400.00", "as113-1").status_code == 200
    assert _topup(api, agent, client, "600.00", "as113-2").status_code == 200

    resp = api(agent).get(ME)
    assert resp.status_code == 200, resp.content
    body = resp.json()
    assert body["profile"] == {"display_name": "مكتب الفرات", "city": "دمشق",
                               "area": "المزة", "opening_hours": "9 ص إلى 9 م",
                               "public_phone": "0999111222", "status": "active",
                               "is_listed": True}
    assert (body["status"], body["can_top_up"], body["blocked_reason"]) == \
        ("active", True, None)
    syp = body["currencies"]["SYP"]
    assert {k: syp[k] for k in (
        "per_transaction_limit", "daily_limit", "monthly_limit", "used_today",
        "used_this_month", "count_today", "count_this_month", "remaining_today",
        "remaining_this_month", "receivable", "receivable_ceiling", "receivable_room",
        "max_topup", "blocked_reason", "last_settlement")} == {
        "per_transaction_limit": "50000.00", "daily_limit": "250000.00",
        "monthly_limit": "5000000.00", "used_today": "1000.00",
        "used_this_month": "1000.00", "count_today": 2, "count_this_month": 2,
        "remaining_today": "249000.00", "remaining_this_month": "4999000.00",
        "receivable": "1000.00", "receivable_ceiling": "600000.00",
        "receivable_room": "599000.00", "max_topup": "50000.00", "blocked_reason": None,
        "last_settlement": None}
    usd = body["currencies"]["USD"]
    assert (usd["daily_limit"], usd["max_topup"], usd["blocked_reason"]) == \
        ("0.00", "0.00", "daily_limit_exceeded")
    # Nothing schedules settlements: null, never an invented date.
    assert body["next_settlement_on"] is None

    today = timezone.localdate()
    profile.next_settlement_on = today + datetime.timedelta(days=2)
    profile.save()
    assert api(agent).get(ME).json()["next_settlement_on"] == \
        (today + datetime.timedelta(days=2)).isoformat()
    profile.next_settlement_on = today - datetime.timedelta(days=1)
    profile.save()
    assert api(agent).get(ME).json()["next_settlement_on"] is None

    assert _code(api(client).get(ME)) == (403, "agents_only")

    profile.status = "suspended"
    profile.save()
    stopped = api(agent).get(ME).json()
    assert (stopped["status"], stopped["can_top_up"], stopped["blocked_reason"]) == \
        ("suspended", False, "agent_not_active")
    assert stopped["currencies"]["SYP"]["max_topup"] == "0.00"


# ===========================================================================
# B-114: the agent's ledger, a receipt by reference, the settlement feed
# ===========================================================================

def test_b114_the_agent_ledger_is_its_own_top_ups_and_settlements(make_user, api):
    from wallet import agents
    from wallet.utils import account_number_for

    one, two = make_user("as114_c1"), make_user("as114_c2")
    agent, profile = _agent(make_user, "as114_a", daily_limit="100000",
                            monthly_limit="100000", receivable_ceiling="100000")
    other, _p = _agent(make_user, "as114_b", daily_limit="100000",
                       monthly_limit="100000", receivable_ceiling="100000")
    operator = _operator(make_user, "as114_op")
    assert _topup(api, agent, one, "400.00", "as114-1").status_code == 200
    second = _topup(api, agent, two, "600.00", "as114-2").json()
    assert _topup(api, other, one, "999.00", "as114-3").status_code == 200
    agents.record_settlement(profile.pk, "SYP", D("500.00"), operator,
                             note="Cash at the office", key="as114-settle")

    body = api(agent).get(LEDGER).json()
    assert body["count"] == 3
    rows = body["results"]
    assert [(r["kind"], r["amount"], r["receivable_after"]) for r in rows] == [
        ("settlement", "-500.00", "500.00"), ("topup", "600.00", "1000.00"),
        ("topup", "400.00", "400.00")]
    topup = rows[1]
    assert topup["reference_id"] == second["reference_id"]
    assert topup["receipt_number"] == second["receipt_number"]
    assert topup["currency"] == "SYP"
    assert topup["client"]["id"] == two.id
    assert topup["client"]["account_number"] == account_number_for(two.id)
    assert rows[0]["settlement"]["note"] == "Cash at the office"
    assert rows[0]["client"] is None

    assert api(agent).get(LEDGER, {"kind": "settlement"}).json()["count"] == 1
    assert api(agent).get(LEDGER, {"kind": "topup", "currency": "SYP"}).json()["count"] == 2
    assert _code(api(agent).get(LEDGER, {"kind": "gift"})) == (400, "bad_kind")
    assert _code(api(agent).get(LEDGER, {"currency": "EUR"})) == \
        (400, "currency_unsupported")
    assert _code(api(agent).get(LEDGER, {"since": "2026-13-01"})) == (400, "bad_date")
    assert _code(api(agent).get(LEDGER, {"since": "2026-09-10", "until": "2026-09-01"})) \
        == (400, "date_range_reversed")
    today = timezone.localdate().isoformat()
    assert api(agent).get(LEDGER, {"since": today, "until": today}).json()["count"] == 3

    assert api(other).get(LEDGER).json()["count"] == 1
    assert _code(api(one).get(LEDGER)) == (403, "agents_only")

    feed = api(agent).get(SETTLEMENTS).json()
    (settled,) = feed["results"]
    assert {k: settled[k] for k in ("amount", "currency", "receivable_before",
                                    "receivable_after", "note")} == {
        "amount": "500.00", "currency": "SYP", "receivable_before": "1000.00",
        "receivable_after": "500.00", "note": "Cash at the office"}
    assert settled["number"] and settled["settled_at"]
    assert api(other).get(SETTLEMENTS).json()["count"] == 0


def test_b114_b115_a_receipt_by_reference_and_the_client_is_told(
        make_user, api, django_capture_on_commit_callbacks):
    from notifications.models import Notification

    client = make_user("as115_c", first_name="سامر", last_name="الخطيب")
    agent, _p = _agent(make_user, "as115_a", daily_limit="250000",
                       monthly_limit="5000000", per_transaction_limit="50000",
                       receivable_ceiling="600000", display_name="مكتب الفرات",
                       area="المزة")
    with django_capture_on_commit_callbacks(execute=True):
        resp = _topup(api, agent, client, "25000.00", "as115-1")
    assert resp.status_code == 200, resp.content
    ref = resp.json()["reference_id"]
    assert resp.json()["receipt_number"].startswith(
        f"TP-{timezone.localdate():%Y-%m-%d}-")
    assert resp.json()["remaining_today"] == "225000.00"

    push = Notification.objects.get(recipient=client, event_type="wallet_topped_up")
    assert push.related_object_id == ref
    assert push.metadata["data"] == {"type": "wallet_topped_up", "reference_id": ref,
                                     "amount": "25000.00", "currency": "SYP"}
    # A replay is the same top-up: no second notice.
    with django_capture_on_commit_callbacks(execute=True):
        assert _topup(api, agent, client, "25000.00", "as115-1").status_code == 200
    assert Notification.objects.filter(recipient=client,
                                       event_type="wallet_topped_up").count() == 1

    receipt = api(agent).get(f"/api/wallet/agent/receipts/{ref}/")
    assert receipt.status_code == 200, receipt.content
    body = receipt.json()
    assert body["receipt_number"] == resp.json()["receipt_number"]
    assert (body["reference_id"], body["amount"], body["currency"]) == \
        (ref, "25000.00", "SYP")
    assert body["client"]["name"] == "سامر الخطيب"
    assert (body["agent"]["name"], body["agent"]["area"]) == ("مكتب الفرات", "المزة")
    assert body["remaining_today"] == "225000.00"
    assert body["client_notified"] is True
    assert body["reversed"] is False
    assert body["created_at"]

    stranger, _p = _agent(make_user, "as115_b")
    assert _code(api(stranger).get(f"/api/wallet/agent/receipts/{ref}/")) == \
        (404, "not_found")
    assert _code(api(agent).get("/api/wallet/agent/receipts/no-such-reference/")) == \
        (404, "not_found")
    assert _code(api(client).get(f"/api/wallet/agent/receipts/{ref}/")) == \
        (403, "agents_only")


# ===========================================================================
# B-116 / B-117: the signing key
# ===========================================================================

def _grouped(key_id):
    hexed = key_id.upper()[:12]
    return " · ".join(hexed[i:i + 4] for i in range(0, 12, 4))


def test_b116_ensure_answers_the_key_id_and_its_fingerprint(make_user, api):
    from wallet.models import AgentAPIKey

    agent, profile = _agent(make_user, "as116_a")
    first = api(agent).post("/api/wallet/agent/apikey/ensure/",
                            {"device_name": "Samsung A54"}, content_type=JSON)
    assert first.status_code == 201, first.content
    key = AgentAPIKey.objects.get(agent=profile, is_active=True)
    assert (first.json()["created"], first.json()["key_id"], first.json()["fingerprint"]) \
        == (True, key.key_id, _grouped(key.key_id))
    assert "secret" not in first.json()

    again = api(agent).post("/api/wallet/agent/apikey/ensure/", {}, content_type=JSON)
    assert again.status_code == 200
    assert (again.json()["created"], again.json()["key_id"], again.json()["fingerprint"]) \
        == (False, key.key_id, _grouped(key.key_id))

    rotated = api(agent).post("/api/wallet/agent/apikey/create/", {}, content_type=JSON)
    assert rotated.status_code == 201
    assert rotated.json()["fingerprint"] == _grouped(rotated.json()["key_id"])


def test_b117_revoke_stops_top_ups_until_a_new_key_and_status_shows_the_pairing(
        make_user, api):
    client = make_user("as117_c")
    agent, _p = _agent(make_user, "as117_a", daily_limit="100000", monthly_limit="100000",
                       receivable_ceiling="100000")
    status_url = "/api/wallet/agent/apikey/status/"

    made = api(agent).post("/api/wallet/agent/apikey/create/",
                           {"device_name": "Samsung A54"}, content_type=JSON).json()
    status = api(agent).get(status_url).json()
    assert (status["has_active"], status["key_id"], status["fingerprint"],
            status["device_name"]) == (True, made["key_id"], _grouped(made["key_id"]),
                                       "Samsung A54")
    assert status["paired_at"] and status["last_signature_at"] is None
    assert status["revoked_at"] is None

    assert _topup(api, agent, client, "100.00", "as117-1").status_code == 200
    assert api(agent).get(status_url).json()["last_signature_at"] is not None
    signed = _signed(api, agent, made["key_id"], made["secret"], str(client.id),
                     "50.00", "as117-2")
    assert signed.status_code == 200, signed.content

    revoked = api(agent).post("/api/wallet/agent/apikey/revoke/", {}, content_type=JSON)
    assert revoked.status_code == 200, revoked.content
    assert revoked.json()["has_active"] is False and revoked.json()["revoked_at"]
    status = api(agent).get(status_url).json()
    assert status["has_active"] is False and status["revoked_at"]
    assert api(agent).get(ME).json()["blocked_reason"] == "agent_key_revoked"

    assert _code(_topup(api, agent, client, "100.00", "as117-3")) == \
        (403, "agent_key_revoked")
    old = _signed(api, agent, made["key_id"], made["secret"], str(client.id), "50.00",
                  "as117-4")
    assert _code(old) == (401, "agent_auth_invalid")
    assert _balance(client) == D("150.00")

    api(agent).post("/api/wallet/agent/apikey/create/", {}, content_type=JSON)
    assert _topup(api, agent, client, "100.00", "as117-5").status_code == 200
    assert api(agent).get(status_url).json()["revoked_at"] is None
    assert _code(api(client).post("/api/wallet/agent/apikey/revoke/", {},
                                  content_type=JSON)) == (403, "agents_only")


# ===========================================================================
# B-118: a suspended or erased client is not credited, and not revealed
# ===========================================================================

def test_b118_a_suspended_or_erased_client_is_not_found(make_user, api):
    from users.models import CustomUser
    from wallet.utils import account_number_for

    client = make_user("as118_c")
    CustomUser.objects.filter(pk=client.pk).update(is_active=False)
    agent, profile = _agent(make_user, "as118_a", daily_limit="100000",
                            monthly_limit="100000", receivable_ceiling="100000")
    _key(profile, "as118signed")

    missing = _topup(api, agent, None, "100.00", "as118-0", identifier="999999999")
    assert _code(missing) == (404, "not_found")
    for n, ident in enumerate((str(client.id), client.email,
                               account_number_for(client.id))):
        refused = _topup(api, agent, client, "100.00", f"as118-{n}", identifier=ident)
        assert _code(refused) == (404, "not_found"), (ident, refused.content)
        assert refused.json()["error"] == missing.json()["error"]
        signed = _signed(api, agent, "as118signed", "s" * 64, ident, "100.00",
                         f"as118-s{n}")
        assert _code(signed) == (404, "not_found"), (ident, signed.content)
    assert _balance(client) == D("0")
    assert _receivable(profile) == D("0")
