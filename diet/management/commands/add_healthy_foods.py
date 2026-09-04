"""
Management command to populate the database with 100 healthy food items.
All nutritional values are per 100g serving.
"""
from django.core.management.base import BaseCommand
from diet.models import FoodItem, FoodCategory


class Command(BaseCommand):
    help = 'Add 100 healthy food items to the database with real nutritional values'

    def handle(self, *args, **options):
        # First, ensure we have the necessary categories
        categories = {
            'Proteins': FoodCategory.objects.get_or_create(
                name='Proteins', defaults={'is_protein': True, 'is_carb': False, 'is_fat': False}
            )[0],
            'Carbs': FoodCategory.objects.get_or_create(
                name='Carbs', defaults={'is_protein': False, 'is_carb': True, 'is_fat': False}
            )[0],
            'Fats': FoodCategory.objects.get_or_create(
                name='Fats', defaults={'is_protein': False, 'is_carb': False, 'is_fat': True}
            )[0],
            'Vegetables': FoodCategory.objects.get_or_create(
                name='Vegetables', defaults={'is_protein': False, 'is_carb': True, 'is_fat': False}
            )[0],
            'Fruits': FoodCategory.objects.get_or_create(
                name='Fruits', defaults={'is_protein': False, 'is_carb': True, 'is_fat': False}
            )[0],
            'Dairy': FoodCategory.objects.get_or_create(
                name='Dairy', defaults={'is_protein': True, 'is_carb': False, 'is_fat': False}
            )[0],
            'Legumes': FoodCategory.objects.get_or_create(
                name='Legumes', defaults={'is_protein': True, 'is_carb': True, 'is_fat': False}
            )[0],
        }

        # 100 Healthy Food Items with real nutritional values per 100g
        # Format: (name, calories, protein, carbs, fat, category_key)
        foods = [
            # ======= PROTEINS (25 items) =======
            ("Chicken Breast (Grilled)", 165, 31.0, 0.0, 3.6, "Proteins"),
            ("Turkey Breast", 135, 30.0, 0.0, 0.7, "Proteins"),
            ("Lean Beef Sirloin", 183, 28.0, 0.0, 7.0, "Proteins"),
            ("Salmon Fillet", 208, 20.0, 0.0, 13.0, "Proteins"),
            ("Tuna (Fresh)", 144, 29.0, 0.0, 1.0, "Proteins"),
            ("Cod Fillet", 82, 18.0, 0.0, 0.7, "Proteins"),
            ("Shrimp", 99, 24.0, 0.0, 0.3, "Proteins"),
            ("Tilapia", 96, 20.0, 0.0, 1.7, "Proteins"),
            ("Egg White", 52, 11.0, 0.7, 0.2, "Proteins"),
            ("Whole Egg", 155, 13.0, 1.1, 11.0, "Proteins"),
            ("Greek Yogurt (Non-Fat)", 59, 10.0, 3.6, 0.7, "Dairy"),
            ("Cottage Cheese (Low-Fat)", 72, 12.0, 2.7, 1.0, "Dairy"),
            ("Tofu (Firm)", 144, 17.0, 3.0, 8.0, "Proteins"),
            ("Tempeh", 195, 20.0, 8.0, 11.0, "Proteins"),
            ("Seitan", 370, 75.0, 14.0, 2.0, "Proteins"),
            ("Chicken Thigh (Skinless)", 177, 24.5, 0.0, 8.0, "Proteins"),
            ("Pork Tenderloin", 143, 26.0, 0.0, 3.5, "Proteins"),
            ("Duck Breast (Skinless)", 140, 23.5, 0.0, 4.5, "Proteins"),
            ("Lamb Leg (Lean)", 162, 26.0, 0.0, 6.0, "Proteins"),
            ("Venison", 158, 30.0, 0.0, 3.2, "Proteins"),
            ("Bison", 143, 28.0, 0.0, 2.4, "Proteins"),
            ("Sardines (Canned)", 208, 25.0, 0.0, 11.5, "Proteins"),
            ("Mackerel", 205, 19.0, 0.0, 14.0, "Proteins"),
            ("Sea Bass", 97, 18.0, 0.0, 2.0, "Proteins"),
            ("Halibut", 111, 21.0, 0.0, 2.3, "Proteins"),
            
            # ======= CARBOHYDRATES (25 items) =======
            ("Brown Rice (Cooked)", 112, 2.6, 24.0, 0.9, "Carbs"),
            ("White Rice (Cooked)", 130, 2.7, 28.0, 0.3, "Carbs"),
            ("Quinoa (Cooked)", 120, 4.4, 21.0, 1.9, "Carbs"),
            ("Oatmeal (Cooked)", 71, 2.5, 12.0, 1.5, "Carbs"),
            # Dry rolled oats, which is what a recipe means when it says "Oats 60 g".
            # Only the cooked form was here, and at 71 kcal against 389 it is mostly
            # water: four breakfast recipes could not resolve their oats at all and were
            # silently skipped by seed_recipes, which is most of why a fresh install
            # served a named dish at breakfast 17% of the time.
            ("Oats (Rolled, Dry)", 389, 16.9, 66.3, 6.9, "Carbs"),
            ("Sweet Potato (Baked)", 90, 2.0, 21.0, 0.1, "Carbs"),
            ("White Potato (Baked)", 93, 2.5, 21.0, 0.1, "Carbs"),
            ("Whole Wheat Bread", 247, 13.0, 41.0, 3.4, "Carbs"),
            ("Whole Wheat Pasta (Cooked)", 124, 5.0, 25.0, 0.5, "Carbs"),
            ("Bulgur (Cooked)", 83, 3.0, 18.0, 0.2, "Carbs"),
            ("Couscous (Cooked)", 112, 3.8, 23.0, 0.2, "Carbs"),
            ("Barley (Cooked)", 123, 2.3, 28.0, 0.4, "Carbs"),
            ("Buckwheat (Cooked)", 92, 3.4, 20.0, 0.6, "Carbs"),
            ("Corn (Cooked)", 96, 3.4, 21.0, 1.5, "Carbs"),
            ("Pita Bread (Whole Wheat)", 266, 10.0, 55.0, 1.2, "Carbs"),
            ("Basmati Rice (Cooked)", 121, 2.7, 26.0, 0.4, "Carbs"),
            ("Jasmine Rice (Cooked)", 129, 2.5, 28.0, 0.4, "Carbs"),
            ("Millet (Cooked)", 119, 3.5, 23.0, 1.0, "Carbs"),
            ("Farro (Cooked)", 130, 5.0, 26.0, 1.0, "Carbs"),
            ("Polenta (Cooked)", 70, 1.6, 15.0, 0.3, "Carbs"),
            ("Rye Bread", 259, 9.0, 48.0, 3.3, "Carbs"),
            ("Sourdough Bread", 274, 11.0, 51.0, 4.0, "Carbs"),
            ("Tortilla (Whole Wheat)", 316, 9.0, 51.0, 8.0, "Carbs"),
            ("English Muffin (Whole Grain)", 223, 9.0, 44.0, 1.6, "Carbs"),
            ("Bagel (Whole Grain)", 250, 10.0, 48.0, 1.5, "Carbs"),
            ("Pumpernickel Bread", 250, 9.0, 47.0, 3.0, "Carbs"),
            
            # ======= HEALTHY FATS (15 items) =======
            ("Avocado", 160, 2.0, 9.0, 15.0, "Fats"),
            ("Almonds", 579, 21.0, 22.0, 50.0, "Fats"),
            ("Walnuts", 654, 15.0, 14.0, 65.0, "Fats"),
            ("Cashews", 553, 18.0, 30.0, 44.0, "Fats"),
            ("Macadamia Nuts", 718, 8.0, 14.0, 76.0, "Fats"),
            ("Pecans", 691, 9.0, 14.0, 72.0, "Fats"),
            ("Pistachios", 560, 20.0, 28.0, 45.0, "Fats"),
            ("Pumpkin Seeds", 559, 30.0, 11.0, 49.0, "Fats"),
            ("Sunflower Seeds", 584, 21.0, 20.0, 51.0, "Fats"),
            ("Chia Seeds", 486, 17.0, 42.0, 31.0, "Fats"),
            ("Flax Seeds", 534, 18.0, 29.0, 42.0, "Fats"),
            ("Extra Virgin Olive Oil", 884, 0.0, 0.0, 100.0, "Fats"),
            ("Coconut Oil", 892, 0.0, 0.0, 99.0, "Fats"),
            ("Peanut Butter (Natural)", 588, 25.0, 20.0, 50.0, "Fats"),
            ("Almond Butter", 614, 21.0, 19.0, 56.0, "Fats"),
            
            # ======= VEGETABLES (20 items) =======
            ("Broccoli", 34, 2.8, 7.0, 0.4, "Vegetables"),
            ("Spinach", 23, 2.9, 3.6, 0.4, "Vegetables"),
            ("Kale", 35, 2.9, 4.4, 1.5, "Vegetables"),
            ("Asparagus", 20, 2.2, 3.9, 0.1, "Vegetables"),
            ("Brussels Sprouts", 43, 3.4, 9.0, 0.3, "Vegetables"),
            ("Cauliflower", 25, 1.9, 5.0, 0.3, "Vegetables"),
            ("Zucchini", 17, 1.2, 3.1, 0.3, "Vegetables"),
            ("Bell Pepper (Red)", 31, 1.0, 6.0, 0.3, "Vegetables"),
            ("Cucumber", 16, 0.7, 3.6, 0.1, "Vegetables"),
            ("Tomato", 18, 0.9, 3.9, 0.2, "Vegetables"),
            ("Carrot", 41, 0.9, 10.0, 0.2, "Vegetables"),
            ("Green Beans", 31, 1.8, 7.0, 0.1, "Vegetables"),
            ("Eggplant", 25, 1.0, 6.0, 0.2, "Vegetables"),
            ("Mushrooms (White)", 22, 3.1, 3.3, 0.3, "Vegetables"),
            ("Celery", 16, 0.7, 3.0, 0.2, "Vegetables"),
            ("Cabbage", 25, 1.3, 6.0, 0.1, "Vegetables"),
            ("Lettuce (Romaine)", 17, 1.2, 3.3, 0.3, "Vegetables"),
            ("Onion", 40, 1.1, 9.3, 0.1, "Vegetables"),
            ("Garlic", 149, 6.4, 33.0, 0.5, "Vegetables"),
            ("Beet", 43, 1.6, 10.0, 0.2, "Vegetables"),
            
            # ======= FRUITS (10 items) =======
            ("Apple", 52, 0.3, 14.0, 0.2, "Fruits"),
            ("Banana", 89, 1.1, 23.0, 0.3, "Fruits"),
            ("Orange", 47, 0.9, 12.0, 0.1, "Fruits"),
            ("Blueberries", 57, 0.7, 14.0, 0.3, "Fruits"),
            ("Strawberries", 32, 0.7, 8.0, 0.3, "Fruits"),
            ("Mango", 60, 0.8, 15.0, 0.4, "Fruits"),
            ("Pineapple", 50, 0.5, 13.0, 0.1, "Fruits"),
            ("Grapes", 69, 0.7, 18.0, 0.2, "Fruits"),
            ("Watermelon", 30, 0.6, 8.0, 0.2, "Fruits"),
            ("Kiwi", 61, 1.1, 15.0, 0.5, "Fruits"),
            
            # ======= LEGUMES (5 items) =======
            ("Chickpeas (Cooked)", 164, 9.0, 27.0, 2.6, "Legumes"),
            ("Lentils (Cooked)", 116, 9.0, 20.0, 0.4, "Legumes"),
            ("Black Beans (Cooked)", 132, 9.0, 24.0, 0.5, "Legumes"),
            ("Kidney Beans (Cooked)", 127, 9.0, 23.0, 0.5, "Legumes"),
            ("Edamame", 121, 11.0, 9.0, 5.0, "Legumes"),
        ]

        created_count = 0
        updated_count = 0
        
        for name, calories, protein, carbs, fat, category_key in foods:
            # Create unique api_id from name
            api_id = f"healthy_{name.lower().replace(' ', '_').replace('(', '').replace(')', '').replace('-', '_')}"
            
            category = categories.get(category_key)
            
            obj, created = FoodItem.objects.update_or_create(
                api_id=api_id,
                defaults={
                    'name': name,
                    'calories': calories,
                    'protein': protein,
                    'carbs': carbs,
                    'fat': fat,
                    'serving_size': '100g',
                    'serving_size_grams': 100,
                    'category': category,
                }
            )
            
            if created:
                created_count += 1
                self.stdout.write(self.style.SUCCESS(f'Created: {name}'))
            else:
                updated_count += 1
                self.stdout.write(f'Updated: {name}')
        
        self.stdout.write('')
        self.stdout.write(self.style.SUCCESS(f'✅ Complete! Created: {created_count}, Updated: {updated_count}'))
        self.stdout.write(self.style.SUCCESS(f'Total food items in database: {FoodItem.objects.count()}'))
