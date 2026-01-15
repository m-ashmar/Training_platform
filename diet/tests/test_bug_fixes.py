"""
Test suite for validating bug fixes in diet plan generation.
Covers:
- Duplicate items within meals
- Per-item cap violations after scaling
- Macro density detection issues
- Unrealistic vegetable quantities
- Persistence merging issues
"""

from django.test import TestCase, TransactionTestCase
from django.contrib.auth import get_user_model
from django.utils import timezone
from unittest.mock import Mock, patch, MagicMock
from datetime import date, timedelta
import json

from diet.models import (
    DietPlan, Meal, MealComponent, FoodItem, FoodCategory,
    UserFoodCategoryPreference, DietConfig
)
from diet.services.rule_based_planner import RuleBasedPlanner
from diet.services.macro_balancer import MacroBalancer
from diet.services.macro_shortage_booster import MacroShortageBooster
from diet.services.macro_cap_enforcer import MacroCapEnforcer
from diet.services.meal_plan_factory import MealPlanFactory
from diet.ai_services import DietGenerator
from diet.ai_models import DietPlanOutput, AIMeal, AIIngredient


User = get_user_model()


class TestDuplicateItemsInMeals(TransactionTestCase):
    """Test cases for duplicate item prevention within single meals."""
    
    def setUp(self):
        self.user = User.objects.create_user(
            username='testuser',
            email='test@test.com',
            password='testpass123',
            phone_number='+1234567890'
        )
        self.user.fitness_goal = 'Maintain'
        self.user.save()
        
        # Create food categories
        self.protein_cat = FoodCategory.objects.create(
            name='Protein', is_protein=True
        )
        self.carb_cat = FoodCategory.objects.create(
            name='Carbs', is_carb=True
        )
        self.fat_cat = FoodCategory.objects.create(
            name='Fats', is_fat=True
        )
        self.veg_cat = FoodCategory.objects.create(
            name='Vegetables', is_carb=True
        )
        
        # Create food items
        self.egg_whites = FoodItem.objects.create(
            name='Egg Whites',
            category=self.protein_cat,
            calories_per_gram=0.52,
            protein_per_gram=0.11,
            carbs_per_gram=0.007,
            fat_per_gram=0.002,
            serving_size_grams=100
        )
        
        self.chicken = FoodItem.objects.create(
            name='Chicken Breast',
            category=self.protein_cat,
            calories_per_gram=1.65,
            protein_per_gram=0.31,
            carbs_per_gram=0,
            fat_per_gram=0.036
        )
        
        self.rice = FoodItem.objects.create(
            name='White Rice',
            category=self.carb_cat,
            calories_per_gram=1.3,
            protein_per_gram=0.027,
            carbs_per_gram=0.28,
            fat_per_gram=0.003
        )
        
        self.sweet_potato = FoodItem.objects.create(
            name='Sweet Potato',
            category=self.carb_cat,
            calories_per_gram=0.86,
            protein_per_gram=0.016,
            carbs_per_gram=0.20,
            fat_per_gram=0.0005
        )
        
        self.olive_oil = FoodItem.objects.create(
            name='Olive Oil',
            category=self.fat_cat,
            calories_per_gram=8.84,
            protein_per_gram=0,
            carbs_per_gram=0,
            fat_per_gram=1.0
        )
        
        # Create user preferences
        for meal in ['Breakfast', 'Lunch', 'Dinner']:
            UserFoodCategoryPreference.objects.create(
                user=self.user, meal=meal, macro='protein',
                food=self.egg_whites
            )
            UserFoodCategoryPreference.objects.create(
                user=self.user, meal=meal, macro='protein',
                food=self.chicken
            )
            UserFoodCategoryPreference.objects.create(
                user=self.user, meal=meal, macro='carb',
                food=self.rice
            )
            UserFoodCategoryPreference.objects.create(
                user=self.user, meal=meal, macro='carb',
                food=self.sweet_potato
            )
            UserFoodCategoryPreference.objects.create(
                user=self.user, meal=meal, macro='fat',
                food=self.olive_oil
            )
    
    @patch('diet.models.User.calculate_daily_calories')
    def test_no_duplicate_items_within_single_meal(self, mock_calories):
        """Test that the same food item cannot appear twice in one meal."""
        mock_calories.return_value = 2500
        
        planner = RuleBasedPlanner(self.user)
        plan_output = planner.generate(
            daily_kcal=2500,
            meal_count=3,
            snack_count=0,
            duration_days=1,
            no_repeat_days=3
        )
        
        # Check each meal for duplicates
        for meal in plan_output.plan:
            food_names = [ing.name for ing in meal.ingredients]
            # No food should appear more than once
            self.assertEqual(len(food_names), len(set(food_names)),
                           f"Duplicate items found in {meal.meal_name}: {food_names}")
            
            # Specifically check for the Egg Whites duplication issue
            egg_count = sum(1 for name in food_names if 'Egg' in name)
            self.assertLessEqual(egg_count, 1, 
                               f"Multiple egg items in {meal.meal_name}: {food_names}")
    
    @patch('diet.models.User.calculate_daily_calories')
    def test_one_protein_per_meal_rule(self, mock_calories):
        """Test that only one protein item appears per meal."""
        mock_calories.return_value = 2500
        
        planner = RuleBasedPlanner(self.user)
        plan_output = planner.generate(
            daily_kcal=2500,
            meal_count=3,
            snack_count=0,
            duration_days=1
        )
        
        for meal in plan_output.plan:
            if meal.meal_name == 'Snack':
                continue
                
            protein_items = []
            for ing in meal.ingredients:
                # Get the food item from DB to check its category
                try:
                    food = FoodItem.objects.get(name=ing.name)
                    if food.category and food.category.is_protein:
                        protein_items.append(ing.name)
                except FoodItem.DoesNotExist:
                    pass
            
            self.assertLessEqual(len(protein_items), 1,
                               f"Multiple protein items in {meal.meal_name}: {protein_items}")
    
    @patch('diet.models.User.calculate_daily_calories')
    @patch('django.conf.settings.DIET_STAGED_MEAL_FILL', True)
    def test_staged_fill_no_duplicates(self, mock_calories):
        """Test that staged fill doesn't create duplicates."""
        mock_calories.return_value = 2500
        
        planner = RuleBasedPlanner(self.user)
        plan_output = planner.generate(
            daily_kcal=2500,
            meal_count=3,
            snack_count=0,
            duration_days=1
        )
        
        for meal in plan_output.plan:
            food_names = [ing.name for ing in meal.ingredients]
            self.assertEqual(len(food_names), len(set(food_names)),
                           f"Staged fill created duplicates in {meal.meal_name}: {food_names}")


