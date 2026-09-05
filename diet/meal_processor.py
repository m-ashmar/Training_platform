"""
meal_processor.py - Advanced Meal Processing and AI Integration

This module provides comprehensive meal processing capabilities including:
- AI-generated meal ingredient resolution
- Nutritional validation and calculation
- Image generation and processing
- Training dataset creation
- Confidence scoring and error handling
"""

import logging
import re
import uuid
from dataclasses import dataclass, field
from typing import List, Tuple, Optional, Dict, Any
from difflib import SequenceMatcher

import requests
from PIL import Image
import io
from django.conf import settings
from django.utils import timezone
from django.db.models import Q

from .models import FoodItem, Meal, MealComponent
from .ai_models import AIMeal, AIIngredient

logger = logging.getLogger(__name__)

@dataclass
class ProcessedIngredient:
    """Data class for processed ingredient information."""
    food_item: FoodItem
    quantity: str
    estimated_calories: float
    estimated_protein: float
    estimated_carbs: float
    estimated_fat: float
    confidence_score: float
    source: str  # 'database', 'ai_generated', 'estimated'


class MealProcessor:
    """
    Advanced meal processor with AI integration, nutritional validation,
    and comprehensive error handling.
    """
    
    def __init__(self, ai_meal: Optional[AIMeal] = None):
        self.ai_meal = ai_meal
        self.processed_ingredients: List[ProcessedIngredient] = []
        self.nutrition_validation_errors: List[str] = []
        self.processing_metadata: Dict[str, Any] = {
            "processing_timestamp": timezone.now().isoformat(),
            "processor_version": "2.0",
            "ai_meal_provided": ai_meal is not None
        }
        self.logger = logging.getLogger(f"{__name__}.{self.__class__.__name__}")
    
    def resolve_ingredients_from_ai_meal(self, ai_meal: AIMeal) -> List[Tuple[FoodItem, str]]:
        """
        Resolve AI-generated ingredients to database food items with confidence scoring.
        
        Args:
            ai_meal: The AI-generated meal object.
            
        Returns:
            List of (FoodItem, quantity) tuples.
        """
        self.ai_meal = ai_meal
        resolved_ingredients = []
        
        for ingredient in ai_meal.ingredients:
            # The deterministic path names the row it chose. Nothing below may reinterpret
            # that: not a name lookup, not a fuzzy scan, not a fallback row. A food the
            # planner cleared for allergens and dislikes must be the food that is saved.
            if ingredient.food_id is not None:
                food_item = FoodItem.objects.filter(pk=ingredient.food_id).first()
                if food_item is None:
                    raise ValueError(
                        f"planner chose FoodItem {ingredient.food_id} ({ingredient.name!r}) "
                        f"but no such row exists")
                amount = ingredient.grams if ingredient.grams is not None else ingredient.quantity
                resolved_ingredients.append((food_item, amount))
                continue

            try:
                processed = self._process_single_ingredient(ingredient)
                self.processed_ingredients.append(processed)
                resolved_ingredients.append((processed.food_item, processed.quantity))
                
                self.logger.info(
                    f"Processed ingredient: {ingredient.name} -> {processed.food_item.name} "
                    f"(confidence: {processed.confidence_score:.2f})"
                )
                
            except Exception as e:
                self.logger.error(f"Failed to process ingredient {ingredient.name}: {str(e)}")
                # Create a fallback food item
                fallback_item = self._create_fallback_food_item(ingredient)
                resolved_ingredients.append((fallback_item, ingredient.quantity))
        
        # Validate overall meal nutrition
        self._validate_meal_nutrition()
        
        return resolved_ingredients
    
    def _process_single_ingredient(self, ingredient: AIIngredient) -> ProcessedIngredient:
        """
        Process a single AI ingredient with comprehensive matching and validation.
        
        Args:
            ingredient: The AI-generated ingredient.
            
        Returns:
            ProcessedIngredient with resolved food item and metadata.
        """
        # Find or create food item
        food_item = self._find_or_create_food_item(ingredient)
        
        # Calculate nutrition estimates
        nutrition_estimates = self._calculate_nutrition_estimates(ingredient, food_item)
        
        # Calculate confidence score
        confidence_score = self._calculate_confidence_score(ingredient, food_item)
        
        # Determine source
        source = self._determine_ingredient_source(food_item)
        
        return ProcessedIngredient(
            food_item=food_item,
            quantity=ingredient.quantity,
            estimated_calories=nutrition_estimates['calories'],
            estimated_protein=nutrition_estimates['protein'],
            estimated_carbs=nutrition_estimates['carbs'],
            estimated_fat=nutrition_estimates['fat'],
            confidence_score=confidence_score,
            source=source
        )
    
    def _find_or_create_food_item(self, ingredient: AIIngredient) -> FoodItem:
        """
        Find existing food item or create new one with intelligent matching.
        
        Args:
            ingredient: The AI-generated ingredient.
            
        Returns:
            FoodItem instance.
        """
        # Try exact match first with de-duplication handling
        food_item = self._safe_select_food_by_name(ingredient.name)
        if food_item:
            return food_item
        
        # Try fuzzy matching
        fuzzy_match = self._fuzzy_match_ingredient(ingredient.name)
        if fuzzy_match:
            return fuzzy_match
        
        # Create new AI-generated food item
        return self._create_ai_generated_food_item(ingredient)

    def _safe_select_food_by_name(self, name: str) -> Optional[FoodItem]:
        """Return a stable FoodItem for a given name, preferring non-AI and categorized items.

        This avoids MultipleObjectsReturned by selecting the best candidate deterministically.
        """
        qs = list(FoodItem.objects.filter(name__iexact=name))
        if not qs:
            return None
        def sort_key(f: FoodItem):
            api = str(getattr(f, 'api_id', '') or '')
            is_ai = api.startswith('AI-')
            has_cat = bool(getattr(f, 'category', None))
            # Prefer categorized, non-AI, newest id
            return (has_cat, not is_ai, f.id)
        qs.sort(key=sort_key, reverse=True)
        # Log when duplicates exist
        if len(qs) > 1:
            self.logger.warning("Duplicate FoodItem records for '%s' detected; choosing id=%s", name, qs[0].id)
        return qs[0]
    
    def _fuzzy_match_ingredient(self, ingredient_name: str) -> Optional[FoodItem]:
        """
        Perform fuzzy matching to find similar food items.
        
        Args:
            ingredient_name: The ingredient name to match.
            
        Returns:
            FoodItem if found, None otherwise.
        """
        # Get all food items
        all_foods = FoodItem.objects.all()
        best_match = None
        best_ratio = 0.8  # Minimum similarity threshold
        
        for food in all_foods:
            ratio = SequenceMatcher(None, ingredient_name.lower(), food.name.lower()).ratio()
            if ratio > best_ratio:
                best_ratio = ratio
                best_match = food
        
        if best_match:
            self.logger.info(f"Fuzzy matched '{ingredient_name}' to '{best_match.name}' (ratio: {best_ratio:.2f})")
        
        return best_match
    
    def _create_ai_generated_food_item(self, ingredient: AIIngredient) -> FoodItem:
        """
        Create a new food item from AI-generated ingredient data.
        
        Args:
            ingredient: The AI-generated ingredient.
            
        Returns:
            Newly created FoodItem.
        """
        # Estimate nutrition if not provided
        if not ingredient.estimated_calories:
            calories, protein, carbs, fat = self._estimate_nutrition_from_name(ingredient.name)
        else:
            calories = ingredient.estimated_calories
            protein = ingredient.estimated_protein or 0
            carbs = ingredient.estimated_carbs or 0
            fat = ingredient.estimated_fat or 0
        
        # If an item now exists (race), reuse it
        existing = self._safe_select_food_by_name(ingredient.name)
        if existing:
            return existing

        # Create the food item (FoodItem requires api_id)
        food_item = FoodItem.objects.create(
            api_id=f"AI-{uuid.uuid4().hex}",
            name=ingredient.name,
            calories=calories,
            protein=protein,
            carbs=carbs,
            fat=fat,
            serving_size="100g",  # Default serving size
            image_url=self._generate_placeholder_image_url(ingredient.name)
        )
        
        self.logger.info(f"Created new AI-generated food item: {ingredient.name}")
        return food_item
    
    def _estimate_nutrition_from_name(self, food_name: str) -> Tuple[float, float, float, float]:
        """
        Estimate nutrition values based on food name using heuristics.
        
        Args:
            food_name: The food name.
            
        Returns:
            Tuple of (calories, protein, carbs, fat) per 100g.
        """
        food_name_lower = food_name.lower()
        
        # Simple heuristics based on food categories
        if any(word in food_name_lower for word in ['chicken', 'beef', 'pork', 'fish', 'meat']):
            return 165.0, 25.0, 0.0, 7.0  # Protein-rich foods
        elif any(word in food_name_lower for word in ['rice', 'pasta', 'bread', 'potato']):
            return 130.0, 3.0, 28.0, 0.5  # Carbohydrate-rich foods
        elif any(word in food_name_lower for word in ['oil', 'butter', 'avocado']):
            return 884.0, 0.0, 0.0, 100.0  # Fat-rich foods
        elif any(word in food_name_lower for word in ['apple', 'banana', 'orange', 'fruit']):
            return 52.0, 0.3, 14.0, 0.2  # Fruits
        elif any(word in food_name_lower for word in ['broccoli', 'spinach', 'carrot', 'vegetable']):
            return 25.0, 2.0, 5.0, 0.3  # Vegetables
        else:
            return 100.0, 5.0, 15.0, 2.0  # Default estimate
    
    def _calculate_nutrition_estimates(self, ingredient: AIIngredient, food_item: FoodItem) -> Dict[str, float]:
        """
        Calculate nutrition estimates for the ingredient based on quantity.
        
        Args:
            ingredient: The AI-generated ingredient.
            food_item: The resolved food item.
            
        Returns:
            Dictionary with nutrition estimates.
        """
        # Calculate scale factor based on quantity
        scale_factor = self._calculate_scale_factor(ingredient.quantity, food_item.serving_size)
        
        return {
            'calories': food_item.calories * scale_factor,
            'protein': food_item.protein * scale_factor,
            'carbs': food_item.carbs * scale_factor,
            'fat': food_item.fat * scale_factor
        }
    
    def _calculate_scale_factor(self, quantity: str, serving_size: str) -> float:
        """
        Calculate scale factor based on quantity and serving size.
        
        Args:
            quantity: The ingredient quantity (e.g., "200g", "2 cups").
            serving_size: The food item serving size (e.g., "100g").
            
        Returns:
            Scale factor as float.
        """
        # Extract numbers from quantity and serving size
        quantity_num = self._extract_number(quantity)
        serving_num = self._extract_number(serving_size)
        
        if serving_num == 0:
            return 1.0
        
        return quantity_num / serving_num
    
    def _extract_number(self, text: str) -> float:
        """
        Extract numeric value from text.
        
        Args:
            text: Text containing a number.
            
        Returns:
            Extracted number as float.
        """
        # Find all numbers in the text
        numbers = re.findall(r'\d+\.?\d*', text)
        if numbers:
            return float(numbers[0])
        return 0.0
    
    def _calculate_confidence_score(self, ingredient: AIIngredient, food_item: FoodItem) -> float:
        """
        Calculate confidence score for ingredient resolution.
        
        Args:
            ingredient: The AI-generated ingredient.
            food_item: The resolved food item.
            
        Returns:
            Confidence score between 0.0 and 1.0.
        """
        score = 0.0
        
        # Exact name match
        if ingredient.name.lower() == food_item.name.lower():
            score += 0.4
        
        # Fuzzy name similarity
        similarity = SequenceMatcher(None, ingredient.name.lower(), food_item.name.lower()).ratio()
        score += similarity * 0.3
        
        # Nutrition data availability
        if food_item.calories > 0:
            score += 0.2
        
        # AI-generated vs database item
        if not getattr(food_item, 'api_id', '').startswith('AI-'):
            score += 0.1
        
        return min(score, 1.0)
    
    def _is_valid_quantity_format(self, quantity: str) -> bool:
        """
        Validate quantity format.
        
        Args:
            quantity: The quantity string to validate.
            
        Returns:
            True if valid, False otherwise.
        """
        # Check for common quantity patterns
        patterns = [
            r'^\d+\.?\d*\s*(g|kg|ml|l|oz|lb|cups?|tablespoons?|teaspoons?)$',
            r'^\d+\.?\d*$',  # Just numbers
            r'^\d+\s*-\s*\d+',  # Range
        ]
        
        return any(re.match(pattern, quantity, re.IGNORECASE) for pattern in patterns)
    
    def _determine_ingredient_source(self, food_item: FoodItem) -> str:
        """
        Determine the source of the ingredient.
        
        Args:
            food_item: The food item.
            
        Returns:
            Source string ('database', 'ai_generated', 'estimated').
        """
        if str(getattr(food_item, 'api_id', '') or '').startswith('AI-'):
            return 'ai_generated'
        elif float(getattr(food_item, 'calories', 0) or 0) > 0:
            return 'database'
        else:
            return 'estimated'

    def _create_fallback_food_item(self, ingredient: AIIngredient) -> FoodItem:
        """
        Create a minimal fallback FoodItem when processing fails.
        """
        calories, protein, carbs, fat = self._estimate_nutrition_from_name(ingredient.name)
        return FoodItem.objects.create(
            api_id=f"AI-{uuid.uuid4().hex}",
            name=f"{ingredient.name}",
            calories=calories,
            protein=protein,
            carbs=carbs,
            fat=fat,
            serving_size="100g",
            image_url=self._generate_placeholder_image_url(ingredient.name)
        )
    
    def _validate_meal_nutrition(self):
        """
        Validate the overall meal nutrition against AI estimates.
        """
        if not self.ai_meal:
            return
        
        # Calculate total nutrition from processed ingredients
        total_calories = sum(pi.estimated_calories for pi in self.processed_ingredients)
        total_protein = sum(pi.estimated_protein for pi in self.processed_ingredients)
        total_carbs = sum(pi.estimated_carbs for pi in self.processed_ingredients)
        total_fat = sum(pi.estimated_fat for pi in self.processed_ingredients)
        
        # Get AI nutrition estimates
        ai_nutrition = self.ai_meal.total_nutrition
        ai_calories = ai_nutrition.get('calories', 0)
        ai_protein = ai_nutrition.get('protein', 0)
        ai_carbs = ai_nutrition.get('carbs', 0)
        ai_fat = ai_nutrition.get('fat', 0)
        
        # Check for significant discrepancies (>20% difference)
        tolerance = 0.2
        
        if ai_calories > 0 and abs(total_calories - ai_calories) / ai_calories > tolerance:
            self.nutrition_validation_errors.append(
                f"Calories mismatch: AI={ai_calories}, Calculated={total_calories}"
            )
        
        if ai_protein > 0 and abs(total_protein - ai_protein) / ai_protein > tolerance:
            self.nutrition_validation_errors.append(
                f"Protein mismatch: AI={ai_protein}, Calculated={total_protein}"
            )
        
        if ai_carbs > 0 and abs(total_carbs - ai_carbs) / ai_carbs > tolerance:
            self.nutrition_validation_errors.append(
                f"Carbs mismatch: AI={ai_carbs}, Calculated={total_carbs}"
            )
        
        if ai_fat > 0 and abs(total_fat - ai_fat) / ai_fat > tolerance:
            self.nutrition_validation_errors.append(
                f"Fat mismatch: AI={ai_fat}, Calculated={total_fat}"
            )
    
    def generate_meal_image(self, ingredients: List[Tuple[FoodItem, str]]) -> str:
        """
        Enhanced 3-tier image generation system with better error handling.
        
        Args:
            ingredients: List of (food_item, quantity) tuples.
            
        Returns:
            str: URL to the generated image.
        """
        try:
            # Tier 1: Use ingredient images
            valid_images = [i[0].image_url for i in ingredients if i[0].image_url]
            
            if valid_images:
                if len(valid_images) > 3:
                    return self._composite_image(valid_images[:3])
                return valid_images[0]
            
            # Tier 2: Use category image
            category_img = self._get_category_image()
            if category_img:
                return category_img
            
            # Tier 3: Default meal image
            return settings.DEFAULT_MEAL_IMAGE
        
        except Exception as e:
            self.logger.error(f"Failed to generate meal image: {str(e)}")
            return settings.DEFAULT_MEAL_IMAGE
    
    def _composite_image(self, urls: List[str]) -> str:
        """
        Create enhanced image collage with better error handling.
        
        Args:
            urls: List of image URLs.
            
        Returns:
            str: URL to the composite image.
        """
        images = []
        
        for url in urls:
            try:
                response = requests.get(url, timeout=10)
                response.raise_for_status()
                img = Image.open(io.BytesIO(response.content))
                img.thumbnail((200, 200))
                images.append(img)
            except Exception as e:
                self.logger.warning(f"Failed to load image {url}: {str(e)}")
                continue
        
        if not images:
            return settings.DEFAULT_MEAL_IMAGE
        
        try:
            # Create horizontal collage
            widths, heights = zip(*(i.size for i in images))
            total_width = sum(widths)
            max_height = max(heights)
            
            collage = Image.new('RGB', (total_width, max_height), (255, 255, 255))
            x_offset = 0
            
            for img in images:
                collage.paste(img, (x_offset, 0))
                x_offset += img.size[0]
            
            # Save to media storage
            return self._save_collage_to_storage(collage)
            
        except Exception as e:
            self.logger.error(f"Failed to create image collage: {str(e)}")
            return settings.DEFAULT_MEAL_IMAGE
    
    def _save_collage_to_storage(self, collage: Image.Image) -> str:
        """
        Save collage image to storage.
        
        Args:
            collage: The PIL image to save.
            
        Returns:
            str: URL to the saved image.
        """
        try:
            # Convert to bytes
            img_buffer = io.BytesIO()
            collage.save(img_buffer, format='JPEG', quality=85)
            img_buffer.seek(0)
            
            # Save to media storage (implementation depends on storage backend)
            # For now, return a placeholder URL
            return f"{settings.MEDIA_URL}generated_collage_{int(timezone.now().timestamp())}.jpg"
            
        except Exception as e:
            self.logger.error(f"Failed to save collage: {str(e)}")
            return settings.DEFAULT_MEAL_IMAGE
    
    def _get_category_image(self) -> Optional[str]:
        """
        Get category-based image.
        
        Returns:
            Optional[str]: URL to category image or None.
        """
        # Implement category-based image selection
        # This could use ML to classify meal type and select appropriate image
        return None
    
    def _generate_placeholder_image_url(self, food_name: str) -> str:
        """
        Generate placeholder image URL for new food items.
        
        Args:
            food_name (str): The food name.
            
        Returns:
            str: Placeholder image URL.
        """
        # This could integrate with food image APIs or use a placeholder service
        return f"{settings.MEDIA_URL}placeholder_food.jpg"
    
    def create_training_dataset_entry(self, meal: Meal, user_id: int) -> Dict[str, Any]:
        """
        Create a comprehensive training dataset entry for future AI model development.
        
        Args:
            meal (Meal): The processed meal.
            user_id (int): The user ID.
            
        Returns:
            Dict[str, Any]: Training dataset entry.
        """
        return {
            "entry_id": str(uuid.uuid4()),
            "timestamp": timezone.now().isoformat(),
            "user_id": user_id,
            "meal_data": {
                "meal_id": meal.id,
                "meal_name": meal.description,
                "meal_type": getattr(meal, 'meal_type', 'Lunch'),
                "template": meal.template,
                "is_ai_generated": meal.is_ai_generated,
                "date": meal.date.isoformat() if meal.date else None
            },
            "ingredients_data": [
                {
                    "food_id": comp.food.id,
                    "food_name": comp.food.name,
                    "quantity": comp.quantity,
                    "calories": comp.food.calories,
                    "protein": comp.food.protein,
                    "carbs": comp.food.carbs,
                    "fat": comp.food.fat,
                    "is_ai_generated": comp.food.is_ai_generated
                }
                for comp in meal.mealcomponent_set.all()
            ],
            "processing_metadata": self.processing_metadata,
            "validation_errors": self.nutrition_validation_errors,
            "confidence_scores": [
                {
                    "ingredient_name": pi.food_item.name,
                    "confidence": pi.confidence_score,
                    "source": pi.source
                }
                for pi in self.processed_ingredients
            ]
        } 