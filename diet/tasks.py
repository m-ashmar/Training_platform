from celery import shared_task
from .ai_services import DietGenerator
from .meal_processor import MealProcessor
from .models import DietPlan, Meal, MealComponent, DailyAdvice
from users.models import CustomUser
from django.utils import timezone
import random

@shared_task
def generate_ai_diet_plan(user_id, meal_count=3):
    user = CustomUser.objects.get(id=user_id)
    generator = DietGenerator(user)
    
    try:
        plan = generator.generate_plan(meal_count)
    except Exception as e:
        # Fallback to rule-based system
        return generate_fallback_plan(user_id, meal_count)
    
    # Create diet plan
    diet_plan = DietPlan.objects.create(
        user=user,
        goal=user.dietplan.goal if hasattr(user, 'dietplan') else 'Maintain',
        daily_calories=user.calculate_daily_calories(),
        start_date=timezone.now().date(),
        end_date=timezone.now().date() + timezone.timedelta(weeks=4),
        generation_strategy='GPT'
    )
    
    # Process meals
    for ai_meal in plan.plan:
        processor = MealProcessor(ai_meal)
        ingredients = processor.resolve_ingredients()
        
        meal = Meal.objects.create(
            diet_plan=diet_plan,
            name=ai_meal.meal_name,
            description=ai_meal.description,
            image_url=processor.generate_meal_image(ingredients),
            is_ai_generated=True
        )
        
        for food, quantity in ingredients:
            MealComponent.objects.create(
                meal=meal,
                food=food,
                quantity=quantity
            )
    
    return f"Generated {len(plan.plan)} meals for {user.email}"

@shared_task
def generate_daily_advice():
    for user in CustomUser.objects.all():
        advice = generate_user_advice(user)
        DailyAdvice.objects.create(
            user=user,
            text=advice['text'],
            context_data=advice['context']
        )

def generate_user_advice(user):
    # Simplified example - expand with LangChain
    return {
        'text': f"Stay hydrated today! Aim for {int(user.weight/30)} cups of water.",
        'context': {'weight': user.weight}
    }