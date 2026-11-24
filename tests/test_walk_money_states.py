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

pytestmark = pytest.mark.django_db

PASSWORD = "Walk-Throughs-2026!"
JSON = "application/json"


@pytest.fixture(autouse=True)
def platform(db, settings, tmp_path):
    """What `bootstrap_platform` gives a development server and these states read: the
    spec's build rows, a paid plan with the diet, a diet template and two foods. And the
    documents a trainer sends for verification kept in a temporary directory."""
    from diet.models import DietPlanTemplate, FoodCategory, FoodItem
    from policy.defaults import DEFAULT_FLAGS
    from policy.models import ChannelCapabilities
    from subscription.models import SubscriptionPlan

    settings.MEDIA_ROOT = str(tmp_path / "media")
    settings.PRIVATE_MEDIA_ROOT = str(tmp_path / "private")
    settings.COACHING_COMMISSION_RATE = Decimal("0.20")
    for channel, flags in DEFAULT_FLAGS.items():
        ChannelCapabilities.objects.get_or_create(channel=channel, min_version="0.0.0",
                                                  defaults=flags)
    plan = SubscriptionPlan.objects.create(
        name="walk premium", plan_type="premium", description="d", price=Decimal("30000.00"),
        currency="SYP", duration_days=30, is_active=True, has_diet_access=True,
        has_routine_access=True)
    DietPlanTemplate.objects.create(name="walk 3 meals", meals_per_day=3, snacks_per_day=0)
    category = FoodCategory.objects.create(name="walk grains")
    for name in ("Walk rice", "Walk lentils"):
        FoodItem.objects.create(name=name, name_en=name, category=category,
                                api_id=f"walk-{name}", calories=130, protein=5, carbs=25,
                                fat=1, serving_size="100g", allergens=[],
                                allergen_source="verified")
    return plan


def _build(name, django_capture_on_commit_callbacks):
    """The state as the command builds it (one transaction), its notices delivered as
    they are on commit. Answers its manifest entry and its main account."""
    from users.models import CustomUser
    from users.walk import STATES, load_states
    from users.walk.framework import build

    load_states()
    with django_capture_on_commit_callbacks(execute=True):
        entry = build(STATES[name], PASSWORD)
    return entry, CustomUser.objects.get(pk=entry["ids"]["user"])


def _code(resp):
    return resp.status_code, (resp.json().get("code") if resp.status_code >= 400 else None)


# ------------------------------------------------------------------ 1. frozen
def test_coaching_frozen(api, django_capture_on_commit_callbacks):
    entry, me = _build("coaching_frozen", django_capture_on_commit_callbacks)
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
def test_coaching_renews_soon(api, django_capture_on_commit_callbacks):
    from notifications.models import Notification

    _entry, me = _build("coaching_renews_soon", django_capture_on_commit_callbacks)
    coaching = api(me).get("/api/v1/coaching/current/").json()["coaching"]
    assert coaching["status"] == "active" and coaching["stop_renewal"] is False
    assert coaching["renewal_reminder"] is True and coaching["renews_at"]
    assert Notification.objects.filter(recipient=me,
                                       event_type="coaching_renewal_soon").exists()


def test_coaching_stops_soon(api, django_capture_on_commit_callbacks):
    from notifications.models import Notification

    _entry, me = _build("coaching_stops_soon", django_capture_on_commit_callbacks)
    coaching = api(me).get("/api/v1/coaching/current/").json()["coaching"]
    assert coaching["status"] == "active" and coaching["stop_renewal"] is True
    assert coaching["ending_reminder"] is True and coaching["ends_at"]
    assert Notification.objects.filter(recipient=me,
                                       event_type="coaching_ending_soon").exists()


# ------------------------------------------------------------------ 3. plans
def test_plan_lapsed(api, django_capture_on_commit_callbacks):
    _entry, me = _build("plan_lapsed", django_capture_on_commit_callbacks)
    c = api(me)
    sub = c.get("/api/subscription/v1/subscriptions/current/").json()
    assert sub["access"] == "frozen" and sub["status"] == "expired", sub
    assert c.get("/api/diet/v1/my/diet-plans/").status_code == 200
    assert c.get("/api/diet/v1/advice/latest/").status_code == 403


def test_plan_not_renewing(api, django_capture_on_commit_callbacks):
    _entry, me = _build("plan_not_renewing", django_capture_on_commit_callbacks)
    sub = api(me).get("/api/subscription/v1/subscriptions/current/").json()
    assert sub["status"] == "active" and sub["auto_renew"] is False and sub["end_date"], sub
    assert sub["access"] == "active"


def test_plan_renewal_due(api, django_capture_on_commit_callbacks):
    from notifications.models import Notification

    _entry, me = _build("plan_renewal_due", django_capture_on_commit_callbacks)
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
def test_withdrawals(api, django_capture_on_commit_callbacks, name, expected):
    entry, me = _build(name, django_capture_on_commit_callbacks)
    row = _withdrawal(api, me, entry["ids"]["withdrawal"])
    assert row["state"] == expected, row
    setup = api(me).get("/api/v1/trainer/withdrawals/setup/").json()
    assert setup["verified"] is True and len(setup["methods"]) == 1
    balance = Decimal(str(api(me).get("/api/wallet/balance/").json()["balance"]))
    # 60,000 earned; 30,000 asked for is out of the balance until it fails.
    assert balance == (Decimal("60000.00") if expected == "failed" else Decimal("30000.00"))


# ------------------------------------------------------------------ 5. agents
def test_agent_busy_day(api, django_capture_on_commit_callbacks):
    _entry, me = _build("agent_busy_day", django_capture_on_commit_callbacks)
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


def test_agent_key_paired_elsewhere(api, django_capture_on_commit_callbacks):
    _entry, me = _build("agent_key_paired_elsewhere", django_capture_on_commit_callbacks)
    here = api(me).post("/api/wallet/agent/apikey/ensure/", {"device_name": "iPhone 15"},
                        content_type=JSON)
    assert _code(here) == (409, "key_paired_elsewhere"), here.content
    assert here.json()["device_name"] == "Samsung A54"

