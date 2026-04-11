"""
diet_analyzer.py — Pre-computes diet adherence insights.

Analyzes meal completion patterns to detect adherence trends,
macro gaps, and weakest meals.
"""

import logging
from datetime import timedelta
from django.db.models import Avg, Count, Q
from django.utils import timezone

logger = logging.getLogger(__name__)


class DietAnalyzer:
    """Compute diet pattern insights for a user."""

    def analyze(self, user) -> dict:
        from diet.models import DietPlan, DailyProgress, Meal

        plan = DietPlan.objects.filter(user=user, is_active=True).first()
        if not plan:
            return {"has_active_plan": False}

        today = timezone.localdate()
        two_weeks_ago = today - timedelta(days=14)

        # --- Adherence trend ---
        progress_qs = DailyProgress.objects.filter(
            user=user, diet_plan=plan, date__gte=two_weeks_ago,
        )

        week1 = progress_qs.filter(
            date__lt=today - timedelta(days=7),
        )
        week2 = progress_qs.filter(
            date__gte=today - timedelta(days=7),
        )

        def _avg_adherence(qs):
            total = 0
            count = 0
            for dp in qs:
                if dp.target_calories > 0:
                    pct = dp.calories_consumed / dp.target_calories * 100
                    total += max(0, 100 - abs(pct - 100))
                    count += 1
            return round(total / count, 1) if count else 0

        week1_adherence = _avg_adherence(week1)
        week2_adherence = _avg_adherence(week2)

        if week2_adherence > week1_adherence + 5:
            adherence_trend = "improving"
        elif week2_adherence < week1_adherence - 5:
            adherence_trend = "declining"
        else:
            adherence_trend = "stable"

        # --- Macro gap (avg % difference from target) ---
        macro_avgs = progress_qs.aggregate(
            avg_cal_pct=Avg('calories_consumed'),
            avg_protein=Avg('protein_consumed'),
            avg_carbs=Avg('carbs_consumed'),
            avg_fat=Avg('fat_consumed'),
            avg_target_cal=Avg('target_calories'),
            avg_target_protein=Avg('target_protein'),
            avg_target_carbs=Avg('target_carbs'),
            avg_target_fat=Avg('target_fat'),
        )

        def _gap(actual, target):
            if not target or target == 0:
                return 0
            return round(((actual or 0) / target - 1) * 100, 1)

        macro_gap = {
            "calories": _gap(macro_avgs['avg_cal_pct'], macro_avgs['avg_target_cal']),
            "protein": _gap(macro_avgs['avg_protein'], macro_avgs['avg_target_protein']),
            "carbs": _gap(macro_avgs['avg_carbs'], macro_avgs['avg_target_carbs']),
            "fat": _gap(macro_avgs['avg_fat'], macro_avgs['avg_target_fat']),
        }

        # --- Weakest meal type (lowest completion rate) ---
        meal_completion = (
            Meal.objects.filter(
                diet_plan=plan, date__gte=two_weeks_ago,
            )
            .values('meal_type')
            .annotate(
                total=Count('id'),
                completed=Count('id', filter=Q(is_completed=True)),
            )
        )

        weakest_meal = None
        lowest_rate = 101
        for mc in meal_completion:
            rate = (mc['completed'] / mc['total'] * 100) if mc['total'] else 100
            if rate < lowest_rate:
                lowest_rate = rate
                weakest_meal = mc['meal_type']

        # --- Calorie variance ---
        cal_values = list(
            progress_qs.values_list('calories_consumed', flat=True)
        )
        calorie_variance = 0
        if cal_values:
            mean = sum(cal_values) / len(cal_values)
            calorie_variance = round(
                (sum((x - mean) ** 2 for x in cal_values) / len(cal_values)) ** 0.5
            )

        return {
            "has_active_plan": True,
            "adherence_trend": adherence_trend,
            "week1_adherence_pct": week1_adherence,
            "week2_adherence_pct": week2_adherence,
            "macro_gap": macro_gap,
            "weakest_meal": weakest_meal,
            "weakest_meal_completion_pct": round(lowest_rate, 1) if weakest_meal else None,
            "calorie_variance_std": calorie_variance,
        }
