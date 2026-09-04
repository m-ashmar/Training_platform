"""Seed a starter recipe library.

Deliberately Levantine/Mediterranean-leaning: the app serves a Syrian market, and a
meal plan is only followed if the food is food the user recognises. Every dish maps onto
FoodItems already in the catalogue, matched by name, and is skipped when its ingredients
are missing rather than inventing them.

    python manage.py seed_recipes
    python manage.py seed_recipes --replace
"""
from django.core.management.base import BaseCommand
from django.db import transaction

# name, meal types, cuisine, prep minutes, [(food name, grams, scalable)]
RECIPES = [
    ("Grilled Chicken with Rice and Greens", ["Lunch", "Dinner"], "Levantine", 25, [
        ("Chicken Breast", 150, True), ("White Rice", 180, True),
        ("Broccoli", 120, True), ("Olive Oil", 8, True)]),
    ("Chicken and Rice with Spinach", ["Lunch", "Dinner"], "Levantine", 35, [
        ("Chicken Breast", 140, True), ("White Rice", 150, True),
        ("Spinach", 80, True), ("Olive Oil", 10, True)]),
    ("Baked Salmon with Sweet Potato", ["Dinner"], "Mediterranean", 30, [
        ("Salmon", 150, True), ("Sweet Potato", 200, True),
        ("Spinach", 100, True), ("Olive Oil", 7, True)]),
    ("Lentil Soup with Greens", ["Lunch", "Dinner"], "Levantine", 40, [
        ("Lentils", 180, True), ("Spinach", 80, True), ("Olive Oil", 8, True)]),
    ("Overnight Oats with Banana", ["Breakfast"], "Everyday", 5, [
        ("Oats", 70, True), ("Greek Yogurt", 150, True), ("Banana", 100, True)]),
    ("Egg White Scramble with Avocado", ["Breakfast"], "Everyday", 12, [
        ("Egg White", 200, True), ("Avocado", 60, True), ("Olive Oil", 5, True)]),
    ("Yogurt, Apple and Almond Butter", ["Breakfast", "Snack"], "Everyday", 5, [
        ("Greek Yogurt", 170, True), ("Apple", 120, True), ("Almond Butter", 15, True)]),
    ("Chicken and Rice Meal Prep", ["Lunch"], "Everyday", 30, [
        ("Chicken Breast", 170, True), ("White Rice", 200, True),
        ("Broccoli", 150, True), ("Olive Oil", 8, True)]),
    ("Banana Almond Snack", ["Snack"], "Everyday", 2, [
        ("Banana", 110, True), ("Almond Butter", 20, True)]),
    ("Greek Yogurt Protein Bowl", ["Snack", "Breakfast"], "Everyday", 3, [
        ("Greek Yogurt", 200, True), ("Oats", 30, True), ("Apple", 80, True)]),
    ("Salmon Rice Bowl", ["Lunch"], "Mediterranean", 25, [
        ("Salmon", 130, True), ("White Rice", 170, True),
        ("Broccoli", 100, True), ("Olive Oil", 6, True)]),
    ("Lentils and Rice (Mujadara style)", ["Lunch", "Dinner"], "Levantine", 45, [
        ("Lentils", 150, True), ("White Rice", 120, True), ("Olive Oil", 10, True)]),
    # Higher-fat breakfasts. The first library skewed low-fat, so a 700 kcal breakfast
    # target with ~17 g fat had nothing that fit and fell back to component assembly.
    ("Avocado Toast with Eggs", ["Breakfast"], "Everyday", 12, [
        ("Egg White", 150, True), ("Whole Wheat Bread", 60, True),
        ("Avocado", 90, True), ("Olive Oil", 6, True)]),
    ("Almond Butter Oat Bowl", ["Breakfast"], "Everyday", 6, [
        ("Oats", 80, True), ("Greek Yogurt", 120, True),
        ("Almond Butter", 25, True), ("Banana", 80, True)]),
    ("Salmon and Avocado Breakfast", ["Breakfast"], "Mediterranean", 15, [
        ("Salmon", 110, True), ("Avocado", 70, True),
        ("Spinach", 60, True), ("Olive Oil", 5, True)]),
    ("Chicken Avocado Rice Bowl", ["Lunch", "Dinner"], "Everyday", 25, [
        ("Chicken Breast", 140, True), ("White Rice", 160, True),
        ("Avocado", 70, True), ("Broccoli", 100, True)]),
]


