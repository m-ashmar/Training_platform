"""Launch data: `bootstrap_platform` and `seed_review_accounts`.

The production-readiness audit (2026-09-27) found that the deploy's seed step ran only
`load_food_catalogue`, so a new database opened with no plans, no diet templates, no
achievements and no shared exercises; and that the demo accounts the store reviewers
need (spec section 10, section 9 B8) had no command at all — only the app's dev script,
which hard-codes a shared password.

`bootstrap_platform` must bring an EMPTY database to launch and change nothing on a
second run; `seed_review_accounts` must build the reviewers' accounts through the
platform's own services, from addresses and a password it is GIVEN, refuse without
them, and change nothing on a second run.
"""
from __future__ import annotations

import io
import re
from pathlib import Path

import pytest
from django.core.management import call_command
from django.utils import translation

ROOT = Path(__file__).resolve().parents[1]
ARABIC = re.compile(r"[؀-ۿ]")


# ------------------------------------------------------------------------- helpers
def _bootstrap(*args):
    out = io.StringIO()
    call_command("bootstrap_platform", *args, stdout=out)
    return out.getvalue()


def _empty_the_launch_tables():
    """What an empty production database holds of everything the bootstrap writes:
    nothing. Deleted inside the test's transaction, so it is all back afterwards."""
    from achievements.models import Achievement
    from diet.models import (DietPlanTemplate, FoodItem, MealComponent, MealSwap, Recipe,
                             RecipeIngredient)
    from policy.models import ChannelCapabilities
    from routine.models import Exercise
    from social.models import Achievement as SocialAchievement
    from subscription.models import SubscriptionFeature, SubscriptionPlan

    for model in (ChannelCapabilities, SubscriptionFeature, SubscriptionPlan,
                  DietPlanTemplate, Achievement, SocialAchievement, Exercise,
                  MealSwap, MealComponent, RecipeIngredient, Recipe, FoodItem):
        model.objects.all().delete()


def _rows(model, **filters):
    """Every column of every row but the timestamps, in a stable order."""
    rows = []
    for row in model.objects.filter(**filters).order_by("pk").values():
        rows.append({k: v for k, v in row.items()
                     if not k.endswith(("_at", "updated", "created"))})
    return rows


def _snapshot():
    from achievements.models import Achievement
    from diet.models import DietPlanTemplate, FoodItem, Recipe, RecipeIngredient
    from policy.models import CapabilityChangeLog, ChannelCapabilities
    from routine.models import Exercise
    from social.models import Achievement as SocialAchievement
    from subscription.models import SubscriptionFeature, SubscriptionPlan

    return {
        "capabilities": _rows(ChannelCapabilities),
        "capability_log": CapabilityChangeLog.objects.count(),
        "features": _rows(SubscriptionFeature),
        "plans": _rows(SubscriptionPlan),
        "templates": _rows(DietPlanTemplate),
        "achievements": _rows(Achievement),
        "social_achievements": _rows(SocialAchievement),
        "exercises": _rows(Exercise),
        "foods": _rows(FoodItem),
        "recipes": _rows(Recipe),
        "recipe_lines": RecipeIngredient.objects.count(),
    }


