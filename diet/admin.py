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
from .models import FoodItem, UserFoodPreference, Meal, DietPlan, FoodCategory, MealComponent, DailyAdvice
import requests
import json
from django.contrib import messages
from django.utils.html import format_html
from datetime import date, timedelta

from django.template.response import TemplateResponse

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
        # Placeholder for actual Edamam API integration
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
    list_display = ['template', 'date', 'get_calories']
    def get_calories(self, obj):
        return sum([comp.food.calories * (comp.quantity / 100) for comp in obj.mealcomponent_set.all()])

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
            planner = GPTDietPlanner(pref.user)
            planner.generate_plan()
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

# Register admin classes
admin.site.register(UserFoodPreference, UserFoodPreferenceAdmin)
admin.site.register(Meal, MealAdmin)
admin.site.register(DietPlan, DietPlanAdmin)
admin.site.register(FoodCategory, FoodCategoryAdmin)
admin.site.register(MealComponent)
admin.site.register(DailyAdvice, DailyAdviceAdmin)