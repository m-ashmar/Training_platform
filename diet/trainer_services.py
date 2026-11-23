"""
trainer_services.py - Trainer Diet Plan Management Services

This module provides services for trainers to create and manage diet plans for their clients.
Includes template management, meal scheduling, and nutritional calculations.
"""

import logging
from types import SimpleNamespace
from typing import List, Dict, Any, Optional
from datetime import date, timedelta, time
from django.db import transaction
from django.utils import timezone
from django.core.exceptions import ValidationError
from django.db.models import Q
from django.utils.translation import gettext_lazy as _

from diet.services.meal_payload import component_payload, meal_payload, name_en_of
from .validators import DietInputValidator
from .models import (
    DietPlan, DietPlanTemplate, Meal, MealComponent,
    FoodItem, DailyProgress, UserFoodPreference
)
from users.models import CustomUser

logger = logging.getLogger(__name__)

#: The most meals one plan may be SENT with (BACKEND_BUGS B-132). The design's longest
#: plan is 30 days of 5 meals, 150; this leaves room for 12 weeks of 5, and a longer plan
#: is sent with its first part and grown with `trainer/meals/`. The whole list is written
#: in a handful of queries (`_insert_meals`), not per meal.
MAX_PLAN_MEALS = 500
#: The longest plan in days (BACKEND_BUGS B-133): the 52 weeks `duration_weeks` has always
#: allowed.
MAX_PLAN_DAYS = 364
#: A trainer's own macro split (BACKEND_BUGS B-137; the owner, 2026-09-28) must add up to
#: the plan's calories within this share, either way: 4 kcal a gram of protein and of
#: carbohydrate, 9 of fat.
MACRO_SPLIT_ENERGY_BAND = 0.10
#: The request fields of the split, and the engine's name for each macro.
MACRO_SPLIT_FIELDS = (("target_protein", "protein"), ("target_carbs", "carb"),
                      ("target_fat", "fat"))


def parse_macro_split(body) -> Optional[Dict[str, float]]:
    """The trainer's macro split from a request body, `{protein, carb, fat}` in grams, or
    None when the body sends none of `target_protein`, `target_carbs`, `target_fat`.

    All three or none: a split of one macro says nothing about the other two. Each is a
    positive finite number of grams (not a boolean). Anything else is 400
    `bad_macro_split`. Whether the three fit the plan's calories and goal is
    `TrainerDietPlanService._plan_targets`'s to judge.
    """
    from .validators import _number

    body = body if isinstance(body, dict) else {}
    sent = {name: body.get(name) for name, _key in MACRO_SPLIT_FIELDS}
    given = [name for name, value in sent.items() if value not in (None, '')]
    if not given:
        return None
    if len(given) != len(MACRO_SPLIT_FIELDS):
        raise ValidationError(
            _("Send target_protein, target_carbs and target_fat together, or none of "
              "them."), code="bad_macro_split")
    split = {}
    for name, key in MACRO_SPLIT_FIELDS:
        try:
            grams = _number(sent[name])
        except (TypeError, ValueError):
            grams = None
        if grams is None or grams <= 0:
            raise ValidationError(
                _("target_protein, target_carbs and target_fat must be positive numbers "
                  "of grams."), code="bad_macro_split")
        split[key] = grams
    return split


class MealRefused(ValidationError):
    """A refusal of one meal in a LIST, carrying `index`, its position in the list, so
    the app can say which meal to fix. `code` and the message are the single-meal
    refusal's own; `extra` rides along in the answer (`fields` for `missing_fields`)."""

    def __init__(self, error, index, **extra):
        messages = getattr(error, 'messages', None) or [str(error)]
        super().__init__(messages[0], code=getattr(error, 'code', None) or 'invalid')
        self.index = index
        self.extra = extra


class UndoWindowClosed(ValidationError):
    """A meal confirmed on an earlier day cannot be undone (Road to 10 S3.5).
    `confirmed_on` is that day, or None when the confirmation carries no time."""

    def __init__(self, confirmed_on):
        super().__init__(_("A meal can be undone only on the day it was confirmed."),
                         code="undo_window_closed")
        self.confirmed_on = confirmed_on


def progress_payload(row) -> Dict[str, Any]:
    """One day's figures, from a `DailyProgress` row or `DailyProgress.figures_for`."""
    return {
        'date': row.date,
        'meals_completed': row.meals_completed,
        'total_meals': row.total_meals,
        'completion_percentage': row.completion_percentage,
        'calories_consumed': row.calories_consumed,
        'target_calories': row.target_calories,
        'calories_percentage': row.calories_percentage,
        'protein_consumed': row.protein_consumed,
        'target_protein': row.target_protein,
        'carbs_consumed': row.carbs_consumed,
        'target_carbs': row.target_carbs,
        'fat_consumed': row.fat_consumed,
        'target_fat': row.target_fat,
        'is_day_completed': row.is_day_completed,
    }


