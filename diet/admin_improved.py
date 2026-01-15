"""
Improved Django Admin Configuration for Diet System
This module provides a clean, powerful admin interface with proper permissions and features.
"""

from django.contrib import admin
from django.contrib.admin import SimpleListFilter
from django.utils.html import format_html
from django.db.models import Count, Sum, Avg, Q
from django.urls import path, reverse
from django.shortcuts import redirect
from django.contrib import messagesgene
from datetime import datetime, timedelta

from .models import (
    FoodItem, FoodCategory, UserFoodPreference, UserFoodCategoryPreference,
    DietPlan, Meal, MealComponent, DietConfig, DailyAdvice, DietPlanTemplate
)


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
class ImprovedFoodItemAdmin(admin.ModelAdmin):
    list_display = [
        'name', 'category_badge', 'calories_display', 'macro_display',
        'serving_info', 'per_gram_info', 'has_image'
    ]
    list_filter = ['category', MacroDominantFilter, CalorieRangeFilter]
    search_fields = ['name', 'api_id']
    readonly_fields = ['created_at', 'updated_at', 'image_preview', 'nutrition_analysis']
    
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
        }),
        ('Metadata', {
            'fields': ('created_at', 'updated_at'),
            'classes': ('collapse',)
        }),
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
        return format_html('<span title="Protein">P:{:.1f}g</span> | <span title="Carbs">C:{:.1f}g</span> | <span title="Fat">F:{:.1f}g</span>',
                          obj.protein or 0, obj.carbs or 0, obj.fat or 0)
    macro_display.short_description = 'Macros'
    
    def serving_info(self, obj):
        return f"{obj.serving_size} ({obj.serving_size_grams}g)"
    serving_info.short_description = 'Serving'
    
    def per_gram_info(self, obj):
        if obj.calories_per_gram:
            return format_html('<small>{:.2f} kcal/g</small>', obj.calories_per_gram)
        return '-'
    per_gram_info.short_description = 'Per Gram'
    
    def has_image(self, obj):
        return '✓' if obj.image_url else '✗'
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
                'Protein: {:.1f}%<br>'
                'Carbs: {:.1f}%<br>'
                'Fat: {:.1f}%<br>'
                '<strong>Dominant:</strong> {}<br>'
                '<strong>Calorie Density:</strong> {:.2f} kcal/g'
                '</div>',
                p_ratio, c_ratio, f_ratio, dominant,
                obj.calories / serving_g if serving_g > 0 else 0
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
    
    actions = ['export_as_csv', 'calculate_per_gram_values']
    
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
    
    readonly_fields = ['nutrition_summary', 'meal_breakdown', 'created_at', 'updated_at']
    
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
        }),
        ('Metadata', {
            'fields': ('created_at', 'updated_at'),
            'classes': ('collapse',)
        }),
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
        completed = obj.meals.filter(is_completed=True).count()
        rate = (completed / total) * 100
        color = 'green' if rate >= 80 else 'orange' if rate >= 50 else 'red'
        return format_html('<span style="color: {};">{:.0f}%</span>', color, rate)
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
                'Calories: {:.0f} kcal (Target: {:.0f})<br>'
                'Protein: {:.1f}g<br>'
                'Carbs: {:.1f}g<br>'
                'Fat: {:.1f}g'
                '</div>',
                daily_avg['calories'], obj.daily_calories,
                daily_avg['protein'], daily_avg['carbs'], daily_avg['fat']
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
    list_display = ['id', 'meal_type', 'date', 'user_email', 'nutrition_info', 'is_completed']
    list_filter = ['meal_type', 'is_completed', 'date']
    search_fields = ['diet_plan__user__email', 'description']
    date_hierarchy = 'date'
    
    def user_email(self, obj):
        return obj.diet_plan.user.email
    user_email.short_description = 'User'
    
    def nutrition_info(self, obj):
        try:
            n = obj.calculate_nutrition()
            return format_html(
                '<small>Cal:{:.0f} P:{:.0f}g C:{:.0f}g F:{:.0f}g</small>',
                n.get('calories', 0), n.get('protein', 0),
                n.get('carbs', 0), n.get('fat', 0)
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
class ImprovedFoodCategoryAdmin(admin.ModelAdmin):
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
admin.site.unregister(DailyAdvice)  # Remove from main admin
admin.site.unregister(UserFoodPreference)  # Keep in database but hide from admin


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


