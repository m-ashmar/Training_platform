"""The money worker's walk states (`users/walk/coaching.py`, `users/walk/wallet.py`): each
one is built the way `manage.py seed_walk_states` builds it, and then read through the
API the way its screen reads it, as the state's own account.

Every state is made by the platform's own paths (the app's API in process, the
operator's service functions, the scheduled tasks after the dates are moved), so these
tests also prove each state is one the server can really reach.
"""
from datetime import timedelta
from decimal import Decimal

import pytest
from django.utils import timezone

from tests.walk_fixtures import other_currency_plan, walk_platform, walk_run  # noqa: F401

pytestmark = pytest.mark.django_db

JSON = "application/json"


def _build(walk_run, name):
    """The state as `seed_walk_states --only <name>` makes it (the shared walk fixture:
    a bootstrapped database, its after-commit work run). Answers its manifest entry and
    its main account."""
    from users.models import CustomUser

    entry = walk_run(name)
    return entry, CustomUser.objects.get(pk=entry["ids"]["user"])


def _code(resp):
    return resp.status_code, (resp.json().get("code") if resp.status_code >= 400 else None)


# ------------------------------------------------------------------ 1. frozen
def test_coaching_frozen(api, walk_run):
    entry, me = _build(walk_run, "coaching_frozen")
    ids = entry["ids"]
    c = api(me)

    coaching = c.get("/api/v1/coaching/current/").json()["coaching"]
    assert coaching["status"] == "frozen" and coaching["trainer_id"] == ids["trainer"]
    # The routine is readable and cannot be started.
    assert c.get(f"/api/routine/routines/{ids['routine']}/").status_code == 200
    started = c.post("/api/routine/workout-sessions/", {"routine": ids["routine"], "day": 1},
                     content_type=JSON)
    assert _code(started)[1] == "coaching_frozen", started.content
    # The plan lapsed: the coach's diet plan is readable, the diet is not usable.
    sub = c.get("/api/subscription/v1/subscriptions/current/")
    assert sub.status_code == 200 and sub.json()["access"] == "frozen", sub.content
    plans = c.get("/api/diet/v1/my/diet-plans/")
    assert plans.status_code == 200, plans.content
    rows = plans.json()["results"] if isinstance(plans.json(), dict) else plans.json()
    assert ids["diet_plan"] in {row["id"] for row in rows}
    assert c.get("/api/diet/v1/advice/latest/").status_code == 403


# ------------------------------------------------------------------ 2. ending soon
def test_coaching_renews_soon(api, walk_run):
    from notifications.models import Notification

    _entry, me = _build(walk_run, "coaching_renews_soon")
    coaching = api(me).get("/api/v1/coaching/current/").json()["coaching"]
    assert coaching["status"] == "active" and coaching["stop_renewal"] is False
    assert coaching["renewal_reminder"] is True and coaching["renews_at"]
    assert Notification.objects.filter(recipient=me,
                                       event_type="coaching_renewal_soon").exists()


def test_coaching_stops_soon(api, walk_run):
    from notifications.models import Notification

    _entry, me = _build(walk_run, "coaching_stops_soon")
    coaching = api(me).get("/api/v1/coaching/current/").json()["coaching"]
    assert coaching["status"] == "active" and coaching["stop_renewal"] is True
    assert coaching["ending_reminder"] is True and coaching["ends_at"]
    assert Notification.objects.filter(recipient=me,
                                       event_type="coaching_ending_soon").exists()


# ------------------------------------------------------------------ 3. plans
def test_plan_lapsed(api, walk_run):
    _entry, me = _build(walk_run, "plan_lapsed")
    c = api(me)
    sub = c.get("/api/subscription/v1/subscriptions/current/").json()
    assert sub["access"] == "frozen" and sub["status"] == "expired", sub
    assert c.get("/api/diet/v1/my/diet-plans/").status_code == 200
    assert c.get("/api/diet/v1/advice/latest/").status_code == 403


def test_plan_not_renewing(api, walk_run):
    _entry, me = _build(walk_run, "plan_not_renewing")
    sub = api(me).get("/api/subscription/v1/subscriptions/current/").json()
    assert sub["status"] == "active" and sub["auto_renew"] is False and sub["end_date"], sub
    assert sub["access"] == "active"


def test_plan_renewal_due(api, walk_run):
    from notifications.models import Notification

    _entry, me = _build(walk_run, "plan_renewal_due")
    sub = api(me).get("/api/subscription/v1/subscriptions/current/").json()
    assert sub["status"] == "active" and sub["renewal_due"] is True, sub
    assert Notification.objects.filter(recipient=me,
                                       event_type="subscription_expiring").exists()


