"""Assert the invariants the diet engine depends on. Non-zero exit when one is broken.

The engine's failures are quiet. An uncurated allergen column does not raise at startup —
it raises the first time a client with a declared allergy taps Generate, as
`NoServableMealError`, which reads as "the catalogue is too small" and sends whoever is on
call looking for missing food. A food with no household unit is planned in whatever the
fallback ladder offers. A recipe whose ingredient did not resolve is simply absent from the
library, and `seed_recipes` prints that and exits 0.

So this is the check a deploy runs after seeding, and what it prints is the shape the
catalogue has to be in before anyone can be served.

    python manage.py check_catalogue          # report, exit 1 on any failure
    python manage.py check_catalogue --quiet  # exit code only
"""
from django.core.management.base import BaseCommand

from diet.models import FoodCategory, FoodItem, Recipe, RecipeIngredient


class Command(BaseCommand):
    help = "Verify the food catalogue is fit to plan from. Exits non-zero if it is not."

    def add_arguments(self, parser):
        parser.add_argument("--quiet", action="store_true",
                            help="print nothing; the exit code is the answer")

    def handle(self, *args, **opts):
        servable = FoodItem.objects.filter(needs_review=False)
        total = servable.count()
        recipes = Recipe.objects.filter(is_active=True).count()

        checks = [
            ("the catalogue holds food at all", total, total > 0,
             "run `load_food_catalogue --apply --wipe`"),
            ("every food has a verified allergen composition",
             servable.filter(allergen_source="verified").count(),
             not servable.exclude(allergen_source="verified").exists(),
             "run `curate_food_allergens --apply`; without it a client who declares an "
             "allergy can be served NOTHING, because the planner refuses what it cannot clear"),
            ("every food belongs to a category",
             servable.filter(category__isnull=False).count(),
             not servable.filter(category__isnull=True).exists(),
             "`FoodCategory.by_canonical_name` returned None for a label; re-run the load"),
            ("every food declares a household unit",
             servable.exclude(household_unit="").count(),
             not servable.filter(household_unit="").exists(),
             "run `seed_food_units --apply`"),
            ("every food declares at least one meal slot",
             servable.exclude(meal_slots=[]).count(),
             not servable.filter(meal_slots=[]).exists(),
             "a food with no slot is invisible to the similarity index"),
            ("the recipe library is present", recipes, recipes > 0,
             "run `seed_recipes`; on an empty catalogue it seeds nothing and still exits 0"),
            ("every recipe has ingredients",
             RecipeIngredient.objects.count(),
             not Recipe.objects.filter(is_active=True, ingredients__isnull=True).exists(),
             "a recipe with no lines can never be served"),
            ("no food carries a name the API cannot render",
             servable.exclude(name_en__isnull=True).exclude(name_en="").count(),
             not servable.filter(name_en__isnull=True).exists()
             and not servable.filter(name_en="").exists(),
             "run `update_translation_fields`"),
            ("category names are readable in English",
             FoodCategory.objects.exclude(name_en__isnull=True).exclude(name_en="").count(),
             not FoodCategory.objects.filter(name_en__isnull=True).exists(),
             "a category with no English name is invisible to `by_canonical_name`"),
        ]

        # A meal slot stored one character at a time — `list("Snack")` — is a non-empty
        # list that names no real slot, which zeroes every slot feature in the similarity
        # index instead of leaving it neutral.
        slots = {"Breakfast", "Lunch", "Dinner", "Snack"}
        malformed = [f.name_en for f in servable.exclude(meal_slots=[])
                     if not (set(f.meal_slots or []) & slots)]
        checks.append(("every meal slot is a real slot name", total - len(malformed),
                       not malformed,
                       f"these declare slots that are not slots: {malformed[:6]}"))

        failed = [c for c in checks if not c[2]]
        if not opts["quiet"]:
            self.stdout.write(f"catalogue: {total} food(s), {recipes} recipe(s)\n")
            for name, measured, ok, remedy in checks:
                mark = self.style.SUCCESS("  ok  ") if ok else self.style.ERROR(" FAIL ")
                self.stdout.write(f"{mark} {name} ({measured})")
                if not ok:
                    self.stdout.write(self.style.WARNING(f"        {remedy}"))
            self.stdout.write("")
            if failed:
                self.stdout.write(self.style.ERROR(
                    f"{len(failed)} invariant(s) broken — the engine is not fit to plan from"))
            else:
                self.stdout.write(self.style.SUCCESS("catalogue is fit to plan from"))

        if failed:
            raise SystemExit(1)
