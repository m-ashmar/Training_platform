"""
users/signals.py — user lifecycle side effects.

Registered from users.apps.UsersConfig.ready(). Previously this module was never
imported by any AppConfig, so none of these receivers were connected.
"""
import logging

from django.db.models.signals import post_save
from django.dispatch import receiver

from users.models import CustomUser

logger = logging.getLogger(__name__)


@receiver(post_save, sender=CustomUser)
def create_user_food_preference(sender, instance, created, **kwargs):
    """
    Ensure every client has a UserFoodPreference row so the diet engine has a
    preference container to read from.

    NOTE: this deliberately does NOT kick off AI diet-plan generation. At
    post_save time a brand-new user has no height/weight/age/gender, so
    calculate_daily_calories() raises ValueError and the task would fail and
    retry 3x — per signup, at OpenAI cost, for an account that is still inactive
    pending OTP verification. Plan generation is user-triggered via
    /api/diet/generate/ (async), /generate-sync/, or /generate-rule-based/ once
    the profile is complete.
    """
    if not created:
        return
    if getattr(instance, 'user_type', None) != 'client':
        return
    try:
        from diet.models import UserFoodPreference
        UserFoodPreference.objects.get_or_create(user=instance)
    except Exception as e:
        logger.error(f"Failed to create UserFoodPreference for user {instance.id}: {e}")
