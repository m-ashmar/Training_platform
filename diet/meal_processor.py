from .models import FoodItem, Meal, MealComponent
from django.conf import settings
import requests
from PIL import Image
import io
import numpy as np

class MealProcessor:
    def __init__(self, ai_meal):
        self.ai_meal = ai_meal
    
    def resolve_ingredients(self):
        """Match ingredients to DB or create placeholders"""
        resolved = []
        for ing in self.ai_meal.ingredients:
            food = FoodItem.objects.filter(name__iexact=ing.name).first()
            
            if not food:
                food = FoodItem.objects.create(
                    name=ing.name,
                    is_ai_generated=True,
                    calories=0,
                    protein=0,
                    carbs=0,
                    fat=0,
                    serving_size=ing.quantity
                )
            resolved.append((food, ing.quantity))
        return resolved
    
    def generate_meal_image(self, ingredients):
        """3-tier image fallback system"""
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
    
    def _composite_image(self, urls):
        """Create simple image collage"""
        images = []
        for url in urls:
            try:
                response = requests.get(url)
                img = Image.open(io.BytesIO(response.content))
                img.thumbnail((200, 200))
                images.append(img)
            except:
                continue
        
        if not images:
            return None
        
        # Create horizontal collage
        widths, heights = zip(*(i.size for i in images))
        total_width = sum(widths)
        max_height = max(heights)
        
        collage = Image.new('RGB', (total_width, max_height))
        x_offset = 0
        for img in images:
            collage.paste(img, (x_offset, 0))
            x_offset += img.size[0]
        
        # Save to media storage (implementation depends on storage backend)
        # Return URL to saved image
        return settings.MEDIA_URL + "generated_collage.jpg"
    
    def _get_category_image(self):
        # Implement category-based image selection
        return None