# ============================================================== bootstrap_platform
@pytest.mark.django_db
def test_bootstrap_brings_an_empty_database_to_launch_and_a_second_run_writes_nothing():
    from achievements.models import Achievement
    from achievements.registry import ACHIEVEMENTS
    from diet.models import DietPlanTemplate, FoodItem, Recipe
    from policy.defaults import DEFAULT_FLAGS
    from policy.models import FLAG_FIELDS, ChannelCapabilities
    from routine.data.exercise_catalogue import STARTER_EXERCISES
    from routine.models import Exercise
    from social.management.commands.create_achievements import Command as Social
    from social.models import Achievement as SocialAchievement
    from subscription.models import SubscriptionFeature, SubscriptionPlan
    from subscription.quota import FEATURES

    _empty_the_launch_tables()
    first = _bootstrap()

    # The spec's table, one base row per channel.
    for channel, flags in DEFAULT_FLAGS.items():
        row = ChannelCapabilities.objects.get(channel=channel, min_version="0.0.0")
        assert {name: getattr(row, name) for name in FLAG_FIELDS} == flags, channel
    # Every quota the permission layer counts has its row.
    assert set(SubscriptionFeature.objects.values_list("name", flat=True)) == set(FEATURES)
    # Four plans, each named in both languages.
    plans = {p.name_en: p for p in SubscriptionPlan.objects.all()}
    assert set(plans) == {"Free Plan", "Basic Plan", "Premium Plan", "Professional Plan"}
    for plan in plans.values():
        assert ARABIC.search(plan.name_ar) and ARABIC.search(plan.description_ar), plan.name_en
    assert DietPlanTemplate.objects.count() == 6
    assert all(ARABIC.search(t.name_ar) for t in DietPlanTemplate.objects.all())
    assert Achievement.objects.count() == len(ACHIEVEMENTS)
    assert SocialAchievement.objects.count() == len(Social().get_achievements_data())
    # The starter exercises: global, both languages, each marked with its key.
    exercises = Exercise.objects.all()
    assert exercises.count() == len(STARTER_EXERCISES)
    assert {e.catalogue_key for e in exercises} == {e.key for e in STARTER_EXERCISES}
    assert all(e.is_global and e.created_by_id is None and e.is_active for e in exercises)
    by_key = {e.catalogue_key: e for e in exercises}
    for entry in STARTER_EXERCISES:
        row = by_key[entry.key]
        assert (row.name_en, row.name_ar, row.target_muscle, row.equipment,
                row.difficulty_level) == (entry.name_en, entry.name_ar,
                                          entry.target_muscle, entry.equipment,
                                          entry.difficulty), entry.key
    # The food catalogue and the recipe library it seeds.
    assert FoodItem.objects.count() > 100 and Recipe.objects.filter(is_active=True).count() > 50
    assert "created" in first

    snapshot = _snapshot()
    second = _bootstrap()
    assert _snapshot() == snapshot, "the second run changed something"
    assert "nothing was missing" in second, second
    assert set(_created_column(second).values()) == {"0"}, second
    assert all(int(n) > 0 for n in _created_column(first).values()), first


def _created_column(out):
    """{step: created} from the command's table."""
    lines = out.splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith("step"))
    table = {}
    for line in lines[start + 1:]:
        cells = re.split(r"\s{2,}", line.strip())
        if len(cells) >= 3 and cells[1].isdigit():
            table[cells[0]] = cells[1]
    return table


@pytest.mark.django_db
def test_bootstrap_under_an_arabic_locale_writes_the_rows_an_english_run_then_finds():
    """Several of these models have translated names, and a lookup on a translated
    field follows the ACTIVE language: run under Arabic, a naive get_or_create finds
    nothing English and writes a second set. The bootstrap runs in the default
    language whatever the caller's locale."""
    from routine.models import Exercise
    from subscription.models import SubscriptionPlan

    _empty_the_launch_tables()
    with translation.override("ar"):
        _bootstrap("--skip-food-catalogue")
        call_command("setup_subscription_plans", stdout=io.StringIO())
        call_command("setup_diet_templates", stdout=io.StringIO())
    snapshot = _snapshot()
    assert "nothing was missing" in _bootstrap("--skip-food-catalogue")
    assert _snapshot() == snapshot
    assert SubscriptionPlan.objects.filter(name_en="Premium Plan").count() == 1
    assert Exercise.objects.filter(name_en="Deadlift").count() == 1


@pytest.mark.django_db
def test_bootstrap_leaves_what_an_operator_changed_alone():
    from decimal import Decimal

    from achievements.models import Achievement
    from policy.models import ChannelCapabilities
    from routine.models import Exercise
    from subscription.models import SubscriptionPlan

    _empty_the_launch_tables()
    _bootstrap("--skip-food-catalogue")

    premium = SubscriptionPlan.objects.get(name_en="Premium Plan")
    premium.price = Decimal("99000.00")
    premium.name_ar = "بريميوم"
    premium.save()
    deadlift = Exercise.objects.get(catalogue_key="deadlift")
    deadlift.name_en, deadlift.is_active = "Conventional Deadlift", False
    deadlift.save()
    apk = ChannelCapabilities.objects.get(channel="apk", min_version="0.0.0")
    apk.can_show_web_hub_link = False
    apk.save()
    badge = Achievement.objects.order_by("id").first()
    badge.points = 999
    badge.save()

    assert "nothing was missing" in _bootstrap("--skip-food-catalogue")
    premium.refresh_from_db()
    deadlift.refresh_from_db()
    apk.refresh_from_db()
    badge.refresh_from_db()
    assert (premium.price, premium.name_ar) == (Decimal("99000.00"), "بريميوم")
    assert (deadlift.name_en, deadlift.is_active) == ("Conventional Deadlift", False)
    assert Exercise.objects.filter(name_en__in=["Deadlift", "Conventional Deadlift"]).count() == 1
    assert apk.can_show_web_hub_link is False
    assert badge.points == 999


