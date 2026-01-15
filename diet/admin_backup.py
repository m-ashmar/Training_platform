"""
admin.py - Django Admin Customization for Diet App

This module customizes the Django admin interface for food items, categories, user preferences,
diet plans, meals, meal components, and daily advice. Includes Edamam import, image previews,
and GPT plan generation actions.
"""

from django.contrib import admin
from django import forms
from django.conf import settings
from django.http import HttpResponseRedirect, JsonResponse
from django.urls import path
from django.shortcuts import render, redirect
from .models import FoodItem, UserFoodPreference, Meal, DietPlan, FoodCategory, MealComponent, DailyAdvice, DietConfig, UserFoodCategoryPreference
import requests
import json
from django.contrib import messages
from django.utils.html import format_html
from datetime import date, timedelta

from django.template.response import TemplateResponse
from .ai_services import DietGenerator

# --- FoodItem Admin ---
class FoodSearchForm(forms.Form):
    search_query = forms.CharField(label='Search Food')

class EdamamImportForm(forms.Form):
    query = forms.CharField(label="Search Food", max_length=100, required=True)

@admin.register(FoodItem)
class FoodItemAdmin(admin.ModelAdmin):
    """
    Admin interface for managing food items, including Edamam import and image previews.
    """
    actions = ['import_from_edamam']
    search_fields = ['name']
    list_display = ['name', 'calories', 'category', 'meal_time_filter', 'image_thumbnail', 'protein', 'carbs', 'fat', 'display_serving']
    list_editable = ['category']
    readonly_fields = ['image_preview']
    list_filter = ['category']
    def display_serving(self, obj):
        return f"{obj.serving_size} ({obj.serving_size_grams}g)"
    display_serving.short_description = "Serving"
    def meal_time_filter(self, obj):
        return obj.category.meal_times if obj.category else ''
    meal_time_filter.short_description = 'Meal Time'
    def image_thumbnail(self, obj):
        if obj.image_url:
            return format_html('<img src="{}" style="max-height: 50px; max-width: 50px;" />', obj.image_url)
        return "-"
    image_thumbnail.short_description = 'Photo'
    def image_preview(self, obj):
        if obj.image_url:
            return format_html('<img src="{}" style="max-height: 200px;" />', obj.image_url)
        return "-"
    image_preview.short_description = 'Image Preview'
    fieldsets = (
        (None, {'fields': ('api_id', 'name', 'image_url', 'image_preview')}),
        ('Nutrition', {'fields': ('calories', 'protein', 'carbs', 'fat')}),
    )
    def get_urls(self):
        """
        Add custom admin URLs for Edamam import.
        """
        urls = super().get_urls()
        custom_urls = [
            path('import-edamam/', self.admin_site.admin_view(self.import_food_from_edamam), name='diet_fooditem_import_edamam'),
        ]
        return custom_urls + urls
    def auto_assign_category(self, food_item):
        """
        Assign a food item to a category based on macronutrient content.
        """
        try:
            serving_grams = food_item.serving_size_grams or 100
            protein_per_100g = (food_item.protein / serving_grams) * 100
            carb_per_100g = (food_item.carbs / serving_grams) * 100
            fat_per_100g = (food_item.fat / serving_grams) * 100
            thresholds = {'protein': 10, 'carb': 15, 'fat': 5}
            category = None
            if protein_per_100g >= thresholds['protein'] and protein_per_100g >= carb_per_100g and protein_per_100g >= fat_per_100g:
                category = FoodCategory.objects.filter(is_protein=True).first()
            elif carb_per_100g >= thresholds['carb'] and carb_per_100g >= protein_per_100g and carb_per_100g >= fat_per_100g:
                category = FoodCategory.objects.filter(is_carb=True).first()
            elif fat_per_100g >= thresholds['fat'] and fat_per_100g >= protein_per_100g and fat_per_100g >= carb_per_100g:
                category = FoodCategory.objects.filter(is_fat=True).first()
            if not category:
                category, _ = FoodCategory.objects.get_or_create(name='Other', defaults={'is_protein': False, 'is_carb': False, 'is_fat': False})
            food_item.category = category
            food_item.save()
        except Exception as e:
            print(f"Error assigning category: {str(e)}")
            raise
    def import_food_from_edamam(self, request):
        """
        Custom admin view to import food items from Edamam API.
        """
        selected_items_json = request.POST.getlist("selected_items")
        if request.method == "POST":
            action = request.POST.get("action")
            if action == "search":
                form = EdamamImportForm(request.POST)
                if form.is_valid():
                    query = form.cleaned_data["query"]
                    results = self.search_edamam(query)
                    request.session["edamam_results"] = results
                    return render(request, "admin/diet/fooditem/import_form.html", {"form": form, "results": results, "title": "Import from Edamam"})
            elif action == "import":
                selected_items_ids = request.POST.getlist("selected_items")
                all_results = request.session.get("edamam_results", [])
                imported_count = 0
                for item_id_str in selected_items_ids:
                    try:
                        index = int(item_id_str) - 1
                        item = all_results[index]
                    except (ValueError, IndexError):
                        continue
                    food_data = item.get("food") or item
                    measures = item.get("measures", [])
                    nutrients = food_data.get("nutrients", {})
                    api_id = food_data.get("foodId") or item.get("foodId")
                    name = food_data.get("label") or item.get("name", "")
                    image = food_data.get("image") or item.get("image", "")
                    if not api_id:
                        continue
                    if FoodItem.objects.filter(api_id=api_id).exists():
                        continue
                    calories = nutrients.get("ENERC_KCAL") or item.get("calories")
                    protein = nutrients.get("PROCNT") or item.get("protein")
                    carbs = nutrients.get("CHOCDF") or item.get("carbs")
                    fat = nutrients.get("FAT") or item.get("fat")
                    if None in (calories, protein, carbs, fat):
                        continue
                    serving_size_label = "Gram"
                    serving_size_grams = 100
                    if measures:
                        serving = measures[0]
                        serving_size_label = serving.get("label", "Gram")
                        serving_size_grams = int(serving.get("weight", 100))
                        raw_weight = serving.get("weight", 100)
                        serving_size_grams = self._calculate_serving_size(raw_weight, serving_size_label)
                    food = FoodItem.objects.create(
                        api_id=api_id,
                        name=name,
                        image_url=image,
                        calories=calories,
                        protein=protein,
                        carbs=carbs,
                        fat=fat,
                        serving_size=serving_size_label,
                        serving_size_grams=serving_size_grams
                    )
                    self.auto_assign_category(food)
                    imported_count += 1
                if imported_count > 0:
                    messages.success(request, f"Imported {imported_count} items!")
                else:
                    messages.warning(request, "No valid items selected")
                return redirect('admin:diet_fooditem_import_edamam')
        return render(request, "admin/diet/fooditem/import_form.html", {'form': EdamamImportForm(), 'results': request.session.get("edamam_results", []), 'title': 'Import from Edamam'})
    def _calculate_serving_size(self, weight, label):
        """
        Convert Edamam measures to grams (placeholder logic).
        """
        return int(weight)
    def search_edamam(self, query):
        """
        Search Edamam API for food items matching the query.
        """
        try:
            from .api import search_food
            response = search_food(query)
            hints = response.get('hints', [])
            
            results = []
            for hint in hints:
                food_data = hint.get('food', {})
                measures = hint.get('measures', [])
                
                if food_data:
                    results.append({
                        'food': food_data,
                        'measures': measures,
                        'foodId': food_data.get('foodId'),
                        'name': food_data.get('label'),
                        'image': food_data.get('image'),
                        'calories': food_data.get('nutrients', {}).get('ENERC_KCAL'),
                        'protein': food_data.get('nutrients', {}).get('PROCNT'),
                        'carbs': food_data.get('nutrients', {}).get('CHOCDF'),
                        'fat': food_data.get('nutrients', {}).get('FAT')
                    })
            
            return results
        except Exception as e:
            print(f"Edamam API error: {str(e)}")
            return []
    def import_from_edamam(self, request, queryset):
        """
        Redirect to the custom Edamam import view.
        """
        return HttpResponseRedirect("import-edamam/")

