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

from diet.services.meal_payload import component_payload, meal_payload
from .validators import DietInputValidator
from .models import (
    DietPlan, DietPlanTemplate, Meal, MealComponent, 
    FoodItem, DailyProgress
)
from users.models import CustomUser

logger = logging.getLogger(__name__)

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
            raise ValidationError(_("User must be a trainer to use this service"), code="not_trainer")
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
        daily_calories: Optional[float] = None
    ) -> DietPlan:
        """
        Create a new diet plan for a client using a template.
        
        Args:
            client: The client user
            template: The diet plan template to use
            start_date: Start date for the plan
            duration_weeks: Duration in weeks
            goal: Fitness goal (Lose, Maintain, Gain)
            daily_calories: Daily calorie target (auto-calculated if None)
            
        Returns:
            Created DietPlan instance
        """
        # Validate trainer-client relationship
        if not self._can_manage_client(client):
            raise ValidationError(_("You can only create diet plans for your assigned clients"), code="not_assigned_client")
        
        # Calculate daily calories if not provided
        if daily_calories is None:
            daily_calories = client.calculate_daily_calories(goal)

        # Validate the calorie target server-side (bounds + type). Prevents a
        # trainer from persisting a medically dangerous target (e.g. 200 or 50000).
        daily_calories = DietInputValidator.validate_daily_calories(daily_calories)

        try:
            duration_weeks = int(duration_weeks)
        except (TypeError, ValueError):
            raise ValidationError(_("duration_weeks must be a whole number"),
                                  code="bad_duration")
        if not 1 <= duration_weeks <= 52:
            raise ValidationError(_("duration_weeks must be between 1 and 52"),
                                  code="bad_duration")

        end_date = start_date + timedelta(weeks=duration_weeks)
        resolved_goal = DietPlan.normalise_goal(goal)

        # The same macro split the deterministic planner builds to. A trainer plan
        # left these null, so the client's progress screen fell back to a generic
        # ratio and disagreed with every generated plan about the same numbers.
        from diet.planner.policy import load_policy
        from diet.planner.targets import day_macro_grams
        macros = day_macro_grams(float(daily_calories), load_policy(resolved_goal),
                                 getattr(client, 'weight', None))

        # One active plan per client per window is a model constraint, and it made
        # trainer plan creation fail with an opaque 400 for any client who already
        # had one — which is every client the engine has ever served. Retiring the
        # overlap is what the trainer means by "here is your new plan".
        with transaction.atomic():
            # Every overlapping active plan, whoever wrote it: a client follows one
            # plan, and the model refuses two over one window. That includes the
            # generated plan the client already had, which is the common case and the
            # reason trainer plan creation used to fail outright. What was missing was
            # not the scoping but the telling — the ids come back in the response so
            # the trainer can say what was replaced.
            superseded_ids = list(DietPlan.objects.filter(
                user=client, is_active=True,
                start_date__lte=end_date, end_date__gte=start_date,
            ).values_list('id', flat=True))
            DietPlan.objects.filter(id__in=superseded_ids).update(is_active=False)

            diet_plan = DietPlan.objects.create(
                user=client,
                goal=resolved_goal,
                daily_calories=daily_calories,
                target_protein=macros['protein'],
                target_carbs=macros['carb'],
                target_fat=macros['fat'],
                start_date=start_date,
                end_date=end_date,
                duration_weeks=duration_weeks,
                generation_strategy='TRAINER',
                template=template,
                created_by=self.trainer
            )
        if superseded_ids:
            logger.info("Trainer %s replaced plan(s) %s for client %s",
                        self.trainer.id, superseded_ids, client.id)
        # The caller reports these to the trainer; a plan vanishing with no
        # explanation is what made this feature look broken.
        diet_plan.superseded_plan_ids = superseded_ids
        return diet_plan
    
    def add_meal_to_plan(
        self,
        diet_plan: DietPlan,
        meal_type: str,
        target_date: date,
        food_items: List[Dict[str, Any]],
        scheduled_time: Optional[time] = None,
        description: str = ""
    ) -> Meal:
        """
        Add a meal to a diet plan.
        
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

        # `choices` is a form-layer rule; nothing called full_clean(), so "Brunch" was
        # persisted and then matched no slot in the target split downstream.
        if meal_type not in dict(Meal.MEAL_TYPES):
            raise ValidationError(_("%(value)s is not a meal type") % {"value": meal_type},
                                  code="bad_meal_type")
        if diet_plan.start_date and diet_plan.end_date and not (
                diet_plan.start_date <= target_date <= diet_plan.end_date):
            raise ValidationError(
                _("That date is outside the plan (%(a)s to %(b)s)")
                % {"a": diet_plan.start_date, "b": diet_plan.end_date},
                code="date_outside_plan")

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

        components = self._validated_items(food_items, meal_type)
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

        components = (self._validated_items(food_items, meal.meal_type)
                      if food_items is not None else None)

        # Atomic, because the old components are deleted before the new ones exist:
        # a failure halfway through emptied the client's meal.
        with transaction.atomic():
            if scheduled_time is not None:
                meal.scheduled_time = scheduled_time
            if description is not None:
                meal.description = description
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
        meal.delete()
        self._recompute_day(stale)
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

        return (DietPlan.objects
                .filter(user=client, created_by=self.trainer)
                .select_related('template')
                .annotate(meals_count=Count('meals'))
                .order_by('-start_date')[:200])
    
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

    #: One legal range for a component, the same one `DietInputValidator` applies
    #: everywhere else. This class declared its own 0-2000 while every other writer
    #: used 1-1000, so the same gram figure was accepted here and refused there, and
    #: 0.0001 g passed as a component that renders as "0 g".

    def _validated_items(self, food_items, meal_type: str):
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

        found = FoodItem.objects.in_bulk([fid for fid, _g in rows])
        missing = [fid for fid, _g in rows if fid not in found]
        if missing:
            raise ValidationError(_("No such food: %(ids)s") % {"ids": missing},
                                  code="unknown_food")
        return [(found[fid], grams) for fid, grams in rows]

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

        slot_ar = {'Breakfast': 'الفطور', 'Lunch': 'الغداء',
                   'Dinner': 'العشاء', 'Snack': 'الوجبة الخفيفة'}.get(meal_type, meal_type)
        return (join(en, ', ', ' and ') or meal_type,
                join(ar, '، ', ' و') or slot_ar,
                str(_("chosen for you by your trainer")),
                f"اختارها لك مدربك لوجبة {slot_ar}")

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
        """
        Get daily progress for the client.
        
        Args:
            target_date: Target date (defaults to today)
            
        Returns:
            Dictionary with daily progress information
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
        
        # Get or create daily progress
        daily_progress, created = DailyProgress.objects.get_or_create(
            user=self.client,
            diet_plan=active_plan,
            date=target_date,
            defaults={
                'target_calories': active_plan.daily_calories,
                'target_protein': active_plan.macro_targets()["protein"],
                'target_carbs': active_plan.macro_targets()["carbs"],
                'target_fat': active_plan.macro_targets()["fat"]
            }
        )
        
        # Update progress
        daily_progress.update_progress()
        
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
        plan = self._get_active_diet_plan(start_date) or self._get_active_diet_plan(end_date)
        meals_by_day: Dict[object, List[int]] = {}
        if plan is not None:
            for row in (Meal.objects
                        .filter(diet_plan=plan, date__gte=start_date, date__lte=end_date)
                        .values('date', 'is_completed')):
                total, done = meals_by_day.setdefault(row['date'], [0, 0])
                meals_by_day[row['date']] = [total + 1, done + (1 if row['is_completed'] else 0)]

        week_progress = []
        for i in range(7):
            day_date = start_date + timedelta(days=i)
            total, done = meals_by_day.get(day_date, (0, 0))
            week_progress.append({
                'date': day_date,
                'has_active_plan': plan is not None and bool(total),
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
                'food_name': component.food.name,
                'food_image': component.food.image_url,
                'quantity': component.quantity,
            })
            components.append(entry)

        from diet.services.meal_payload import day_targets, target_payload
        day = list(meal.diet_plan.meals.filter(date=meal.date))
        return {**meal_payload(meal), 'notes': meal.notes, 'components': components,
                'targets': target_payload(day_targets(meal.diet_plan, day), meal)}
    
    def get_enhanced_daily_progress(self, target_date: date = None) -> Dict[str, Any]:
        """
        Get enhanced daily progress with detailed meal and nutritional information.
        
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
        
        # Get or create daily progress
        daily_progress, created = DailyProgress.objects.get_or_create(
            user=self.client,
            diet_plan=active_plan,
            date=target_date,
            defaults={
                'target_calories': active_plan.daily_calories,
                'target_protein': active_plan.macro_targets()["protein"],
                'target_carbs': active_plan.macro_targets()["carbs"],
                'target_fat': active_plan.macro_targets()["fat"]
            }
        )
        
        # Update progress (using optimized Aggregation)
        # Pass the pre-fetched meals to update_progress to allow it to use them?
        # Actually our new update_progress uses its own efficient query, so we just call it.
        daily_progress.update_progress()
        
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
                    'food_name': food.name,
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
        now = timezone.now()
        with transaction.atomic():
            for component in MealComponent.objects.filter(meal=meal).select_related('food'):
                if component.is_completed:
                    continue
                component.is_completed = True
                component.completed_at = now
                component.save(update_fields=['is_completed', 'completed_at'])
        
        # Update parent meal status explicitly
        Meal.objects.filter(id=meal.id).update(is_completed=True)
        meal.is_completed = True # Update local instance for consistency
        
        # Update daily progress efficiently
        daily_progress, created = DailyProgress.objects.get_or_create(
            user=self.client,
            diet_plan=meal.diet_plan,
            date=meal.date,
            defaults={
                'target_calories': meal.diet_plan.daily_calories,
                'target_protein': meal.diet_plan.macro_targets()["protein"],
                'target_carbs': meal.diet_plan.macro_targets()["carbs"],
                'target_fat': meal.diet_plan.macro_targets()["fat"]
            }
        )
        # Use new optimized method that doesn't iterate
        daily_progress.update_progress()
        
        total_components = MealComponent.objects.filter(meal=meal).count()
        
        return {
            'meal_id': meal.id,
            'components_completed': total_components,
            'total_components': total_components,
            'completed_at': timezone.now(),
            'meal_completion_percentage': 100.0
        }
    
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