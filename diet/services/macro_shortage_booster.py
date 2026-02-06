from __future__ import annotations

from typing import Dict

from ..models import DietPlan, MealComponent, FoodItem
from ..utils.nutrition import get_macro_ratios, dominant_macro_of_food, macro_per_gram


class MacroShortageBooster:
    """
    If a day's macro is short by more than 15g, iteratively increase
    quantities of components dominated by that macro by +10% per iteration
    across all meals, re-checking after each pass, up to a max number of passes.
    """

    def boost(self, diet_plan: DietPlan, max_passes: int = 6) -> None:
        ratios = get_macro_ratios(diet_plan.goal or 'Maintain')
        target_kcal = float(diet_plan.daily_calories or 0.0)
        if target_kcal <= 0:
            return
        targets = {
            'protein': target_kcal * ratios['protein'] / 4.0,
            'carb': target_kcal * ratios['carb'] / 4.0,
            'fat': target_kcal * ratios['fat'] / 9.0,
        }

        dates = sorted({m.date for m in diet_plan.meals.all()})
        for d in dates:
            for _ in range(max_passes):
                totals = diet_plan.calculate_daily_nutrition(d)
                cur = {
                    'protein': float(totals.get('protein', 0.0)),
                    'carb': float(totals.get('carbs', 0.0)),
                    'fat': float(totals.get('fat', 0.0)),
                }
                shortages = {
                    'protein': max(0.0, targets['protein'] - cur['protein']),
                    'carb': max(0.0, targets['carb'] - cur['carb']),
                    'fat': max(0.0, targets['fat'] - cur['fat']),
                }
                # Stop if all within 10g for protein, 15g for others
                protein_threshold = 10.0
                other_threshold = 15.0
                if (shortages.get('protein', 0.0) <= protein_threshold and 
                    shortages.get('carb', 0.0) <= other_threshold and
                    shortages.get('fat', 0.0) <= other_threshold):
                    break
                
                # FIX #8: Goal-aware priority order
                # For GAIN: boost carbs first (carbs are the priority for bulking)
                # For LOSE/MAINTAIN: boost protein first (preserve muscle)
                goal_lower = (diet_plan.goal or 'Maintain').lower()
                if 'gain' in goal_lower or 'bulk' in goal_lower:
                    # GAIN: Boost carbs first, then protein
                    if shortages.get('carb', 0.0) > other_threshold:
                        boost_ratio = 0.15 if shortages.get('carb', 0.0) > 30.0 else 0.10
                        self._boost_macro_for_day(diet_plan, d, 'carb', boost_ratio)
                    if shortages.get('protein', 0.0) > protein_threshold:
                        self._boost_macro_for_day(diet_plan, d, 'protein', 0.10)
                else:
                    # LOSE/MAINTAIN: Boost protein first (original behavior)
                    protein_short = shortages.get('protein', 0.0)
                    if protein_short > protein_threshold:
                        boost_ratio = 0.15 if protein_short > 30.0 else 0.10
                        self._boost_macro_for_day(diet_plan, d, 'protein', boost_ratio)
                    if shortages.get('carb', 0.0) > other_threshold:
                        self._boost_macro_for_day(diet_plan, d, 'carb', 0.10)

    # ------------------------ helpers ------------------------
    def _boost_macro_for_day(self, diet_plan: DietPlan, day, macro: str, amount_ratio: float) -> None:
        # BUG FIX: Optimize with select_related for food category
        components = MealComponent.objects.filter(meal__diet_plan=diet_plan, meal__date=day).select_related('food', 'food__category', 'meal')
        # Skip fat-only items and prefer high-density staples by macro_per_gram
        items = []  # (comp, macro_per_g)
        for comp in components:
            dom = dominant_macro_of_food(comp.food)
            if dom != macro:
                continue
            # BUG FIX: Handle all three macros, not just protein and carbs
            mg = macro_per_gram(comp.food, macro)
            if mg > 0.0:
                items.append((comp, mg))
        items.sort(key=lambda x: x[1], reverse=True)
        for comp, _ in items:
            comp.quantity = comp.quantity * (1.0 + amount_ratio)
            # Clamp per-item quantity to caps
            dom = dominant_macro_of_food(comp.food)
            from ..utils.nutrition import portion_sanity_cap_grams
            cap = portion_sanity_cap_grams(dom)
            if dom == 'carb':
                cap = min(cap, 400.0)
            if dom == 'protein':
                cap = min(cap, 350.0)
            if dom == 'fat':
                cap = min(cap, 100.0)
            # Veggie-like stricter cap
            name_l = (comp.food.name or '').lower()
            if any(k in name_l for k in ('lettuce','tomato','tomatoes','cucumber','green bean','spinach','zucchini','broccoli')):
                cap = min(cap, 300.0)
            if comp.quantity > cap:
                comp.quantity = cap
            comp.save(update_fields=['quantity'])


