"""
Improved Django Admin Configuration for Diet System
This module provides a clean, powerful admin interface with proper permissions and features.
"""

from django.contrib import admin
from django.contrib.admin import SimpleListFilter
from django.utils.html import format_html
from django.db.models import Count, Sum, Avg, Q
from django.urls import path, reverse
from django.shortcuts import redirect, render
from django.contrib import messages
from django.http import HttpResponse, HttpResponseRedirect
from modeltranslation.admin import TranslationAdmin as TranslAdmin
from django import forms
import csv
from datetime import datetime, timedelta

from .models import (
    FoodItem, FoodCategory, UserFoodPreference, UserFoodCategoryPreference,
    DietPlan, Meal, MealComponent, DietConfig, DailyAdvice, DietPlanTemplate
)


# Form for Edamam import
class EdamamImportForm(forms.Form):
    query = forms.CharField(label="Search Food", max_length=100, required=True, widget=forms.TextInput(attrs={'placeholder': 'e.g., chicken breast, rice, apple'}))


# Custom Filters
class MacroDominantFilter(SimpleListFilter):
    title = 'Dominant Macro'
    parameter_name = 'dominant_macro'
    
    def lookups(self, request, model_admin):
        return (
            ('protein', 'Protein Dominant'),
            ('carb', 'Carb Dominant'),
            ('fat', 'Fat Dominant'),
            ('balanced', 'Balanced'),
        )
    
    def queryset(self, request, queryset):
        if self.value() == 'protein':
            return queryset.filter(category__is_protein=True)
        elif self.value() == 'carb':
            return queryset.filter(category__is_carb=True)
        elif self.value() == 'fat':
            return queryset.filter(category__is_fat=True)
        elif self.value() == 'balanced':
            return queryset.filter(
                category__is_protein=False,
                category__is_carb=False,
                category__is_fat=False
            )


class CalorieRangeFilter(SimpleListFilter):
    title = 'Calorie Range'
    parameter_name = 'calorie_range'
    
    def lookups(self, request, model_admin):
        return (
            ('0-100', 'Low (0-100)'),
            ('100-200', 'Medium-Low (100-200)'),
            ('200-400', 'Medium (200-400)'),
            ('400+', 'High (400+)'),
        )
    
    def queryset(self, request, queryset):
        if self.value() == '0-100':
            return queryset.filter(calories__lte=100)
        elif self.value() == '100-200':
            return queryset.filter(calories__gt=100, calories__lte=200)
        elif self.value() == '200-400':
            return queryset.filter(calories__gt=200, calories__lte=400)
        elif self.value() == '400+':
            return queryset.filter(calories__gt=400)


