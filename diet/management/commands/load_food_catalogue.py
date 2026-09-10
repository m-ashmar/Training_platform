"""Rebuild the food catalogue from the curated source of truth.

    python manage.py load_food_catalogue                 # report only
    python manage.py load_food_catalogue --apply         # add and update, keep the rest
    python manage.py load_food_catalogue --apply --wipe  # DESTRUCTIVE, see below

`--wipe` deletes every FoodItem, Recipe and RecipeIngredient, and everything that cascades
from them: meal components, per-user food categorisations, and the liked/disliked/choice
links on every UserFoodPreference. It is the right thing to do exactly once, before launch,
against a database whose contents are development data — which is what this one holds: 327
rows across six `api_id` prefixes, 44 of them created by the persistence layer itself, with
23 duplicated names.

The load is idempotent by canonical name, so running it again after adding rows to
`diet/data/catalogue.py` converges the database on the file rather than layering on top of
it. Units and roles are then assigned by `seed_food_units`, which derives both by pattern.
"""
from __future__ import annotations

import json
from pathlib import Path

from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from diet.data.catalogue import LEVANTINE, SLOTS, USDA, WESTERN
from diet.models import FoodCategory, FoodItem, MealComponent, Recipe, RecipeIngredient

CACHE = Path(__file__).resolve().parents[2] / "data" / "usda_cache.json"


def _slug(name: str) -> str:
    return "".join(c if c.isalnum() else "-" for c in name.lower()).strip("-")


