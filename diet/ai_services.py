"""
ai_services.py - AI Diet Plan Generation Utilities

This module provides the DietGenerator class for
structuring and generating AI-powered diet plans using OpenAI GPT-5 Nano.
Enhanced with comprehensive data collection for future AI model training.
Now supports meal count preferences, snack preferences, and template-based generation.
"""

from langchain_core.prompts import PromptTemplate
from langchain_core.output_parsers import PydanticOutputParser
from typing import List, Optional, Dict, Any
import requests
import json
import logging
import uuid
from difflib import SequenceMatcher
from datetime import datetime
from django.conf import settings
from django.utils import timezone
from .models import UserFoodPreference, DietPlanTemplate, FoodItem, DietPlan, UserFoodCategoryPreference
from .ai_models import DietPlanOutput, AIMeal
from .services.prompt_builder import PromptBuilder
from .services.ai_response_handler import AIResponseHandler
from .services.diet_persistence import DietPersistenceService

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
        
        # Template building service
        self.prompt_builder = PromptBuilder()
        # AI response handler
        self.ai = AIResponseHandler()

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

    @staticmethod
    def _sanitize_prompt_text(value, max_len: int = 200) -> str:
        """
        Neutralize user-supplied free text before embedding it in the LLM prompt.
        Collapses newlines (block multi-line injected instructions), strips code
        fences/braces used to break out of the template, drops common injection
        phrases, and hard-caps length.
        """
        import re
        s = str(value or "")
        s = s.replace("`", "").replace("{", "").replace("}", "")
        s = re.sub(r"\s+", " ", s).strip()
        s = re.sub(
            r"(?i)\b(ignore|disregard|forget)\b[^.]*\b(previous|above|prior|instructions?|prompt)\b",
            "[removed]",
            s,
        )
        s = re.sub(r"(?i)\bsystem\s*:|\bassistant\s*:|\buser\s*:", "", s)
        if len(s) > max_len:
            s = s[:max_len].rstrip()
        return s

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
            # Build prompt via Jinja2 template. Sanitize user-controlled free-text
            # fields to blunt prompt-injection before they reach the LLM.
            final_prompt = self.prompt_builder.build({
                **user_data,
                "user_name": self._sanitize_prompt_text(user_data.get("user_name", ""), max_len=60),
                "allergies": self._sanitize_prompt_text(user_data.get("allergies", ""), max_len=200),
                "dietary_restrictions": self._sanitize_prompt_text(user_data.get("dietary_restrictions", ""), max_len=200),
                "meal_count": meal_count,
                "snack_count": snack_count,
            })

            # Call AI and parse
            plan_output = self.ai.generate(final_prompt)
            # Attach request id to generation metadata
            if self.last_openai_request_id:
                generation_metadata.setdefault("generation_parameters", {})
                generation_metadata["generation_parameters"]["openai_request_id"] = self.last_openai_request_id
            
            # Add generation metadata
            generation_time = (timezone.now() - generation_start).total_seconds()
            generation_metadata["performance_metrics"].update({
                "generation_time": generation_time,
                "success": True,
                "raw_output_length": 0,
                "parsed_meals_count": len(plan_output.plan)
            })
            
            # Add metadata to output
            plan_output.generation_metadata = generation_metadata
            
            # Log successful generation
            self.logger.info(
                f"Successfully generated diet plan for user {self.user.id}",
                extra={
                    "user_id": self.user.id,
                    "generation_id": self.generation_id,
                    "meal_count": meal_count,
                    "snack_count": snack_count,
                    "generation_time": generation_time,
                    "meals_generated": len(plan_output.plan)
                }
            )
            
            return plan_output
            
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

    def save_plan_to_database(self, plan_output: DietPlanOutput, meal_count: int, snack_count: int = 0, start_date: Optional[str] = None):
        """
        Delegate persistence to the DietPersistenceService.
        """
        service = DietPersistenceService(self.user)
        return service.save_plan(plan_output, meal_count, snack_count, start_date)

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
        return self.user.resolve_fitness_goal()

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
            # Optional side effect: swallowing this silently is what made the
            # surrounding failures invisible in logs. Control flow is unchanged.
            logger.debug('suppressed non-fatal error', exc_info=True)
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