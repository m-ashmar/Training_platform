"""
training_analyzer.py — Pre-computes training insights.

Analyzes workout patterns to detect consistency trends, plateaus,
overtraining risk, and strongest/weakest lifts.
"""

import logging
from datetime import timedelta
from django.db.models import Count, Sum, Avg, F, Q
from django.utils import timezone

logger = logging.getLogger(__name__)


class TrainingAnalyzer:
    """Compute training pattern insights for a user."""

    def analyze(self, user) -> dict:
        from routine.models import (
            WorkoutSession, UserExerciseProgress, ExerciseSetLog,
        )

        now = timezone.now()
        today = timezone.localdate()

        # --- Consistency ---
        # Compare last 2 weeks vs previous 2 weeks
        two_weeks_ago = today - timedelta(days=14)
        four_weeks_ago = today - timedelta(days=28)

        recent_days = UserExerciseProgress.objects.filter(
            user=user, date__gte=two_weeks_ago, skipped=False,
        ).values('date').distinct().count()

        previous_days = UserExerciseProgress.objects.filter(
            user=user, date__gte=four_weeks_ago, date__lt=two_weeks_ago, skipped=False,
        ).values('date').distinct().count()

        if recent_days > previous_days:
            consistency = "improving"
        elif recent_days < previous_days:
            consistency = "declining"
        else:
            consistency = "stable"

        # --- Volume trend (4 weeks) ---
        weekly_volumes = []
        for week in range(4):
            start = today - timedelta(days=(week + 1) * 7)
            end = today - timedelta(days=week * 7)
            vol = ExerciseSetLog.objects.filter(
                user_exercise_progress__user=user,
                date__gte=start, date__lt=end,
            ).aggregate(
                total=Sum(F('weight') * F('reps')),
            )['total'] or 0
            weekly_volumes.append(round(vol))

        volume_trend = "stable"
        if len(weekly_volumes) >= 2:
            if weekly_volumes[0] > weekly_volumes[1] * 1.05:
                volume_trend = "increasing"
            elif weekly_volumes[0] < weekly_volumes[1] * 0.95:
                volume_trend = "decreasing"

        # --- Plateaus: exercises with no weight increase in 3+ weeks ---
        plateaus = []
        three_weeks_ago = today - timedelta(days=21)
        exercises_with_data = (
            ExerciseSetLog.objects.filter(
                user_exercise_progress__user=user,
                date__gte=three_weeks_ago,
            )
            .values('user_exercise_progress__exercise__name')
            .annotate(
                max_weight=Avg('weight'),
                sessions=Count('user_exercise_progress', distinct=True),
            )
            .filter(sessions__gte=3)
        )

        for ex in exercises_with_data:
            ex_name = ex['user_exercise_progress__exercise__name']
            # Check if max weight hasn't changed in recent sessions
            recent_max = ExerciseSetLog.objects.filter(
                user_exercise_progress__user=user,
                user_exercise_progress__exercise__name=ex_name,
                date__gte=today - timedelta(days=7),
            ).aggregate(max_w=Avg('weight'))['max_w'] or 0

            older_max = ExerciseSetLog.objects.filter(
                user_exercise_progress__user=user,
                user_exercise_progress__exercise__name=ex_name,
                date__gte=three_weeks_ago,
                date__lt=today - timedelta(days=7),
            ).aggregate(max_w=Avg('weight'))['max_w'] or 0

            if older_max > 0 and abs(recent_max - older_max) / older_max < 0.02:
                plateaus.append(ex_name)

        # --- Overtraining risk (simple heuristic) ---
        consecutive_days = 0
        for i in range(14):
            day = today - timedelta(days=i)
            worked = UserExerciseProgress.objects.filter(
                user=user, date=day, skipped=False,
            ).exists()
            if worked:
                consecutive_days += 1
            else:
                break

        overtraining_risk = min(100, consecutive_days * 15)  # 7+ days = 100

        # --- Strongest / weakest lifts by volume ---
        muscle_volumes = (
            ExerciseSetLog.objects.filter(
                user_exercise_progress__user=user,
                date__gte=four_weeks_ago,
            )
            .values(muscle=F('user_exercise_progress__exercise__target_muscle'))
            .annotate(total_vol=Sum(F('weight') * F('reps')))
            .order_by('-total_vol')
        )
        muscles = [m for m in muscle_volumes if m['total_vol']]
        strongest = [m['muscle'] for m in muscles[:3]] if muscles else []
        weakest = [m['muscle'] for m in muscles[-3:]] if len(muscles) >= 3 else []

        return {
            "consistency": consistency,
            "recent_training_days": recent_days,
            "previous_training_days": previous_days,
            "volume_trend": volume_trend,
            "weekly_volumes": weekly_volumes,
            "plateaus": plateaus[:5],
            "overtraining_risk": overtraining_risk,
            "consecutive_training_days": consecutive_days,
            "strongest_muscles": strongest,
            "weakest_muscles": weakest,
        }