class TrainerDietPlanService:
    """
    Service for trainers to create and manage diet plans for their clients.
    """
    
    def __init__(self, trainer: CustomUser):
        """
        Initialize the service for a specific trainer.
        
        Args:
            trainer: The trainer user instance
        """
        if not (trainer.is_trainer or trainer.is_admin):
            raise ValidationError(_("User must be a trainer to use this service"), code="not_a_trainer")
        self.trainer = trainer
        #: An admin acts on behalf of support and is not anybody's assigned trainer,
        #: so the assignment tests below would refuse them. Widening the view gate
        #: without widening this one only changed the wording of the 403.
        self.is_support = bool(trainer.is_admin)
    
    def get_available_templates(self) -> List[DietPlanTemplate]:
        """
        Get all available diet plan templates.
        
        Returns:
            List of active diet plan templates
        """
        return DietPlanTemplate.objects.filter(is_active=True).order_by('meals_per_day', 'snacks_per_day')
    
    def create_diet_plan(
        self,
        client: CustomUser,
        template: DietPlanTemplate,
        start_date: date,
        duration_weeks: int = 4,
        goal: str = 'Maintain',
        daily_calories: Optional[float] = None,
        *,
        duration_days=None,
        meals: Optional[List[Dict[str, Any]]] = None,
        macro_split: Optional[Dict[str, float]] = None,
    ) -> DietPlan:
        """
        Create a new diet plan for a client using a template.

        Two ways (BACKEND_BUGS B-132). Creating a plan used to retire every active plan
        of the client that overlapped it — his automatic plan included — before a single
        meal existed, while the meals came in later requests: a refused meal list or a
        dropped phone left him an empty active plan and his own plan gone.

        * `meals` given (a list of parsed meals, see `prepare_meals`): the plan is SENT
          whole. Plan, meals and the retirement of what it overlaps happen in one
          transaction, after every meal is valid; one refused meal (`MealRefused`, with
          its `index`) and nothing is written or retired.
        * `meals` None (the old call): a DRAFT (`is_draft`, not active) that retires
          nothing and that the client never sees. Its first meals send it
          (`add_meal_to_plan`), in the transaction that writes them.

        The length is `duration_days` (1-364, BACKEND_BUGS B-133) when given, else
        `duration_weeks` (1-52) as before; the stored `duration_weeks` is the days
        rounded up to whole weeks, as the engine stores it.

        Args:
            client: The client user
            template: The diet plan template to use
            start_date: Start date for the plan
            duration_weeks: Duration in weeks
            goal: Fitness goal (Lose, Maintain, Gain)
            daily_calories: Daily calorie target (auto-calculated if None)
            duration_days: Duration in days; wins over `duration_weeks`
            meals: The plan's meals, to send it whole; None for a draft
            macro_split: The trainer's own `{protein, carb, fat}` grams
                (`parse_macro_split`, BACKEND_BUGS B-137), stored instead of the
                goal's derived split; None derives it as before. Set here, while the
                plan is written: no route edits the split of a plan once it exists.

        Returns:
            The DietPlan, carrying `superseded_plan_ids` (what this call retired) and
            `will_replace_plan_ids` (for a draft: the active plans its sending would
            retire, as they stand now).
        """
        # Validate trainer-client relationship
        if not self._can_manage_client(client):
            raise ValidationError(_("You can only create diet plans for your assigned clients"), code="not_assigned_client")

        daily_calories, resolved_goal, macros = self._plan_targets(
            client, goal, daily_calories, macro_split)
        end_date, duration_weeks = self._plan_window(start_date, duration_weeks, duration_days)

        fields = dict(
            user=client,
            goal=resolved_goal,
            daily_calories=daily_calories,
            target_protein=macros['protein'],
            target_carbs=macros['carb'],
            target_fat=macros['fat'],
            start_date=start_date,
            end_date=end_date,
            duration_weeks=duration_weeks,
            generation_strategy=DietPlan.TRAINER,
            template=template,
            created_by=self.trainer,
            # Written as a draft either way: sending is `_publish`, the one place that
            # retires what a trainer plan replaces.
            is_draft=True,
            is_active=False,
        )

        if meals is None:
            diet_plan = DietPlan.objects.create(**fields)
            diet_plan.superseded_plan_ids = []
            diet_plan.will_replace_plan_ids = self._overlapping_active(diet_plan)
            return diet_plan

        with transaction.atomic():
            # Serialise on the client's row, exactly as `save_plan` does, so two plans
            # sent at once cannot each retire what they saw and both stay active.
            type(client).objects.select_for_update().filter(pk=client.pk).first()
            diet_plan = DietPlan.objects.create(**fields)
            self._insert_meals(diet_plan, meals)
            superseded_ids = self._publish(diet_plan)
        self._recompute_allergens(diet_plan)
        # The caller reports these to the trainer; a plan vanishing with no
        # explanation is what made this feature look broken.
        diet_plan.superseded_plan_ids = superseded_ids or []
        diet_plan.will_replace_plan_ids = []
        return diet_plan

    def _plan_targets(self, client, goal, daily_calories, macro_split=None):
        """`(daily_calories, goal, macro grams)` for a plan written for `client`: the
        figure the trainer set, or the one computed from the client's profile.

        The grams are the trainer's `macro_split` when given (BACKEND_BUGS B-137), else
        the goal's split derived from the calories, as the engine builds to. A split is
        refused, 400 `bad_macro_split`, when its energy (4P + 4C + 9F) is more than
        `MACRO_SPLIT_ENERGY_BAND` from the calories, or its protein is outside the
        engine's bounds for the goal (`protein_floor_g` .. `protein_ceiling_g`).
        """
        # Calculate daily calories if not provided. Not from an activity level the client
        # never chose (the owner, 2026-09-20): the default would pass for sedentary.
        if daily_calories is None:
            if getattr(client, 'activity_level_unanswered', False):
                raise ValidationError(
                    _("This client has not chosen an activity level yet. Ask them to, or "
                      "set the daily calories yourself."),
                    code="client_activity_level_required")
            daily_calories = client.calculate_daily_calories(goal)

        # Validate the calorie target server-side (bounds + type). Prevents a
        # trainer from persisting a medically dangerous target (e.g. 200 or 50000).
        daily_calories = DietInputValidator.validate_daily_calories(daily_calories)
        resolved_goal = DietPlan.normalise_goal(goal)

        # The same macro split the deterministic planner builds to. A trainer plan
        # left these null, so the client's progress screen fell back to a generic
        # ratio and disagreed with every generated plan about the same numbers.
        from diet.planner.policy import load_policy
        from diet.planner.targets import day_macro_grams
        policy = load_policy(resolved_goal)
        if macro_split is not None:
            return daily_calories, resolved_goal, self._check_split(
                macro_split, float(daily_calories), policy)
        macros = day_macro_grams(float(daily_calories), policy,
                                 getattr(client, 'weight', None))
        return daily_calories, resolved_goal, macros

    @staticmethod
    def _check_split(split, daily_calories: float, policy) -> Dict[str, float]:
        """The trainer's split, when it fits the calories and the goal; else 400
        `bad_macro_split`. The owner's bounds (2026-09-28)."""
        energy = 4 * split['protein'] + 4 * split['carb'] + 9 * split['fat']
        if abs(energy - daily_calories) > MACRO_SPLIT_ENERGY_BAND * daily_calories:
            raise ValidationError(
                _("These macros come to %(energy)d kcal a day; they must be within "
                  "%(band)d percent of the daily calories (%(calories)d kcal).")
                % {"energy": round(energy), "band": round(MACRO_SPLIT_ENERGY_BAND * 100),
                   "calories": round(daily_calories)},
                code="bad_macro_split")
        low, high = policy.protein_floor_g, policy.protein_ceiling_g
        if not low <= split['protein'] <= high:
            raise ValidationError(
                _("Protein must be between %(low)d and %(high)d g a day for this goal.")
                % {"low": round(low), "high": round(high)},
                code="bad_macro_split")
        return {'protein': split['protein'], 'carb': split['carb'], 'fat': split['fat']}

    @staticmethod
    def _plan_window(start_date, duration_weeks, duration_days=None):
        """`(end_date, duration_weeks)`: the plan's last day, INCLUSIVE, and its weeks.

        `duration_days` when given (BACKEND_BUGS B-133: the design's 30 days could only
        be asked for as 35), else `duration_weeks`.
        """
        if duration_days is not None:
            days = TrainerDietPlanService.parse_duration_days(duration_days)
            weeks = (days + 6) // 7
        else:
            try:
                weeks = int(duration_weeks)
            except (TypeError, ValueError):
                raise ValidationError(_("duration_weeks must be a whole number"),
                                      code="bad_duration")
            if not 1 <= weeks <= 52:
                raise ValidationError(_("duration_weeks must be between 1 and 52"),
                                      code="bad_duration")
            days = weeks * 7
        # INCLUSIVE, matching `DietPlan.end_date` and the engine. `start + weeks` made a
        # "4 week" plan span 29 days: the trainer got a day they did not ask for,
        # `add_meal_to_plan` accepted a meal on it, and two back-to-back four-week plans
        # overlapped on one day, so creating the second silently superseded the first.
        end_date = start_date + timedelta(days=days - 1) if start_date else None
        return end_date, weeks

    @staticmethod
    def parse_duration_days(value) -> int:
        """A whole number of days from 1 to `MAX_PLAN_DAYS`, or 400 `bad_duration`.

        Refused, not clamped: a length is a choice the trainer made, and 7.5 or 400 days
        is a mistake to show them, not one to correct silently.
        """
        refusal = ValidationError(
            _("duration_days must be a whole number from 1 to %(max)d") % {"max": MAX_PLAN_DAYS},
            code="bad_duration")
        if isinstance(value, (bool, float)):
            raise refusal
        try:
            days = int(str(value).strip())
        except (TypeError, ValueError):
            raise refusal
        if not 1 <= days <= MAX_PLAN_DAYS:
            raise refusal
        return days

    @staticmethod
    def _overlapping_active(diet_plan) -> List[int]:
        """The client's active plans whose dates overlap `diet_plan`'s, whoever wrote
        them: what sending it retires."""
        return list(DietPlan.objects.filter(
            user_id=diet_plan.user_id, is_active=True,
            start_date__lte=diet_plan.end_date, end_date__gte=diet_plan.start_date,
        ).exclude(pk=diet_plan.pk).order_by('id').values_list('id', flat=True))

    def _publish(self, diet_plan) -> Optional[List[int]]:
        """SEND a draft: retire every active plan it overlaps and make it the client's.

        Returns the ids it retired, or None when it was not a draft (already sent — by a
        request that raced this one, too). Runs inside the caller's transaction, so the
        meals that trigger it and the retirement land together or not at all.

        One active plan per client per window is the rule (`DietPlan.clean`), and it
        made trainer plan creation fail with an opaque 400 for any client who already
        had one — every client the engine has served. Retiring the overlap, whoever
        wrote it (the client's own generated plan is the common case), is what the
        trainer means by "here is your new plan"; the ids go back so they can say what
        was replaced.
        """
        with transaction.atomic():
            # Serialise on the client's row, exactly as `save_plan` does. Superseding
            # and then activating is two statements, and without the lock two plans
            # (or a plan and the client's own generate) could each supersede what they
            # saw, leaving two active plans over one window.
            CustomUser.objects.select_for_update().filter(pk=diet_plan.user_id).first()
            # NO KEY UPDATE, not UPDATE: a second request writing this draft's first
            # meals holds KEY SHARE on the row (its Meal insert), and a plain FOR UPDATE
            # would wait for it while it waits for the client's row — a deadlock.
            still_draft = (DietPlan.objects.select_for_update(no_key=True)
                           .filter(pk=diet_plan.pk).values_list('is_draft', flat=True).first())
            if not still_draft:
                diet_plan.refresh_from_db(fields=['is_draft', 'is_active'])
                return None
            # Never over days the client already lived. A draft's window is fixed when it
            # is made, and sending it retires every plan it overlaps: a draft sent after
            # its start date retired the plan the client ate from on those days, their
            # ticks dropped out of every progress read, and the trainer's never-shown
            # meals scored those days 0 for good (the review, 2026-09-24). Refused, and
            # the meal write that triggered it rolls back with it: re-date the plan.
            if diet_plan.start_date and diet_plan.start_date < timezone.localdate():
                raise ValidationError(
                    _("This plan starts on a day that has passed. Choose a start date "
                      "from today on, then send it."), code="draft_start_passed")
            superseded_ids = self._overlapping_active(diet_plan)
            today = timezone.localdate()
            DietPlan.objects.filter(id__in=superseded_ids).update(
                is_active=False, superseded_on=today)
            DietPlan.objects.filter(pk=diet_plan.pk).update(
                is_draft=False, is_active=True, updated_at=timezone.now())
            # A queryset update fires no signal: retire the client's home here, after
            # the commit, so their first read shows the plan (Road to 10 S0.6).
            from training_platform.signals import bust_home_on_commit
            bust_home_on_commit(diet_plan.user_id)
        diet_plan.is_draft, diet_plan.is_active = False, True
        if superseded_ids:
            logger.info("Trainer %s sent plan %s, replacing %s for client %s",
                        self.trainer.id, diet_plan.pk, superseded_ids, diet_plan.user_id)
        return superseded_ids

    @staticmethod
    def _check_slot(start_date, end_date, meal_type, target_date) -> None:
        """A meal type on the list, on a day the plan covers."""
        # `choices` is a form-layer rule; nothing called full_clean(), so "Brunch" was
        # persisted and then matched no slot in the target split downstream.
        if meal_type not in dict(Meal.MEAL_TYPES):
            raise ValidationError(_("%(value)s is not a meal type") % {"value": meal_type},
                                  code="bad_meal_type")
        if start_date and end_date and not (start_date <= target_date <= end_date):
            raise ValidationError(
                _("That date is outside the plan (%(a)s to %(b)s)")
                % {"a": start_date, "b": end_date},
                code="date_outside_plan")

    def prepare_meals(self, start_date, end_date, meals, client=None):
        """Every meal of a plan checked as `add_meal_to_plan` checks one; nothing written.

        `meals` is a list of `{meal_type, target_date (date), food_items,
        scheduled_time (time or None), description}`. Returns `[(meal, components)]`
        with `components` `[(FoodItem, grams)]`. A refusal is `MealRefused` naming the
        meal's `index`. With `client`, a declared allergen is refused as it is on the
        meal route; without, it is left for the review to report.
        """
        taken, prepared = set(), []
        for index, meal in enumerate(meals):
            try:
                self._check_slot(start_date, end_date, meal['meal_type'], meal['target_date'])
                # The slot key the database enforces (`one_meal_per_slot_per_day`,
                # NULL times equal), checked here because the rows are new.
                slot = (meal['target_date'], meal['meal_type'], meal.get('scheduled_time'))
                if slot in taken:
                    raise ValidationError(
                        _("A %(slot)s already exists for %(date)s in this plan")
                        % {"slot": meal['meal_type'], "date": meal['target_date']},
                        code="duplicate_meal")
                taken.add(slot)
                components = self._validated_items(meal['food_items'], meal['meal_type'],
                                                   client=client)
            except MealRefused:
                raise
            except ValidationError as refusal:
                raise MealRefused(refusal, index)
            prepared.append((meal, components))
        return prepared

    def _insert_meals(self, diet_plan, meals) -> None:
        """Write a new plan's meals in a few queries, whatever their number.

        The same checks as `add_meal_to_plan` (`prepare_meals`), the same names and
        reasons, the same row validation minus the two foreign-key lookups (the plan
        was created in this transaction; no recipe). One insert for the meals and one
        for their components: a 150-meal plan at `add_meal_to_plan`'s six queries a
        meal was 900 round trips inside one transaction.
        """
        prepared = self.prepare_meals(diet_plan.start_date, diet_plan.end_date, meals,
                                      client=diet_plan.user)
        rows = []
        for index, (meal, components) in enumerate(prepared):
            name, name_ar, reason, reason_ar = self._meal_names(meal['meal_type'], components)
            row = Meal(diet_plan=diet_plan, meal_type=meal['meal_type'],
                       date=meal['target_date'], scheduled_time=meal.get('scheduled_time'),
                       description=meal.get('description') or '', is_ai_generated=False,
                       name=name[:160], name_ar=name_ar[:160], reason=reason[:200],
                       reason_ar=reason_ar[:200])
            try:
                row.full_clean(exclude={'diet_plan', 'recipe'}, validate_unique=False,
                               validate_constraints=False)
            except ValidationError as refusal:
                raise MealRefused(refusal, index)
            rows.append(row)
        Meal.objects.bulk_create(rows)
        MealComponent.objects.bulk_create([
            MealComponent(meal=row, food=food, quantity=grams, meal_time=row.meal_type)
            for row, (_meal, components) in zip(rows, prepared)
            for food, grams in components
        ])

    def add_meal_to_plan(
        self,
        diet_plan: DietPlan,
        meal_type: str,
        target_date: date,
        food_items: List[Dict[str, Any]],
        scheduled_time: Optional[time] = None,
        description: str = "",
        recompute_allergens: bool = True,
    ) -> Meal:
        """
        Add a meal to a diet plan. On a draft, the first meal sends the plan
        (`_publish`) in the same transaction; `diet_plan.publication` then says so.

        Args:
            diet_plan: The diet plan to add the meal to
            meal_type: Type of meal (Breakfast, Lunch, Dinner, Snack)
            target_date: Date for the meal
            food_items: List of dicts with 'food_id' and 'quantity' keys
            scheduled_time: Optional scheduled time for the meal
            description: Optional meal description
            
        Returns:
            Created Meal instance
        """
        self._authorize_plan(diet_plan)
        self._check_slot(diet_plan.start_date, diet_plan.end_date, meal_type, target_date)

        # A retried POST wrote a second Breakfast for the same day, and the database
        # cannot refuse it: `unique_together` includes `scheduled_time`, which is
        # optional, and Postgres treats NULLs as distinct. The client then saw the
        # meal twice and could never reach 100%.
        clash = Meal.objects.filter(diet_plan=diet_plan, date=target_date,
                                    meal_type=meal_type, scheduled_time=scheduled_time)
        if clash.exists():
            raise ValidationError(
                _("A %(slot)s already exists for %(date)s in this plan")
                % {"slot": meal_type, "date": target_date},
                code="duplicate_meal")

        components = self._validated_items(food_items, meal_type, client=diet_plan.user)
        name, name_ar, reason, reason_ar = self._meal_names(meal_type, components)

        # Atomic. A second line failing left the Meal row and the first component
        # behind, so the client saw a half-written meal the trainer never saved.
        with transaction.atomic():
            meal = Meal.objects.create(
                diet_plan=diet_plan,
                meal_type=meal_type,
                date=target_date,
                scheduled_time=scheduled_time,
                description=description,
                is_ai_generated=False,
                name=name[:160],
                name_ar=name_ar[:160],
                reason=reason[:200],
                reason_ar=reason_ar[:200],
            )
            MealComponent.objects.bulk_create([
                MealComponent(meal=meal, food=food, quantity=grams, meal_time=meal_type)
                for food, grams in components
            ])
            # A draft's first meal SENDS it (BACKEND_BUGS B-132), in this transaction:
            # the plan the old create made retires the client's plan only now that it
            # holds something to eat, and a refused meal sends nothing. The caller
            # reads `publication` off the plan to tell the trainer what was replaced.
            if diet_plan.is_draft:
                superseded_ids = self._publish(diet_plan)
                if superseded_ids is not None:
                    diet_plan.publication = {"diet_plan_id": diet_plan.pk,
                                             "superseded_plan_ids": superseded_ids}
        # The batch endpoint passes False and recomputes ONCE at the end. The report is
        # derived from every component of the whole plan, so doing it per meal made a
        # hundred-meal batch re-derive it a hundred times — about twelve queries per meal
        # inside one transaction, two seconds of it, on a small connection budget.
        if recompute_allergens:
            self._recompute_allergens(diet_plan)
        return meal
    
    def update_meal(
        self,
        meal: Meal,
        food_items: List[Dict[str, Any]] = None,
        scheduled_time: Optional[time] = None,
        description: str = None
    ) -> Meal:
        """
        Update an existing meal.
        
        Args:
            meal: The meal to update
            food_items: New food items (optional)
            scheduled_time: New scheduled time (optional)
            description: New description (optional)
            
        Returns:
            Updated Meal instance
        """
        self._authorize_plan(meal.diet_plan)

        # A meal the client has already lived through is a RECORD, not a plan. The
        # component rows carry `is_completed`, `completed_at` and
        # `actual_quantity_consumed`, and replacing them deletes all three: a trainer
        # tidying up last month's plan silently zeroed days the client actually ate,
        # and the coach and analytics layers then read the zeros as non-compliance.
        # Resetting completion is right for a FUTURE meal; nothing distinguished the two.
        if food_items is not None and meal.date < timezone.localdate():
            raise ValidationError(
                _("That meal is in the past and records what the client ate. "
                  "Change a future meal instead."),
                code="meal_already_happened")

        components = (self._validated_items(food_items, meal.meal_type,
                                            client=meal.diet_plan.user)
                      if food_items is not None else None)

        # Moving a meal onto a slot another meal already holds is "that slot is taken",
        # not a server fault. `add_meal_to_plan` has an explicit `duplicate_meal`
        # pre-check and this had none, so `one_meal_per_slot_per_day` fired at the
        # database and the view's broad handler answered
        # `{"code": "server_error"}` with a 500. The data was safe — the atomic block
        # rolls back cleanly — but the answer was wrong and it spent the error budget.
        if scheduled_time is not None and scheduled_time != meal.scheduled_time:
            clash = Meal.objects.filter(
                diet_plan=meal.diet_plan, date=meal.date, meal_type=meal.meal_type,
                scheduled_time=scheduled_time).exclude(pk=meal.pk)
            if clash.exists():
                raise ValidationError(
                    _("A %(slot)s already exists at that time on %(date)s")
                    % {"slot": meal.meal_type, "date": meal.date},
                    code="duplicate_meal")

        # Atomic, because the old components are deleted before the new ones exist:
        # a failure halfway through emptied the client's meal.
        with transaction.atomic():
            # The meal's row lock FIRST, the order the client's confirm and undo take
            # (meal, then its foods, then the day): deleting the foods and then saving
            # the meal deadlocked against a confirm of the same meal (Road to 10 S3.5,
            # the phase-3 review).
            list(Meal.objects.select_for_update().filter(pk=meal.pk)
                 .values_list('pk', flat=True))
            if scheduled_time is not None:
                meal.scheduled_time = scheduled_time
            if description is not None:
                meal.description = description
                # The engine stores an Arabic description beside the English one
                # (`translations['ar']['description']`, read as `description_ar`). It
                # described the dish as the engine wrote it; left in place, an edit was
                # invisible to an Arabic reader. Dropped, `description_ar` falls back to
                # the text written here.
                translations = dict(meal.translations or {})
                arabic = dict(translations.get('ar') or {})
                if arabic.pop('description', None) is not None:
                    translations['ar'] = arabic
                    meal.translations = translations
            if components is not None:
                name, name_ar, reason, reason_ar = self._meal_names(meal.meal_type, components)
                meal.name, meal.name_ar = name[:160], name_ar[:160]
                meal.reason, meal.reason_ar = reason[:200], reason_ar[:200]
                meal.components.all().delete()
                MealComponent.objects.bulk_create([
                    MealComponent(meal=meal, food=food, quantity=grams,
                                  meal_time=meal.meal_type)
                    for food, grams in components
                ])
                # The client did not eat what the trainer has just put here. The meal
                # kept `is_completed=True` over entirely different food, and
                # DailyProgress sums the components of completed meals — so swapping
                # 100 g of eggs for 2000 g of rice added 2,600 kcal the client never
                # ate, while the same meal reported 0% complete.
                meal.is_completed = False
                # The components carry the completion marks; they were just deleted
                # and replaced, so the new ones start uncompleted by default.
            meal.save()
            if components is not None:
                self._recompute_day(meal)
        self._recompute_allergens(meal.diet_plan)
        return meal

    @staticmethod
    def _recompute_day(meal) -> None:
        """Bring the client's stored progress back in line with the meals as they are.

        `DailyProgress` is a cache of the day's meals, and nothing invalidated it when
        a trainer rewrote one.
        """
        from .models import DailyProgress

        try:
            progress = DailyProgress.objects.filter(
                user=meal.diet_plan.user, diet_plan=meal.diet_plan, date=meal.date).first()
            if progress is not None:
                progress.update_progress()
        except Exception:
            logger.exception("Could not recompute progress for meal %s", meal.pk)
    
    def delete_meal(self, meal: Meal) -> bool:
        """
        Delete a meal from a diet plan.
        
        Args:
            meal: The meal to delete
            
        Returns:
            True if deleted successfully
        """
        self._authorize_plan(meal.diet_plan)
        # Remember the day before the row is gone: `DailyProgress` counts this plan's
        # meals for that date, and deleting one left the client's day showing 3 of 4
        # forever. `update_meal` and `add_meal_to_plan` both recompute; this did not.
        stale = SimpleNamespace(diet_plan=meal.diet_plan, date=meal.date, pk=meal.pk)
        plan = meal.diet_plan
        meal.delete()
        self._recompute_day(stale)
        self._recompute_allergens(plan)
        return True
    
    def get_client_diet_plans(self, client: CustomUser) -> List[DietPlan]:
        """
        Get all diet plans for a specific client.
        
        Args:
            client: The client user
            
        Returns:
            List of diet plans for the client
        """
        if not self._can_manage_client(client):
            raise ValidationError(_("You can only view diet plans for your assigned clients"), code="not_assigned_view")
        
        # Counted in the query and the template fetched with it: the view read
        # `plan.meals.count()` and `plan.template` per row, so a trainer with a long
        # history paid two queries per plan to render one list.
        from django.db.models import Count

        # Support sees the client's whole history, not just the plans it authored.
        # `_authorize_plan` already exempts `is_support` from the authorship test and the
        # view already admits admins, so an admin could WRITE to a plan this list would
        # not show them: `?client_id=N` came back `{"count": 0, "results": []}`.
        plans = DietPlan.objects.filter(user=client)
        if not self.is_support:
            plans = plans.filter(created_by=self.trainer)
        return (plans
                .select_related('template')
                .annotate(meals_count=Count('meals'))
                # `-id` breaks ties: two plans starting the same day (a draft and the
                # plan it replaces) came back in no defined order.
                .order_by('-start_date', '-id')[:200])
    
    def _can_manage_client(self, client: CustomUser) -> bool:
        """
        Check if trainer can manage this client.
        
        Args:
            client: The client to check
            
        Returns:
            True if trainer can manage this client
        """
        # One gate for the whole platform: `can_access_user_data`, which reads the
        # approved TrainerClientRelation. This used to test the `assigned_trainer`
        # pointer, then both, and the routine app tested something else again — three
        # spellings of one rule. The relation is the record that carries consent, the
        # agreed price and the escrow hold, so it is the one that decides.
        return self.trainer.can_access_user_data(client)

    def _authorize_plan(self, diet_plan: DietPlan) -> None:
        """Every write to a plan, checked the same way.

        Authorship alone was the test, so authority granted once was never revoked:
        a trainer the client had dismissed could still rewrite or delete every meal of
        that client's live plan, because they had created it. Being the author AND
        still being this client's trainer are both required.
        """
        if not self.is_support and diet_plan.created_by_id != self.trainer.id:
            raise ValidationError(_("You can only modify diet plans you created"),
                                  code="not_plan_owner")
        if not self._can_manage_client(diet_plan.user):
            raise ValidationError(_("This client is no longer assigned to you"),
                                  code="not_assigned_client")
        # A draft (BACKEND_BUGS B-132) is not followed YET, which is different: it is
        # the plan being written, and its first meals send it.
        if not diet_plan.is_active and not diet_plan.is_draft:
            # A plan the client no longer follows. `diet_persistence` identified this
            # exact failure — "every meal they added to it afterwards landed on a plan
            # nobody follows" — and fixed only the LOGGING on the superseding side. The
            # client taps Generate, silently retiring the trainer's plan; the trainer
            # then spends an afternoon building next week's meals, gets 200 on every one,
            # and the client sees an empty day.
            raise ValidationError(
                _("This plan has been replaced by a newer one and is no longer followed"),
                code="plan_superseded")

    #: One legal range for a component, the same one `DietInputValidator` applies
    #: everywhere else. This class declared its own 0-2000 while every other writer
    #: used 1-1000, so the same gram figure was accepted here and refused there, and
    #: 0.0001 g passed as a component that renders as "0 g".

    def _validated_items(self, food_items, meal_type: str, client=None):
        """Turn the request's food list into rows we are willing to persist.

        Nothing here was checked: a missing food id raised `DoesNotExist` into a
        generic 400, a negative quantity became negative calories on the client's
        progress screen, and two lines naming the same food hit the unique constraint
        halfway through and left the meal half-built.
        """
        if not food_items:
            raise ValidationError(_("A meal needs at least one food"), code="no_food_items")
        if not isinstance(food_items, (list, tuple)):
            raise ValidationError(_("food_items must be a list"), code="bad_food_items")

        seen, rows = set(), []
        for entry in food_items:
            if not isinstance(entry, dict):
                raise ValidationError(_("Each food item must be an object"), code="bad_food_item")
            try:
                raw_id, raw_grams = entry.get('food_id'), entry.get('quantity')
                # `int(True)` is 1 and `int(12.9)` is 12 — a silently different food.
                if isinstance(raw_id, bool) or isinstance(raw_grams, bool):
                    raise TypeError("booleans are not ids or quantities")
                food_id = int(raw_id)
                if float(raw_id) != food_id:
                    raise ValueError("food_id must be a whole number")
                grams = float(raw_grams)
            except (TypeError, ValueError):
                raise ValidationError(_("Each food item needs a numeric food_id and quantity"),
                                      code="bad_food_item")
            if not (DietInputValidator.MIN_QUANTITY <= grams <= DietInputValidator.MAX_QUANTITY):
                raise ValidationError(
                    _("Quantity for food %(id)s must be between %(lo)d and %(hi)d grams")
                    % {"id": food_id, "lo": int(DietInputValidator.MIN_QUANTITY),
                       "hi": int(DietInputValidator.MAX_QUANTITY)},
                    code="bad_quantity")
            if food_id in seen:
                raise ValidationError(
                    _("Food %(id)s appears twice in this meal") % {"id": food_id},
                    code="duplicate_food")
            seen.add(food_id)
            rows.append((food_id, grams))

        # Fetched once per service instance (one request), not once per meal: a plan
        # sent whole repeats the same few foods over a hundred meals.
        found = self.__dict__.setdefault('_foods', {})
        wanted = [fid for fid, _g in rows if fid not in found]
        if wanted:
            found.update(FoodItem.objects.in_bulk(wanted))
        missing = [fid for fid, _g in rows if fid not in found]
        if missing:
            raise ValidationError(_("No such food: %(ids)s") % {"ids": missing},
                                  code="unknown_food")
        items = [(found[fid], grams) for fid, grams in rows]
        self._refuse_declared_allergens(items, client)
        return items

    def _allergen_checker_for(self, client):
        """This client's checker, read from the database once per service instance.

        From the DATABASE, not `client.userfoodpreference`: that reverse accessor caches
        on the instance at first access, so a caller that had touched it before the
        allergies were written saw them as they were, and the check silently did nothing.
        Cached HERE instead, per service, because the bulk endpoint validates up to a
        hundred meals for the same client in one request.
        """
        from diet.services.meal_validator import AllergenChecker

        cache = getattr(self, "_allergen_checkers", None)
        if cache is None:
            cache = self._allergen_checkers = {}
        key = getattr(client, "pk", None)
        if key not in cache:
            allergies = (UserFoodPreference.objects.filter(user=client)
                         .values_list("allergies", flat=True).first())
            cache[key] = AllergenChecker(allergies)
        return cache[key]

    def _refuse_declared_allergens(self, items, client) -> None:
        """A trainer may not put a client's declared allergen on their plate.

        The generator has checked this since the allergen work landed; the trainer's own
        write never did. A client who declared peanuts got a 200 on a peanut-butter meal
        and `allergen_report` on their plan still said `is_safe: true`, because that field
        is written once at generation and by nothing else.
        """
        if client is None:
            return
        from diet.allergens import label
        from diet.services.meal_validator import VIOLATION

        # Read from the DATABASE, not from the reverse accessor. `client.userfoodpreference`
        # is cached on the instance from the first access, so a caller that touched it
        # before the client edited their allergies — or in the same request that wrote
        # them — got the value as it was, and this check silently did nothing. An
        # allergy check must not depend on when someone last looked.
        checker = self._allergen_checker_for(client)
        if not checker.active:
            return
        refused = [(food, v) for food, _g in items
                   if (v := checker.check_food(food)).verdict == VIOLATION]
        if refused:
            raise ValidationError(
                _("%(food)s contains %(allergens)s, which this client has declared")
                % {"food": refused[0][0].name,
                   "allergens": ", ".join(label(t) for t in refused[0][1].matched)},
                code="declared_allergen")

    @staticmethod
    def _recompute_allergens(plan) -> None:
        """Re-derive `DietPlan.allergen_report` from the meals as they now stand.

        It was written exactly once, by the generator, and never again — so every
        trainer edit and every client swap left the client and their app reading a
        verdict about a plate that no longer existed.
        """
        from diet.services.meal_validator import AllergenChecker, AllergenReport
        from .models import MealComponent

        try:
            allergies = (UserFoodPreference.objects.filter(user=plan.user)
                         .values_list("allergies", flat=True).first())
            checker = AllergenChecker(allergies)
            foods = {c.food for c in MealComponent.objects
                     .filter(meal__diet_plan=plan).select_related("food")}
            report = AllergenReport([checker.check_food(f) for f in foods])
            plan.allergen_report = report.as_dict()
            plan.save(update_fields=["allergen_report"])
        except Exception:
            logger.exception("Could not recompute the allergen report for plan %s", plan.pk)

    @staticmethod
    def _meal_names(meal_type: str, components):
        """The title and reason the client app reads, in both languages.

        A trainer meal set none of them, so a hand-written meal reached the client with
        an empty reason where a generated one explains itself.
        """
        from diet.planner.recipes import plain_name

        en = [plain_name(f) for f, _g in components][:4]
        ar = [(getattr(f, 'name_ar', None) or f.name) for f, _g in components][:4]

        def join(parts, sep, last):
            if not parts:
                return ''
            if len(parts) == 1:
                return parts[0]
            return sep.join(parts[:-1]) + last + parts[-1]

        from diet.services.rule_based_planner import FOR_SLOT_AR, SLOT_AR

        slot_ar = SLOT_AR.get(meal_type, meal_type)
        # "for <slot>" whole, from the planner's table: `لوجبة {slot_ar}` read «لوجبة
        # الوجبة الخفيفة» for a snack (BACKEND_BUGS B-063, the same fault as the engine's).
        for_slot_ar = FOR_SLOT_AR.get(meal_type, f"لوجبة {slot_ar}")
        return (join(en, ', ', ' and ') or meal_type,
                join(ar, '، ', ' و') or slot_ar,
                str(_("chosen for you by your trainer")),
                f"اختارها لك مدربك {for_slot_ar}")

