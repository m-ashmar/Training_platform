from __future__ import annotations

from ..models import DietPlan, MealComponent, FoodItem
from ..utils.logging_utils import log_day_macros
from ..utils.nutrition import get_macro_ratios, dominant_macro_of_food, macro_per_gram


class MacroBalancer:
    """
    Critical post-processing macro correction.
    Adjust meals minimally to meet daily macro targets within tight tolerances.
    """

    def rebalance(self, diet_plan: DietPlan) -> None:
        user_goal = diet_plan.goal or 'Maintain'
        ratios = get_macro_ratios(user_goal)
        daily_target_cals = float(diet_plan.daily_calories or 0)
        if daily_target_cals <= 0:
            return
        target = {
            'protein': daily_target_cals * ratios['protein'] / 4.0,
            'carbs': daily_target_cals * ratios['carb'] / 4.0,
            'fat': daily_target_cals * ratios['fat'] / 9.0,
        }

        # Tolerances
        tol = {'protein': 5.0, 'carbs': 10.0, 'fat': 5.0}

        dates = sorted({m.date for m in diet_plan.meals.all()})
        for d in dates:
            try:
                log_day_macros('macro_balance_before', diet_plan, d)
            except Exception:
                pass
            # Multipass convergence (max 3 passes)
            for _ in range(3):
                totals = diet_plan.calculate_daily_nutrition(d)
                deltas = {
                    'protein': float(totals.get('protein', 0.0)) - target['protein'],
                    'carbs': float(totals.get('carbs', 0.0)) - target['carbs'],
                    'fat': float(totals.get('fat', 0.0)) - target['fat'],
                }

                # Protein correction
                if abs(deltas['protein']) > tol['protein']:
                    self._adjust_macro_for_day(diet_plan, d, 'protein', -deltas['protein'])

                # Carb correction
                if abs(deltas['carbs']) > tol['carbs']:
                    self._adjust_macro_for_day(diet_plan, d, 'carb', -deltas['carbs'])

                # Fat correction
                if abs(deltas['fat']) > tol['fat']:
                    self._adjust_macro_for_day(diet_plan, d, 'fat', -deltas['fat'])

                # Stop early if within tolerance
                totals = diet_plan.calculate_daily_nutrition(d)
                if (
                    abs(float(totals.get('protein', 0.0)) - target['protein']) <= tol['protein'] and
                    abs(float(totals.get('carbs', 0.0)) - target['carbs']) <= tol['carbs'] and
                    abs(float(totals.get('fat', 0.0)) - target['fat']) <= tol['fat']
                ):
                    break
            try:
                log_day_macros('macro_balance_after', diet_plan, d)
            except Exception:
                pass

    def _adjust_macro_for_day(self, diet_plan: DietPlan, day, macro: str, needed_delta_g: float) -> None:
        """
        Positive needed_delta_g: add macro grams by scaling quantities.
        Negative needed_delta_g: reduce macro grams by scaling quantities.
        Distribute across all components that contribute to the macro.
        Caps: add/reduce up to 20% per component in a single pass to avoid extreme jumps.
        """
        # BUG FIX: Optimize with select_related for food category
        components = list(MealComponent.objects.filter(meal__diet_plan=diet_plan, meal__date=day).select_related('food', 'food__category', 'meal'))

        # Build contributions using ORIGINAL quantities (before any scaling in this pass)
        contribs = []  # (comp, macro_per_g, contrib_macro_g)
        total_macro = 0.0
        for comp in components:
            mg = macro_per_gram(comp.food, macro)
            if mg <= 0.0:
                continue
            contrib_macro = float(comp.quantity) * mg
            if contrib_macro <= 0.0:
                continue
            contribs.append((comp, mg, contrib_macro))
            total_macro += contrib_macro
        if not contribs:
            return

        if needed_delta_g > 0:
            # Prefer higher macro density first
            contribs.sort(key=lambda x: x[1], reverse=True)
            remaining = needed_delta_g
            for comp, mg, contrib_macro in contribs:
                if remaining <= 0:
                    break
                # Max macro we can add with 20% cap
                max_add = 0.20 * contrib_macro
                macro_add = min(remaining, max_add)
                if macro_add <= 0:
                    continue
                # scale by macro basis: new_qty = qty * (1 + macro_add / contrib_macro)
                orig_qty = float(comp.quantity)
                scale_by = 1.0 + (macro_add / max(contrib_macro, 1e-6))
                new_qty = orig_qty * scale_by
                
                # Apply per-item caps to prevent unrealistic quantities
                dom = dominant_macro_of_food(comp.food)
                from ..utils.nutrition import portion_sanity_cap_grams
                cap = portion_sanity_cap_grams(dom)
                if dom == 'carb':
                    cap = min(cap, 400.0)
                if dom == 'protein':
                    cap = min(cap, 350.0)
                if dom == 'fat':
                    cap = min(cap, 100.0)
                # Vegetable-specific caps
                name_l = (comp.food.name or '').lower()
                if any(k in name_l for k in ('lettuce','tomato','tomatoes','cucumber','green bean','spinach','zucchini','broccoli')):
                    cap = min(cap, 300.0)
                
                comp.quantity = min(new_qty, cap)
                comp.save(update_fields=['quantity'])
                remaining -= macro_add
        else:
            # Reduce across contributors, proportional with 20% cap
            remaining = abs(needed_delta_g)
            # Prefer higher contributors first
            contribs.sort(key=lambda x: x[2], reverse=True)
            for comp, mg, contrib_macro in contribs:
                if remaining <= 0:
                    break
                max_reduce = 0.20 * contrib_macro
                macro_reduce = min(remaining, max_reduce, contrib_macro)
                if macro_reduce <= 0:
                    continue
                orig_qty = float(comp.quantity)
                scale_by = 1.0 - (macro_reduce / max(contrib_macro, 1e-6))
                scale_by = max(0.0, scale_by)
                new_qty = orig_qty * scale_by
                
                # Even when reducing, ensure we don't create unrealistic quantities
                # (though reduction shouldn't cause cap issues, good to be consistent)
                dom = dominant_macro_of_food(comp.food)
                from ..utils.nutrition import portion_sanity_cap_grams
                cap = portion_sanity_cap_grams(dom)
                if dom == 'carb':
                    cap = min(cap, 400.0)
                if dom == 'protein':
                    cap = min(cap, 350.0)
                if dom == 'fat':
                    cap = min(cap, 100.0)
                name_l = (comp.food.name or '').lower()
                if any(k in name_l for k in ('lettuce','tomato','tomatoes','cucumber','green bean','spinach','zucchini','broccoli')):
                    cap = min(cap, 300.0)
                
                comp.quantity = min(new_qty, cap)
                comp.save(update_fields=['quantity'])
                remaining -= macro_reduce