# ------------------------------------------------------------------ 4. withdrawals
def _withdrawal(api, me, wid):
    rows = api(me).get("/api/v1/trainer/withdrawals/").json()
    rows = rows["results"] if isinstance(rows, dict) else rows
    [row] = [r for r in rows if r["id"] == wid]
    return row


@pytest.mark.parametrize("name,expected", [
    ("trainer_withdrawal_requested", "requested"),
    ("trainer_withdrawal_sent", "sent"),
    ("trainer_withdrawal_refused", "failed")])
def test_withdrawals(api, walk_run, name, expected):
    entry, me = _build(walk_run, name)
    row = _withdrawal(api, me, entry["ids"]["withdrawal"])
    assert row["state"] == expected, row
    setup = api(me).get("/api/v1/trainer/withdrawals/setup/").json()
    assert setup["verified"] is True and len(setup["methods"]) == 1
    balance = Decimal(str(api(me).get("/api/wallet/balance/").json()["balance"]))
    # 60,000 earned; 30,000 asked for is out of the balance until it fails.
    assert balance == (Decimal("60000.00") if expected == "failed" else Decimal("30000.00"))


# ------------------------------------------------------------------ 5. agents
def test_agent_busy_day(api, walk_run):
    _entry, me = _build(walk_run, "agent_busy_day")
    pounds = api(me).get("/api/wallet/agent/me/").json()["currencies"]["SYP"]
    # 240,000 handed over today, 60,000 settled: 180,000 held of a 200,000 ceiling.
    assert (pounds["count_today"], Decimal(pounds["used_today"])) == (5, Decimal("240000"))
    assert Decimal(pounds["receivable"]) == Decimal("180000")
    assert Decimal(pounds["receivable_ceiling"]) == Decimal("200000")
    assert Decimal(pounds["receivable_room"]) == Decimal("20000")
    assert pounds["last_settlement"] and Decimal(pounds["last_settlement"]["amount"]) == \
        Decimal("60000")
    ledger = api(me).get("/api/wallet/agent/ledger/").json()
    assert ledger


def test_agent_key_paired_elsewhere(api, walk_run):
    _entry, me = _build(walk_run, "agent_key_paired_elsewhere")
    here = api(me).post("/api/wallet/agent/apikey/ensure/", {"device_name": "iPhone 15"},
                        content_type=JSON)
    assert _code(here) == (409, "key_paired_elsewhere"), here.content
    assert here.json()["device_name"] == "Samsung A54"


# ------------------------------------------------------------------ 6. before a coaching
def _balance(api, me):
    return api(me).get("/api/wallet/balance/").json()


def test_client_request_waiting(api, walk_run):
    entry, me = _build(walk_run, "client_request_waiting")
    status = api(me).get("/api/auth/client/request-status/").json()
    [row] = [r for r in status["requests"] if r["trainer_id"] == entry["ids"]["trainer"]]
    assert row["status"] == "pending" and Decimal(str(row["agreed_rate"])) == Decimal("40000")
    wallet = _balance(api, me)
    assert Decimal(str(wallet["balance"])) == Decimal("45000.00")   # nothing held yet
    assert Decimal(str(wallet["held"] or 0)) == 0


def test_client_awaiting_first_program(api, walk_run):
    entry, me = _build(walk_run, "client_awaiting_first_program")
    coaching = api(me).get("/api/v1/coaching/current/").json()["coaching"]
    assert coaching["status"] == "awaiting_first_program"
    assert coaching["trainer_id"] == entry["ids"]["trainer"]
    wallet = _balance(api, me)
    assert Decimal(str(wallet["balance"])) == Decimal("5000.00")
    assert Decimal(str(wallet["held"])) == Decimal("40000.00")


def test_client_invited_by_coach(api, walk_run):
    entry, me = _build(walk_run, "client_invited_by_coach")
    invites = api(me).get("/api/auth/client/trainer-invites/").json()["invitations"]
    [row] = invites
    assert (row["request_id"], row["trainer_id"]) == (entry["ids"]["request"],
                                                      entry["ids"]["trainer"])
    assert Decimal(str(row["agreed_rate"])) == Decimal("40000") and row["note"]