class TestPerItemCapAfterScaling(TestCase):
    """Test cases for per-item quantity caps after scaling operations."""
    
    def setUp(self):
        self.user = User.objects.create_user(
            username='testuser',
            email='test@test.com',
            phone_number='+1234567890'
        )
        
        # Create categories and foods
        self.protein_cat = FoodCategory.objects.create(
            name='Protein', is_protein=True
        )
        self.carb_cat = FoodCategory.objects.create(
            name='Carbs', is_carb=True
        )
        
        self.chicken = FoodItem.objects.create(
            name='Chicken Breast',
            category=self.protein_cat,
            calories_per_gram=1.65,
            protein_per_gram=0.31,
            carbs_per_gram=0,
            fat_per_gram=0.036
        )
        
        self.pasta = FoodItem.objects.create(
            name='Pasta',
            category=self.carb_cat,
            calories_per_gram=3.71,
            protein_per_gram=0.13,
            carbs_per_gram=0.75,
            fat_per_gram=0.015
        )
        
        # Create a diet plan with meals
        self.diet_plan = DietPlan.objects.create(
            user=self.user,
            goal='Maintain',
            daily_calories=2500,
            start_date=date.today(),
            end_date=date.today() + timedelta(days=3)
        )
        
        self.meal = Meal.objects.create(
            diet_plan=self.diet_plan,
            meal_type='Lunch',
            date=date.today(),
            template='COMPLETE'
        )
    
    def test_macro_balancer_respects_caps(self):
        """Test that MacroBalancer enforces per-item caps after scaling."""
        # Create components with quantities that will be scaled
        comp1 = MealComponent.objects.create(
            meal=self.meal,
            food=self.chicken,
            quantity=300  # Near but under the 350g protein cap
        )
        comp2 = MealComponent.objects.create(
            meal=self.meal,
            food=self.pasta,
            quantity=350  # Near but under the 400g carb cap
        )
        
        balancer = MacroBalancer()
        
        # Mock the diet plan to need more protein (will trigger scaling up)
        with patch.object(self.diet_plan, 'calculate_daily_nutrition') as mock_nutrition:
            # First call - show we're short on protein
            mock_nutrition.return_value = {
                'calories': 2200,
                'protein': 150,  # Short of ~190g target
                'carbs': 300,
                'fat': 60
            }
            
            balancer.rebalance(self.diet_plan)
        
        # Reload components
        comp1.refresh_from_db()
        comp2.refresh_from_db()
        
        # Check that quantities didn't exceed caps
        self.assertLessEqual(comp1.quantity, 350,  # Protein cap
                           f"Chicken exceeded protein cap: {comp1.quantity}g")
        self.assertLessEqual(comp2.quantity, 400,  # Carb cap
                           f"Pasta exceeded carb cap: {comp2.quantity}g")
    
    def test_macro_shortage_booster_enforces_caps(self):
        """Test that MacroShortageBooster enforces caps during boosting."""
        comp = MealComponent.objects.create(
            meal=self.meal,
            food=self.chicken,
            quantity=320  # Starting quantity
        )
        
        booster = MacroShortageBooster()
        
        # Run multiple boost passes
        for _ in range(5):
            booster._boost_macro_for_day(self.diet_plan, date.today(), 'protein', 0.10)
        
        comp.refresh_from_db()
        
        # Even after multiple 10% boosts, should not exceed cap
        self.assertLessEqual(comp.quantity, 350,
                           f"Protein item exceeded cap after boosting: {comp.quantity}g")
    
    def test_vegetable_volume_caps(self):
        """Test that vegetable items are capped to prevent volume blow-ups."""
        # Create a vegetable item
        broccoli = FoodItem.objects.create(
            name='Broccoli',
            category=self.carb_cat,
            calories_per_gram=0.34,  # Very low calorie density
            protein_per_gram=0.028,
            carbs_per_gram=0.066,
            fat_per_gram=0.004
        )
        
        comp = MealComponent.objects.create(
            meal=self.meal,
            food=broccoli,
            quantity=250
        )
        
        booster = MacroShortageBooster()
        
        # Try to boost carbs (which would normally scale up broccoli a lot)
        for _ in range(5):
            booster._boost_macro_for_day(self.diet_plan, date.today(), 'carb', 0.20)
        
        comp.refresh_from_db()
        
        # Vegetables should be capped at 300g
        self.assertLessEqual(comp.quantity, 300,
                           f"Vegetable exceeded volume cap: {comp.quantity}g")


