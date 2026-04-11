"""
Input Validation Module for Diet System
Prevents invalid data from entering the system and causing calculation errors.
"""

from typing import Dict, Any, List, Tuple
from django.core.exceptions import ValidationError
from django.utils.translation import gettext_lazy as _


class DietInputValidator:
    """Validates all inputs for diet plan generation."""
    
    # Valid ranges
    MIN_CALORIES = 1000.0
    MAX_CALORIES = 6000.0
    MIN_QUANTITY = 1.0
    MAX_QUANTITY = 1000.0
    MIN_SERVING_SIZE = 1
    MAX_SERVING_SIZE = 2000
    
    @staticmethod
    def validate_daily_calories(calories: float) -> float:
        """Validate daily calorie target."""
        try:
            calories = float(calories)
        except (ValueError, TypeError):
            raise ValidationError(
                _("Daily calories must be a number."), code="invalid_calories_type"
            )
        
        if calories < DietInputValidator.MIN_CALORIES:
            raise ValidationError(
                _("Daily calories too low. Minimum is %(min)s kcal.") % {"min": int(DietInputValidator.MIN_CALORIES)},
                code="calories_too_low",
            )
        
        if calories > DietInputValidator.MAX_CALORIES:
            raise ValidationError(
                _("Daily calories too high. Maximum is %(max)s kcal.") % {"max": int(DietInputValidator.MAX_CALORIES)},
                code="calories_too_high",
            )
        
        return calories
    
    @staticmethod
    def validate_meal_count(meal_count: int) -> int:
        """Validate meal count."""
        try:
            meal_count = int(meal_count)
        except (ValueError, TypeError):
            raise ValidationError(
                _("Meal count must be an integer."), code="invalid_meal_count_type"
            )
        
        if meal_count < 1:
            raise ValidationError(
                _("Meal count must be at least 1."), code="meal_count_too_low"
            )
        
        if meal_count > 6:
            raise ValidationError(
                _("Meal count cannot exceed 6."), code="meal_count_too_high"
            )
        
        return meal_count
    
    @staticmethod
    def validate_duration_days(duration_days: int) -> int:
        """Validate plan duration."""
        try:
            duration_days = int(duration_days)
        except (ValueError, TypeError):
            raise ValidationError(
                _("Duration days must be an integer."), code="invalid_duration_type"
            )
        
        if duration_days < 1:
            raise ValidationError(
                _("Duration must be at least 1 day."), code="duration_too_short"
            )
        
        if duration_days > 30:
            raise ValidationError(
                _("Duration cannot exceed 30 days."), code="duration_too_long"
            )
        
        return duration_days
    
    @staticmethod
    def validate_quantity(quantity: float, context: str = "quantity") -> float:
        """Validate food quantity in grams."""
        try:
            quantity = float(quantity)
        except (ValueError, TypeError):
            raise ValidationError(
                _("Quantity must be a number."), code="invalid_quantity_type"
            )
        
        if quantity < DietInputValidator.MIN_QUANTITY:
            raise ValidationError(
                _("Quantity too low. Minimum is %(min)sg.") % {"min": DietInputValidator.MIN_QUANTITY},
                code="quantity_too_low",
            )
        
        if quantity > DietInputValidator.MAX_QUANTITY:
            raise ValidationError(
                _("Quantity too high. Maximum is %(max)sg.") % {"max": DietInputValidator.MAX_QUANTITY},
                code="quantity_too_high",
            )
        
        return quantity
    
    @staticmethod
    def validate_macro_targets(protein_g: float, carb_g: float, fat_g: float) -> Tuple[float, float, float]:
        """Validate macro targets are reasonable."""
        try:
            protein_g = float(protein_g)
            carb_g = float(carb_g)
            fat_g = float(fat_g)
        except (ValueError, TypeError):
            raise ValidationError(
                _("Macro targets must be numbers."), code="invalid_macro_type"
            )
        
        # Check non-negative
        if protein_g < 0 or carb_g < 0 or fat_g < 0:
            raise ValidationError(
                _("Macro targets cannot be negative."), code="negative_macros"
            )
        
        # Check minimum values
        if protein_g < 30:
            raise ValidationError(
                _("Protein target too low. Minimum is 30g per day."), code="protein_too_low"
            )
        
        if carb_g < 20:
            raise ValidationError(
                _("Carb target too low. Minimum is 20g per day."), code="carbs_too_low"
            )
        
        if fat_g < 20:
            raise ValidationError(
                _("Fat target too low. Minimum is 20g per day."), code="fat_too_low"
            )
        
        # Check maximum values
        if protein_g > 400:
            raise ValidationError(
                _("Protein target too high. Maximum is 400g per day."), code="protein_too_high"
            )
        
        if carb_g > 800:
            raise ValidationError(
                _("Carb target too high. Maximum is 800g per day."), code="carbs_too_high"
            )
        
        if fat_g > 200:
            raise ValidationError(
                _("Fat target too high. Maximum is 200g per day."), code="fat_too_high"
            )
        
        # Check total calories make sense (not too extreme)
        total_kcal = (protein_g * 4) + (carb_g * 4) + (fat_g * 9)
        if total_kcal < DietInputValidator.MIN_CALORIES:
            raise ValidationError(
                _("Total calories from macros is too low."), code="macro_calories_too_low"
            )
        
        if total_kcal > DietInputValidator.MAX_CALORIES:
            raise ValidationError(
                _("Total calories from macros is too high."), code="macro_calories_too_high"
            )
        
        return protein_g, carb_g, fat_g
    
    @staticmethod
    def validate_food_item(food_data: Dict[str, Any]) -> Dict[str, Any]:
        """Validate food item nutritional data."""
        required_fields = ['name', 'calories', 'protein', 'carbs', 'fat', 'serving_size_grams']
        
        for field in required_fields:
            if field not in food_data:
                raise ValidationError(
                    _("Missing required field: %(field)s") % {"field": field},
                    code="missing_field",
                )
        
        # Validate name
        if not food_data['name'] or not food_data['name'].strip():
            raise ValidationError(
                _("Food name cannot be empty."), code="empty_food_name"
            )
        
        if len(food_data['name']) > 255:
            raise ValidationError(
                _("Food name too long (max 255 characters)."), code="food_name_too_long"
            )
        
        # Validate nutritional values
        try:
            calories = float(food_data['calories'])
            protein = float(food_data['protein'])
            carbs = float(food_data['carbs'])
            fat = float(food_data['fat'])
            serving_size_grams = int(food_data['serving_size_grams'])
        except (ValueError, TypeError):
            raise ValidationError(
                _("Nutritional values must be numbers."), code="invalid_nutrition_type"
            )
        
        # Check non-negative
        if any(v < 0 for v in [calories, protein, carbs, fat]):
            raise ValidationError(
                _("Nutritional values cannot be negative."), code="negative_nutrition"
            )
        
        # Check serving size
        if serving_size_grams < DietInputValidator.MIN_SERVING_SIZE:
            raise ValidationError(
                _("Serving size too small."), code="serving_too_small"
            )
        
        if serving_size_grams > DietInputValidator.MAX_SERVING_SIZE:
            raise ValidationError(
                _("Serving size too large."), code="serving_too_large"
            )
        
        # Check calories make sense relative to macros
        theoretical_kcal = (protein * 4) + (carbs * 4) + (fat * 9)
        
        # Allow 20% margin for fiber, alcohol, etc.
        if calories > theoretical_kcal * 1.2:
            raise ValidationError(
                _("Calories don't match macros. Check if values are correct."),
                code="calorie_macro_mismatch",
            )
        
        return food_data
    
    @staticmethod
    def validate_meal_component(food_id: int, quantity: float) -> Tuple[int, float]:
        """Validate a meal component."""
        try:
            food_id = int(food_id)
        except (ValueError, TypeError):
            raise ValidationError(
                _("Food ID must be an integer."), code="invalid_food_id_type"
            )
        
        if food_id <= 0:
            raise ValidationError(
                _("Food ID must be positive."), code="invalid_food_id"
            )
        
        quantity = DietInputValidator.validate_quantity(quantity, "Component quantity")
        
        return food_id, quantity
    
    @staticmethod
    def validate_generation_params(params: Dict[str, Any]) -> Dict[str, Any]:
        """Validate all parameters for diet plan generation."""
        validated = {}
        
        # Validate daily calories
        if 'daily_calories' in params:
            validated['daily_calories'] = DietInputValidator.validate_daily_calories(params['daily_calories'])
        
        # Validate meal count
        if 'meal_count' in params:
            validated['meal_count'] = DietInputValidator.validate_meal_count(params['meal_count'])
        
        # Validate snack count
        if 'snack_count' in params:
            try:
                snack_count = int(params['snack_count'])
            except (ValueError, TypeError):
                raise ValidationError(
                    _("Snack count must be an integer."), code="invalid_snack_count_type"
                )
            
            if snack_count < 0 or snack_count > 3:
                raise ValidationError(
                    _("Snack count must be between 0 and 3."), code="snack_count_out_of_range"
                )
            
            validated['snack_count'] = snack_count
        
        # Validate duration
        if 'duration_days' in params:
            validated['duration_days'] = DietInputValidator.validate_duration_days(params['duration_days'])
        
        # Validate start date if provided
        if 'start_date' in params and params['start_date']:
            from datetime import date
            try:
                if isinstance(params['start_date'], str):
                    date.fromisoformat(params['start_date'])
            except ValueError:
                raise ValidationError(
                    _("Invalid start date format. Use YYYY-MM-DD."), code="invalid_date_format"
                )
        
        return validated


# Convenience functions
def validate_diet_generation(daily_calories: float, meal_count: int, duration_days: int) -> None:
    """Validate all inputs for diet plan generation."""
    DietInputValidator.validate_daily_calories(daily_calories)
    DietInputValidator.validate_meal_count(meal_count)
    DietInputValidator.validate_duration_days(duration_days)


def validate_component_quantity(quantity: float) -> float:
    """Validate meal component quantity."""
    return DietInputValidator.validate_quantity(quantity)
