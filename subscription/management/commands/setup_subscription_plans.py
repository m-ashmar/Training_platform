from django.core.management.base import BaseCommand
from subscription.models import SubscriptionPlan
from decimal import Decimal


class Command(BaseCommand):
    help = 'Create default subscription plans for the platform'

    def handle(self, *args, **options):
        self.stdout.write('Creating default subscription plans...')
        
        # Free Plan (Limited Access)
        free_plan, created = SubscriptionPlan.objects.get_or_create(
            name='Free Plan',
            defaults={
                'plan_type': 'basic',
                'description': 'Basic access with limited features. Perfect for trying out the platform.',
                'price': Decimal('0.00'),
                'duration_days': 30,
                'has_diet_access': True,
                'has_routine_access': False,
                'has_challenges_access': False,
                'has_ai_advice': False,
                'has_priority_support': False,
                'max_meals_per_day': 1,
                'max_routines': 1,
            }
        )
        
        if created:
            self.stdout.write(self.style.SUCCESS(f'✓ Created Free Plan'))
        else:
            self.stdout.write(self.style.WARNING(f'⚠ Free Plan already exists'))
        
        # Basic Plan
        basic_plan, created = SubscriptionPlan.objects.get_or_create(
            name='Basic Plan',
            defaults={
                'plan_type': 'basic',
                'description': 'Essential features for beginners. Includes diet planning and basic routines.',
                'price': Decimal('1000.00'),  # 1000 SYP
                'duration_days': 30,
                'has_diet_access': True,
                'has_routine_access': True,
                'has_challenges_access': False,
                'has_ai_advice': False,
                'has_priority_support': False,
                'max_meals_per_day': 3,
                'max_routines': 3,
            }
        )
        
        if created:
            self.stdout.write(self.style.SUCCESS(f'✓ Created Basic Plan'))
        else:
            self.stdout.write(self.style.WARNING(f'⚠ Basic Plan already exists'))
        
        # Premium Plan
        premium_plan, created = SubscriptionPlan.objects.get_or_create(
            name='Premium Plan',
            defaults={
                'plan_type': 'premium',
                'description': 'Advanced features with AI-powered diet advice and unlimited access.',
                'price': Decimal('2500.00'),  # 2500 SYP
                'duration_days': 30,
                'has_diet_access': True,
                'has_routine_access': True,
                'has_challenges_access': True,
                'has_ai_advice': True,
                'has_priority_support': False,
                'max_meals_per_day': 5,
                'max_routines': 10,
            }
        )
        
        if created:
            self.stdout.write(self.style.SUCCESS(f'✓ Created Premium Plan'))
        else:
            self.stdout.write(self.style.WARNING(f'⚠ Premium Plan already exists'))
        
        # Professional Plan
        pro_plan, created = SubscriptionPlan.objects.get_or_create(
            name='Professional Plan',
            defaults={
                'plan_type': 'pro',
                'description': 'Complete access with priority support and unlimited features.',
                'price': Decimal('5000.00'),  # 5000 SYP
                'duration_days': 30,
                'has_diet_access': True,
                'has_routine_access': True,
                'has_challenges_access': True,
                'has_ai_advice': True,
                'has_priority_support': True,
                'max_meals_per_day': 0,  # Unlimited
                'max_routines': 0,  # Unlimited
            }
        )
        
        if created:
            self.stdout.write(self.style.SUCCESS(f'✓ Created Professional Plan'))
        else:
            self.stdout.write(self.style.WARNING(f'⚠ Professional Plan already exists'))
        
        self.stdout.write(self.style.SUCCESS('Subscription plans setup completed!'))
        
        # Display summary
        self.stdout.write('\n📋 Subscription Plans Summary:')
        self.stdout.write('=' * 50)
        
        plans = SubscriptionPlan.objects.filter(is_active=True).order_by('price')
        for plan in plans:
            features = []
            if plan.has_diet_access:
                features.append('Diet Planning')
            if plan.has_routine_access:
                features.append('Routines')
            if plan.has_challenges_access:
                features.append('Challenges')
            if plan.has_ai_advice:
                features.append('AI Advice')
            if plan.has_priority_support:
                features.append('Priority Support')
            
            self.stdout.write(f'\n{plan.name}:')
            self.stdout.write(f'  Price: {plan.price} SYP')
            self.stdout.write(f'  Features: {", ".join(features)}')
            self.stdout.write(f'  Meals/Day: {plan.max_meals_per_day if plan.max_meals_per_day > 0 else "Unlimited"}')
            self.stdout.write(f'  Routines: {plan.max_routines if plan.max_routines > 0 else "Unlimited"}')