class TestMacroDensityDetection(TestCase):
    """Test cases for macro density and dominant macro detection."""
    
    def setUp(self):
        self.protein_cat = FoodCategory.objects.create(
            name='Protein', is_protein=True
        )
        self.carb_cat = FoodCategory.objects.create(
            name='Carbs', is_carb=True
        )
        self.fat_cat = FoodCategory.objects.create(
            name='Fats', is_fat=True
        )
    
    def test_dominant_macro_with_category(self):
        """Test dominant macro detection using category flags."""
        chicken = FoodItem.objects.create(
            name='Chicken',
            category=self.protein_cat,
            calories_per_gram=1.65,
            protein_per_gram=0.31,
            carbs_per_gram=0,
            fat_per_gram=0.036
        )
        
        factory = MealPlanFactory({})
        
        # Should use category flag first
        self.assertEqual(factory._dominant_macro_of_food(chicken), 'protein')
    
    def test_dominant_macro_fallback_to_calories(self):
        """Test dominant macro detection falls back to calorie calculation."""
        # Food without category
        mixed_food = FoodItem.objects.create(
            name='Mixed Food',
            category=None,
            calories_per_gram=2.5,
            protein_per_gram=0.15,  # 0.6 kcal/g from protein
            carbs_per_gram=0.30,     # 1.2 kcal/g from carbs (dominant)
            fat_per_gram=0.08        # 0.72 kcal/g from fat
        )
        
        factory = MealPlanFactory({})
        
        # Should calculate based on caloric contribution
        self.assertEqual(factory._dominant_macro_of_food(mixed_food), 'carb')
    
    def test_missing_per_gram_attributes(self):
        """Test handling of foods with missing per-gram attributes."""
        from diet.utils.nutrition import get_macro_densities_for_food
        
        # Food with missing per_gram but has per serving
        food = FoodItem.objects.create(
            name='Test Food',
            calories=200,  # Per serving
            serving_size_grams=100,
            protein_per_gram=None,  # Missing
            carbs_per_gram=None,
            fat_per_gram=None,
            calories_per_gram=None  # Missing
        )
        
        p_pg, c_pg, f_pg, kcal_pg = get_macro_densities_for_food(food)
        
        # Should fallback to calculate from serving
        self.assertEqual(kcal_pg, 2.0)  # 200 cal / 100g = 2.0


