from __future__ import annotations

from typing import Dict, List, Tuple, Set
from dataclasses import dataclass

from django.utils import timezone
from django.conf import settings
import random
import math
import hashlib

from ..models import FoodItem, UserFoodCategoryPreference, DietConfig
from ..ai_models import DietPlanOutput, AIMeal, AIIngredient
from ..utils.nutrition import (
    get_macro_densities_for_food,
    macro_efficiency_score,
    portion_sanity_cap_grams,
    is_piece_food_name,
    goal_meal_kcal_split,
    SAFE_FALLBACK_FOODS,
)
from ..utils.logging_utils import safe_json_log
from ..experimental.staged_fill import StagedMealFiller, MealTargets


@dataclass
class MealTarget:
    name: str
    kcal_target: float
    macro_targets: Dict[str, float]  # grams per macro for this meal


@dataclass
class PlannerContext:
    user: object
    goal: str
    macro_order: List[str]
    meal_names: List[str]
    allowed_map: Dict[str, Dict[str, List[FoodItem]]]
    piece_weights: Dict[str, float]
    common_veggies: List[FoodItem]
    common_fruits: List[FoodItem]


@dataclass
class DayContext:
    date: object
    meal_kcal: Dict[str, float]
    meal_distribution: Dict[str, float]
    meal_targets: Dict[str, MealTarget]
    recent_exclusions_ids: Dict[str, Set[int]]
    recent_exclusions_names: Dict[str, Set[str]]
    used_in_window: Dict[str, Set[int]]
    used_names_window: Dict[str, Set[str]]


@dataclass
class MealState:
    components: List[Tuple[FoodItem, float]]
    used_macro_counts: Dict[str, int]
    kcal_consumed: float
    macro_consumed: Dict[str, float]
    meal_existing_ids: Set[int]
    meal_existing_names: Set[str]


