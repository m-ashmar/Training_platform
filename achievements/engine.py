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

from analytics import activity_types
from users.models import CustomUser

#: Every criteria type `_get_metric_value` can actually compute from data this platform
#: writes. The registry may only ship achievements whose criteria are in here — two of
#: them were not, and could never be earned: `total_distance` read a `PerformanceMetric`
#: type nothing has ever recorded, and `perfect_days_streak` was `return 0` under a
#: comment saying so. `test_every_shipped_achievement_can_be_earned` holds the two lists
#: in step, so adding a definition for an unimplemented metric fails the gate rather than
#: shipping a badge nobody can reach.
IMPLEMENTED_CRITERIA = frozenset({
    "workout_count", "meal_count", "post_count", "follower_count",
    "total_likes_received", "challenge_joined_count", "challenge_wins",
    "goals_completed", "workout_streak", "calorie_tracking_streak", "weight_loss",
    "late_night_workout", "early_morning_workout",
})

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

    # Being liked is a social event, and it was NOT in this map — so
    # `on_post_liked` fell through to the unfiltered branch below and every like
    # scanned the WHOLE achievement catalogue. Measured at 50 definitions: a like cost
    # 352 queries and 279 ms, a follow 817 queries and 704 ms, and a post 354. Roughly
    # seven queries per definition per social write, so growing the badge catalogue
    # degraded every post, like and follow linearly.
    'post_liked': ['social'],
    'comment_created': ['social'],
    'like_given': ['social'],
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
            
            # Which ones this user already holds, in ONE query. This was an `.exists()`
            # per definition, inside a loop over the whole catalogue.
            already = set(
                UserAchievement.objects.filter(user=user)
                .values_list('achievement_id', flat=True))

            # One memo for this whole sweep. `metric_value_cached` exists for exactly
            # this and the award path did not use it: `_evaluate_criteria` computed the
            # metric, and `_record_progress` then computed the SAME metric a second time
            # for the same definition. Twenty badges that all count workouts re-ran the
            # same COUNT forty times.
            metric_cache: dict = {}
            pending_progress: list = []

            # Check each achievement
            for achievement in achievements:
                # Skip if already earned
                if achievement.id in already:
                    continue
                
                # Evaluate criteria
                if cls._evaluate_criteria(user, achievement, metric_cache=metric_cache,
                                          **context):
                    result = cls._award_achievement(user, achievement, **context)
                    if result:
                        awarded.append(result)
                else:
                    # Record how close they are. AchievementProgress had a model, a
                    # serializer, an admin page and an import in achievements/views.py,
                    # and nothing ever wrote a row — so a "3 of 5 workouts" screen was
                    # fully plumbed and always empty.
                    #
                    # COLLECTED, not written one at a time: `update_or_create` per
                    # definition is a SELECT plus a write each, so a 40-badge catalogue
                    # cost ~80 queries on every post, like and follow. One upsert now.
                    row = cls._progress_row(user, achievement, metric_cache=metric_cache,
                                            **context)
                    if row is not None:
                        pending_progress.append(row)

            cls._flush_progress(pending_progress)
        
        except Exception as e:
            logger.error(f"Error checking achievements for user {user.id}: {e}")
        
        return awarded

    @classmethod
    @classmethod
    def _flush_progress(cls, rows) -> None:
        """Write every collected progress row in one statement."""
        if not rows:
            return
        try:
            from .models import AchievementProgress

            AchievementProgress.objects.bulk_create(
                rows,
                update_conflicts=True,
                update_fields=["current_value", "target_value", "progress_percentage"],
                unique_fields=["user", "achievement"],
            )
        except Exception:
            # Progress is a display nicety; it must never break awarding.
            logger.debug("could not write achievement progress", exc_info=True)

    @classmethod
    def _progress_row(cls, user: CustomUser, achievement, metric_cache=None,
                      **context):
        """An unsaved AchievementProgress for this definition, or None."""
        try:
            from .models import AchievementProgress

            criteria = getattr(achievement, "criteria", None) or {}
            criteria_type = criteria.get("type")
            # `target` is the key _evaluate_criteria uses; matching it keeps the progress
            # bar and the award decision measuring the same thing.
            target = criteria.get("target")
            if not criteria_type or not target:
                return None

            # The SAME memo the evaluator just used. This recomputed the metric a
            # second time for every definition, so each one cost its query twice.
            current = cls.metric_value_cached(user, criteria, cache=metric_cache)
            if current is None:
                return None

            target = float(target)
            if target <= 0:
                return None
            pct = max(0.0, min(100.0, (float(current) / target) * 100.0))

            return AchievementProgress(
                user=user, achievement=achievement,
                current_value=float(current), target_value=target,
                progress_percentage=round(pct, 2),
            )
        except Exception:
            # Progress is a display nicety; it must never break awarding.
            logger.debug("could not record progress for achievement %s",
                         getattr(achievement, "pk", "?"), exc_info=True)

    @classmethod
    def _evaluate_criteria(cls, user: CustomUser, achievement, metric_cache=None,
                           **context) -> bool:
        """Evaluate if user meets the achievement criteria."""
        criteria = achievement.criteria
        criteria_type = criteria.get('type')
        target = criteria.get('target', 1)
        condition = criteria.get('condition', 'gte')
        
        # Get current value for the criteria type, memoised across this sweep.
        current_value = cls.metric_value_cached(user, criteria, cache=metric_cache)
        
        if current_value is None:
            return False
        
        # Evaluate condition
        return cls._check_condition(current_value, target, condition)

    @classmethod
    def metric_value_cached(cls, user: CustomUser, criteria: dict, cache=None):
        """`_get_metric_value` with a per-request memo.

        A catalogue of achievements shares a handful of metric types — twenty of them
        may all count workouts, each with a different target — so computing the metric
        once per achievement re-ran the same COUNT twenty times. The key is everything
        the metric actually reads: its type, and the unit that `total_distance` takes.
        """
        criteria_type = criteria.get('type')
        key = (criteria_type, criteria.get('unit', 'km'))
        if cache is not None and key in cache:
            return cache[key]
        value = cls._get_metric_value(user, criteria_type, criteria)
        if cache is not None:
            cache[key] = value
        return value

    @classmethod
    def _get_metric_value(cls, user: CustomUser, criteria_type: str, 
                          criteria: dict, **context) -> Optional[float]:
        """Get the current metric value for the user."""
        from analytics.models import UserActivity, PerformanceMetric, UserGoal
        from social.models import Post, UserFollow, ChallengeParticipation
        
        try:
            # Workout counts - use CORRECT activity types
            if criteria_type == 'workout_count':
                # Finished workouts, plus the days trained without one being finished:
                # a session left open and abandoned overnight, or sets synced with no
                # session. Those days wrote a row per SET until B-071 made the log one
                # row per workout; without this they stopped counting at all (M1).
                finished = UserActivity.objects.filter(
                    user=user, activity_type__in=activity_types.WORKOUT)
                finished_days = {t.date() for t in finished.values_list('timestamp', flat=True)}
                return finished.count() + len(cls._set_log_days(user) - finished_days)
            
            # MEALS, not meal components. Analytics writes one `meal_completed` row
            # per component (`session_id="meal-component-<pk>"`, analytics/signals.py),
            # so a four-food meal counted as four: "log your first meal" fired on the
            # first FOOD, and "log 100 meals" at about 25. The component id is in the
            # session_id, so the meal is recovered by counting distinct meals behind
            # them rather than the rows themselves.
            elif criteria_type == 'meal_count':
                from diet.models import MealComponent

                component_ids = [
                    int(sid.rsplit("-", 1)[-1])
                    for sid in UserActivity.objects.filter(
                        user=user, activity_type__in=activity_types.MEAL
                    ).values_list('session_id', flat=True)
                    if sid and sid.rsplit("-", 1)[-1].isdigit()
                ]
                if not component_ids:
                    return 0
                return (MealComponent.objects
                        .filter(id__in=component_ids)
                        .values('meal_id').distinct().count())
            
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
                # A WIN, not a lead. `rank` is rewritten on every `update_progress`,
                # `join` and `leave` while a challenge runs, so counting `rank=1`
                # without asking whether the challenge is over awarded the Challenge
                # Winner badge to whoever happened to be first when any check ran —
                # and a badge is never taken back (SIDE_BUGS S-044). A challenge is over
                # when someone closed it as completed, or when its end date has passed
                # and it was not cancelled: `Challenge.lifecycle_status` as a query.
                # From that moment the standing is frozen (`_update_challenge_rankings`
                # does not re-rank a finished board), so the count cannot move again.
                # Owner, 2026-09-24 (F20): a win also needs the challenge RANKED
                # (counted by the server, created by a trainer or staff — no prize
                # for a self-reported number), its winner SETTLED after the review
                # window (`winner_checked_at`), and the person CLEAR of review. The
                # check that awards it runs at settlement
                # (`social/tasks.py settle_winner`).
                from django.db.models import Q
                from social.models import Challenge

                ended = (Q(challenge__status='completed')
                         | (Q(challenge__end_date__lt=timezone.now())
                            & ~Q(challenge__status__in=Challenge.TERMINAL_STATUSES)))
                # Never for an account that is not active (the owner's decision 8,
                # 2026-09-24): a suspended or erased account holds no place.
                from users.visibility import public_q

                return ChallengeParticipation.objects.filter(
                    ended,
                    public_q('user'),
                    Challenge.ranked_q('challenge__'),
                    user=user,
                    status='completed',
                    rank=1,
                    review_state=ChallengeParticipation.REVIEW_CLEAR,
                    challenge__winner_checked_at__isnull=False,
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

            # `total_distance` and `perfect_days_streak` used to be handled here. Their
            # metrics read a `PerformanceMetric` type nothing writes and a function whose
            # body was `return 0`, so the two achievements that used them could never be
            # earned. Both definitions are gone from the registry; see the note at the
            # end of `achievements/registry.py`.

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

    #: The longest streak this platform will count. A streak is read to award a badge,
    #: and the longest one any badge asks for is well inside this.
    MAX_STREAK_DAYS = 400

    @classmethod
    def _set_log_days(cls, user: CustomUser, since=None) -> set:
        """The days this user logged a set on, whatever became of the session.

        A trained day is a day with logged sets: the activity log holds one row per
        FINISHED workout (BACKEND_BUGS B-071), and a session abandoned overnight or sets
        synced with no session leave no such row — yet those sets "stay, and stay
        counted" (routine/views.py). One query."""
        from routine.models import UserExerciseProgress

        rows = UserExerciseProgress.objects.filter(user=user, set_logs__isnull=False)
        if since is not None:
            rows = rows.filter(date__gte=since)
        return set(rows.values_list('date', flat=True).distinct())

    @classmethod
    def _streak_of(cls, user: CustomUser, kinds, exclude=None, also=()) -> int:
        """Consecutive days, ending today, on which this user did one of `kinds`.

        `exclude`: days that must not count however much was logged on them — the owner's
        rule that a training day closed short breaks the streak.

        ONE query, and the client's own days. It was a `while True` issuing a query per
        consecutive day with no upper bound — a user with a long history paid one
        round-trip per day of it, every time any achievement was evaluated — and it
        counted days in the SERVER's timezone while `subscription.quota` and
        `diet.tasks` resolve "today" from `CustomUser.preferred_timezone`. A client
        outside Asia/Damascus had their allowance reset at their own midnight and their
        streak judged at Damascus midnight, so a workout logged in the gap broke a streak
        the reminder job still believed was alive.
        """
        from zoneinfo import ZoneInfo

        from analytics.models import UserActivity
        from training_platform.user_time import today_for

        today = today_for(user)
        window_start = today - timedelta(days=cls.MAX_STREAK_DAYS)
        try:
            zone = ZoneInfo(getattr(user, "preferred_timezone", None) or "UTC")
        except Exception:
            zone = None

        stamps = UserActivity.objects.filter(
            user=user, activity_type__in=kinds,
            timestamp__date__gte=window_start - timedelta(days=2),
        ).values_list("timestamp", flat=True)
        days = {(t.astimezone(zone) if zone else timezone.localtime(t)).date()
                for t in stamps}
        days |= set(also or ())
        days -= set(exclude or ())

        streak, day = 0, today
        while day in days and streak < cls.MAX_STREAK_DAYS:
            streak += 1
            day -= timedelta(days=1)
        return streak

    @classmethod
    def _calculate_workout_streak(cls, user: CustomUser) -> int:
        """Consecutive days with a workout that finished what it planned.

        A day whose training day was closed with sets still unlogged, or skipped, does
        not count and breaks the streak (the owner, 2026-09-20; routine/outcomes.py).
        Training that belongs to no routine day, and a day on which another routine WAS
        finished in full, still count.
        """
        from routine.outcomes import broken_dates
        from training_platform.user_time import today_for

        window_start = today_for(user) - timedelta(days=cls.MAX_STREAK_DAYS)
        # Days with logged sets count too, finished or not (M1): the log holds one row
        # per FINISHED workout, and a day trained in full and never closed still counts.
        return cls._streak_of(user, activity_types.WORKOUT,
                              exclude=broken_dates(user, since=window_start),
                              also=cls._set_log_days(user, since=window_start))

    @classmethod
    def _calculate_calorie_streak(cls, user: CustomUser) -> int:
        """Consecutive days with a meal recorded."""
        return cls._streak_of(user, activity_types.MEAL)

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
            activity_type__in=activity_types.WORKOUT,
            timestamp__time__gte=time(22, 0)
        ).exists()
        
        # Check for workouts before 6 AM
        early = UserActivity.objects.filter(
            user=user,
            activity_type__in=activity_types.WORKOUT,
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
            activity_type__in=activity_types.WORKOUT,
            timestamp__time__lt=time(6, 0)
        ).exists()

    @classmethod


    @classmethod
    @transaction.atomic
    def _award_achievement(cls, user: CustomUser, achievement, **context) -> Optional[Dict]:
        """Award achievement to user and send notification."""
        from achievements.models import UserAchievement

        try:
            # Create user achievement
            user_achievement = UserAchievement.objects.create(
                user=user,
                achievement=achievement,
                progress_data=context
            )

            # Emit the domain event. The notifications listener persists to the
            # canonical notifications.Notification store and dispatches FCM.
            # (This previously wrote a legacy social.Notification row that no API
            # endpoint reads, so achievement notifications were never delivered.)
            from django.db import transaction as _transaction

            from notifications.domain.dispatcher import emit_event
            from notifications.domain.events import AchievementAwardedEvent

            # Queued on commit, not now. `.delay()` inside an open transaction puts
            # the message on the broker before the row is visible, so a worker can read
            # state that is not there yet — and if the transaction rolls back the
            # notification still goes out, telling the user about something that never
            # happened.
            _transaction.on_commit(lambda: emit_event(AchievementAwardedEvent(
                user_id=user.id,
                achievement_id=achievement.id,
                achievement_name=achievement.name,
                points=achievement.points,
            )))

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

        # `_get_metric_value` returns None on any internal error, and dividing by a
        # target with None on the left is a TypeError -> 500 on the progress endpoint.
        # `_record_progress` already returns early on None; this path did not.
        if current_value is None:
            current_value = 0
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
        
        # Rank among all users — the ones the leaderboard shows: not a suspended or
        # erased account (the owner's decision 8, 2026-09-24).
        from django.db.models import Sum as DjSum
        from users.visibility import public_q
        user_points = total_points
        users_with_more = UserAchievement.objects.filter(public_q('user')).values('user').annotate(
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
