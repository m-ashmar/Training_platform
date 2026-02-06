from __future__ import annotations

from typing import Dict

from ..models import DietPlan, MealComponent, FoodItem
from ..utils.nutrition import dominant_macro_of_food


class CalorieTrimmer:
    """
    Goal-based post-processing trimmer (Block 2):
    - If total daily kcal > target:
      - Gain: no change (bulking tolerates surplus)
      - Lose (shredding): reduce quantities of fat-dominant components by 3%, recheck;
        if still above target, reduce fat-dominant components by an additional 2%
      - Maintain: reduce all component quantities by 3% and recheck (single pass)
    """

    def trim(self, diet_plan: DietPlan) -> None:
        goal = (diet_plan.goal or 'Maintain')
        dates = sorted({m.date for m in diet_plan.meals.all()})
        for d in dates:
            totals = diet_plan.calculate_daily_nutrition(d)
            total_kcal = float(totals.get('calories', 0.0))
            target_kcal = float(diet_plan.daily_calories or 0.0)
            if total_kcal <= 0.0 or target_kcal <= 0.0:
                continue

            if goal == 'Gain':
                # Allow surplus in bulking
                if total_kcal <= target_kcal:
                    continue

            # BUG FIX: Optimize with select_related for food category
            components = MealComponent.objects.filter(meal__diet_plan=diet_plan, meal__date=d).select_related('food', 'food__category', 'meal')

            if goal == 'Lose':
                # Always trim fat if fat is above target (even when kcal <= target)
                cur_fat = float(totals.get('fat', 0.0))
                fat_target = float(diet_plan.daily_calories or 0.0) * 0.25 / 9.0  # from ratios for Lose
                if cur_fat > fat_target:
                    self._scale_components(components, macro_filter='fat', scale=0.97)
                    totals = diet_plan.calculate_daily_nutrition(d)
                    cur_fat = float(totals.get('fat', 0.0))
                    if cur_fat > fat_target:
                        self._scale_components(components, macro_filter='fat', scale=0.98)
                # If kcal still above target after fat trim, trim fat again based on kcal
                totals = diet_plan.calculate_daily_nutrition(d)
                if float(totals.get('calories', 0.0)) > target_kcal:
                    self._scale_components(components, macro_filter='fat', scale=0.97)

            else:
                # Maintain: reduce all components by 3%
                self._scale_components(components, macro_filter=None, scale=0.97)

    # ------------------------ helpers ------------------------
    def _scale_components(self, components, macro_filter: str | None, scale: float) -> None:
        for comp in components:
            if macro_filter is not None and dominant_macro_of_food(comp.food) != macro_filter:
                continue
            new_qty = comp.quantity * scale
            if new_qty <= 0:
                continue
            comp.quantity = new_qty
            comp.save(update_fields=['quantity'])


