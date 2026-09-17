"""Cycle 9/10 minor findings that need committed data across threads.

Sorts after test_regression_gate.py for the reason test_zz_concurrency.py explains.
"""
import threading
import uuid

import pytest


@pytest.mark.django_db(transaction=True)
def test_two_ingredients_ticked_together_both_count(make_user, monkeypatch):
    """Each tick recomputed the day from what was committed when it looked and wrote the
    whole row, so the recompute that read first could save last and the day showed one
    ingredient's calories with both marked eaten."""
    from django.db import connections
    from django.utils import timezone

    from diet.models import DailyProgress, DietPlan, FoodCategory, FoodItem, Meal, MealComponent

    user = make_user(f"tick_{uuid.uuid4().hex[:8]}")
    today = timezone.localdate()
    cat = FoodCategory.objects.create(name=f"tick-{uuid.uuid4().hex[:6]}")
    foods = [FoodItem.objects.create(
        name=n, name_en=n, category=cat, api_id=f"tick-{uuid.uuid4().hex[:8]}",
        calories=100, protein=10, carbs=10, fat=1, serving_size="100g",
        allergens=[], allergen_source="verified") for n in ("Tick A", "Tick B")]
    plan = DietPlan.objects.create(user=user, goal="Maintain", daily_calories=2000,
                                   start_date=today, end_date=today)
    meal = Meal.objects.create(diet_plan=plan, date=today, meal_type="Lunch",
                               scheduled_time="12:00")
    first, second = (MealComponent.objects.create(meal=meal, food=f, quantity=100.0)
                     for f in foods)
    DailyProgress.objects.create(user=user, diet_plan=plan, date=today)

    first_has_read = threading.Event()
    second_done = threading.Event()
    original_save = DailyProgress.save

    def slow_first_save(self, *args, **kwargs):
        # The first recompute has aggregated; hold its write until the second tick has
        # had every chance to finish (it cannot, if the row is locked).
        if threading.current_thread().name == "first":
            first_has_read.set()
            second_done.wait(timeout=2)
        return original_save(self, *args, **kwargs)

    monkeypatch.setattr(DailyProgress, "save", slow_first_save)

    def tick(component):
        try:
            MealComponent.objects.get(pk=component.pk).complete()
        finally:
            if threading.current_thread().name == "second":
                second_done.set()
            connections.close_all()

    a = threading.Thread(target=tick, args=(first,), name="first")
    a.start()
    assert first_has_read.wait(timeout=10)
    b = threading.Thread(target=tick, args=(second,), name="second")
    b.start()
    a.join(timeout=20)
    b.join(timeout=20)

    progress = DailyProgress.objects.get(user=user, diet_plan=plan, date=today)
    assert progress.calories_consumed == 200.0
    meal.refresh_from_db()
    assert meal.is_completed is True
    assert progress.is_day_completed is True
