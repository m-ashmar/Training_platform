
import os
import sys
import django

# Setup Django environment
sys.path.append(os.getcwd())
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'training_platform.settings')
django.setup()

from django.contrib.auth import get_user_model
from django.db import transaction
from diet.services.rule_based_planner import RuleBasedPlanner
from diet.services.diet_persistence import DietPersistenceService
from diet.models import DietPlan

def generate_plan():
    User = get_user_model()
    email = 'oo@gmai.com'
    password = '121212aA'

    try:
        user = User.objects.get(email=email)
        print(f"User {email} already exists.")
        # Update password just in case
        user.set_password(password)
        user.save()
    except User.DoesNotExist:
        print(f"User {email} not found. Creating...")
        username = email.split('@')[0]
        user = User.objects.create_user(username=username, email=email, password=password, phone_number='1234567890')
        # Set some reasonable defaults if missing
        user.fitness_goal = 'Maintain'
        user.gender = 'Male'
        user.weight = 70.0
        user.height = 175.0
        user.age = 30
        user.activity_level = 'Moderate'
        user.save()
        print(f"Created user {email} with defaults.")

    # Calculate daily calories needed
    try:
        daily_kcal = user.calculate_daily_calories()
    except Exception as e:
        print(f"Error calculating calories: {e}. Defaulting to 2000.")
        daily_kcal = 2000.0

    print(f"Generating 7-day plan with {daily_kcal} kcal/day for {user.email}...")

    # setup default allowed foods to pass persistence validation
    from diet.models import FoodItem, UserFoodCategoryPreference
    
    # Common foods likely to be picked by planner, create if missing
    
    # 100g raw/standard values
    NEW_FOODS = {
        # Protein
        "Turkey Bacon": {"kcal": 380, "p": 30, "c": 1, "f": 28, "cat": "Protein"},
        "Smoked Salmon": {"kcal": 117, "p": 18, "c": 0, "f": 4, "cat": "Protein"},
        "Seitan": {"kcal": 370, "p": 75, "c": 14, "f": 2, "cat": "Protein"}, # Wheat Gluten
        "Lean Beef": {"kcal": 250, "p": 26, "c": 0, "f": 15, "cat": "Protein"},
        "Cottage Cheese": {"kcal": 98, "p": 11, "c": 3.4, "f": 4.3, "cat": "Protein"},
        "Beef Jerky": {"kcal": 410, "p": 33, "c": 11, "f": 26, "cat": "Protein"},
        "Protein Shake": {"kcal": 380, "p": 75, "c": 10, "f": 3, "cat": "Protein"}, # Powder approximation

        # Carbs
        "Bagel": {"kcal": 250, "p": 10, "c": 49, "f": 1, "cat": "Carbohydrates"},
        "English Muffin": {"kcal": 235, "p": 8, "c": 46, "f": 1, "cat": "Carbohydrates"},
        "Bran Flakes": {"kcal": 360, "p": 10, "c": 77, "f": 2, "cat": "Carbohydrates"},
        "Corn Flakes": {"kcal": 370, "p": 7, "c": 84, "f": 0.4, "cat": "Carbohydrates"},
        "Granola": {"kcal": 471, "p": 10, "c": 64, "f": 20, "cat": "Carbohydrates"},
        "Couscous": {"kcal": 376, "p": 13, "c": 77, "f": 1, "cat": "Carbohydrates"}, # Raw
        "Lentils": {"kcal": 353, "p": 25, "c": 60, "f": 1, "cat": "Carbohydrates"}, # Dry
        "Butternut Squash": {"kcal": 45, "p": 1, "c": 12, "f": 0.1, "cat": "Carbohydrates"},
        "Rice Cake": {"kcal": 387, "p": 8, "c": 82, "f": 3, "cat": "Carbohydrates"},
        "Granola Bar": {"kcal": 450, "p": 10, "c": 65, "f": 18, "cat": "Carbohydrates"},
        "Crackers": {"kcal": 420, "p": 8, "c": 65, "f": 15, "cat": "Carbohydrates"},

        # Fats
        "Flaxseed Oil": {"kcal": 884, "p": 0, "c": 0, "f": 100, "cat": "Fats"},
        "Sesame Oil": {"kcal": 884, "p": 0, "c": 0, "f": 100, "cat": "Fats"},
        "Feta Cheese": {"kcal": 264, "p": 14, "c": 4, "f": 21, "cat": "Fats"},
        "Pistachios": {"kcal": 560, "p": 20, "c": 27, "f": 45, "cat": "Fats"},
        "Dark Chocolate": {"kcal": 546, "p": 4.9, "c": 61, "f": 31, "cat": "Fats"},

        # Fruits
        "Raspberries": {"kcal": 52, "p": 1.2, "c": 12, "f": 0.7, "cat": "Fruits"},
        "Blackberries": {"kcal": 43, "p": 1.4, "c": 10, "f": 0.5, "cat": "Fruits"},
        "Melon": {"kcal": 34, "p": 0.8, "c": 8, "f": 0.2, "cat": "Fruits"}, # Cantaloupe
        "Cantaloupe": {"kcal": 34, "p": 0.8, "c": 8, "f": 0.2, "cat": "Fruits"},
        "Honeydew": {"kcal": 36, "p": 0.5, "c": 9, "f": 0.1, "cat": "Fruits"},
        "Plum": {"kcal": 46, "p": 0.7, "c": 11, "f": 0.3, "cat": "Fruits"},
        "Apricot": {"kcal": 48, "p": 1.4, "c": 11, "f": 0.4, "cat": "Fruits"},
        "Kiwi": {"kcal": 61, "p": 1.1, "c": 15, "f": 0.5, "cat": "Fruits"},
        "Grapefruit": {"kcal": 42, "p": 0.8, "c": 11, "f": 0.1, "cat": "Fruits"},
        "Raisins": {"kcal": 299, "p": 3, "c": 79, "f": 0.5, "cat": "Fruits"},
        "Longans, Dried": {"kcal": 286, "p": 5, "c": 74, "f": 0.4, "cat": "Fruits"},
    }

    from diet.models import FoodCategory

    # helper to ensure food exists (if not, skipping for now as planner found them) and add pref
    def add_pref(food_name, meals, macro):
        # Case insensitive lookup
        food = FoodItem.objects.filter(name__iexact=food_name).first()
        
        if not food:
            # Check if we have data to create it
            if food_name in NEW_FOODS:
                data = NEW_FOODS[food_name]
                print(f"Creating missing food: {food_name}")
                # Get valid category
                cat_obj = FoodCategory.objects.filter(name__iexact=data["cat"]).first()
                if not cat_obj and data["cat"] == "Carbohydrates":
                     cat_obj = FoodCategory.objects.filter(name__iexact="Carbs").first()
                
                food = FoodItem.objects.create(
                    api_id=f"custom_{food_name.lower().replace(' ', '_')}",
                    name=food_name,
                    calories=data["kcal"],
                    protein=data["p"],
                    carbs=data["c"],
                    fat=data["f"],
                    category=cat_obj,
                    serving_size="100g",
                    serving_size_grams=100
                )
            else:
                print(f"Warning: Food '{food_name}' not found in DB and no NEW_FOODS data, skipping preference.")
                return

        for meal in meals:
            # Check if exists
            if not UserFoodCategoryPreference.objects.filter(user=user, food=food, meal=meal, macro=macro).exists():
                UserFoodCategoryPreference.objects.update_or_create(
                user=user, 
                food=food, 
                defaults={'meal': meal, 'macro': macro}
            )
                print(f"Added preference: {food.name} -> {meal} {macro}")

    # Common foods likely to be picked by planner
    # --- EXTENDED PREFERENCES (4-5 items per meal/macro) ---

    # --- EXTENDED PREFERENCES (Broadened for Planner Flexibility) ---

    all_meals = ["Breakfast", "Lunch", "Dinner", "Snack"]
    main_meals = ["Breakfast", "Lunch", "Dinner"]
    lunch_dinner = ["Lunch", "Dinner"]

    # --- EXTENDED PREFERENCES (DISTINCT per meal to avoid Unique Constraint Overwrites) ---

    # BREAKFAST
    add_pref("Egg", ["Breakfast"], "protein")
    add_pref("Greek Yogurt", ["Breakfast"], "protein")
    add_pref("Turkey Bacon", ["Breakfast"], "protein")
    add_pref("Smoked Salmon", ["Breakfast"], "protein")
    add_pref("Cottage Cheese", ["Breakfast"], "protein")

    add_pref("Oats", ["Breakfast"], "carb")
    add_pref("Whole Wheat Bread", ["Breakfast"], "carb")
    add_pref("Granola", ["Breakfast"], "carb")
    add_pref("Bagel", ["Breakfast"], "carb")
    add_pref("English Muffin", ["Breakfast"], "carb")
    add_pref("Bran Flakes", ["Breakfast"], "carb")
    add_pref("Corn Flakes", ["Breakfast"], "carb")
    add_pref("Blueberries", ["Breakfast"], "carb") # Use as carb source
    add_pref("Banana", ["Breakfast"], "carb")

    add_pref("Avocado", ["Breakfast"], "fat")
    add_pref("Peanut Butter", ["Breakfast"], "fat")
    add_pref("Chia Seeds", ["Breakfast"], "fat")

    add_pref("Tomato", ["Breakfast"], "vegetable")
    add_pref("Spinach", ["Breakfast"], "vegetable")
    add_pref("Mushrooms", ["Breakfast"], "vegetable")

    # LUNCH (Distinct from Dinner)
    add_pref("Chicken Breast", ["Lunch"], "protein")
    add_pref("Seitan", ["Lunch"], "protein")
    add_pref("Lean Beef", ["Lunch"], "protein")
    add_pref("Tofu", ["Lunch"], "protein")
    add_pref("Turkey Breast", ["Lunch"], "protein")

    add_pref("Carnaroli Rice", ["Lunch"], "carb")
    add_pref("Brown Rice", ["Lunch"], "carb")
    add_pref("Quinoa", ["Lunch"], "carb")
    add_pref("Pasta", ["Lunch"], "carb")
    add_pref("Sweet Potato", ["Lunch"], "carb")

    add_pref("Olive Oil", ["Lunch"], "fat")
    add_pref("Cheese", ["Lunch"], "fat")
    add_pref("Hummus", ["Lunch"], "fat")
    add_pref("Sunflower Seeds", ["Lunch"], "fat")

    add_pref("Bell Pepper", ["Lunch"], "vegetable")
    add_pref("Carrot", ["Lunch"], "vegetable")
    add_pref("Cucumber", ["Lunch"], "vegetable")
    add_pref("Lettuce", ["Lunch"], "vegetable")
    add_pref("Zucchini", ["Lunch"], "vegetable")

    # DINNER (Distinct items where possible)
    add_pref("Cod", ["Dinner"], "protein")
    add_pref("Salmon", ["Dinner"], "protein")
    add_pref("Tuna", ["Dinner"], "protein")
    add_pref("Tilapia", ["Dinner"], "protein")
    add_pref("Shrimp", ["Dinner"], "protein")
    # Add a fallback common protein to Dinner just in case
    add_pref("Chicken Thigh", ["Dinner"], "protein")
    add_pref("Steak", ["Dinner"], "protein")

    add_pref("Arborio Rice", ["Dinner"], "carb")
    add_pref("Couscous", ["Dinner"], "carb")
    add_pref("Potato", ["Dinner"], "carb")
    add_pref("Butternut Squash", ["Dinner"], "carb")
    add_pref("Lentils", ["Dinner"], "carb")
    add_pref("White Rice", ["Dinner"], "carb") # Moved White Rice to Dinner explicitly
    # Add White Rice to Dinner as fallback for Lunch overlap issues? 
    # Actually if I add White Rice here, it's Dinner only.

    add_pref("Flaxseed Oil", ["Dinner"], "fat")
    add_pref("Sesame Oil", ["Dinner"], "fat")
    add_pref("Feta Cheese", ["Dinner"], "fat")
    add_pref("Pumpkin Seeds", ["Dinner"], "fat")
    add_pref("Cashews", ["Dinner"], "fat")

    add_pref("Broccoli", ["Dinner"], "vegetable")
    add_pref("Asparagus", ["Dinner"], "vegetable")
    add_pref("Cauliflower", ["Dinner"], "vegetable")
    add_pref("Green Bean", ["Dinner"], "vegetable")
    add_pref("Kale", ["Dinner"], "vegetable")

    # SNACK
    add_pref("Protein Shake", ["Snack"], "protein")
    add_pref("Hard Boiled Egg", ["Snack"], "protein")
    add_pref("Beef Jerky", ["Snack"], "protein")
    # Greek Yogurt and Cottage Cheese used in Breakfast, so avoid overwriting if possible
    # But if user wants Yogurt as Snack, they lose it for Breakfast.
    # I'll assign "Yogurt Parfait" or similar if exists, or just accept the move.
    # Let's check overlap:
    # Egg (Breakfast) vs Hard Boiled Egg (Snack) -> Distinct names? Probably "Egg" and "Hard Boiled Egg" are different items? Or same?
    # If same "Egg", it moves to Snack. I'll rely on "Hard Boiled Egg" being distinct or just accept Egg is snack.

    add_pref("Apple", ["Snack"], "carb")
    add_pref("Rice Cake", ["Snack"], "carb")
    add_pref("Granola Bar", ["Snack"], "carb")
    add_pref("Crackers", ["Snack"], "carb")

    add_pref("Almonds", ["Snack"], "fat")
    add_pref("Walnuts", ["Snack"], "fat")
    add_pref("Dark Chocolate", ["Snack"], "fat")
    add_pref("Pistachios", ["Snack"], "fat")
    # Peanut Butter used in Breakfast.

    # FRUITS
    # Fruits are tricky because RuleBasedPlanner adds them to meals.
    # If I assign Banana to Breakfast, and Planner puts it in Snack...
    # FRUITS (DISTINCT per meal to avoid overwrites)
    # Breakfast Fruits (Need 4-5 to survive recency)
    add_pref("Banana", ["Breakfast"], "fruit")
    add_pref("Blueberries", ["Breakfast"], "fruit")
    add_pref("Strawberries", ["Breakfast"], "fruit")
    add_pref("Raspberries", ["Breakfast"], "fruit")
    add_pref("Blackberries", ["Breakfast"], "fruit")
    add_pref("Melon", ["Breakfast"], "fruit")
    
    # Lunch Fruits
    add_pref("Apple", ["Lunch"], "fruit")
    add_pref("Pear", ["Lunch"], "fruit")
    add_pref("Peach", ["Lunch"], "fruit")
    add_pref("Plum", ["Lunch"], "fruit")
    add_pref("Apricot", ["Lunch"], "fruit")

    # Dinner Fruits
    add_pref("Grapes", ["Dinner"], "fruit")
    add_pref("Watermelon", ["Dinner"], "fruit")
    add_pref("Pineapple", ["Dinner"], "fruit")
    add_pref("Cantaloupe", ["Dinner"], "fruit")
    add_pref("Honeydew", ["Dinner"], "fruit")

    # Snack Fruits
    # Note: Added unique ones here to avoid conflict with above
    add_pref("Orange", ["Snack"], "fruit")
    add_pref("Mango", ["Snack"], "fruit")
    add_pref("Kiwi", ["Snack"], "fruit")
    add_pref("Longans, Dried", ["Snack"], "fruit")
    add_pref("Pepeao, Dried", ["Snack"], "fruit") # Fallback observed
    # Add common fallbacks to Snack just in case
    add_pref("Grapefruit", ["Snack"], "fruit")
    
    # Add missing fallbacks mentioned in error
    add_pref("Chicken Breast (Grilled)", ["Lunch"], "protein")
    # Dried fruits likely Snack
    add_pref("Raisins", ["Snack"], "fruit")

    planner = RuleBasedPlanner(user)
    try:
        # Generate plan output
        output = planner.generate(
            daily_kcal=daily_kcal,
            meal_count=3,
            snack_count=1,
            duration_days=14,
            no_repeat_days=2
        )
        
        # Persist plan
        persistence = DietPersistenceService(user)
        plan = persistence.save_plan(
            plan_output=output,
            meal_count=3,
            snack_count=1
        )
        print(f"Successfully generated and saved DietPlan ID {plan.id} for 7 days.")
        print(f"Plan Start Date: {plan.start_date}")
        print(f"Plan End Date: {plan.end_date}")
        
    except Exception as e:
        print(f"Failed to generate plan: {e}")
        import traceback
        traceback.print_exc()

if __name__ == '__main__':
    generate_plan()
