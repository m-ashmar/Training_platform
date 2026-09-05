"""
ai_models.py - Pydantic Models for AI Diet Plan Generation

This module contains the Pydantic models used for AI diet plan generation,
separated from the main AI services to avoid circular imports.
"""

from typing import List, Dict, Any, Optional
from pydantic import BaseModel, Field, field_validator

class AIIngredient(BaseModel):
    """
    Pydantic model for a single ingredient in a meal.
    Enhanced with validation and nutritional tracking.
    """
    name: str = Field(description="Standard food name")
    quantity: str = Field(description="Amount with unit e.g. '100g' or '2 eggs'")
    #: Identity and amount as the planner decided them. `name` and `quantity` exist for
    #: the LLM path and the client; they are display text. Persistence used to re-resolve
    #: the name (23 duplicate names, then a fuzzy scan, then auto-create) and re-parse the
    #: grams, so the food that reached the plate was not provably the food the planner
    #: had constraint-filtered and ranked. When these are set, persistence uses them and
    #: nothing else.
    food_id: Optional[int] = Field(default=None, description="FoodItem pk chosen by the planner")
    grams: Optional[float] = Field(default=None, description="Amount in grams as decided")
    estimated_calories: Optional[float] = Field(default=None, description="Estimated calories")
    estimated_protein: Optional[float] = Field(default=None, description="Estimated protein in grams")
    estimated_carbs: Optional[float] = Field(default=None, description="Estimated carbs in grams")
    estimated_fat: Optional[float] = Field(default=None, description="Estimated fat in grams")
    
    @field_validator('name')
    @classmethod
    def validate_name(cls, v):
        if not v or len(v.strip()) == 0:
            raise ValueError('Ingredient name cannot be empty')
        return v.strip()
    
    @field_validator('quantity')
    @classmethod
    def validate_quantity(cls, v):
        if not v or len(v.strip()) == 0:
            raise ValueError('Quantity cannot be empty')
        return v.strip()

class AIMeal(BaseModel):
    """
    Pydantic model for a meal, including its ingredients and macros.
    Enhanced with comprehensive nutritional tracking and meal analysis.
    """
    meal_name: str = Field(description="Mealtime designation")
    description: str = Field(description="1-2 sentence appealing description")
    ingredients: List[AIIngredient]
    total_nutrition: Dict[str, float] = Field(description="Dict with calories, protein, carbs, fat")
    meal_type: Optional[str] = Field(default=None, description="Breakfast, Lunch, Dinner, Snack")
    #: Provenance. A recipe id when the meal is a dish from the library; the shape name
    #: when the engine built it from a template. Both were computed and then dropped at
    #: the database boundary.
    recipe_id: Optional[int] = Field(default=None, description="Recipe pk, if a library dish")
    shape: Optional[str] = Field(default=None, description="Template shape, if engine-built")
    reason: Optional[str] = Field(default=None, description="Why the engine chose this meal")
    preparation_time: Optional[int] = Field(default=None, description="Estimated preparation time in minutes")
    difficulty_level: Optional[str] = Field(default=None, description="Easy, Medium, Hard")
    
    @field_validator('total_nutrition')
    @classmethod
    def validate_nutrition(cls, v):
        required_keys = ['calories', 'protein', 'carbs', 'fat']
        for key in required_keys:
            if key not in v:
                raise ValueError(f'Missing required nutrition key: {key}')
        return v

class DietPlanOutput(BaseModel):
    """
    Pydantic model for the overall diet plan output.
    Enhanced with comprehensive metadata for AI training.
    """
    plan: List[AIMeal]
    plan_metadata: Dict[str, Any] = Field(default_factory=dict, description="Additional plan metadata")
    generation_metadata: Dict[str, Any] = Field(default_factory=dict, description="AI generation metadata")
    
    @field_validator('plan')
    @classmethod
    def validate_plan(cls, v):
        if not v or len(v) == 0:
            raise ValueError('Diet plan must contain at least one meal')
        return v 