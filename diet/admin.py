# diet/admin.py
from django.contrib import admin
from django import forms
from django.conf import settings
from django.http import HttpResponseRedirect
from django.urls import path
from django.shortcuts import render
from .models import FoodItem, UserFoodPreference, Meal, DietPlan,FoodCategory , MealComponent , DailyAdvice
import requests
import json
from django.contrib import messages
from .models import FoodItem
from django.utils.html import format_html
from datetime import date , timedelta
from django.shortcuts import redirect
from diet.services import DietOptimizer
# diet/admin.py







class FoodSearchForm(forms.Form):
    search_query = forms.CharField(label='Search Food')


class EdamamImportForm(forms.Form):
    query = forms.CharField(label="Search Food", max_length=100, required=True)

        
@admin.register(FoodItem)
class FoodItemAdmin(admin.ModelAdmin):
    

    actions = ['import_from_edamam']
    search_fields = ['name']
    list_display = ['name', 'calories' , 'category' ,'meal_time_filter', 'image_thumbnail' , 'protein', 'carbs', 'fat' ,'display_serving']
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
        (None, {
            'fields': ('api_id', 'name', 'image_url', 'image_preview')
        }),
        ('Nutrition', {
            'fields': ('calories', 'protein', 'carbs', 'fat')
        }),
    )
    
    
    # Custom view for Edamam search
    def get_urls(self):
        urls = super().get_urls()
        custom_urls = [
            path('import-edamam/', 
                self.admin_site.admin_view(self.import_food_from_edamam),
                name='diet_fooditem_import_edamam'
            ),
        ]
        return custom_urls + urls
    
       
    def auto_assign_category(self, food_item):
        """Improved category assignment with fallback"""
        try:
            # Calculate per 100g values
            serving_grams = food_item.serving_size_grams
            if serving_grams <= 0:
                serving_grams = 100  # Prevent division by zero

            protein_per_100g = (food_item.protein / serving_grams) * 100
            carb_per_100g = (food_item.carbs / serving_grams) * 100
            fat_per_100g = (food_item.fat / serving_grams) * 100

            # Debug print
            print(f"\nCategory Assignment for {food_item.name}:")
            print(f"Protein: {protein_per_100g}g/100g | Carbs: {carb_per_100g}g/100g | Fat: {fat_per_100g}g/100g")

            # Thresholds (adjust these as needed)
            thresholds = {
                'protein': 10,  # At least 10g/100g to be considered protein source
                'carb': 15,     # At least 15g/100g for carbs
                'fat': 5        # At least 5g/100g for fats
            }

            # Determine primary category
            category = None
            if protein_per_100g >= thresholds['protein'] and protein_per_100g >= carb_per_100g and protein_per_100g >= fat_per_100g:
                category = FoodCategory.objects.filter(is_protein=True).first()
                print(f"Assigned Protein category: {category}")
            elif carb_per_100g >= thresholds['carb'] and carb_per_100g >= protein_per_100g and carb_per_100g >= fat_per_100g:
                category = FoodCategory.objects.filter(is_carb=True).first()
                print(f"Assigned Carb category: {category}")
            elif fat_per_100g >= thresholds['fat'] and fat_per_100g >= protein_per_100g and fat_per_100g >= carb_per_100g:
                category = FoodCategory.objects.filter(is_fat=True).first()
                print(f"Assigned Fat category: {category}")

            # Fallback to 'Other' category
            if not category:
                category, created = FoodCategory.objects.get_or_create(
                    name='Other',
                    defaults={'is_protein': False, 'is_carb': False, 'is_fat': False}
                )
                print(f"Assigned Other category (created: {created})")

            food_item.category = category
            food_item.save()

        except Exception as e:
            print(f"Error assigning category: {str(e)}")
            raise
            
            


    def import_food_from_edamam(self, request):
        selected_items_json = request.POST.getlist("selected_items")
        if request.method == "POST":
            action = request.POST.get("action")
            
            print(f"Action: {action}")

            if action == "search":
                form = EdamamImportForm(request.POST)
                if form.is_valid():
                    query = form.cleaned_data["query"]
                    print(f"Searching Edamam for query: {query}")
                    results = self.search_edamam(query)
                   # print(f"Search results: {results}")
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

                for item_id_str in selected_items_ids:
                    try:
                        index = int(item_id_str) - 1
                        item = all_results[index]
                        print(f"Importing item at index {index}: {item}")
                    except (ValueError, IndexError):
                        print(f"Skipping invalid index: {item_id_str}")
                        continue

                    # Normalize structure: edamam format or custom flattened
                    food_data = item.get("food") or item
                    measures = item.get("measures", [])
                    nutrients = food_data.get("nutrients", {})

                    api_id = food_data.get("foodId") or item.get("foodId")
                    name = food_data.get("label") or item.get("name", "")
                    image = food_data.get("image") or item.get("image", "")

                    print(f"API ID: {api_id}")
                    if not api_id:
                        print("Skipping item with missing API ID")
                        continue

                    if FoodItem.objects.filter(api_id=api_id).exists():
                        print(f"FoodItem with API ID {api_id} already exists. Skipping.")
                        continue

                    # Extract nutrients from either 'nutrients' or flat keys
                    calories = nutrients.get("ENERC_KCAL") or item.get("calories")
                    protein = nutrients.get("PROCNT") or item.get("protein")
                    carbs = nutrients.get("CHOCDF") or item.get("carbs")
                    fat = nutrients.get("FAT") or item.get("fat")

                    if None in (calories, protein, carbs, fat):
                        print(f"Skipping item with missing nutrients: {item}")
                        continue

                    # Serving info
                    serving_size_label = "Gram"
                    serving_size_grams = 100
                    if measures:
                        serving = measures[0]
                        serving_size_label = serving.get("label", "Gram")
                        serving_size_grams = int(serving.get("weight", 100))
                        raw_weight = serving.get("weight", 100)
                        serving_size_grams = self._calculate_serving_size(raw_weight, serving_size_label)
                        print(f"calculated grams: {serving_size_grams}")
                        print(f"Serving size: {serving_size_label}, grams: {serving_size_grams}")

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
                    print(f"Created FoodItem: {food}")
                    self.auto_assign_category(food)
                    imported_count += 1

                if imported_count > 0:
                    messages.success(request, f"Imported {imported_count} items!")
                else:
                    messages.warning(request, "No valid items selected")

                return redirect('admin:diet_fooditem_import_edamam')
        return render(request, "admin/diet/fooditem/import_form.html", {
            'form': EdamamImportForm(),
            'results': request.session.get("edamam_results", []),
            'title': 'Import from Edamam'
        })            

   

    def _calculate_serving_size(self, weight, label):
        """Convert Edamam measures to grams"""
    # Handle common serving types
        label = str(label).lower().strip()
        weight = float(weight or 0)
        if 'cup' in label:
            print('cup')
            return 240  # Standard cup ≈ 240g
        if 'tablespoon' in label:
            print('cup1')
            return 15   # 1 tbsp ≈ 15g
        if 'teaspoon' in label:
            print('cup2')
            return 5    # 1 tsp ≈ 5g
        if 'egg' in label:
            print('cup3')
            return 50   # 1 large egg ≈ 50g        
        if 'slice' in label and 'bread' in label:
            print('cup4')
            return 30   # 1 bread slice ≈ 30g
            # Fallback to Edamam's weight if available
        if 'serving' in label:  # Generic serving fallback
            return max(weight, 100) 
    
        return weight if weight > 0 else 100 
            
       

    def search_edamam(self, query):
        import requests
        url = "https://api.edamam.com/api/food-database/v2/parser"
        params = {
            'app_id': settings.EDAMAM_APP_ID,
            'app_key': settings.EDAMAM_APP_KEY,
            'ingr': query
        }
        response = requests.get(url, params=params)
        data = response.json()
        import pprint
        pprint.pprint(data)  # Pretty print for better structure readability in console
        
        results = []
        for item in data.get('hints', []):
            food = item.get('food', {})
            nutrients = food.get('nutrients', {})

            results.append({
                'foodId': food.get("foodId"),
                'image': food.get('image', ''),
                'name': food.get('label', 'Unnamed'),
                'calories': nutrients.get('ENERC_KCAL', 0),
                'protein': nutrients.get('PROCNT', 0),
                'carbs': nutrients.get('CHOCDF', 0),
                'fat': nutrients.get('FAT', 0),
                'image': food.get('image', ''),
                'foodId': food.get('foodId'),
                'measures': item.get('measures', [])
        })

        return results
       
    

        return response.json().get('hints', [])[:10]  # First 10 results

    def import_from_edamam(self, request, queryset):
        # Redirect to custom import view
        return HttpResponseRedirect("import-edamam/")
    
    import_from_edamam.short_description = "Import food items from Edamam"