class RuleBasedPlanner:
    """
    Deterministic diet planner that uses user category preferences and macro density.

    Key rules:
    - Daily targets across all meals; default 3 meals + optional snack.
    - Reserve 200 kcal for snack; distribute remaining kcal across meals by goal-based pattern.
    - For each meal, fill macros in priority order with 10% slack for the first two macros.
    - Sort candidate foods by macro density per kcal (macro_per_gram / calories_per_gram) descending.
    - Max 2 items per macro per meal.
    - Carbs placement rules per goal: Gain (all meals have carbs); Shred (carbs only breakfast/lunch);
      Maintain (dinner carbs optional).
    - Quantity rounding to nearest 5g; piece foods may be left in grams (conversion handled later).
    """

    def __init__(self, user):
        self.user = user
        self._smart_summary: List[Dict] = []

    def _normalize_name_for_repeat(self, name: str) -> str:
        n = (name or '').strip().lower()
        # Collapse common variants to base token to reduce repetition (best-effort)
        mapping = [
            ('sweet potato', 'sweet potato'),
            ('chicken', 'chicken'),
            ('breast', 'chicken'),
            ('tuna', 'tuna'),
            ('fish', 'fish'),
            ('egg', 'egg'),
            ('rice', 'rice'),
            ('potato', 'potato'),
            ('almond', 'almonds'),
            ('banana', 'banana'),
            ('oats', 'oats'),
            ('apple', 'apple'),
            ('broccoli', 'broccoli'),
            ('asparagus', 'asparagus'),
        ]
        for key, base in mapping:
            if key in n:
                return base
        return n

    def _is_oil(self, name: str) -> bool:
        try:
            n = (name or '').strip().lower()
        except Exception:
            n = ''
        return 'oil' in n

    def _is_vegetable(self, food: FoodItem) -> bool:
        try:
            cat = getattr(food, 'category', None)
            if cat and hasattr(cat, 'name'):
                nm = (getattr(cat, 'name', '') or '').lower()
                if 'vegetable' in nm or 'vegetables' in nm or 'veggie' in nm:
                    return True
        except Exception:
            pass
        name = ((getattr(food, 'name', '') or '')).lower()
        veg_keywords = (
            'lettuce', 'tomato', 'tomatoes', 'cucumber', 'green bean', 'spinach',
            'zucchini', 'broccoli', 'asparagus', 'carrot', 'pepper', 'cabbage',
            'cauliflower', 'celery', 'kale', 'brussels sprout', 'brussels sprouts'
        )
        return any(k in name for k in veg_keywords)

    def _resolve_goal(self) -> str:
        """
        Resolve user goal into one of: 'lose' | 'gain' | 'maintain'.
        Checks: fitness_goal, goal, then client_goals list.
        """
        goal = getattr(self.user, 'fitness_goal', None) or getattr(self.user, 'goal', None)
        if goal:
            g = str(goal).lower()
            if 'lose' in g or 'fat' in g:
                return 'lose'
            if 'gain' in g or 'muscle' in g:
                return 'gain'
            return 'maintain'
        try:
            goals = (getattr(self.user, 'client_goals', []) or [])
            goals_l = ','.join(goals).lower()
            if 'lose' in goals_l or 'fat' in goals_l:
                return 'lose'
            if 'gain' in goals_l or 'muscle' in goals_l:
                return 'gain'
        except Exception:
            pass
        return 'maintain'

    # ------------------------ public API ------------------------
    def generate(
        self,
        daily_kcal: float,
        meal_count: int = 3,
        snack_count: int = 1,
        start_date: str | None = None,
        duration_days: int = 1,
        no_repeat_days: int = 3,
    ) -> DietPlanOutput:
        from datetime import date as _date, timedelta as _timedelta
        from ..validators import validate_diet_generation
        
        # BUG FIX: Validate inputs before processing
        validate_diet_generation(daily_kcal, meal_count, duration_days)

        base_date = _date.fromisoformat(start_date) if start_date else timezone.now().date()
        meals = ["Breakfast", "Lunch", "Dinner"][:meal_count]

        # Allowed foods per meal/macro (static per user)
        allowed = self._build_allowed_foods_map()
        order = self._macro_priority_order()
        goal = self._resolve_goal()

        # Preload commonly used fallbacks and piece weights to avoid repeated DB hits
        try:
            self._common_veggies = self._load_common_foods([
                'Broccoli', 'Spinach', 'Carrot', 'Green Bean', 'Zucchini',
                'Bell Pepper', 'Cucumber', 'Lettuce', 'Tomato', 'Asparagus',
                'Cauliflower', 'Kale', 'Brussels Sprouts'
            ])
        except Exception:
            self._common_veggies = []
        try:
            self._common_fruits = self._load_common_foods([
                'Apple', 'Banana', 'Orange', 'Strawberry', 'Blueberry',
                'Mango', 'Pineapple', 'Grapes', 'Watermelon', 'Kiwi'
            ])
        except Exception:
            self._common_fruits = []
        try:
            cfg = DietConfig.objects.last()
            self._piece_weights = (cfg.piece_weights if cfg and cfg.piece_weights else {})
        except Exception:
            self._piece_weights = {}

        # Build planner context (read-only use for now)
        ctx = self._build_planner_context(
            user=self.user,
            goal=goal,
            macro_order=order,
            meal_names=meals,
            allowed_map=allowed,
            piece_weights=self._piece_weights,
            common_veggies=self._common_veggies,
            common_fruits=self._common_fruits,
        )

        planned_meals: List[AIMeal] = []
        # Track foods chosen in this generation window to prevent reuse within next 3 days
        used_in_window: Dict[str, Set[int]] = {m: set() for m in ("Breakfast", "Lunch", "Dinner", "Snack")}
        used_names_window: Dict[str, Set[str]] = {m: set() for m in ("Breakfast", "Lunch", "Dinner", "Snack")}
        # Maintain per-day sliding windows of chosen FoodItem IDs (ID-only)
        day_windows: List[Dict[str, Set[int]]] = []

        for day_idx in range(max(1, duration_days)):
            current_date = base_date + _timedelta(days=day_idx)
            # Deterministic randomness per user/day for reproducible variety
            try:
                # uid is user id
                uid = int(getattr(self.user, 'id', 0) or 0)
            except Exception:
                uid = 0
            salt = f"rbp:{uid}:{current_date.isoformat()}"
            seed = int(hashlib.sha256(salt.encode('utf-8')).hexdigest()[:12], 16)
            random.seed(seed)
            day_start_idx = len(planned_meals)
            # Rebuild in-run recency from last `no_repeat_days` day windows
            try:
                rebuilt: Dict[str, Set[int]] = {m: set() for m in ("Breakfast", "Lunch", "Dinner", "Snack")}
                # Union of previous day windows
                for w in day_windows[-int(no_repeat_days):]:
                    for _m in rebuilt:
                        rebuilt[_m].update(w.get(_m, set()))
                used_in_window = rebuilt
                # Always exclude by id only (names unused)
                used_names_window = {m: set() for m in ("Breakfast", "Lunch", "Dinner", "Snack")}
            except Exception:
                pass

            # Per-day allocations and macro targets are computed via DayContext
            # Build DayContext mirroring current per-day calculations (read-only usage for now)
            day_ctx = self._prepare_day(
                ctx=ctx,
                current_date=current_date,
                daily_kcal=daily_kcal,
                snack_count=snack_count,
                meals=meals,
                used_in_window=used_in_window,
                used_names_window=used_names_window,
                no_repeat_days=no_repeat_days,
            )
            try:
                day_alloc = {mm: round(day_ctx.meal_kcal.get(mm, 0.0), 1) for mm in meals}
                targets_json = {
                    mm: {
                        'protein': round(day_ctx.meal_targets[mm].macro_targets['protein'], 1),
                        'carb': round(day_ctx.meal_targets[mm].macro_targets['carb'], 1),
                        'fat': round(day_ctx.meal_targets[mm].macro_targets['fat'], 1),
                    } for mm in meals
                }
                safe_json_log(stage="allocation", data={
                    'date': str(current_date),
                    'daily_kcal': float(daily_kcal or 0.0),
                    'per_meal_kcal': day_alloc,
                    'targets': targets_json,
                    'goal': goal,
                }, logger_name='diet')
            except Exception:
                pass

            for meal_name in meals:
                planned_meals.append(self._plan_meal(meal_name, ctx, day_ctx))
                

            # Add fruits to 2 meals per day (100-150g each) before adding snack
            today_meals = planned_meals[day_start_idx:]
            self._add_fruits_to_day(meals, allowed, today_meals, used_in_window)

            if snack_count:
                snack_ing = self._build_snack(allowed, used_in_window)
                planned_meals.append(
                    AIMeal(
                        meal_name="Snack",
                        description="Snack planned by rule-based system",
                        ingredients=snack_ing,
                        total_nutrition={"calories": 0.0, "protein": 0.0, "carbs": 0.0, "fat": 0.0},
                        meal_type="Snack",
                    )
                )

            # Note: rebalancing now occurs inside _plan_meal on (FoodItem, grams) components before finalization.

            # After finishing the full day, accumulate recency from actual meals
            try:
                today_meals = planned_meals[day_start_idx:]
                today_used: Dict[str, Set[int]] = {m: set() for m in ("Breakfast","Lunch","Dinner","Snack")}
                from diet.models import FoodItem as _FoodItem
                for m in today_meals:
                    meal_key = m.meal_name if m.meal_name in today_used else (m.meal_type or "")
                    if meal_key not in today_used:
                        meal_key = "Snack" if (m.meal_type == "Snack" or m.meal_name == "Snack") else "Dinner"
                    for ing in m.ingredients:
                        try:
                            fi = _FoodItem.objects.filter(name=ing.name).first() or _FoodItem.objects.filter(name__iexact=ing.name).first()
                            if fi and getattr(fi, "id", None):
                                today_used[meal_key].add(int(fi.id))
                        except Exception:
                            continue
                day_windows.append(today_used)
                # Prune to last `no_repeat_days`
                while len(day_windows) > int(no_repeat_days):
                    day_windows.pop(0)
                try:
                    dbg_counts = {k: len(v) for k,v in today_used.items()}
                    print(f"[RECENCY] accumulated actual ids for day {current_date}: {dbg_counts}")
                except Exception:
                    pass
            except Exception:
                pass

        output = DietPlanOutput(plan=planned_meals)
        if getattr(settings, 'DIET_SMART_MACRO_PLANNER', False):
            try:
                output.plan_metadata['smart_macro_summary'] = self._smart_summary
            except Exception:
                pass
        return output

    # ------------------------ meal rebalancer utilities ------------------------
    def _grams_from_qty(self, qty: str) -> float:
        try:
            import re
            m = re.search(r"(\d+(?:\.\d+)?)\s*g", qty or "")
            return float(m.group(1)) if m else 0.0
        except Exception:
            return 0.0

    def _set_qty(self, ing, grams: float) -> None:
        g = max(0.0, float(grams))
        ing.quantity = f"{int(round(g))}g"

    def _compute_meal_nutrition(self, ingredients) -> dict:
        from diet.models import FoodItem
        kcal = p = c = f = 0.0
        for ing in ingredients:
            fi = FoodItem.objects.filter(name=ing.name).first() or FoodItem.objects.filter(name__iexact=ing.name).first()
            if not fi:
                continue
            g = self._grams_from_qty(getattr(ing, 'quantity', '') or '')
            if g <= 0.0:
                continue
            kcal += g * float(getattr(fi, 'calories_per_gram', 0.0) or 0.0)
            p    += g * float(getattr(fi, 'protein_per_gram', 0.0) or 0.0)
            c    += g * float(getattr(fi, 'carbs_per_gram', 0.0) or 0.0)
            f    += g * float(getattr(fi, 'fat_per_gram', 0.0) or 0.0)
        return {'calories': kcal, 'protein': p, 'carbs': c, 'fat': f}

    def _is_oil_like(self, name: str) -> bool:
        nm = (name or '').lower()
        return ('oil' in nm) or ('ghee' in nm) or ('butter' in nm)

    def _scale_by_macro(self, ingredients, macro: str, factor: float) -> None:
        from diet.models import FoodItem
        factor = float(factor)
        for ing in ingredients:
            fi = FoodItem.objects.filter(name=ing.name).first() or FoodItem.objects.filter(name__iexact=ing.name).first()
            if not fi:
                continue
            # Choose items whose dominant macro matches
            dom = self._dominant_macro_of_food(fi)
            if dom != macro:
                continue
            g = self._grams_from_qty(ing.quantity)
            if g <= 0.0:
                continue
            new_g = g * factor
            # Floors to avoid absurd tiny portions
            min_g = 5.0 if self._is_oil_like(getattr(fi, 'name', '')) else 25.0
            try:
                print(f"[REBALANCE_SCALE] item={getattr(fi,'name','')} macro={macro} dom={dom} old_g={round(g,1)} factor={round(factor,3)} target_g={round(new_g,1)} min_floor={min_g}")
            except Exception:
                pass
            self._set_qty(ing, max(min_g, new_g))

    def _within_band(self, actual: float, target: float, band: float) -> bool:
        if target <= 0.0:
            return True
        lo = (1.0 - band) * target
        hi = (1.0 + band) * target
        return (actual >= lo) and (actual <= hi)

    def _rebalance_meal_accept(self, meal: AIMeal, kcal_target: float, macro_targets: dict) -> AIMeal:
        # Iteratively nudge by 10% towards acceptance bands
        band = 0.09
        max_iter = 10
        try:
            print(f"[REBALANCE] start meal={meal.meal_name} kcal_t={round(kcal_target,1)} tp={round(float(macro_targets.get('protein',0.0) or 0.0),1)} tc={round(float(macro_targets.get('carb',0.0) or 0.0),1)} tf={round(float(macro_targets.get('fat',0.0) or 0.0),1)} band=±{int(band*100)}%")
        except Exception:
            pass
        accepted = False
        for it in range(max_iter):
            totals = self._compute_meal_nutrition(meal.ingredients)
            kcal = totals['calories']; p = totals['protein']; c = totals['carbs']; f = totals['fat']
            tp = float(macro_targets.get('protein', 0.0) or 0.0)
            tc = float(macro_targets.get('carb', 0.0) or 0.0)
            tf = float(macro_targets.get('fat', 0.0) or 0.0)

            # Acceptance check
            if (self._within_band(kcal, kcal_target, band)
                and self._within_band(p, tp, band)
                and self._within_band(c, tc, band)
                and self._within_band(f, tf, band)):
                accepted = True
                try:
                    print(f"[ACCEPT] meal={meal.meal_name} iter={it} kcal={round(kcal,1)} p={round(p,1)} c={round(c,1)} f={round(f,1)}")
                except Exception:
                    pass
                break

            # Hard triggers
            if kcal > 1.10 * kcal_target or (tf > 0.0 and f > 1.30 * tf):
                try:
                    print(f"[REBALANCE] iter={it} trigger=over_kcal_or_fat kcal={round(kcal,1)}>{round(1.10*kcal_target,1)} or fat={round(f,1)}>{round(1.30*tf,1)} -> scale fat x0.9")
                except Exception:
                    pass
                self._scale_by_macro(meal.ingredients, 'fat', 0.9)
                continue
            if tp > 0.0 and p > 1.10 * tp:
                try:
                    print(f"[REBALANCE] iter={it} trigger=over_protein p={round(p,1)}>{round(1.10*tp,1)} -> scale protein x0.9")
                except Exception:
                    pass
                self._scale_by_macro(meal.ingredients, 'protein', 0.9)
                continue
            if tc > 0.0 and c > 1.10 * tc:
                try:
                    print(f"[REBALANCE] iter={it} trigger=over_carb c={round(c,1)}>{round(1.10*tc,1)} -> scale carb x0.9")
                except Exception:
                    pass
                self._scale_by_macro(meal.ingredients, 'carb', 0.9)
                continue

            # Under targets with kcal headroom
            if tp > 0.0 and p < 0.90 * tp and kcal < 1.09 * kcal_target:
                try:
                    print(f"[REBALANCE] iter={it} trigger=under_protein p={round(p,1)}<{round(0.90*tp,1)} and kcal={round(kcal,1)}<{round(1.09*kcal_target,1)} -> scale protein x1.1")
                except Exception:
                    pass
                self._scale_by_macro(meal.ingredients, 'protein', 1.1)
                continue
            if tc > 0.0 and c < 0.90 * tc and kcal < 1.09 * kcal_target:
                try:
                    print(f"[REBALANCE] iter={it} trigger=under_carb c={round(c,1)}<{round(0.90*tc,1)} and kcal={round(kcal,1)}<{round(1.09*kcal_target,1)} -> scale carb x1.1")
                except Exception:
                    pass
                self._scale_by_macro(meal.ingredients, 'carb', 1.1)
                continue
            # If none matched, gently scale down overall calories if high
            if kcal > 1.09 * kcal_target:
                try:
                    print(f"[REBALANCE] iter={it} trigger=kcal_high_gentle kcal={round(kcal,1)}>{round(1.09*kcal_target,1)} -> scale fat x0.95, carb x0.97, protein x0.98")
                except Exception:
                    pass
                self._scale_by_macro(meal.ingredients, 'fat', 0.95)
                self._scale_by_macro(meal.ingredients, 'carb', 0.97)
                self._scale_by_macro(meal.ingredients, 'protein', 0.98)
                continue
            else:
                try:
                    print(f"[REBALANCE] iter={it} trigger=no_change break kcal={round(kcal,1)} p={round(p,1)} c={round(c,1)} f={round(f,1)}")
                except Exception:
                    pass
                break

        # Update totals into the meal
        totals = self._compute_meal_nutrition(meal.ingredients)
        meal.total_nutrition = {
            'calories': float(round(totals['calories'], 1)),
            'protein': float(round(totals['protein'], 1)),
            'carbs': float(round(totals['carbs'], 1)),
            'fat': float(round(totals['fat'], 1)),
        }
        try:
            if not accepted:
                print(f"[ACCEPT_FAIL] meal={meal.meal_name} final kcal={round(totals['calories'],1)} p={round(totals['protein'],1)} c={round(totals['carbs'],1)} f={round(totals['fat'],1)} vs targets kcal={round(kcal_target,1)} tp={round(float(macro_targets.get('protein',0.0) or 0.0),1)} tc={round(float(macro_targets.get('carb',0.0) or 0.0),1)} tf={round(float(macro_targets.get('fat',0.0) or 0.0),1)} band=±{int(band*100)}%")
            else:
                print(f"[REBALANCE_DONE] meal={meal.meal_name} kcal={round(totals['calories'],1)} p={round(totals['protein'],1)} c={round(totals['carbs'],1)} f={round(totals['fat'],1)}")
        except Exception:
            pass
        return meal
    
    # New: component-level rebalancer operating on (FoodItem, grams) before finalization
    def _components_contributions(self, components: List[Tuple[FoodItem, float]]) -> List[Dict]:
        out: List[Dict] = []
        for idx, (f, g) in enumerate(components):
            p_pg = float(getattr(f, 'protein_per_gram', 0.0) or 0.0)
            c_pg = float(getattr(f, 'carbs_per_gram', 0.0) or 0.0)
            f_pg = float(getattr(f, 'fat_per_gram', 0.0) or 0.0)
            k_pg = float(getattr(f, 'calories_per_gram', 0.0) or 0.0)
            out.append({
                'idx': idx, 'food': f, 'grams': g,
                'p_pg': p_pg, 'c_pg': c_pg, 'f_pg': f_pg, 'k_pg': k_pg,
                'p': p_pg * g, 'c': c_pg * g, 'f': f_pg * g, 'kcal': k_pg * g,
            })
        return out
    
    def _apply_grams(self, components: List[Tuple[FoodItem, float]], idx: int, new_g: float) -> None:
        f, _ = components[idx]
        g2 = max(0.0, float(new_g))
        components[idx] = (f, self._round_grams(g2))
    
    def _min_floor_for(self, food: FoodItem) -> float:
        name = (getattr(food, 'name', '') or '').lower()
        if 'oil' in name or 'ghee' in name or 'butter' in name:
            return 5.0
        return 25.0
    
    def _reduce_macro_over(self, components: List[Tuple[FoodItem, float]], macro: str, reduce_g: float) -> float:
        contrib = self._components_contributions(components)
        key = {'protein': 'p', 'carb':'c', 'fat':'f'}[macro]
        pg_key = {'protein': 'p_pg', 'carb':'c_pg', 'fat':'f_pg'}[macro]
        # Restrict to items whose dominant macro matches the target macro
        dom_matched = []
        for x in contrib:
            if self._dominant_macro_of_food(x['food']) == macro:
                dom_matched.append(x)
        if not dom_matched:
            try:
                if macro == 'fat':
                    print("[REBAL2_SKIP_FAT_NO_FAT_ITEM] reason=no_fat_dominant_item")
                else:
                    print(f"[REBAL2_SKIP_REDUCE_NO_DOM_MATCH] macro={macro}")
            except Exception:
                pass
            return reduce_g
        total_macro = sum(x[key] for x in dom_matched)
        if total_macro <= 0.0 or reduce_g <= 0.0:
            return 0.0
        remaining = float(reduce_g)
        # Iterative proportional reduction with floors
        for _ in range(3):
            if remaining <= 0.0:
                break
            shares = []
            avail_total = 0.0
            for x in dom_matched:
                pg = x[pg_key]
                if pg <= 0.0 or x['grams'] <= 0.0:
                    continue
                floor_g = self._min_floor_for(x['food'])
                reducible_g = max(0.0, x['grams'] - floor_g)
                reducible_macro = reducible_g * pg
                if reducible_macro > 0.0:
                    shares.append((x, reducible_macro))
                    avail_total += reducible_macro
            if avail_total <= 0.0:
                break
            for x, avail_macro in shares:
                if remaining <= 0.0:
                    break
                take_macro = min(avail_macro, remaining * (avail_macro / avail_total))
                delta_g = take_macro / max(1e-9, x[pg_key])
                new_g = max(self._min_floor_for(x['food']), x['grams'] - delta_g)
                self._apply_grams(components, x['idx'], new_g)
                try:
                    print(f"[REBAL2_MACRO_REDUCE] macro={macro} item={getattr(x['food'],'name','')} old_g={round(x['grams'],1)} new_g={round(new_g,1)} delta_g={round(x['grams']-new_g,1)}")
                except Exception:
                    pass
                remaining -= (x['grams'] - new_g) * x[pg_key]
                x['grams'] = new_g
            contrib = self._components_contributions(components)
            dom_matched = [x for x in contrib if self._dominant_macro_of_food(x['food']) == macro]
        return max(0.0, remaining)
    
    def _increase_macro_under(self, components: List[Tuple[FoodItem, float]], macro: str, add_g: float, kcal_headroom: float, meal_name: str) -> float:
        contrib = self._components_contributions(components)
        key = {'protein': 'p', 'carb':'c', 'fat':'f'}[macro]
        pg_key = {'protein': 'p_pg', 'carb':'c_pg', 'fat':'f_pg'}[macro]
        if add_g <= 0.0 or kcal_headroom <= 0.0:
            return 0.0
        # Prefer items with higher macro per kcal, restricted to dominant macro == target
        cand = [x for x in contrib if x[pg_key] > 0.0 and x['k_pg'] > 0.0 and self._dominant_macro_of_food(x['food']) == macro]
        if not cand:
            try:
                print(f"[REBAL2_SKIP_INCREASE_NO_DOM_MATCH] macro={macro}")
            except Exception:
                pass
            return add_g
        cand.sort(key=lambda x: (x[pg_key] / x['k_pg']), reverse=True)
        remaining_macro = float(add_g)
        remaining_kcal = float(kcal_headroom)
        for x in cand:
            if remaining_macro <= 0.0 or remaining_kcal <= 0.0:
                break
            pg = x[pg_key]; kpg = x['k_pg']
            # Rough cap: do not exceed portion sanity for dominant macro
            dom = self._dominant_macro_of_food(x['food'])
            cap = portion_sanity_cap_grams(dom)
            max_g = max(0.0, cap - x['grams'])
            if max_g <= 0.0:
                continue
            need_g_by_macro = remaining_macro / pg
            need_g_by_kcal = remaining_kcal / kpg
            add_here = min(max_g, need_g_by_macro, need_g_by_kcal)
            if add_here <= 0.0:
                continue
            new_g = x['grams'] + add_here
            self._apply_grams(components, x['idx'], new_g)
            try:
                print(f"[REBAL2_MACRO_INCREASE] macro={macro} item={getattr(x['food'],'name','')} old_g={round(x['grams'],1)} new_g={round(new_g,1)} add_g={round(add_here,1)}")
            except Exception:
                pass
            remaining_macro -= add_here * pg
            remaining_kcal -= add_here * kpg
            x['grams'] = new_g
        return remaining_macro
    
    def _reduce_kcal_over(
        self,
        components: List[Tuple[FoodItem, float]],
        over_kcal: float,
        macro_consumed: Dict[str, float] | None = None,
        macro_targets: Dict[str, float] | None = None,
    ) -> float:
        contrib = self._components_contributions(components)
        if over_kcal <= 0.0:
            return 0.0
        remaining = float(over_kcal)
        # Determine which dominant macros are over target (if provided)
        over_macros: Set[str] = set()
        if macro_consumed and macro_targets:
            try:
                if macro_consumed.get('protein', 0.0) > (macro_targets.get('protein', 0.0) or 0.0):
                    over_macros.add('protein')
                if macro_consumed.get('carb', 0.0) > (macro_targets.get('carb', 0.0) or 0.0):
                    over_macros.add('carb')
                if macro_consumed.get('fat', 0.0) > (macro_targets.get('fat', 0.0) or 0.0):
                    over_macros.add('fat')
            except Exception:
                over_macros = set()
        # Weight fat-dominant items slightly higher; limit to overage macro sources when possible
        weights = []
        avail_total = 0.0
        for x in contrib:
            kpg = x['k_pg']
            if kpg <= 0.0 or x['grams'] <= 0.0:
                continue
            floor_g = self._min_floor_for(x['food'])
            reducible_g = max(0.0, x['grams'] - floor_g)
            reducible_kcal = reducible_g * kpg
            if reducible_kcal <= 0.0:
                continue
            dom = self._dominant_macro_of_food(x['food'])
            # If over_macros is non-empty, only consider items whose dominant macro is over
            if over_macros and dom not in over_macros:
                continue
            w = 1.25 if dom == 'fat' else 1.0
            weights.append((x, reducible_kcal, w))
            avail_total += reducible_kcal * w
        if avail_total <= 0.0:
            # Fallback: if no overage macro sources, try fat-dominant only
            weights = []
            avail_total = 0.0
            for x in contrib:
                if self._dominant_macro_of_food(x['food']) != 'fat':
                    continue
                kpg = x['k_pg']
                if kpg <= 0.0 or x['grams'] <= 0.0:
                    continue
                floor_g = self._min_floor_for(x['food'])
                reducible_g = max(0.0, x['grams'] - floor_g)
                reducible_kcal = reducible_g * kpg
                if reducible_kcal <= 0.0:
                    continue
                weights.append((x, reducible_kcal, 1.25))
                avail_total += reducible_kcal * 1.25
            if avail_total <= 0.0:
                try:
                    print("[REBAL2_SKIP_KCAL_NO_OVERAGE_SOURCES]")
                except Exception:
                    pass
                return remaining
        for x, avail_k, w in weights:
            if remaining <= 0.0:
                break
            take_k = min(avail_k, remaining * ((avail_k * w) / avail_total))
            delta_g = take_k / max(1e-9, x['k_pg'])
            new_g = max(self._min_floor_for(x['food']), x['grams'] - delta_g)
            self._apply_grams(components, x['idx'], new_g)
            try:
                print(f"[REBAL2_KCAL_REDUCE] item={getattr(x['food'],'name','')} old_g={round(x['grams'],1)} new_g={round(new_g,1)} delta_g={round(x['grams']-new_g,1)}")
            except Exception:
                pass
            remaining -= (x['grams'] - new_g) * x['k_pg']
            x['grams'] = new_g
        return max(0.0, remaining)

    # ------------------------ helpers ------------------------
    def _build_planner_context(
        self,
        user,
        goal: str,
        macro_order: List[str],
        meal_names: List[str],
        allowed_map: Dict[str, Dict[str, List[FoodItem]]],
        piece_weights: Dict[str, float],
        common_veggies: List[FoodItem],
        common_fruits: List[FoodItem],
    ) -> PlannerContext:
        return PlannerContext(
            user=user,
            goal=goal,
            macro_order=macro_order,
            meal_names=meal_names,
            allowed_map=allowed_map,
            piece_weights=piece_weights,
            common_veggies=common_veggies,
            common_fruits=common_fruits,
        )

    def _prepare_day(
        self,
        ctx: PlannerContext,
        current_date,
        daily_kcal: float,
        snack_count: int,
        meals: List[str],
        used_in_window: Dict[str, Set[int]],
        used_names_window: Dict[str, Set[str]],
        no_repeat_days: int,
    ) -> DayContext:
        # Recent history (ids and normalized names)
        try:
            ids_by_meal, norm_names_by_meal = self._get_recent_food_history(days=no_repeat_days, until=current_date)
        except Exception:
            ids_by_meal = {m: set() for m in ("Breakfast", "Lunch", "Dinner", "Snack")}
            norm_names_by_meal = {m: set() for m in ("Breakfast", "Lunch", "Dinner", "Snack")}
        recent_ids = {}
        recent_names = {}
        for m in ("Breakfast", "Lunch", "Dinner", "Snack"):
            recent_ids[m] = set(ids_by_meal.get(m, set())) | set(used_in_window.get(m, set()))
            recent_names[m] = set(norm_names_by_meal.get(m, set())) | set(used_names_window.get(m, set()))

        # Kcal allocation
        snack_kcal = 200.0 if snack_count else 0.0
        meal_kcal_budget = max(0.0, daily_kcal - snack_kcal)
        if getattr(settings, 'DIET_DYNAMIC_MEAL_ALLOCATION', False):
            split = goal_meal_kcal_split(ctx.goal)
            meal_distribution = {m: split.get(m, 1.0/len(meals)) for m in meals}
        else:
            meal_distribution = self._choose_distribution_for_goal_value(ctx.goal, meals)
        per_meal_kcal = {m: meal_kcal_budget * meal_distribution[m] for m in meals}

        # Macro targets per meal
        ratios = self._macro_ratios_for_goal_value(ctx.goal)
        protein_target = daily_kcal * ratios["protein"] / 4.0
        carb_target = daily_kcal * ratios["carb"] / 4.0
        fat_target = daily_kcal * ratios["fat"] / 9.0
        meal_targets: Dict[str, MealTarget] = {}
        for m in meals:
            meal_targets[m] = MealTarget(
                name=m,
                kcal_target=per_meal_kcal[m],
                macro_targets={
                    "protein": protein_target * meal_distribution[m],
                    "carb": carb_target * meal_distribution[m],
                    "fat": fat_target * meal_distribution[m],
                },
            )
        return DayContext(
            date=current_date,
            meal_kcal=per_meal_kcal,
            meal_distribution=meal_distribution,
            meal_targets=meal_targets,
            recent_exclusions_ids=recent_ids,
            recent_exclusions_names=recent_names,
            used_in_window=used_in_window,
            used_names_window=used_names_window,
        )
    def _recompute_meal_totals(self, components: List[Tuple[FoodItem, float]]) -> Tuple[float, Dict[str, float]]:
        kcal_consumed = sum(g * float(getattr(f, "calories_per_gram", 0.0) or 0.0) for f, g in components)
        macro_consumed = {
            "protein": sum(g * self._macro_per_gram(f, "protein") for f, g in components),
            "carb": sum(g * self._macro_per_gram(f, "carb") for f, g in components),
            "fat": sum(g * self._macro_per_gram(f, "fat") for f, g in components),
        }
        return kcal_consumed, macro_consumed

    def _build_snack(self, allowed: Dict[str, Dict[str, List[FoodItem]]], used_in_window: Dict[str, Set[int]]) -> List[AIIngredient]:
        snack_ing: List[AIIngredient] = []
        for macro in ("fat", "protein", "carb"):
            cands = allowed.get("Snack", {}).get(macro, [])
            if not cands:
                continue
            food = sorted(cands, key=lambda f: self._macro_density_per_kcal(f, macro), reverse=True)[0]
            kcal_pg = float(getattr(food, "calories_per_gram", 0.0) or 0.0)
            if kcal_pg <= 0.0:
                cals_per_serv = float(getattr(food, "calories", 0.0) or 0.0)
                serv_g = float(getattr(food, "serving_size_grams", 100.0) or 100.0)
                kcal_pg = (cals_per_serv / serv_g) if serv_g > 0 else 0.0
            grams = 200.0 / kcal_pg if kcal_pg > 0 else 0.0
            if grams > 300.0:
                grams = 300.0
            grams = self._round_grams(grams)
            if grams > 0:
                snack_ing.append(AIIngredient(name=food.name, quantity=f"{int(grams)}g"))
                used_in_window.setdefault("Snack", set()).add(food.id)
                break
        return snack_ing

    def _staged_fill(self, meal_name: str, ctx: PlannerContext, day_ctx: DayContext, recent_set: Set[int]) -> List[Tuple[FoodItem, float]]:
        if not getattr(settings, 'DIET_STAGED_MEAL_FILL', False):
            return []
        pools = {
            'protein': ctx.allowed_map.get(meal_name, {}).get('protein', []),
            'carb': ctx.allowed_map.get(meal_name, {}).get('carb', []),
            'fat': ctx.allowed_map.get(meal_name, {}).get('fat', []),
        }
        mt = MealTargets(
            kcal_target=day_ctx.meal_kcal[meal_name],
            protein_g=day_ctx.meal_targets[meal_name].macro_targets['protein'],
            carb_g=day_ctx.meal_targets[meal_name].macro_targets['carb'],
            fat_g=day_ctx.meal_targets[meal_name].macro_targets['fat'],
        )
        try:
            return StagedMealFiller().fill(meal_name, mt, pools, recent_set)
        except Exception:
            return []

    def _plan_meal(self, meal_name: str, ctx: PlannerContext, day_ctx: DayContext) -> AIMeal:
        try:
            components: List[Tuple[FoodItem, float]] = []
            used_macro_counts = {"protein": 0, "carb": 0, "fat": 0, "vegetable": 0, "fruit": 0}
            kcal_consumed = 0.0
            macro_consumed = {"protein": 0.0, "carb": 0.0, "fat": 0.0}
            meal_existing_ids: Set[int] = set()
            meal_existing_names: Set[str] = set()

            # Staged fill
            recent_set = day_ctx.recent_exclusions_ids.get(meal_name, set())
            staged = self._staged_fill(meal_name, ctx, day_ctx, recent_set)
            components.extend(staged)
            if components:
                kcal_consumed, macro_consumed = self._recompute_meal_totals(components)
                for f, _g in components:
                    meal_existing_ids.add(getattr(f, 'id', 0))
                    meal_existing_names.add(self._normalize_name_for_repeat(getattr(f, 'name', '') or ''))
                for f, _g in components:
                    dom = self._dominant_macro_of_food(f)
                    used_macro_counts[dom] = used_macro_counts.get(dom, 0) + 1
                day_ctx.recent_exclusions_ids.setdefault(meal_name, set()).update(meal_existing_ids)
                day_ctx.recent_exclusions_names.setdefault(meal_name, set()).update(meal_existing_names)
                for f, _g in components:
                    day_ctx.used_in_window.setdefault(meal_name, set()).add(getattr(f, 'id', 0))
                    day_ctx.used_names_window.setdefault(meal_name, set()).add(self._normalize_name_for_repeat(getattr(f, 'name', '')))

            # Prefill protein
            protein_floor_g = 40.0 if meal_name.lower() in ("lunch", "dinner") else 35.0
            carb_floor_g = 50.0 if (ctx.goal in ("gain", "maintain") or meal_name.lower() in ("breakfast", "lunch")) else 0.0
            protein_present = any(self._dominant_macro_of_food(f) == 'protein' for f, _g in components)
            pre_need_protein = 0.0 if protein_present else max(0.0, protein_floor_g)
            if pre_need_protein > 0.0:
                adjusted_kcal_target = day_ctx.meal_targets[meal_name].kcal_target
                if ctx.goal in ("maintain", "gain") and meal_name == "Dinner":
                    adjusted_kcal_target = max(0.0, adjusted_kcal_target - 50.0 * 4.0)
                self._add_macro_component(
                    meal_name,
                    "protein",
                    pre_need_protein,
                    adjusted_kcal_target,
                    ctx.allowed_map,
                    used_macro_counts,
                    macro_consumed,
                    components,
                    kcal_consumed,
                    day_ctx.recent_exclusions_ids,
                    day_ctx.recent_exclusions_names,
                    day_ctx.used_names_window,
                    macro_cap=1,
                    existing_ids=meal_existing_ids,
                )
                kcal_consumed, macro_consumed = self._recompute_meal_totals(components)

            # Prefill carb exactly one
            if carb_floor_g > 0.0:
                pre_need_carb = max(0.0, carb_floor_g - macro_consumed["carb"])
                if pre_need_carb > 0.0:
                    self._add_macro_component(
                        meal_name,
                        "carb",
                        pre_need_carb,
                        day_ctx.meal_targets[meal_name].kcal_target,
                        ctx.allowed_map,
                        used_macro_counts,
                        macro_consumed,
                        components,
                        kcal_consumed,
                        day_ctx.recent_exclusions_ids,
                        day_ctx.recent_exclusions_names,
                        day_ctx.used_names_window,
                        macro_cap=1,
                        existing_ids=meal_existing_ids,
                        gram_cap_override=350.0,
                    )
                    kcal_consumed, macro_consumed = self._recompute_meal_totals(components)
                
            # Vegetables
            veg_added = self._add_vegetables_to_meal(
                meal_name,
                ctx.allowed_map,
                components,
                meal_existing_ids,
                meal_existing_names,
                day_ctx.recent_exclusions_ids,
                day_ctx.recent_exclusions_names,
                day_ctx.used_in_window,
                day_ctx.used_names_window,
            )
            if veg_added:
                kcal_consumed, macro_consumed = self._recompute_meal_totals(components)

            # Macro selection
            for idx, macro in enumerate(ctx.macro_order):
                if macro == "carb" and ctx.goal == "lose" and meal_name.lower() == "dinner":
                    continue
                target = day_ctx.meal_targets[meal_name].macro_targets[macro]
                if idx < 2:
                    target *= 0.9
                remaining = max(0.0, target - macro_consumed[macro])
                if remaining <= 0.0:
                    continue
                candidates = ctx.allowed_map.get(meal_name, {}).get(macro, [])
                if not candidates:
                    continue
                exclude_ids = meal_existing_ids | set(day_ctx.recent_exclusions_ids.get(meal_name, set()))
                # ID-only recency filtering (no name-based exclusions)
                candidates = [f for f in candidates if f.id not in exclude_ids]
                if not candidates:
                    continue
                use_smart = getattr(settings, 'DIET_SMART_MACRO_PLANNER', False)
                if use_smart:
                    ranked = self._smart_rank_candidates(
                        meal_name=meal_name,
                        macro=macro,
                        candidates=candidates,
                        macro_consumed=macro_consumed,
                        meal_target=day_ctx.meal_targets[meal_name],
                        kcal_consumed=kcal_consumed,
                        goal=ctx.goal,
                    )
                else:
                    ranked = [
                        {'food': f, 'score': self._macro_density_per_kcal(f, macro), 'penalty': 0.0, 'grams': None}
                        for f in sorted(candidates, key=lambda f: self._macro_density_per_kcal(f, macro), reverse=True)
                    ]
                cap_per_macro = {'protein': 1, 'carb': 1, 'fat': 0 if ctx.goal == 'lose' else 1}
                if cap_per_macro.get(macro, 0) - used_macro_counts.get(macro, 0) <= 0:
                    continue
                selected_name = None
                for item in ranked:
                    food = item['food']
                    if ctx.goal == 'lose' and macro == 'fat':
                        continue
                    if used_macro_counts[macro] >= cap_per_macro.get(macro, 0):
                        break
                    macro_per_g = self._macro_per_gram(food, macro)
                    kcal_per_g = float(getattr(food, "calories_per_gram", 0.0) or 0.0)
                    if macro_per_g <= 0.0 or kcal_per_g <= 0.0:
                        continue
                    if use_smart:
                        grams = item.get('grams') or 0.0
                    else:
                        remaining_kcal = max(0.0, day_ctx.meal_targets[meal_name].kcal_target - kcal_consumed)
                        grams = self._compute_grams_for_pick(food, macro, remaining, remaining_kcal, ctx.goal, None)
                    grams = self._snap_to_piece_grams_if_applicable(food, grams)
                    if grams <= 0:
                        continue
                    if any(getattr(f0, 'id', None) == getattr(food, 'id', None) for f0,_ in components):
                        continue
                    components.append((food, grams))
                    used_macro_counts[macro] += 1
                    selected_name = food.name
                    meal_existing_ids.add(food.id)
                    meal_existing_names.add(self._normalize_name_for_repeat(food.name))
                    day_ctx.used_in_window.setdefault(meal_name, set()).add(food.id)
                    # Keep names window for observability, but do not use for filtering
                    day_ctx.used_names_window.setdefault(meal_name, set()).add(self._normalize_name_for_repeat(food.name))
                    kcal_add = grams * kcal_per_g
                    kcal_consumed += kcal_add
                    macro_consumed[macro] += grams * macro_per_g
                    macro_consumed['protein'] += grams * self._macro_per_gram(food, 'protein') if macro != 'protein' else 0.0
                    macro_consumed['carb'] += grams * self._macro_per_gram(food, 'carb') if macro != 'carb' else 0.0
                    macro_consumed['fat'] += grams * self._macro_per_gram(food, 'fat') if macro != 'fat' else 0.0
                    try:
                        safe_json_log(stage="pick", data={
                            'meal_type': meal_name,
                            'macro': macro,
                            'food': food.name,
                            'grams': round(grams,1),
                            'kcal_add': round(kcal_add,1),
                            'kcal_consumed': round(kcal_consumed,1),
                            'macro_consumed': {k: round(v,1) for k,v in macro_consumed.items()},
                            'target': round(target,1),
                            'remaining': round(max(0.0, target - macro_consumed[macro]),1),
                        }, logger_name='diet')
                    except Exception:
                        pass
                    remaining = max(0.0, target - macro_consumed[macro])
                    if remaining <= 0.0:
                        break
                if use_smart and ranked:
                    top = ranked[:5]
                    log_payload = {
                        "meal_type": meal_name,
                        "macro": macro,
                        "candidates": [
                            {"food": it['food'].name, "eff_score": round(float(it.get('score', 0.0)), 4), "penalty": round(float(it.get('penalty', 0.0)), 4)}
                            for it in top
                        ],
                        "selected": selected_name,
                        "goal": ctx.goal,
                    }
                    safe_json_log(stage="macro_selection", data=log_payload, logger_name='diet')
                    self._smart_summary.append(log_payload)

            # Dinner safeguard
            if meal_name == "Dinner" and ctx.goal in ("maintain", "gain"):
                dinner_carb_floor = 30.0
                if macro_consumed['carb'] < dinner_carb_floor:
                    deficit = dinner_carb_floor - macro_consumed['carb']
                    carb_idx = next((i for i, (f, g) in enumerate(components) if self._dominant_macro_of_food(f) == 'carb'), None)
                    if carb_idx is not None:
                        fcarb, gold = components[carb_idx]
                        c_pg = self._macro_per_gram(fcarb, 'carb')
                        kcal_pg = float(getattr(fcarb, 'calories_per_gram', 0.0) or 0.0)
                        rem_kcal = max(0.0, day_ctx.meal_targets[meal_name].kcal_target - kcal_consumed)
                        add_g = 0.0
                        if c_pg > 0.0 and kcal_pg > 0.0 and rem_kcal > 0.0:
                            add_g = min(deficit / c_pg, rem_kcal / kcal_pg)
                        dom = self._dominant_macro_of_food(fcarb)
                        max_total = portion_sanity_cap_grams(dom)
                        new_total = min(gold + add_g, max_total)
                        new_total = self._snap_to_piece_grams_if_applicable(fcarb, new_total)
                        new_total = self._round_grams(new_total)
                        delta_g = max(0.0, new_total - gold)
                        if delta_g > 0.0:
                            components[carb_idx] = (fcarb, new_total)
                            kcal_consumed += delta_g * kcal_pg
                            macro_consumed['carb'] += delta_g * c_pg
                    else:
                        self._add_macro_component(
                            meal_name,
                            'carb',
                            deficit,
                            day_ctx.meal_targets[meal_name].kcal_target,
                            ctx.allowed_map,
                            used_macro_counts,
                            macro_consumed,
                            components,
                            kcal_consumed,
                            day_ctx.recent_exclusions_ids,
                            day_ctx.recent_exclusions_names,
                            day_ctx.used_names_window,
                            macro_cap=1,
                            existing_ids=meal_existing_ids,
                        )
                    kcal_consumed, macro_consumed = self._recompute_meal_totals(components)

            # Post-pass rebalancing on components (gradient-based, ID-centric)
            try:
                mt = day_ctx.meal_targets[meal_name]
                band = 0.09
                for it in range(6):
                    kcal_consumed, macro_consumed = self._recompute_meal_totals(components)
                    kcal_t = float(mt.kcal_target or 0.0)
                    tp = float(mt.macro_targets.get('protein', 0.0) or 0.0)
                    tc = float(mt.macro_targets.get('carb', 0.0) or 0.0)
                    tf = float(mt.macro_targets.get('fat', 0.0) or 0.0)
                    ok = (
                        self._within_band(kcal_consumed, kcal_t, band) and
                        self._within_band(macro_consumed['protein'], tp, band) and
                        self._within_band(macro_consumed['carb'], tc, band) and
                        self._within_band(macro_consumed['fat'], tf, band)
                    )
                    try:
                        print(f"[REBAL2_START] meal={meal_name} iter={it} kcal={round(kcal_consumed,1)} p={round(macro_consumed['protein'],1)} c={round(macro_consumed['carb'],1)} f={round(macro_consumed['fat'],1)} targets kcal={round(kcal_t,1)} tp={round(tp,1)} tc={round(tc,1)} tf={round(tf,1)} band=±{int(band*100)}%")
                    except Exception:
                        pass
                    if ok:
                        print(f"[REBAL2_ACCEPT] meal={meal_name} iter={it}")
                        break
                    # Overages first: choose biggest deviation by Kcal among macros, then overall kcal
                    over_k_p = max(0.0, macro_consumed['protein'] - tp) * 4.0
                    over_k_c = max(0.0, macro_consumed['carb'] - tc) * 4.0
                    over_k_f = max(0.0, macro_consumed['fat'] - tf) * 9.0
                    dev_over = [('protein', over_k_p), ('carb', over_k_c), ('fat', over_k_f)]
                    dev_over.sort(key=lambda x: x[1], reverse=True)
                    acted = False
                    did_reduce = False
                    if dev_over and dev_over[0][1] > 0.0:
                        mac = dev_over[0][0]
                        try:
                            print(f"[REBAL2_CHOSEN_MACRO] meal={meal_name} iter={it} chosen={mac} over_k={round(dev_over[0][1],1)} pk={round(over_k_p,1)} ck={round(over_k_c,1)} fk={round(over_k_f,1)}")
                        except Exception:
                            pass
                        # Reduce grams for the macro with largest kcal overage
                        over_g = macro_consumed[mac] - {'protein': tp, 'carb': tc, 'fat': tf}[mac]
                        if over_g > 0.0:
                            _ = self._reduce_macro_over(components, mac, over_g * 0.7)
                            acted = True
                            did_reduce = True
                    # If no macro selected OR still over total kcal, reduce kcal using overage sources only
                    if not acted and kcal_consumed > 1.10 * kcal_t:
                        _ = self._reduce_kcal_over(
                            components,
                            (kcal_consumed - kcal_t) * 0.7,
                            macro_consumed={'protein': macro_consumed['protein'], 'carb': macro_consumed['carb'], 'fat': macro_consumed['fat']},
                            macro_targets={'protein': tp, 'carb': tc, 'fat': tf},
                        )
                        acted = True
                    # Recompute after any reductions to evaluate deficits
                    if did_reduce:
                        kcal_consumed, macro_consumed = self._recompute_meal_totals(components)
                    # Under targets with headroom (allow increase even if a reduction happened in this iteration)
                    # Trigger increase if kcal is below lower band OR any key macro is below 90% target
                    if (kcal_consumed < (1.0 - band) * kcal_t) or (macro_consumed['protein'] < 0.90 * tp) or (macro_consumed['carb'] < 0.90 * tc):
                        # Choose biggest kcal deficit among macros
                        def_k_p = max(0.0, tp - macro_consumed['protein']) * 4.0
                        def_k_c = max(0.0, tc - macro_consumed['carb']) * 4.0
                        def_k_f = max(0.0, tf - macro_consumed['fat']) * 9.0
                        dev_under = [('protein', def_k_p), ('carb', def_k_c), ('fat', def_k_f)]
                        dev_under.sort(key=lambda x: x[1], reverse=True)
                        
                        # FIX: Try all under-target macros in order if previous one can't be increased
                        for macu, deficit_kcal in dev_under:
                            if deficit_kcal <= 0.0:
                                continue
                            try:
                                print(f"[REBAL2_CHOSEN_INCREASE] meal={meal_name} iter={it} chosen={macu} under_k={round(deficit_kcal,1)} pk={round(def_k_p,1)} ck={round(def_k_c,1)} fk={round(def_k_f,1)}")
                            except Exception:
                                pass
                            
                            # FIX: Target 100% of target, not 90%
                            target_map = {'protein': tp, 'carb': tc, 'fat': tf}
                            macro_deficit = max(0.0, target_map[macu] - macro_consumed[macu])
                            kcal_headroom = max(0.0, kcal_t - kcal_consumed)
                            
                            if macro_deficit > 0.0 and kcal_headroom > 0.0:
                                result = self._increase_macro_under(
                                    components,
                                    macu,
                                    macro_deficit * 0.8,  # Try to fill 80% of deficit
                                    kcal_headroom,
                                    meal_name
                                )
                                # If we successfully increased something (result < input), mark acted
                                if result < macro_deficit * 0.8:
                                    acted = True
                                    break  # Successfully increased, move to next iteration
                                else:
                                    print(f"[REBAL2_INCREASE_FAILED] meal={meal_name} iter={it} macro={macu} - no items to increase, trying next macro")
                    if not acted:
                        print(f"[REBAL2_STOP] meal={meal_name} iter={it} no_action")
                        break
                # recompute after rebalancing
                kcal_consumed, macro_consumed = self._recompute_meal_totals(components)
                print(f"[REBAL2_DONE] meal={meal_name} kcal={round(kcal_consumed,1)} p={round(macro_consumed['protein'],1)} c={round(macro_consumed['carb'],1)} f={round(macro_consumed['fat'],1)}")
            except Exception:
                pass

            # Fallback if empty
            if not components and getattr(settings, 'DIET_DYNAMIC_MEAL_ALLOCATION', False):
                fallback_items = self._fallback_safe_set(meal_name)
                protein_added = False
                for food in fallback_items:
                    dom = self._dominant_macro_of_food(food)
                    if dom == 'protein' and protein_added:
                        continue
                    grams = min(100.0 if dom != 'fat' else 20.0, portion_sanity_cap_grams(dom))
                    if dom == 'carb':
                        grams = min(grams, 400.0)
                    grams = self._round_grams(grams)
                    components.append((food, grams))
                    if dom == 'protein':
                        protein_added = True
                self._smart_summary.append({"meal_type": meal_name, "used_fallback": True, "fallback_items": [f.name for f,_ in components]})
                safe_json_log(stage="fallback_meal", data=self._smart_summary[-1], logger_name='diet')
                kcal_consumed, macro_consumed = self._recompute_meal_totals(components)

            return self._finalize_meal(meal_name, components, day_ctx.meal_targets, kcal_consumed, macro_consumed)

        except Exception as e:
            # Hard fallback: log and return a minimal safe meal
            try:
                safe_json_log(stage="plan_meal_error", data={"meal_type": meal_name, "error": str(e), "error_type": type(e).__name__}, logger_name='diet')
            except Exception:
                pass
            components: List[Tuple[FoodItem, float]] = []
            try:
                fallback_items = self._fallback_safe_set(meal_name)
            except Exception:
                fallback_items = []
            for food in fallback_items:
                dom = self._dominant_macro_of_food(food)
                grams = min(100.0 if dom != 'fat' else 20.0, portion_sanity_cap_grams(dom))
                if dom == 'carb':
                    grams = min(grams, 400.0)
                grams = self._round_grams(grams)
                components.append((food, grams))
                try:
                    day_ctx.used_in_window.setdefault(meal_name, set()).add(getattr(food, 'id', 0))
                    day_ctx.used_names_window.setdefault(meal_name, set()).add(self._normalize_name_for_repeat(getattr(food, 'name', '') or ''))
                except Exception:
                    pass
            kcal_consumed, macro_consumed = self._recompute_meal_totals(components)
            return self._finalize_meal(meal_name, components, day_ctx.meal_targets, kcal_consumed, macro_consumed)

    def _finalize_meal(self, meal_name: str, components: List[Tuple[FoodItem, float]], meal_targets: Dict[str, MealTarget], kcal_consumed: float, macro_consumed: Dict[str, float]) -> AIMeal:
        try:
            safe_json_log(stage="meal_summary", data={
                'meal_type': meal_name,
                'kcal_consumed': round(kcal_consumed,1),
                'macro_consumed': {k: round(v,1) for k,v in macro_consumed.items()},
                'target_per_macro': {k: round(v,1) for k,v in meal_targets[meal_name].macro_targets.items()},
                'kcal_target': round(meal_targets[meal_name].kcal_target,1),
                'components': [f"{f.name}:{int(g)}g" for f,g in components],
            }, logger_name='diet')
        except Exception:
            pass
        ingredients = [AIIngredient(name=f.name, quantity=f"{int(g)}g") for (f, g) in components]
        return AIMeal(
            meal_name=meal_name,
            description=f"{meal_name} planned by rule-based system",
            ingredients=ingredients,
            total_nutrition={"calories": 0.0, "protein": 0.0, "carbs": 0.0, "fat": 0.0},
            meal_type=meal_name,
        )
    def _macro_ratios_for_goal(self) -> Dict[str, float]:
        goal = self._resolve_goal()
        if "lose" in goal:
            return {"protein": 0.35, "carb": 0.40, "fat": 0.25}
        if "gain" in goal:
            return {"protein": 0.25, "carb": 0.55, "fat": 0.20}
        return {"protein": 0.30, "carb": 0.50, "fat": 0.20}

    def _macro_ratios_for_goal_value(self, goal: str) -> Dict[str, float]:
        g = (goal or '').lower()
        if 'lose' in g:
            return {"protein": 0.35, "carb": 0.40, "fat": 0.25}
        if 'gain' in g:
            return {"protein": 0.25, "carb": 0.55, "fat": 0.20}
        return {"protein": 0.30, "carb": 0.50, "fat": 0.20}

    def _macro_priority_order(self) -> List[str]:
        goal = self._resolve_goal()
        if "lose" in goal:
            return ["protein", "carb", "fat"]
        if "gain" in goal:
            return ["carb", "protein", "fat"]
        return ["protein", "carb", "fat"]

    def _choose_distribution_for_goal(self, meals: List[str]) -> Dict[str, float]:
        goal = self._resolve_goal()
        # Percent patterns
        if "gain" in goal:
            pattern = [0.40, 0.40, 0.20]
        elif "lose" in goal:
            pattern = [0.30, 0.40, 0.30]
        else:
            pattern = [0.35, 0.35, 0.30]
        distribution: Dict[str, float] = {}
        for idx, m in enumerate(meals):
            distribution[m] = pattern[idx] if idx < len(pattern) else pattern[-1]
        return distribution

    def _choose_distribution_for_goal_value(self, goal: str, meals: List[str]) -> Dict[str, float]:
        g = (goal or '').lower()
        if 'gain' in g:
            pattern = [0.40, 0.40, 0.20]
        elif 'lose' in g:
            pattern = [0.30, 0.40, 0.30]
        else:
            pattern = [0.35, 0.35, 0.30]
        distribution: Dict[str, float] = {}
        for idx, m in enumerate(meals):
            distribution[m] = pattern[idx] if idx < len(pattern) else pattern[-1]
        return distribution

    def _build_allowed_foods_map(self) -> Dict[str, Dict[str, List[FoodItem]]]:
        out: Dict[str, Dict[str, List[FoodItem]]] = {
            m: {"protein": [], "carb": [], "fat": [], "vegetable": [], "fruit": []} 
            for m in ("Breakfast", "Lunch", "Dinner", "Snack")
        }
        # BUG FIX: Optimize query with select_related to avoid N+1
        qs = UserFoodCategoryPreference.objects.filter(user=self.user).select_related("food", "food__category")
        for rec in qs:
            name = rec.meal
            macro = rec.macro
            if name in out and macro in out[name]:
                out[name][macro].append(rec.food)
        # Fill missing from UserFoodPreference when category prefs are absent
        try:
            from ..models import UserFoodPreference
            pref = UserFoodPreference.objects.get(user=self.user)
        except Exception:
            pref = None
        def _extend_if_empty(meal_key: str, macro_key: str, items: List[FoodItem]):
            if not out[meal_key][macro_key] and items:
                out[meal_key][macro_key].extend(items)
        if pref:
            try:
                proteins = list(pref.protein_choices.all())
            except Exception:
                proteins = []
            try:
                carbs = list(pref.carb_choices.all())
            except Exception:
                carbs = []
            try:
                fats = list(pref.fat_choices.all())
            except Exception:
                fats = []
            try:
                vegs = list(pref.vegetable_choices.all())
            except Exception:
                vegs = []
            try:
                fruits = list(pref.fruit_choices.all())
            except Exception:
                fruits = []
            for meal_key in ("Breakfast", "Lunch", "Dinner", "Snack"):
                _extend_if_empty(meal_key, 'protein', proteins)
                _extend_if_empty(meal_key, 'carb', carbs)
                _extend_if_empty(meal_key, 'fat', fats)
                _extend_if_empty(meal_key, 'vegetable', vegs)
                _extend_if_empty(meal_key, 'fruit', fruits)
        # Deduplicate by name order-preserving
        for m in out:
            for mac in out[m]:
                seen = set()
                unique: List[FoodItem] = []
                for f in out[m][mac]:
                    if f.name not in seen:
                        seen.add(f.name)
                        unique.append(f)
                out[m][mac] = unique
        return out

    def _get_recent_food_ids(self, meal_type: str, days: int, until) -> Set[int]:
        from datetime import timedelta as _timedelta
        from ..models import MealComponent, Meal
        since = until - _timedelta(days=days)
        qs = MealComponent.objects.filter(
            meal__diet_plan__user=self.user,
            meal__meal_type=meal_type,
            meal__date__gte=since,
            meal__date__lt=until,
        ).values_list('food_id', flat=True)
        return set(qs)

    def _get_recent_food_history(self, days: int, until) -> Tuple[Dict[str, Set[int]], Dict[str, Set[str]]]:
        """
        Batch query of recent MealComponents across all meals for the user, returning:
        - ids_by_meal: {meal_type: set(food_id)}
        - norm_names_by_meal: {meal_type: set(normalized_name)}
        """
        from datetime import timedelta as _timedelta
        from ..models import MealComponent, FoodItem
        ids_by_meal: Dict[str, Set[int]] = {m: set() for m in ("Breakfast", "Lunch", "Dinner", "Snack")}
        norm_names_by_meal: Dict[str, Set[str]] = {m: set() for m in ("Breakfast", "Lunch", "Dinner", "Snack")}
        since = until - _timedelta(days=days)
        rows = MealComponent.objects.filter(
            meal__diet_plan__user=self.user,
            meal__date__gte=since,
            meal__date__lt=until,
        ).values_list('meal__meal_type', 'food_id')
        all_ids: Set[int] = set()
        for meal_type, food_id in rows:
            if meal_type in ids_by_meal:
                ids_by_meal[meal_type].add(food_id)
                all_ids.add(food_id)
        id_to_name: Dict[int, str] = {}
        if all_ids:
            try:
                id_to_name = {fid: name for fid, name in FoodItem.objects.filter(id__in=list(all_ids)).values_list('id', 'name')}
            except Exception:
                id_to_name = {}
        for m in ids_by_meal:
            norm_names_by_meal[m] = set(self._normalize_name_for_repeat(id_to_name.get(fid, '') or '') for fid in ids_by_meal[m])
        return ids_by_meal, norm_names_by_meal

    def _add_macro_component(
        self,
        meal_name: str,
        macro: str,
        need_grams: float,
        meal_kcal_target: float,
        allowed: Dict[str, Dict[str, List[FoodItem]]],
        used_macro_counts: Dict[str, int],
        macro_consumed: Dict[str, float],
        components: List[Tuple[FoodItem, float]],
        kcal_consumed: float,
        recent_exclusions: Dict[str, Set[int]],
        recent_exclusions_names: Dict[str, Set[str]],
        used_names_window: Dict[str, Set[str]],
        macro_cap: int = 2,
        gram_cap_override: float | None = None,
        existing_ids: Set[int] | None = None,
    ) -> None:
        if need_grams <= 0.0:
            return
        cands = allowed.get(meal_name, {}).get(macro, [])
        if not cands:
            # Fallback to DB staples by macro if user's allowed list empty
            cands = self._fallback_staples_for_macro(meal_name, macro)
            if not cands:
                return
        exclude_ids_local = set(recent_exclusions.get(meal_name, set()))
        existing_ids = existing_ids or set()
        # ID-only recency filtering
        cands = [f for f in cands if f.id not in exclude_ids_local and f.id not in existing_ids]
        if not cands:
            return
        # Sort by macro density per kcal
        cands_sorted = sorted(cands, key=lambda f: self._macro_density_per_kcal(f, macro), reverse=True)
        for food in cands_sorted:
            if used_macro_counts[macro] >= macro_cap:
                break
            remaining_kcal = max(0.0, meal_kcal_target - kcal_consumed)
            grams = self._compute_grams_for_pick(
                food=food,
                macro=macro,
                remaining_macro_g=need_grams,
                remaining_kcal=remaining_kcal,
                goal=self._resolve_goal(),
                gram_cap_override=gram_cap_override,
            )
            if grams <= 0:
                continue
            # Guard intra-meal duplicates
            if any(getattr(f0, 'id', None) == getattr(food, 'id', None) for f0,_ in components):
                continue
            components.append((food, grams))
            used_macro_counts[macro] += 1
            # Do not mutate caller's exclusion set; rely on existing_ids + used_names_window
            used_names_window.setdefault(meal_name, set()).add(self._normalize_name_for_repeat(food.name))
            macro_consumed[macro] += grams * self._macro_per_gram(food, macro)
            break

    def _fallback_staples_for_macro(self, meal_name: str, macro: str) -> List[FoodItem]:
        from ..models import FoodItem
        q = []
        try:
            if macro == 'protein':
                q = list(FoodItem.objects.filter(category__is_protein=True).order_by('-protein_per_gram')[:10])
            elif macro == 'carb':
                q = list(FoodItem.objects.filter(category__is_carb=True).order_by('-carbs_per_gram')[:10])
            else:
                q = list(FoodItem.objects.filter(category__is_fat=True).order_by('-fat_per_gram')[:10])
        except Exception:
            q = []
        return q

    def _macro_per_gram(self, food: FoodItem, macro: str) -> float:
        if macro == "protein":
            return float(getattr(food, "protein_per_gram", 0.0) or 0.0)
        if macro == "carb":
            return float(getattr(food, "carbs_per_gram", 0.0) or 0.0)
        return float(getattr(food, "fat_per_gram", 0.0) or 0.0)

    def _macro_density_per_kcal(self, food: FoodItem, macro: str) -> float:
        macro_pg = self._macro_per_gram(food, macro)
        kcal_pg = float(getattr(food, "calories_per_gram", 0.0) or 0.0)
        if kcal_pg <= 0.0:
            return 0.0
        return macro_pg / kcal_pg

    def _compute_grams_for_pick(
        self,
        food: FoodItem,
        macro: str,
        remaining_macro_g: float,
        remaining_kcal: float,
        goal: str,
        gram_cap_override: float | None = None,
        carb_variable: bool = True,
    ) -> float:
        """Compute grams to pick for a food given macro/kcal constraints and caps."""
        macro_per_g = self._macro_per_gram(food, macro)
        kcal_per_g = float(getattr(food, "calories_per_gram", 0.0) or 0.0)
        if macro_per_g <= 0.0 or kcal_per_g <= 0.0:
            return 0.0
        grams_for_macro = remaining_macro_g / macro_per_g if macro_per_g > 0 else 0.0
        grams_for_kcal = remaining_kcal / kcal_per_g if kcal_per_g > 0 else grams_for_macro
        grams = max(0.0, min(grams_for_macro, grams_for_kcal))
        # Strict override cap if provided
        if gram_cap_override is not None:
            grams = min(grams, float(gram_cap_override))
        # Cap oils and overall fat portion size
        if macro == 'fat' and self._is_oil(getattr(food, 'name', '') or ''):
            grams = min(grams, 15.0)
        if macro == 'fat':
            grams = min(grams, 50.0)
        # Portion sanity by dominant macro
        dom = self._dominant_macro_of_food(food)
        grams = min(grams, portion_sanity_cap_grams(dom))
        # Add variability to carbs to avoid always maxing out
        if carb_variable and macro == 'carb':
            carb_cap = random.uniform(250.0, 350.0)
            grams = min(grams, carb_cap)
        # Apply vegetable-specific cap only for vegetables
        if self._is_vegetable(food):
            grams = min(grams, 300.0)
        # Round and snap to piece
        grams = self._round_grams(grams)
        grams = self._snap_to_piece_grams_if_applicable(food, grams)
        return grams

    def _round_grams(self, grams: float) -> float:
        if grams <= 0.0:
            return 0.0
        # Half-up rounding to nearest 5 grams
        return float((math.floor((grams + 2.5) / 5.0)) * 5.0)

    def _dominant_macro_of_food(self, food: FoodItem) -> str:
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
        p_pg, c_pg, f_pg, _ = get_macro_densities_for_food(food)
        p_cals = 4.0 * p_pg
        c_cals = 4.0 * c_pg
        f_cals = 9.0 * f_pg
        if p_cals >= c_cals and p_cals >= f_cals:
            return 'protein'
        if c_cals >= p_cals and c_cals >= f_cals:
            return 'carb'
        return 'fat'

    def _snap_to_piece_grams_if_applicable(self, food: FoodItem, grams: float) -> float:
        try:
            piece_weights = getattr(self, '_piece_weights', None)
            if piece_weights is None:
                cfg = DietConfig.objects.last()
                piece_weights = (cfg.piece_weights if cfg and cfg.piece_weights else {})
            key = is_piece_food_name((food.name or '').lower(), piece_weights)
            if key:
                pw = float(piece_weights.get(key, 0.0) or 0.0)
                if pw > 0.0:
                    pieces = max(1, int(round(grams / pw)))
                    return float(pieces * pw)
        except Exception:
            pass
        return grams

    def _load_common_foods(self, names: List[str]) -> List[FoodItem]:
        from ..models import FoodItem
        if not names:
            return []
        try:
            return list(FoodItem.objects.filter(name__in=names))
        except Exception:
            return []

    def _smart_rank_candidates(
        self,
        meal_name: str,
        macro: str,
        candidates: List[FoodItem],
        macro_consumed: Dict[str, float],
        meal_target: MealTarget,
        kcal_consumed: float,
        goal: str,
    ) -> List[Dict]:
        ranked: List[Dict] = []
        fat_cap_g = float(meal_target.macro_targets.get('fat', 0.0) or 0.0)
        for food in candidates:
            p_pg, c_pg, f_pg, kcal_pg = get_macro_densities_for_food(food)
            if kcal_pg <= 0.0:
                continue
            eff = macro_efficiency_score(p_pg, c_pg, f_pg, goal)
            try:
                eff *= float(getattr(food, 'smart_score_weight', 1.0) or 1.0)
            except Exception:
                pass
            macro_pg = self._macro_per_gram(food, macro)
            if macro_pg <= 0.0:
                continue
            remaining_macro = max(0.0, float(meal_target.macro_targets.get(macro, 0.0) or 0.0) - macro_consumed.get(macro, 0.0))
            remaining_kcal = max(0.0, float(meal_target.kcal_target or 0.0) - kcal_consumed)
            grams_for_macro = remaining_macro / macro_pg if macro_pg > 0 else 0.0
            grams_for_kcal = remaining_kcal / kcal_pg if kcal_pg > 0 else grams_for_macro
            grams = max(0.0, min(grams_for_macro, grams_for_kcal))
            # Portion sanity cap and oil cap
            dom = self._dominant_macro_of_food(food)
            grams = min(grams, portion_sanity_cap_grams(dom))
            if macro == 'fat' and self._is_oil(food.name):
                grams = min(grams, 20.0)
            grams = self._round_grams(grams)
            # Predict spillovers
            fat_after = macro_consumed.get('fat', 0.0) + grams * f_pg
            fat_over = max(0.0, fat_after - fat_cap_g)
            kcal_after = kcal_consumed + grams * kcal_pg
            kcal_over = max(0.0, kcal_after - meal_target.kcal_target)
            penalty = 0.1 * fat_over + 0.05 * kcal_over
            base = eff / max(kcal_pg, 1e-9)
            jitter = random.uniform(0.95, 1.05)
            score = (base - penalty) * jitter
            ranked.append({'food': food, 'score': score, 'penalty': penalty, 'grams': grams})
        ranked.sort(key=lambda x: x['score'], reverse=True)
        return ranked

    def _fallback_safe_set(self, meal_name: str) -> List[FoodItem]:
        """Return a list of safe fallback FoodItem objects for this meal."""
        names = []
        # Mix protein/carb/fat and a veggie
        names += SAFE_FALLBACK_FOODS['protein'][:2]
        names += SAFE_FALLBACK_FOODS['carb'][:2]
        names += SAFE_FALLBACK_FOODS['fat'][:1]
        names += SAFE_FALLBACK_FOODS['veggie'][:1]
        try:
            qs = FoodItem.objects.filter(name__in=names).order_by('name')
            # Preserve order by names list
            name_to_food = {f.name: f for f in qs}
            ordered = [name_to_food[n] for n in names if n in name_to_food]
            return ordered
        except Exception:
            # BUG FIX: Use proper limit query instead of loading all items
            return list(FoodItem.objects.all().order_by('id')[:3])
    
    def _add_vegetables_to_meal(
        self, meal_name: str, allowed: Dict, components: List[Tuple[FoodItem, float]],
        meal_existing_ids: set, meal_existing_names: set,
        recent_exclusions: Dict, recent_exclusions_names: Dict,
        used_in_window: Dict, used_names_window: Dict
    ) -> bool:
        """Add 100g of vegetables to every meal. Returns True if vegetable was added."""
        vegetable_candidates = allowed.get(meal_name, {}).get('vegetable', [])
        
        # If no vegetables in preferences, use common vegetables (preloaded)
        if not vegetable_candidates:
            common_vegs = getattr(self, '_common_veggies', [])
            vegetable_candidates = common_vegs[:10] if common_vegs else []
        
        if not vegetable_candidates:
            return False
        
        # Filter out already used vegetables
        exclude_ids = meal_existing_ids | recent_exclusions.get(meal_name, set())
        available_vegs = [v for v in vegetable_candidates if v.id not in exclude_ids]
        
        # If no available vegs, try all vegetables
        if not available_vegs:
            available_vegs = vegetable_candidates[:3]  # Take first 3 even if used before
        
        if available_vegs:
            # Pick a random vegetable or the first available
            import random
            veg = random.choice(available_vegs) if len(available_vegs) > 1 else available_vegs[0]
            
            # Add 100g of the vegetable
            components.append((veg, 100.0))
            meal_existing_ids.add(veg.id)
            meal_existing_names.add(self._normalize_name_for_repeat(veg.name))
            used_in_window.setdefault(meal_name, set()).add(veg.id)
            used_names_window.setdefault(meal_name, set()).add(self._normalize_name_for_repeat(veg.name))
            return True
        return False
    
    def _add_fruits_to_day(
        self, meals: List[str], allowed: Dict, 
        today_meals: List[AIMeal], used_in_window: Dict
    ) -> None:
        """Add 2 portions of fruits across today's meals (100-150g each), respecting meal allowances."""
        import random
        
        # Identify target meals (Breakfast > Lunch > Dinner)
        candidates = [m for m in today_meals if m.meal_name in ('Breakfast', 'Lunch', 'Dinner')]
        candidates.sort(key=lambda m: {'Breakfast':0, 'Lunch':1, 'Dinner':2}.get(m.meal_name, 99))
        
        # Target up to 2 meals
        targets = candidates[:2]
        
        for meal in targets:
            meal_name = meal.meal_name
            
            # Get allowed fruits SPECIFICALLY for this meal
            allowed_candidates = allowed.get(meal_name, {}).get('fruit', [])
            
            # Filter logic (recency, used today)
            # Used today across all meals
            try:
                used_all = set().union(*(used_in_window.get(k, set()) for k in ("Breakfast","Lunch","Dinner","Snack")))
            except Exception:
                used_all = set()
            
            valid_fruits = [f for f in allowed_candidates if f.id not in used_all]
            
            # Fallback ONLY if we have safe common fruits allowed for this meal?
            # Actually, we can't easily know which common fruits are allowed for this meal unless we check preferences.
            # But the 'allowed' dict ALREADY contains preferences.
            # So if valid_fruits is empty, we effectively have NO allowed fruits for this meal.
            # We should SKIP to avoid PersistenceError.
            
            if not valid_fruits:
                # Try to use any candidate even if used recently?
                valid_fruits = allowed_candidates
                
            if not valid_fruits:
                continue
                
            # Pick one
            fruit = random.choice(valid_fruits)
            
            # Check if fruit already in meal?
            existing_names = {ing.name.lower() for ing in meal.ingredients}
            if fruit.name.lower() in existing_names:
                continue
                
            # Add to meal
            grams = random.uniform(100.0, 150.0)
            grams = self._round_grams(grams)
            
            # Convert FoodItem to ingredient
            # Warning: meal.ingredients is list of AIIngredient
            meal.ingredients.append(AIIngredient(name=fruit.name, quantity=f"{int(grams)}g"))
            
            # Update Nutrition
            # We need to calculate kcal/macros for the added fruit
            # approximate values from food object
            f_cal = float(fruit.calories) / 100.0 * grams
            f_pro = float(fruit.protein) / 100.0 * grams
            f_carb = float(fruit.carbs) / 100.0 * grams
            f_fat = float(fruit.fat) / 100.0 * grams
            
            if not meal.total_nutrition:
                meal.total_nutrition = {"calories": 0.0, "protein": 0.0, "carbs": 0.0, "fat": 0.0}
            
            meal.total_nutrition["calories"] += f_cal
            meal.total_nutrition["protein"] += f_pro
            meal.total_nutrition["carbs"] += f_carb
            meal.total_nutrition["fat"] += f_fat
            
            # Mark as used (id is int)
            try:
                fid = int(fruit.id)
                used_in_window.setdefault(meal_name, set()).add(fid)
                # Also prevent reuse in same day (handled by 'used_in_window' updates?)
                # We need to update user_all for next iteration
                used_in_window.setdefault("Snack", set()).add(fid) # Add to dummy to ensure global exclusion for today
            except Exception:
                pass
