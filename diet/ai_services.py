"""
ai_services.py - AI Diet Plan Generation Utilities

This module provides the DietGenerator class for
structuring and generating AI-powered diet plans using OpenAI GPT-5 Nano.
Enhanced with comprehensive data collection for future AI model training.
Now supports meal count preferences, snack preferences, and template-based generation.
"""

from langchain.prompts import PromptTemplate
from langchain.output_parsers import PydanticOutputParser
from typing import List, Optional, Dict, Any
import requests
import json
import logging
import uuid
from difflib import SequenceMatcher
from datetime import datetime
from django.conf import settings
from django.utils import timezone
from .models import UserFoodPreference, DietPlan, Meal, MealComponent, DailyAdvice, DietPlanTemplate, FoodItem, DietConfig, UserFoodCategoryPreference
from .meal_processor import MealProcessor
from .ai_models import AIIngredient, AIMeal, DietPlanOutput

logger = logging.getLogger(__name__)

class DietGenerator:
    """
    Enhanced DietGenerator uses OpenAI GPT-5 Nano to generate structured diet plans
    with comprehensive data collection for future AI model training.
    Now supports meal count preferences, snack preferences, and template-based generation.
    """
    
    def __init__(self, user):
        """
        Initialize the generator for a specific user.

        Args:
            user (CustomUser): The user for whom to generate the plan.
        """
        self.user = user
        self.logger = logger
        self.generation_id = str(uuid.uuid4())
        self.start_time = timezone.now()
        
        # Initialize OpenAI provider only
        self.openai_api_key = getattr(settings, 'OPENAI_API_KEY', '')
        self.openai_model = getattr(settings, 'OPENAI_MODEL', 'gpt-5-nano')
        self.parser = PydanticOutputParser(pydantic_object=DietPlanOutput)
        self.last_openai_request_id = None
        
        # Enhanced prompt template with structured per-user categories
        self.prompt_template = """
        You're a Michelin-star chef nutritionist creating a personalized meal plan for {user_name}.
        
        **User Profile**
        - BMI: {bmi}
        - BMR: {bmr}
        - Age: {age}
        - Gender: {gender}
        - Activity Level: {activity_level}
        - Allergies: {allergies}
        - Dietary Restrictions: {dietary_restrictions}
        
        **Structured User Input (authoritative) - USE STRICTLY**
        This JSON fully defines goal, macro targets, macro priority, and per-meal allowed foods. Use only items listed for each meal category. Do not invent or swap foods.
        ```json
        {user_structured_json}
        ```
        
        **Meal Structure Preferences**
        - Exactly {meal_count} meals per day
        - Exactly {snack_count} snacks per day (snacks must be healthy and light)
        - Total eating occasions: {total_meals} per day
        
        **Nutritional Constraints**
        - Fitness Goal: {fitness_goal}
        - Macro Priority Policy: {macro_priority_text}
        - Macronutrient balance: Protein {protein_ratio}%, Carbs {carb_ratio}%, Fat {fat_ratio}%
        - Daily calorie target: {calories} kcal (acceptable deviation ±200 kcal)
        - Protein target: {protein_target}g (acceptable deviation ±10g)
        - Carb target: {carb_target}g (acceptable deviation ±10g)
        - Fat target: {fat_target}g (acceptable deviation ±10g)
        
        **Distribution Guidance (soft)**
        - Aim for a logical calorie distribution across meals: breakfast and lunch richer, dinner lighter
        
        **Hard Constraints**
        - Use ONLY foods listed under the corresponding user.food_categories for that meal and macro
        - Do NOT include any other foods; if an item is missing, adjust quantities instead of swapping foods
        - Output ingredient names exactly as they appear in the lists
        - For piece-type foods (eggs, fruits, bread slices), prefer pieces (e.g., "2 eggs"); use grams for others
        
        **Output Format**
        {format_instructions}
        
        **Additional Instructions**
        - Each meal should be nutritionally balanced within the macro priority rules
        - Include estimated preparation time in minutes and difficulty level (Easy/Medium/Hard)
        - Ensure total daily nutrition meets targets (±200 kcal) and macros within ±10g
        - Include plan_metadata.daily_totals with aggregate calories, protein, carbs, fat for the day, and plan_metadata.target_calories
        - IMPORTANT: Return ONLY a JSON object conforming to the schema above. Do NOT include the schema itself or any extra text.
        """

    def _get_user_data(self, meal_count=3, snack_count=0):
        """
        Enhanced user data collection with comprehensive health metrics and meal preferences.
        """
        try:
            preferences = UserFoodPreference.objects.get(user=self.user)
            
            # Structured per-user categories for model input
            structured_food_cats = self._get_user_food_categories()
            
            # Calculate nutritional targets (per goal)
            daily_calories = self.user.calculate_daily_calories()
            user_goal = self._determine_user_goal()
            ratios = self._macro_ratios_for_goal(user_goal)
            protein_target = (daily_calories * ratios["protein"]) / 4
            carb_target = (daily_calories * ratios["carb"]) / 4
            fat_target = (daily_calories * ratios["fat"]) / 9
            
            total_meals = meal_count + snack_count
            
            structured = {
                "user": {
                    "goal": ("shredding" if user_goal == "Lose" else ("bulking" if user_goal == "Gain" else "maintenance")),
                    "target_calories": round(daily_calories, 1),
                    "macros": {
                        "protein": round(protein_target, 1),
                        "carbs": round(carb_target, 1),
                        "fat": round(fat_target, 1)
                    },
                    "food_categories": structured_food_cats,
                    "macro_priority": self._macro_priority_list(user_goal)
                }
            }

            return {
                "user_name": self.user.username,
                "bmi": self.user.calculate_bmi(),
                "bmr": self.user.calculate_bmr(),
                "calories": daily_calories,
                "age": getattr(self.user, 'age', 30),
                "gender": getattr(self.user, 'gender', 'Not specified'),
                "activity_level": getattr(self.user, 'activity_level', 'Moderate'),
                "allergies": preferences.allergies,
                "dietary_restrictions": getattr(self.user, 'dietary_restrictions', 'None'),
                "user_structured_json": json.dumps(structured, ensure_ascii=False),
                "meal_count": meal_count,
                "snack_count": snack_count,
                "total_meals": total_meals,
                "protein_ratio": int(ratios["protein"] * 100),
                "carb_ratio": int(ratios["carb"] * 100),
                "fat_ratio": int(ratios["fat"] * 100),
                "fitness_goal": user_goal,
                "macro_priority_text": self._macro_priority_text(user_goal),
                "protein_target": round(protein_target, 1),
                "carb_target": round(carb_target, 1),
                "fat_target": round(fat_target, 1),
                "format_instructions": self.parser.get_format_instructions()
            }
        except UserFoodPreference.DoesNotExist:
            # Create default preferences if none exist
            preferences = UserFoodPreference.objects.create(user=self.user)
            return self._get_user_data(meal_count, snack_count)

    def _create_generation_metadata(self, meal_count: int, snack_count: int, user_data: Dict[str, Any]) -> Dict[str, Any]:
        """
        Create comprehensive metadata for AI training data collection.
        """
        return {
            "generation_id": self.generation_id,
            "timestamp": self.start_time.isoformat(),
            "user_id": self.user.id,
            "user_profile": {
                "bmi": user_data.get("bmi"),
                "bmr": user_data.get("bmr"),
                "daily_calories": user_data.get("calories"),
                "age": user_data.get("age"),
                "gender": user_data.get("gender"),
                "activity_level": user_data.get("activity_level")
            },
            "generation_parameters": {
                "meal_count": meal_count,
                "snack_count": snack_count,
                "total_meals": meal_count + snack_count,
                "protein_ratio": user_data.get("protein_ratio"),
                "carb_ratio": user_data.get("carb_ratio"),
                "fat_ratio": user_data.get("fat_ratio"),
                "model": getattr(settings, 'OPENAI_MODEL', 'gpt-5-nano'),
                "temperature": 0.3,
                "max_tokens": 4000
            },
            "user_preferences": {
                "likes": user_data.get("likes"),
                "dislikes": user_data.get("dislikes"),
                "allergies": user_data.get("allergies"),
                "dietary_restrictions": user_data.get("dietary_restrictions")
            },
            "performance_metrics": {
                "generation_time": None,  # Will be set after generation
                "token_usage": None,      # Will be set after generation
                "success": None           # Will be set after generation
            }
        }

    def generate_plan(self, meal_count=3, snack_count=1):
        """
        Enhanced diet plan generation with meal count and snack preferences.

        Args:
            meal_count (int): Number of main meals per day (default: 3).
            snack_count (int): Number of snacks per day (default: 0).
        Returns:
            DietPlanOutput: Structured diet plan output with metadata.
        Raises:
            Exception: If generation fails.
        """
        generation_start = timezone.now()
        user_data = self._get_user_data(meal_count, snack_count)
        generation_metadata = self._create_generation_metadata(meal_count, snack_count, user_data)
        
        try:
            # Build prompt text using PromptTemplate to avoid brace collisions
            data_for_prompt = dict(user_data)
            data_for_prompt.pop('meal_count', None)
            data_for_prompt.pop('snack_count', None)
            prompt = PromptTemplate(
                template=self.prompt_template,
                input_variables=["meal_count", "snack_count"],
                partial_variables=data_for_prompt
            )
            final_prompt = prompt.format(meal_count=meal_count, snack_count=snack_count)

            # OpenAI path with model-aware routing
            chat_url = "https://api.openai.com/v1/chat/completions"
            resp_url = "https://api.openai.com/v1/responses"
            headers = {"Authorization": f"Bearer {self.openai_api_key}", "Content-Type": "application/json"}

            def _post(url, payload, timeout=120):
                try:
                    r = requests.post(url, headers=headers, json=payload, timeout=timeout)
                    if r.status_code >= 400:
                        try:
                            logger.error("OpenAI error: %s", r.text)
                        except Exception:
                            pass
                    r.raise_for_status()
                    self.last_openai_request_id = r.headers.get('x-request-id') or r.headers.get('openai-request-id')
                    return r.json()
                except requests.ReadTimeout:
                    # one quick retry
                    r = requests.post(url, headers=headers, json=payload, timeout=timeout)
                    r.raise_for_status()
                    self.last_openai_request_id = r.headers.get('x-request-id') or r.headers.get('openai-request-id')
                    return r.json()

            def _parse_output(data: dict) -> str:
                # Responses API shape
                if isinstance(data, dict) and "output" in data:
                    try:
                        parts = data.get("output", [])
                        texts = []
                        for p in parts:
                            for c in p.get("content", []) or []:
                                t = c.get("text")
                                if t:
                                    texts.append(t)
                        if texts:
                            return "\n".join(texts)
                    except Exception:
                        pass
                # Chat Completions shape
                try:
                    return data["choices"][0]["message"]["content"]
                except Exception:
                    return json.dumps(data)

            model_name = str(self.openai_model)

            if model_name.startswith("gpt-5") or model_name == "gpt-4-nano":
                # Use Responses API, omit temperature; rely on prompt for JSON output
                resp_payload = {
                    "model": model_name,
                    "input": (
                        "System: You are a diet planning assistant that outputs strictly structured JSON as instructed.\n\n"
                        + final_prompt
                    )
                }
                try:
                    data = _post(resp_url, resp_payload)
                except requests.HTTPError:
                    # Fallback to gpt-4-nano via Responses API
                    fb_model = "gpt-4-nano"
                    data = _post(resp_url, {"model": fb_model, "input": resp_payload["input"]})
                    generation_metadata["generation_parameters"]["model"] = fb_model
            else:
                # Use Chat Completions for other models
                cc_payload = {
                    "model": model_name,
                    "messages": [
                        {"role": "system", "content": "You are a diet planning assistant that outputs strictly structured JSON as instructed."},
                        {"role": "user", "content": final_prompt}
                    ],
                    "response_format": {"type": "json_object"}
                }
                # include temperature for non-gpt-5
                cc_payload["temperature"] = 0.3
                try:
                    data = _post(chat_url, cc_payload)
                except requests.HTTPError:
                    fb_model = "gpt-4-nano"
                    data = _post(resp_url, {"model": fb_model, "input": cc_payload["messages"][1]["content"]})
                    generation_metadata["generation_parameters"]["model"] = fb_model

            raw_output = _parse_output(data)
            # Attach request id to generation metadata
            if self.last_openai_request_id:
                generation_metadata.setdefault("generation_parameters", {})
                generation_metadata["generation_parameters"]["openai_request_id"] = self.last_openai_request_id
            
            # Parse and validate output
            parsed_output = self.parser.parse(raw_output)
            
            # Add generation metadata
            generation_time = (timezone.now() - generation_start).total_seconds()
            generation_metadata["performance_metrics"].update({
                "generation_time": generation_time,
                "success": True,
                "raw_output_length": len(str(raw_output)),
                "parsed_meals_count": len(parsed_output.plan)
            })
            
            # Add metadata to output
            parsed_output.generation_metadata = generation_metadata
            
            # Log successful generation
            self.logger.info(
                f"Successfully generated diet plan for user {self.user.id}",
                extra={
                    "user_id": self.user.id,
                    "generation_id": self.generation_id,
                    "meal_count": meal_count,
                    "snack_count": snack_count,
                    "generation_time": generation_time,
                    "meals_generated": len(parsed_output.plan)
                }
            )
            
            return parsed_output
            
        except Exception as e:
            # Update metadata with error information
            generation_time = (timezone.now() - generation_start).total_seconds()
            generation_metadata["performance_metrics"].update({
                "generation_time": generation_time,
                "success": False,
                "error": str(e),
                "error_type": type(e).__name__
            })
            
            # Log error with comprehensive context
            self.logger.error(
                f"Failed to generate diet plan: {str(e)}",
                extra={
                    "user_id": self.user.id,
                    "generation_id": self.generation_id,
                    "generation_metadata": generation_metadata,
                    "user_data": user_data,
                    "meal_count": meal_count,
                    "snack_count": snack_count
                }
            )
            
            raise Exception(f"Failed to generate diet plan: {str(e)}")

    def generate_plan_with_template(self, template: DietPlanTemplate):
        """
        Generate a diet plan using a specific template.

        Args:
            template (DietPlanTemplate): The template to use for generation.
        Returns:
            DietPlanOutput: Structured diet plan output with metadata.
        """
        return self.generate_plan(
            meal_count=template.meals_per_day,
            snack_count=template.snacks_per_day
        )

    def save_plan_to_database(self, plan_output: DietPlanOutput, meal_count: int, snack_count: int = 0):
        """
        Save the generated plan to database with comprehensive metadata.
        This creates the training dataset for future AI models.
        Enhanced to support meal count and snack preferences.
        """
        try:
            import re
            def _to_grams(q: str | float | int) -> float:
                if isinstance(q, (int, float)):
                    return float(q)
                if not isinstance(q, str):
                    return 100.0
                txt = q.strip().lower()
                m = re.findall(r"\d+\.?\d*", txt)
                val = float(m[0]) if m else 100.0
                if "kg" in txt:
                    return val * 1000.0
                if "g" in txt:
                    return val
                if "lb" in txt:
                    return val * 453.592
                if "oz" in txt:
                    return val * 28.3495
                if "cup" in txt:
                    return val * 240.0
                if "tablespoon" in txt or "tbsp" in txt:
                    return val * 15.0
                if "teaspoon" in txt or "tsp" in txt:
                    return val * 5.0
                return val
            
            # Create diet plan record (3-day window)
            diet_plan = DietPlan.objects.create(
                user=self.user,
                goal=getattr(self.user, 'fitness_goal', 'Maintain'),
                daily_calories=self.user.calculate_daily_calories(),
                start_date=timezone.now().date(),
                end_date=timezone.now().date() + timezone.timedelta(days=3),
                duration_weeks=1,
                generated_plan=plan_output.dict(),
                generation_strategy='GPT'
            )
            
            # Process and save meals
            meal_processor = MealProcessor(None)
            
            # Piece weight mapping
            piece_weights = {
                "egg": 50.0,
                "banana": 118.0,
                "apple": 182.0,
                "orange": 131.0,
                "bread": 28.0,
                "avocado": 200.0,
                "tomato": 123.0,
                "cherry tomato": 17.0,
            }
            # Load overrides from DietConfig if present
            try:
                cfg = DietConfig.objects.last()
                if cfg and cfg.piece_weights:
                    piece_weights.update(cfg.piece_weights)
            except Exception:
                pass
            unit_tokens = (
                "kg", "g", "lb", "oz", "cup", "cups", "tablespoon", "tbsp",
                "teaspoon", "tsp", "ml", "l"
            )

            # Breakfast replacement keywords (allowed at breakfast per culture)
            breakfast_allowed_keywords = (
                "oat", "yogurt", "egg", "bread", "potato", "pasta", "cheese",
                "milk", "banana", "apple", "honey", "butter"
            )
            try:
                cfg = DietConfig.objects.last()
                if cfg and cfg.breakfast_allowed_keywords:
                    breakfast_allowed_keywords = tuple(cfg.breakfast_allowed_keywords)
            except Exception:
                pass
            
            # Proceed to persist meals using categorized foods only
            
            # Enforce per-meal category foods: use only categorized foods for this user
            cat_qs = UserFoodCategoryPreference.objects.filter(user=self.user).select_related('food')
            def key_for(meal: str, macro: str) -> str:
                macro_key = 'carbs' if macro == 'carb' else macro
                return f"{meal.lower()}_{macro_key}"
            categories: Dict[str, set] = {}
            for m in ("Breakfast", "Lunch", "Dinner", "Snack"):
                for mac in ("carb", "protein", "fat"):
                    categories[key_for(m, mac)] = set()
            for m in cat_qs:
                categories[key_for(m.meal, m.macro)].add(m.food.name)
            # Macro pools across meals as fallback for missing meal-specific categories
            macro_pool: Dict[str, set] = {"carb": set(), "protein": set(), "fat": set()}
            for mac in ("carb", "protein", "fat"):
                for meal_key, names in categories.items():
                    if meal_key.endswith('carbs') and mac == 'carb':
                        macro_pool['carb'].update(names)
                    if meal_key.endswith('protein') and mac == 'protein':
                        macro_pool['protein'].update(names)
                    if meal_key.endswith('fat') and mac == 'fat':
                        macro_pool['fat'].update(names)
            # Build FoodItem lookup for all category names
            all_cat_names = set()
            for s in categories.values():
                all_cat_names.update(s)
            name_to_food: Dict[str, FoodItem] = {}
            if all_cat_names:
                for f in FoodItem.objects.filter(name__in=list(all_cat_names)):
                    name_to_food[f.name.lower()] = f

            def _closest_from_pool(name: str, pool_names: set[str]) -> FoodItem | None:
                name_l = (name or "").lower()
                # Exact match first
                if name_l in name_to_food:
                    if name_to_food[name_l].name in pool_names:
                        return name_to_food[name_l]
                # Fuzzy within provided pool
                best = None
                best_ratio = 0.0
                for an in pool_names:
                    an_l = (an or "").lower()
                    r = SequenceMatcher(None, name_l, an_l).ratio()
                    if r > best_ratio:
                        best_ratio = r
                        best = an_l
                if best and best_ratio >= 0.88:
                    return name_to_food.get(best)
                return None

            for i, ai_meal in enumerate(plan_output.plan):
                meal = Meal.objects.create(
                    diet_plan=diet_plan,
                    template=self._determine_meal_template(ai_meal),
                    date=timezone.now().date() + timezone.timedelta(
                        days=i // (meal_count + snack_count if (meal_count + snack_count) else 1)
                    ),
                    description=ai_meal.description,
                    meal_type=ai_meal.meal_type or 'Lunch',
                    is_ai_generated=True
                )
                
                resolved_ingredients = meal_processor.resolve_ingredients_from_ai_meal(ai_meal)
                
                for food_item, quantity in resolved_ingredients:
                    # Enforce per-meal categorized foods only
                    meal_type = ai_meal.meal_type or 'Lunch'
                    dom_macro = self._dominant_macro_of_food(food_item)
                    cat_key = key_for(meal_type, dom_macro)
                    pool = categories.get(cat_key, set())
                    if pool:
                        if food_item.name not in pool:
                            replacement = _closest_from_pool(food_item.name, pool)
                            if not replacement:
                                # Fallback to macro pool across meals
                                replacement = _closest_from_pool(food_item.name, macro_pool.get(dom_macro, set()))
                            if not replacement:
                                self.logger.info(
                                    "Skipping item '%s' not in categorized pool for %s/%s",
                                    food_item.name, meal_type, dom_macro
                                )
                                continue
                            food_item = replacement
                    else:
                        # If no pool for this meal/macro, fallback to macro pool across meals
                        pool2 = macro_pool.get(dom_macro, set())
                        if pool2 and food_item.name not in pool2:
                            replacement = _closest_from_pool(food_item.name, pool2)
                            if replacement:
                                food_item = replacement
                            else:
                                self.logger.info(
                                    "Skipping item '%s' (no categorized pool for %s/%s)",
                                    food_item.name, meal_type, dom_macro
                                )
                                continue

                    q_txt = str(quantity).strip().lower()
                    grams = _to_grams(quantity)
                    # If quantity explicitly in grams but unrealistically low for piece-type, coerce based on piece map
                    def _is_piece_food(name_l: str) -> str | None:
                        for key in piece_weights.keys():
                            if key in name_l:
                                return key
                        # egg synonyms
                        if any(k in name_l for k in ("poached egg", "hard-boiled", "fried egg", "scrambled egg", "egg,", "eggs")):
                            return "egg"
                        return None
                    name_l = (food_item.name or "").lower()
                    piece_key = _is_piece_food(name_l)
                    if piece_key:
                        # Extract numeric value from the quantity text if present
                        import re as _re
                        nums = _re.findall(r"\d+\.?\d*", q_txt) if q_txt else []
                        num_val = float(nums[0]) if nums else None
                        # If there is no unit token or grams is tiny (< piece weight * 0.25), treat as piece count
                        if (not any(tok in q_txt for tok in unit_tokens)) or (("g" in q_txt) and grams < piece_weights[piece_key] * 0.25 and (num_val is not None)):
                            try:
                                count = num_val if num_val is not None else 1.0
                            except Exception:
                                count = 1.0
                            grams = max(grams, count * piece_weights[piece_key])
                    if (
                        isinstance(quantity, (int, float)) or
                        (q_txt and q_txt.isdigit()) or
                        (q_txt and all(ch.isdigit() or ch == '.' for ch in q_txt))
                    ) and not any(tok in q_txt for tok in unit_tokens):
                        name_l = (food_item.name or "").lower()
                        for key, wt in piece_weights.items():
                            if key in name_l:
                                try:
                                    num = float(q_txt) if q_txt else float(quantity)
                                except Exception:
                                    num = 1.0
                                grams = num * wt
                                break
                    MealComponent.objects.create(
                        meal=meal,
                        food=food_item,
                        quantity=grams,
                        meal_time=ai_meal.meal_type or 'Lunch'
                    )
            
            # Training data logging: capture inputs/outputs/repairs for future model training
            training_data = {
                "user_profile": {
                    "user_id": self.user.id,
                    "bmi": getattr(self.user, 'calculate_bmi', lambda: None)(),
                    "bmr": getattr(self.user, 'calculate_bmr', lambda: None)(),
                    "target_calories": diet_plan.daily_calories,
                    "macro_ratios": {"protein": 30, "carb": 50, "fat": 20},
                },
                "categorized_foods": {k: sorted(list(v)) for k, v in categories.items()},
                "piece_weights": piece_weights,
                "rules": {"no_rice_breakfast": True},
                "model": plan_output.generation_metadata.get("generation_parameters", {}).get("model"),
                "generation_metadata": plan_output.generation_metadata,
                "final_plan_id": diet_plan.id,
            }
            DailyAdvice.objects.create(
                user=self.user,
                text=f"AI-generated diet plan created with {meal_count} meals and {snack_count} snacks",
                context_data={
                    "generation_metadata": plan_output.generation_metadata,
                    "training_data": training_data,
                }
            )

            # Priority-aware macro rebalancing per day (before kcal scaling)
            try:
                self._rebalance_macros_by_goal(diet_plan)
            except Exception as _:
                pass

            # Final kcal scaling per day to keep within ±100 kcal
            try:
                from collections import defaultdict
                meals_by_date = defaultdict(list)
                for m in diet_plan.meals.all():
                    meals_by_date[m.date].append(m)
                for d, meals_list in meals_by_date.items():
                    # compute total calories
                    total = 0.0
                    for m in meals_list:
                        total += m.calculate_nutrition()["calories"]
                    target = diet_plan.daily_calories
                    diff = total - target
                    if abs(diff) > 100 and total > 0:
                        scale = max(0.8, min(1.2, target / total))
                        for m in meals_list:
                            for comp in m.components.all():
                                comp.quantity = comp.quantity * scale
                                comp.save(update_fields=["quantity"])
            except Exception as _:
                pass
            
            self.logger.info(
                f"Successfully saved diet plan to database",
                extra={
                    "user_id": self.user.id,
                    "diet_plan_id": diet_plan.id,
                    "meals_created": len(plan_output.plan),
                    "meal_count": meal_count,
                    "snack_count": snack_count,
                    "generation_id": self.generation_id
                }
            )
            
            return diet_plan
            
        except Exception as e:
            self.logger.error(
                f"Failed to save diet plan to database: {str(e)}",
                extra={
                    "user_id": self.user.id,
                    "generation_id": self.generation_id,
                    "plan_output": plan_output.dict() if plan_output else None
                }
            )
            raise

    def _determine_meal_template(self, ai_meal: AIMeal) -> str:
        """
        Determine meal template based on nutritional content.
        """
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

    # =============================
    # Goal & Macro Prioritization
    # =============================
    def _determine_user_goal(self) -> str:
        """Infer user's goal: 'Lose', 'Gain', or 'Maintain'."""
        # Prefer explicit attribute if present
        goal = getattr(self.user, 'fitness_goal', None) or getattr(self.user, 'goal', None)
        if not goal:
            # Try client_goals list
            try:
                goals = (getattr(self.user, 'client_goals', []) or [])
                goals_l = ",".join(goals).lower()
                if 'lose' in goals_l or 'fat' in goals_l:
                    return 'Lose'
                if 'gain' in goals_l or 'muscle' in goals_l:
                    return 'Gain'
            except Exception:
                pass
            return 'Maintain'
        g = str(goal).lower()
        if 'lose' in g or 'fat' in g:
            return 'Lose'
        if 'gain' in g or 'muscle' in g:
            return 'Gain'
        return 'Maintain'

    def _macro_ratios_for_goal(self, goal: str) -> Dict[str, float]:
        """Default macro ratios by goal."""
        g = (goal or '').lower()
        if 'lose' in g:
            return {"protein": 0.35, "carb": 0.40, "fat": 0.25}
        if 'gain' in g:
            return {"protein": 0.25, "carb": 0.55, "fat": 0.20}
        return {"protein": 0.30, "carb": 0.50, "fat": 0.20}

    def _macro_priority_text(self, goal: str) -> str:
        """Human-readable priority rules text injected in the prompt."""
        g = (goal or 'Maintain').lower()
        if 'lose' in g:
            return (
                "Shredding/Fat Loss: prioritize Protein > Fat > Carbs; protein should meet or "
                "slightly exceed target; fat should meet target with small deviations; carbs must not "
                "exceed target and a slight deficit is acceptable."
            )
        if 'gain' in g:
            return (
                "Bulking/Muscle Gain: prioritize Carbs > Protein > Fats; carbs may exceed target if "
                "needed; protein should meet or slightly exceed target; fats may exceed target."
            )
        return (
            "Maintenance/Balanced: equal priority for all macros; aim to match targets within a small "
            "tolerance."
        )

    def _macro_priority_list(self, goal: str) -> List[str]:
        g = (goal or 'Maintain').lower()
        if 'lose' in g:
            return ["protein", "fat", "carbs"]
        if 'gain' in g:
            return ["carbs", "protein", "fat"]
        return ["protein", "carbs", "fat"]

    def _get_user_food_categories(self) -> Dict[str, List[str]]:
        """Build per-meal macro food lists from UserFoodCategoryPreference."""
        cat_qs = UserFoodCategoryPreference.objects.filter(user=self.user).select_related('food')
        out: Dict[str, List[str]] = {
            'breakfast_carbs': [], 'lunch_carbs': [], 'dinner_carbs': [], 'snack_carbs': [],
            'breakfast_protein': [], 'lunch_protein': [], 'dinner_protein': [], 'snack_protein': [],
            'breakfast_fat': [], 'lunch_fat': [], 'dinner_fat': [], 'snack_fat': []
        }
        def key(meal: str, macro: str) -> str:
            macro_key = 'carbs' if macro == 'carb' else macro
            return f"{meal.lower()}_{macro_key}"
        for m in cat_qs:
            k = key(m.meal, m.macro)
            lst = out.get(k)
            if lst is not None:
                if m.food.name not in lst:
                    lst.append(m.food.name)
        return out

    def _dominant_macro_of_food(self, food: FoodItem) -> str:
        """Return 'protein' | 'carb' | 'fat' depending on dominant caloric contribution."""
        try:
            # Prefer explicit category flags if available
            if food.category:
                if getattr(food.category, 'is_protein', False):
                    return 'protein'
                if getattr(food.category, 'is_carb', False):
                    return 'carb'
                if getattr(food.category, 'is_fat', False):
                    return 'fat'
        except Exception:
            pass
        # Fallback to per-gram macro calories
        p_cals = 4.0 * float(getattr(food, 'protein_per_gram', 0.0))
        c_cals = 4.0 * float(getattr(food, 'carbs_per_gram', 0.0))
        f_cals = 9.0 * float(getattr(food, 'fat_per_gram', 0.0))
        if p_cals >= c_cals and p_cals >= f_cals:
            return 'protein'
        if c_cals >= p_cals and c_cals >= f_cals:
            return 'carb'
        return 'fat'

    def _rebalance_macros_by_goal(self, diet_plan: DietPlan) -> None:
        """Soft macro balancing per day according to goal priority rules."""
        from collections import defaultdict
        user_goal = diet_plan.goal or self._determine_user_goal()
        ratios = self._macro_ratios_for_goal(user_goal)
        daily_target_cals = float(diet_plan.daily_calories or 0)
        if daily_target_cals <= 0:
            return
        protein_target = daily_target_cals * ratios['protein'] / 4.0
        carb_target = daily_target_cals * ratios['carb'] / 4.0
        fat_target = daily_target_cals * ratios['fat'] / 9.0

        meals_by_date = defaultdict(list)
        for m in diet_plan.meals.all():
            meals_by_date[m.date].append(m)

        # Tolerances
        tol_pct = 0.10  # 10% default
        for d, meals_list in meals_by_date.items():
            # Compute current totals
            totals = diet_plan.calculate_daily_nutrition(d)
            cur_p = float(totals.get('protein', 0.0))
            cur_c = float(totals.get('carbs', 0.0))
            cur_f = float(totals.get('fat', 0.0))

            def apply_scale_to_components(macro_key: str, scale: float):
                for m in meals_list:
                    for comp in m.components.all():
                        dom = self._dominant_macro_of_food(comp.food)
                        if dom == macro_key:
                            comp.quantity = comp.quantity * scale
                            comp.save(update_fields=['quantity'])

            g = (user_goal or 'Maintain').lower()
            if 'lose' in g:
                # Protein: raise if below target (allow up to +15% step)
                if cur_p < protein_target:
                    scale = min(1.15, (protein_target / max(cur_p, 1e-6)))
                    apply_scale_to_components('protein', scale)
                    totals = diet_plan.calculate_daily_nutrition(d)
                    cur_p = float(totals.get('protein', 0.0))

                # Carbs: reduce if above target
                if cur_c > carb_target:
                    scale = max(0.85, (carb_target / max(cur_c, 1e-6)))
                    apply_scale_to_components('carb', scale)
                    totals = diet_plan.calculate_daily_nutrition(d)
                    cur_c = float(totals.get('carbs', 0.0))

                # Fat: keep near target within tolerance
                upper_f = fat_target * (1.0 + tol_pct)
                if cur_f > upper_f:
                    scale = max(0.85, (fat_target / max(cur_f, 1e-6)))
                    apply_scale_to_components('fat', scale)

            elif 'gain' in g:
                # Carbs: raise if below target
                if cur_c < carb_target:
                    scale = min(1.20, (carb_target / max(cur_c, 1e-6)))
                    apply_scale_to_components('carb', scale)
                    totals = diet_plan.calculate_daily_nutrition(d)
                    cur_c = float(totals.get('carbs', 0.0))

                # Protein: raise if below target
                if cur_p < protein_target:
                    scale = min(1.15, (protein_target / max(cur_p, 1e-6)))
                    apply_scale_to_components('protein', scale)

                # Fats: permissive; no strict reduction

            else:
                # Maintenance: keep each within ±10%
                lower_p = protein_target * (1.0 - tol_pct)
                upper_p = protein_target * (1.0 + tol_pct)
                lower_c = carb_target * (1.0 - tol_pct)
                upper_c = carb_target * (1.0 + tol_pct)
                lower_f = fat_target * (1.0 - tol_pct)
                upper_f = fat_target * (1.0 + tol_pct)

                if cur_p < lower_p:
                    scale = min(1.15, (protein_target / max(cur_p, 1e-6)))
                    apply_scale_to_components('protein', scale)
                    totals = diet_plan.calculate_daily_nutrition(d)
                    cur_p = float(totals.get('protein', 0.0))
                elif cur_p > upper_p:
                    scale = max(0.85, (protein_target / max(cur_p, 1e-6)))
                    apply_scale_to_components('protein', scale)

                if cur_c < lower_c:
                    scale = min(1.15, (carb_target / max(cur_c, 1e-6)))
                    apply_scale_to_components('carb', scale)
                    totals = diet_plan.calculate_daily_nutrition(d)
                    cur_c = float(totals.get('carbs', 0.0))
                elif cur_c > upper_c:
                    scale = max(0.85, (carb_target / max(cur_c, 1e-6)))
                    apply_scale_to_components('carb', scale)

                if cur_f < lower_f:
                    scale = min(1.15, (fat_target / max(cur_f, 1e-6)))
                    apply_scale_to_components('fat', scale)
                elif cur_f > upper_f:
                    scale = max(0.85, (fat_target / max(cur_f, 1e-6)))
                    apply_scale_to_components('fat', scale)