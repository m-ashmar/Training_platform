"""Shared fixtures for the regression gate.

The 53 scripts in tests/security/ are the audit's evidence — each one builds its own
database and is run by hand. This module gives the highest-signal checks a home in a
real pytest suite so CI can gate on them: one database for the whole session, seeded
once, asserted many times.
"""
import pytest
from django.utils import timezone


@pytest.fixture(scope="session")
def django_db_setup(django_db_setup, django_db_blocker):
    """Reuse one database for the whole session (see --reuse-db in pytest.ini)."""
    return django_db_setup


@pytest.fixture(scope="session", autouse=True)
def strict_planner():
    """The planner must RAISE in the gate, never fail open.

    `RuleBasedPlanner` logs and swallows a path's exception unless `DIET_PLANNER_STRICT`
    (default: `DEBUG`) is set, and pytest-django runs with `DEBUG = False`. That is how a
    crash inside the recipe path hid behind "no recipe fits" for two commits: every meal
    was silently template-built and the metrics simply moved. In production fail-open
    is the right call — a client gets dinner, an engineer gets an error log. In the gate
    it is a defect detector switched off.
    """
    from django.conf import settings
    settings.DIET_PLANNER_STRICT = True


@pytest.fixture(autouse=True)
def english_active():
    """`FoodItem.name` is a translated column, so `filter(name=...)` resolves against the
    ACTIVE language, and the test client leaves whatever an Arabic request activated.
    A chooser then resolved zero foods and the dislike test passed vacuously."""
    from django.utils import translation
    translation.activate("en")
    yield
    translation.deactivate()


@pytest.fixture(autouse=True)
def speaks_as_the_direct_apk(monkeypatch):
    """Every test client declares the direct-APK channel unless the request says otherwise.

    The server refuses to open a checkout or move wallet money for a caller that does
    not say which build it is (`policy/services.py`), and every real build always does.
    A test that is about another build passes `HTTP_X_APP_CHANNEL="ios"` on the request;
    one that is about a caller declaring nothing passes `HTTP_X_APP_CHANNEL=""`.
    """
    from django.test.client import RequestFactory

    original = RequestFactory.__init__

    def __init__(self, *args, **kwargs):
        original(self, *args, **kwargs)
        self.defaults.setdefault("HTTP_X_APP_CHANNEL", "apk")

    monkeypatch.setattr(RequestFactory, "__init__", __init__)


@pytest.fixture(autouse=True)
def coaching_commission_is_set_per_test(settings):
    """The platform's cut of coaching (owner, 2026-09-19: 20%) is 0 unless a test sets it.

    A deliberate contract change: every coaching payout now pays the platform its
    commission. The tests written before it pin exact trainer balances to prove a hold is
    paid ONCE — idempotency and lock order, not the split — and they keep proving that
    with the rate at 0. The split has its own tests (tests/test_coaching_months.py),
    which set the owner's rate explicitly.
    """
    from decimal import Decimal
    settings.COACHING_COMMISSION_RATE = Decimal("0")


@pytest.fixture
def make_user(db):
    from users.models import CustomUser

    def _make(username, user_type="client", **kw):
        u = CustomUser.objects.create_user(
            email=f"{username}@gate.test", username=username, password="Xx!23456"
        )
        u.user_type = user_type
        u.is_active = True
        if user_type == "admin":
            u.is_staff = u.is_superuser = True
        for k, v in kw.items():
            setattr(u, k, v)
        u.save()
        return u

    return _make


@pytest.fixture
def api(make_user):
    """An authenticated DRF client for a given user."""
    from django.test import Client
    from rest_framework_simplejwt.tokens import RefreshToken

    def _client(user=None):
        c = Client()
        if user is not None:
            c.defaults["HTTP_AUTHORIZATION"] = f"Bearer {RefreshToken.for_user(user).access_token}"
        return c

    return _client


@pytest.fixture(autouse=True)
def _reset_rate_limits():
    """Clear the rate-limit counters between tests.

    The anonymous route sweep touches every endpoint and trips the limiter, so without
    this the next test gets 429 instead of the status it is asserting — a test-isolation
    artefact that looks exactly like a product failure.
    """
    from training_platform.cache import ratelimit_cache

    try:
        ratelimit_cache().clear()
    except Exception:
        pass
    yield
    try:
        ratelimit_cache().clear()
    except Exception:
        pass


