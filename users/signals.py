# users/signals.py (FIXED)
from django.db.models.signals import post_save
from django.dispatch import receiver
from users.models import CustomUser
from diet.tasks import generate_ai_diet_plan

@receiver(post_save, sender=CustomUser)
def create_diet_plan(sender, instance, created, **kwargs):
    if created:
        from diet.models import UserFoodPreference
        UserFoodPreference.objects.get_or_create(user=instance)
        # Trigger the diet app's GPT-based async plan generation
        generate_ai_diet_plan.delay(instance.id)
        # NOTE: All diet plan logic is handled by the diet app's GPT system.