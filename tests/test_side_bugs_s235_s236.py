"""SIDE_BUGS S-235 and S-236: the launch commands, run again on a live database.

S-235. `seed_review_accounts` gave all six review accounts one password on a guessable
address pattern, and the review notes hand two of those addresses and that password to
Apple and Google. The seeded `agent` was an ACTIVE cash agent with the platform's default
caps, so anyone holding the notes could work out `...+agent@...`, sign in with the same
password and mint wallet money for any real client (a top-up creates money and books a
receivable the fake agent never settles). And a rotation of the password (`set_password`)
left every session taken with the old one working.

S-236. `ensure_default_plans` found a default plan by its English name only. After an
operator renamed or deleted one in the console, the next `bootstrap_platform` (which the
review notes tell the owner to run before each submission) made a second ACTIVE plan at
the placeholder price, brought a deleted one back on sale, or died on the unique Arabic
name and never ran its later steps. The diet templates had the same lookup.
"""
from __future__ import annotations

import io
import json
from decimal import Decimal

import pytest
from django.core.management import call_command


def _bootstrap(*args):
    out = io.StringIO()
    call_command("bootstrap_platform", *args, stdout=out)
    return out.getvalue()


def _seed(*args):
    out = io.StringIO()
    call_command("seed_review_accounts", *args, stdout=out)
    return out.getvalue()


def _post(client, url, body, **extra):
    return client.post(url, data=json.dumps(body), content_type="application/json",
                       **extra)


@pytest.fixture
def storage_roots(settings, tmp_path):
    settings.MEDIA_ROOT = str(tmp_path / "media")
    settings.PRIVATE_MEDIA_ROOT = str(tmp_path / "private")
    return tmp_path


@pytest.fixture
def review_env(monkeypatch):
    """The two variables, with a password made up at run time: none is written here."""
    import secrets

    password = f"Rv-{secrets.token_urlsafe(18)}!7"
    monkeypatch.setenv("REVIEW_ACCOUNTS_EMAIL", "store-review@yalla-review.test")
    monkeypatch.setenv("REVIEW_ACCOUNTS_PASSWORD", password)
    return password


# ============================================================================ S-235
def test_s235_the_review_agent_cannot_sign_in_or_top_up_and_a_rotation_ends_old_sessions(
        db, seeded_catalogue, review_env, storage_roots, settings, monkeypatch,
        django_capture_on_commit_callbacks, make_user, api):
    from django.test import Client

    from users.models import CustomUser
    from wallet.models import AgentProfile, Wallet

    settings.COACHING_COMMISSION_RATE = Decimal("0.20")
    _bootstrap("--skip-food-catalogue")
    with django_capture_on_commit_callbacks(execute=True):
        _seed()
    agent = CustomUser.objects.get(username="review_agent")
    client = CustomUser.objects.get(username="review_client")
    assert agent.email == "store-review+agent@yalla-review.test"

    # 1. The address follows from the ones in the notes, and the password is theirs:
    #    neither sign-in route lets anyone in as the agent.
    anonymous = Client()
    assert not agent.has_usable_password()
    for url in ("/api/auth/token/", "/api/auth/login/"):
        resp = _post(anonymous, url, {"email": agent.email, "password": review_env})
        assert resp.status_code != 200, (url, resp.content)
        assert "access" not in resp.json()
    # The reviewers' own accounts still sign in with it.
    signed_in = _post(anonymous, "/api/auth/token/",
                      {"email": client.email, "password": review_env})
    assert signed_in.status_code == 200, signed_in.content
    old_refresh = signed_in.json()["refresh"]

    # 2. A session held from before (a JWT) tops nobody up: the profile is suspended
    #    once the seed's own top-ups are made.
    profile = AgentProfile.objects.get(user=agent)
    assert profile.status == "suspended"
    victim = make_user("s235_real_client")
    resp = _post(api(agent), "/api/wallet/agent/topup/proxy/",
                 {"client_identifier": str(victim.id), "amount": "50000.00",
                  "currency": "SYP", "idempotency_key": "s235-topup-1"})
    assert (resp.status_code, resp.json().get("code")) == (403, "agent_not_active")
    assert not Wallet.objects.filter(owner=victim, balance__gt=0).exists()

    # 3. Re-applied on every run: an operator who switched the agent back on, or gave it
    #    a password, finds both undone by the next run.
    AgentProfile.objects.filter(pk=profile.pk).update(status="active")
    agent.set_password(review_env)
    agent.save(update_fields=["password"])

    # 4. And a rotation of the password ends the sessions taken with the old one.
    monkeypatch.setenv("REVIEW_ACCOUNTS_PASSWORD", review_env + "-rotated")
    with django_capture_on_commit_callbacks(execute=True):
        out = _seed()
    agent.refresh_from_db()
    profile.refresh_from_db()
    assert not agent.has_usable_password(), out
    assert profile.status == "suspended", out
    refreshed = _post(anonymous, "/api/auth/token/refresh/", {"refresh": old_refresh})
    assert refreshed.status_code == 401, refreshed.content


# ============================================================================ S-236
def _empty_the_catalogue_tables():
    """What an empty production database holds of the plans and diet templates:
    nothing. Deleted inside the test's transaction."""
    from diet.models import DietPlanTemplate
    from subscription.models import SubscriptionPlan

    SubscriptionPlan.objects.all().delete()
    DietPlanTemplate.objects.all().delete()