def _seed_catalogue(django_db_blocker):
    """Build the catalogue and recipe library from the project's own seed commands."""
    from django.core.management import call_command

    with django_db_blocker.unblock():
        from diet.models import FoodItem, Recipe

        call_command("load_food_catalogue", "--apply", "--wipe", "--force", verbosity=0)
        call_command("seed_recipes", verbosity=0)
        call_command("seed_food_units", "--apply", verbosity=0)
        return {
            "foods": FoodItem.objects.filter(needs_review=False).count(),
            "recipes": Recipe.objects.filter(is_active=True).count(),
        }


@pytest.fixture(scope="session")
def _catalogue_once(django_db_setup, django_db_blocker):
    """Seed once per session; see `_ensure_catalogue` for why that is not enough."""
    return _seed_catalogue(django_db_blocker)


def _ensure_migration_seed(django_db_blocker):
    """Put back the rows a MIGRATION created, if something took them away.

    `SubscriptionFeature` is written by subscription migration 0008 and read by every
    quota check, which answers "feature row is missing" -> denied when it is gone. A
    `transaction=True` test truncates it along with everything else, and with
    `--reuse-db` that damage outlives the run: the whole suite then failed on
    subscriptions, pointing nowhere near the test that caused it.

    Those tests live in `tests/test_zz_concurrency.py` now, which sorts last precisely so
    nothing follows them. This stays as the belt to that braces: it is one query, it runs
    for every database test, and it means a stray truncation from anywhere costs a
    re-create rather than a wall of unrelated failures.
    """
    with django_db_blocker.unblock():
        from subscription.models import SubscriptionFeature
        from subscription.quota import FEATURES

        missing = set(FEATURES) - set(
            SubscriptionFeature.objects.filter(name__in=FEATURES)
            .values_list("name", flat=True))
        for name in missing:
            SubscriptionFeature.objects.get_or_create(
                name=name, defaults={"description": name.replace("_", " ")})


@pytest.fixture(autouse=True)
def _migration_seed_is_present(request, django_db_blocker):
    """Put the migration-seeded rows back for EVERY database test.

    `_ensure_migration_seed` used to hang off `seeded_catalogue`, so a test that took
    only `make_user` — every quota test does — still found an empty
    `subscription_subscriptionfeature` after a transactional test had truncated it, and
    failed with "feature row is missing". The check is one query and it runs wherever a
    database is in play, which is the only place the damage can be observed.
    """
    wants_db = any(name in request.fixturenames
                   for name in ("db", "transactional_db", "django_db_setup"))
    if not (wants_db or request.node.get_closest_marker("django_db")):
        return
    _ensure_migration_seed(django_db_blocker)


def _ensure_catalogue(django_db_blocker, counts):
    """Return the catalogue counts, re-seeding first if the tables were emptied.

    Seeding once per session is not enough. A test marked `django_db(transaction=True)`
    truncates every table when it finishes, including the catalogue the session fixture
    committed, so whichever test ran next found an empty database and failed for reasons
    that had nothing to do with it. Checking costs one query and removes the whole class
    of order dependence, without weakening a single assertion.
    """
    _ensure_migration_seed(django_db_blocker)
    with django_db_blocker.unblock():
        from diet.models import FoodItem, Recipe

        if (FoodItem.objects.filter(needs_review=False).exists()
                and Recipe.objects.filter(is_active=True).exists()):
            return counts
    return _seed_catalogue(django_db_blocker)


@pytest.fixture
def seeded_catalogue(_catalogue_once, django_db_blocker):
    """The catalogue, guaranteed present for THIS test."""
    return _ensure_catalogue(django_db_blocker, _catalogue_once)


@pytest.fixture(scope="session")
def diet_quality(_catalogue_once, django_db_blocker):
    """One quality measurement, shared across every assertion that reads it.

    Generating 168 meals takes about six seconds, so it runs once per session. Measured
    against the seeded catalogue rather than the development database, so the numbers
    describe what a fresh install does and survive that database being dropped.
    """
    from tests.diet_quality import measure, PROFILES

    _ensure_catalogue(django_db_blocker, _catalogue_once)
    with django_db_blocker.unblock():
        return measure(PROFILES, days=7)
