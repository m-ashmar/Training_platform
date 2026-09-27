"""Bring an EMPTY database to a launchable state; do nothing to one that already is.

    python manage.py bootstrap_platform
    python manage.py bootstrap_platform --skip-food-catalogue

Run after `migrate` on a new database, and safely again at any time: every step finds
what is there before it writes, and a second run prints `created 0` on every line. It
needs no environment variable of its own and holds no secret, so it is the same command
on a laptop, on staging and on production.

What it writes, in order (each step in its own transaction):

| Step | Source of truth | On a row that exists |
|---|---|---|
| channel capabilities | `policy/defaults.py` (the spec's table) | left alone |
| quota features | `subscription/quota.py` FEATURES (as migration 0008 seeds them) | left alone |
| subscription plans | `subscription/default_plans.py` | left alone; empty Arabic filled |
| diet plan templates | `setup_diet_templates` | left alone; empty Arabic filled |
| achievements | `achievements/registry.py` | left alone (`sync_achievements` updates) |
| social achievements | `create_achievements` | left alone |
| starter exercises | `routine/data/exercise_catalogue.py` | left alone |
| food catalogue | `load_food_catalogue --apply` (NEVER `--wipe`) | converged on the file |

Why each is here even though some are ALSO written by a migration: a restore that missed
rows, a database whose data migrations were faked, or a flush (the thread-based tests
truncate every table) leaves them missing, and the platform then refuses things it should
not — every quota check answers "feature row is missing" -> denied. Putting the rows back
is this command's job; it never creates a second copy of one.

The food catalogue is the one step that UPDATES existing rows: `load_food_catalogue`
converges the catalogue on `diet/data/catalogue.py` by design (its docstring), and that
is what a redeploy wants. `--wipe` is never passed — it would delete what clients have
eaten. `--skip-food-catalogue` leaves the catalogue to an operator who manages it by
hand.

Everything runs in the default language (`settings.LANGUAGE_CODE`): several of these
models have translated names, and a lookup on a translated field follows the ACTIVE
language — under Arabic it would miss every English row and write a second set.
"""
from __future__ import annotations

import io

from django.conf import settings
from django.core.management import call_command
from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import translation

#: What migration 0008 seeds for each quota feature (`subscription/quota.py` FEATURES).
#: Copied, not imported: a migration is history and is not imported at runtime.
#: `tests/test_bootstrap_commands.py` fails if the two ever differ.
QUOTA_FEATURE_DESCRIPTIONS = {
    "daily_meals": "Meals and diet plans a subscriber may generate per day.",
    "routines": "Routines a subscriber may hold in a paid period.",
}