# Rest of your admin models remain the same...

    
    
    
    
    

class MealComponentInline(admin.TabularInline):
    model = MealComponent
    extra = 1
    fields = ('food', 'quantity', 'meal_time')

@admin.register(Meal)
class MealAdmin(admin.ModelAdmin):
    inlines = [MealComponentInline]
    list_display = ['template', 'date', 'get_calories']
    
    def get_calories(self, obj):
        return sum(c.food.calories * c.quantity for c in obj.mealcomponent_set.all())
    
    
class MealInline(admin.TabularInline):
    model = Meal
    extra = 0  # This will show no empty rows by default
    fields = ('template', 'date')
    # Customize fields as needed
    
    
@admin.register(DietPlan)
class DietPlanAdmin(admin.ModelAdmin):
    actions = ['generate_optimized_plan' , 'view_optimization_report']
    inlines = [MealInline]
    
    
    def view_optimization_report(self, request, queryset):
        plan = queryset.first()
        optimizer = DietOptimizer(plan.user)
        report = optimizer.get_optimization_report()  # New method
        return render(request, 'admin/diet_optim_report.html', {'report': report})
    
    def generate_optimized_plan(self, request, queryset):
        success = 0
        errors = 0
        
        for plan in queryset:
            try:
                plan.user.generate_diet_plan(plan.goal)
                success += 1
            except Exception as e:
                messages.error(request, f"Failed for {plan.user}: {str(e)}")
                errors += 1
        
        self.message_user(request, 
            f"Successfully generated {success} plans, {errors} failures",
            level=messages.SUCCESS if success > 0 else messages.ERROR
        )
    
