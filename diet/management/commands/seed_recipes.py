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
        ("Broccoli", 120, True), ("Olive Oil", 8, False)]),
    ("Chicken Freekeh Bowl", ["Lunch", "Dinner"], "Levantine", 35, [
        ("Chicken Breast", 140, True), ("White Rice", 150, True),
        ("Spinach", 80, True), ("Olive Oil", 10, False)]),
    ("Baked Salmon with Sweet Potato", ["Dinner"], "Mediterranean", 30, [
        ("Salmon", 150, True), ("Sweet Potato", 200, True),
        ("Spinach", 100, True), ("Olive Oil", 7, False)]),
    ("Lentil Soup with Greens", ["Lunch", "Dinner"], "Levantine", 40, [
        ("Lentils", 180, True), ("Spinach", 80, True), ("Olive Oil", 8, False)]),
    ("Overnight Oats with Banana", ["Breakfast"], "Everyday", 5, [
        ("Oats", 70, True), ("Greek Yogurt", 150, True), ("Banana", 100, True)]),
    ("Egg White Scramble with Avocado", ["Breakfast"], "Everyday", 12, [
        ("Egg White", 200, True), ("Avocado", 60, True), ("Olive Oil", 5, False)]),
    ("Yogurt, Apple and Almond Butter", ["Breakfast", "Snack"], "Everyday", 5, [
        ("Greek Yogurt", 170, True), ("Apple", 120, True), ("Almond Butter", 15, False)]),
    ("Chicken and Rice Meal Prep", ["Lunch"], "Everyday", 30, [
        ("Chicken Breast", 170, True), ("White Rice", 200, True),
        ("Broccoli", 150, True), ("Olive Oil", 8, False)]),
    ("Banana Almond Snack", ["Snack"], "Everyday", 2, [
        ("Banana", 110, True), ("Almond Butter", 20, False)]),
    ("Greek Yogurt Protein Bowl", ["Snack", "Breakfast"], "Everyday", 3, [
        ("Greek Yogurt", 200, True), ("Oats", 30, True), ("Apple", 80, True)]),
    ("Salmon Rice Bowl", ["Lunch"], "Mediterranean", 25, [
        ("Salmon", 130, True), ("White Rice", 170, True),
        ("Broccoli", 100, True), ("Olive Oil", 6, False)]),
    ("Lentils and Rice (Mujadara style)", ["Lunch", "Dinner"], "Levantine", 45, [
        ("Lentils", 150, True), ("White Rice", 120, True), ("Olive Oil", 10, False)]),
    # Higher-fat breakfasts. The first library skewed low-fat, so a 700 kcal breakfast
    # target with ~17 g fat had nothing that fit and fell back to component assembly.
    ("Avocado Toast with Eggs", ["Breakfast"], "Everyday", 12, [
        ("Egg White", 150, True), ("Oats", 60, True),
        ("Avocado", 90, True), ("Olive Oil", 6, False)]),
    ("Almond Butter Oat Bowl", ["Breakfast"], "Everyday", 6, [
        ("Oats", 80, True), ("Greek Yogurt", 120, True),
        ("Almond Butter", 25, True), ("Banana", 80, True)]),
    ("Salmon and Avocado Breakfast", ["Breakfast"], "Mediterranean", 15, [
        ("Salmon", 110, True), ("Avocado", 70, True),
        ("Spinach", 60, True), ("Olive Oil", 5, False)]),
    ("Chicken Avocado Rice Bowl", ["Lunch", "Dinner"], "Everyday", 25, [
        ("Chicken Breast", 140, True), ("White Rice", 160, True),
        ("Avocado", 70, True), ("Broccoli", 100, True)]),
]


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

        created = skipped = 0
        for name, meals, cuisine, minutes, lines in RECIPES:
            foods = {}
            missing = []
            for food_name, grams, scalable in lines:
                food = (FoodItem.objects.filter(name__iexact=food_name).first()
                        or FoodItem.objects.filter(name__icontains=food_name).first())
                if food is None:
                    missing.append(food_name)
                else:
                    foods[food_name] = food
            if missing:
                skipped += 1
                self.stdout.write(f"  skip {name!r}: catalogue has no {missing}")
                continue

            recipe, was_new = Recipe.objects.get_or_create(
                name=name,
                defaults=dict(meal_types=meals, cuisine=cuisine, prep_minutes=minutes,
                              description=f"{cuisine} · about {minutes} minutes"),
            )
            if not was_new:
                skipped += 1
                continue
            for food_name, grams, scalable in lines:
                RecipeIngredient.objects.create(
                    recipe=recipe, food=foods[food_name], grams=grams, scalable=scalable)
            created += 1

        self.stdout.write(self.style.SUCCESS(
            f"seeded {created} recipe(s); {skipped} skipped"))
