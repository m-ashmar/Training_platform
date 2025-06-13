# models.py (new diet app)
from django.db import models
from users.models import CustomUser
from datetime import date

class FoodCategory(models.Model):
    MEAL_TIME_CHOICES = [
        ('ANY', 'Any Time'),
        ('BREAKFAST', 'Breakfast'),
        ('LUNCH', 'Lunch'), 
        ('DINNER', 'Dinner'),
    ]
    
    name = models.CharField(max_length=50)  # e.g., "Proteins", "Morning Carbs"
    meal_times = models.CharField(max_length=20, choices=MEAL_TIME_CHOICES, default='ANY')
    is_protein = models.BooleanField(default=False)
    is_carb = models.BooleanField(default=False)
    is_fat = models.BooleanField(default=False)
    
    
    

    def __str__(self):
        return f"{self.name} ({self.get_meal_times_display()})"

        
class FoodItem(models.Model):
    api_id = models.CharField(max_length=255, unique=True)  # Unique constraint here
    name = models.CharField(max_length=255)
    image_url = models.URLField(max_length=500, blank=True, null=True) 
    calories = models.FloatField()
    protein = models.FloatField()
    carbs = models.FloatField()
    fat = models.FloatField()
    serving_size = models.CharField(max_length=100) 
    #cost_per_serving = models.FloatField(null=True, blank=True)
    
    category = models.ForeignKey(FoodCategory, on_delete=models.SET_NULL, null=True, blank=True)
    serving_size_grams = models.PositiveIntegerField(default=100)  # Add this
    calories_per_gram = models.FloatField(default=1.0)  # Calculated field
    protein_per_gram = models.FloatField(default=0.0)
    carbs_per_gram = models.FloatField(default=0.0)
    fat_per_gram = models.FloatField(default=0.0)

    def save(self, *args, **kwargs):
        # Auto-calculate per-gram values
        if self.serving_size_grams > 0:
            self.calories_per_gram = self.calories / self.serving_size_grams
            self.protein_per_gram = self.protein / self.serving_size_grams
            self.carbs_per_gram = self.carbs / self.serving_size_grams
            self.fat_per_gram = self.fat / self.serving_size_grams
        else:
        # Set safe defaults if serving size is missing
            self.serving_size_grams = 100
            self.calories_per_gram = self.calories / 100
            self.protein_per_gram = self.protein / 100
            self.carbs_per_gram = self.carbs / 100
            self.fat_per_gram = self.fat / 100    
        super().save(*args, **kwargs)
    
    def __str__(self):
        return self.name

class UserFoodPreference(models.Model):
    user = models.ForeignKey(CustomUser, on_delete=models.CASCADE)
    liked_foods = models.ManyToManyField(FoodItem, related_name='liked_by', blank=True)
    disliked_foods = models.ManyToManyField(FoodItem, related_name='disliked_by', blank=True)
    allergies = models.TextField(blank=True)
    protein_choices = models.ManyToManyField(FoodItem, related_name='protein_prefs', limit_choices_to={'category__name': 'Proteins'})
    carb_choices = models.ManyToManyField(FoodItem, related_name='carb_prefs', limit_choices_to={'category__name': 'Carbs'})
    fat_choices = models.ManyToManyField(FoodItem, related_name='fat_prefs', limit_choices_to={'category__name': 'Fats'})

    
class DietPlan(models.Model):
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
        choices=[('GPT', 'AI Generated'), ('FALLBACK', 'Rule-Based')],
        default='GPT'
    )
    
    @property
    def period(self):
        # Smart period detection
        days = (self.end_date - self.start_date).days
        return 'weekly' if days == 7 else 'monthly' if days > 14 else 'daily'
    
    
 
    
class Meal(models.Model):
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
    description = models.TextField(blank=True)  # For GPT-generated descriptions
    is_ai_generated = models.BooleanField(default=False)

    
    
    
class DailyAdvice(models.Model):
    user = models.ForeignKey(CustomUser, on_delete=models.CASCADE)
    text = models.TextField()
    generated_at = models.DateTimeField(auto_now_add=True)
    context_data = models.JSONField()  # Stores input data for debugging





class MealComponent(models.Model):
    meal = models.ForeignKey('Meal', on_delete=models.CASCADE )
    food = models.ForeignKey(FoodItem, on_delete=models.CASCADE)
    quantity = models.FloatField()
    meal_time = models.CharField(  # Changed from portion_type
        max_length=20, 
        choices=Meal.MEAL_TYPES, 
        default='Lunch'
    )