# diet/services.py
import pulp
from users.models import CustomUser
import random  
import pandas as pd
from collections import defaultdict
from django.db import transaction
from diet.models import UserFoodPreference  , Meal , MealComponent , FoodItem ,FoodCategory ,DietPlan
import traceback
from datetime import date , timedelta

# diet/services.py



class DietOptimizer:
    def __init__(self, user):
        print(f"\n=== INITIALIZING DIET OPTIMIZER FOR {user.email} ===")
        self.user = user
        self.preferences, _ = UserFoodPreference.objects.get_or_create(user=user)
        print(f"Preferences loaded: {self.preferences}")
        self.foods = self._get_qualified_foods()
        print(f"Found {len(self.foods)} qualified foods")
        self.used_foods = defaultdict(set)
        self._validate_food_supply()
        
    
    
    
    def _validate_food_supply(self):
        """Check if selected foods can theoretically meet requirements"""
        print("\n=== FOOD SUPPLY VALIDATION ===")
        
        # Calculate maximum possible from each category
        max_protein = sum(
            self._get_max_grams(row) * float(row['protein_per_gram'])
            for _, row in self.foods.iterrows() 
            if row['category__is_protein']
        )
        
        max_carbs = sum(
            self._get_max_grams(row) * float(row['carbs_per_gram'])
            for _, row in self.foods.iterrows()
            if row['category__is_carb']
        )
        
        max_fat = sum(
            self._get_max_grams(row) * float(row['fat_per_gram'])
            for _, row in self.foods.iterrows()
            if row['category__is_fat']
        )
        
        max_calories = sum(
            self._get_max_grams(row) * row['calories_per_gram']
            for _, row in self.foods.iterrows()
        )

        print(f"Max Possible Protein: {max_protein:.1f}g (Required: 100g)")
        print(f"Max Possible Carbs: {max_carbs:.1f}g (Required: 150g)") 
        print(f"Max Possible Fat: {max_fat:.1f}g (Required: 30g)")
        print(f"Max Possible Calories: {max_calories:.1f} (Required: {self.user.calculate_daily_calories()*0.95:.1f})")

        # Check constraints
        issues = []
        if max_protein < 100:
            issues.append(f"Protein deficiency ({max_protein:.1f}g < 100g)")
        if max_carbs < 150:
            issues.append(f"Carb deficiency ({max_carbs:.1f}g < 150g)")
        if max_fat < 30:
            issues.append(f"Fat deficiency ({max_fat:.1f}g < 30g)")
        if max_calories < self.user.calculate_daily_calories()*0.95:
            issues.append(f"Calorie deficiency ({max_calories:.1f} < {self.user.calculate_daily_calories()*0.95:.1f})")

        if issues:
            msg = "Insufficient food supply:\n- " + "\n- ".join(issues)
            print(f"VALIDATION FAILED: {msg}")
            raise ValueError(msg)
            
            
            
            
        

        
        
        
        
    def _get_qualified_foods(self):
        print("\n[DEBUG] Gathering qualified foods...")
        qs = (self.preferences.protein_choices.all() |
              self.preferences.carb_choices.all() |
              self.preferences.fat_choices.all()).exclude(id__in=self.preferences.disliked_foods.all())
        
        print(f"Raw food count: {qs.count()}")
        print(f"Protein choices: {self.preferences.protein_choices.count()}")
        print(f"Carb choices: {self.preferences.carb_choices.count()}")
        print(f"Fat choices: {self.preferences.fat_choices.count()}")
        
        df = pd.DataFrame.from_records(qs.values(
            'id', 'name', 'calories', 'protein', 'carbs', 'fat','calories_per_gram', 'protein_per_gram','carbs_per_gram', 'fat_per_gram',
            'category__is_protein', 'category__is_carb', 'category__is_fat',
            'category__meal_times'
        ))
        print(f"Qualified foods dataframe shape: {df.shape}")
        
        print("\nSelected Foods Breakdown:")
        proteins = df.loc[df['category__is_protein'], 'name'].tolist()
        carbs = df.loc[df['category__is_carb'], 'name'].tolist()
        fats = df.loc[df['category__is_fat'], 'name'].tolist()
    
        print(f"- Proteins ({len(proteins)}): {proteins}")
        print(f"- Carbs ({len(carbs)}): {carbs}")
        print(f"- Fats ({len(fats)}): {fats}")
    
        return df
    
    
    def optimize(self):
        print("\n=== STARTING OPTIMIZATION ===")
        try:
            goal = self.user.dietplan_set.last().goal
            total_calories = self.user.calculate_daily_calories()
            print(f"Using existing plan settings | Goal: {goal} | Calories: {total_calories}")
        except AttributeError:
            goal = 'Maintain'
            total_calories = 2000
            print(f"Using default settings | Goal: {goal} | Calories: {total_calories}")

        try:
            print("\n[PHASE 1] Attempting primary optimization")
            prob, food_vars = self._create_lp_problem(total_calories, goal)
            prob.solve(pulp.PULP_CBC_CMD(msg=False))
            print(f"Solver status: {pulp.LpStatus[prob.status]}")

            if pulp.LpStatus[prob.status] != 'Optimal':
                print("!!! PRIMARY OPTIMIZATION FAILED !!!")
                raise ValueError("No solution with current constraints")
                
            print("Primary optimization successful")
            return self._package_solution(food_vars)

        except ValueError as e:
            print(f"\n[ERROR] Primary optimization failed: {str(e)}")
            print("Attempting fallback optimization...")
            return self._fallback_optimize(total_calories)
    
    def _create_lp_problem(self, total_calories, goal):
        print(f"\n[DEBUG] Creating LP Problem | Calories: {total_calories} | Goal: {goal}")
        prob = pulp.LpProblem("DietOptimization", pulp.LpMinimize)
        
        print("Initializing food variables...")
        food_vars = pulp.LpVariable.dicts("Portions", range(len(self.foods)), lowBound=0, cat='Integer')
        
        print("Setting up objective function...")
        # Prioritize larger portions and variety
        prob += (
            -0.7 * pulp.lpSum(  # Negative coefficient to MAXIMIZE portions
                food_vars[i] * (
                    2.0 if self.foods.iloc[i]['category__is_protein'] else 
                    1.5 if self.foods.iloc[i]['category__is_carb'] else 
                    1.0
                ) for i in range(len(self.foods))
            ) +
            0.3 * pulp.lpSum(food_vars.values())  # Secondary gram minimization
        )
        
        print("Adding calorie constraint...")
        prob += pulp.lpSum([
            self.foods.iloc[i]['calories_per_gram'] * 25 * food_vars[i] 
            for i in range(len(self.foods))
        ]) >= total_calories * 0.95
        
        print("Adding strict macro constraints...")
        macro_ratios = self._get_macro_ratios(goal)
        # Protein constraint (min 100g total)
        prob += pulp.lpSum([
            food_vars[i] for i in range(len(self.foods))
            if self.foods.iloc[i]['category__is_protein']
        ]) >= 40
        
        # Carb constraint (min 150g total)
        prob += pulp.lpSum([
            food_vars[i] for i in range(len(self.foods))
            if self.foods.iloc[i]['category__is_carb']
        ]) >= 75
        
        # Fat constraint (min 30g total)
        prob += pulp.lpSum([
            food_vars[i] for i in range(len(self.foods))
            if self.foods.iloc[i]['category__is_fat']
        ]) >= 30
        
        print("Adding portion size constraints...")
        for i in range(len(self.foods)):
            food = self.foods.iloc[i]
            min_grams = self._get_min_grams(food)
            max_grams = self._get_max_grams(food)
            min_portions = max(1, min_grams // 25) 
            max_portions = max_grams // 25
            
            prob += food_vars[i] >= min_portions
            prob += food_vars[i] <= max_portions
           
        
        return prob, food_vars
    
    
    
    
    
    
    

    def _get_macro_ratios(self, goal):
        print(f"\n[DEBUG] Getting macro ratios for goal: {goal}")
        if len(self.foods) < 3:
            print("!!! ERROR: Less than 3 foods in preferences !!!")
            raise ValueError("Select at least 3 foods in your preferences")
        ratios = {
            'Lose': {'protein': 0.4, 'carbs': 0.3, 'fat': 0.3},
            'Maintain': {'protein': 0.3, 'carbs': 0.4, 'fat': 0.3},
            'Gain': {'protein': 0.35, 'carbs': 0.45, 'fat': 0.2}
        }.get(goal, {'protein': 0.3, 'carbs': 0.4, 'fat': 0.3})
        print(f"Macro ratios selected: {ratios}")
        return ratios
    
    
    def _get_min_grams(self, food_data):
        """Minimum grams to consider a valid serving"""
        return 25  # Minimum 25g per food item
    
    
    def _get_max_grams(self, food_data):
        """Realistic maximum grams per food type"""
        if food_data['category__is_protein']:
            return 300  # Max 300g protein/day
        if food_data['category__is_carb']:
            return 400  # Max 400g carbs/day
        return 100  # Max 100g fats/day
    
    def _create_meal_template_constraints(self, prob, food_vars):
        print("\n[DEBUG] Adding meal template constraints")
        proteins = self.foods[self.foods['category__is_protein']].index
        carbs = self.foods[self.foods['category__is_carb']].index
        fats = self.foods[self.foods['category__is_fat']].index

        print(f"Protein items: {len(proteins)}")
        print(f"Carb items: {len(carbs)}")
        print(f"Fat items: {len(fats)}")

        print("Adding minimum food type constraints:")
        prob += (pulp.lpSum(food_vars[i] for i in proteins) >= 2)
        prob += (pulp.lpSum(food_vars[i] for i in carbs) >= 2)
        prob += (pulp.lpSum(food_vars[i] for i in fats) >= 1)
        print("Minimum constraints added: Protein(2), Carbs(2), Fats(1)")
        
    
    
    def _fallback_optimize(self, total_calories):
        print("\n[PHASE 2] Starting fallback optimization")
        print("Relaxing macro constraints to 50% of original values")
        
        prob = pulp.LpProblem("FallbackDiet", pulp.LpMinimize)
        food_vars = pulp.LpVariable.dicts("Food", self.foods.index, lowBound=0 , cat='Continuous')
        
        print("Setting up basic calorie constraint...")
        prob += pulp.lpSum([self.foods.iloc[i]['calories'] * food_vars[i] 
                          for i in self.foods.index]) == total_calories
        
        print("Adding relaxed macro constraints:")
        prob += pulp.lpSum([self.foods.iloc[i]['protein'] * food_vars[i] 
                          for i in self.foods.index]) >= total_calories * 0.15
        prob += pulp.lpSum([self.foods.iloc[i]['carbs'] * food_vars[i] 
                          for i in self.foods.index]) >= total_calories * 0.20
        prob += pulp.lpSum([self.foods.iloc[i]['fat'] * food_vars[i] 
                          for i in self.foods.index]) >= total_calories * 0.05

        prob.solve(pulp.PULP_CBC_CMD(msg=False))
        print(f"Fallback solver status: {pulp.LpStatus[prob.status]}")

        if pulp.LpStatus[prob.status] != 'Optimal':
            print("!!! FALLBACK OPTIMIZATION FAILED !!!")
            print("Possible reasons:")
            print("- Insufficient protein choices" if len(self.preferences.protein_choices.all()) < 2 else "")
            print("- Insufficient carb choices" if len(self.preferences.carb_choices.all()) < 2 else "")
            print("- Insufficient fat choices" if len(self.preferences.fat_choices.all()) < 1 else "")
            print("- Calorie targets impossible with current food selections")
            raise ValueError("Cannot create plan with current preferences")

        print("Fallback optimization successful")
        return self._package_solution(food_vars)
    
    
    
    def _package_solution(self, food_vars):
        print("\n[DEBUG] Packaging solution...")
        valid_foods = []
        
        # 1. Validate and collect food portions
        print("Processing food variables:")
        for idx in self.foods.index:
            food = self.foods.iloc[idx]
            grams = food_vars[idx].value()
           
            
            # More conservative portioning
           
            
            if grams >= 25:
                valid_foods.append((food, grams))
                print(f"- KEPT {food['name']}: {grams}g")
            else:
                print(f"- DISCARDED {food['name']}: {grams}g")


        print(f"[DEBUG] Number of valid foods: {len(valid_foods)}")        
        if not valid_foods:
            print("!!! CRITICAL ERROR: No valid food portions created !!!")
            raise ValueError("No valid food portions could be created")

        print(f"Created {len(valid_foods)} valid food components")
        # 2. Get or create diet plan with duration
        try:
            plan = self.user.dietplan_set.last()
            duration_days = (plan.end_date - plan.start_date).days
            print(f"[DEBUG] Number of valid foods: {len(valid_foods)}")
            print(f"Creating plan for {duration_days} days")
        except AttributeError:
            print("!! Using default 7-day plan !!")
            plan = DietPlan.objects.create(
                user=self.user,
                goal='Maintain',
                daily_calories=2000,
                start_date=date.today(),
                end_date=date.today() + timedelta(days=6)
            )
            print(f"[DEBUG] Plan created with ID: {plan.id}")

        # 3. Create meals with variety and template adherence
        for day in range(duration_days):
            current_date = plan.start_date + timedelta(days=day)
            print(f"\n=== Day {day+1} - {current_date} ===")
            
            # Reset food variety every 3 days
            variety_cycle = 7  # Weekly rotation instead of 3 days
            daily_foods = valid_foods.copy()
             
            random.shuffle(daily_foods)  # Randomize order for variety
            
            # Create meals for each time slot
            self._create_daily_meals(plan, current_date, daily_foods, day)
            
        print(f"\nSuccessfully created diet plan ID: {plan.id}")
        
        return plan
    
    
    
    def _create_daily_meals(self, plan, current_date, daily_foods, day):
        """Create meals with rotation awareness"""
        print(f"\n=== DAY {day+1} - {current_date} ===")
        
        # Get foods used in last 3 days
        excluded_foods = [
            food_id for d in range(max(0, day-2), day)
            for food_id in self.used_foods.get(d, [])
        ]
        
        meal_params = [
            ('Breakfast', 'COMPLETE', 0.25, {'protein': 1, 'carb': 1, 'fat': 1}),
            ('Lunch', 'COMPLETE', 0.35, {'protein': 1, 'carb': 1, 'fat': 1}),
            ('Dinner', 'COMPLETE', 0.30, {'protein': 1, 'carb': 1, 'fat': 1}),
            ('Snack', 'CARB_FAT', 0.10, {'carb': 1, 'fat': 1})
        ]
    
        for meal_time, template, calorie_ratio, components in meal_params:
            print(f"\n[MEAL] Creating {meal_time} ({template})")
            meal = Meal.objects.create(
                diet_plan=plan,
                template=template,
                date=current_date
            )
            
            # Filter available foods by category
            available = {
                'protein': [],
                'carb': [],
                'fat': []
            }
            for food, grams in daily_foods:
                if food['id'] in excluded_foods:
                    continue
                cat = self._get_primary_category(food)
                if cat in available:
                    available[cat].append((food, grams))
            
            # Enforce realistic portions per meal component
            for macro, count in components.items():
                candidates = available.get(macro, [])
                if not candidates:
                    self._add_fallback_component(meal, macro, meal_time)
                    continue
                
                # Select 1 item per macro with realistic quantity
                food_data, max_grams = random.choice(candidates)
                portion_range = self._get_portion_range(macro)
                grams = min(
                    random.randint(portion_range[0], portion_range[1]),
                    max_grams
                )
                
                # Create component
                MealComponent.objects.create(
                    meal=meal,
                    food=FoodItem.objects.get(id=food_data['id']),
                    quantity=grams,
                    meal_time=meal_time
                )
                print(f"Added realistic {grams}g of {food_data['name']} ({macro})")
                
                # Update tracking
                self.used_foods.setdefault(day, set()).add(food_data['id'])
                excluded_foods.append(food_data['id'])
                daily_foods.remove((food_data, max_grams))
    
    
    
    
        


    

    

    def _get_primary_category(self, food_data):
        """Determine a food's main macro category"""
        if food_data['category__is_protein'] == True:
            return 'protein'
        if food_data['category__is_carb'] == True:
            return 'carb'
        if food_data['category__is_fat']== True:
            return 'fat'
        return 'other'
    
    def _get_portion_range(self, macro):
        return {
            'protein': (150, 300),  # 150-300g per protein serving
            'carb': (100, 200),     # 100-200g per carb serving
            'fat': (10, 30)        # 10-30g per fat serving
        }.get(macro, (50, 100))
    
    
    def _add_fallback_components(self, meal, meal_time, needed):
        """Add emergency components from different categories"""
        categories = ['protein', 'carb', 'fat']
        added = 0
        
        for category in categories:
            filter_key = f"category__is_{category}"
            filter_kwargs = {filter_key: True}
            fallback_food = FoodItem.objects.filter(
                **filter_kwargs
    ).exclude(id__in=self.used_foods.get(meal.date, set())).first()
            
            if fallback_food:
                MealComponent.objects.create(
                    meal=meal,
                    food=fallback_food,
                    quantity=50,
                    meal_time=meal_time
                )
                print(f"Added fallback 50g of {fallback_food.name}")
                added += 1
                if added >= needed:
                    break

    
    
    
    
    
    
    
    def _compile_report(self, meal_plan):
        """Structure report based on actual database objects"""
        try:
            goal = meal_plan.goal
        except AttributeError:
            goal = 'No diet plan found'
        
        # Get meals through ORM relationships
        meals = Meal.objects.filter(diet_plan=meal_plan)
        components = MealComponent.objects.filter(meal__diet_plan=meal_plan)
        
        self.report_data = {
            'user': self.user.email,
            'goal': goal,
            'target_calories': meal_plan.daily_calories,
            'status': 'optimal',
            'total_foods': components.count(),
            'total_meals': meals.count(),
            'meal_breakdown': self._calculate_template_stats(meals)
        }

    def _calculate_template_stats(self, meals):
        """Calculate stats from actual Meal objects"""
        breakdown = defaultdict(list)
        
        for meal in meals:
            breakdown[meal.template].append({
                'components': meal.mealcomponent_set.count(),
                'calories': sum(c.food.calories * c.quantity 
                              for c in meal.mealcomponent_set.all())
            })
        
        return {
            template: {
                'count': len(items),
                'total_calories': sum(item['calories'] for item in items),
                'components': sum(item['components'] for item in items)
            }
            for template, items in breakdown.items()
        }
    
    def get_optimization_report(self):
        """Returns structured optimization report"""
        try:
            meal_plan = self.optimize()
            self._compile_report(meal_plan)
            return self.report_data
        except Exception as e:
            tb = traceback.format_exc()
            print(f"\n⚠️ OPTIMIZATION ERROR ⚠️\n{tb}")
            return {
                'status': 'error',
                'message': str(e),
                'traceback': tb
            }
        
        
    
    
    
        
    
    
    # diet/services.py
    