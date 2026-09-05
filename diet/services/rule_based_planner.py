from __future__ import annotations
import logging

from typing import Dict, List, Tuple, Set
from dataclasses import dataclass, field

from django.utils import timezone
from django.conf import settings
import random
import math
import hashlib

from ..models import FoodItem, UserFoodCategoryPreference
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

logger = logging.getLogger(__name__)


@dataclass
class MealTarget:
    name: str
    kcal_target: float
    macro_targets: Dict[str, float]  # grams per macro for this meal


@dataclass
class PlannerContext:
    """What a generation needs that does not change from day to day."""
    goal: str
    allowed_map: Dict[str, Dict[str, List[FoodItem]]]


@dataclass
class DayContext:
    date: object
    meal_kcal: Dict[str, float]
    meal_targets: Dict[str, MealTarget]
    #: Food ids served for this slot inside the no-repeat window, persisted history
    #: and this run's earlier days combined.
    recent_exclusions_ids: Dict[str, Set[int]]
    used_in_window: Dict[str, Set[int]]
    #: Recipe ids served for this slot inside the no-repeat window.
    recent_recipe_ids: Dict[str, Set[int]] = field(default_factory=dict)
    #: Recipes already served TODAY, across every slot. The window above is a snapshot
    #: taken before the day starts and never updated while it runs, and the set the
    #: recipe path wrote to during the day was only read the following morning — two
    #: stores for one idea, so nothing stopped a dish appearing at both lunch and
    #: dinner. It happened on 9 of 42 measured days, and the snack dish was also being
    #: served as that morning's breakfast.
    served_today: Set[int] = field(default_factory=set)


class _PoolView:
    """Reads `allowed_map` with the same call shape as a CandidatePool."""

    def __init__(self, allowed_map):
        self._map = allowed_map or {}

    def get(self, meal: str, macro: str):
        return self._map.get(meal, {}).get(macro, [])



class NoServableMealError(RuntimeError):
    """The pool cannot supply a dish or a meal shape for this slot."""


