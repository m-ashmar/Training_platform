"""The ONE fixture module every walk-state test uses (`manage.py seed_walk_states`,
`users/walk/`). Import the fixtures into your area's test file::

    from tests.walk_fixtures import walk_platform, walk_run  # noqa: F401

    def test_client_waiting_for_coach(walk_run, api):
        entry = walk_run("client_waiting_for_coach")      # its manifest entry
        ...                                                 # read it through the API

**`walk_platform`** gives THIS test's database what a dev server has after
`manage.py bootstrap_platform`, by running that command itself, so it can never drift:

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
* `WALK_ACCOUNTS_PASSWORD` set for the command.

It answers the password, for a test that signs in with it.

**`other_currency_plan`** (on top of `walk_platform`) makes this database priced the way
the app builders' development database is (`yalla_gym/app/tool/seed_local_backend.py`):
a paid plan with the diet in the platform's OTHER currency, cheaper BY NUMBER than every
plan in the base one (12.00 USD beside 1,000 SYP and up), on sale beside them, and NO
exchange rate set by an operator. Every state must still run: a state that compared
prices across currencies, or priced a plan in a currency its client does not trade in,
fails here as it failed on that database. Answers the plan.

**`walk_run(name, ...)`** resets states through the command itself
(`seed_walk_states --only <name> --manifest <tmp>`), with the after-commit work executed
as it is outside a test (counts, notices, recounts), and answers the state's manifest
entry (or, for several names, the whole manifest). The manifest stays in `tmp_path`.
"""
import io
import json

import pytest

WALK_PASSWORD = "Walk-Throughs-2026!"


@pytest.fixture
def walk_platform(db, seeded_catalogue, settings, tmp_path, monkeypatch):
    from django.core.management import call_command

    settings.MEDIA_ROOT = str(tmp_path / "media")
    settings.PRIVATE_MEDIA_ROOT = str(tmp_path / "private_media")
    monkeypatch.setenv("WALK_ACCOUNTS_PASSWORD", WALK_PASSWORD)
    call_command("bootstrap_platform", "--skip-food-catalogue", stdout=io.StringIO())
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
    from wallet.currencies import CURRENCY_CODES
    from wallet.models import ExchangeRate

    base = settings.PLATFORM_CURRENCY
    other = next(code for code in CURRENCY_CODES if code != base)
    plan = SubscriptionPlan.objects.create(
        name=f"Pro ({other})", plan_type="pro", description="خطة تغذية وتدريب لمدة شهر.",
        price=Decimal("12.00"), currency=other, duration_days=30, is_active=True,
        has_diet_access=True, has_routine_access=True, has_challenges_access=True,
        has_ai_advice=True)
    on_sale = SubscriptionPlan.objects.filter(is_active=True, price__gt=0)
    assert on_sale.filter(currency=base, has_diet_access=True).exists()
    assert not on_sale.filter(currency=base, price__lte=plan.price).exists()
    assert not ExchangeRate.objects.exists(), "the development database has no rate"
    return plan
