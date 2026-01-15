"""
Input Validation Module for Diet System
Prevents invalid data from entering the system and causing calculation errors.
"""

from typing import Dict, Any, List, Tuple
from django.core.exceptions import ValidationError


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
            raise ValidationError(f"Daily calories must be a number, got: {calories}")
        
        if calories < DietInputValidator.MIN_CALORIES:
            raise ValidationError(
                f"Daily calories too low ({calories}). Minimum is {DietInputValidator.MIN_CALORIES} kcal"
            )
        
        if calories > DietInputValidator.MAX_CALORIES:
            raise ValidationError(
                f"Daily calories too high ({calories}). Maximum is {DietInputValidator.MAX_CALORIES} kcal"
            )
        
        return calories
    
    @staticmethod
    def validate_meal_count(meal_count: int) -> int:
        """Validate meal count."""
        try:
            meal_count = int(meal_count)
        except (ValueError, TypeError):
            raise ValidationError(f"Meal count must be an integer, got: {meal_count}")
        
        if meal_count < 1:
            raise ValidationError("Meal count must be at least 1")
        
        if meal_count > 6:
            raise ValidationError("Meal count cannot exceed 6")
        
        return meal_count
    
    @staticmethod
    def validate_duration_days(duration_days: int) -> int:
        """Validate plan duration."""
        try:
            duration_days = int(duration_days)
        except (ValueError, TypeError):
            raise ValidationError(f"Duration days must be an integer, got: {duration_days}")
        
        if duration_days < 1:
            raise ValidationError("Duration must be at least 1 day")
        
        if duration_days > 30:
            raise ValidationError("Duration cannot exceed 30 days")
        
        return duration_days
    
    @staticmethod
    def validate_quantity(quantity: float, context: str = "quantity") -> float:
        """Validate food quantity in grams."""
        try:
            quantity = float(quantity)
        except (ValueError, TypeError):
            raise ValidationError(f"{context} must be a number, got: {quantity}")
        
        if quantity < DietInputValidator.MIN_QUANTITY:
            raise ValidationError(
                f"{context} too low ({quantity}g). Minimum is {DietInputValidator.MIN_QUANTITY}g"
            )
        
        if quantity > DietInputValidator.MAX_QUANTITY:
            raise ValidationError(
                f"{context} too high ({quantity}g). Maximum is {DietInputValidator.MAX_QUANTITY}g"
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
            raise ValidationError("Macro targets must be numbers")
        
        # Check non-negative
        if protein_g < 0 or carb_g < 0 or fat_g < 0:
            raise ValidationError("Macro targets cannot be negative")
        
        # Check minimum values
        if protein_g < 30:
            raise ValidationError("Protein target too low. Minimum is 30g per day")
        
        if carb_g < 20:
            raise ValidationError("Carb target too low. Minimum is 20g per day")
        
        if fat_g < 20:
            raise ValidationError("Fat target too low. Minimum is 20g per day")
        
        # Check maximum values
        if protein_g > 400:
            raise ValidationError("Protein target too high. Maximum is 400g per day")
        
        if carb_g > 800:
            raise ValidationError("Carb target too high. Maximum is 800g per day")
        
        if fat_g > 200:
            raise ValidationError("Fat target too high. Maximum is 200g per day")
        
        # Check total calories make sense (not too extreme)
        total_kcal = (protein_g * 4) + (carb_g * 4) + (fat_g * 9)
        if total_kcal < DietInputValidator.MIN_CALORIES:
            raise ValidationError(
                f"Total calories from macros ({total_kcal:.0f}) is too low. Minimum is {DietInputValidator.MIN_CALORIES}"
            )
        
        if total_kcal > DietInputValidator.MAX_CALORIES:
            raise ValidationError(
                f"Total calories from macros ({total_kcal:.0f}) is too high. Maximum is {DietInputValidator.MAX_CALORIES}"
            )
        
        return protein_g, carb_g, fat_g
    
    @staticmethod
    def validate_food_item(food_data: Dict[str, Any]) -> Dict[str, Any]:
        """Validate food item nutritional data."""
        required_fields = ['name', 'calories', 'protein', 'carbs', 'fat', 'serving_size_grams']
        
        for field in required_fields:
            if field not in food_data:
                raise ValidationError(f"Missing required field: {field}")
        
        # Validate name
        if not food_data['name'] or not food_data['name'].strip():
            raise ValidationError("Food name cannot be empty")
        
        if len(food_data['name']) > 255:
            raise ValidationError("Food name too long (max 255 characters)")
        
        # Validate nutritional values
        try:
            calories = float(food_data['calories'])
            protein = float(food_data['protein'])
            carbs = float(food_data['carbs'])
            fat = float(food_data['fat'])
            serving_size_grams = int(food_data['serving_size_grams'])
        except (ValueError, TypeError):
            raise ValidationError("Nutritional values must be numbers")
        
        # Check non-negative
        if any(v < 0 for v in [calories, protein, carbs, fat]):
            raise ValidationError("Nutritional values cannot be negative")
        
        # Check serving size
        if serving_size_grams < DietInputValidator.MIN_SERVING_SIZE:
            raise ValidationError(
                f"Serving size too small ({serving_size_grams}g). Minimum is {DietInputValidator.MIN_SERVING_SIZE}g"
            )
        
        if serving_size_grams > DietInputValidator.MAX_SERVING_SIZE:
            raise ValidationError(
                f"Serving size too large ({serving_size_grams}g). Maximum is {DietInputValidator.MAX_SERVING_SIZE}g"
            )
        
        # Check calories make sense relative to macros
        # Theoretical max from macros
        theoretical_kcal = (protein * 4) + (carbs * 4) + (fat * 9)
        
        # Allow 20% margin for fiber, alcohol, etc.
        if calories > theoretical_kcal * 1.2:
            raise ValidationError(
                f"Calories ({calories}) don't match macros (theoretical: {theoretical_kcal:.1f}). "
                "Check if values are correct."
            )
        
        # Warn if calories significantly lower (might indicate missing macros)
        if calories < theoretical_kcal * 0.7 and calories > 0:
            # This is a warning, not an error - some foods have fiber that reduces calories
            pass
        
        return food_data
    
    @staticmethod
    def validate_meal_component(food_id: int, quantity: float) -> Tuple[int, float]:
        """Validate a meal component."""
        try:
            food_id = int(food_id)
        except (ValueError, TypeError):
            raise ValidationError(f"Food ID must be an integer, got: {food_id}")
        
        if food_id <= 0:
            raise ValidationError("Food ID must be positive")
        
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
                raise ValidationError("Snack count must be an integer")
            
            if snack_count < 0 or snack_count > 3:
                raise ValidationError("Snack count must be between 0 and 3")
            
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
                raise ValidationError("Invalid start date format. Use YYYY-MM-DD")
        
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



