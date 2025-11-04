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

        for day_idx in range(max(1, duration_days)):
            current_date = base_date + _timedelta(days=day_idx)
            # Deterministic randomness per user/day for reproducible variety
            try:
                uid = int(getattr(self.user, 'id', 0) or 0)
                except Exception:
                uid = 0
            salt = f"rbp:{uid}:{current_date.isoformat()}"
            seed = int(hashlib.sha256(salt.encode('utf-8')).hexdigest()[:12], 16)
            random.seed(seed)
            day_start_idx = len(planned_meals)
            # Recency handled via DayContext (built below)

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

        output = DietPlanOutput(plan=planned_meals)
        if getattr(settings, 'DIET_SMART_MACRO_PLANNER', False):
            try:
                output.plan_metadata['smart_macro_summary'] = self._smart_summary
            except Exception:
                pass
        return output

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
                meal_name, ctx.allowed_map, components, meal_existing_ids, meal_existing_names,
                day_ctx.recent_exclusions_ids, day_ctx.recent_exclusions_names, day_ctx.used_in_window,
                day_ctx.used_names_window
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
                exclude_names = meal_existing_names | set(day_ctx.recent_exclusions_names.get(meal_name, set()))
                    candidates = [f for f in candidates if f.id not in exclude_ids and self._normalize_name_for_repeat(f.name) not in exclude_names]
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
        exclude_names = set(recent_exclusions_names.get(meal_name, set()))
        existing_ids = existing_ids or set()
        cands = [f for f in cands if f.id not in exclude_ids_local and f.id not in existing_ids and self._normalize_name_for_repeat(f.name) not in exclude_names]
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
        """Add 2 portions of fruits across today's meals (100-150g each)."""
        # Get fruit candidates from preferences
        all_fruit_candidates = []
        for meal_name in meals:
            meal_fruits = allowed.get(meal_name, {}).get('fruit', [])
            all_fruit_candidates.extend(meal_fruits)
        
        # Remove duplicates and filter out fruits already used today across any meal
        seen_ids = set()
        unique_fruits = []
        for fruit in all_fruit_candidates:
            if fruit.id not in seen_ids:
                seen_ids.add(fruit.id)
                unique_fruits.append(fruit)
        try:
            used_all = set().union(*(used_in_window.get(k, set()) for k in ("Breakfast","Lunch","Dinner","Snack")))
        except Exception:
            used_all = set()
        unique_fruits = [f for f in unique_fruits if f.id not in used_all]
        
        # Fallback to common fruits if no preferences (preloaded)
        if not unique_fruits:
            common_f = getattr(self, '_common_fruits', [])
            unique_fruits = common_f[:5] if common_f else []
        
        if len(unique_fruits) < 2:
            return  # Not enough fruits available
        
        # Select 2 different fruits randomly
        import random
        selected_fruits = random.sample(unique_fruits, min(2, len(unique_fruits)))
        
        # Use the provided today_meals slice (pre-snack)
        if len(today_meals) < 2:
            return  # Not enough meals to add fruits
        
        # Prioritize breakfast, then lunch for fruit addition
        target_indices = []
        meal_names_today = [m.meal_name for m in today_meals]
        
        for preferred in ['Breakfast', 'Lunch', 'Dinner']:
            if preferred in meal_names_today and len(target_indices) < 2:
                idx = meal_names_today.index(preferred)
                target_indices.append(idx)
        
        # Add fruits to selected meals
        for i, meal_idx in enumerate(target_indices[:2]):
            if i < len(selected_fruits) and meal_idx < len(today_meals):
                fruit = selected_fruits[i]
                meal = today_meals[meal_idx]
                
                # Check if this fruit is already in the meal
                existing_names = {ing.name.lower() for ing in meal.ingredients}
                if fruit.name.lower() in existing_names:
                    continue
                
                # Calculate portion size (100-150g)
                portion = random.uniform(100.0, 150.0)
                
                # Add fruit to meal's ingredients
                meal.ingredients.append(
                    AIIngredient(name=fruit.name, quantity=f"{int(portion)}g")
                )
                
                # Track usage
                used_in_window.setdefault(meal.meal_name, set()).add(fruit.id)