# Improved FoodItem Admin
@admin.register(FoodItem)
class ImprovedFoodItemAdmin(TranslAdmin):
    list_display = [
        'name', 'category_badge', 'calories_display', 'macro_display',
        'serving_info', 'per_gram_info', 'has_image'
    ]
    list_filter = ['category', MacroDominantFilter, CalorieRangeFilter]
    search_fields = ['name', 'api_id']
    readonly_fields = ['image_preview', 'nutrition_analysis']
    
    fieldsets = (
        ('Basic Information', {
            'fields': ('name', 'category', 'api_id', 'image_url', 'image_preview')
        }),
        ('Nutrition Per Serving', {
            'fields': ('calories', 'protein', 'carbs', 'fat', 'serving_size', 'serving_size_grams'),
            'description': 'Nutrition values per serving'
        }),
        ('Nutrition Per Gram', {
            'fields': ('calories_per_gram', 'protein_per_gram', 'carbs_per_gram', 'fat_per_gram'),
            'description': 'Automatically calculated or manually set per-gram values'
        }),
        ('Analysis', {
            'fields': ('nutrition_analysis',),
            'classes': ('collapse',)
        })
    )
    
    def category_badge(self, obj):
        if not obj.category:
            return '-'
        color = 'green' if obj.category.is_protein else 'blue' if obj.category.is_carb else 'orange' if obj.category.is_fat else 'gray'
        return format_html('<span style="background:{}; color:white; padding:3px 8px; border-radius:3px;">{}</span>',
                          color, obj.category.name)
    category_badge.short_description = 'Category'
    
    def calories_display(self, obj):
        return f"{obj.calories:.0f} kcal"
    calories_display.short_description = 'Calories'
    calories_display.admin_order_field = 'calories'
    
    def macro_display(self, obj):
        return format_html(
            '<span title="Protein">P:{}g</span> | <span title="Carbs">C:{}g</span> | <span title="Fat">F:{}g</span>',
            f"{(obj.protein or 0):.1f}",
            f"{(obj.carbs or 0):.1f}",
            f"{(obj.fat or 0):.1f}"
        )
    macro_display.short_description = 'Macros'
    
    def serving_info(self, obj):
        return f"{obj.serving_size} ({obj.serving_size_grams}g)"
    serving_info.short_description = 'Serving'
    
    def per_gram_info(self, obj):
        if obj.calories_per_gram:
            return format_html('<small>{} kcal/g</small>', f"{obj.calories_per_gram:.2f}")
        return '-'
    per_gram_info.short_description = 'Per Gram'
    
    def has_image(self, obj):
        return bool(obj.image_url)
    has_image.short_description = 'Image'
    has_image.boolean = True
    
    def image_preview(self, obj):
        if obj.image_url:
            return format_html('<img src="{}" style="max-height: 200px; max-width: 200px;" />', obj.image_url)
        return "No image"
    image_preview.short_description = 'Preview'
    
    def nutrition_analysis(self, obj):
        try:
            serving_g = obj.serving_size_grams or 100
            p_ratio = (obj.protein * 4 / obj.calories * 100) if obj.calories > 0 else 0
            c_ratio = (obj.carbs * 4 / obj.calories * 100) if obj.calories > 0 else 0
            f_ratio = (obj.fat * 9 / obj.calories * 100) if obj.calories > 0 else 0
            
            dominant = 'Protein' if p_ratio > c_ratio and p_ratio > f_ratio else \
                      'Carb' if c_ratio > p_ratio and c_ratio > f_ratio else \
                      'Fat' if f_ratio > p_ratio and f_ratio > c_ratio else 'Balanced'
            
            return format_html(
                '<div style="font-family: monospace;">'
                '<strong>Macro Ratios:</strong><br>'
                'Protein: {}%<br>'
                'Carbs: {}%<br>'
                'Fat: {}%<br>'
                '<strong>Dominant:</strong> {}<br>'
                '<strong>Calorie Density:</strong> {} kcal/g'
                '</div>',
                f"{p_ratio:.1f}", f"{c_ratio:.1f}", f"{f_ratio:.1f}", dominant,
                f"{(obj.calories / serving_g if serving_g > 0 else 0):.2f}"
            )
        except Exception:
            return "Error calculating analysis"
    nutrition_analysis.short_description = 'Nutritional Analysis'
    
    def save_model(self, request, obj, form, change):
        # Auto-calculate per-gram values if not set
        if obj.serving_size_grams and obj.serving_size_grams > 0:
            if not obj.calories_per_gram:
                obj.calories_per_gram = obj.calories / obj.serving_size_grams
            if not obj.protein_per_gram:
                obj.protein_per_gram = obj.protein / obj.serving_size_grams
            if not obj.carbs_per_gram:
                obj.carbs_per_gram = obj.carbs / obj.serving_size_grams
            if not obj.fat_per_gram:
                obj.fat_per_gram = obj.fat / obj.serving_size_grams
        super().save_model(request, obj, form, change)
    
    actions = ['export_as_csv', 'calculate_per_gram_values', 'import_from_edamam']
    
    def export_as_csv(self, request, queryset):
        response = HttpResponse(content_type='text/csv')
        response['Content-Disposition'] = 'attachment; filename="food_items.csv"'
        
        writer = csv.writer(response)
        writer.writerow(['Name', 'Category', 'Calories', 'Protein', 'Carbs', 'Fat', 
                        'Serving Size', 'Serving Grams', 'Cal/g', 'P/g', 'C/g', 'F/g'])
        
        for item in queryset:
            writer.writerow([
                item.name, item.category.name if item.category else '',
                item.calories, item.protein, item.carbs, item.fat,
                item.serving_size, item.serving_size_grams,
                item.calories_per_gram, item.protein_per_gram,
                item.carbs_per_gram, item.fat_per_gram
            ])
        
        return response
    export_as_csv.short_description = "Export selected items as CSV"
    
    def calculate_per_gram_values(self, request, queryset):
        updated = 0
        for item in queryset:
            if item.serving_size_grams and item.serving_size_grams > 0:
                item.calories_per_gram = item.calories / item.serving_size_grams
                item.protein_per_gram = item.protein / item.serving_size_grams
                item.carbs_per_gram = item.carbs / item.serving_size_grams
                item.fat_per_gram = item.fat / item.serving_size_grams
                item.save()
                updated += 1
        
        self.message_user(request, f"Calculated per-gram values for {updated} items.")
    calculate_per_gram_values.short_description = "Calculate per-gram values"
    
    def get_urls(self):
        """
        Add custom admin URLs for Edamam import.
        """
        urls = super().get_urls()
        custom_urls = [
            path('import-edamam/', self.admin_site.admin_view(self.import_food_from_edamam), name='diet_fooditem_import_edamam'),
        ]
        return custom_urls + urls
    
    def import_food_from_edamam(self, request):
        """
        Custom admin view to import food items from Edamam API.
        """
        if request.method == "POST":
            action = request.POST.get("action")
            if action == "search":
                form = EdamamImportForm(request.POST)
                if form.is_valid():
                    query = form.cleaned_data["query"]
                    results = self.search_edamam(query, request)
                    request.session["edamam_results"] = results
                    return render(request, "admin/diet/fooditem/import_form.html", {
                        "form": form, 
                        "results": results, 
                        "title": "Import from Edamam"
                    })
            elif action == "import":
                selected_items_ids = request.POST.getlist("selected_items")
                all_results = request.session.get("edamam_results", [])
                imported_count = 0
                skipped_count = 0
                
                for item_id_str in selected_items_ids:
                    try:
                        index = int(item_id_str) - 1
                        if index < 0 or index >= len(all_results):
                            continue
                        item = all_results[index]
                    except (ValueError, IndexError):
                        continue
                    
                    food_data = item.get("food") or item
                    measures = item.get("measures", [])
                    nutrients = food_data.get("nutrients", {})
                    api_id = food_data.get("foodId") or item.get("foodId")
                    name = food_data.get("label") or item.get("name", "")
                    image = food_data.get("image") or item.get("image", "")
                    
                    if not api_id or not name:
                        skipped_count += 1
                        continue
                    
                    # Check if already exists
                    if FoodItem.objects.filter(api_id=api_id).exists():
                        skipped_count += 1
                        continue
                    
                    calories = nutrients.get("ENERC_KCAL") or item.get("calories", 0)
                    protein = nutrients.get("PROCNT") or item.get("protein", 0)
                    carbs = nutrients.get("CHOCDF") or item.get("carbs", 0)
                    fat = nutrients.get("FAT") or item.get("fat", 0)
                    
                    if calories is None or protein is None or carbs is None or fat is None:
                        skipped_count += 1
                        continue
                    
                    # Always normalize to 100g serving size
                    # Edamam returns nutrients per 100g by default
                    serving_size_label = "100g"
                    serving_size_grams = 100
                    
                    # Note: Edamam nutrients are already per 100g, so we use 100g as standard
                    
                    # Create food item
                    try:
                        food = FoodItem.objects.create(
                            api_id=api_id,
                            name=name,
                            image_url=image,
                            calories=calories or 0,
                            protein=protein or 0,
                            carbs=carbs or 0,
                            fat=fat or 0,
                            serving_size=serving_size_label,
                            serving_size_grams=serving_size_grams
                        )
                        self.auto_assign_category(food)
                        imported_count += 1
                    except Exception as e:
                        skipped_count += 1
                        continue
                
                if imported_count > 0:
                    messages.success(request, f"Successfully imported {imported_count} food item(s)!")
                if skipped_count > 0:
                    messages.warning(request, f"Skipped {skipped_count} item(s) (already exist or invalid data).")
                
                return redirect('admin:diet_fooditem_import_edamam')
        
        return render(request, "admin/diet/fooditem/import_form.html", {
            'form': EdamamImportForm(), 
            'results': request.session.get("edamam_results", []), 
            'title': 'Import from Edamam'
        })
    
    def search_edamam(self, query, request=None):
        """
        Search Edamam API for food items matching the query.
        """
        try:
            from .api import search_food
            response = search_food(query)
            hints = response.get('hints', [])
            
            results = []
            for hint in hints[:20]:  # Limit to 20 results
                food_data = hint.get('food', {})
                measures = hint.get('measures', [])
                
                if food_data:
                    nutrients = food_data.get('nutrients', {})
                    # Extract serving size information
                    serving_info = "100g"
                    if measures:
                        # Get the first measure (usually the standard one)
                        first_measure = measures[0]
                        serving_label = first_measure.get('label', '100g')
                        serving_weight = first_measure.get('weight', 100)
                        serving_info = f"{serving_label} ({serving_weight:.0f}g)"
                    
                    results.append({
                        'food': food_data,
                        'measures': measures,
                        'foodId': food_data.get('foodId'),
                        'name': food_data.get('label', ''),
                        'image': food_data.get('image', ''),
                        'calories': nutrients.get('ENERC_KCAL', 0),
                        'protein': nutrients.get('PROCNT', 0),
                        'carbs': nutrients.get('CHOCDF', 0),
                        'fat': nutrients.get('FAT', 0),
                        'serving': serving_info
                    })
            
            return results
        except Exception as e:
            # Log error
            import logging
            logger = logging.getLogger(__name__)
            logger.error(f"Edamam API error: {str(e)}")
            if request:
                messages.error(request, f"Edamam API error: {str(e)}")
            return []
    
    def _calculate_serving_size(self, weight, label):
        """
        Convert Edamam measures to grams.
        """
        try:
            return int(float(weight))
        except (ValueError, TypeError):
            return 100
    
    def auto_assign_category(self, food_item):
        """
        Assign a food item to a category based on macronutrient content.
        """
        try:
            serving_grams = food_item.serving_size_grams or 100
            if serving_grams <= 0:
                serving_grams = 100
            
            protein_per_100g = (food_item.protein / serving_grams) * 100 if serving_grams > 0 else 0
            carb_per_100g = (food_item.carbs / serving_grams) * 100 if serving_grams > 0 else 0
            fat_per_100g = (food_item.fat / serving_grams) * 100 if serving_grams > 0 else 0
            
            thresholds = {'protein': 10, 'carb': 15, 'fat': 5}
            category = None
            
            if protein_per_100g >= thresholds['protein'] and protein_per_100g >= carb_per_100g and protein_per_100g >= fat_per_100g:
                category = FoodCategory.objects.filter(is_protein=True).first()
            elif carb_per_100g >= thresholds['carb'] and carb_per_100g >= protein_per_100g and carb_per_100g >= fat_per_100g:
                category = FoodCategory.objects.filter(is_carb=True).first()
            elif fat_per_100g >= thresholds['fat'] and fat_per_100g >= protein_per_100g and fat_per_100g >= carb_per_100g:
                category = FoodCategory.objects.filter(is_fat=True).first()
            
            if not category:
                category, _ = FoodCategory.objects.get_or_create(
                    name='Other', 
                    defaults={'is_protein': False, 'is_carb': False, 'is_fat': False}
                )
            
            food_item.category = category
            food_item.save()
        except Exception as e:
            # Silently fail - category assignment is not critical
            pass
    
    def import_from_edamam(self, request, queryset):
        """
        Admin action to redirect to the Edamam import page.
        """
        return HttpResponseRedirect("import-edamam/")
    import_from_edamam.short_description = "Import from Edamam API"


