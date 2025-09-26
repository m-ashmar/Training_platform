"""
Management command to set up initial diet plan templates.
"""

from django.core.management.base import BaseCommand
from diet.models import DietPlanTemplate

class Command(BaseCommand):
    help = 'Set up initial diet plan templates'

    def handle(self, *args, **options):
        self.stdout.write('Setting up diet plan templates...')
        
        templates_data = [
            {
                'name': '3 Meals + 1 Snack',
                'description': 'Standard 3 meals with 1 snack - perfect for most users',
                'meals_per_day': 3,
                'snacks_per_day': 1,
                'days_variation': 1
            },
            {
                'name': '4 Meals + 2 Snacks',
                'description': '4 meals with 2 snacks - ideal for active users and muscle building',
                'meals_per_day': 4,
                'snacks_per_day': 2,
                'days_variation': 2
            },
            {
                'name': '5 Meals + 1 Snack',
                'description': '5 meals with 1 snack - for advanced users and bodybuilding',
                'meals_per_day': 5,
                'snacks_per_day': 1,
                'days_variation': 3
            },
            {
                'name': '6 Meals + 2 Snacks',
                'description': '6 meals with 2 snacks - for professional athletes and extreme fitness',
                'meals_per_day': 6,
                'snacks_per_day': 2,
                'days_variation': 4
            },
            {
                'name': '3 Meals Only',
                'description': 'Simple 3 meals per day - for beginners and simple tracking',
                'meals_per_day': 3,
                'snacks_per_day': 0,
                'days_variation': 1
            },
            {
                'name': '4 Meals Only',
                'description': '4 meals per day - balanced approach for moderate activity',
                'meals_per_day': 4,
                'snacks_per_day': 0,
                'days_variation': 2
            }
        ]
        
        created_count = 0
        for template_data in templates_data:
            template, created = DietPlanTemplate.objects.get_or_create(
                name=template_data['name'],
                defaults=template_data
            )
            if created:
                created_count += 1
                self.stdout.write(f'  Created template: {template.name}')
            else:
                self.stdout.write(f'  Template already exists: {template.name}')
        
        self.stdout.write(
            self.style.SUCCESS(f'Successfully set up {created_count} new templates. '
                             f'Total templates: {DietPlanTemplate.objects.count()}')
        ) 