from django.core.management.base import BaseCommand
from subscription.models import SubscriptionPlan, SubscriptionFeature

class Command(BaseCommand):
    help = 'Set up default subscription plans for the training platform'
    
    def handle(self, *args, **options):
        self.stdout.write('Setting up default subscription plans...')
        
        # Create default subscription plans
        plans_data = [
            {
                'name': 'Basic Plan',
                'plan_type': 'basic',
                'description': 'Perfect for beginners. Access to basic diet planning and routine creation.',
                'price': 9.99,
                'duration_days': 30,
                'has_diet_access': True,
                'has_routine_access': True,
                'has_challenges_access': False,
                'has_ai_advice': False,
                'has_priority_support': False,
                'max_meals_per_day': 3,
                'max_routines': 3,
            },
            {
                'name': 'Premium Plan',
                'plan_type': 'premium',
                'description': 'Advanced features for fitness enthusiasts. Full access to diet, routines, and challenges.',
                'price': 19.99,
                'duration_days': 30,
                'has_diet_access': True,
                'has_routine_access': True,
                'has_challenges_access': True,
                'has_ai_advice': True,
                'has_priority_support': False,
                'max_meals_per_day': 5,
                'max_routines': 10,
            },
            {
                'name': 'Professional Plan',
                'plan_type': 'pro',
                'description': 'Complete access for professionals. All features including priority support.',
                'price': 39.99,
                'duration_days': 30,
                'has_diet_access': True,
                'has_routine_access': True,
                'has_challenges_access': True,
                'has_ai_advice': True,
                'has_priority_support': True,
                'max_meals_per_day': 10,
                'max_routines': 50,
            },
            {
                'name': 'Enterprise Plan',
                'plan_type': 'enterprise',
                'description': 'For organizations and teams. Unlimited access with custom features.',
                'price': 99.99,
                'duration_days': 30,
                'has_diet_access': True,
                'has_routine_access': True,
                'has_challenges_access': True,
                'has_ai_advice': True,
                'has_priority_support': True,
                'max_meals_per_day': 0,  # Unlimited
                'max_routines': 0,  # Unlimited
            },
        ]
        
        created_count = 0
        updated_count = 0
        
        for plan_data in plans_data:
            plan, created = SubscriptionPlan.objects.get_or_create(
                name=plan_data['name'],
                defaults=plan_data
            )
            
            if created:
                self.stdout.write(
                    self.style.SUCCESS(f'Created plan: {plan.name}')
                )
                created_count += 1
            else:
                # Update existing plan
                for key, value in plan_data.items():
                    setattr(plan, key, value)
                plan.save()
                self.stdout.write(
                    self.style.WARNING(f'Updated plan: {plan.name}')
                )
                updated_count += 1
        
        # Create default subscription features
        features_data = [
            {
                'name': 'daily_meals',
                'description': 'Daily meal planning and tracking',
            },
            {
                'name': 'routines',
                'description': 'Workout routine creation and management',
            },
            {
                'name': 'challenges',
                'description': 'Fitness challenges and competitions',
            },
            {
                'name': 'ai_advice',
                'description': 'AI-powered fitness and nutrition advice',
            },
            {
                'name': 'priority_support',
                'description': 'Priority customer support',
            },
        ]
        
        for feature_data in features_data:
            feature, created = SubscriptionFeature.objects.get_or_create(
                name=feature_data['name'],
                defaults=feature_data
            )
            
            if created:
                self.stdout.write(
                    self.style.SUCCESS(f'Created feature: {feature.name}')
                )
            else:
                self.stdout.write(
                    self.style.WARNING(f'Feature already exists: {feature.name}')
                )
        
        self.stdout.write(
            self.style.SUCCESS(
                f'Successfully set up subscription plans! '
                f'Created: {created_count}, Updated: {updated_count}'
            )
        ) 