# Improved DietPlan Admin
@admin.register(DietPlan)
class ImprovedDietPlanAdmin(admin.ModelAdmin):
    list_display = [
        'id', 'user_link', 'goal', 'daily_calories', 'date_range',
        'meal_count', 'compliance_rate', 'strategy', 'status_badge'
    ]
    list_filter = ['goal', 'generation_strategy', 'created_at']
    search_fields = ['user__email', 'user__username']
    date_hierarchy = 'start_date'
    
    readonly_fields = ['nutrition_summary', 'meal_breakdown']
    
    fieldsets = (
        ('User & Goal', {
            'fields': ('user', 'goal', 'daily_calories')
        }),
        ('Schedule', {
            'fields': ('start_date', 'end_date', 'duration_weeks')
        }),
        ('Generation', {
            'fields': ('generation_strategy', 'generated_plan')
        }),
        ('Analytics', {
            'fields': ('nutrition_summary', 'meal_breakdown'),
            'classes': ('wide',)
        })
    )
    
    def user_link(self, obj):
        return format_html('<a href="{}">{}</a>',
                          reverse('admin:users_customuser_change', args=[obj.user.id]),
                          obj.user.email)
    user_link.short_description = 'User'
    
    def date_range(self, obj):
        return f"{obj.start_date} to {obj.end_date}"
    date_range.short_description = 'Period'
    
    def meal_count(self, obj):
        return obj.meals.count()
    meal_count.short_description = 'Meals'
    
    def compliance_rate(self, obj):
        total = obj.meals.count()
        if total == 0:
            return '-'
        # BUG FIX: is_completed field might not exist, calculate based on component count
        completed = obj.meals.filter(components__isnull=False).distinct().count()
        rate = (completed / total) * 100 if total > 0 else 0
        color = 'green' if rate >= 80 else 'orange' if rate >= 50 else 'red'
        return format_html('<span style="color: {};">{}%</span>', color, f"{rate:.0f}")
    compliance_rate.short_description = 'Compliance'
    
    def strategy(self, obj):
        return obj.generation_strategy
    strategy.short_description = 'Strategy'
    
    def status_badge(self, obj):
        from datetime import date
        if obj.end_date < date.today():
            badge = '<span style="background:gray;">Completed</span>'
        elif obj.start_date <= date.today() <= obj.end_date:
            badge = '<span style="background:green;">Active</span>'
        else:
            badge = '<span style="background:blue;">Upcoming</span>'
        return format_html(badge)
    status_badge.short_description = 'Status'
    
    def nutrition_summary(self, obj):
        try:
            meals = obj.meals.all()
            if not meals:
                return "No meals"
            
            totals = {'calories': 0, 'protein': 0, 'carbs': 0, 'fat': 0}
            for meal in meals:
                nutrition = meal.calculate_nutrition()
                for key in totals:
                    totals[key] += nutrition.get(key, 0)
            
            days = (obj.end_date - obj.start_date).days + 1
            daily_avg = {k: v / days for k, v in totals.items()}
            
            return format_html(
                '<div style="font-family: monospace;">'
                '<strong>Daily Averages:</strong><br>'
                'Calories: {} kcal (Target: {})<br>'
                'Protein: {}g<br>'
                'Carbs: {}g<br>'
                'Fat: {}g'
                '</div>',
                f"{daily_avg['calories']:.0f}", f"{obj.daily_calories:.0f}",
                f"{daily_avg['protein']:.1f}", f"{daily_avg['carbs']:.1f}", f"{daily_avg['fat']:.1f}"
            )
        except Exception:
            return "Error calculating summary"
    nutrition_summary.short_description = 'Nutrition Summary'
    
    def meal_breakdown(self, obj):
        from django.db.models import Count
        breakdown = obj.meals.values('meal_type').annotate(count=Count('id'))
        html = '<div style="font-family: monospace;"><strong>Meal Distribution:</strong><br>'
        for item in breakdown:
            html += f"{item['meal_type']}: {item['count']}<br>"
        html += '</div>'
        return format_html(html)
    meal_breakdown.short_description = 'Meal Breakdown'


