"""The ONE fixture module every walk-state test uses (`manage.py seed_walk_states`,
`users/walk/`). Import the fixtures into your area's test file::

    from tests.walk_fixtures import walk_platform, walk_run  # noqa: F401

    def test_client_waiting_for_coach(walk_run, api):
        entry = walk_run("client_waiting_for_coach")      # its manifest entry
        ...                                                 # read it through the API

**`walk_platform`** gives THIS test's database what a developer's database has: what
`manage.py bootstrap_platform` writes, by running that command itself, so it can never
drift, AND what the app builders' own seed puts beside it
(`yalla_gym/app/tool/seed_local_backend.py`), so a state that only works on a clean
bootstrap fails here and not on their database (the first real run, 2026-11-24, stopped
at a state that took "the cheapest plan" and got a dollar one):

* the channel capabilities, the quota features, the subscription plans (a paid plan on
  sale), the diet plan templates, the badges (achievements and social achievements), the
  starter exercises and the platform's templates (the free first workouts and the
  starter templates) — `bootstrap_platform --skip-food-catalogue`, inside the test's own
  transaction, so every row is rolled back with the test (about a second);
* the food catalogue (foods, recipes, household units) — the session's
  `seeded_catalogue`, built once per session and re-seeded after a truncation, so a
  diet plan can be generated;
* `MEDIA_ROOT` and `PRIVATE_MEDIA_ROOT` in the test's own `tmp_path`, so an upload a state
  makes through the real route (a trainer's verification documents, a picture) never
  lands in the project's real media folders;
* `WALK_ACCOUNTS_PASSWORD` set for the command;
* the developers' database (`DEVELOPERS_PLANS`, `_the_developers_database`): their three
  plans by name — two in pounds and `Pro (USD)` at 12.00 dollars, the cheapest by its
  NUMBER — and **no exchange rate** (so nothing may price one currency in the other);
  and ordinary accounts at `@dev.test` with their own money: a trainer and the client
  they accepted (month 1 held), a client on the dollar plan in a dollar wallet, a new
  client. None of them is a walk account, so the command must not touch them.

It answers the password, for a test that signs in with it.

**`other_currency_plan`** (on top of `walk_platform`, which already holds it) answers the
developers' plan in the platform's OTHER currency (`Pro (USD)`, 12.00), and checks what
makes it the trap it was on their database: on sale with the diet, cheaper BY NUMBER
than every plan in the base currency (1,000 SYP and up), and NO exchange rate set by an
operator. Every state must still run: a state that compared prices across currencies,
or priced a plan in a currency its client does not trade in, fails here as it failed
there. (The money worker's fixture; one plan since walk_platform made the same one.)

**`walk_run(name, ...)`** resets states through the command itself
(`seed_walk_states --only <name> --manifest <tmp>`), with the after-commit work executed
as it is outside a test (counts, notices, recounts), and answers the state's manifest
entry (or, for several names, the whole manifest). The manifest stays in `tmp_path`.
"""
import io
import json

import pytest

WALK_PASSWORD = "Walk-Throughs-2026!"


#: The plans `yalla_gym/app/tool/seed_local_backend.py` makes beside bootstrap's, as it
#: makes them: (name, plan_type, price, currency, has_ai_advice, is_featured).
DEVELOPERS_PLANS = (
    ("أساسي", "basic", "50000", "SYP", False, False),
    ("مميز", "premium", "120000", "SYP", True, True),
    ("Pro (USD)", "pro", "12.00", "USD", True, False),
)

#: The ordinary accounts' domain: not `yalla.test`, so never a walk account.
DEVELOPERS_DOMAIN = "dev.test"


