from django.core.management.base import BaseCommand
from subscription.models import SubscriptionPlan


class Command(BaseCommand):
    help = 'Create default subscription plans for the platform'

    def handle(self, *args, **options):
        # The catalogue lives in `subscription/default_plans.py`, shared with
        # `bootstrap_platform`. It finds a plan by its `catalogue_key` (adopting one from
        # before the key by its ENGLISH name), and writes the catalogue only into an
        # empty table (SIDE_BUGS S-236). It used to be
        # `get_or_create(name=...)`, and `name` is translated, so under an Arabic locale
        # the lookup went to `name_ar`, found nothing and wrote a second set of plans
        # (the trap `setup_diet_templates` documents). It also writes the Arabic name and
        # description, and a free plan switched OFF — see that module for why.
        from subscription.default_plans import ensure_default_plans

        self.stdout.write('Creating default subscription plans...')
        for plan, outcome in ensure_default_plans():
            if outcome == 'created':
                self.stdout.write(self.style.SUCCESS(f'✓ Created {plan.name_en}'))
                if not plan.is_active:
                    self.stdout.write(self.style.WARNING(
                        f'  {plan.name_en} is free and was created INACTIVE: a free '
                        f'subscription is never activated yet, so it is not offered'))
            elif outcome == 'arabic added':
                self.stdout.write(self.style.WARNING(
                    f'⚠ {plan.name_en} already exists; its empty Arabic was filled in'))
            elif outcome == 'missing':
                self.stdout.write(self.style.WARNING(
                    f'⚠ {plan.name_en} not found and NOT re-created: this database has '
                    f'plans, and they are the operator\'s (SIDE_BUGS S-236)'))
            elif outcome.startswith('failed'):
                self.stdout.write(self.style.ERROR(f'✗ {plan.name_en} {outcome}'))
            else:
                self.stdout.write(self.style.WARNING(f'⚠ {plan.name_en} already exists'))

        self.stdout.write(self.style.SUCCESS('Subscription plans setup completed!'))

        # Display summary
        self.stdout.write('\n📋 Subscription Plans Summary:')
        self.stdout.write('=' * 50)
        
        plans = SubscriptionPlan.objects.filter(is_active=True).order_by('price', 'id')
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
            self.stdout.write(f'  Price: {plan.price} {plan.currency}')
            self.stdout.write(f'  Features: {", ".join(features)}')
            self.stdout.write(f'  Meals/Day: {plan.max_meals_per_day if plan.max_meals_per_day > 0 else "Unlimited"}')
            self.stdout.write(f'  Routines: {plan.max_routines if plan.max_routines > 0 else "Unlimited"}')
