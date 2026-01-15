"""
Achievement Signals - Auto-trigger achievements on user events.

This module connects to Django signals from various apps to automatically
check and award achievements when users complete activities.
"""

import logging
from django.db.models.signals import post_save
from django.dispatch import receiver

logger = logging.getLogger(__name__)


# =============================================================================
# SOCIAL SIGNALS - Post, Follow, Challenge
# =============================================================================

@receiver(post_save, sender='social.Post')
def on_post_created(sender, instance, created, **kwargs):
    """Trigger achievement check when a post is created."""
    if created:
        from achievements.engine import trigger_achievement_check
        try:
            trigger_achievement_check(
                instance.author, 
                'post_created',
                post_id=instance.id,
                post_type=instance.post_type
            )
        except Exception as e:
            logger.error(f"Error checking achievements on post creation: {e}")


@receiver(post_save, sender='social.UserFollow')
def on_user_followed(sender, instance, created, **kwargs):
    """Trigger achievement check when a user follows someone."""
    if created:
        from achievements.engine import trigger_achievement_check
        try:
            # Check achievements for the person who got a new follower
            trigger_achievement_check(
                instance.following,
                'follower_gained'
            )
            # Check achievements for the person who followed someone
            trigger_achievement_check(
                instance.follower,
                'user_followed'
            )
        except Exception as e:
            logger.error(f"Error checking achievements on follow: {e}")


@receiver(post_save, sender='social.ChallengeParticipation')
def on_challenge_joined(sender, instance, created, **kwargs):
    """Trigger achievement check when a user joins a challenge."""
    if created:
        from achievements.engine import trigger_achievement_check
        try:
            trigger_achievement_check(
                instance.user,
                'challenge_joined',
                challenge_id=instance.challenge_id
            )
        except Exception as e:
            logger.error(f"Error checking achievements on challenge join: {e}")


# =============================================================================
# ANALYTICS SIGNALS - User Activity
# =============================================================================

@receiver(post_save, sender='analytics.UserActivity')
def on_user_activity(sender, instance, created, **kwargs):
    """Trigger achievement check on any tracked user activity."""
    if created and instance.user:
        from achievements.engine import trigger_achievement_check, ACTIVITY_TO_CATEGORIES
        
        activity_type = instance.activity_type
        
        # Only trigger for activity types we track
        if activity_type in ACTIVITY_TO_CATEGORIES:
            try:
                trigger_achievement_check(
                    instance.user,
                    activity_type,
                    activity_id=instance.id,
                    metadata=instance.metadata
                )
            except Exception as e:
                logger.error(f"Error checking achievements on activity {activity_type}: {e}")


# =============================================================================
# ANALYTICS SIGNALS - Goal Completion
# =============================================================================

@receiver(post_save, sender='analytics.UserGoal')
def on_goal_updated(sender, instance, **kwargs):
    """Trigger achievement check when a goal is completed."""
    if instance.status == 'completed':
        from achievements.engine import trigger_achievement_check
        try:
            trigger_achievement_check(
                instance.user,
                'goal_completed',
                goal_id=instance.id,
                goal_type=instance.goal_type
            )
        except Exception as e:
            logger.error(f"Error checking achievements on goal completion: {e}")


# =============================================================================
# POST-LIKE SIGNALS
# =============================================================================

@receiver(post_save, sender='social.PostLike')
def on_post_liked(sender, instance, created, **kwargs):
    """Trigger achievement check when a post receives a like."""
    if created:
        from achievements.engine import trigger_achievement_check
        try:
            # Check achievements for the post author (total_likes_received)
            trigger_achievement_check(
                instance.post.author,
                'post_liked',
            )
        except Exception as e:
            logger.error(f"Error checking achievements on post like: {e}")