# ------------------------------------------------------------------ 7. 8. 9. wallets
def test_client_wallet_covers_plan(api, walk_run):
    from subscription.models import SubscriptionPlan

    _entry, me = _build(walk_run, "client_wallet_covers_plan")
    current = api(me).get("/api/subscription/v1/subscriptions/current/")
    assert current.status_code == 404 or not current.json().get("id"), current.content
    gateways = api(me).get("/api/subscription/v1/gateways/").json()
    assert gateways["can_pay_from_wallet"] is True
    # The cheapest plan IN THE CLIENT'S CURRENCY: prices in two currencies are never
    # compared (a 12.00 USD plan is not cheaper than a 1,000 SYP one), the walk-state
    # fix of 2026-11-24 after the app builders' database had both and no rate.
    currency = gateways["wallet"]["currency"]
    cheapest = (SubscriptionPlan.objects.filter(is_active=True, price__gt=0,
                                                currency=currency)
                .order_by("price", "id").first())
    assert Decimal(gateways["wallet"]["balance"]) == cheapest.price
    assert _entry["ids"]["plan"] == cheapest.pk


def test_client_wallet_short_of_coach(api, walk_run):
    entry, me = _build(walk_run, "client_wallet_short_of_coach")
    assert Decimal(str(_balance(api, me)["balance"])) == Decimal("10000.00")
    asked = api(me).post("/api/auth/client/request-trainer/",
                         {"trainer_id": entry["ids"]["trainer"]}, content_type=JSON)
    assert _code(asked) == (402, "insufficient_balance"), asked.content
    assert Decimal(str(asked.json()["required"])) == Decimal("40000")


def test_client_wallet_untouched(api, walk_run):
    from wallet.models import Wallet

    _entry, me = _build(walk_run, "client_wallet_untouched")
    wallet = _balance(api, me)
    assert Decimal(str(wallet["balance"])) == 0 and wallet["currency_locked"] is False
    assert wallet["currency_options"]
    assert not Wallet.objects.filter(owner=me, currency_chosen_at__isnull=False).exists()


# ------------------------------------------------------------------ nobody else touched
MONEY_STATES = (
    "coaching_frozen", "coaching_renews_soon", "coaching_stops_soon", "plan_lapsed",
    "plan_not_renewing", "plan_renewal_due", "trainer_withdrawal_requested",
    "trainer_withdrawal_sent", "trainer_withdrawal_refused", "agent_busy_day",
    "agent_key_paired_elsewhere", "client_request_waiting", "client_awaiting_first_program",
    "client_invited_by_coach", "client_wallet_covers_plan", "client_wallet_short_of_coach",
    "client_wallet_untouched")


def test_no_account_but_the_walk_accounts_is_renewed_lapsed_or_reminded(
        make_user, api, walk_run):
    """A builder's own accounts on the same development database, each one run of the
    scheduled work away from a change: a plan whose cover has ended, a coaching month
    that has ended and is paid for, and a month ending in three days. Every money state
    runs; none of them is touched (the framework owner's review, M1)."""
    from notifications.models import Notification
    from subscription.models import Subscription, SubscriptionPlan
    from tests.test_coaching_months import _accepted, _deliver_first_program

    plan = SubscriptionPlan.objects.filter(is_active=True, price__gt=0).first()
    lapsing = make_user("m1_builder_plan")
    sub = Subscription.objects.create(user=lapsing, plan=plan, status="active",
                                      source="shamcash")
    Subscription.objects.filter(pk=sub.pk).update(
        status="active", end_date=timezone.now() - timedelta(hours=3),
        start_date=timezone.now() - timedelta(days=31))
    trainer, due_client, due = _accepted(make_user, api, "m1_due", funds="1000.00")
    _deliver_first_program(api, trainer, due_client)
    _soon_trainer, soon_client, soon = _accepted(make_user, api, "m1_soon", funds="1000.00")
    _deliver_first_program(api, _soon_trainer, soon_client)
    for relation, when in ((due, timezone.now() - timedelta(hours=1)),
                           (soon, timezone.now() + timedelta(days=2, hours=20))):
        period = relation.periods.order_by("-number").first()
        shift = period.ends_at - when
        type(period).objects.filter(pk=period.pk).update(
            starts_at=period.starts_at - shift, ends_at=period.ends_at - shift)
    people = [lapsing, due_client, soon_client]
    notices = Notification.objects.filter(recipient__in=people).count()

    walk_run(*MONEY_STATES)

    assert Subscription.objects.get(pk=sub.pk).status == "active"           # not lapsed
    due_month = due.periods.order_by("-number").first()
    assert (due.periods.count(), due_month.frozen_at) == (1, None)     # not renewed/frozen
    soon_month = soon.periods.order_by("-number").first()
    assert soon_month.renewal_reminder_sent_at is None                       # not reminded
    assert Notification.objects.filter(recipient__in=people).count() == notices