def _the_developers_database(api):
    """What the app builders' dev database holds beside bootstrap: their plans in both
    currencies, no exchange rate, and ordinary accounts with their own plans and
    coachings, made through the routes where there is one."""
    from decimal import Decimal

    from subscription.models import Subscription, SubscriptionPlan
    from users.models import CustomUser, TrainerClientRelation
    from wallet.models import ExchangeRate, move_funds_atomic, set_currency_for, wallet_for

    for name, kind, price, currency, ai, featured in DEVELOPERS_PLANS:
        SubscriptionPlan.objects.get_or_create(name=name, defaults={
            "plan_type": kind, "description": "خطة تغذية وتدريب لمدة شهر.",
            "price": Decimal(price), "currency": currency, "duration_days": 30,
            "is_active": True, "is_featured": featured, "has_diet_access": True,
            "has_routine_access": True, "has_challenges_access": True, "has_ai_advice": ai})
    assert not ExchangeRate.objects.exists(), "the developers' database has no rate"

    def person(tag, role="client", **fields):
        user = CustomUser.objects.create_user(
            email=f"{tag}@{DEVELOPERS_DOMAIN}", username=f"dev_{tag}",
            password=WALK_PASSWORD, user_type=role)
        for name, value in fields.items():
            setattr(user, name, value)
        user.save()
        return user

    # A trainer and the client they accepted: month 1 of the fee is held.
    trainer = person("rami", "trainer", first_name="رامي", last_name="المدرب",
                     trainer_hourly_rate=Decimal("10000.00"), trainer_is_available=True)
    coached = person("samer", first_name="سامر", last_name="الحمصي")
    move_funds_atomic(None, wallet_for(coached, "client"), Decimal("50000"),
                      tx_type="topup", metadata={"seed": "walk_fixtures"},
                      money_type="WALLET_MOVE")
    asked = api(coached).post("/api/auth/client/request-trainer/",
                              {"trainer_id": trainer.pk}, content_type="application/json")
    assert asked.status_code == 200, asked.content
    relation = TrainerClientRelation.objects.get(trainer=trainer, client=coached)
    accepted = api(trainer).post("/api/auth/trainer/respond-to-request/",
                                 {"request_id": relation.pk, "action": "approve"},
                                 content_type="application/json")
    assert accepted.status_code == 200, accepted.content
    # A client who trades in dollars, on the dollar plan.
    dollars = person("lina", first_name="لينا")
    set_currency_for(dollars, "USD", "client")
    Subscription.objects.create(user=dollars, plan=SubscriptionPlan.objects.get(
        name="Pro (USD)"), status="active")
    # A client with nothing yet.
    person("new")


@pytest.fixture
def walk_platform(db, seeded_catalogue, settings, tmp_path, monkeypatch, api):
    from django.core.management import call_command

    settings.MEDIA_ROOT = str(tmp_path / "media")
    settings.PRIVATE_MEDIA_ROOT = str(tmp_path / "private_media")
    monkeypatch.setenv("WALK_ACCOUNTS_PASSWORD", WALK_PASSWORD)
    call_command("bootstrap_platform", "--skip-food-catalogue", stdout=io.StringIO())
    _the_developers_database(api)
    return WALK_PASSWORD


@pytest.fixture
def walk_run(walk_platform, tmp_path, django_capture_on_commit_callbacks):
    from django.core.management import call_command

    manifest = tmp_path / "walk_states.json"

    def run(*names, extra=()):
        args = []
        for name in names:
            args += ["--only", name]
        with django_capture_on_commit_callbacks(execute=True):
            call_command("seed_walk_states", *args, "--manifest", str(manifest), *extra,
                         stdout=io.StringIO())
        data = json.loads(manifest.read_text(encoding="utf-8"))
        return data["states"][names[0]] if len(names) == 1 else data

    run.manifest = manifest
    return run


@pytest.fixture
def other_currency_plan(walk_platform):
    from decimal import Decimal

    from django.conf import settings

    from subscription.models import SubscriptionPlan
    from wallet.models import ExchangeRate

    base = settings.PLATFORM_CURRENCY
    plan = SubscriptionPlan.objects.get(name="Pro (USD)")
    assert plan.currency != base and plan.price == Decimal("12.00")
    on_sale = SubscriptionPlan.objects.filter(is_active=True, price__gt=0)
    assert on_sale.filter(pk=plan.pk, has_diet_access=True).exists()
    assert on_sale.filter(currency=base, has_diet_access=True).exists()
    assert not on_sale.filter(currency=base, price__lte=plan.price).exists()
    assert not ExchangeRate.objects.exists(), "the development database has no rate"
    return plan
