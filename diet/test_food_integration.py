"""
test_food_integration.py - Comprehensive tests for food search, import, and preferences

This module tests the complete food integration workflow including:
- Food search from local DB and Edamam API
- Food import from Edamam to local DB
- User preferences management (like/dislike)
- API endpoints functionality
"""

import json
from django.test import TestCase, Client
from django.urls import reverse
from django.contrib.auth import get_user_model
from rest_framework.test import APITestCase
from rest_framework import status
from unittest.mock import patch, MagicMock
from .models import FoodItem, UserFoodPreference, FoodCategory
from .views import FoodSearchView, FoodImportView, UserPreferencesView

User = get_user_model()

class FoodIntegrationTestCase(APITestCase):
    """
    Comprehensive test case for food search, import, and preferences functionality.
    """
    
    def setUp(self):
        """Set up test data and user."""
        # Create test user with required phone_number
        self.user = User.objects.create_user(
            username='testuser',
            email='test@example.com',
            password='testpass123',
            phone_number='+1234567890'  # Add required phone_number
        )
        
        # Get or create food categories (use existing ones if they exist)
        self.protein_category = FoodCategory.objects.filter(is_protein=True).first()
        if not self.protein_category:
            self.protein_category = FoodCategory.objects.create(
                name='Proteins',
                is_protein=True,
                is_carb=False,
                is_fat=False
            )
        
        self.carb_category = FoodCategory.objects.filter(is_carb=True).first()
        if not self.carb_category:
            self.carb_category = FoodCategory.objects.create(
                name='Carbs',
                is_protein=False,
                is_carb=True,
                is_fat=False
            )
        
        # Create test food items in local DB
        self.local_food = FoodItem.objects.create(
            api_id='local_001',
            name='Chicken Breast',
            calories=165,
            protein=31,
            carbs=0,
            fat=3.6,
            serving_size='100g',
            serving_size_grams=100,
            category=self.protein_category
        )
        
        # Sample Edamam API response
        self.edamam_response = {
            'hints': [
                {
                    'food': {
                        'foodId': 'edamam_001',
                        'label': 'Salmon',
                        'image': 'https://example.com/salmon.jpg',
                        'nutrients': {
                            'ENERC_KCAL': 208,
                            'PROCNT': 25,
                            'CHOCDF': 0,
                            'FAT': 12
                        }
                    },
                    'measures': [
                        {
                            'label': '100g',
                            'weight': 100
                        }
                    ]
                }
            ]
        }
        
        # Authenticate client
        self.client.force_authenticate(user=self.user)
    
    @patch('diet.views.search_food')
    def test_food_search_combined_results(self, mock_search_food):
        """Test food search returns both local and Edamam results."""
        # Mock Edamam API response
        mock_search_food.return_value = self.edamam_response
        
        # Make search request
        response = self.client.get('/diet/api/food/search/', {'q': 'chicken'})
        
        # Verify response
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.json()
        
        # Check response structure
        self.assertIn('query', data)
        self.assertIn('local_count', data)
        self.assertIn('edamam_count', data)
        self.assertIn('total_count', data)
        self.assertIn('results', data)
        
        # Verify local results
        self.assertGreater(data['local_count'], 0)
        local_results = [r for r in data['results'] if r['source'] == 'local']
        self.assertGreater(len(local_results), 0)
        
        # Verify Edamam results
        self.assertGreater(data['edamam_count'], 0)
        edamam_results = [r for r in data['results'] if r['source'] == 'edamam']
        self.assertGreater(len(edamam_results), 0)
        
        # Verify result structure
        for result in data['results']:
            self.assertIn('id', result)
            self.assertIn('name', result)
            self.assertIn('calories', result)
            self.assertIn('protein', result)
            self.assertIn('carbs', result)
            self.assertIn('fat', result)
            self.assertIn('source', result)
    
    @patch('diet.views.search_food')
    def test_food_search_empty_query(self, mock_search_food):
        """Test food search with empty query returns error."""
        response = self.client.get('/diet/api/food/search/', {'q': ''})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('error', response.json())
    
    @patch('diet.views.search_food')
    def test_food_search_edamam_error_handling(self, mock_search_food):
        """Test food search handles Edamam API errors gracefully."""
        # Mock Edamam API error
        mock_search_food.side_effect = Exception("API Error")
        
        response = self.client.get('/diet/api/food/search/', {'q': 'chicken'})
        
        # Should still return local results
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.json()
        self.assertEqual(data['edamam_count'], 0)
        self.assertGreater(data['local_count'], 0)
    
    def test_food_import_new_item(self):
        """Test importing a new food item from Edamam."""
        food_data = {
            'api_id': 'edamam_001',
            'name': 'Salmon',
            'image_url': 'https://example.com/salmon.jpg',
            'calories': 208,
            'protein': 25,
            'carbs': 0,
            'fat': 12,
            'serving_size': '100g',
            'measures': [{'label': '100g', 'weight': 100}]
        }
        
        response = self.client.post('/diet/api/food/import/', food_data)
        
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        data = response.json()
        
        # Verify response
        self.assertIn('message', data)
        self.assertIn('food_id', data)
        self.assertIn('food_name', data)
        self.assertEqual(data['food_name'], 'Salmon')
        
        # Verify food was created in database
        food_item = FoodItem.objects.get(api_id='edamam_001')
        self.assertEqual(food_item.name, 'Salmon')
        self.assertEqual(food_item.calories, 208)
        self.assertIsNotNone(food_item.category)  # Should be auto-assigned
    
    def test_food_import_existing_item(self):
        """Test importing an already existing food item."""
        # Create existing food item
        existing_food = FoodItem.objects.create(
            api_id='edamam_001',
            name='Existing Salmon',
            calories=200,
            protein=20,
            carbs=0,
            fat=10,
            serving_size='100g',
            serving_size_grams=100
        )
        
        food_data = {
            'api_id': 'edamam_001',
            'name': 'Salmon',
            'calories': 208,
            'protein': 25,
            'carbs': 0,
            'fat': 12,
            'serving_size': '100g'
        }
        
        response = self.client.post('/diet/api/food/import/', food_data)
        
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.json()
        
        # Should return existing food info
        self.assertIn('message', data)
        self.assertEqual(data['food_id'], existing_food.id)
        self.assertEqual(data['food_name'], 'Existing Salmon')
    
    def test_food_import_missing_api_id(self):
        """Test food import with missing API ID returns error."""
        food_data = {
            'name': 'Salmon',
            'calories': 208,
            'protein': 25,
            'carbs': 0,
            'fat': 12
        }
        
        response = self.client.post('/diet/api/food/import/', food_data)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('error', response.json())
    
    def test_user_preferences_get_empty(self):
        """Test getting user preferences when none exist."""
        response = self.client.get('/diet/api/preferences/')
        
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.json()
        
        self.assertIn('liked_foods', data)
        self.assertIn('disliked_foods', data)
        self.assertIn('allergies', data)
        self.assertEqual(len(data['liked_foods']), 0)
        self.assertEqual(len(data['disliked_foods']), 0)
    
    def test_user_preferences_add_like(self):
        """Test adding a food to liked preferences."""
        response = self.client.post('/diet/api/preferences/', {
            'food_id': self.local_food.id,
            'action': 'like'
        })
        
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.json()
        
        self.assertIn('message', data)
        self.assertEqual(data['action'], 'like')
        
        # Verify food was added to preferences
        preferences = UserFoodPreference.objects.get(user=self.user)
        self.assertIn(self.local_food, preferences.liked_foods.all())
        self.assertNotIn(self.local_food, preferences.disliked_foods.all())
    
    def test_user_preferences_add_dislike(self):
        """Test adding a food to disliked preferences."""
        response = self.client.post('/diet/api/preferences/', {
            'food_id': self.local_food.id,
            'action': 'dislike'
        })
        
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.json()
        
        self.assertIn('message', data)
        self.assertEqual(data['action'], 'dislike')
        
        # Verify food was added to preferences
        preferences = UserFoodPreference.objects.get(user=self.user)
        self.assertIn(self.local_food, preferences.disliked_foods.all())
        self.assertNotIn(self.local_food, preferences.liked_foods.all())
    
    def test_user_preferences_switch_from_like_to_dislike(self):
        """Test switching a food from liked to disliked."""
        # First add to liked
        preferences, _ = UserFoodPreference.objects.get_or_create(user=self.user)
        preferences.liked_foods.add(self.local_food)
        
        # Then switch to disliked
        response = self.client.post('/diet/api/preferences/', {
            'food_id': self.local_food.id,
            'action': 'dislike'
        })
        
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        
        # Verify food was moved from liked to disliked
        preferences.refresh_from_db()
        self.assertNotIn(self.local_food, preferences.liked_foods.all())
        self.assertIn(self.local_food, preferences.disliked_foods.all())
    
    def test_user_preferences_remove_like(self):
        """Test removing a food from liked preferences."""
        # First add to liked
        preferences, _ = UserFoodPreference.objects.get_or_create(user=self.user)
        preferences.liked_foods.add(self.local_food)
        
        # Then remove
        response = self.client.delete('/diet/api/preferences/', {
            'food_id': self.local_food.id,
            'action': 'like'
        })
        
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        
        # Verify food was removed
        preferences.refresh_from_db()
        self.assertNotIn(self.local_food, preferences.liked_foods.all())
    
    def test_user_preferences_invalid_action(self):
        """Test preferences with invalid action returns error."""
        response = self.client.post('/diet/api/preferences/', {
            'food_id': self.local_food.id,
            'action': 'invalid'
        })
        
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('error', response.json())
    
    def test_user_preferences_missing_food_id(self):
        """Test preferences with missing food_id returns error."""
        response = self.client.post('/diet/api/preferences/', {
            'action': 'like'
        })
        
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('error', response.json())
    
    def test_user_preferences_nonexistent_food(self):
        """Test preferences with nonexistent food returns 404."""
        response = self.client.post('/diet/api/preferences/', {
            'food_id': 99999,
            'action': 'like'
        })
        
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
    
    def test_authentication_required(self):
        """Test that all endpoints require authentication."""
        # Create unauthenticated client
        client = Client()
        
        # Test food search
        response = client.get('/diet/api/food/search/', {'q': 'chicken'})
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)
        
        # Test food import
        response = client.post('/diet/api/food/import/', {})
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)
        
        # Test preferences
        response = client.get('/diet/api/preferences/')
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)


