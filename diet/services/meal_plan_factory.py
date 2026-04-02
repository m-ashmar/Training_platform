from __future__ import annotations

from typing import Iterable, Tuple, List
from ..ai_models import AIMeal
from ..models import Meal, MealComponent, FoodItem, DietPlan
from ..utils.nutrition import convert_to_grams, piece_based_grams_if_appropriate, dominant_macro_of_food


class MealPlanFactory:
    """
    Responsible for constructing `Meal` and `MealComponent` objects from AI meals.
    """

    def __init__(self, piece_weights: dict[str, float]):
        self.piece_weights = piece_weights

    def create_meal(self, diet_plan: DietPlan, ai_meal: AIMeal, date) -> Meal:
        return Meal.objects.create(
            diet_plan=diet_plan,
            template=self._determine_meal_template(ai_meal),
            date=date,
            description=ai_meal.description,
            meal_type=ai_meal.meal_type or 'Lunch',
            is_ai_generated=True,
        )

    def add_components(self, meal: Meal, resolved: Iterable[Tuple[FoodItem, str]]) -> None:
        for food_item, quantity in resolved:
            grams = convert_to_grams(quantity)
            grams = piece_based_grams_if_appropriate(quantity, grams, food_item.name, self.piece_weights)
            
            # Merge with existing component of the same food within this meal if present
            # First try exact ID match
            existing = MealComponent.objects.filter(meal=meal, food=food_item).select_related('food', 'food__category').first()
            
            # If not found, try normalized name match to prevent visual duplicates
            if not existing:
                normalized_name = self._normalize_food_name(food_item.name)
                # BUG FIX: Fetch all components with select_related once instead of N queries
                all_components = list(MealComponent.objects.filter(meal=meal).select_related('food', 'food__category'))
                for comp in all_components:
                    if self._normalize_food_name(comp.food.name) == normalized_name:
                        existing = comp
                        break
            
            if existing:
                existing.quantity = float(existing.quantity or 0.0) + float(grams or 0.0)
                # Clamp per-item cap
                from ..utils.nutrition import portion_sanity_cap_grams
                dom = dominant_macro_of_food(existing.food)  # Use existing food for consistency
                cap = portion_sanity_cap_grams(dom)
                if dom == 'carb':
                    cap = min(cap, 400.0)
                if dom == 'protein':
                    cap = min(cap, 350.0)
                if dom == 'fat':
                    cap = min(cap, 100.0)
                name_l = (existing.food.name or '').lower()
                veg_keywords = ('lettuce','tomato','tomatoes','cucumber','green bean',
                              'spinach','zucchini','broccoli','asparagus','carrot',
                              'pepper','cabbage','cauliflower','celery')
                if any(k in name_l for k in veg_keywords):
                    cap = min(cap, 300.0)
                if existing.quantity > cap:
                    existing.quantity = cap
                existing.save(update_fields=['quantity'])
            else:
                MealComponent.objects.create(
                    meal=meal,
                    food=food_item,
                    quantity=grams,
                    meal_time=meal.meal_type,
                )

    def _determine_meal_template(self, ai_meal: AIMeal) -> str:
        nutrition = ai_meal.total_nutrition
        protein = nutrition.get('protein', 0)
        carbs = nutrition.get('carbs', 0)
        fat = nutrition.get('fat', 0)
        if protein > carbs and protein > fat:
            return 'PROTEIN_CARB' if carbs > fat else 'PROTEIN_FAT'
        elif carbs > protein and carbs > fat:
            return 'CARB_FAT' if fat > protein else 'PROTEIN_CARB'
        else:
            return 'COMPLETE'
    
    def _normalize_food_name(self, name: str) -> str:
        """Normalize food name for comparison to prevent duplicates."""
        n = (name or '').strip().lower()
        # Collapse common variants to base token to reduce duplication
        mapping = [
            ('sweet potato', 'sweet potato'),
            ('egg whites', 'egg whites'),
            ('egg white', 'egg whites'),
            ('chicken breast', 'chicken'),
            ('breast', 'chicken'),
            ('tuna', 'tuna'),
            ('fish', 'fish'),
            ('egg', 'egg'),
            ('rice', 'rice'),
            ('white rice', 'rice'),
            ('brown rice', 'rice'),
            ('potato', 'potato'),
            ('almond', 'almonds'),
            ('banana', 'banana'),
            ('oats', 'oats'),
            ('apple', 'apple'),
            ('broccoli', 'broccoli'),
            ('asparagus', 'asparagus'),
            ('olive oil', 'olive oil'),
            ('oil', 'oil'),
        ]
        for key, base in mapping:
            if key in n:
                return base
        return n