@admin.register(UserFoodPreference)
class UserFoodPreferenceAdmin(admin.ModelAdmin):
    filter_horizontal = ['protein_choices', 'carb_choices', 'fat_choices']
    list_display = ['user', 'get_protein_count', 'get_carb_count', 'get_fat_count']
    actions = ['generate_diet_plan', 'generate_with_ai']
    
    
    def generate_ai_plan(self, request, queryset):
        from .tasks import generate_ai_diet_plan
        for pref in queryset:
            generate_ai_diet_plan.delay(pref.user.id)
        self.message_user(request, f"Started AI plan generation for {queryset.count()} users")


    def get_protein_count(self, obj):
        return obj.protein_choices.count()

    def get_carb_count(self, obj):
        return obj.carb_choices.count()

    def get_fat_count(self, obj):
        return obj.fat_choices.count()

    def generate_diet_plan(self, request, queryset):
        created_count = 0
        for preference in queryset:
            user = preference.user
            try:
                plan = user.generate_diet_plan(goal='Maintain')  # Call your model method
                if plan:
                    created_count += 1
            except Exception as e:
                messages.warning(request, f"Error generating plan for {user}: {str(e)}")

        if created_count:
            self.message_user(request, f"Successfully generated {created_count} diet plan(s).", level=messages.SUCCESS)
        else:
            self.message_user(request, "No diet plans were created.", level=messages.WARNING)
        return redirect('/admin/diet/userfoodpreference/')

    generate_diet_plan.short_description = "Generate Diet Plan"


@admin.register(FoodCategory)    
class FoodCategoryAdmin(admin.ModelAdmin):
    list_display = ['name', 'meal_times', 'is_protein', 'is_carb', 'is_fat']
    list_editable = ['meal_times', 'is_protein', 'is_carb', 'is_fat']
    list_filter = ['meal_times']    
@admin.register(DailyAdvice)
class DailyAdviceAdmin(admin.ModelAdmin):
    list_display = ['user', 'generated_at', 'text']
    readonly_fields = ['context_data']