class FoodCategoryAssignmentTestCase(TestCase):
    """
    Test case for automatic food category assignment functionality.
    """
    
    def setUp(self):
        """Set up test categories."""
        # Use existing categories or create new ones with unique names
        self.protein_category = FoodCategory.objects.filter(is_protein=True).first()
        if not self.protein_category:
            self.protein_category = FoodCategory.objects.create(
                name='Test Proteins',
                is_protein=True,
                is_carb=False,
                is_fat=False
            )
        
        self.carb_category = FoodCategory.objects.filter(is_carb=True).first()
        if not self.carb_category:
            self.carb_category = FoodCategory.objects.create(
                name='Test Carbs',
                is_protein=False,
                is_carb=True,
                is_fat=False
            )
        
        self.fat_category = FoodCategory.objects.filter(is_fat=True).first()
        if not self.fat_category:
            self.fat_category = FoodCategory.objects.create(
                name='Test Fats',
                is_protein=False,
                is_carb=False,
                is_fat=True
            )
    
    def test_protein_food_assignment(self):
        """Test that high-protein foods are assigned to protein category."""
        food = FoodItem.objects.create(
            api_id='test_protein',
            name='Chicken Breast',
            calories=165,
            protein=31,  # High protein
            carbs=0,
            fat=3.6,
            serving_size='100g',
            serving_size_grams=100
        )
        
        # Trigger category assignment
        view = FoodImportView()
        view._auto_assign_category(food)
        
        # Check that it's assigned to a protein category (any protein category)
        self.assertTrue(food.category.is_protein)
    
    def test_carb_food_assignment(self):
        """Test that high-carb foods are assigned to carb category."""
        food = FoodItem.objects.create(
            api_id='test_carb',
            name='Rice',
            calories=130,
            protein=2.7,
            carbs=28,  # High carb
            fat=0.3,
            serving_size='100g',
            serving_size_grams=100
        )
        
        # Trigger category assignment
        view = FoodImportView()
        view._auto_assign_category(food)
        
        # Check that it's assigned to a carb category (any carb category)
        self.assertTrue(food.category.is_carb)
    
    def test_fat_food_assignment(self):
        """Test that high-fat foods are assigned to fat category."""
        food = FoodItem.objects.create(
            api_id='test_fat',
            name='Avocado',
            calories=160,
            protein=2,
            carbs=9,
            fat=15,  # High fat
            serving_size='100g',
            serving_size_grams=100
        )
        
        # Trigger category assignment
        view = FoodImportView()
        view._auto_assign_category(food)
        
        # Check that it's assigned to a fat category (any fat category)
        self.assertTrue(food.category.is_fat)
    
    def test_balanced_food_assignment(self):
        """Test that balanced foods are assigned to 'Other' category."""
        food = FoodItem.objects.create(
            api_id='test_balanced',
            name='Mixed Food',
            calories=150,
            protein=5,  # Balanced macros
            carbs=10,
            fat=8,
            serving_size='100g',
            serving_size_grams=100
        )
        
        # Trigger category assignment
        view = FoodImportView()
        view._auto_assign_category(food)
        
        # Should create 'Other' category
        other_category = FoodCategory.objects.get(name='Other')
        self.assertEqual(food.category, other_category) 