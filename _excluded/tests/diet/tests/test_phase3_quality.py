from __future__ import annotations

import json
from unittest.mock import patch, MagicMock
import requests
from django.test import TestCase
from django.contrib.auth import get_user_model

from diet.utils.http import post_json_with_retry
from diet.exceptions import HTTPPermanentError, OpenAIError, DietParsingError
from diet.utils.logging_utils import redact_pii
from diet.services.ai_response_handler import AIResponseHandler
from diet.services.diet_persistence import DietPersistenceService
from diet.services.meal_validator import MealValidator
from diet.ai_models import DietPlanOutput, AIMeal, AIIngredient
from diet.models import FoodItem, FoodCategory


class HttpRetryTests(TestCase):
    @patch("diet.utils.http.requests.post")
    def test_retry_on_timeout_then_success(self, mock_post):
        first = requests.Timeout()

        class Response:
            def raise_for_status(self):
                return None

            def json(self):
                return {"ok": True}

        mock_post.side_effect = [first, Response()]
        data = post_json_with_retry("https://example.com", {}, {})
        self.assertEqual(data["ok"], True)
        self.assertEqual(mock_post.call_count, 2)

    @patch("diet.utils.http.requests.post")
    def test_no_retry_on_permanent_400(self, mock_post):
        resp = MagicMock()
        resp.status_code = 400
        err = requests.HTTPError(response=resp)
        mock_post.side_effect = [err]
        with self.assertRaises(HTTPPermanentError):
            post_json_with_retry("https://example.com", {}, {})
        self.assertEqual(mock_post.call_count, 1)


class AIResponseHandlerTests(TestCase):
    @patch("diet.services.ai_response_handler.post_json_with_retry")
    def test_parsing_error_mapped(self, mock_post):
        # Force chat completions branch by using gpt-4
        handler = AIResponseHandler(model="gpt-4")
        mock_post.return_value = {"choices": [{"message": {"content": "{}"}}]}
        with self.assertRaises(DietParsingError):
            handler.generate("prompt")


class LoggingRedactionTests(TestCase):
    def test_redact_pii(self):
        data = {
            "email": "user@example.com",
            "age": 30,
            "gender": "Male",
            "nested": {"username": "john"},
            "tags": ["alpha", "beta", "john@example.com"],
        }
        red = redact_pii(data)
        self.assertEqual(red["email"], "<redacted>")
        self.assertEqual(red["age"], "<redacted>")
        self.assertEqual(red["gender"], "<redacted>")
        self.assertEqual(red["nested"]["username"], "<redacted>")
        self.assertEqual(red["tags"][2], "<redacted>")


class PersistenceFlowTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="testuser", email="t@t.com", password="x", phone_number="+1234567890",
            age=30, gender="M", height=180, weight=80
        )
        cat = FoodCategory.objects.create(name="Proteins")
        # Minimal food setup
        self.chicken = FoodItem.objects.create(
            api_id="test_chicken_breast",
            name="Chicken Breast",
            category=cat,
            calories=165,
            protein=31,
            carbs=0,
            fat=3.6,
            serving_size="100g",
            serving_size_grams=100,
        )
        from diet.models import UserFoodCategoryPreference
        UserFoodCategoryPreference.objects.create(
            user=self.user,
            meal="Lunch",
            macro="protein",
            food=self.chicken
        )

    @patch("diet.meal_processor.MealProcessor")
    def test_save_plan_persists_meal_and_components(self, MockMP):
        # Fake resolver returns one known FoodItem
        instance = MockMP.return_value
        instance.resolve_ingredients_from_ai_meal.return_value = [(self.chicken, "150g")]

        ai_meal = AIMeal(
            meal_name="Lunch",
            description="Tasty chicken",
            ingredients=[AIIngredient(name="Chicken Breast", quantity="150g")],
            total_nutrition={"calories": 300, "protein": 50, "carbs": 0, "fat": 6},
            meal_type="Lunch",
        )
        output = DietPlanOutput(plan=[ai_meal])

        svc = DietPersistenceService(self.user)
        plan = svc.save_plan(output, meal_count=1, snack_count=0, start_date=None)
        self.assertIsNotNone(plan.id)
        self.assertEqual(plan.meals.count(), 1)
        comps = plan.meals.first().components.all()
        self.assertEqual(comps.count(), 1)
        self.assertEqual(comps.first().food.name, "Chicken Breast")


class MealValidatorTests(TestCase):
    def test_allergen_filter(self):
        egg = FoodItem.objects.create(
            api_id="test_egg",
            name="Egg",
            calories=78,
            protein=6,
            carbs=0.6,
            fat=5.3,
            serving_size="50g",
            serving_size_grams=50,
        )
        validator = MealValidator(user_allergies="egg", strict=False)
        comps = list(validator.validate([(egg, "1 piece")]))
        self.assertEqual(len(comps), 0)


