from __future__ import annotations

from typing import Dict, Any, Optional, Tuple, Iterable
from datetime import date as _date, timedelta as _timedelta
from django.utils import timezone
from django.db import IntegrityError, transaction
from django.core.exceptions import ValidationError

from ..models import DietPlan, Meal, MealComponent, FoodItem, DietConfig, UserFoodCategoryPreference
from ..ai_models import DietPlanOutput, AIMeal
from ..utils.nutrition import convert_to_grams, is_piece_food_name, piece_based_grams_if_appropriate
from ..exceptions import PersistenceError, ConstraintViolationError
from ..utils.logging_utils import get_logger, log_json, log_day_macros
from .meal_plan_factory import MealPlanFactory
from .meal_validator import MealValidator
from .meal_rebalancer import MealRebalancer
from .calorie_trimmer import CalorieTrimmer
from .macro_cap_enforcer import MacroCapEnforcer
from .snack_enforcer import SnackCalorieEnforcer
from .per_meal_fat_capper import PerMealFatCapper
from .macro_shortage_booster import MacroShortageBooster
from .macro_balancer import MacroBalancer as LegacyMacroBalancer  # reuse macro rebalance logic
import logging

logger = logging.getLogger(__name__)


class DietPersistenceService:
    """
    Responsible for saving AI-generated plans into the database.
    Extracted from the monolithic generator for clarity and testability.
    """

    def __init__(self, user):
        self.user = user
        self.logger = get_logger(__name__)

    def save_plan(
        self,
        plan_output: DietPlanOutput,
        meal_count: int,
        snack_count: int = 0,
        start_date: Optional[str] = None,
    ) -> DietPlan:
        try:
            sd = self._resolve_start_date(start_date)
            total_meals = len(plan_output.plan)
            # Safely handle excessive meal request vs generated: enforce minimum 1 day duration
            daily_meals = meal_count + snack_count
            if daily_meals > 0:
                duration_days = max(1, total_meals // daily_meals)
            else:
                duration_days = 1
            ed = sd + _timedelta(days=duration_days - 1)

            with transaction.atomic():
                # Serialise generation for this user. Five parallel "generate" taps each
                # created their own active plan, so the user ended up with five active
                # plans and twenty meals for one day. Locking the user row makes the
                # supersede below a real check rather than a race.
                type(self.user).objects.select_for_update().filter(pk=self.user.pk).first()

                # A new plan REPLACES any active plan covering the same dates. Nothing
                # did this, and DietPlan.clean() — which exists to reject exactly that
                # overlap — was never executed because save() does not call full_clean().
                # So plans accumulated: the list showed N duplicates for one day while
                # progress tracked only the newest, and which plan applied was undefined.
                superseded = DietPlan.objects.filter(
                    user=self.user, is_active=True,
                    start_date__lte=ed, end_date__gte=sd,
                ).update(is_active=False)
                if superseded:
                    log_json(self.logger, "info", "Superseded overlapping active plans",
                             user_id=self.user.id, count=superseded)

                # Validate AI ingredients are in-DB and allowed by per-meal lists
                categories, macro_pool, name_to_food = self._build_food_category_maps()
                self._validate_ingredients_in_db(plan_output)
                self._validate_ingredients_allowed(plan_output, categories)

                diet_plan = DietPlan.objects.create(
                    user=self.user,
                    goal=self.user.resolve_fitness_goal(),
                    daily_calories=self.user.calculate_daily_calories(),
                    start_date=sd,
                    end_date=ed,
                    duration_weeks=(duration_days + 6) // 7,
                    generated_plan=plan_output.dict(),
                    generation_strategy='GPT',
                )

                piece_weights = self._load_piece_weights()
                # Cache preferences per user for strict category mapping
                strict_index = {k: set(v) for k, v in categories.items()}
                factory = MealPlanFactory(piece_weights)
                # BUG FIX: Safer nested attribute access to prevent AttributeError
                user_pref = getattr(self.user, 'userfoodpreference', None)
                user_allergies = getattr(user_pref, 'allergies', None) if user_pref else None
                validator = MealValidator(
                    user_allergies=user_allergies,
                    category_pool=None,
                    strict=False,
                )

                for i, ai_meal in enumerate(plan_output.plan):
                    meal_date = sd + _timedelta(days=i // (meal_count + snack_count if (meal_count + snack_count) else 1))
                    meal = factory.create_meal(diet_plan, ai_meal, meal_date)

                    from ..meal_processor import MealProcessor
                    meal_processor = MealProcessor(self.user)  # BUG FIX: Pass user instead of None
                    try:
                        resolved = meal_processor.resolve_ingredients_from_ai_meal(ai_meal)
                    except Exception as e:
                        log_json(self.logger, "error", "Failed to resolve ingredients from AI meal", error=str(e))
                        raise PersistenceError(str(e))

                    # Strict mapping only: replace fuzzy with dictionary/index lookups
                    mapped: list[tuple[FoodItem, str]] = []
                    meal_type = ai_meal.meal_type or 'Lunch'
                    identified = {ing.food_id for ing in ai_meal.ingredients if ing.food_id}
                    for food_item, quantity in resolved:
                        if food_item.id in identified:
                            # Chosen by id upstream. Re-mapping by name here is the same
                            # leak in a second place.
                            mapped.append((food_item, quantity))
                            continue
                        dom_macro = self._dominant_macro_of_food(food_item)
                        cat_key = self._cat_key(meal_type, dom_macro)
                        pool = strict_index.get(cat_key, set())
                        if pool and food_item.name not in pool:
                            # Try exact name match by lowercased map; if not found, keep original
                            candidate = name_to_food.get(food_item.name.lower())
                            food_item = candidate or food_item
                        mapped.append((food_item, quantity))

                    # Validate components (allergy/category strict off by default)
                    validated = list(validator.validate(mapped))
                    factory.add_components(meal, validated)

                # Log before normalizers
                try:
                    dates = sorted({m.date for m in diet_plan.meals.all()})
                    for d in dates:
                        log_day_macros('persisted', diet_plan, d)
                except Exception:
                    # Optional side effect: swallowing this silently is what made the
                    # surrounding failures invisible in logs. Control flow is unchanged.
                    logger.debug('suppressed non-fatal error', exc_info=True)

                # --- convergence -------------------------------------------------
                # Replaces seven correctors that ran once each in a fixed order with no
                # shared objective. Traced on a real plan, that chain reached +4.1%
                # (inside tolerance) after stage three and then degraded to -6.6%, and
                # the degraded plan is what shipped. `CalorieTrimmer` trimmed a plan
                # already under target; `MacroShortageBooster` did nothing against a 35%
                # fat gap; `SnackCalorieEnforcer` had to run twice because later stages
                # undid it.
                #
                # The optimiser picks the macro furthest outside its OWN tolerance, keeps
                # a move only if it improved the objective, and returns the best plan it
                # saw. The result is reported rather than hidden.
                from diet.planner.converge import converge_plan

                convergence = converge_plan(diet_plan)
                try:
                    payload = diet_plan.generated_plan
                    if not isinstance(payload, dict):
                        payload = {'raw': payload} if payload else {}
                    payload['convergence'] = convergence
                    diet_plan.generated_plan = payload
                    diet_plan.save(update_fields=['generated_plan'])
                except Exception:
                    logger.debug('could not store convergence report', exc_info=True)

                log_json(
                    self.logger,
                    "info",
                    "Diet plan persisted",
                    user_id=getattr(self.user, "id", None),
                    diet_plan_id=diet_plan.id,
                    meals=len(plan_output.plan),
                )
                # Surface what the allergen checker found. Silence used to be the only
                # signal, and it meant nothing: the old validator filtered on the food's
                # NAME and could not see composition at all. `report` distinguishes an
                # ingredient that definitely violates a declared allergy from one whose
                # allergen data is simply missing — the latter must not pass as safe.
                report = validator.report
                if report.violations or report.unverified:
                    log_json(
                        self.logger,
                        "warning" if report.violations else "info",
                        "Allergen check on generated plan",
                        user_id=self.user.id,
                        diet_plan_id=diet_plan.id,
                        violations=[v.food_name for v in report.violations],
                        unverified_count=len(report.unverified),
                    )
                diet_plan.allergen_report = report.as_dict()
                diet_plan.save(update_fields=['allergen_report'])

                return diet_plan
        except (IntegrityError, ValidationError) as e:
            log_json(self.logger, "error", "Constraint violation while saving diet plan", error=str(e))
            raise ConstraintViolationError(str(e))
        except PersistenceError:
            raise
        except Exception as e:
            log_json(self.logger, "error", "Unexpected persistence error", error=str(e))
            raise PersistenceError(str(e))

    # ---------------------- validations ----------------------
    def _validate_ingredients_in_db(self, plan_output: DietPlanOutput) -> None:
        from ..models import FoodItem
        wanted_lower = set()
        original_names = set()
        for m in plan_output.plan:
            for ing in getattr(m, 'ingredients', []) or []:
                n_raw = (getattr(ing, 'name', '') or '').strip()
                if n_raw:
                    original_names.add(n_raw)
                    wanted_lower.add(n_raw.lower())
        if not wanted_lower:
            return
        # Case-insensitive existence check by loading names and normalizing
        existing_all = set(FoodItem.objects.values_list('name', flat=True))
        existing_lower = {n.lower() for n in existing_all}
        missing = wanted_lower - existing_lower
        if missing:
            raise PersistenceError(f"unknown_foods:{','.join(sorted(missing))}")

    def _validate_ingredients_allowed(self, plan_output: DietPlanOutput, categories: Dict[str, set]) -> None:
        """Ensure all ingredients are allowed for their meal.

        Relaxation: If the user has NO explicit vegetable/fruit entries for a given meal,
        auto-allow a curated list of common vegetables/fruits so planner-added produce doesn't fail.
        """

        DEFAULT_VEGETABLES = {
            'broccoli', 'spinach', 'carrot', 'green bean', 'zucchini', 'bell pepper',
            'cucumber', 'lettuce', 'tomato', 'asparagus', 'cauliflower', 'kale', 'brussels sprouts'
        }
        DEFAULT_FRUITS = {
            'apple', 'banana', 'orange', 'strawberry', 'blueberry', 'mango',
            'pineapple', 'grapes', 'watermelon', 'kiwi'
        }

        def meal_allowed_set(meal_type: str) -> set:
            keys = [
                self._cat_key(meal_type, 'protein'),
                self._cat_key(meal_type, 'carb'),
                self._cat_key(meal_type, 'fat'),
                self._cat_key(meal_type, 'vegetable'),
                self._cat_key(meal_type, 'fruit'),
            ]
            allowed = set()
            for k in keys:
                allowed.update(categories.get(k, set()))

            # Auto-allow defaults only when the user has no explicit veg/fruit for this meal
            veg_key = self._cat_key(meal_type, 'vegetable')
            fruit_key = self._cat_key(meal_type, 'fruit')
            if not categories.get(veg_key):
                allowed.update(DEFAULT_VEGETABLES)
            if not categories.get(fruit_key):
                allowed.update(DEFAULT_FRUITS)

            return {n.lower() for n in allowed}

        # HARD constraints only. This used to reject any food the user had not explicitly
        # categorised, which made sense when the planner could only pick from that same
        # set. Now that preferences RANK the catalogue instead of gating it (see
        # diet.planner.candidates), re-gating here rejected every plan generated for a
        # user with partial preferences — a second copy of the bug that produced the
        # 233 kcal plan. Allergens and explicit dislikes are the real constraints, and
        # both are already enforced in the pool builder and by MealValidator.
        from ..models import UserFoodPreference

        pref = UserFoodPreference.objects.filter(user=self.user).first()
        disliked = {f.name.strip().lower() for f in pref.disliked_foods.all()} if pref else set()
        if not disliked:
            return

        violations = []
        for m in plan_output.plan:
            for ing in getattr(m, 'ingredients', []) or []:
                n = (getattr(ing, 'name', '') or '').strip().lower()
                if n and n in disliked:
                    violations.append(n)
        if violations:
            raise ConstraintViolationError(
                f"disliked_foods:{','.join(sorted(set(violations)))}"
            )

    # ---------------------- internals ----------------------

    def _resolve_start_date(self, start_date: Optional[str]) -> _date:
        if start_date:
            try:
                return _date.fromisoformat(str(start_date))
            except Exception:
                # Optional side effect: swallowing this silently is what made the
                # surrounding failures invisible in logs. Control flow is unchanged.
                logger.debug('suppressed non-fatal error', exc_info=True)
        return timezone.localdate()

    def _load_piece_weights(self) -> Dict[str, float]:
        pw = {
            "egg": 50.0,
            "banana": 118.0,
            "apple": 182.0,
            "orange": 131.0,
            "bread": 28.0,
            "avocado": 200.0,
            "tomato": 123.0,
            "cherry tomato": 17.0,
        }
        try:
            cfg = DietConfig.objects.last()
            if cfg and cfg.piece_weights:
                pw.update(cfg.piece_weights)
        except Exception:
            # Optional side effect: swallowing this silently is what made the
            # surrounding failures invisible in logs. Control flow is unchanged.
            logger.debug('suppressed non-fatal error', exc_info=True)
        return pw

    def _cat_key(self, meal: str, macro: str) -> str:
        macro_key = 'carbs' if macro == 'carb' else macro
        return f"{(meal or '').lower()}_{macro_key}"

    def _build_food_category_maps(self):
        cat_qs = UserFoodCategoryPreference.objects.filter(user=self.user).select_related('food')
        categories: Dict[str, set] = {}
        for m in ("Breakfast", "Lunch", "Dinner", "Snack"):
            for mac in ("carb", "protein", "fat", "vegetable", "fruit"):
                categories[self._cat_key(m, mac)] = set()
        for m in cat_qs:
            categories[self._cat_key(m.meal, m.macro)].add(m.food.name)

        macro_pool: Dict[str, set] = {"carb": set(), "protein": set(), "fat": set(), "vegetable": set(), "fruit": set()}
        for mac in ("carb", "protein", "fat", "vegetable", "fruit"):
            for meal_key, names in categories.items():
                if meal_key.endswith('carbs') and mac == 'carb':
                    macro_pool['carb'].update(names)
                if meal_key.endswith('protein') and mac == 'protein':
                    macro_pool['protein'].update(names)
                if meal_key.endswith('fat') and mac == 'fat':
                    macro_pool['fat'].update(names)
                if meal_key.endswith('vegetable') and mac == 'vegetable':
                    macro_pool['vegetable'].update(names)
                if meal_key.endswith('fruit') and mac == 'fruit':
                    macro_pool['fruit'].update(names)

        name_to_food: Dict[str, FoodItem] = {}
        all_cat_names = set()
        for s in categories.values():
            all_cat_names.update(s)
        if all_cat_names:
            for f in FoodItem.objects.filter(name__in=list(all_cat_names)):
                name_to_food[f.name.lower()] = f

        return categories, macro_pool, name_to_food

    def _determine_meal_template(self, ai_meal: AIMeal) -> str:
        nutrition = ai_meal.total_nutrition
        protein = nutrition.get('protein', 0)
        carbs = nutrition.get('carbs', 0)
        fat = nutrition.get('fat', 0)
        if protein > carbs and protein > fat:
            return 'PROTEIN_CARB' if carbs > fat else 'PROTEIN_FAT'
        elif carbs > protein and carbs > fat:
            return 'CARB_FAT' if fat > protein else 'PROTEIN_CARB'
        else:
            return 'COMPLETE'

    def _dominant_macro_of_food(self, food: FoodItem) -> str:
        """Return 'protein' | 'carb' | 'fat' for a FoodItem by dominant calories."""
        try:
            if food.category:
                if getattr(food.category, 'is_protein', False):
                    return 'protein'
                if getattr(food.category, 'is_carb', False):
                    return 'carb'
                if getattr(food.category, 'is_fat', False):
                    return 'fat'
        except Exception:
            # Optional side effect: swallowing this silently is what made the
            # surrounding failures invisible in logs. Control flow is unchanged.
            logger.debug('suppressed non-fatal error', exc_info=True)
        p_cals = 4.0 * float(getattr(food, 'protein_per_gram', 0.0))
        c_cals = 4.0 * float(getattr(food, 'carbs_per_gram', 0.0))
        f_cals = 9.0 * float(getattr(food, 'fat_per_gram', 0.0))
        if p_cals >= c_cals and p_cals >= f_cals:
            return 'protein'
        if c_cals >= p_cals and c_cals >= f_cals:
            return 'carb'
        return 'fat'