class ClientProgressService:
    """
    Service for tracking client progress and meal completion.
    """
    
    def __init__(self, client: CustomUser):
        """
        Initialize the service for a specific client.
        
        Args:
            client: The client user instance
        """
        if not client.is_client:
            raise ValidationError(_("User must be a client to use this service"), code="not_client")
        self.client = client
    
    def complete_meal_component(
        self,
        component: MealComponent,
        actual_quantity: Optional[float] = None
    ) -> MealComponent:
        """
        Mark a meal component as completed.
        
        Args:
            component: The meal component to complete
            actual_quantity: Actual quantity consumed (optional)
            
        Returns:
            Updated MealComponent instance
        """
        # Validate client owns this component
        if component.meal.diet_plan.user != self.client:
            raise ValidationError(_("You can only complete your own meal components"), code="not_component_owner")

        # And validate the AMOUNT. This came straight from a request body into the
        # client's own progress figures and into the planner's learned weights, with
        # nothing between: `actual_quantity: -500` was accepted with a 200 and stored,
        # and so was 1,000,000,000, and `"abc"` was a 500. Every other numeric input in
        # this app goes through DietInputValidator; this one was missed because it is
        # optional. Zero is a real answer — "I had none of it".
        if actual_quantity is not None:
            if isinstance(actual_quantity, bool):
                raise ValidationError(_("actual_quantity must be a number"),
                                      code="bad_actual_quantity")
            try:
                actual_quantity = float(actual_quantity)
            except (TypeError, ValueError):
                raise ValidationError(_("actual_quantity must be a number"),
                                      code="bad_actual_quantity")
            if actual_quantity != actual_quantity or actual_quantity in (
                    float("inf"), float("-inf")):
                raise ValidationError(_("actual_quantity must be a number"),
                                      code="bad_actual_quantity")
            if not (0 <= actual_quantity <= DietInputValidator.MAX_QUANTITY):
                raise ValidationError(
                    _("actual_quantity must be between 0 and %(hi)d grams")
                    % {"hi": int(DietInputValidator.MAX_QUANTITY)},
                    code="bad_actual_quantity")

        component.complete(actual_quantity)
        return component
    
    def rate_meal(self, meal: Meal, is_liked: bool, notes: str = "") -> Meal:
        """
        Rate a meal (like/dislike) and add notes.
        
        Args:
            meal: The meal to rate
            is_liked: Whether the meal was liked
            notes: Optional notes about the meal
            
        Returns:
            Updated Meal instance
        """
        # Validate client owns this meal
        if meal.diet_plan.user != self.client:
            raise ValidationError(_("You can only rate your own meals"), code="not_rating_owner")
        
        meal.is_liked = is_liked
        meal.notes = notes
        meal.save()
        return meal
    
    def get_daily_progress(self, target_date: date = None) -> Dict[str, Any]:
        """The day's figures for the client. READ ONLY — writes nothing, locks nothing.

        Serves `GET /api/diet/v1/client/progress/`. It used to `get_or_create` a
        `DailyProgress` row, take `SELECT … FOR UPDATE` on it and `save()` it, on a
        GET: opening the screen wrote a row, and two readers of the same day queued
        behind the lock (SIDE_BUGS S-023). The numbers now come from
        `DailyProgress.figures_for`, which computes them on a detached instance. A
        write path that needs the row to exist calls `_ensure_daily_progress`.

        Args:
            target_date: Target date (defaults to today)

        Returns:
            Dictionary with daily progress information. `has_active_plan: False` and
            zeros when no plan covers the date; a day with nothing logged answers the
            plan's targets with zero consumed, exactly as before.
        """
        if target_date is None:
            target_date = timezone.localdate()
        
        # Get active diet plan
        active_plan = self._get_active_diet_plan(target_date)
        if not active_plan:
            return {
                'date': target_date,
                'has_active_plan': False,
                'meals_completed': 0,
                'total_meals': 0,
                'completion_percentage': 0
            }
        
        # The day's figures, computed and thrown away. Nothing is created or locked.
        daily_progress = DailyProgress.figures_for(self.client, active_plan, target_date)

        return {
            'date': target_date,
            'has_active_plan': True,
            'diet_plan_id': active_plan.id,
            'meals_completed': daily_progress.meals_completed,
            'total_meals': daily_progress.total_meals,
            'completion_percentage': daily_progress.completion_percentage,
            'calories_consumed': daily_progress.calories_consumed,
            'target_calories': daily_progress.target_calories,
            'calories_percentage': daily_progress.calories_percentage,
            'is_day_completed': daily_progress.is_day_completed
        }
    
    def get_weekly_progress(self, start_date: date = None) -> List[Dict[str, Any]]:
        """
        Get weekly progress for the client.
        
        Args:
            start_date: Start date for the week (defaults to start of current week)
            
        Returns:
            List of daily progress dictionaries
        """
        if start_date is None:
            # Start of current week (Monday)
            today = timezone.localdate()
            start_date = today - timedelta(days=today.weekday())
        
        # One plan lookup and one aggregate for the week, not seven of each. This was
        # seven `get_daily_progress` calls: about forty queries and seven WRITES on a
        # GET, and an unbounded `start_date` minted DailyProgress rows for any date
        # asked about.
        end_date = start_date + timedelta(days=6)

        # EVERY active plan that overlaps the week, not one resolved from the two
        # boundary dates. `_get_active_diet_plan(start) or _get_active_diet_plan(end)`
        # requires a plan to cover the Monday or the Sunday, so a plan covering only
        # mid-week satisfied neither probe and the whole week came back blank — which is
        # every 1-day and 7-day plan that does not start on a Monday, and `duration_days`
        # defaults to 1. The client's own progress screen showed today's meals while this
        # one showed seven empty days. It also reported a week containing two plans
        # entirely against one of them.
        plans = list(DietPlan.objects.filter(
            user=self.client, is_active=True,
            start_date__lte=end_date, end_date__gte=start_date,
        ).values_list('id', flat=True))

        meals_by_day: Dict[object, List[int]] = {}
        if plans:
            for row in (Meal.objects
                        .filter(diet_plan_id__in=plans,
                                date__gte=start_date, date__lte=end_date)
                        .values('date', 'is_completed')):
                total, done = meals_by_day.setdefault(row['date'], [0, 0])
                meals_by_day[row['date']] = [total + 1, done + (1 if row['is_completed'] else 0)]

        week_progress = []
        for i in range(7):
            day_date = start_date + timedelta(days=i)
            total, done = meals_by_day.get(day_date, (0, 0))
            week_progress.append({
                'date': day_date,
                'has_active_plan': bool(total),
                'meals_completed': done,
                'total_meals': total,
                'completion_percentage': round((done / total) * 100, 1) if total else 0,
            })
        
        return week_progress
    
    def get_meal_details(self, meal: Meal) -> Dict[str, Any]:
        """
        Get detailed information about a meal.
        
        Args:
            meal: The meal to get details for
            
        Returns:
            Dictionary with meal details
        """
        # Validate client owns this meal
        if meal.diet_plan.user != self.client:
            raise ValidationError(_("You can only view your own meals"), code="not_meal_viewer")
        
        components = []
        for component in meal.components.all():
            entry = component_payload(component)
            entry.update({
                'food_name': name_en_of(component.food),
                'food_name_ar': getattr(component.food, 'name_ar', None) or name_en_of(component.food),
                'food_image': component.food.image_url,
                'quantity': component.quantity,
            })
            components.append(entry)

        from diet.services.meal_payload import day_targets, target_payload
        day = list(meal.diet_plan.meals.filter(date=meal.date))
        return {**meal_payload(meal), 'notes': meal.notes, 'components': components,
                'targets': target_payload(day_targets(meal.diet_plan, day), meal)}
    
    def get_enhanced_daily_progress(self, target_date: date = None) -> Dict[str, Any]:
        """The day's meals and figures in detail. READ ONLY — writes nothing, locks nothing.

        Serves `GET /api/diet/v1/client/progress/enhanced/`. Same defect and same fix
        as `get_daily_progress` (SIDE_BUGS S-023): it used to mint a `DailyProgress`
        row and lock it on a GET. `DailyProgress.figures_for` computes the same
        numbers without a row; `_ensure_daily_progress` is the write-path counterpart.

        Args:
            target_date: Target date (defaults to today)

        Returns:
            Dictionary with enhanced daily progress information
        """
        if target_date is None:
            target_date = timezone.localdate()
        
        # Get active diet plan with optimized prefetching
        # We need to fetch everything in 3-4 queries max
        # The plan governing TARGET_DATE, not today: the meals below are prefetched on
        # target_date, so resolving the plan by today reported a day's meals against a
        # plan that did not cover it.
        from django.db.models import Prefetch

        active_plan = DietPlan.objects.filter(
            user=self.client,
            is_active=True,
            start_date__lte=target_date,
            end_date__gte=target_date
        ).order_by('-start_date', '-id').prefetch_related(
            Prefetch(
                'meals',
                queryset=Meal.objects.filter(date=target_date)
                    .order_by('scheduled_time')
                    .prefetch_related(
                        'components',
                        'components__food',
                        'components__food__category'
                    )
            )
        ).first()

        if not active_plan:
            return {
                'date': target_date,
                'has_active_plan': False,
                'meals_completed': 0,
                'total_meals': 0,
                'completion_percentage': 0
            }
        
        # The day's figures, computed and thrown away. Nothing is created or locked.
        daily_progress = DailyProgress.figures_for(self.client, active_plan, target_date)

        # Processing meals in-memory using prefetched data
        meals = active_plan.meals.all() # This uses the Prefetch from above!
        meals_data = []
        
        from diet.services.meal_payload import day_targets, target_payload
        by_slot = day_targets(active_plan, list(meals))
        for meal in meals:
            # Calculate nutrition in-memory
            # (Duplicate logic from Meal.calculate_nutrition but strictly in-memory)
            total_calories = 0
            total_protein = 0
            total_carbs = 0
            total_fat = 0
            
            components_data = []
            # Access related components (Already prefetched)
            for component in meal.components.all():
                food = component.food
                scale_factor = component.quantity / food.serving_size_grams
                
                comp_cals = round(food.calories * scale_factor, 1)
                comp_prot = round(food.protein * scale_factor, 1)
                comp_carbs = round(food.carbs * scale_factor, 1)
                comp_fat = round(food.fat * scale_factor, 1)
                
                total_calories += comp_cals
                total_protein += comp_prot
                total_carbs += comp_carbs
                total_fat += comp_fat
                
                entry = component_payload(component)
                entry.update({
                    'food_name': name_en_of(food),
                    'food_name_ar': getattr(food, 'name_ar', None) or name_en_of(food),
                    'quantity': component.quantity,
                    'nutrition': {
                        'calories': comp_cals,
                        'protein': comp_prot,
                        'carbs': comp_carbs,
                        'fat': comp_fat
                    }
                })
                components_data.append(entry)

            # The shared meal shape (name, reason, dish, both languages), with the
            # totals computed in memory above rather than a second pass.
            meal_data = meal_payload(meal)
            meal_data['targets'] = target_payload(by_slot, meal)
            meal_data['nutrition'] = {
                'calories': round(total_calories, 1),
                'protein': round(total_protein, 1),
                'carbs': round(total_carbs, 1),
                'fat': round(total_fat, 1)
            }
            meal_data['components'] = components_data
            meals_data.append(meal_data)
        
        # Calculate plan nutrition in-memory from the meals we just processed
        # This avoids calling active_plan.calculate_daily_nutrition which would trigger new DB queries
        plan_nutrition = {
            'calories': 0,
            'protein': 0,
            'carbs': 0,
            'fat': 0
        }
        
        for m_data in meals_data:
            nutrition = m_data['nutrition']
            plan_nutrition['calories'] += nutrition['calories']
            plan_nutrition['protein'] += nutrition['protein']
            plan_nutrition['carbs'] += nutrition['carbs']
            plan_nutrition['fat'] += nutrition['fat']
            
        # Round final values
        plan_nutrition['calories'] = round(plan_nutrition['calories'], 1)
        plan_nutrition['protein'] = round(plan_nutrition['protein'], 1)
        plan_nutrition['carbs'] = round(plan_nutrition['carbs'], 1)
        plan_nutrition['fat'] = round(plan_nutrition['fat'], 1)
        
        return {
            'date': target_date,
            'has_active_plan': True,
            'diet_plan': {
                'id': active_plan.id,
                'goal': active_plan.goal,
                'daily_calories': active_plan.daily_calories
            },
            'meals': meals_data,
            'plan_nutrition': plan_nutrition,
            'progress': {
                'meals_completed': daily_progress.meals_completed,
                'total_meals': daily_progress.total_meals,
                'completion_percentage': daily_progress.completion_percentage,
                'calories_consumed': daily_progress.calories_consumed,
                'calories_target': daily_progress.target_calories,
                'calories_percentage': daily_progress.calories_percentage,
                'protein_consumed': daily_progress.protein_consumed,
                'protein_target': daily_progress.target_protein,
                'protein_percentage': daily_progress.protein_percentage,
                'carbs_consumed': daily_progress.carbs_consumed,
                'carbs_target': daily_progress.target_carbs,
                'carbs_percentage': daily_progress.carbs_percentage,
                'fat_consumed': daily_progress.fat_consumed,
                'fat_target': daily_progress.target_fat,
                'fat_percentage': daily_progress.fat_percentage
            },
            'summary': {
                'total_meals': len(meals_data),
                'completed_meals': sum(1 for meal in meals_data if meal['is_completed']),
                'total_components': sum(len(meal['components']) for meal in meals_data),
                'completed_components': sum(
                    sum(1 for comp in meal['components'] if comp['is_completed']) 
                    for meal in meals_data
                ),
                'day_completed': daily_progress.is_day_completed
            }
        }
    
    def complete_entire_meal(self, meal: Meal) -> Dict[str, Any]:
        """
        Complete an entire meal by marking all components as completed.
        
        Args:
            meal: The meal to complete
            
        Returns:
            Dictionary with completion details
        """
        # Validate client owns this meal
        if meal.diet_plan.user != self.client:
            raise ValidationError(_("You can only complete your own meals"), code="not_meal_completion_owner")
        
        # A bulk `update()` fires no `post_save`, and completion is the ONLY thing the
        # analytics and achievement listeners watch on MealComponent. So the app's main
        # "I ate this" button recorded nothing anywhere downstream: adherence metrics
        # and the meal-tracking achievement could only be earned by ticking components
        # one at a time. Written per row, in one transaction, so the listeners run.
        #
        # All of it under the MEAL's row lock, taken first: the lock `undo_meal` takes
        # (Road to 10 S3.5). The ticks were one transaction and the meal's flag a write
        # after it, unlocked, so an undo landing in between reopened the foods and the
        # confirm then marked the meal completed: eaten, with none of its foods eaten,
        # and the day counting it (the phase-3 review). Now a confirm and an undo of one
        # meal run one after the other, and the components are read after the lock, so
        # a tick an undo has just taken back is ticked again rather than skipped.
        now = timezone.now()
        with transaction.atomic():
            list(Meal.objects.select_for_update().filter(pk=meal.pk)
                 .values_list('pk', flat=True))
            for component in MealComponent.objects.filter(meal=meal).select_related('food'):
                if component.is_completed:
                    continue
                component.is_completed = True
                component.completed_at = now
                component.save(update_fields=['is_completed', 'completed_at'])

            # Update parent meal status explicitly
            Meal.objects.filter(id=meal.id).update(is_completed=True)
            meal.is_completed = True # Update local instance for consistency

            # A write: the client just ate, so the day's row is created if it is not
            # there yet and recomputed under its lock.
            self._ensure_daily_progress(meal.diet_plan, meal.date)
        
        total_components = MealComponent.objects.filter(meal=meal).count()
        
        return {
            'meal_id': meal.id,
            'components_completed': total_components,
            'total_components': total_components,
            'completed_at': timezone.now(),
            'meal_completion_percentage': 100.0
        }
    
    @staticmethod
    def confirmed_on(meal: Meal, ticked) -> Optional[date]:
        """The server-calendar day this meal's confirmation was made, or None if unknown.

        A confirmation is the components' ticks (`complete_entire_meal` stamps each one,
        `MealComponent.complete` stamps its own). A meal ticked across two days was
        confirmed on the EARLIER one, so it is judged by that day. A tick with no
        `completed_at` (written before the column was kept, or by hand) has no day we
        can stand behind: None, and the undo refuses it. A meal flagged complete with no
        component ticked (a meal with no components) is judged by its own date.
        """
        if not ticked:
            return meal.date
        stamps = [component.completed_at for component in ticked]
        if any(stamp is None for stamp in stamps):
            return None
        return min(timezone.localtime(stamp).date() for stamp in stamps)

    def undo_meal(self, meal: Meal) -> Dict[str, Any]:
        """Reopen a meal the client confirmed today (Road to 10 S3.5, the owner's D11).

        Everything the confirmation wrote is taken back, so every figure derived from it
        follows: the components are un-ticked (with the amount the client said they ate),
        the meal is not completed, the day's `DailyProgress` is recomputed and saved (its
        save retires home's cached page, `training_platform/signals.py`), the funnel's
        COMPLETED `MealEvent` goes, and so do the analytics `meal_completed` row
        (achievement `meal_count` and the activity log read it) and the AI coach's
        `meal_completed` behaviour rows for those components. The trainer's adherence
        week and the client's progress reads compute from the components, so they follow
        on the next read. A rating (`is_liked`, notes) is not part of a confirmation and
        stays. An achievement already awarded stays awarded; its progress counts again
        from the rows.

        "Today" is the SERVER's calendar day (`Asia/Damascus`), the calendar every meal
        date and every home day uses. Raises `UndoWindowClosed` when the confirmation
        was made on an earlier day (`confirmed_on`). Nothing confirmed is not an error:
        undoing twice answers the same, and changes nothing the second time.

        Returns `{reopened, components_reopened, progress}`; `progress` is the day's
        figures after the undo (`progress_payload`).
        """
        if meal.diet_plan.user_id != self.client.pk:
            raise ValidationError(_("You can only undo your own meals"),
                                  code="not_meal_completion_owner")
        from ai_assistant.models import UserBehaviorEvent
        from analytics.models import UserActivity
        from .models import MealEvent

        today = timezone.localdate()
        with transaction.atomic():
            # The meal's row lock, the one `update_completion_status` and the analytics
            # listener take, so a tick landing at the same moment is not half undone.
            locked = Meal.objects.select_for_update().get(pk=meal.pk)
            ticked = list(MealComponent.objects.select_for_update()
                          .filter(meal_id=meal.pk, is_completed=True).order_by('id'))
            if not ticked and not locked.is_completed:
                figures = DailyProgress.figures_for(self.client, meal.diet_plan, meal.date)
                return {'reopened': False, 'components_reopened': 0,
                        'progress': progress_payload(figures)}
            day = self.confirmed_on(locked, ticked)
            if day != today:
                raise UndoWindowClosed(day)

            ids = [component.pk for component in ticked]
            MealComponent.objects.filter(pk__in=ids).update(
                is_completed=False, completed_at=None, actual_quantity_consumed=None)
            Meal.objects.filter(pk=meal.pk).update(is_completed=False)
            meal.is_completed = False
            MealEvent.objects.filter(user=self.client, meal_id=meal.pk,
                                     kind=MealEvent.COMPLETED).delete()
            # The meal's `meal_completed` row, by both of the keys the analytics listener
            # recognises (`analytics/signals.py on_meal_completed`): the meal's id, or an
            # older per-food row named after any of its components.
            keys = [f"meal-component-{pk}" for pk in
                    MealComponent.objects.filter(meal_id=meal.pk).values_list('pk', flat=True)]
            UserActivity.objects.filter(
                Q(metadata__meal_id=meal.pk) | Q(session_id__in=keys),
                user_id=self.client.pk, activity_type='meal_completed').delete()
            UserBehaviorEvent.objects.filter(
                user_id=self.client.pk, event_type='meal_completed',
                event_data__component_id__in=ids).delete()
            row = self._ensure_daily_progress(meal.diet_plan, meal.date)
        return {'reopened': True, 'components_reopened': len(ids),
                'progress': progress_payload(row)}

    def _ensure_daily_progress(self, diet_plan: DietPlan, on_date: date) -> DailyProgress:
        """Create the day's `DailyProgress` row if it is missing, then recompute it.

        THIS ONE WRITES, and takes the row's lock. Only a path that has just changed
        what the client ate may call it — never a GET (SIDE_BUGS S-023). The read
        counterpart is `DailyProgress.figures_for`, which computes the same numbers
        without touching the table.
        """
        row, _created = DailyProgress.objects.get_or_create(
            user=self.client, diet_plan=diet_plan, date=on_date)
        # Targets are written by the recompute itself, from the plan, so they stay
        # right when the plan's calories change.
        row.update_progress()
        return row

    def _get_active_diet_plan(self, on_date: date = None) -> Optional[DietPlan]:
        """The plan governing `on_date`, defaulting to today.

        This ignored the date it was asked about and always resolved by today, while
        its callers went on to report and PERSIST progress against the date they were
        given: asking about last month returned "you have a plan and ate none of it",
        and wrote a DailyProgress row for a date the plan never covered.
        """
        today = on_date or timezone.localdate()
        # strictly order by start_date descending to get the latest one
        return DietPlan.objects.filter(
            user=self.client,
            is_active=True,
            start_date__lte=today,
            end_date__gte=today
        ).order_by('-start_date', '-id').first() 