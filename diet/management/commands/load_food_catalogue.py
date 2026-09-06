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

from diet.data.catalogue import LEVANTINE, USDA, WESTERN
from diet.models import FoodCategory, FoodItem, Recipe, RecipeIngredient

CACHE = Path(__file__).resolve().parents[2] / "data" / "usda_cache.json"


def _slug(name: str) -> str:
    return "".join(c if c.isalnum() else "-" for c in name.lower()).strip("-")


class Command(BaseCommand):
    help = "Rebuild FoodItem from diet/data/catalogue.py plus the USDA cache."

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true")
        parser.add_argument("--wipe", action="store_true",
                            help="Delete the existing catalogue first. Destructive.")
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

        with transaction.atomic():
            if opts["wipe"]:
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
            for label in {r["category"] for r in rows}:
                categories[label] = FoodCategory.objects.filter(name=label).first() \
                    or FoodCategory.objects.create(
                        name=label,
                        is_protein=label == "Proteins",
                        is_carb=label in ("Carbs", "Fruits", "Vegetables"),
                        is_fat=label == "Fats")

            created = updated = 0
            for row in rows:
                obj, was_new = FoodItem.objects.update_or_create(
                    name=row["name"],
                    defaults=dict(
                        api_id=row["api_id"],
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
