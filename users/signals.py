# users/signals.py (FIXED)
from django.db.models.signals import post_save
from django.dispatch import receiver
from users.models import CustomUser

@receiver(post_save, sender=CustomUser)
def create_diet_plan(sender, instance, created, **kwargs):
    if created:
        from diet.models import UserFoodPreference
        UserFoodPreference.objects.get_or_create(user=instance)
        instance.generate_diet_plan()