"""One shape for a meal on the wire.

Five views and one service each built a meal dictionary by hand, and none of them
carried what the engine had decided: the meal's name, the reason it was chosen, the
dish it is, or the target it was built to. The client read "Levantine · 25 minutes"
where the engine had said "Mujadara with Salad (Quinoa instead of Rice)". Every meal
payload is assembled here, so a field the engine adds reaches every endpoint at once,
and the target beside a meal is the one the planner built it to rather than a second
arithmetic that disagreed with the first.
"""
from __future__ import annotations

from typing import Dict, Iterable, Optional

from django.utils import translation

from diet.planner.portion import describe


def current_lang() -> str:
    return (translation.get_language() or "en")[:2]


def day_targets(diet_plan, meals_of_day: Iterable) -> Dict[str, object]:
    """Per-slot targets for one day, from the split the planner built to.

    One `plan_targets` call for the day, not one per meal.
    """
    from diet.planner.targets import plan_targets

    meals_of_day = list(meals_of_day)
    slots = [s for s in ("Breakfast", "Lunch", "Dinner")
             if any(m.meal_type == s for m in meals_of_day)] or ["Lunch"]
    snacks = sum(1 for m in meals_of_day if m.meal_type == "Snack")
    return {t.name: t for t in plan_targets(diet_plan, slots, snacks).meals}


def target_payload(by_slot: Dict[str, object], meal) -> Dict[str, float]:
    """The target the meal was built to when the planner recorded one; the plan's
    split for meals that predate the column or were written by a trainer."""
    built = getattr(meal, "target", None)
    if isinstance(built, dict) and built.get("calories"):
        return {"calories": round(float(built.get("calories", 0)), 1),
                "protein": round(float(built.get("protein", 0)), 1),
                "carbs": round(float(built.get("carb", built.get("carbs", 0)) or 0), 1),
                "fat": round(float(built.get("fat", 0)), 1)}
    t = by_slot.get(meal.meal_type) or by_slot.get("Snack") or next(iter(by_slot.values()))
    return {"calories": t.calories, "protein": t.protein, "carbs": t.carb, "fat": t.fat}


def _composed_name(meal, lang: str) -> str:
    names = []
    for c in list(meal.components.all())[:4]:
        f = c.food
        names.append((getattr(f, "name_ar", None) or f.name) if lang == "ar" else f.name)
    if not names:
        return ""
    if len(names) == 1:
        return names[0]
    if lang == "ar":
        return "، ".join(names[:-1]) + " و" + names[-1]
    return ", ".join(names[:-1]) + " and " + names[-1]


def component_payload(component, lang: Optional[str] = None) -> dict:
    lang = lang or current_lang()
    food = component.food
    grams = float(component.quantity or 0)
    name_ar = getattr(food, "name_ar", None) or food.name
    return {
        "id": component.id,
        "food_id": food.id,
        "name": food.name,
        "name_ar": name_ar,
        "display_name": name_ar if lang == "ar" else food.name,
        "quantity_grams": grams,
        # What a person would say they are eating, beside the grams. Derived on read so
        # it cannot drift from the quantity it describes.
        "serving": describe(food, grams),
        "serving_ar": describe(food, grams, "ar"),
        "image_url": food.image_url,
        "nutrition": component.calculate_nutrition(),
        "is_completed": component.is_completed,
        "completed_at": component.completed_at,
        "actual_quantity_consumed": component.actual_quantity_consumed,
        "is_liked": component.is_liked,
    }


def meal_payload(meal, lang: Optional[str] = None, targets: Optional[dict] = None,
                 with_components: bool = False) -> dict:
    lang = lang or current_lang()
    description = meal.description
    if meal.translations and lang in meal.translations:
        description = meal.translations[lang].get("description", description)
    # A meal without a stored name (written by a trainer, or before the column
    # existed) is named from what is on it, in both languages, rather than "".
    name = meal.name or _composed_name(meal, "en")
    name_ar = meal.name_ar or _composed_name(meal, "ar") or name
    reason = meal.reason or ""
    reason_ar = meal.reason_ar or reason
    out = {
        "id": meal.id,
        "meal_type": meal.meal_type,
        "date": meal.date,
        "scheduled_time": meal.scheduled_time,
        "name": name,
        "name_ar": name_ar,
        "display_name": name_ar if lang == "ar" else name,
        "description": description,
        "reason": reason,
        "reason_ar": reason_ar,
        "display_reason": reason_ar if lang == "ar" else reason,
        "shape": meal.shape or "",
        "recipe_id": meal.recipe_id,
        "is_dish": meal.recipe_id is not None,
        "image_url": meal.image_url,
        "is_ai_generated": meal.is_ai_generated,
        "is_completed": meal.is_completed,
        "completion_percentage": meal.completion_percentage,
        "is_liked": meal.is_liked,
        "nutrition": meal.calculate_nutrition(),
    }
    if targets is not None:
        out["targets"] = targets
    if with_components:
        out["components"] = [component_payload(c, lang) for c in meal.components.all()]
    return out
