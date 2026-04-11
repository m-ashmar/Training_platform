from django.core.management.base import BaseCommand
from django.contrib.auth import get_user_model
from diet.services.diet_persistence import DietPersistenceService
from diet.ai_models import DietPlanOutput, AIMeal
from datetime import date, timedelta

class Command(BaseCommand):
    help = 'Reproduce Duration Bug'

    def handle(self, *args, **options):
        User = get_user_model()
        user = User.objects.filter(email='verify_fix@example.com').first()
        if not user:
            self.stdout.write(self.style.ERROR("User verify_fix@example.com not found. Run verify_diet_fix first or create user."))
            return

        # Simulate RuleBasedPlanner behavior:
        # It generates Max 3 Meals + Max 1 Snack per day.
        # Even if we request snack_count=2, it generates 1 snack.
        
        # Test Case: Request 7 days, 3 meals, 2 snacks.
        # Generated: 7 days * (3 meals + 1 snack) = 28 meals.
        # Persistence Logic: daily_meals = 3 + 2 = 5.
        # Duration = 28 // 5 = 5. (Should be 7).
        
        # Test Case 2: Request 7 days, 3 meals, 1 snack.
        # Generated: 7 * 4 = 28.
        # Persistence: 4. Duration = 7.
        
        # Test Case 1: Request 7 days, 3 meals, 2 snacks (Simulated View Clamp -> 1 snack)
        # View logic: req_snacks=2 -> snack_count=1.
        # Generator: 4 meals/day. Persistence: 3+1=4.
        # Duration: (7 * 4) // 4 = 7. MATCH.
        self.test_duration(user, req_duration=7, req_meals=3, req_snacks=1, gen_meals_per_day=4)
        self.test_duration(user, req_duration=7, req_meals=3, req_snacks=1, gen_meals_per_day=4)
        self.test_duration(user, req_duration=3, req_meals=3, req_snacks=2, gen_meals_per_day=4)

    def test_duration(self, user, req_duration, req_meals, req_snacks, gen_meals_per_day):
        self.stdout.write(f"\n--- Testing Request: Duration={req_duration}, Meals={req_meals}, Snacks={req_snacks} ---")
        self.stdout.write(f"Generator produces: {gen_meals_per_day} meals/day")
        
        total_gen_meals = req_duration * gen_meals_per_day
        nutrition = {'calories': 500, 'protein': 30, 'carbs': 50, 'fat': 20}
        
        meals = []
        for d in range(req_duration):
            for m in range(gen_meals_per_day):
                meals.append(AIMeal(
                    meal_name=f"Meal {d}-{m}", 
                    description="Desc", 
                    ingredients=[], 
                    total_nutrition=nutrition
                ))
                
        plan_output = DietPlanOutput(plan=meals)
        service = DietPersistenceService(user)
        
        try:
            diet_plan = service.save_plan(
                plan_output=plan_output,
                meal_count=req_meals,
                snack_count=req_snacks,
                start_date=date.today().isoformat()
            )
            
            calc_duration = (diet_plan.end_date - diet_plan.start_date).days + 1
            
            self.stdout.write(f"Created Plan ID: {diet_plan.id}")
            self.stdout.write(f"Start: {diet_plan.start_date}, End: {diet_plan.end_date}")
            self.stdout.write(f"Calculated Duration (Days): {calc_duration}")
            
            if calc_duration == req_duration:
                self.stdout.write(self.style.SUCCESS(f"MATCH: Duration is {calc_duration}"))
            else:
                self.stdout.write(self.style.ERROR(f"MISMATCH: Expected {req_duration}, got {calc_duration}"))
                
            diet_plan.delete()
            
        except Exception as e:
             self.stdout.write(self.style.ERROR(f"Error: {e}"))