class Command(BaseCommand):
    help = "Rebuild FoodItem from diet/data/catalogue.py plus the USDA cache."

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true")
        parser.add_argument("--wipe", action="store_true",
                            help="Delete the existing catalogue first. Destructive.")
        parser.add_argument("--force", action="store_true",
                            help="Wipe even though meals reference these foods. This "
                                 "erases what clients have eaten. Development only.")
        parser.add_argument("--skip-units", action="store_true",
                            help="Do not run seed_food_units afterwards.")

    def handle(self, *args, **opts):
        cache = json.loads(CACHE.read_text()) if CACHE.exists() else {}
        rows, missing = [], []

        for name, query, category, slots, name_ar in USDA:
            data = cache.get(name)
            if not data:
                missing.append(name)
                continue
            rows.append(dict(
                name=name, name_ar=name_ar, category=category, slots=slots,
                cuisine="western" if name in WESTERN else "universal",
                api_id=f"usda-{data['fdc_id']}",
                calories=data["calories"], protein=data["protein"],
                carbs=data["carbs"], fat=data["fat"],
                source=data.get("usda_description", ""),
            ))

        for name, name_ar, kcal, protein, carbs, fat, category, slots in LEVANTINE:
            rows.append(dict(
                name=name, name_ar=name_ar, category=category, slots=slots,
                cuisine="levantine",
                api_id=f"lev-{_slug(name)}",
                calories=kcal, protein=protein, carbs=carbs, fat=fat,
                source="curated (absent from USDA SR Legacy and Foundation)",
            ))

        # A row's slots must be a COLLECTION of real slot names. Passing the bare
        # constant instead of a tuple type-checks, reads correctly, and stores
        # `["S","n","a","c","k"]`.
        malformed = [r["name"] for r in rows
                     if isinstance(r["slots"], str) or not set(r["slots"]) <= SLOTS]
        if malformed:
            raise CommandError(
                "these rows declare meal slots that are not slots (a bare string instead "
                f"of a tuple?): {malformed}")

        self.stdout.write(f"catalogue file declares {len(USDA) + len(LEVANTINE)} food(s)")
        self.stdout.write(f"  resolvable now : {len(rows)}")
        if missing:
            self.stdout.write(self.style.WARNING(
                f"  awaiting USDA   : {len(missing)} (run fetch_usda --api-key ...)"))
            for name in missing[:8]:
                self.stdout.write(f"      {name}")
            if len(missing) > 8:
                self.stdout.write(f"      ... and {len(missing) - 8} more")

        if not opts["apply"]:
            self.stdout.write("dry run; pass --apply to write")
            return
        if missing and not opts["wipe"]:
            self.stdout.write(self.style.WARNING(
                "loading a partial catalogue; re-run after fetch_usda to complete it"))
        if opts["wipe"] and not rows:
            raise CommandError("refusing to wipe with nothing to load")

        # A food that has been EATEN cannot be deleted. `MealComponent.food` is PROTECT
        # now, so the wipe below would raise mid-transaction; refuse before starting,
        # and say what to do instead. This command destroyed every component in the
        # development database — 2,720 meals with nothing on them — and the release
        # plan runs it against production.
        if opts["wipe"]:
            # EVERYTHING a wipe destroys, not only the one FK that was changed to
            # PROTECT. The guard counted `MealComponent` alone, so on any database whose
            # meal history had been trimmed the command needed no `--force`, printed
            # nothing about it, and cascade-deleted four other kinds of client data:
            # `UserFoodWeight` (the planner's LEARNED per-client weights),
            # `UserFoodCategoryPreference`, and both sides of the liked/disliked M2M —
            # the entire signal the personalisation is built on.
            from diet.models import (MealComponent, MealSwap, UserFoodCategoryPreference,
                                     UserFoodPreference, UserFoodWeight)

            at_risk = {
                "meal component(s) — what clients have eaten": MealComponent.objects.count(),
                "meal swap(s)": MealSwap.objects.count(),
                "learned food weight(s) — the planner's personalisation":
                    UserFoodWeight.objects.count(),
                "food category preference(s)": UserFoodCategoryPreference.objects.count(),
                "liked food(s)": UserFoodPreference.liked_foods.through.objects.count(),
                "disliked food(s)": UserFoodPreference.disliked_foods.through.objects.count(),
            }
            losses = {label: n for label, n in at_risk.items() if n}
            if losses and not opts.get("force"):
                detail = "\n  ".join(f"{n} {label}" for label, n in losses.items())
                raise CommandError(
                    f"refusing to wipe: deleting these foods would also erase\n  {detail}\n"
                    f"Run without --wipe to converge the catalogue on the file instead, "
                    f"or pass --force if this database has no real client history."
                )
            if losses:
                self.stdout.write(self.style.WARNING(
                    "  --force: erasing " + ", ".join(
                        f"{n} {label}" for label, n in losses.items())))

        with transaction.atomic():
            if opts["wipe"]:
                # `MealComponent.food` and the two `MealSwap` foods are PROTECT, so a
                # plain delete raises. `--force` means "this database holds no real
                # client history", and the only honest way to honour it is to say
                # exactly what is being destroyed and then destroy it.
                MealSwap.objects.all().delete()
                MealComponent.objects.all().delete()

                # RecipeIngredient.food is PROTECT, so recipes go first.
                n_lines = RecipeIngredient.objects.count()
                n_recipes = Recipe.objects.count()
                RecipeIngredient.objects.all().delete()
                Recipe.objects.all().delete()
                n_foods = FoodItem.objects.count()
                FoodItem.objects.all().delete()
                self.stdout.write(self.style.WARNING(
                    f"  deleted {n_foods} food(s), {n_recipes} recipe(s), {n_lines} line(s)"))

            categories = {}
            # `sorted`, not set-iteration order: string hashing is randomised per
            # process, so the ids handed to the categories changed from run to run and
            # nothing that depends on them was reproducible.
            for label in sorted({r["category"] for r in rows}):
                categories[label], _created = FoodCategory.by_canonical_name(
                    label,
                    is_protein=label == "Proteins",
                    is_carb=label in ("Carbs", "Fruits", "Vegetables"),
                    is_fat=label == "Fats")
                if categories[label] is None:
                    # It used to be assigned anyway, and every food in that macro was
                    # stored with no category.
                    raise CommandError(f"could not resolve or create category {label!r}")

            created = updated = 0
            for row in rows:
                # Keyed on `api_id`, the only UNIQUE column on this table. `name` is not
                # unique — the admin's Edamam import dedups on `api_id` alone — so a
                # duplicate name raised `MultipleObjectsReturned` and rolled the whole
                # catalogue load back; and `name` is a translated virtual field, so the
                # lookup silently moved to `name_ar` under a non-English locale and
                # matched nothing, re-creating all 136 rows.
                obj, was_new = FoodItem.objects.update_or_create(
                    api_id=row["api_id"],
                    defaults=dict(
                        name=row["name"],
                        name_en=row["name"],
                        calories=row["calories"], protein=row["protein"],
                        carbs=row["carbs"], fat=row["fat"],
                        serving_size="100g", serving_size_grams=100,
                        category=categories[row["category"]],
                        meal_slots=list(row["slots"]),
                        cuisine=row["cuisine"],
                        needs_review=False,
                    ),
                )
                if hasattr(obj, "name_ar"):
                    obj.name_ar = row["name_ar"]
                    obj.save(update_fields=["name_ar"])
                created += was_new
                updated += not was_new

        self.stdout.write(self.style.SUCCESS(
            f"  created {created}, updated {updated}, total {FoodItem.objects.count()}"))

        if not opts["skip_units"]:
            self.stdout.write("assigning household units and roles")
            call_command("seed_food_units", "--apply", verbosity=0)
            unitless = FoodItem.objects.filter(household_unit="").count()
            self.stdout.write(self.style.SUCCESS(
                f"  {FoodItem.objects.count() - unitless}/{FoodItem.objects.count()} "
                f"food(s) have a household unit"))

        # The recipe library is the engine's culinary knowledge — every shape, pairing and
        # meal-appropriateness is derived from it — and `--wipe` deletes it. Rebuilding
        # the catalogue without it left a database with 133 foods and no dishes at all.
        # Seed it here so one command really does rebuild the whole thing.
        self.stdout.write("seeding the recipe library")
        call_command("seed_recipes", verbosity=0)
        self.stdout.write(self.style.SUCCESS(
            f"  {Recipe.objects.filter(is_active=True).count()} recipe(s) active"))

        # Allergen data is not on the FoodItem defaults above, so `--wipe` used to leave
        # every row at `allergen_source='unknown'` with `allergens=[]` — the state the
        # model's own help text calls out as "must NEVER be treated as safe". A rebuild
        # that restores the food but not what it is made of is not a rebuild.
        self.stdout.write("curating allergen composition")
        call_command("curate_food_allergens", "--apply", verbosity=0)
        unverified = FoodItem.objects.exclude(allergen_source="verified").count()
        self.stdout.write(self.style.SUCCESS(
            f"  {FoodItem.objects.count() - unverified}/{FoodItem.objects.count()} "
            f"food(s) have a verified composition"))
        if unverified:
            self.stdout.write(self.style.WARNING(
                f"  {unverified} row(s) are not verified and will not be served to a "
                f"client with a declared allergy — add them to diet/data/food_allergens.py"))
