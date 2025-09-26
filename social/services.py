"""
Achievement Service for automatic awarding of achievements.

This service monitors user activities and automatically awards achievements
when users meet the defined criteria/standards.
"""

import logging
from datetime import datetime, timedelta
from django.utils import timezone
from django.db.models import Q, Count, Sum, F
from django.contrib.contenttypes.models import ContentType
from django.db import transaction

from users.models import CustomUser
from social.models import Achievement, UserAchievement, Post, UserFollow, Notification
from analytics.models import UserActivity, PerformanceMetric, UserGoal


logger = logging.getLogger(__name__)


class AchievementService:
    """
    Service for checking and awarding achievements automatically.
    """
    
    @classmethod
    def check_and_award_achievements(cls, user, activity_type=None, **kwargs):
        """
        Check if user meets criteria for any achievements and award them.
        
        Args:
            user: User instance
            activity_type: Type of activity that triggered this check
            **kwargs: Additional context data
        """
        try:
            achievements = Achievement.objects.filter(is_active=True)
            if activity_type:
                # Filter achievements relevant to the activity type
                achievements = cls._filter_achievements_by_activity(
                    achievements, activity_type
                )
            
            for achievement in achievements:
                cls._check_and_award_single_achievement(user, achievement, **kwargs)
                
        except Exception as e:
            logger.error(f"Error checking achievements for user {user.id}: {e}")
    
    @classmethod
    def _filter_achievements_by_activity(cls, achievements, activity_type):
        """Filter achievements that might be relevant to the activity type."""
        activity_mappings = {
            'workout_completed': ['workout', 'streak', 'milestone'],
            'meal_logged': ['diet'],
            'post_created': ['social'],
            'user_followed': ['social'],
            'challenge_joined': ['challenge'],
            'goal_completed': ['milestone'],
            'login': ['streak'],
        }
        
        relevant_categories = activity_mappings.get(activity_type, [])
        if relevant_categories:
            return achievements.filter(category__in=relevant_categories)
        return achievements
    
    @classmethod
    def _check_and_award_single_achievement(cls, user, achievement, **kwargs):
        """Check and award a single achievement if criteria is met."""
        # Skip if user already has this achievement
        if UserAchievement.objects.filter(
            user=user, achievement=achievement
        ).exists():
            return
        
        criteria = achievement.criteria
        criteria_type = criteria.get('type')
        
        # Check if user meets the achievement criteria
        meets_criteria = cls._evaluate_criteria(user, criteria, **kwargs)
        
        if meets_criteria:
            cls._award_achievement(user, achievement, **kwargs)
    
    @classmethod
    def _evaluate_criteria(cls, user, criteria, **kwargs):
        """Evaluate if user meets the achievement criteria."""
        criteria_type = criteria.get('type')
        target = criteria.get('target')
        condition = criteria.get('condition', 'greater_than_or_equal')
        
        # Get current user value for the criteria type
        current_value = cls._get_user_metric_value(user, criteria_type, criteria)
        
        if current_value is None:
            return False
        
        # Evaluate condition
        return cls._evaluate_condition(current_value, target, condition)
    
    @classmethod
    def _get_user_metric_value(cls, user, criteria_type, criteria):
        """Get the current metric value for the user."""
        try:
            if criteria_type == 'workout_count':
                return UserActivity.objects.filter(
                    user=user, activity_type='workout_completed'
                ).count()
            
            elif criteria_type == 'meal_count':
                return UserActivity.objects.filter(
                    user=user, activity_type='meal_logged'
                ).count()
            
            elif criteria_type == 'post_count':
                return Post.objects.filter(author=user).count()
            
            elif criteria_type == 'follower_count':
                return UserFollow.objects.filter(following=user).count()
            
            elif criteria_type == 'total_likes_received':
                return Post.objects.filter(author=user).aggregate(
                    total_likes=Sum('likes_count')
                )['total_likes'] or 0
            
            elif criteria_type == 'challenge_joined_count':
                from social.models import ChallengeParticipation
                return ChallengeParticipation.objects.filter(user=user).count()
            
            elif criteria_type == 'challenge_wins':
                # This would need implementation based on challenge completion logic
                return 0  # Placeholder
            
            elif criteria_type == 'goals_completed':
                return UserGoal.objects.filter(
                    user=user, status='completed'
                ).count()
            
            elif criteria_type == 'workout_streak':
                return cls._calculate_workout_streak(user)
            
            elif criteria_type == 'weight_loss':
                return cls._calculate_weight_loss(user)
            
            elif criteria_type == 'total_distance':
                unit = criteria.get('unit', 'km')
                return cls._calculate_total_distance(user, unit)
            
            elif criteria_type == 'calorie_tracking_streak':
                return cls._calculate_calorie_tracking_streak(user)
            
            elif criteria_type == 'perfect_days_streak':
                return cls._calculate_perfect_days_streak(user)
            
            elif criteria_type in ['late_night_workout', 'early_morning_workout']:
                return cls._check_time_based_workout(user, criteria_type)
            
        except Exception as e:
            logger.error(f"Error calculating metric {criteria_type}: {e}")
            return None
        
        return 0
    
    @classmethod
    def _evaluate_condition(cls, current_value, target, condition):
        """Evaluate if current value meets the target based on condition."""
        # Handle None values
        if current_value is None or target is None:
            return False
            
        if condition == 'greater_than_or_equal':
            return current_value >= target
        elif condition == 'equal':
            return current_value == target
        elif condition == 'greater_than':
            return current_value > target
        elif condition == 'custom':
            return current_value  # For boolean-like custom conditions
        return False
    
    @classmethod
    def _calculate_workout_streak(cls, user):
        """Calculate current workout streak for user."""
        today = timezone.now().date()
        streak = 0
        current_date = today
        
        while True:
            has_workout = UserActivity.objects.filter(
                user=user,
                activity_type='workout_completed',
                timestamp__date=current_date
            ).exists()
            
            if has_workout:
                streak += 1
                current_date -= timedelta(days=1)
            else:
                break
        
        return streak
    
    @classmethod
    def _calculate_weight_loss(cls, user):
        """Calculate total weight loss from starting weight."""
        weight_metrics = PerformanceMetric.objects.filter(
            user=user, metric_type='weight'
        ).order_by('recorded_at')
        
        if weight_metrics.count() < 2:
            return 0
        
        starting_weight = weight_metrics.first().value
        current_weight = weight_metrics.last().value
        
        return max(0, starting_weight - current_weight)
    
    @classmethod
    def _calculate_total_distance(cls, user, unit='km'):
        """Calculate total distance covered in workouts."""
        # This would need integration with workout tracking
        # For now, return placeholder
        return 0
    
    @classmethod
    def _calculate_calorie_tracking_streak(cls, user):
        """Calculate consecutive days of calorie tracking."""
        today = timezone.now().date()
        streak = 0
        current_date = today
        
        while True:
            has_calorie_log = UserActivity.objects.filter(
                user=user,
                activity_type='meal_logged',
                timestamp__date=current_date
            ).exists()
            
            if has_calorie_log:
                streak += 1
                current_date -= timedelta(days=1)
            else:
                break
        
        return streak
    
    @classmethod
    def _calculate_perfect_days_streak(cls, user):
        """Calculate streak of perfect days (all goals completed)."""
        # This would need implementation based on daily goal tracking
        return 0  # Placeholder
    
    @classmethod
    def _check_time_based_workout(cls, user, criteria_type):
        """Check if user has workout at specific times."""
        if criteria_type == 'late_night_workout':
            # Check for workouts between 10 PM and 6 AM
            return UserActivity.objects.filter(
                user=user,
                activity_type='workout_completed',
                timestamp__time__gte='22:00'
            ).exists() or UserActivity.objects.filter(
                user=user,
                activity_type='workout_completed',
                timestamp__time__lt='06:00'
            ).exists()
        
        elif criteria_type == 'early_morning_workout':
            # Check for workouts before 6 AM
            return UserActivity.objects.filter(
                user=user,
                activity_type='workout_completed',
                timestamp__time__lt='06:00'
            ).exists()
        
        return False
    
    @classmethod
    def _award_achievement(cls, user, achievement, **kwargs):
        """Award achievement to user and send notification."""
        try:
            with transaction.atomic():
                # Create user achievement record
                user_achievement = UserAchievement.objects.create(
                    user=user,
                    achievement=achievement,
                    progress_data=kwargs
                )
                
                # Send achievement notification
                cls._send_achievement_notification(user, achievement)
                
                logger.info(
                    f"Awarded achievement '{achievement.name}' "
                    f"to user {user.username}"
                )
                
                return user_achievement
                
        except Exception as e:
            logger.error(f"Error awarding achievement {achievement.id}: {e}")
            return None
    
    @classmethod
    def _send_achievement_notification(cls, user, achievement):
        """Send notification to user about new achievement."""
        try:
            Notification.objects.create(
                recipient=user,
                notification_type='achievement',
                title='Achievement Unlocked! 🏆',
                message=f'You earned the "{achievement.name}" achievement! '
                       f'+{achievement.points} points'
            )
        except Exception as e:
            logger.error(f"Error sending achievement notification: {e}")
    
    @classmethod
    def bulk_check_achievements_for_user(cls, user):
        """
        Manually trigger achievement check for all criteria for a user.
        Useful for retroactive achievement awarding.
        """
        logger.info(f"Running bulk achievement check for user {user.username}")
        cls.check_and_award_achievements(user)
    
    @classmethod
    def get_user_achievement_progress(cls, user, achievement):
        """Get user's progress towards a specific achievement."""
        criteria = achievement.criteria
        current_value = cls._get_user_metric_value(user, criteria.get('type'), criteria)
        target = criteria.get('target', 1)
        
        progress_percentage = min(100, (current_value / target) * 100) if target > 0 else 0
        
        return {
            'achievement_id': achievement.id,
            'achievement_name': achievement.name,
            'current_value': current_value,
            'target_value': target,
            'progress_percentage': progress_percentage,
            'is_completed': progress_percentage >= 100,
            'is_earned': UserAchievement.objects.filter(
                user=user, achievement=achievement
            ).exists()
        }


# Signal handlers to trigger achievement checks
def trigger_achievement_check(user, activity_type, **kwargs):
    """
    Convenience function to trigger achievement checks.
    This can be called from views, signals, or other services.
    """
    AchievementService.check_and_award_achievements(
        user=user,
        activity_type=activity_type,
        **kwargs
    ) 