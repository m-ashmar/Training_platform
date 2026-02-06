from __future__ import annotations

from typing import Dict, List

from ..models import DietPlan, Meal, MealComponent, FoodItem
from ..utils.logging_utils import get_logger
from ..utils.nutrition import get_macro_ratios, dominant_macro_of_food


class PerMealFatCapper:
    """
    Enforce a per-meal fat cap BEFORE day-level passes.

    Logic:
    - Derive daily fat target from user goal ratios.
    - Allocate a target fat share per meal using a goal-based meal kcal distribution
      (Gain: 40/40/20, Lose: 30/40/30, Maintain: 35/35/30).
    - For each meal, if its computed fat grams exceed the target share,
      scale down ALL components that contribute fat (fat_per_gram > 0) proportionally
      so that meal fat equals the cap.
    """

    def __init__(self) -> None:
        self.logger = get_logger(__name__)

    def enforce(self, diet_plan: DietPlan) -> None:
        daily_kcal = float(diet_plan.daily_calories or 0.0)
        if daily_kcal <= 0.0:
            return

        goal = (diet_plan.goal or "Maintain").lower()
        ratios = get_macro_ratios(goal)
        fat_target_g = daily_kcal * ratios["fat"] / 9.0

        dates = sorted({m.date for m in diet_plan.meals.all()})
        for d in dates:
            meals_for_day = list(Meal.objects.filter(diet_plan=diet_plan, date=d).prefetch_related("components", "components__food"))
            if not meals_for_day:
                continue

            distribution = self._choose_distribution_for_goal(goal, [m.meal_type for m in meals_for_day])

            for meal in meals_for_day:
                # First: enforce hard per-item oil cap at 20g wherever mentioned
                oil_capped = 0
                for comp in meal.components.all():
                    if self._is_oil(comp.food):
                        if float(comp.quantity or 0.0) > 20.0:
                            comp.quantity = 20.0
                            comp.save(update_fields=["quantity"])
                            oil_capped += 1

                # Compute current meal fat using robust calculation
                current_fat = 0.0
                try:
                    current_fat = meal.calculate_nutrition().get("fat", 0.0)
                except Exception:
                    for comp in meal.components.all():
                        current_fat += float(getattr(comp.food, "fat_per_gram", 0.0) or 0.0) * float(comp.quantity or 0.0)

                # Per-meal fat ceiling: min(share-based cap, absolute 25g)
                share = distribution.get(meal.meal_type, 1.0 / max(1, len(meals_for_day)))
                share_cap = fat_target_g * share
                hard_cap_g = min(20.0, share_cap)
                if current_fat > hard_cap_g:
                    scale = hard_cap_g / max(current_fat, 1e-6)
                    scale = max(0.0, min(1.0, scale))

                    changed = False
                    for comp in meal.components.all():
                        fat_pg = float(getattr(comp.food, "fat_per_gram", 0.0) or 0.0)
                        if fat_pg <= 0.0 and dominant_macro_of_food(comp.food) != 'fat':
                            continue
                        new_qty = float(comp.quantity or 0.0) * scale
                        comp.quantity = new_qty
                        comp.save(update_fields=["quantity"])
                        changed = True

                    if changed:
                        try:
                            self.logger.info(
                                "per_meal_fat_cap_applied",
                                extra={
                                    "diet_plan_id": diet_plan.id,
                                    "date": d.isoformat(),
                                    "meal": getattr(meal, "meal_type", None),
                                    "fat_before_g": round(current_fat, 2),
                                    "fat_cap_g": round(hard_cap_g, 2),
                                    "scale": round(scale, 4),
                                    "oil_items_capped": oil_capped,
                                },
                            )
                        except Exception:
                            pass

    # ------------------------ helpers ------------------------
    def _choose_distribution_for_goal(self, goal: str, meal_types: List[str]) -> Dict[str, float]:
        # Normalize common meals
        normalized: List[str] = []
        for m in meal_types:
            mv = (m or "").title()
            normalized.append(mv if mv in ("Breakfast", "Lunch", "Dinner") else mv)

        if len(normalized) == 3 and set(normalized) == {"Breakfast", "Lunch", "Dinner"}:
            if "gain" in (goal or "").lower():
                pattern = [0.40, 0.40, 0.20]
            elif "lose" in (goal or "").lower():
                pattern = [0.30, 0.40, 0.30]
            else:
                pattern = [0.35, 0.35, 0.30]
            return {"Breakfast": pattern[0], "Lunch": pattern[1], "Dinner": pattern[2]}

        # Fallback: equal share across available meals for the day
        n = max(1, len(meal_types))
        equal = 1.0 / n
        return {m: equal for m in meal_types}

    def _is_oil(self, food: FoodItem) -> bool:
        try:
            name = (getattr(food, 'name', '') or '').strip().lower()
        except Exception:
            name = ''
        return 'oil' in name


