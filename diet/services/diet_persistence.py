from __future__ import annotations

from typing import Dict, Any, Optional, Tuple, Iterable
from datetime import date as _date, timedelta as _timedelta
from django.utils import timezone
from django.db import IntegrityError, InterfaceError, OperationalError, transaction
from django.core.exceptions import ValidationError

from ..models import DietPlan, Meal, MealComponent, FoodItem, DietConfig
from ..ai_models import DietPlanOutput, AIMeal
from ..utils.nutrition import convert_to_grams, is_piece_food_name, piece_based_grams_if_appropriate
from ..exceptions import PersistenceError, ConstraintViolationError
from ..utils.logging_utils import get_logger, log_json, log_day_macros
from .meal_plan_factory import MealPlanFactory
from .meal_validator import MealValidator
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

                self._require_decided_foods(plan_output)

                from diet.planner.policy import load_policy
                from diet.planner.targets import day_macro_grams
                # Computed once, so the plan row and its targets describe the same client.
                goal = self.user.resolve_fitness_goal()
                # The target the plan was BUILT to, not the profile's default. Persistence
                # recomputed daily_calories from the profile, so a plan generated at
                # 1,400 kcal was stored as 2,695 and converge_plan then re-optimised every
                # meal toward the wrong number: persisted at 2,282 kcal, +63% over what
                # was asked for. The planner records the requested figure; read it.
                requested = ((getattr(plan_output, 'plan_metadata', None) or {})
                             .get('delivery', {}) or {}).get('requested_kcal_per_day')
                daily_calories = float(requested) if requested else self.user.calculate_daily_calories()
                _day = day_macro_grams(float(daily_calories or 0.0), load_policy(goal),
                                       getattr(self.user, 'weight', None))
                targets = {k: round(v, 1) for k, v in _day.items()}
                diet_plan = DietPlan.objects.create(
                    user=self.user,
                    goal=goal,
                    daily_calories=daily_calories,
                    start_date=sd,
                    end_date=ed,
                    duration_weeks=(duration_days + 6) // 7,
                    generated_plan=plan_output.dict(),
                    generation_strategy=DietPlan.ENGINE,
                    target_protein=targets['protein'],
                    target_carbs=targets['carb'],
                    target_fat=targets['fat'],
                )

                piece_weights = self._load_piece_weights()
                factory = MealPlanFactory(piece_weights)
                slot_occurrences: dict = {}
                # From the DATABASE, like `ClientConstraints.for_user` does, not from
                # `self.user.userfoodpreference`. That reverse accessor caches on the
                # instance at first access, so a client who changed their allergies in
                # the same request that generated their plan was checked against the
                # allergies they had before. The safety check must not depend on when
                # something else last looked at the row.
                from ..models import UserFoodPreference

                user_allergies = (UserFoodPreference.objects.filter(user=self.user)
                                  .values_list('allergies', flat=True).first())
                validator = MealValidator(
                    user_allergies=user_allergies,
                    category_pool=None,
                    strict=False,
                    # Every catalogue row now carries a curated allergen composition
                    # (`diet/data/food_allergens.py`), so "we cannot vouch for this" is
                    # a real signal rather than the default state of 346 legacy rows.
                    # It is dropped, not reported and served.
                    drop_unverified=True,
                )

                # Every food the planner chose, in one query for the whole plan. A
                # seven-day plan names about 110 ingredients and used to look each one
                # up on its own.
                decided = FoodItem.objects.in_bulk(
                    {ing.food_id for m in plan_output.plan for ing in m.ingredients})

                for i, ai_meal in enumerate(plan_output.plan):
                    meal_date = sd + _timedelta(days=i // (meal_count + snack_count if (meal_count + snack_count) else 1))
                    # How many of this slot are already on this date, so a second
                    # snack gets a later time than the first and the two rows differ.
                    slot_key = (meal_date, ai_meal.meal_type or 'Lunch')
                    occurrence = slot_occurrences.get(slot_key, 0)
                    slot_occurrences[slot_key] = occurrence + 1
                    meal = factory.create_meal(diet_plan, ai_meal, meal_date,
                                               occurrence=occurrence)

                    mapped = self._resolve_by_id(ai_meal, decided)
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
            # Convergence runs OUTSIDE the transaction that holds the user row lock.
            # A 30-day plan is about 120 optimiser runs and hundreds of UPDATEs; doing
            # that inside select_for_update held one client's row for the whole of it,
            # and Fly's connection budget is small. The rows are committed by now, so a
            # failure here leaves a valid, unconverged plan rather than no plan.
            from diet.planner.converge import converge_plan

            convergence = None
            try:
                convergence = converge_plan(diet_plan)
            except Exception:
                # The plan is committed and active. A failure here must not turn
                # into a 400 that makes the client retry and supersede a good plan.
                #
                # And it must not RETURN either: this skipped the allergen report
                # below, so the one record of what the checker found was lost exactly
                # when something had gone wrong. Carry on without a convergence
                # summary instead.
                log_json(self.logger, "error", "convergence failed after commit",
                         plan_id=diet_plan.id)
            try:
                payload = diet_plan.generated_plan
                if not isinstance(payload, dict):
                    payload = {'raw': payload} if payload else {}
                if convergence is not None:
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
            try:
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
                # Not `update_fields=['allergen_report']`. That re-ran `DietPlan.clean()`
                # through `validate_row()`, which asks a CROSS-ROW question — "does this
                # user already have an overlapping active plan?" — and by now the lock is
                # released, so a second request that superseded this one made this save
                # raise ValidationError for a plan that is already committed and active.
                # The client got an error for the plan they can see, and the allergen
                # report was never stored.
                DietPlan.objects.filter(pk=diet_plan.pk).update(
                    allergen_report=diet_plan.allergen_report)
            except (OperationalError, InterfaceError):
                # The plan is COMMITTED. Letting a transient error escape from here
                # reaches the task's `autoretry_for`, and the retry builds a SECOND plan
                # that supersedes the first — up to four plans for one request, with the
                # client polling the task seeing only the last. Nothing after the commit
                # may ask to be retried.
                logger.warning("could not store the allergen report for plan %s",
                               diet_plan.id, exc_info=True)
            except Exception:
                logger.warning("could not store the allergen report for plan %s",
                               diet_plan.id, exc_info=True)

            return diet_plan
        except (IntegrityError, ValidationError) as e:
            log_json(self.logger, "error", "Constraint violation while saving diet plan", error=str(e))
            raise ConstraintViolationError(str(e))
        except PersistenceError:
            raise
        except (OperationalError, InterfaceError):
            # Transient. Wrapping it as PersistenceError defeated the task's
            # autoretry and answered a database blip with a 400.
            raise
        except Exception as e:
            log_json(self.logger, "error", "Unexpected persistence error", error=str(e))
            raise PersistenceError(str(e))

    # ---------------------- validations ----------------------
    @staticmethod
    def _require_decided_foods(plan_output: DietPlanOutput) -> None:
        """Every ingredient must name the catalogue row it is.

        There used to be a second way in: an ingredient with a `name` and no `food_id`,
        resolved downstream by an exact-name lookup, then a fuzzy `SequenceMatcher` scan
        over the whole catalogue, then — failing both — by CREATING a FoodItem with
        macros from a table of guesses ("Default estimate: 100, 5, 15, 2"). That row
        entered the catalogue with `needs_review=False`, and once a client had eaten it,
        `MealComponent.food` PROTECT made it undeletable.

        That path existed for a generator that no longer exists. The engine names every
        food by id. A plan that does not is not a plan we can price, portion or check for
        allergens, so it is refused here rather than guessed at.
        """
        nameless = [
            (getattr(meal, "meal_type", "?"), getattr(ing, "name", "") or "?")
            for meal in plan_output.plan
            for ing in (getattr(meal, "ingredients", None) or [])
            if getattr(ing, "food_id", None) is None
        ]
        if nameless:
            raise PersistenceError(
                "ingredients_without_a_food_id:"
                + ",".join(f"{slot}/{name}" for slot, name in nameless[:10]))

    @staticmethod
    def _resolve_by_id(ai_meal: AIMeal, decided: dict) -> list:
        """The rows the planner chose, in the order it chose them.

        No name lookup and no fuzzy match: a food the planner cleared for allergens,
        dislikes and cuisine must be the food that is saved.
        """
        rows = []
        for ing in ai_meal.ingredients:
            food = decided.get(ing.food_id)
            if food is None:
                raise PersistenceError(
                    f"planner chose FoodItem {ing.food_id} ({ing.name!r}) "
                    f"but no such row exists")
            rows.append((food, ing.grams if ing.grams is not None else ing.quantity))
        return rows

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
            # `.first()`, not `.last()`. `DietConfig.Meta.ordering` is ['-id'], so
            # `.last()` reverses it and returns the OLDEST row — the planner read a
            # stale piece-weight table while the admin edited the current one.
            cfg = DietConfig.objects.first()
            if cfg and cfg.piece_weights:
                pw.update(cfg.piece_weights)
        except Exception:
            # Optional side effect: swallowing this silently is what made the
            # surrounding failures invisible in logs. Control flow is unchanged.
            logger.debug('suppressed non-fatal error', exc_info=True)
        return pw

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

