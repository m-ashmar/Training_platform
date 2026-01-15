"""
Achievement Engine - Central processing for achievement system.

This engine handles all achievement logic including:
- Evaluating user progress towards achievements
- Awarding achievements when criteria are met
- Sending notifications for earned achievements
"""

import logging
from datetime import timedelta
from typing import Optional, Dict, Any, List

from django.utils import timezone
from django.db.models import Sum, Count
from django.db import transaction
from django.contrib.contenttypes.models import ContentType

from users.models import CustomUser

logger = logging.getLogger(__name__)


# =============================================================================
# ACTIVITY TYPE MAPPINGS
# Fixed mappings from actual activity types to achievement categories
# =============================================================================

ACTIVITY_TO_CATEGORIES = {
    # Routine/Workout activities
    'routine_completed': ['workout', 'streak', 'milestone'],
    'exercise_completed': ['workout'],
    
    # Diet activities
    'meal_completed': ['diet'],
    'diet_plan_generated': ['diet'],
    
    # Social activities
    'post_created': ['social'],
    'user_followed': ['social'],
    'follower_gained': ['social'],
    
    # Challenge activities
    'challenge_joined': ['challenge'],
    'challenge_completed': ['challenge', 'milestone'],
    
    # Goal activities
    'goal_completed': ['milestone'],
    
    # Login/streak activities
    'login': ['streak'],
}


