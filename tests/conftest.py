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


@pytest.fixture(scope="session")
def seeded_catalogue(django_db_setup, django_db_blocker):
    """The catalogue and recipe library, built by the project's own seed commands.

    The quality harness measures what the engine puts on a plate, so it needs food to
    put there, and the test database starts empty. Seeding through `add_healthy_foods`
    and `seed_recipes` rather than a hand-written fixture means the gate measures what
    a fresh production database will actually contain — which matters, because the
    development database is going to be dropped before launch and every number taken
    from it goes with it.
    """
    from django.core.management import call_command

    with django_db_blocker.unblock():
        from diet.models import FoodItem, Recipe

        # Both commands are idempotent — update_or_create and get_or_create — so run
        # them every time rather than guarding on a row count. The guard meant a food
        # added to the seed never appeared in a reused test database, which is how a
        # missing staple stayed invisible across several runs.
        call_command("add_healthy_foods", verbosity=0)
        call_command("seed_recipes", verbosity=0)
        return {
            "foods": FoodItem.objects.filter(needs_review=False).count(),
            "recipes": Recipe.objects.filter(is_active=True).count(),
        }


@pytest.fixture(scope="session")
def diet_quality(seeded_catalogue, django_db_blocker):
    """One quality measurement, shared across every assertion that reads it.

    Generating 168 meals takes about six seconds, so it runs once per session. Measured
    against the seeded catalogue rather than the development database, so the numbers
    describe what a fresh install does and survive that database being dropped.
    """
    from tests.diet_quality import measure, PROFILES

    with django_db_blocker.unblock():
        return measure(PROFILES, days=7)
