"""
models.py - Database Models for Diet App

This module defines the database schema for food items, categories, user preferences,
diet plans, meals, meal components, and daily advice.
Enhanced to support both AI-generated and trainer-created diet plans.
"""

from django.db import models
from users.models import CustomUser
from datetime import date, datetime
from django.utils import timezone
from django.core.validators import MinValueValidator, MaxValueValidator

class FoodCategory(models.Model):
    """
    Represents a category of food (e.g., Proteins, Carbs, Fats) and its meal time association.
    """
    MEAL_TIME_CHOICES = [
        ('ANY', 'Any Time'),
        ('BREAKFAST', 'Breakfast'),
        ('LUNCH', 'Lunch'),
        ('DINNER', 'Dinner'),
    ]
    name = models.CharField(max_length=50)
    meal_times = models.CharField(max_length=20, choices=MEAL_TIME_CHOICES, default='ANY')
    is_protein = models.BooleanField(default=False)
    is_carb = models.BooleanField(default=False)
    is_fat = models.BooleanField(default=False)
    def __str__(self):
        return f"{self.name} ({self.get_meal_times_display()})"

class FoodItem(models.Model):
    """
    Represents a food item with nutritional information and category.
    """
    api_id = models.CharField(max_length=255, unique=True)
    name = models.CharField(max_length=255)
    image_url = models.URLField(max_length=500, blank=True, null=True)
    calories = models.FloatField()
    protein = models.FloatField()
    carbs = models.FloatField()
    fat = models.FloatField()
    serving_size = models.CharField(max_length=100)
    category = models.ForeignKey(FoodCategory, on_delete=models.SET_NULL, null=True, blank=True)
    serving_size_grams = models.PositiveIntegerField(default=100)
    calories_per_gram = models.FloatField(default=1.0)
    protein_per_gram = models.FloatField(default=0.0)
    carbs_per_gram = models.FloatField(default=0.0)
    fat_per_gram = models.FloatField(default=0.0)
    smart_score_weight = models.FloatField(default=1.0, help_text="Adaptive weight for smart macro planner")
    def save(self, *args, **kwargs):
        """
        Normalize macros to 100g and auto-calculate per-gram values on save.
        
        This ensures all foods store macros per 100g for consistency:
        - If serving_size_grams != 100, normalize macros to 100g
        - Always calculate per-gram values from normalized 100g values
        """
        from .validators import DietInputValidator
        from django.core.exceptions import ValidationError
        
        # Cast to float in case incoming values are strings
        try:
            self.calories = float(self.calories)
        except Exception:
            self.calories = 0.0
        try:
            self.protein = float(self.protein)
        except Exception:
            self.protein = 0.0
        try:
            self.carbs = float(self.carbs)
        except Exception:
            self.carbs = 0.0
        try:
            self.fat = float(self.fat)
        except Exception:
            self.fat = 0.0
        
        # BUG FIX: Validate nutritional values are non-negative
        if self.calories < 0:
            raise ValidationError("Calories cannot be negative")
        if self.protein < 0:
            raise ValidationError("Protein cannot be negative")
        if self.carbs < 0:
            raise ValidationError("Carbs cannot be negative")
        if self.fat < 0:
            raise ValidationError("Fat cannot be negative")
        
        # BUG FIX: Ensure serving size is valid
        if self.serving_size_grams <= 0:
            self.serving_size_grams = 100
        
        # NORMALIZE: Convert all macros to per-100g standard
        # If serving_size_grams != 100, normalize the macro values to 100g
        if self.serving_size_grams != 100:
            # Calculate normalization factor (how many 100g servings in current serving)
            normalization_factor = 100.0 / self.serving_size_grams
            
            # Normalize macros to 100g
            self.calories = self.calories * normalization_factor
            self.protein = self.protein * normalization_factor
            self.carbs = self.carbs * normalization_factor
            self.fat = self.fat * normalization_factor
            
            # Update serving_size_grams to 100g standard
            self.serving_size_grams = 100
            
            # Update serving_size string to reflect 100g standard
            if not self.serving_size or '100g' not in self.serving_size.lower():
                self.serving_size = '100g'
        
        # Calculate per-gram values from normalized 100g values
        # Since serving_size_grams is now always 100, this is: macro_value / 100
        self.calories_per_gram = self.calories / 100.0
        self.protein_per_gram = self.protein / 100.0
        self.carbs_per_gram = self.carbs / 100.0
        self.fat_per_gram = self.fat / 100.0
        
        super().save(*args, **kwargs)
    def __str__(self):
        return self.name