# --- Meal and MealComponent Admin ---
class MealComponentInline(admin.TabularInline):
    model = MealComponent
    extra = 1
    fields = ('food', 'quantity', 'meal_time')

class MealAdmin(admin.ModelAdmin):
    """
    Admin interface for managing meals and their components.
    """
    inlines = [MealComponentInline]
    list_display = ['meal_type', 'date', 'diet_user', 'diet_goal', 'total_calories', 'components_count']
    readonly_fields = []
    search_fields = ['diet_plan__user__email', 'diet_plan__user__username', 'description']
    list_filter = ['meal_type', 'date', 'diet_plan__goal']
    
    def diet_user(self, obj):
        return getattr(obj.diet_plan.user, 'email', obj.diet_plan.user_id)
    diet_user.short_description = 'User'
    
    def diet_goal(self, obj):
        return obj.diet_plan.goal
    diet_goal.short_description = 'Goal'
    
    def total_calories(self, obj):
        try:
            n = obj.calculate_nutrition()
            return round(n.get('calories', 0), 1)
        except Exception:
            return 0
    total_calories.short_description = 'Calories'
    
    def components_count(self, obj):
        try:
            return obj.components.count()
        except Exception:
            return 0
    components_count.short_description = 'Components'
    def get_calories(self, obj):
        try:
            return sum([comp.food.calories * (comp.quantity / 100) for comp in obj.mealcomponent_set.all()])
        except (AttributeError, ZeroDivisionError):
            return 0

class MealInline(admin.TabularInline):
    model = Meal
    extra = 0
    fields = ('template', 'date')