class TestPersistenceMerging(TestCase):
    """Test cases for persistence and merging of duplicate food items."""
    
    def setUp(self):
        self.user = User.objects.create_user(
            username='testuser',
            email='test@test.com',
            phone_number='+1234567890'
        )
        
        self.diet_plan = DietPlan.objects.create(
            user=self.user,
            goal='Maintain',
            daily_calories=2500,
            start_date=date.today(),
            end_date=date.today() + timedelta(days=3)
        )
        
        self.meal = Meal.objects.create(
            diet_plan=self.diet_plan,
            meal_type='Breakfast',
            date=date.today(),
            template='COMPLETE'
        )
        
        # Create similar foods with different IDs
        self.egg1 = FoodItem.objects.create(
            name='Egg Whites',
            calories_per_gram=0.52,
            protein_per_gram=0.11
        )
        self.egg2 = FoodItem.objects.create(
            name='Egg Whites',  # Same name, different ID
            calories_per_gram=0.52,
            protein_per_gram=0.11
        )
    
    def test_meal_plan_factory_merges_by_id(self):
        """Test that MealPlanFactory merges components with same food ID."""
        factory = MealPlanFactory({})
        
        # Add same food twice
        resolved = [
            (self.egg1, '100g'),
            (self.egg1, '150g')  # Same food ID
        ]
        
        factory.add_components(self.meal, resolved)
        
        # Should have merged into one component
        components = MealComponent.objects.filter(meal=self.meal)
        self.assertEqual(components.count(), 1)
        self.assertEqual(components.first().quantity, 250)  # 100 + 150
    
    def test_meal_plan_factory_does_not_merge_different_ids(self):
        """Test that components with different IDs aren't merged (current behavior)."""
        factory = MealPlanFactory({})
        
        resolved = [
            (self.egg1, '100g'),
            (self.egg2, '150g')  # Different ID, same name
        ]
        
        factory.add_components(self.meal, resolved)
        
        # Currently creates separate components (the bug)
        components = MealComponent.objects.filter(meal=self.meal)
        self.assertEqual(components.count(), 2)  # This is the issue to fix
    
    def test_normalized_name_merging_proposal(self):
        """Test proposed fix: merge by normalized name when appropriate."""
        # This test demonstrates what the fix should do
        from diet.services.rule_based_planner import RuleBasedPlanner
        
        planner = RuleBasedPlanner(self.user)
        
        # Test normalization
        self.assertEqual(
            planner._normalize_name_for_repeat('Egg Whites'),
            planner._normalize_name_for_repeat('egg whites')
        )
        self.assertEqual(
            planner._normalize_name_for_repeat('Chicken Breast'),
            'chicken'  # Maps to base token
        )