class UserFoodPreference(models.Model):
    """
    Stores user-specific food preferences, allergies, and macro choices.
    """
    user = models.ForeignKey(CustomUser, on_delete=models.CASCADE)
    liked_foods = models.ManyToManyField(FoodItem, related_name='liked_by', blank=True)
    disliked_foods = models.ManyToManyField(FoodItem, related_name='disliked_by', blank=True)
    allergies = models.TextField(blank=True)
    protein_choices = models.ManyToManyField(FoodItem, related_name='protein_prefs', limit_choices_to={'category__name': 'Proteins'})
    carb_choices = models.ManyToManyField(FoodItem, related_name='carb_prefs', limit_choices_to={'category__name': 'Carbs'})
    fat_choices = models.ManyToManyField(FoodItem, related_name='fat_prefs', limit_choices_to={'category__name': 'Fats'})
    vegetable_choices = models.ManyToManyField(FoodItem, related_name='vegetable_prefs', blank=True)
    fruit_choices = models.ManyToManyField(FoodItem, related_name='fruit_prefs', blank=True)


class UserFoodCategoryPreference(models.Model):
    """Per-user meal categorization for liked foods (e.g., Oats → Breakfast Carb)."""
    MEAL_CHOICES = [
        ('Breakfast', 'Breakfast'),
        ('Lunch', 'Lunch'),
        ('Dinner', 'Dinner'),
        ('Snack', 'Snack'),
    ]
    MACRO_CHOICES = [
        ('carb', 'Carb'),
        ('protein', 'Protein'),
        ('fat', 'Fat'),
        ('vegetable', 'Vegetable'),
        ('fruit', 'Fruit'),
    ]
    user = models.ForeignKey(CustomUser, on_delete=models.CASCADE, related_name='food_meal_categories')
    food = models.ForeignKey(FoodItem, on_delete=models.CASCADE)
    meal = models.CharField(max_length=16, choices=MEAL_CHOICES)
    macro = models.CharField(max_length=16, choices=MACRO_CHOICES)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True, null=True, blank=True)

    class Meta:
        unique_together = [('user', 'food')]
        indexes = [
            models.Index(fields=['user', 'meal']),
            models.Index(fields=['user', 'macro']),
        ]

    def __str__(self):
        return f"{self.user_id}:{self.food.name}→{self.meal} {self.macro}"

class DietPlanTemplate(models.Model):
    """
    Predefined diet plan templates for trainer-created plans.
    """
    name = models.CharField(max_length=100, help_text="Template name (e.g., '3 Meals + 1 Snack')")
    description = models.TextField(blank=True)
    meals_per_day = models.PositiveIntegerField(
        validators=[MinValueValidator(1), MaxValueValidator(6)],
        help_text="Number of meals per day (1-6)"
    )
    snacks_per_day = models.PositiveIntegerField(
        validators=[MinValueValidator(0), MaxValueValidator(3)],
        default=0,
        help_text="Number of snacks per day (0-3)"
    )
    days_variation = models.PositiveIntegerField(
        validators=[MinValueValidator(1), MaxValueValidator(7)],
        default=1,
        help_text="How often the meal plan repeats (1-7 days)"
    )
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    
    class Meta:
        ordering = ['meals_per_day', 'snacks_per_day']
    
    def __str__(self):
        return f"{self.name} ({self.meals_per_day}M/{self.snacks_per_day}S - {self.days_variation}D)"
    
    @property
    def total_meals_per_cycle(self):
        """Total meals in one complete cycle."""
        return (self.meals_per_day + self.snacks_per_day) * self.days_variation

