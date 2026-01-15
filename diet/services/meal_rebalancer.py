from __future__ import annotations

from ..models import DietPlan
from ..utils.nutrition import get_macro_ratios


class MealRebalancer:
    """
    Post-insert macro rebalancer extracted from DietGenerator._rebalance_macros_by_goal.
    """

    def rebalance(self, diet_plan: DietPlan) -> None:
        from collections import defaultdict

        def dominant_macro_of_food(food) -> str:
            try:
                if food.category:
                    if getattr(food.category, 'is_protein', False):
                        return 'protein'
                    if getattr(food.category, 'is_carb', False):
                        return 'carb'
                    if getattr(food.category, 'is_fat', False):
                        return 'fat'
            except Exception:
                pass
            p_cals = 4.0 * float(getattr(food, 'protein_per_gram', 0.0))
            c_cals = 4.0 * float(getattr(food, 'carbs_per_gram', 0.0))
            f_cals = 9.0 * float(getattr(food, 'fat_per_gram', 0.0))
            if p_cals >= c_cals and p_cals >= f_cals:
                return 'protein'
            if c_cals >= p_cals and c_cals >= f_cals:
                return 'carb'
            return 'fat'

        user_goal = diet_plan.goal or 'Maintain'
        ratios = self._macro_ratios_for_goal(user_goal)
        daily_target_cals = float(diet_plan.daily_calories or 0)
        if daily_target_cals <= 0:
            return
        protein_target = daily_target_cals * ratios['protein'] / 4.0
        carb_target = daily_target_cals * ratios['carb'] / 4.0
        fat_target = daily_target_cals * ratios['fat'] / 9.0

        meals_by_date = defaultdict(list)
        for m in diet_plan.meals.all():
            meals_by_date[m.date].append(m)

        tol_pct = 0.10
        for d, meals_list in meals_by_date.items():
            totals = diet_plan.calculate_daily_nutrition(d)
            cur_p = float(totals.get('protein', 0.0))
            cur_c = float(totals.get('carbs', 0.0))
            cur_f = float(totals.get('fat', 0.0))

            def apply_scale_to_components(macro_key: str, scale: float):
                for m in meals_list:
                    for comp in m.components.all():
                        if dominant_macro_of_food(comp.food) == macro_key:
                            comp.quantity = comp.quantity * scale
                            comp.save(update_fields=['quantity'])

            g = (user_goal or 'Maintain').lower()
            if 'lose' in g:
                if cur_p < protein_target:
                    scale = min(1.15, (protein_target / max(cur_p, 1e-6)))
                    apply_scale_to_components('protein', scale)
                if cur_c > carb_target:
                    scale = max(0.85, (carb_target / max(cur_c, 1e-6)))
                    apply_scale_to_components('carb', scale)
                upper_f = fat_target * (1.0 + tol_pct)
                if cur_f > upper_f:
                    scale = max(0.85, (fat_target / max(cur_f, 1e-6)))
                    apply_scale_to_components('fat', scale)
            elif 'gain' in g:
                if cur_c < carb_target:
                    scale = min(1.20, (carb_target / max(cur_c, 1e-6)))
                    apply_scale_to_components('carb', scale)
                if cur_p < protein_target:
                    scale = min(1.15, (protein_target / max(cur_p, 1e-6)))
                    apply_scale_to_components('protein', scale)
            else:
                lower_p = protein_target * (1.0 - tol_pct)
                upper_p = protein_target * (1.0 + tol_pct)
                lower_c = carb_target * (1.0 - tol_pct)
                upper_c = carb_target * (1.0 + tol_pct)
                lower_f = fat_target * (1.0 - tol_pct)
                upper_f = fat_target * (1.0 + tol_pct)

                if cur_p < lower_p:
                    scale = min(1.15, (protein_target / max(cur_p, 1e-6)))
                    apply_scale_to_components('protein', scale)
                elif cur_p > upper_p:
                    scale = max(0.85, (protein_target / max(cur_p, 1e-6)))
                    apply_scale_to_components('protein', scale)
                if cur_c < lower_c:
                    scale = min(1.15, (carb_target / max(cur_c, 1e-6)))
                    apply_scale_to_components('carb', scale)
                elif cur_c > upper_c:
                    scale = max(0.85, (carb_target / max(cur_c, 1e-6)))
                    apply_scale_to_components('carb', scale)
                if cur_f < lower_f:
                    scale = min(1.15, (fat_target / max(cur_f, 1e-6)))
                    apply_scale_to_components('fat', scale)
                elif cur_f > upper_f:
                    scale = max(0.85, (fat_target / max(cur_f, 1e-6)))
                    apply_scale_to_components('fat', scale)

    def _macro_ratios_for_goal(self, goal: str) -> dict[str, float]:
        """Use centralized macro ratios from utils/nutrition.py"""
        return get_macro_ratios(goal)


