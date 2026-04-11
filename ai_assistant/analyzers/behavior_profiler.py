"""
behavior_profiler.py — Pre-computes user engagement patterns.

Analyzes UserBehaviorEvent data to build a behavioral profile
for the AI to understand user habits and communication preferences.
"""

import logging
from collections import Counter
from datetime import timedelta
from django.db.models import Count
from django.utils import timezone

logger = logging.getLogger(__name__)


class BehaviorProfiler:
    """Build an engagement and behavior profile for a user."""

    def analyze(self, user) -> dict:
        from ai_assistant.models import UserBehaviorEvent, ChatSession

        today = timezone.localdate()
        month_ago = today - timedelta(days=30)

        events = UserBehaviorEvent.objects.filter(
            user=user, created_at__date__gte=month_ago,
        )

        total_events = events.count()
        if total_events == 0:
            return {
                "engagement_level": "new_user",
                "total_events": 0,
                "message": "Not enough data to build a profile.",
            }

        # --- Engagement level ---
        if total_events >= 100:
            engagement = "highly_active"
        elif total_events >= 40:
            engagement = "active"
        elif total_events >= 15:
            engagement = "moderate"
        else:
            engagement = "low"

        # --- Event distribution ---
        event_counts = dict(
            events.values_list('event_type')
            .annotate(c=Count('id'))
            .values_list('event_type', 'c')
        )

        # --- Preferred training time (hour of day) ---
        workout_events = events.filter(
            event_type__in=['workout_completed', 'set_logged'],
        )
        hour_counter = Counter()
        for e in workout_events.values_list('created_at', flat=True):
            hour_counter[e.hour] += 1

        preferred_hour = None
        if hour_counter:
            preferred_hour = hour_counter.most_common(1)[0][0]

        preferred_time = None
        if preferred_hour is not None:
            if preferred_hour < 6:
                preferred_time = "early_morning"
            elif preferred_hour < 12:
                preferred_time = "morning"
            elif preferred_hour < 17:
                preferred_time = "afternoon"
            elif preferred_hour < 21:
                preferred_time = "evening"
            else:
                preferred_time = "night"

        # --- Consistency pattern (active days per week) ---
        active_days = events.values('created_at__date').distinct().count()
        weeks = max(1, 30 / 7)
        days_per_week = round(active_days / weeks, 1)

        if days_per_week >= 5:
            consistency = "very_consistent"
        elif days_per_week >= 3:
            consistency = "consistent"
        elif days_per_week >= 1:
            consistency = "irregular"
        else:
            consistency = "inactive"

        # --- Chat engagement ---
        chat_sessions = ChatSession.objects.filter(
            user=user, created_at__date__gte=month_ago,
        ).count()

        return {
            "engagement_level": engagement,
            "total_events": total_events,
            "event_breakdown": event_counts,
            "preferred_training_time": preferred_time,
            "preferred_training_hour": preferred_hour,
            "consistency_pattern": consistency,
            "active_days_per_week": days_per_week,
            "chat_sessions_this_month": chat_sessions,
        }