# --- UserFoodPreference Admin ---
class UserFoodPreferenceAdmin(admin.ModelAdmin):
    """
    Admin interface for managing user food preferences and generating GPT diet plans.
    """
    filter_horizontal = ['protein_choices', 'carb_choices', 'fat_choices']
    list_display = ['user', 'get_protein_count', 'get_carb_count', 'get_fat_count']
    actions = ['generate_gpt_plan']
    def get_urls(self):
        urls = super().get_urls()
        custom_urls = [
            path('generate-gpt-plan/', self.admin_site.admin_view(self.generate_gpt_plan_view), name='diet_userfoodpreference_generate_gpt_plan'),
        ]
        return custom_urls + urls
    def generate_gpt_plan(self, request, queryset):
        """
        Admin action to generate a GPT diet plan for selected users.
        """
        for pref in queryset:
            try:
                generator = DietGenerator(pref.user)
                output = generator.generate_plan()
                generator.save_plan_to_database(output, meal_count=3, snack_count=0)
            except Exception as e:
                messages.error(request, f"Failed to generate plan for {pref.user}: {e}")
        self.message_user(request, "GPT diet plan generation started.")
    def generate_gpt_plan_view(self, request):
        """
        Custom admin view for GPT plan generation.
        """
        return TemplateResponse(request, "admin/diet/dietplan/generate_gpt_plan.html", {})
    def get_protein_count(self, obj):
        return obj.protein_choices.count()
    def get_carb_count(self, obj):
        return obj.carb_choices.count()
    def get_fat_count(self, obj):
        return obj.fat_choices.count()

# --- DietPlan Admin ---
class DietPlanAdmin(admin.ModelAdmin):
    """
    Admin interface for managing diet plans and viewing plan details.
    """
    inlines = [MealInline]
    list_display = ['user', 'goal', 'daily_calories', 'start_date', 'end_date', 'generation_strategy', 'view_plan_link']
    list_filter = ['goal', 'generation_strategy', 'start_date']
    search_fields = ['user__email', 'user__username']
    def view_plan_link(self, obj):
        return format_html('<a href="{}">View Plan</a>', f'/admin/diet/dietplan/{obj.id}/view/')
    def get_urls(self):
        urls = super().get_urls()
        custom_urls = [
            path('<int:plan_id>/view/', self.admin_site.admin_view(self.view_plan_details), name='diet_dietplan_view_plan'),
        ]
        return custom_urls + urls
    def view_plan_details(self, request, plan_id):
        plan = DietPlan.objects.get(id=plan_id)
        return TemplateResponse(request, "admin/diet/dietplan/view_plan.html", {"plan": plan})

# --- FoodCategory Admin ---
class FoodCategoryAdmin(admin.ModelAdmin):
    """
    Admin interface for managing food categories.
    """
    list_display = ['name', 'meal_times', 'is_protein', 'is_carb', 'is_fat']
    list_editable = ['meal_times', 'is_protein', 'is_carb', 'is_fat']
    list_filter = ['meal_times']

# --- DailyAdvice Admin ---
class DailyAdviceAdmin(admin.ModelAdmin):
    """
    Admin interface for viewing daily advice records.
    """
    list_display = ['user', 'generated_at', 'text']
    readonly_fields = ['context_data']

# DietConfig Admin
@admin.register(DietConfig)
class DietConfigAdmin(admin.ModelAdmin):
    list_display = ['updated_at']
    readonly_fields = ['updated_at']

# Register admin classes
admin.site.register(UserFoodPreference, UserFoodPreferenceAdmin)
admin.site.register(Meal, MealAdmin)
admin.site.register(DietPlan, DietPlanAdmin)
admin.site.register(FoodCategory, FoodCategoryAdmin)
@admin.register(MealComponent)
class MealComponentAdmin(admin.ModelAdmin):
    list_display = ['component_label', 'meal_link', 'diet_link', 'user_email', 'quantity', 'macro_info']
    search_fields = ['meal__diet_plan__user__email', 'food__name']
    list_filter = ['meal__meal_type']
    raw_id_fields = ['meal', 'food']

    def component_label(self, obj):
        try:
            return f"{obj.food.name} ({round(obj.quantity,1)}g)"
        except Exception:
            return str(obj.pk)
    component_label.short_description = 'Component'
    
    def meal_link(self, obj):
        return format_html('<a href="/admin/diet/meal/{}/change/">{}</a>', obj.meal.id, obj.meal.meal_type)
    meal_link.short_description = 'Meal'
    
    def diet_link(self, obj):
        dp = obj.meal.diet_plan
        return format_html('<a href="/admin/diet/dietplan/{}/change/">Plan #{}</a>', dp.id, dp.id)
    diet_link.short_description = 'Diet Plan'
    
    def user_email(self, obj):
        return getattr(obj.meal.diet_plan.user, 'email', obj.meal.diet_plan.user_id)
    user_email.short_description = 'User'
    
    def macro_info(self, obj):
        f = obj.food
        return f"P:{f.protein}g C:{f.carbs}g F:{f.fat}g per {f.serving_size}"
    macro_info.short_description = 'Food macros'
admin.site.register(DailyAdvice, DailyAdviceAdmin)

# UserFoodCategoryPreference Admin
@admin.register(UserFoodCategoryPreference)
class UserFoodCategoryPreferenceAdmin(admin.ModelAdmin):
    list_display = ['user', 'food', 'meal', 'macro', 'updated_at']
    list_filter = ['meal', 'macro']
    search_fields = ['user__email', 'food__name']