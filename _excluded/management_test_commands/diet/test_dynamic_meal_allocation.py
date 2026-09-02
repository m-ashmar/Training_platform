from django.core.management.base import BaseCommand
from django.conf import settings
from diet.models import FoodItem


class Command(BaseCommand):
    help = "Verify dynamic meal allocation toggles and safe fallback pool presence"

    def handle(self, *args, **options):
        enabled = getattr(settings, 'DIET_DYNAMIC_MEAL_ALLOCATION', False)
        self.stdout.write(self.style.SUCCESS(f"DIET_DYNAMIC_MEAL_ALLOCATION={enabled}"))
        required = set([
            'Chicken Breast','Egg Whites','Tofu','Salmon','Tuna','Greek Yogurt','Cottage Cheese',
            'Oats','Brown Rice','Sweet Potato','Whole Grain Bread','Quinoa','Lentils','Chickpeas',
            'Almonds','Walnuts','Olive Oil','Avocado','Flax Seeds','Peanut Butter',
            'Broccoli','Spinach','Zucchini','Carrot','Bell Pepper','Cherry Tomato',
        ])
        existing = set(FoodItem.objects.filter(name__in=required).values_list('name', flat=True))
        missing = sorted(list(required - existing))
        if missing:
            self.stdout.write(self.style.WARNING(f"Missing fallback foods in DB: {', '.join(missing[:10])}{'...' if len(missing)>10 else ''}"))
        else:
            self.stdout.write(self.style.SUCCESS("All fallback foods present."))