# ------------------------------------------------------------------ a second run
def _withdrawal_queue(api, make_user):
    from users.models import CustomUser

    operator = (CustomUser.objects.filter(username="m2_console_operator").first()
                or make_user("m2_console_operator", user_type="admin"))
    queues = api(operator).get("/api/v1/ops/queue-counts/").json()["queues"]
    return {row["key"]: row["count"] for row in queues}["withdrawals"]


def test_a_second_run_leaves_the_withdrawal_queue_as_the_first(api, make_user, walk_run):
    """Each run of the withdrawal states opens two withdrawals (requested, sent). The
    reset closes the retired trainers' open ones as an operator closes one that cannot
    be paid, so the operator's queue holds the same after run 2 as after run 1 (the
    framework owner's review, M2)."""
    from payouts.models import Withdrawal

    states = ("trainer_withdrawal_requested", "trainer_withdrawal_sent",
              "trainer_withdrawal_refused")
    first = walk_run(*states)
    after_first = _withdrawal_queue(api, make_user)
    assert after_first == 2
    walk_run(*states)
    assert _withdrawal_queue(api, make_user) == after_first
    for name in states[:2]:
        old = Withdrawal.objects.get(pk=first["states"][name]["ids"]["withdrawal"])
        assert old.state == "failed" and old.failure_reason


def test_a_second_run_leaves_no_held_month_behind(walk_run):
    """A month held for a programme never sent is given back to its client when the
    state is reset (M3); a paid-out month holds nothing. Escrow is the same after run 2
    as after run 1."""
    from users.models import TrainerClientRelation
    from wallet.utils import get_escrow_wallet

    states = ("client_awaiting_first_program", "coaching_frozen")
    first = walk_run(*states)
    held_after_first = get_escrow_wallet("SYP").balance
    walk_run(*states)
    assert get_escrow_wallet("SYP").balance == held_after_first
    old = TrainerClientRelation.objects.get(
        pk=first["states"]["client_awaiting_first_program"]["ids"]["relation"])
    assert old.hold_reference == ""


# ------------------------------------------------------------------ two currencies, no rate
def test_every_money_state_runs_beside_a_cheaper_plan_in_the_other_currency(
        other_currency_plan, walk_run):
    """The app builders' development database: a dollar plan at 12.00 beside the pound
    plans, and no exchange rate. Every money state runs; each plan a state bought or
    priced is in its client's currency; and no rate was set on the way."""
    from django.conf import settings

    from subscription.models import Subscription, SubscriptionPlan
    from users.models import CustomUser
    from wallet import currency_for
    from wallet.models import ExchangeRate

    manifest = walk_run(*MONEY_STATES)

    assert set(manifest["states"]) >= set(MONEY_STATES)
    for name in MONEY_STATES:
        ids = manifest["states"][name]["ids"]
        me = CustomUser.objects.get(pk=ids["user"])
        if "plan" in ids:
            plan = SubscriptionPlan.objects.get(pk=ids["plan"])
            assert plan.currency == currency_for(me) == settings.PLATFORM_CURRENCY, name
        if "subscription" in ids:
            bought = Subscription.objects.get(pk=ids["subscription"])
            assert bought.plan_id != other_currency_plan.pk, name
    assert not ExchangeRate.objects.exists()


def test_a_plan_state_refuses_by_name_when_no_plan_is_on_sale_in_its_currency(
        other_currency_plan, walk_run):
    """With only the other currency's plan on sale, a state that needs a plan refuses
    and names the currency, rather than converting, making a plan or moving the
    wallet's currency; nothing of it is kept."""
    from django.conf import settings
    from django.core.management.base import CommandError

    from subscription.models import SubscriptionPlan
    from wallet.models import ExchangeRate, Wallet

    base = settings.PLATFORM_CURRENCY
    SubscriptionPlan.objects.filter(currency=base).update(is_active=False)
    wallets = Wallet.objects.count()

    with pytest.raises(CommandError) as refused:
        walk_run("client_wallet_covers_plan")

    assert f"no paid subscription plan on sale in {base}" in str(refused.value)
    assert Wallet.objects.count() == wallets
    assert not SubscriptionPlan.objects.filter(currency=base, is_active=True).exists()
    assert not ExchangeRate.objects.exists()
