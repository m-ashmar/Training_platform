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

from django.utils import timezone
import logging

logger = logging.getLogger(__name__)

from .planner.portion import describe
from .models import (
    FoodItem, FoodCategory, UserFoodPreference, UserFoodCategoryPreference,
    DietPlan, Meal, MealComponent, DietConfig, DailyAdvice, DietPlanTemplate,
    Recipe, RecipeIngredient, UserFoodWeight,
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
    """The food library, curated by hand.

    Everything the planner reads about a food lives here and none of it used to be
    editable: meal slots, role, household unit, cuisine and the review flag were set
    only by `seed_food_units` and could not be corrected without a code change. A food
    with no meal slot is invisible to the planner however good its macros are, so this
    screen is where a new food actually becomes usable.

    `needs_review` is the vetting gate. A food carrying it is visible to clients but
    never planned, which is what keeps the import endpoint from re-creating the 327
    rows of noise the catalogue rebuild removed.
    """
    list_display = [
        'name', 'review_badge', 'category_badge', 'calories_display', 'macro_display',
        'slots_display', 'unit_display', 'cuisine', 'role'
    ]
    list_filter = ['needs_review', 'cuisine', 'role', 'category',
                   MacroDominantFilter, CalorieRangeFilter]
    list_editable = ['cuisine', 'role']
    search_fields = ['name', 'api_id']
    readonly_fields = ['image_preview', 'nutrition_analysis', 'planner_view']

    fieldsets = (
        ('Vetting', {
            'fields': ('needs_review', 'planner_view'),
            'description': ('A food with "needs review" ticked is shown to clients but '
                            'never put in a plan. Untick it once the macros, the meal '
                            'slots and the household unit below are right.')
        }),
        ('Basic Information', {
            'fields': ('name', 'category', 'api_id', 'image_url', 'image_preview')
        }),
        ('Nutrition Per Serving', {
            'fields': ('calories', 'protein', 'carbs', 'fat', 'serving_size', 'serving_size_grams'),
            'description': 'Per 100 g, which is what the whole catalogue uses.'
        }),
        ('How the planner uses it', {
            'fields': ('meal_slots', 'cuisine', 'role', 'household_unit', 'unit_grams'),
            'description': ('meal_slots decides which meals it can appear in — a food '
                            'with none is never planned. household_unit and unit_grams '
                            'are what the client reads ("1½ cups"), and they set the '
                            'portion sizes the engine is allowed to serve.')
        }),
        ('Allergens', {
            'fields': ('allergens', 'allergen_source'),
            'classes': ('collapse',)
        }),
        ('Nutrition Per Gram', {
            'fields': ('calories_per_gram', 'protein_per_gram', 'carbs_per_gram', 'fat_per_gram'),
            'classes': ('collapse',),
            'description': 'Automatically calculated or manually set per-gram values'
        }),
        ('Analysis', {
            'fields': ('nutrition_analysis',),
            'classes': ('collapse',)
        })
    )

    @admin.display(description='vetted', ordering='needs_review')
    def review_badge(self, obj):
        if obj.needs_review:
            return format_html(
                '<span style="background:#b00;color:#fff;padding:2px 7px;'
                'border-radius:3px">needs review</span>')
        return format_html('<span style="color:#080">✓</span>')

    @admin.display(description='meals')
    def slots_display(self, obj):
        slots = obj.meal_slots or []
        if not slots:
            return format_html('<span style="color:#b00" title="never planned">none</span>')
        return ', '.join(s[:2] for s in slots)

    @admin.display(description='serving')
    def unit_display(self, obj):
        if not obj.household_unit or not obj.unit_grams:
            return format_html('<span style="color:#b00">no unit</span>')
        return f"{obj.household_unit} = {obj.unit_grams:g} g"

    @admin.display(description='What the planner sees')
    def planner_view(self, obj):
        """The portions this food can actually be served in, and why."""
        if obj is None or obj.pk is None:
            return 'Save the food first.'
        from .planner.candidates import classify_food
        from .planner.portion import portions_for

        rungs = portions_for(obj)
        ladder = ', '.join(f"{describe(obj, p.grams)} ({p.grams:g} g)" for p in rungs[:8]) or 'none'
        blocked = []
        if obj.needs_review:
            blocked.append('needs review, so it is never planned')
        if not (obj.meal_slots or []):
            blocked.append('no meal slots, so it is never planned')
        note = ('<p style="color:#b00"><b>{}</b></p>'.format('; '.join(blocked))
                if blocked else '')
        return format_html(
            '{}<p>macro slot: <b>{}</b></p><p>servings it can be given in:<br>{}</p>',
            format_html(note), classify_food(obj), ladder)

    @admin.action(description='Approve: allow the planner to serve these')
    def approve_foods(self, request, queryset):
        # Row by row, so `save()` runs. `needs_review` is the flag that SUPPRESSES the
        # nutrition checks (an impossible kcal/g, an Atwater mismatch), so approving
        # with `queryset.update()` let exactly the rows that were flagged for being
        # impossible into the planner's pool without ever re-running those checks —
        # and left them permanently unsaveable through the form afterwards.
        from django.core.exceptions import ValidationError as _Invalid

        missing, refused, approved = [], [], []
        for food in queryset:
            if not (food.meal_slots or []):
                missing.append(food.name)
            food.needs_review = False
            try:
                food.save()
            except _Invalid as exc:
                food.needs_review = True
                refused.append(f"{food.name}: {'; '.join(exc.messages)}")
                continue
            approved.append(food.name)
        n = len(approved)
        if refused:
            self.message_user(
                request,
                "Held back because their nutrition does not check out: "
                + " | ".join(refused[:5]),
                messages.ERROR)
        self.message_user(request, f"{n} food(s) approved.", messages.SUCCESS)
        if missing:
            self.message_user(
                request,
                "Still never planned, because they have no meal slots: "
                + ', '.join(missing[:8]),
                messages.WARNING)

    @admin.action(description='Send back: stop the planner serving these')
    def send_back_for_review(self, request, queryset):
        n = queryset.update(needs_review=True)
        self.message_user(request, f"{n} food(s) held for review.", messages.SUCCESS)

    @admin.action(description='Guess household unit, role and slots from the name')
    def assign_units_and_roles(self, request, queryset):
        """The same patterns `seed_food_units` applies — to the SELECTED foods only.

        This ran the whole command and never looked at `queryset`, so an operator who
        selected one food and used the action reverted every hand-corrected household
        unit, gram weight, portion range and role in the catalogue to the pattern guess.
        This screen exists because those fields could not be corrected by hand; the
        action beside it undid the corrections.
        """
        from diet.management.commands.seed_food_units import role_for, unit_for

        changed = 0
        for food in queryset:
            name = getattr(food, "name_en", None) or food.name or ""
            unit = unit_for(name)
            fields = []
            role = role_for(name)
            if role and food.role != role:
                food.role = role
                fields.append("role")
            if unit:
                for attr, value in zip(
                        ("household_unit", "unit_grams", "min_units", "max_units"), unit):
                    if getattr(food, attr, None) != value:
                        setattr(food, attr, value)
                        fields.append(attr)
            if fields:
                food.save(update_fields=fields)
                changed += 1
        self.message_user(
            request,
            f"Units and roles reassigned for {changed} of {len(queryset)} selected food(s).",
            messages.SUCCESS)
    
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
    
    # No `save_model` override computing per-gram values. `FoodItem.save()` recomputes
    # all four unconditionally from the per-100 g columns, so anything written here was
    # discarded on the next line. Verified: setting `calories_per_gram = 999.0` and
    # saving read back 0.96.
    
    # One list. A second `actions = [...]` further down this class body silently
    # replaced the first, so the vetting actions were defined and unreachable.
    actions = ['approve_foods', 'send_back_for_review', 'assign_units_and_roles',
               'export_as_csv', 'calculate_per_gram_values', 'import_from_edamam']
    
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
        """Recompute the per-gram columns from the per-100 g ones.

        It did that already and then called `save()`, which does the same thing — so the
        action's own arithmetic was overwritten by the model's every time. Kept because
        the button is useful after a bulk import that bypassed `save()`; it just delegates
        to the one place that owns the calculation.
        """
        updated = 0
        for item in queryset:
            item.save()   # FoodItem.save() derives all four
            updated += 1
        self.message_user(request, f"Recalculated per-gram values for {updated} item(s).")
    calculate_per_gram_values.short_description = "Recalculate per-gram values"

    
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
                            # Unvetted, like every other import. An external row
                            # arrives with no meal slots, no household unit and no
                            # cuisine, so the planner must not serve it until a person
                            # has filled those in and approved it.
                            needs_review=True,
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
            
            # By NAME, not by flag-and-luck. `filter(is_protein=True).first()` under
            # `ordering = ['name_en', 'id']` returns whichever protein-flagged category
            # sorts first — "Dairy" ahead of "Proteins", "Morning Carbs" ahead of both —
            # so an imported protein was filed under whatever happened to be alphabetically
            # earliest. The catalogue's own labels are the answer.
            if protein_per_100g >= thresholds['protein'] and protein_per_100g >= carb_per_100g and protein_per_100g >= fat_per_100g:
                category = FoodCategory.objects.filter(name_en="Proteins").first() \
                    or FoodCategory.objects.filter(is_protein=True).order_by("id").first()
            elif carb_per_100g >= thresholds['carb'] and carb_per_100g >= protein_per_100g and carb_per_100g >= fat_per_100g:
                category = FoodCategory.objects.filter(name_en="Carbs").first() \
                    or FoodCategory.objects.filter(is_carb=True).order_by("id").first()
            elif fat_per_100g >= thresholds['fat'] and fat_per_100g >= protein_per_100g and fat_per_100g >= carb_per_100g:
                category = FoodCategory.objects.filter(name_en="Fats").first() \
                    or FoodCategory.objects.filter(is_fat=True).order_by("id").first()
            
            if not category:
                # `get_or_create(name=...)` reads whichever column the ACTIVE language
                # selects, so under a non-English admin session it looked at `name_ar`,
                # missed, and the INSERT violated `UNIQUE (name)` — swallowed by the
                # `except Exception` below, leaving the imported food with no category
                # and no message. Identity is the English column.
                category, _ = FoodCategory.by_canonical_name(
                    'Other', is_protein=False, is_carb=False, is_fat=False)
            
            food_item.category = category
            food_item.save()
        except Exception as e:
            # Silently fail - category assignment is not critical
            # Optional side effect: swallowing this silently is what made the
            # surrounding failures invisible in logs. Control flow is unchanged.
            logger.debug('suppressed non-fatal error', exc_info=True)
    
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
    # `user_link` reads `obj.user.email`, `meal_count` counts meals and
    # `compliance_rate` runs two more aggregates — about four queries per ROW, on a
    # page of a hundred. Fetched in the changelist query instead.
    list_select_related = ['user', 'template']
    
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
        if obj.end_date < timezone.localdate():
            badge = '<span style="background:gray;">Completed</span>'
        elif obj.start_date <= timezone.localdate() <= obj.end_date:
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
    list_select_related = ['diet_plan', 'diet_plan__user']

    def get_queryset(self, request):
        # `nutrition_info` calls `calculate_nutrition()`, which walks the meal's
        # components and dereferences each one's food. Without this the meal
        # changelist cost one query per component per row.
        return super().get_queryset(request).prefetch_related('components__food')
    
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
    # Optional side effect: swallowing this silently is what made the
    # surrounding failures invisible in logs. Control flow is unchanged.
    logger.debug('suppressed non-fatal error', exc_info=True)

try:
    admin.site.unregister(UserFoodPreference)  # Keep in database but hide from admin
except admin.sites.NotRegistered:
    # Optional side effect: swallowing this silently is what made the
    # surrounding failures invisible in logs. Control flow is unchanged.
    logger.debug('suppressed non-fatal error', exc_info=True)


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



class RecipeIngredientInline(admin.TabularInline):
    model = RecipeIngredient
    extra = 1
    autocomplete_fields = ['food']
    fields = ('food', 'grams', 'line_nutrition', 'scalable', 'swap_group', 'note')
    readonly_fields = ('line_nutrition',)
    autocomplete_fields = ('food',)

    @admin.display(description='this line')
    def line_nutrition(self, obj):
        """What this one amount contributes. A gram figure means nothing on its own;
        180 g of rice is 234 kcal and 180 g of lettuce is 27, and the reviewer is
        judging the plate, not the number."""
        if obj is None or obj.pk is None or obj.food is None:
            return '—'
        grams, food = float(obj.grams or 0), obj.food
        k = grams / 100.0
        serving = describe(food, grams)
        return format_html(
            '<b>{}</b> &nbsp; {} kcal &nbsp; <span style="color:#666">P {} · C {} · F {}</span>',
            serving,
            round(float(food.calories or 0) * k),
            round(float(food.protein or 0) * k, 1),
            round(float(food.carbs or 0) * k, 1),
            round(float(food.fat or 0) * k, 1))


@admin.register(Recipe)
class RecipeAdmin(TranslAdmin):
    """The recipe library had no write path: no admin, no endpoint, reachable only by
    the seed command, so sixteen rows were the ceiling on culinary knowledge.

    It is also where the gram amounts get reviewed. 89 of the 105 dishes were written
    to a macro target rather than to a plate, and no test can tell whether 180 g of
    bulgur is what a Syrian kitchen serves. So this screen shows what each dish comes
    to and what each line contributes, and `reviewed` tracks the pass.
    """
    list_display = ('name', 'reviewed_flag', 'cuisine', 'meal_types', 'dish_kcal',
                    'dish_macros', 'line_count', 'prep_minutes', 'is_active')
    list_filter = ('reviewed', 'cuisine', 'is_active')
    list_editable = ('prep_minutes',)
    search_fields = ('name', 'description')
    inlines = [RecipeIngredientInline]
    actions = ['mark_reviewed', 'mark_unreviewed']
    readonly_fields = ('nutrition_panel', 'reviewed_at')
    fieldsets = (
        ('Dish', {'fields': ('name', 'description', 'cuisine', 'meal_types',
                             'prep_minutes', 'instructions', 'image_url', 'is_active')}),
        ('Review', {'fields': ('reviewed', 'reviewed_at', 'nutrition_panel'),
                    'description': 'Check the gram amounts against a real plate, then tick reviewed.'}),
    )

    def get_queryset(self, request):
        return super().get_queryset(request).prefetch_related('ingredients__food')

    @admin.display(description='reviewed', ordering='reviewed', boolean=True)
    def reviewed_flag(self, obj):
        return obj.reviewed

    @admin.display(description='kcal')
    def dish_kcal(self, obj):
        kcal = obj.nutrition()['calories']
        # Red at the ends of the range: a 200 kcal main or a 1,200 kcal snack is a
        # gram amount that slipped, and it is visible from the list without opening it.
        colour = '#b00' if kcal < 150 or kcal > 1100 else '#111'
        return format_html('<b style="color:{}">{}</b>', colour, round(kcal))

    @admin.display(description='P / C / F')
    def dish_macros(self, obj):
        n = obj.nutrition()
        return f"{n['protein']:.0f} / {n['carbs']:.0f} / {n['fat']:.0f}"

    @admin.display(description='lines')
    def line_count(self, obj):
        return len(obj.ingredients.all())

    @admin.display(description='Nutrition of one serving as written')
    def nutrition_panel(self, obj):
        if obj is None or obj.pk is None:
            return 'Save the dish first, then its lines appear here.'
        n = obj.nutrition()
        rows = ''.join(
            '<tr><td style="padding:2px 12px 2px 0">{}</td>'
            '<td style="padding:2px 12px 2px 0">{}</td>'
            '<td style="padding:2px 12px 2px 0">{} g</td>'
            '<td style="padding:2px 12px 2px 0">{} kcal</td></tr>'.format(
                line.food.name, describe(line.food, float(line.grams or 0)),
                round(float(line.grams or 0)),
                round(float(line.food.calories or 0) * float(line.grams or 0) / 100))
            for line in obj.ingredients.all() if line.food)
        return format_html(
            '<table style="border-collapse:collapse">{}</table>'
            '<p style="margin-top:8px"><b>{} kcal</b> &nbsp; protein {} g &nbsp; '
            'carbs {} g &nbsp; fat {} g</p>',
            format_html(rows), n['calories'], n['protein'], n['carbs'], n['fat'])

    @admin.action(description='Mark selected dishes reviewed')
    def mark_reviewed(self, request, queryset):
        n = queryset.update(reviewed=True, reviewed_at=timezone.now())
        self.message_user(request, f"{n} dish(es) marked reviewed.", messages.SUCCESS)

    @admin.action(description='Mark selected dishes NOT reviewed')
    def mark_unreviewed(self, request, queryset):
        n = queryset.update(reviewed=False, reviewed_at=None)
        self.message_user(request, f"{n} dish(es) sent back for review.", messages.SUCCESS)


@admin.register(UserFoodWeight)
class UserFoodWeightAdmin(admin.ModelAdmin):
    list_display = ('user', 'food', 'weight', 'observations', 'updated_at')
    search_fields = ('user__username', 'food__name')
    readonly_fields = ('updated_at',)