class AchievementEngine:
    """
    Central engine for processing achievements.
    """

    @classmethod
    def check_and_award(cls, user: CustomUser, event_type: str = None, **context) -> List[Dict]:
        """
        Check if user meets criteria for achievements and award them.
        
        Args:
            user: User to check achievements for
            event_type: Type of event that triggered this check
            **context: Additional context data (e.g., workout details)
            
        Returns:
            List of newly awarded achievements
        """
        from achievements.models import Achievement, UserAchievement
        
        awarded = []
        
        try:
            # Get relevant achievements to check
            achievements = Achievement.objects.filter(is_active=True)
            
            if event_type and event_type in ACTIVITY_TO_CATEGORIES:
                categories = ACTIVITY_TO_CATEGORIES[event_type]
                achievements = achievements.filter(category__in=categories)
            
            # Check each achievement
            for achievement in achievements:
                # Skip if already earned
                if UserAchievement.objects.filter(
                    user=user, achievement=achievement
                ).exists():
                    continue
                
                # Evaluate criteria
                if cls._evaluate_criteria(user, achievement, **context):
                    result = cls._award_achievement(user, achievement, **context)
                    if result:
                        awarded.append(result)
        
        except Exception as e:
            logger.error(f"Error checking achievements for user {user.id}: {e}")
        
        return awarded

    @classmethod
    def _evaluate_criteria(cls, user: CustomUser, achievement, **context) -> bool:
        """Evaluate if user meets the achievement criteria."""
        criteria = achievement.criteria
        criteria_type = criteria.get('type')
        target = criteria.get('target', 1)
        condition = criteria.get('condition', 'gte')
        
        # Get current value for the criteria type
        current_value = cls._get_metric_value(user, criteria_type, criteria, **context)
        
        if current_value is None:
            return False
        
        # Evaluate condition
        return cls._check_condition(current_value, target, condition)

    @classmethod
    def _get_metric_value(cls, user: CustomUser, criteria_type: str, 
                          criteria: dict, **context) -> Optional[float]:
        """Get the current metric value for the user."""
        from analytics.models import UserActivity, PerformanceMetric, UserGoal
        from social.models import Post, UserFollow, ChallengeParticipation
        
        try:
            # Workout counts - use CORRECT activity types
            if criteria_type == 'workout_count':
                return UserActivity.objects.filter(
                    user=user, 
                    activity_type__in=['routine_completed', 'exercise_completed']
                ).count()
            
            # Meal counts - use CORRECT activity type
            elif criteria_type == 'meal_count':
                return UserActivity.objects.filter(
                    user=user, 
                    activity_type='meal_completed'
                ).count()
            
            # Social metrics
            elif criteria_type == 'post_count':
                return Post.objects.filter(author=user).count()
            
            elif criteria_type == 'follower_count':
                return UserFollow.objects.filter(following=user).count()
            
            elif criteria_type == 'total_likes_received':
                result = Post.objects.filter(author=user).aggregate(
                    total_likes=Sum('likes_count')
                )
                return result['total_likes'] or 0
            
            # Challenge metrics
            elif criteria_type == 'challenge_joined_count':
                return ChallengeParticipation.objects.filter(user=user).count()
            
            elif criteria_type == 'challenge_wins':
                return ChallengeParticipation.objects.filter(
                    user=user, 
                    status='completed',
                    rank=1
                ).count()
            
            # Goal metrics
            elif criteria_type == 'goals_completed':
                return UserGoal.objects.filter(
                    user=user, 
                    status='completed'
                ).count()
            
            # Streak calculations
            elif criteria_type == 'workout_streak':
                return cls._calculate_workout_streak(user)
            
            elif criteria_type == 'calorie_tracking_streak':
                return cls._calculate_calorie_streak(user)
            
            # Weight/body metrics
            elif criteria_type == 'weight_loss':
                return cls._calculate_weight_loss(user)
            
            # Time-based workouts
            elif criteria_type == 'late_night_workout':
                return cls._check_late_night_workout(user)
            
            elif criteria_type == 'early_morning_workout':
                return cls._check_early_morning_workout(user)
            
            # Distance tracking
            elif criteria_type == 'total_distance':
                return cls._calculate_total_distance(user, criteria.get('unit', 'km'))
            
            # Perfect days
            elif criteria_type == 'perfect_days_streak':
                return cls._calculate_perfect_days_streak(user)
        
        except Exception as e:
            logger.error(f"Error calculating metric {criteria_type}: {e}")
            return None
        
        return 0

    @classmethod
    def _check_condition(cls, current: float, target: float, condition: str) -> bool:
        """Check if current value meets target based on condition."""
        if current is None or target is None:
            return False
        
        if condition in ('gte', 'greater_than_or_equal'):
            return current >= target
        elif condition in ('gt', 'greater_than'):
            return current > target
        elif condition in ('eq', 'equal'):
            return current == target
        elif condition == 'custom':
            # For boolean-like custom conditions
            return bool(current)
        
        return False

    @classmethod
    def _calculate_workout_streak(cls, user: CustomUser) -> int:
        """Calculate consecutive days with workouts."""
        from analytics.models import UserActivity
        
        today = timezone.now().date()
        streak = 0
        current_date = today
        
        while True:
            has_workout = UserActivity.objects.filter(
                user=user,
                activity_type__in=['routine_completed', 'exercise_completed'],
                timestamp__date=current_date
            ).exists()
            
            if has_workout:
                streak += 1
                current_date -= timedelta(days=1)
            else:
                break
        
        return streak

    @classmethod
    def _calculate_calorie_streak(cls, user: CustomUser) -> int:
        """Calculate consecutive days with meal logging."""
        from analytics.models import UserActivity
        
        today = timezone.now().date()
        streak = 0
        current_date = today
        
        while True:
            has_meal = UserActivity.objects.filter(
                user=user,
                activity_type='meal_completed',
                timestamp__date=current_date
            ).exists()
            
            if has_meal:
                streak += 1
                current_date -= timedelta(days=1)
            else:
                break
        
        return streak

    @classmethod
    def _calculate_weight_loss(cls, user: CustomUser) -> float:
        """Calculate total weight loss from starting weight."""
        from analytics.models import PerformanceMetric
        
        weight_metrics = PerformanceMetric.objects.filter(
            user=user, 
            metric_type='weight'
        ).order_by('recorded_at')
        
        if weight_metrics.count() < 2:
            return 0
        
        starting = weight_metrics.first().value
        current = weight_metrics.last().value
        
        return max(0, starting - current)

    @classmethod
    def _check_late_night_workout(cls, user: CustomUser) -> bool:
        """Check if user has exercised between 10 PM and 6 AM."""
        from analytics.models import UserActivity
        from datetime import time
        
        # Check for workouts after 10 PM
        late = UserActivity.objects.filter(
            user=user,
            activity_type__in=['routine_completed', 'exercise_completed'],
            timestamp__time__gte=time(22, 0)
        ).exists()
        
        # Check for workouts before 6 AM
        early = UserActivity.objects.filter(
            user=user,
            activity_type__in=['routine_completed', 'exercise_completed'],
            timestamp__time__lt=time(6, 0)
        ).exists()
        
        return late or early

    @classmethod
    def _check_early_morning_workout(cls, user: CustomUser) -> bool:
        """Check if user has exercised before 6 AM."""
        from analytics.models import UserActivity
        from datetime import time
        
        return UserActivity.objects.filter(
            user=user,
            activity_type__in=['routine_completed', 'exercise_completed'],
            timestamp__time__lt=time(6, 0)
        ).exists()

    @classmethod
    def _calculate_total_distance(cls, user: CustomUser, unit: str = 'km') -> float:
        """Calculate total distance from workout activities."""
        from analytics.models import PerformanceMetric
        
        result = PerformanceMetric.objects.filter(
            user=user,
            metric_type='distance_run'
        ).aggregate(total=Sum('value'))
        
        return result['total'] or 0

    @classmethod
    def _calculate_perfect_days_streak(cls, user: CustomUser) -> int:
        """Calculate streak of days where all goals were completed."""
        from analytics.models import UserGoal
        
        # This is a placeholder - would need daily goal tracking
        return 0

    @classmethod
    @transaction.atomic
    def _award_achievement(cls, user: CustomUser, achievement, **context) -> Optional[Dict]:
        """Award achievement to user and send notification."""
        from achievements.models import UserAchievement
        from social.models import Notification
        
        try:
            # Create user achievement
            user_achievement = UserAchievement.objects.create(
                user=user,
                achievement=achievement,
                progress_data=context
            )
            
            # Send notification
            Notification.objects.create(
                recipient=user,
                notification_type='achievement',
                title='Achievement Unlocked! 🏆',
                message=f'You earned the "{achievement.name}" achievement! +{achievement.points} points',
                content_type=ContentType.objects.get_for_model(achievement),
                object_id=achievement.id
            )
            
            logger.info(f"Awarded '{achievement.name}' to user {user.username}")
            
            return {
                'achievement_id': achievement.id,
                'achievement_name': achievement.name,
                'points': achievement.points,
                'earned_at': user_achievement.earned_at.isoformat()
            }
        
        except Exception as e:
            logger.error(f"Error awarding achievement {achievement.id}: {e}")
            return None

    @classmethod
    def get_user_progress(cls, user: CustomUser, achievement) -> Dict:
        """Get user's progress towards a specific achievement."""
        criteria = achievement.criteria
        current_value = cls._get_metric_value(
            user, 
            criteria.get('type'), 
            criteria
        )
        target = criteria.get('target', 1)
        
        progress_pct = min(100.0, (current_value / target) * 100) if target > 0 else 0
        
        from achievements.models import UserAchievement
        is_earned = UserAchievement.objects.filter(
            user=user, 
            achievement=achievement
        ).exists()
        
        return {
            'achievement_id': achievement.id,
            'achievement_name': achievement.name,
            'current_value': current_value,
            'target_value': target,
            'progress_percentage': progress_pct,
            'remaining': max(0, target - (current_value or 0)),
            'is_earned': is_earned
        }

    @classmethod
    def bulk_check_for_user(cls, user: CustomUser) -> List[Dict]:
        """
        Check all achievements for a user.
        Useful for retroactive achievement awarding.
        """
        logger.info(f"Running bulk achievement check for user {user.username}")
        return cls.check_and_award(user)

    @classmethod
    def get_user_stats(cls, user: CustomUser) -> Dict:
        """Get comprehensive achievement stats for a user."""
        from achievements.models import Achievement, UserAchievement
        
        earned = UserAchievement.objects.filter(user=user).select_related('achievement')
        
        # Calculate stats
        total_points = sum(ua.achievement.points for ua in earned)
        
        # Category breakdown
        categories = {}
        for category, _ in Achievement.ACHIEVEMENT_CATEGORIES:
            categories[category] = earned.filter(achievement__category=category).count()
        
        # Rank among all users
        from django.db.models import Sum as DjSum
        user_points = total_points
        users_with_more = UserAchievement.objects.values('user').annotate(
            total=DjSum('achievement__points')
        ).filter(total__gt=user_points).count()
        
        return {
            'total_points': total_points,
            'total_achievements': earned.count(),
            'rank': users_with_more + 1,
            'categories': categories,
            'recent': [
                {
                    'name': ua.achievement.name,
                    'earned_at': ua.earned_at.isoformat()
                }
                for ua in earned.order_by('-earned_at')[:5]
            ]
        }


# Convenience function for other apps
def trigger_achievement_check(user: CustomUser, event_type: str, **context):
    """
    Convenience function to trigger achievement checks.
    Call this from views, signals, or other services.
    
    Example:
        trigger_achievement_check(user, 'routine_completed', routine_id=123)
    """
    return AchievementEngine.check_and_award(user, event_type, **context)