class Command(BaseCommand):
    help = ("Write what an empty database needs to launch: channel capabilities, quota "
            "features, plans, diet templates, achievements, the starter exercise "
            "catalogue and the food catalogue. Idempotent; never deletes.")

    def add_arguments(self, parser):
        parser.add_argument(
            "--skip-food-catalogue", action="store_true",
            help="Do not run load_food_catalogue (the slowest step, and the only one "
                 "that updates rows that exist).")

    def handle(self, *args, **options):
        self.rows = []
        with translation.override(settings.LANGUAGE_CODE):
            self._step("channel capabilities", self.capabilities)
            self._step("quota features", self.quota_features)
            self._step("subscription plans", self.plans)
            self._step("diet plan templates", self.diet_templates)
            self._step("achievements", self.achievements)
            self._step("social achievements", self.social_achievements)
            self._step("starter exercises", self.starter_exercises)
            if options["skip_food_catalogue"]:
                self.rows.append(("food catalogue", "-", "-", "skipped"))
            else:
                self._step("food catalogue", self.food_catalogue)

        width = max(len(row[0]) for row in self.rows)
        self.stdout.write(f"{'step'.ljust(width)}  created    found  note")
        for name, created, found, note in self.rows:
            self.stdout.write(f"{name.ljust(width)}  {str(created):>7}  {str(found):>7}  {note}")
        total = sum(row[1] for row in self.rows if isinstance(row[1], int))
        self.stdout.write(self.style.SUCCESS(
            f"bootstrap complete: {total} row(s) created" if total
            else "bootstrap complete: nothing was missing"))

    def _step(self, name, fn):
        with transaction.atomic():
            created, found, note = fn()
        self.rows.append((name, created, found, note))

    # ----------------------------------------------------------------- the steps
    def capabilities(self):
        """One base row (`min_version` 0.0.0) per channel, with the spec's flags.

        Written through the model's `save()`, so the change log records the row as
        `created` by nobody — which is what happened. A channel that has its base row
        keeps it exactly as an operator left it.
        """
        from policy.defaults import DEFAULT_FLAGS
        from policy.models import ChannelCapabilities

        created = found = 0
        for channel, flags in DEFAULT_FLAGS.items():
            if ChannelCapabilities.objects.filter(channel=channel,
                                                  min_version="0.0.0").exists():
                found += 1
                continue
            ChannelCapabilities(channel=channel, min_version="0.0.0", **flags).save()
            created += 1
        return created, found, ""

    def quota_features(self):
        from subscription.models import SubscriptionFeature
        from subscription.quota import FEATURES

        created = found = 0
        for name in FEATURES:
            _row, was_created = SubscriptionFeature.objects.get_or_create(
                name=name, defaults={"description": QUOTA_FEATURE_DESCRIPTIONS[name],
                                     "is_active": True})
            created += was_created
            found += not was_created
        return created, found, ""

    def plans(self):
        from subscription.default_plans import ensure_default_plans

        outcomes = ensure_default_plans()
        created = [plan for plan, outcome in outcomes if outcome == "created"]
        filled = sum(1 for _plan, outcome in outcomes if outcome == "arabic added")
        notes = []
        if filled:
            notes.append(f"Arabic added to {filled}")
        inactive = [plan.name_en for plan in created if not plan.is_active]
        if inactive:
            notes.append(f"{', '.join(inactive)} created INACTIVE (free plans are never "
                         f"activated yet: subscription/default_plans.py)")
        if created:
            notes.append("prices are placeholders: set them in the console")
        return len(created), len(outcomes) - len(created), "; ".join(notes)

    def diet_templates(self):
        from diet.management.commands.setup_diet_templates import ARABIC
        from diet.models import DietPlanTemplate

        names = list(ARABIC)
        before = DietPlanTemplate.objects.filter(name_en__in=names).count()
        call_command("setup_diet_templates", stdout=io.StringIO())
        after = DietPlanTemplate.objects.filter(name_en__in=names).count()
        return after - before, before, ""

    def achievements(self):
        """Create the registry's achievements that do not exist; never update one.

        `sync_achievements` is the command that makes existing rows agree with the
        registry (it overwrites an operator's edit, by design). A bootstrap that ran it
        would do the same on every deploy, so this only fills the gaps, with the same
        columns (`AchievementDef.model_defaults`)."""
        from achievements.models import Achievement
        from achievements.registry import ACHIEVEMENTS

        existing = set(Achievement.objects.filter(
            key__in=[a.key for a in ACHIEVEMENTS]).values_list("key", flat=True))
        missing = [a for a in ACHIEVEMENTS if a.key not in existing]
        for definition in missing:
            Achievement.objects.create(key=definition.key, **definition.model_defaults())
        return len(missing), len(existing), ""

    def social_achievements(self):
        from social.management.commands.create_achievements import Command as Social
        from social.models import Achievement

        names = [row["name"] for row in Social().get_achievements_data()]
        before = Achievement.objects.filter(name_en__in=names).count()
        call_command("create_achievements", stdout=io.StringIO())
        after = Achievement.objects.filter(name_en__in=names).count()
        return after - before, before, ""

    def starter_exercises(self):
        """Each catalogue row, once, as a GLOBAL exercise (no owner).

        Found by `catalogue_key`. A global exercise with the same English name and no
        key — a library imported before this catalogue existed — is ADOPTED rather
        than duplicated: it gets the key and nothing else about it changes."""
        from routine.data.exercise_catalogue import STARTER_EXERCISES
        from routine.models import Exercise

        keyed = set(Exercise.objects.filter(
            catalogue_key__in=[e.key for e in STARTER_EXERCISES])
            .values_list("catalogue_key", flat=True))
        created = adopted = 0
        for entry in STARTER_EXERCISES:
            if entry.key in keyed:
                continue
            twin = (Exercise.objects.filter(created_by__isnull=True, catalogue_key__isnull=True,
                                            name_en__iexact=entry.name_en)
                    .order_by("id").first())
            if twin is not None:
                twin.catalogue_key = entry.key
                twin.save(update_fields=["catalogue_key", "updated_at"])
                adopted += 1
                continue
            Exercise(name=entry.name_en, name_en=entry.name_en, name_ar=entry.name_ar,
                     target_muscle=entry.target_muscle, equipment=entry.equipment,
                     difficulty_level=entry.difficulty, created_by=None,
                     is_active=True, catalogue_key=entry.key).save()
            created += 1
        note = f"{adopted} existing exercise(s) adopted by name" if adopted else ""
        return created, len(keyed) + adopted, note

    def food_catalogue(self):
        from diet.models import FoodItem, Recipe

        foods, recipes = FoodItem.objects.count(), Recipe.objects.count()
        out = io.StringIO()
        call_command("load_food_catalogue", "--apply", stdout=out)
        made_foods = FoodItem.objects.count() - foods
        made_recipes = Recipe.objects.count() - recipes
        note = f"{made_recipes} recipe(s) created; catalogue converged on the file"
        if "awaiting USDA" in out.getvalue():
            note += "; some foods await fetch_usda (see load_food_catalogue)"
        return made_foods, foods, note