@pytest.mark.django_db
def test_bootstrap_adopts_an_exercise_library_that_is_already_there():
    """A database that already holds a shared library (the development one had 554
    exercises) gets no duplicate of a same-named exercise: the existing row takes the
    key, and nothing else about it changes."""
    from routine.models import Exercise

    _empty_the_launch_tables()
    with translation.override("en"):
        mine = Exercise(name="Deadlift", name_en="Deadlift", name_ar="ديدلفت",
                        target_muscle="Hamstrings", difficulty_level="expert")
        mine.save()
    out = _bootstrap("--skip-food-catalogue")
    mine.refresh_from_db()
    assert mine.catalogue_key == "deadlift"
    assert (mine.name_ar, mine.target_muscle, mine.difficulty_level) == (
        "ديدلفت", "Hamstrings", "expert")
    assert Exercise.objects.filter(name_en="Deadlift").count() == 1
    assert "adopted" in out


@pytest.mark.django_db
def test_a_free_plan_is_created_switched_off_and_the_paid_ones_on():
    """Subscribing to a price-0 plan leaves the subscription pending for ever (the Pay
    button answers `payment_not_required` and nothing activates it), so a new database
    must not offer one until something grants it. `setup_subscription_plans` shares the
    catalogue and the rule."""
    from subscription.models import SubscriptionPlan

    _empty_the_launch_tables()
    call_command("setup_subscription_plans", stdout=io.StringIO())
    active = dict(SubscriptionPlan.objects.values_list("name_en", "is_active"))
    assert active == {"Free Plan": False, "Basic Plan": True, "Premium Plan": True,
                      "Professional Plan": True}


def test_the_starter_catalogue_is_a_usable_mix():
    """About sixty exercises, compound and isolation, for the gym and for home, every
    one named in both languages and using only the keys the model stores."""
    from routine.data.exercise_catalogue import STARTER_EXERCISES
    from routine.models import EQUIPMENT_CHOICES, Exercise

    muscles = {key for key, _label in Exercise._meta.get_field("target_muscle").choices}
    levels = {key for key, _label in Exercise._meta.get_field("difficulty_level").choices}
    equipment = {code for code, _label in EQUIPMENT_CHOICES}

    assert 55 <= len(STARTER_EXERCISES) <= 70
    assert len({e.key for e in STARTER_EXERCISES}) == len(STARTER_EXERCISES)
    assert len({e.name_en.lower() for e in STARTER_EXERCISES}) == len(STARTER_EXERCISES)
    for e in STARTER_EXERCISES:
        assert re.fullmatch(r"[a-z0-9]+(-[a-z0-9]+)*", e.key) and len(e.key) <= 64, e.key
        assert e.target_muscle in muscles and e.equipment in equipment, e.key
        assert e.difficulty in levels and e.kind in ("compound", "isolation"), e.key
        assert ARABIC.search(e.name_ar) and e.name_en.isascii(), e.key
    assert {e.kind for e in STARTER_EXERCISES} == {"compound", "isolation"}
    assert {e.home for e in STARTER_EXERCISES} == {True, False}
    assert {e.equipment for e in STARTER_EXERCISES} == equipment
    home = [e for e in STARTER_EXERCISES if e.home]
    assert all(e.equipment in ("bodyweight", "dumbbell") for e in home)
    # Every region of the body a program is built from.
    regions = {"Middle Chest", "Lats", "Front Deltoid", "Biceps", "Triceps",
               "Front Quads", "Hamstrings", "Glutes", "Calves", "Abdominals"}
    assert regions <= {e.target_muscle for e in STARTER_EXERCISES}


def test_the_bootstrap_seeds_the_quota_features_migration_0008_seeds():
    import importlib

    from subscription.quota import FEATURES
    from users.management.commands.bootstrap_platform import QUOTA_FEATURE_DESCRIPTIONS

    migration = importlib.import_module(
        "subscription.migrations.0008_quota_periods_and_features")
    assert migration.FEATURES == QUOTA_FEATURE_DESCRIPTIONS
    assert set(QUOTA_FEATURE_DESCRIPTIONS) == set(FEATURES)