# Simplified UserFoodCategoryPreference Admin
@admin.register(UserFoodCategoryPreference)
class ImprovedUserFoodCategoryPreferenceAdmin(admin.ModelAdmin):
    list_display = ['user', 'food_name', 'meal', 'macro', 'created_at']
    list_filter = ['meal', 'macro', 'created_at']
    search_fields = ['user__email', 'food__name']
    autocomplete_fields = ['user', 'food']
    
    def food_name(self, obj):
        return obj.food.name
    food_name.short_description = 'Food'
    food_name.admin_order_field = 'food__name'
    
    def get_queryset(self, request):
        return super().get_queryset(request).select_related('user', 'food')


# Meal Admin - Simplified
@admin.register(Meal)
class ImprovedMealAdmin(admin.ModelAdmin):
    list_display = ['id', 'meal_type', 'date', 'user_email', 'nutrition_info']
    list_filter = ['meal_type', 'date']
    search_fields = ['diet_plan__user__email', 'description']
    date_hierarchy = 'date'
    
    def user_email(self, obj):
        return obj.diet_plan.user.email
    user_email.short_description = 'User'
    
    def nutrition_info(self, obj):
        try:
            n = obj.calculate_nutrition()
            return format_html(
                '<small>Cal:{} P:{}g C:{}g F:{}g</small>',
                f"{n.get('calories', 0):.0f}", f"{n.get('protein', 0):.0f}",
                f"{n.get('carbs', 0):.0f}", f"{n.get('fat', 0):.0f}"
            )
        except Exception:
            return '-'
    nutrition_info.short_description = 'Nutrition'


