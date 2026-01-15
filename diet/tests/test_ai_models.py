import pytest
from pydantic import ValidationError
from diet.ai_models import AIIngredient, AIMeal, DietPlanOutput

class TestAIIngredient:
    def test_valid_ingredient(self):
        ing = AIIngredient(name='  Chicken  ', quantity=' 100g ')
        assert ing.name == 'Chicken'
        assert ing.quantity == '100g'

    def test_empty_name(self):
        with pytest.raises(ValueError, match='Ingredient name cannot be empty'):
            AIIngredient(name='', quantity='100g')

    def test_empty_quantity(self):
        with pytest.raises(ValueError, match='Quantity cannot be empty'):
            AIIngredient(name='Chicken', quantity='')

class TestAIMeal:
    def test_valid_meal(self):
        ing = AIIngredient(name='Chicken', quantity='100g')
        meal = AIMeal(
            meal_name='Lunch',
            description='A healthy meal',
            ingredients=[ing],
            total_nutrition={'calories': 100, 'protein': 10, 'carbs': 20, 'fat': 5}
        )
        assert meal.meal_name == 'Lunch'
        assert meal.total_nutrition['calories'] == 100

    def test_missing_nutrition_keys(self):
        ing = AIIngredient(name='Chicken', quantity='100g')
        with pytest.raises(ValueError, match='Missing required nutrition key: carbs'):
            AIMeal(
                meal_name='Lunch',
                description='A healthy meal',
                ingredients=[ing],
                total_nutrition={'calories': 100, 'protein': 10, 'fat': 5}
            )

class TestDietPlanOutput:
    def test_valid_plan(self):
        ing = AIIngredient(name='Chicken', quantity='100g')
        meal = AIMeal(
            meal_name='Lunch',
            description='A healthy meal',
            ingredients=[ing],
            total_nutrition={'calories': 100, 'protein': 10, 'carbs': 20, 'fat': 5}
        )
        plan = DietPlanOutput(plan=[meal])
        assert len(plan.plan) == 1

    def test_empty_plan(self):
        with pytest.raises(ValueError, match='Diet plan must contain at least one meal'):
            DietPlanOutput(plan=[]) 