class DietPlan(models.Model):
    """
    Represents a generated diet plan for a user, including goal, calories, and plan data.
    Enhanced to support both AI-generated and trainer-created plans.
    """
    user = models.ForeignKey(CustomUser, on_delete=models.CASCADE)
    GOAL_CHOICES = [
        ('Lose', 'Lose Weight'),
        ('Maintain', 'Maintain Weight'),
        ('Gain', 'Gain Muscle')
    ]
    goal = models.CharField(max_length=20, choices=GOAL_CHOICES)
    daily_calories = models.FloatField()
    start_date = models.DateField()
    end_date = models.DateField()
    duration_weeks = models.PositiveIntegerField(default=4)
    generated_plan = models.JSONField(null=True, blank=True)
    generation_strategy = models.CharField(
        max_length=20,
        choices=[('GPT', 'AI Generated'), ('TRAINER', 'Trainer Created'), ('FALLBACK', 'Rule-Based')],
        default='GPT'
    )
    
    # New fields for trainer-created plans
    template = models.ForeignKey(
        DietPlanTemplate, 
        on_delete=models.SET_NULL, 
        null=True, 
        blank=True,
        help_text="Template used for trainer-created plans"
    )
    created_by = models.ForeignKey(
        CustomUser, 
        on_delete=models.SET_NULL, 
        null=True, 
        blank=True,
        related_name='created_diet_plans',
        help_text="Trainer who created this plan"
    )
    is_active = models.BooleanField(default=True, help_text="Whether this plan is currently active")
    created_at = models.DateTimeField(default=timezone.now, null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    @property
    def period(self):
        """
        Smart period detection for the plan.
        """
        days = (self.end_date - self.start_date).days
        return 'weekly' if days == 7 else 'monthly' if days > 14 else 'daily'
    
    @property
    def is_ai_generated(self):
        """Check if this plan was generated by AI."""
        return self.generation_strategy == 'GPT'
    
    @property
    def is_trainer_created(self):
        """Check if this plan was created by a trainer."""
        return self.generation_strategy == 'TRAINER'
    
    def calculate_daily_nutrition(self, target_date=None):
        """
        Calculate total nutrition for a specific day.
        """
        if target_date is None:
            target_date = date.today()
        
        meals = self.meals.filter(date=target_date)
        total_calories = 0
        total_protein = 0
        total_carbs = 0
        total_fat = 0
        
        for meal in meals:
            meal_nutrition = meal.calculate_nutrition()
            total_calories += meal_nutrition['calories']
            total_protein += meal_nutrition['protein']
            total_carbs += meal_nutrition['carbs']
            total_fat += meal_nutrition['fat']
        
        return {
            'calories': round(total_calories, 1),
            'protein': round(total_protein, 1),
            'carbs': round(total_carbs, 1),
            'fat': round(total_fat, 1)
        }

class Meal(models.Model):
    """
    Represents a meal within a diet plan, including template, date, and description.
    Enhanced with scheduling and completion tracking.
    """
    MEAL_TYPES = [
        ('Breakfast', 'Breakfast'),
        ('Lunch', 'Lunch'),
        ('Dinner', 'Dinner'),
        ('Snack', 'Snack')
    ]
    MEAL_TEMPLATES = [
        ('PROTEIN_CARB', 'Protein + Carb'),
        ('PROTEIN_FAT', 'Protein + Fat'),
        ('CARB_FAT', 'Carb + Fat'),
        ('COMPLETE', 'Complete Meal')
    ]
    template = models.CharField(max_length=20, choices=MEAL_TEMPLATES, default='COMPLETE')
    date = models.DateField(default=date.today)
    diet_plan = models.ForeignKey(DietPlan, on_delete=models.CASCADE, related_name='meals')
    image_url = models.URLField(max_length=500, blank=True, null=True)
    description = models.TextField(blank=True)
    is_ai_generated = models.BooleanField(default=False)
    
    # New fields for meal scheduling
    meal_type = models.CharField(max_length=20, choices=MEAL_TYPES, default='Lunch')
    scheduled_time = models.TimeField(null=True, blank=True, help_text="Scheduled time for this meal")
    estimated_duration = models.PositiveIntegerField(
        null=True, 
        blank=True, 
        help_text="Estimated duration to complete meal in minutes"
    )
    
    # Meal interaction fields
    is_liked = models.BooleanField(null=True, blank=True, help_text="User's like/dislike rating")
    notes = models.TextField(blank=True, help_text="User's notes about this meal")
    
    class Meta:
        ordering = ['date', 'scheduled_time']
        unique_together = ['diet_plan', 'date', 'meal_type', 'scheduled_time']
    
    def calculate_nutrition(self):
        """
        Calculate total nutrition for this meal.
        """
        components = self.components.all()
        total_calories = 0
        total_protein = 0
        total_carbs = 0
        total_fat = 0
        
        for component in components:
            # Calculate nutrition based on quantity
            scale_factor = component.quantity / component.food.serving_size_grams
            total_calories += component.food.calories * scale_factor
            total_protein += component.food.protein * scale_factor
            total_carbs += component.food.carbs * scale_factor
            total_fat += component.food.fat * scale_factor
        
        return {
            'calories': round(total_calories, 1),
            'protein': round(total_protein, 1),
            'carbs': round(total_carbs, 1),
            'fat': round(total_fat, 1)
        }
    
    @property
    def is_completed(self):
        """Check if all components of this meal are completed."""
        components = self.components.all()
        if not components.exists():
            return False
        return all(component.is_completed for component in components)
    
    @property
    def completion_percentage(self):
        """Calculate completion percentage of this meal."""
        components = self.components.all()
        if not components.exists():
            return 0
        completed_count = sum(1 for component in components if component.is_completed)
        return round((completed_count / components.count()) * 100, 1)

class MealComponent(models.Model):
    """
    Represents a component (food item) of a meal, with quantity and meal time.
    Enhanced with completion tracking.
    """
    meal = models.ForeignKey('Meal', on_delete=models.CASCADE, related_name='components')
    food = models.ForeignKey(FoodItem, on_delete=models.CASCADE)
    quantity = models.FloatField(help_text="Quantity in grams")
    meal_time = models.CharField(max_length=20, choices=Meal.MEAL_TYPES, default='Lunch')
    
    # Completion tracking
    is_completed = models.BooleanField(default=False, help_text="Whether this component was consumed")
    completed_at = models.DateTimeField(null=True, blank=True, help_text="When this component was completed")
    actual_quantity_consumed = models.FloatField(
        null=True, 
        blank=True, 
        help_text="Actual quantity consumed (may differ from planned quantity)"
    )
    
    def complete(self, actual_quantity=None):
        """
        Mark this component as completed.
        """
        from django.utils import timezone
        self.is_completed = True
        self.completed_at = timezone.now()
        if actual_quantity is not None:
            self.actual_quantity_consumed = actual_quantity
        self.save()
    
    def calculate_nutrition(self):
        """
        Calculate nutrition for this component.
        """
        scale_factor = self.quantity / self.food.serving_size_grams
        return {
            'calories': round(self.food.calories * scale_factor, 1),
            'protein': round(self.food.protein * scale_factor, 1),
            'carbs': round(self.food.carbs * scale_factor, 1),
            'fat': round(self.food.fat * scale_factor, 1)
        }

class DailyProgress(models.Model):
    """
    Tracks daily progress for diet plans.
    """
    user = models.ForeignKey(CustomUser, on_delete=models.CASCADE)
    diet_plan = models.ForeignKey(DietPlan, on_delete=models.CASCADE)
    date = models.DateField()
    
    # Progress metrics
    meals_completed = models.PositiveIntegerField(default=0)
    total_meals = models.PositiveIntegerField(default=0)
    calories_consumed = models.FloatField(default=0)
    protein_consumed = models.FloatField(default=0)
    carbs_consumed = models.FloatField(default=0)
    fat_consumed = models.FloatField(default=0)
    
    # Target values
    target_calories = models.FloatField(default=0)
    target_protein = models.FloatField(default=0)
    target_carbs = models.FloatField(default=0)
    target_fat = models.FloatField(default=0)
    
    # Completion status
    is_day_completed = models.BooleanField(default=False)
    completed_at = models.DateTimeField(null=True, blank=True)
    
    class Meta:
        unique_together = ['user', 'diet_plan', 'date']
        ordering = ['-date']
    
    @property
    def completion_percentage(self):
        """Calculate overall completion percentage for the day."""
        if self.total_meals == 0:
            return 0
        return round((self.meals_completed / self.total_meals) * 100, 1)
    
    @property
    def calories_percentage(self):
        """Calculate calories percentage of target."""
        if self.target_calories == 0:
            return 0
        return round((self.calories_consumed / self.target_calories) * 100, 1)
    
    @property
    def protein_percentage(self):
        """Calculate protein percentage of target."""
        if self.target_protein == 0:
            return 0
        return round((self.protein_consumed / self.target_protein) * 100, 1)
    
    @property
    def carbs_percentage(self):
        """Calculate carbs percentage of target."""
        if self.target_carbs == 0:
            return 0
        return round((self.carbs_consumed / self.target_carbs) * 100, 1)
    
    @property
    def fat_percentage(self):
        """Calculate fat percentage of target."""
        if self.target_fat == 0:
            return 0
        return round((self.fat_consumed / self.target_fat) * 100, 1)
    
    def update_progress(self):
        """
        Update progress based on completed meals.
        """
        from django.utils import timezone
        
        # Get all meals for this day
        meals = self.diet_plan.meals.filter(date=self.date)
        self.total_meals = meals.count()
        self.meals_completed = sum(1 for meal in meals if meal.is_completed)
        
        # Calculate consumed nutrition
        total_calories = 0
        total_protein = 0
        total_carbs = 0
        total_fat = 0
        
        for meal in meals:
            if meal.is_completed:
                nutrition = meal.calculate_nutrition()
                total_calories += nutrition['calories']
                total_protein += nutrition['protein']
                total_carbs += nutrition['carbs']
                total_fat += nutrition['fat']
        
        self.calories_consumed = total_calories
        self.protein_consumed = total_protein
        self.carbs_consumed = total_carbs
        self.fat_consumed = total_fat
        
        # Check if day is completed
        if self.meals_completed == self.total_meals and self.total_meals > 0:
            self.is_day_completed = True
            self.completed_at = timezone.now()
        
        self.save()

class DailyAdvice(models.Model):
    """
    Stores AI-generated daily dietary advice for a user.
    """
    user = models.ForeignKey(CustomUser, on_delete=models.CASCADE)
    text = models.TextField()
    generated_at = models.DateTimeField(auto_now_add=True)
    context_data = models.JSONField()


class DietConfig(models.Model):
    """Admin-configurable diet generation settings (piece weights, keywords)."""
    piece_weights = models.JSONField(default=dict, blank=True)
    breakfast_allowed_keywords = models.JSONField(default=list, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"DietConfig(updated_at={self.updated_at})"