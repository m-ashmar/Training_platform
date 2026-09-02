from __future__ import annotations

from typing import Dict

from ..models import DietPlan, MealComponent, FoodItem
from ..utils.logging_utils import log_day_macros
from ..utils.nutrition import get_macro_ratios, dominant_macro_of_food, macro_per_gram
import logging

logger = logging.getLogger(__name__)


class MacroCapEnforcer:
    """
    Enforce macro caps per day: if any macro exceeds (target + 15g),
    reduce quantities of components dominated by that macro proportionally
    to bring totals back within cap.
    """

    def enforce(self, diet_plan: DietPlan) -> None:
        goal = (diet_plan.goal or 'Maintain')
        ratios = get_macro_ratios(goal)
        target_kcal = float(diet_plan.daily_calories or 0.0)
        if target_kcal <= 0:
            return
        fat_cap = target_kcal * ratios['fat'] / 9.0
        if (goal or '').lower().startswith('lose'):
            fat_cap = fat_cap  # no +15g tolerance for Lose
        else:
            fat_cap = fat_cap + 15.0
        caps = {
            'protein': target_kcal * ratios['protein'] / 4.0 + 15.0,
            'carb': target_kcal * ratios['carb'] / 4.0 + 15.0,
            'fat': fat_cap,
        }

        dates = sorted({m.date for m in diet_plan.meals.all()})
        for d in dates:
            try:
                log_day_macros('caps_before', diet_plan, d)
            except Exception:
                # Optional side effect: swallowing this silently is what made the
                # surrounding failures invisible in logs. Control flow is unchanged.
                logger.debug('suppressed non-fatal error', exc_info=True)
            totals = diet_plan.calculate_daily_nutrition(d)
            cur = {
                'protein': float(totals.get('protein', 0.0)),
                'carb': float(totals.get('carbs', 0.0)),
                'fat': float(totals.get('fat', 0.0)),
            }
            for macro in ('protein', 'carb', 'fat'):
                if cur[macro] <= caps[macro]:
                    continue
                excess = cur[macro] - caps[macro]
                self._reduce_macro_for_day(diet_plan, d, macro, excess)
            # If carbs are below (target - 15g), gently boost carb-dominant components by up to +10%
            carb_floor = target_kcal * ratios['carb'] / 4.0 - 15.0
            if cur['carb'] < carb_floor:
                self._boost_macro_for_day(diet_plan, d, 'carb', amount_ratio=0.10)
            try:
                log_day_macros('caps_after', diet_plan, d)
            except Exception:
                # Optional side effect: swallowing this silently is what made the
                # surrounding failures invisible in logs. Control flow is unchanged.
                logger.debug('suppressed non-fatal error', exc_info=True)

    # ------------------------ helpers ------------------------
    def _reduce_macro_for_day(self, diet_plan: DietPlan, day, macro: str, reduce_grams: float) -> None:
        # BUG FIX: Optimize with select_related for food category
        components = MealComponent.objects.filter(meal__diet_plan=diet_plan, meal__date=day).select_related('food', 'food__category', 'meal')
        # Consider all components contributing to this macro (not only dominant)
        contribs = []  # (comp, macro_per_g, macro_contrib_g)
        total_macro = 0.0
        for comp in components:
            mg = macro_per_gram(comp.food, macro)
            if mg <= 0.0:
                continue
            contrib = comp.quantity * mg
            if contrib <= 0.0:
                continue
            contribs.append((comp, mg, contrib))
            total_macro += contrib
        if total_macro <= 0.0 or not contribs:
            return
        target_macro = max(0.0, total_macro - reduce_grams)
        scale = target_macro / total_macro if total_macro > 0 else 1.0
        scale = max(0.5, min(1.0, scale))
        for comp, _, _ in contribs:
            new_qty = comp.quantity * scale
            if new_qty <= 0:
                continue
            comp.quantity = new_qty
            comp.save(update_fields=['quantity'])

    def _boost_macro_for_day(self, diet_plan: DietPlan, day, macro: str, amount_ratio: float) -> None:
        # BUG FIX: Optimize with select_related for food category
        components = MealComponent.objects.filter(meal__diet_plan=diet_plan, meal__date=day).select_related('food', 'food__category', 'meal')
        # Prefer items with higher macro density, and never boost pure-fat items
        items = []  # (comp, macro_per_g)
        for comp in components:
            # skip fat-only items when boosting anything
            if dominant_macro_of_food(comp.food) == 'fat':
                continue
            mg = macro_per_gram(comp.food, macro)
            if mg > 0.0:
                items.append((comp, mg))
        items.sort(key=lambda x: x[1], reverse=True)
        if not items:
            return
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
            name_l = (comp.food.name or '').lower()
            if any(k in name_l for k in ('lettuce','tomato','tomatoes','cucumber','green bean','spinach','zucchini','broccoli')):
                cap = min(cap, 300.0)
            if comp.quantity > cap:
                comp.quantity = cap
            comp.save(update_fields=['quantity'])


