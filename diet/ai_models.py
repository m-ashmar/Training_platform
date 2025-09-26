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