def feasible_kcal_ceiling(meal_count: int, snack_count: int, goal: str = "maintain") -> float:
    """The most a day of this shape can carry in servable portions."""
    from diet.planner.policy import load_policy
    policy = load_policy(goal)
    return (max(0, int(meal_count)) * policy.max_kcal_per_meal
            + max(0, int(snack_count)) * policy.max_kcal_per_snack)


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

    def __init__(self, user, seed_salt: str | None = None):
        self.user = user
        # Instance-local RNG — avoid reseeding the process-global `random`, which
        # would leak deterministic state into any other code in the worker.
        self._rng = random.Random()
        #: What the day's generator is seeded from, in place of the user's row id.
        #: In production the id IS the client's stable identity and nothing needs this.
        #: A measurement harness creates a new client on every run, so the id changes,
        #: so the seed changes, so every plan changes — and every number it reported was
        #: one sample from a distribution presented as a measurement. Drift read 1.2%
        #: and 9.2% on consecutive runs of identical code.
        self._seed_salt = seed_salt

    def _resolve_goal(self) -> str:
        """The user's goal in this planner's lower-case vocabulary.

        One resolver, on the user; this is the boundary that lower-cases it.
        """
        return self.user.resolve_fitness_goal().lower()

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

        base_date = _date.fromisoformat(start_date) if start_date else timezone.localdate()
        meals = ["Breakfast", "Lunch", "Dinner"][:meal_count]

        # Allowed foods per meal/macro (static per user)
        allowed = self._build_allowed_foods_map()
        goal = self._resolve_goal()
        ctx = PlannerContext(goal=goal, allowed_map=allowed)

        planned_meals: List[AIMeal] = []
        # Track foods chosen in this generation window to prevent reuse within next 3 days
        used_in_window: Dict[str, Set[int]] = {m: set() for m in ("Breakfast", "Lunch", "Dinner", "Snack")}
        # Maintain per-day sliding windows of chosen FoodItem IDs (ID-only)
        day_windows: List[Dict[str, Set[int]]] = []
        recipe_windows: List[Dict[str, Set[int]]] = []

        for day_idx in range(max(1, duration_days)):
            current_date = base_date + _timedelta(days=day_idx)
            # Deterministic randomness per user/day for reproducible variety
            try:
                # uid is user id
                uid = int(getattr(self.user, 'id', 0) or 0)
            except Exception:
                uid = 0
            salt = f"rbp:{self._seed_salt or uid}:{current_date.isoformat()}"
            seed = int(hashlib.sha256(salt.encode('utf-8')).hexdigest()[:12], 16)
            self._rng = random.Random(seed)
            day_start_idx = len(planned_meals)
            # Recipes served in the last `no_repeat_days`, per meal, so a dish is not
            # offered again while something else still fits.
            recent_recipes: Dict[str, Set[int]] = {
                m: set() for m in ("Breakfast", "Lunch", "Dinner", "Snack")}
            # Persisted dishes first. Without this the recipe window was this run only,
            # and with duration_days defaulting to 1 a client generating each morning was
            # served the same dish forever while the food-level window reshuffled it.
            for _m, ids in self._get_recent_recipe_history(no_repeat_days, current_date).items():
                recent_recipes[_m].update(ids)
            for window in recipe_windows[-int(no_repeat_days):]:
                for _m in recent_recipes:
                    recent_recipes[_m].update(window.get(_m, set()))
            self._recipes_today = {m: set() for m in recent_recipes}
            # Rebuild in-run recency from last `no_repeat_days` day windows
            try:
                rebuilt: Dict[str, Set[int]] = {m: set() for m in ("Breakfast", "Lunch", "Dinner", "Snack")}
                # Union of previous day windows
                for w in day_windows[-int(no_repeat_days):]:
                    for _m in rebuilt:
                        rebuilt[_m].update(w.get(_m, set()))
                used_in_window = rebuilt
            except Exception:
                # Optional side effect: swallowing this silently is what made the
                # surrounding failures invisible in logs. Control flow is unchanged.
                logger.debug('suppressed non-fatal error', exc_info=True)

            # Per-day allocations and macro targets are computed via DayContext
            # Build DayContext mirroring current per-day calculations (read-only usage for now)
            day_ctx = self._prepare_day(
                ctx=ctx,
                current_date=current_date,
                daily_kcal=daily_kcal,
                snack_count=snack_count,
                recent_recipe_ids=recent_recipes,
                meals=meals,
                used_in_window=used_in_window,
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
                # Optional side effect: swallowing this silently is what made the
                # surrounding failures invisible in logs. Control flow is unchanged.
                logger.debug('suppressed non-fatal error', exc_info=True)

            # The snack is a meal. It used to be planned by sixty lines of its own that
            # predated the recipe and template paths and were never folded in: they
            # called the recipe search without the client, without the day's generator
            # and without the recipes already served, so the snack was a deterministic
            # best fit — one dish, forty-two times out of forty-two, identical for every
            # client. Routing it through `_plan_meal` gives it personalisation, variety,
            # shapes and servable portions in one move rather than four.
            slots = list(meals) + (["Snack"] if snack_count else [])
            for index, meal_name in enumerate(slots):
                meal = self._plan_meal(meal_name, ctx, day_ctx)
                planned_meals.append(meal)
                # Meals compensate each other. Each was optimised to a fixed target and
                # the day summed afterwards, so drift was per-meal by construction: a
                # lunch that lands 60 kcal over stays 60 over. The signed residual is
                # carried into the next slot's target, bounded so one bad meal cannot
                # drag the next into something nobody would serve.
                if index + 1 < len(slots):
                    self._carry_residual(meal, day_ctx.meal_targets.get(meal_name),
                                         day_ctx.meal_targets.get(slots[index + 1]))

            # Nothing is appended to a meal after it has been planned. Two portions of
            # fruit used to be stapled on here, at a gram figure drawn uniformly between
            # 100 and 150 and never checked against the food's own ladder, onto meals
            # that had already been solved to their calorie target — and onto a named
            # dish, which made it a different dish. Fruit is a slot in a meal's shape,
            # chosen and portioned with the rest of it; four of the ten derived
            # templates carry one.


            # Note: rebalancing now occurs inside _plan_meal on (FoodItem, grams) components before finalization.

            # After finishing the full day, accumulate recency from actual meals
            try:
                today_meals = planned_meals[day_start_idx:]
                today_used: Dict[str, Set[int]] = {m: set() for m in ("Breakfast","Lunch","Dinner","Snack")}
                from diet.models import FoodItem as _FoodItem
                for m in today_meals:
                    meal_key = m.meal_type if m.meal_type in today_used else "Dinner"
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
                recipe_windows.append(dict(self._recipes_today))
                while len(recipe_windows) > int(no_repeat_days):
                    recipe_windows.pop(0)
                try:
                    dbg_counts = {k: len(v) for k,v in today_used.items()}
                    logger.debug(f"[RECENCY] accumulated actual ids for day {current_date}: {dbg_counts}")
                except Exception:
                    # Optional side effect: swallowing this silently is what made the
                    # surrounding failures invisible in logs. Control flow is unchanged.
                    logger.debug('suppressed non-fatal error', exc_info=True)
            except Exception:
                # Optional side effect: swallowing this silently is what made the
                # surrounding failures invisible in logs. Control flow is unchanged.
                logger.debug('suppressed non-fatal error', exc_info=True)

        output = DietPlanOutput(plan=planned_meals)
        self._record_delivery(output, daily_kcal, max(1, duration_days))
        return output

    #: How far a later meal's target may move to absorb an earlier one's miss.
    RESIDUAL_CARRY_CAP = 0.15

    def _carry_residual(self, meal, served_target, next_target) -> None:
        """Move the signed miss of `meal` into `next_target`, bounded per macro."""
        if meal is None or served_target is None or next_target is None:
            return
        got = getattr(meal, "total_nutrition", None) or {}
        pairs = (("calories", "kcal_target", None),
                 ("protein", "macro_targets", "protein"),
                 ("carbs", "macro_targets", "carb"),
                 ("fat", "macro_targets", "fat"))
        for got_key, attr, macro in pairs:
            if macro is None:
                want, actual = float(served_target.kcal_target), float(got.get(got_key, 0) or 0)
                base = float(next_target.kcal_target)
                shift = max(-self.RESIDUAL_CARRY_CAP * base,
                            min(self.RESIDUAL_CARRY_CAP * base, want - actual))
                next_target.kcal_target = max(0.0, base + shift)
            else:
                want = float(served_target.macro_targets.get(macro, 0.0))
                actual = float(got.get(got_key, 0) or 0)
                base = float(next_target.macro_targets.get(macro, 0.0))
                shift = max(-self.RESIDUAL_CARRY_CAP * base,
                            min(self.RESIDUAL_CARRY_CAP * base, want - actual))
                next_target.macro_targets[macro] = max(0.0, base + shift)

    def _record_delivery(self, output, daily_kcal: float, days: int) -> None:
        """State what the plan actually delivers against what was asked for.

        Nothing in the output said whether the target had been met. A day is capped by
        what its meals can hold: three meals and a snack top out near 3,700 kcal, because
        every portion is bounded by an amount a person would serve. A 5,000 kcal request
        therefore returned a 3,912 kcal plan, correct in every other respect, labelled
        5,000 and silent about the 22% it was short. A ceiling is a fact about food and
        is fine; not saying so is not.
        """
        requested = float(daily_kcal or 0.0)
        delivered = sum(float((m.total_nutrition or {}).get("calories", 0) or 0)
                        for m in output.plan) / max(1, days)
        shortfall = (delivered / requested - 1.0) if requested > 0 else 0.0
        report = {
            "requested_kcal_per_day": round(requested, 1),
            "delivered_kcal_per_day": round(delivered, 1),
            "deviation": round(shortfall, 4),
            "met": abs(shortfall) <= 0.10,
        }
        if not report["met"]:
            report["reason"] = (
                "the day's meals cannot carry this many calories in servable portions"
                if shortfall < 0 else
                "the day's meals overshoot the target in servable portions")
            logger.warning(
                "Plan for user %s delivers %.0f kcal/day against %.0f requested (%+.1f%%)",
                getattr(self.user, "id", "?"), delivered, requested, shortfall * 100)
        try:
            output.plan_metadata["delivery"] = report
        except Exception:
            logger.debug("could not record the delivery report", exc_info=True)

    # ------------------------ meal rebalancer utilities ------------------------
    # Four helpers used to live here that parsed grams back out of a "180g" string,
    # scaled them by a factor and wrote the string again. They served the bespoke snack
    # builder and the old rebalancer, both of which are gone: a quantity is chosen from
    # the food's own ladder now and never re-derived from its own display text.

    def _prepare_day(
        self,
        ctx: PlannerContext,
        current_date,
        daily_kcal: float,
        snack_count: int,
        meals: List[str],
        used_in_window: Dict[str, Set[int]],
        no_repeat_days: int,
        recent_recipe_ids: Dict[str, Set[int]] | None = None,
    ) -> DayContext:
        # Recent history: persisted meals plus this run's earlier days, by food id.
        try:
            ids_by_meal = self._get_recent_food_history(days=no_repeat_days, until=current_date)
        except Exception:
            logger.warning("could not read recent food history; variety window is this run only",
                           exc_info=True)
            ids_by_meal = {m: set() for m in ("Breakfast", "Lunch", "Dinner", "Snack")}
        recent_ids = {m: set(ids_by_meal.get(m, set())) | set(used_in_window.get(m, set()))
                      for m in ("Breakfast", "Lunch", "Dinner", "Snack")}

        # Kcal allocation. `snack_kcal` comes from the policy rather than a literal:
        # the number lived in `PlannerPolicy` and this line hardcoded it, so a
        # deployment that changed the snack budget changed only half the arithmetic.
        from diet.planner.policy import load_policy

        policy = load_policy(ctx.goal)
        snack_kcal = float(policy.snack_kcal) if snack_count else 0.0
        meal_kcal_budget = max(0.0, daily_kcal - snack_kcal)
        if getattr(settings, 'DIET_DYNAMIC_MEAL_ALLOCATION', False):
            split = goal_meal_kcal_split(ctx.goal)
            meal_distribution = {m: split.get(m, 1.0/len(meals)) for m in meals}
        else:
            meal_distribution = self._choose_distribution_for_goal_value(ctx.goal, meals)
        # Normalize shares to sum to 1.0 over the ACTUAL meals. The goal patterns are
        # defined for 3 meals, so with meal_count of 1 or 2 the raw shares sum to <1,
        # which would silently under-deliver both calories and macros for the day.
        _total_share = sum(meal_distribution.values())
        if _total_share > 0:
            meal_distribution = {m: (v / _total_share) for m, v in meal_distribution.items()}
        else:
            meal_distribution = {m: 1.0 / len(meals) for m in meals}

        # One distribution over the whole day, snack included. The meal shares used to
        # be normalised over the three main meals while the MACRO targets were shares
        # of the whole day, so the three meals were handed 100% of the day's protein,
        # carbohydrate and fat and the snack's macros were added on top of a complete
        # day. Energy and macros are now split by the same numbers.
        snack_share = (snack_kcal / daily_kcal) if daily_kcal > 0 else 0.0
        meal_distribution = {m: v * (1.0 - snack_share) for m, v in meal_distribution.items()}
        if snack_count:
            meal_distribution["Snack"] = snack_share
        per_meal_kcal = {m: daily_kcal * share for m, share in meal_distribution.items()}

        # Macro targets per meal, from the one function persistence and convergence
        # also read, with the client's bodyweight driving protein.
        from diet.planner.targets import day_macro_grams
        day = day_macro_grams(daily_kcal, policy, getattr(self.user, "weight", None))
        protein_target, carb_target, fat_target = day["protein"], day["carb"], day["fat"]
        meal_targets: Dict[str, MealTarget] = {}
        for m in meal_distribution:
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
            meal_targets=meal_targets,
            recent_exclusions_ids=recent_ids,
            used_in_window=used_in_window,
            recent_recipe_ids=recent_recipe_ids or {},
        )
    def _plan_meal(self, meal_name: str, ctx: PlannerContext, day_ctx: DayContext) -> AIMeal:
        # Three paths, in descending order of how much a person had to do with the
        # result. A recipe is a dish someone wrote down. A template is a shape read off
        # those recipes and filled from this client's own chosen foods — coherent, and
        # unlike a recipe it can express a client nobody wrote a dish for. Component
        # assembly hits the macro targets and produces a pile: "chicken 180 g, oats
        # 90 g, olive oil 12 g, broccoli 400 g" is not something anyone cooks.
        recipe_meal = self._plan_meal_from_recipe(meal_name, ctx, day_ctx)
        if recipe_meal is not None:
            return recipe_meal
        template_meal = self._plan_meal_from_template(meal_name, ctx, day_ctx)
        if template_meal is not None:
            return template_meal
        # No third path. Component assembly — a greedy fill with its own optimiser, its
        # own floors and its own rounding — executed zero times in 168 instrumented
        # meals and was deleted. A pool that can supply neither a dish nor a shape is a
        # catalogue or constraint problem, and it must surface as one rather than as a
        # confidently wrong plate.
        raise NoServableMealError(
            f"no dish or meal shape can be built for {meal_name} from this client's pool")

    def _plan_meal_from_template(self, meal_name: str, ctx: PlannerContext,
                                 day_ctx: DayContext) -> AIMeal | None:
        """Build a meal from a shape and this client's pool.

        The shapes come from the recipe library rather than from anyone's judgement, and
        the pool arrives already ranked with the client's chosen foods at the top, so
        what comes out is structurally a meal and made of what they asked for.
        """
        try:
            from diet.planner.portion import totals as portion_totals
            from diet.planner.candidates import classify_food
            from diet.planner.templates import plan_meal as plan_from_template

            target = day_ctx.meal_targets.get(meal_name)
            if target is None:
                return None
            macro_targets = getattr(target, "macro_targets", None) or {}
            targets = {
                "calories": float(getattr(target, "kcal_target", 0) or 0),
                "protein": float(macro_targets.get("protein", 0) or 0),
                "carb": float(macro_targets.get("carb", 0) or 0),
                "fat": float(macro_targets.get("fat", 0) or 0),
            }
            if targets["calories"] <= 0:
                return None

            portions, _score, template = plan_from_template(
                meal_name,
                getattr(self, "_pool", None) or _PoolView(ctx.allowed_map),
                targets,
                templates=self._templates(),
                edges=self._pairings(),
                recent=set(day_ctx.recent_exclusions_ids.get(meal_name, ())),
                rng=getattr(self, "_rng", None),
            )
            if not portions or template is None:
                return None

            constraints = self._constraints()
            if constraints.active and constraints.forbids_any(p.food for p in portions):
                return None

            nutrition = portion_totals(portions)
            pool_obj = getattr(self, "_pool", None)
            chosen_here = []
            if pool_obj is not None:
                for p in portions:
                    if pool_obj.weights(meal_name, classify_food(p.food)).get(p.food.id, 0) >= 80:
                        chosen_here.append(p.food.name)
            reason = (f"because you chose {', '.join(chosen_here[:2])} for {meal_name.lower()}"
                      if chosen_here else f"built from your {meal_name.lower()} foods to fit your target")
            return AIMeal(
                meal_name=meal_name,
                description=f"Built from your {meal_name.lower()} foods.",
                reason=reason,
                ingredients=[
                    AIIngredient(
                        name=p.food.name,
                        quantity=f"{p.grams:g}g",
                        food_id=p.food.id,
                        grams=float(p.grams),
                        estimated_calories=round(float(p.food.calories or 0) * p.grams / 100, 1),
                        estimated_protein=round(float(p.food.protein or 0) * p.grams / 100, 1),
                        estimated_carbs=round(float(p.food.carbs or 0) * p.grams / 100, 1),
                        estimated_fat=round(float(p.food.fat or 0) * p.grams / 100, 1),
                    )
                    for p in portions
                ],
                total_nutrition={
                    "calories": round(nutrition["calories"], 1),
                    "protein": round(nutrition["protein"], 1),
                    "carbs": round(nutrition["carb"], 1),
                    "fat": round(nutrition["fat"], 1),
                },
                meal_type=meal_name,
                shape=getattr(template, "name", None),
                target=dict(targets),
                preparation_time=15,
                difficulty_level="easy",
            )
        except Exception:
            # Loud. This used to log at DEBUG and return None, so any bug inside
            # diet/planner/ presented as "the engine built a pile" and nothing else.
            # Re-raised where a developer will see it; in production the recipe path
            # and NoServableMealError still stand between the client and silence.
            logger.error("template path failed for %s", meal_name, exc_info=True)
            if getattr(settings, "DIET_PLANNER_STRICT", settings.DEBUG):
                raise
            return None

    def _recipes(self):
        """The active library, loaded once per generation with its foods and categories.

        `find_recipe` reloaded and re-prefetched it for every meal of every day: 28
        identical queries per week-long plan, each followed by the same ladder arithmetic.
        """
        if getattr(self, "_recipe_cache", None) is None:
            from diet.models import Recipe
            self._recipe_cache = list(
                Recipe.objects.filter(is_active=True)
                .prefetch_related("ingredients__food__category"))
            self._ladder_cache = {}
        return self._recipe_cache

    def _templates(self):
        """Derived once per generation; they only change when a recipe does."""
        if getattr(self, "_template_cache", None) is None:
            from diet.planner.templates import derive_templates
            self._template_cache = derive_templates()
        return self._template_cache

    def _pairings(self):
        if getattr(self, "_pairing_cache", None) is None:
            from diet.planner.templates import pairing_edges
            self._pairing_cache = pairing_edges()
        return self._pairing_cache

    def _constraints(self):
        """Everything this client may not eat, read once per generation.

        Was `_allergen_checker`, which is half the question. Dislikes were enforced in
        the pool builder and nowhere else, so the recipe path — three quarters of meals —
        never saw them.
        """
        if getattr(self, "_constraint_cache", None) is None:
            from diet.planner.constraints import ClientConstraints
            try:
                self._constraint_cache = ClientConstraints.for_user(self.user)
            except Exception:
                logger.debug("could not load client constraints", exc_info=True)
                self._constraint_cache = ClientConstraints()
        return self._constraint_cache

    def _plan_meal_from_recipe(self, meal_name: str, ctx: PlannerContext,
                               day_ctx: DayContext) -> AIMeal | None:
        """Build the meal from a recipe when one fits inside tolerance.

        Returns None when the library has nothing suitable, so component assembly stays
        the fallback — a recipe library will never cover every target.
        """
        try:
            from diet.planner.optimize import totals_of
            from diet.planner.policy import load_policy
            from diet.planner.recipes import chosen_food_ids, find_recipe

            target = day_ctx.meal_targets.get(meal_name)
            if target is None:
                return None
            macro_targets = getattr(target, "macro_targets", None) or {}
            targets = {
                "calories": float(getattr(target, "kcal_target", 0) or 0),
                "protein": float(macro_targets.get("protein", 0) or 0),
                "carb": float(macro_targets.get("carb", 0) or 0),
                "fat": float(macro_targets.get("fat", 0) or 0),
            }
            if targets["calories"] <= 0:
                return None

            policy = load_policy(ctx.goal)
            # The client, so the dish can be chosen from what they picked; the day's
            # seeded generator, so the choice varies without becoming unreproducible;
            # and the recipes already served inside the no-repeat window, which the
            # recipe path was computing and then ignoring.
            # Today is a ban, earlier days are a penalty. Banning a dish for three
            # days emptied a sixteen-recipe library inside a week and dropped the
            # planner back to assembling piles, which is the worse trade. Banning it
            # for the rest of the same day costs at most three dishes and the fallback
            # is a meal built from the client's own foods, so nobody is served the same
            # dish at lunch and dinner to save a recipe for tomorrow.
            served = set(getattr(day_ctx, "recent_recipe_ids", {}).get(meal_name, ()))
            match = find_recipe(
                meal_name, targets, policy,
                constraints=self._constraints(),
                recipes=self._recipes(),
                ladders=self._ladder_cache,
                pool=getattr(self, "_pool", None),
                edges=self._pairings(),
                exclude_ids=tuple(getattr(day_ctx, "served_today", ())),
                recent_ids=tuple(served),
                user=self.user,
                rng=getattr(self, "_rng", None),
            )
            if match is None or not match.deviation.within(policy.tolerance):
                return None

            ingredients = [
                AIIngredient(
                    name=food.name,
                    quantity=f"{grams:g}g",
                    food_id=food.id,
                    grams=float(grams),
                    estimated_calories=round(float(food.calories or 0) * grams / 100, 1),
                    estimated_protein=round(float(food.protein or 0) * grams / 100, 1),
                    estimated_carbs=round(float(food.carbs or 0) * grams / 100, 1),
                    estimated_fat=round(float(food.fat or 0) * grams / 100, 1),
                )
                for food, grams in match.components
            ]
            totals = totals_of(match.components)
            today = getattr(self, "_recipes_today", None)
            if today is not None:
                today.setdefault(meal_name, set()).add(match.recipe.id)
            day_ctx.served_today.add(match.recipe.id)
            picked = chosen_food_ids(self.user, meal_name)
            chosen_here = sorted(f.name for f, _g in match.components if f.id in picked)
            cuisine = (getattr(match.recipe, "cuisine", "") or "").strip()
            reason = (f"because you chose {', '.join(chosen_here[:2])} for {meal_name.lower()}"
                      if chosen_here else
                      (f"a {cuisine} dish that fits your {meal_name.lower()} target" if cuisine
                       else f"a dish that fits your {meal_name.lower()} target"))
            return AIMeal(
                meal_name=match.name,
                description=getattr(match.recipe, "description", "") or "",
                reason=reason,
                ingredients=ingredients,
                total_nutrition={
                    "calories": round(totals["calories"], 1),
                    "protein": round(totals["protein"], 1),
                    "carbs": round(totals["carb"], 1),
                    "fat": round(totals["fat"], 1),
                },
                meal_type=meal_name,
                recipe_id=match.recipe.id,
                target=dict(targets),
                preparation_time=int(getattr(match.recipe, "prep_minutes", 15) or 15),
                difficulty_level="easy",
            )
        except Exception:
            logger.error("recipe path failed for %s", meal_name, exc_info=True)
            if getattr(settings, "DIET_PLANNER_STRICT", settings.DEBUG):
                raise
            return None

    def _macro_ratios_for_goal_value(self, goal: str) -> Dict[str, float]:
        g = (goal or '').lower()
        if 'lose' in g:
            return {"protein": 0.35, "carb": 0.40, "fat": 0.25}
        if 'gain' in g:
            return {"protein": 0.25, "carb": 0.55, "fat": 0.20}
        return {"protein": 0.30, "carb": 0.50, "fat": 0.20}

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
        """Candidate foods per (meal, macro), ranked.

        Delegates to `diet.planner.candidates`, which treats preferences as a RANKING
        over the whole catalogue rather than a gate on it. The previous implementation
        returned only foods the user had explicitly categorised, so a user who had not
        completed food preferences got an empty pool and a 233 kcal plan (-90% of
        target) stored silently as if normal.

        Allergens and explicit dislikes remain hard filters — they are applied inside
        the pool builder, before anything is ranked.
        """
        from diet.planner.candidates import build_pool
        from diet.planner.policy import load_policy

        policy = load_policy(self._resolve_goal())
        pool = build_pool(self.user, policy, constraints=self._constraints())
        # Kept whole as well as flattened: `by_slot` is a list per slot and the
        # template path needs the scores behind that order, not just the order.
        self._pool = pool
        return pool.by_slot

    def _get_recent_recipe_history(self, days: int, until) -> Dict[str, Set[int]]:
        """Dishes served to this client in the window, from persisted meals."""
        from datetime import timedelta as _timedelta
        from ..models import Meal
        out: Dict[str, Set[int]] = {m: set() for m in ("Breakfast", "Lunch", "Dinner", "Snack")}
        try:
            rows = Meal.objects.filter(
                diet_plan__user=self.user, recipe__isnull=False,
                date__gte=until - _timedelta(days=days), date__lt=until,
            ).values_list('meal_type', 'recipe_id')
            for meal_type, recipe_id in rows:
                if meal_type in out:
                    out[meal_type].add(recipe_id)
        except Exception:
            logger.warning("could not read recent recipe history", exc_info=True)
        return out

    def _get_recent_food_history(self, days: int, until) -> Dict[str, Set[int]]:
        """
        Batch query of recent MealComponents across all meals for the user, returning:
        - ids_by_meal: {meal_type: set(food_id)}
        """
        from datetime import timedelta as _timedelta
        from ..models import MealComponent
        ids_by_meal: Dict[str, Set[int]] = {m: set() for m in ("Breakfast", "Lunch", "Dinner", "Snack")}
        since = until - _timedelta(days=days)
        rows = MealComponent.objects.filter(
            meal__diet_plan__user=self.user,
            meal__date__gte=since,
            meal__date__lt=until,
        ).values_list('meal__meal_type', 'food_id')
        for meal_type, food_id in rows:
            if meal_type in ids_by_meal:
                ids_by_meal[meal_type].add(food_id)
        return ids_by_meal