class TestIntegrationScenarios(TransactionTestCase):
    """Integration tests for complete meal planning scenarios."""
    
    def setUp(self):
        self.user = User.objects.create_user(
            username='testuser',
            email='test@test.com',
            phone_number='+1234567890'
        )
        self.user.fitness_goal = 'Maintain'
        self.user.save()
        
        # Set up realistic food database
        self._setup_food_database()
        self._setup_user_preferences()
    
    def _setup_food_database(self):
        """Create a realistic set of foods."""
        # Categories
        protein = FoodCategory.objects.create(name='Protein', is_protein=True)
        carb = FoodCategory.objects.create(name='Carbs', is_carb=True)
        fat = FoodCategory.objects.create(name='Fats', is_fat=True)
        veg = FoodCategory.objects.create(name='Vegetables', is_carb=True)
        
        # Proteins
        FoodItem.objects.create(
            name='Egg Whites', category=protein,
            calories_per_gram=0.52, protein_per_gram=0.11,
            carbs_per_gram=0.007, fat_per_gram=0.002
        )
        FoodItem.objects.create(
            name='Chicken Breast', category=protein,
            calories_per_gram=1.65, protein_per_gram=0.31,
            carbs_per_gram=0, fat_per_gram=0.036
        )
        FoodItem.objects.create(
            name='Salmon', category=protein,
            calories_per_gram=2.08, protein_per_gram=0.22,
            carbs_per_gram=0, fat_per_gram=0.13
        )
        
        # Carbs
        FoodItem.objects.create(
            name='White Rice', category=carb,
            calories_per_gram=1.3, protein_per_gram=0.027,
            carbs_per_gram=0.28, fat_per_gram=0.003
        )
        FoodItem.objects.create(
            name='Sweet Potato', category=carb,
            calories_per_gram=0.86, protein_per_gram=0.016,
            carbs_per_gram=0.20, fat_per_gram=0.0005
        )
        FoodItem.objects.create(
            name='Pasta', category=carb,
            calories_per_gram=3.71, protein_per_gram=0.13,
            carbs_per_gram=0.75, fat_per_gram=0.015
        )
        
        # Vegetables (low calorie, can cause volume issues)
        FoodItem.objects.create(
            name='Green Bean', category=veg,
            calories_per_gram=0.31, protein_per_gram=0.018,
            carbs_per_gram=0.07, fat_per_gram=0.001
        )
        FoodItem.objects.create(
            name='Broccoli', category=veg,
            calories_per_gram=0.34, protein_per_gram=0.028,
            carbs_per_gram=0.066, fat_per_gram=0.004
        )
        
        # Fats
        FoodItem.objects.create(
            name='Olive Oil', category=fat,
            calories_per_gram=8.84, protein_per_gram=0,
            carbs_per_gram=0, fat_per_gram=1.0
        )
        FoodItem.objects.create(
            name='Almonds', category=fat,
            calories_per_gram=5.79, protein_per_gram=0.21,
            carbs_per_gram=0.22, fat_per_gram=0.49
        )
    
    def _setup_user_preferences(self):
        """Set up user food preferences."""
        foods = FoodItem.objects.all()
        
        for meal in ['Breakfast', 'Lunch', 'Dinner']:
            for food in foods:
                if food.category:
                    if food.category.is_protein:
                        macro = 'protein'
                    elif food.category.is_fat:
                        macro = 'fat'
                    else:
                        macro = 'carb'
                    
                    UserFoodCategoryPreference.objects.create(
                        user=self.user,
                        meal=meal,
                        macro=macro,
                        food=food
                    )
    
    @patch('diet.models.User.calculate_daily_calories')
    def test_complete_meal_plan_generation(self, mock_calories):
        """Test full meal plan generation with all fixes."""
        mock_calories.return_value = 2538  # Match the user's target
        
        # Generate plan
        planner = RuleBasedPlanner(self.user)
        plan_output = planner.generate(
            daily_kcal=2538,
            meal_count=3,
            snack_count=0,
            duration_days=3,
            no_repeat_days=3
        )
        
        # Validate each day
        meals_by_day = {}
        for i, meal in enumerate(plan_output.plan):
            day = i // 3
            if day not in meals_by_day:
                meals_by_day[day] = []
            meals_by_day[day].append(meal)
        
        for day, meals in meals_by_day.items():
            # Check no duplicates within meals
            for meal in meals:
                food_names = [ing.name for ing in meal.ingredients]
                self.assertEqual(len(food_names), len(set(food_names)),
                               f"Day {day}, {meal.meal_name}: Found duplicates")
                
                # Check realistic quantities
                for ing in meal.ingredients:
                    quantity_g = float(ing.quantity.replace('g', ''))
                    
                    # No single item should exceed reasonable bounds
                    self.assertLessEqual(quantity_g, 500,
                                       f"Unrealistic quantity: {ing.name} - {quantity_g}g")
                    
                    # Vegetables specifically should be under 300g
                    if any(veg in ing.name.lower() for veg in ['bean', 'broccoli', 'spinach', 'tomato']):
                        self.assertLessEqual(quantity_g, 300,
                                           f"Veg exceeded cap: {ing.name} - {quantity_g}g")
    
    @patch('diet.models.User.calculate_daily_calories')
    def test_macro_targets_achieved(self, mock_calories):
        """Test that macro targets are reasonably achieved."""
        mock_calories.return_value = 2538
        
        # Generate and persist plan
        generator = DietGenerator(self.user)
        planner = RuleBasedPlanner(self.user)
        plan_output = planner.generate(
            daily_kcal=2538,
            meal_count=3,
            snack_count=0,
            duration_days=1
        )
        
        diet_plan = generator.save_plan_to_database(
            plan_output,
            meal_count=3,
            snack_count=0
        )
        
        # Calculate achieved macros
        nutrition = diet_plan.calculate_daily_nutrition(date.today())
        
        # Targets (Maintain goal: 30% protein, 50% carb, 20% fat)
        target_protein = 2538 * 0.30 / 4  # ~190g
        target_carbs = 2538 * 0.50 / 4    # ~317g
        target_fat = 2538 * 0.20 / 9      # ~56g
        
        achieved_protein = nutrition.get('protein', 0)
        achieved_carbs = nutrition.get('carbs', 0)
        achieved_fat = nutrition.get('fat', 0)
        
        # Check within 10% of targets
        self.assertAlmostEqual(achieved_protein, target_protein, delta=target_protein*0.1,
                             msg=f"Protein off target: {achieved_protein}g vs {target_protein}g")
        self.assertAlmostEqual(achieved_carbs, target_carbs, delta=target_carbs*0.1,
                             msg=f"Carbs off target: {achieved_carbs}g vs {target_carbs}g")
        self.assertAlmostEqual(achieved_fat, target_fat, delta=target_fat*0.1,
                             msg=f"Fat off target: {achieved_fat}g vs {target_fat}g")
