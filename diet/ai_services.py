from langchain.chains import LLMChain
from langchain.prompts import PromptTemplate
from langchain.output_parsers import PydanticOutputParser
from langchain_openai import ChatOpenAI
from pydantic import BaseModel, Field
from typing import List
import json
from django.conf import settings

# Define strict output schema
class AIIngredient(BaseModel):
    name: str = Field(description="Standard food name")
    quantity: str = Field(description="Amount with unit e.g. '100g' or '2 eggs'")

class AIMeal(BaseModel):
    meal_name: str = Field(description="Mealtime designation")
    description: str = Field(description="1-2 sentence appealing description")
    ingredients: List[AIIngredient]
    total_nutrition: dict = Field(description="Dict with calories, protein, carbs, fat")

class DietPlanOutput(BaseModel):
    plan: List[AIMeal]

class DietGenerator:
    def __init__(self, user):
        self.user = user
        self.llm = ChatOpenAI(
            model="gpt-3.5-turbo-1106",
            temperature=0.3,
            api_key=settings.OPENAI_API_KEY
        )
        self.parser = PydanticOutputParser(pydantic_object=DietPlanOutput)
        
        self.prompt_template = """
        You're a Michelin-star chef nutritionist creating a personalized meal plan for {user_name}.
        
        **User Profile**
        - BMI: {bmi}
        - BMR: {bmr}
        - Daily Target: {calories} kcal
        - Preferences: {likes}
        - Dislikes: {dislikes}
        - Allergies: {allergies}
        
        **Constraints**
        - {meal_count} meals/day
        - Macronutrient balance: Protein {protein_ratio}%, Carbs {carb_ratio}%, Fat {fat_ratio}%
        - Use common ingredients only
        - Strictly validate nutrition facts
        
        {format_instructions}
        """
    
    def _get_user_data(self):
        preferences = UserFoodPreference.objects.get(user=self.user)
        return {
            "user_name": self.user.username,
            "bmi": self.user.calculate_bmi(),
            "bmr": self.user.calculate_bmr(),
            "calories": self.user.calculate_daily_calories(),
            "likes": ", ".join([f.name for f in preferences.liked_foods.all()]),
            "dislikes": ", ".join([f.name for f in preferences.disliked_foods.all()]),
            "allergies": preferences.allergies,
            "protein_ratio": 30,
            "carb_ratio": 50,
            "fat_ratio": 20,
            "format_instructions": self.parser.get_format_instructions()
        }
    
    def generate_plan(self, meal_count=3):
        prompt = PromptTemplate(
            template=self.prompt_template,
            input_variables=["meal_count"],
            partial_variables=self._get_user_data()
        )
        
        chain = LLMChain(llm=self.llm, prompt=prompt)
        raw_output = chain.invoke({"meal_count": meal_count})
        return self.parser.parse(raw_output)