# Clean up unnecessary admins
# Remove MealComponent from main view - only accessible inline
class MealComponentInline(admin.TabularInline):
    model = MealComponent
    extra = 0
    fields = ('food', 'quantity')
    autocomplete_fields = ['food']


# Food Category - Minimal
@admin.register(FoodCategory)
class ImprovedFoodCategoryAdmin(TranslAdmin):
    list_display = ['name', 'macro_type', 'item_count']
    
    def macro_type(self, obj):
        types = []
        if obj.is_protein: types.append('Protein')
        if obj.is_carb: types.append('Carb')
        if obj.is_fat: types.append('Fat')
        return ', '.join(types) if types else 'Mixed'
    macro_type.short_description = 'Type'
    
    def item_count(self, obj):
        return obj.fooditem_set.count()
    item_count.short_description = 'Items'


# Diet Config - Super Admin Only
@admin.register(DietConfig)
class ImprovedDietConfigAdmin(admin.ModelAdmin):
    list_display = ['id', 'updated_at']
    
    def has_add_permission(self, request):
        # Only allow one config
        return not DietConfig.objects.exists()
    
    def has_delete_permission(self, request, obj=None):
        # Prevent deletion of config
        return False


# Remove/Hide less important models from admin
try:
    admin.site.unregister(DailyAdvice)  # Remove from main admin
except admin.sites.NotRegistered:
    pass

try:
    admin.site.unregister(UserFoodPreference)  # Keep in database but hide from admin
except admin.sites.NotRegistered:
    pass


# DietPlanTemplate Admin with translation support
@admin.register(DietPlanTemplate)
class DietPlanTemplateAdmin(TranslAdmin):
    """Admin for DietPlanTemplate with multilingual name/description."""
    list_display = ['name', 'meals_per_day', 'snacks_per_day', 'days_variation', 'is_active', 'created_at']
    list_filter = ['is_active', 'meals_per_day']
    search_fields = ['name', 'description']
    readonly_fields = ['created_at']


# Custom admin dashboard
admin.site.site_header = "Diet Management System"
admin.site.site_title = "Diet Admin"
admin.site.index_title = "Diet System Dashboard"


# TODO: Add these features in future updates
# - Bulk meal generation action
# - User diet history charts
# - Food recommendation engine
# - Nutritional goal tracking
# - Export diet plans to PDF
# - Integration with fitness trackers
# - Automated meal prep scheduling
# - Shopping list generation
# - Recipe suggestions based on available foods