def _plans():
    from subscription.models import SubscriptionPlan

    return sorted(((p.name_en, p.name_ar or "", p.price, p.is_active)
                   for p in SubscriptionPlan.objects.all()), key=repr)


@pytest.mark.django_db
def test_s236_a_plan_renamed_in_english_only_neither_stops_the_bootstrap_nor_is_written_again():
    """(a) The console edits the translated `name`; in an English console that is only
    `name_en`. The next run died on the unique Arabic name at step 3."""
    from achievements.models import Achievement
    from subscription.models import SubscriptionPlan

    _empty_the_catalogue_tables()
    _bootstrap("--skip-food-catalogue")
    premium = SubscriptionPlan.objects.get(name_en="Premium Plan")
    premium.name_en = "Gold"
    premium.save()
    before = _plans()
    Achievement.objects.all().delete()          # a later step, to see that it still runs

    out = _bootstrap("--skip-food-catalogue")

    assert "bootstrap complete" in out, out
    assert _plans() == before, out
    assert not SubscriptionPlan.objects.filter(name_en="Premium Plan").exists()
    assert Achievement.objects.exists(), "the steps after the plans never ran"


@pytest.mark.django_db
def test_s236_a_renamed_and_repriced_plan_is_not_put_back_on_sale_at_the_placeholder_price():
    """(b) Renamed in both languages and repriced: the next run put a second ACTIVE
    'Premium Plan' on sale at 2,500 beside the operator's 'Gold' at 9,000."""
    from subscription.models import SubscriptionPlan

    _empty_the_catalogue_tables()
    _bootstrap("--skip-food-catalogue")
    premium = SubscriptionPlan.objects.get(name_en="Premium Plan")
    premium.name_en, premium.name_ar, premium.price = "Gold", "الذهبية", Decimal("9000.00")
    premium.save()
    before = _plans()
    active = SubscriptionPlan.objects.filter(is_active=True).count()

    _bootstrap("--skip-food-catalogue")
    call_command("setup_subscription_plans", stdout=io.StringIO())

    assert _plans() == before
    assert SubscriptionPlan.objects.filter(is_active=True).count() == active == 3
    assert not SubscriptionPlan.objects.filter(price=Decimal("2500.00")).exists()


@pytest.mark.django_db
def test_s236_a_deleted_plan_or_template_does_not_come_back():
    """(c) An unwanted default deleted in the console came back ACTIVE on the next
    run; a diet template renamed or deleted came back as a second copy."""
    from diet.models import DietPlanTemplate
    from subscription.models import SubscriptionPlan

    _empty_the_catalogue_tables()
    _bootstrap("--skip-food-catalogue")
    SubscriptionPlan.objects.filter(name_en="Professional Plan").delete()
    DietPlanTemplate.objects.filter(name_en="4 Meals Only").delete()
    renamed = DietPlanTemplate.objects.get(name_en="3 Meals Only")
    renamed.name_en = "Three meals"
    renamed.save()
    templates = DietPlanTemplate.objects.count()

    out = _bootstrap("--skip-food-catalogue")

    assert not SubscriptionPlan.objects.filter(name_en="Professional Plan").exists()
    assert SubscriptionPlan.objects.count() == 3
    assert DietPlanTemplate.objects.count() == templates == 5
    assert not DietPlanTemplate.objects.filter(name_en__in=["4 Meals Only",
                                                            "3 Meals Only"]).exists()
    assert "NOT re-created" in out, out


@pytest.mark.django_db
def test_s236_one_plan_that_cannot_be_written_is_reported_and_the_rest_still_run():
    """A per-plan failure (its missing Arabic is already another plan's name) is a line
    of the report, not the end of the bootstrap."""
    from achievements.models import Achievement
    from subscription.models import SubscriptionPlan

    _empty_the_catalogue_tables()
    _bootstrap("--skip-food-catalogue")
    SubscriptionPlan.objects.filter(name_en="Premium Plan").update(name_ar=None)
    SubscriptionPlan.objects.filter(name_en="Professional Plan").update(
        name_ar="الخطة المميزة")
    Achievement.objects.all().delete()

    out = _bootstrap("--skip-food-catalogue")

    assert "failed" in out and "Premium Plan" in out, out
    assert Achievement.objects.exists(), "the steps after the plans never ran"
    assert SubscriptionPlan.objects.get(name_en="Premium Plan").name_ar is None


def test_s236_the_review_seed_finds_the_premium_plan_under_the_operators_name(
        db, seeded_catalogue, review_env, make_user):
    """seed_review_accounts refused ('no plan Premium Plan', go run bootstrap_platform)
    once the operator had renamed it, and the bootstrap could not help."""
    from subscription.models import Subscription, SubscriptionPlan
    from users.management.commands.seed_review_accounts import Command

    _bootstrap("--skip-food-catalogue")
    premium = SubscriptionPlan.objects.get(name_en="Premium Plan")
    premium.name_en, premium.name_ar = "Gold", "الذهبية"
    premium.save()

    command = Command()
    command._check_prerequisites()              # refused before
    command.users, command.log = {"client": make_user("s236_review_client")}, []
    command._premium()
    assert Subscription.objects.get(user=command.users["client"]).plan_id == premium.pk