#: Recipes whose name no longer describes what is in them. A dish called after an
#: ingredient it does not contain is the fastest way to lose a user's trust in the whole
#: feature, and two of these sixteen were: this one carried white rice and no freekeh,
#: because the catalogue holds none, and "Avocado Toast with Eggs" carried oats and no
#: bread while the catalogue held six breads. Renaming here rather than in the table
#: alone, so a database that already ran the seed converges too.
RENAMED = {"Chicken Freekeh Bowl": "Chicken and Rice with Spinach"}


class Command(BaseCommand):
    help = "Seed a starter recipe library mapped onto the existing food catalogue."

    def add_arguments(self, parser):
        parser.add_argument("--replace", action="store_true",
                            help="delete existing seeded recipes first")

    @transaction.atomic
    def handle(self, *args, **opts):
        from diet.models import FoodItem, Recipe, RecipeIngredient

        if opts["replace"]:
            deleted = Recipe.objects.filter(name__in=[r[0] for r in RECIPES]).delete()[0]
            self.stdout.write(f"removed {deleted} existing rows")

        created = updated = skipped = 0
        for was, now in RENAMED.items():
            Recipe.objects.filter(name=was).exclude(
                name__in=Recipe.objects.filter(name=now).values("name")).update(name=now)
        Recipe.objects.filter(name__in=RENAMED).delete()
        for name, meals, cuisine, minutes, lines in RECIPES:
            foods = {}
            missing = []
            for food_name, grams, scalable in lines:
                # Exact first. Failing that, the *shortest* containing name, because
                # `.first()` on an unordered icontains picks arbitrarily: asking for
                # "Olive Oil" against a catalogue holding both "Extra Virgin Olive Oil"
                # and "Olive Oil Spray" would take whichever the database returned
                # first, and the macros of the two are not the same food.
                food = FoodItem.objects.filter(name__iexact=food_name).first()
                if food is None:
                    candidates = sorted(
                        FoodItem.objects.filter(name__icontains=food_name),
                        key=lambda f: (len(f.name), f.name),
                    )
                    food = candidates[0] if candidates else None
                    if food is not None:
                        self.stdout.write(
                            f"  {name!r}: {food_name!r} -> {food.name!r}")
                if food is None:
                    missing.append(food_name)
                else:
                    foods[food_name] = food
            if missing:
                skipped += 1
                self.stdout.write(f"  skip {name!r}: catalogue has no {missing}")
                continue

            # Reconcile, do not skip. `get_or_create` plus `continue` meant this file
            # stopped being the source of truth the moment a database had run it once:
            # correcting a recipe's ingredients, its meal types or its scalable flags
            # here changed nothing anywhere the seed had already been applied. That is
            # how twelve recipes kept a pinned fat line long after it was known to be
            # the reason no dish could reach its fat target.
            recipe, was_new = Recipe.objects.get_or_create(
                name=name,
                defaults=dict(meal_types=meals, cuisine=cuisine, prep_minutes=minutes,
                              description=f"{cuisine} · about {minutes} minutes"),
            )
            if not was_new:
                recipe.meal_types = meals
                recipe.cuisine = cuisine
                recipe.prep_minutes = minutes
                recipe.save(update_fields=["meal_types", "cuisine", "prep_minutes"])
                updated += 1
            else:
                created += 1

            wanted = {foods[food_name].id: (grams, scalable)
                      for food_name, grams, scalable in lines}
            recipe.ingredients.exclude(food_id__in=wanted).delete()
            for line in recipe.ingredients.all():
                grams, scalable = wanted.pop(line.food_id)
                if float(line.grams or 0) != float(grams) or bool(line.scalable) != scalable:
                    line.grams, line.scalable = grams, scalable
                    line.save(update_fields=["grams", "scalable"])
            for food_id, (grams, scalable) in wanted.items():
                RecipeIngredient.objects.create(
                    recipe=recipe, food_id=food_id, grams=grams, scalable=scalable)

        self.stdout.write(self.style.SUCCESS(
            f"seeded {created} recipe(s); reconciled {updated}"))
