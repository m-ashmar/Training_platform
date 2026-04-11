"""
data_collector.py — Logs AI interactions as training data.

Captures complete interaction snapshots for future model fine-tuning.
Runs as a Celery task to avoid blocking the chat response.
"""

import logging
import time
from django.utils import timezone

logger = logging.getLogger(__name__)


class DataCollector:
    """Collects and stores training data from AI chat interactions."""

    def log_interaction(
        self,
        user,
        session,
        user_message: str,
        ai_response: str,
        tools_called: list,
        tool_results: list,
        response_tokens: int = 0,
        response_latency_ms: int = 0,
    ):
        """
        Create an AITrainingData record with a full context snapshot.

        This method is called asynchronously via Celery so it doesn't
        block the chat response.
        """
        from ai_assistant.models import AITrainingData

        # Build user context snapshot
        context = self._build_context_snapshot(user)

        try:
            AITrainingData.objects.create(
                user=user,
                session=session,
                user_context_snapshot=context,
                user_message=user_message,
                tools_called=tools_called,
                tool_results=tool_results,
                ai_response=ai_response,
                response_tokens=response_tokens,
                response_latency_ms=response_latency_ms,
            )
        except Exception as e:
            logger.exception(f"Failed to log training data: {e}")

    def _build_context_snapshot(self, user) -> dict:
        """
        Capture the user's state at interaction time.
        This becomes part of the training dataset for future models.
        """
        snapshot = {
            "user_id": user.id,
            "age": user.age,
            "gender": user.gender,
            "height": user.height,
            "weight": user.weight,
            "activity_level": user.activity_level,
            "goals": user.client_goals or [],
            "injury": user.specific_injury,
            "bmi": user.calculate_bmi(),
        }

        # Active routine info
        try:
            from routine.models import Routine
            active_routines = Routine.objects.filter(
                assigned_to=user, is_active=True,
            ).values_list('name', flat=True)
            snapshot["active_routines"] = list(active_routines)
        except Exception:
            snapshot["active_routines"] = []

        # Active diet info
        try:
            from diet.models import DietPlan
            plan = DietPlan.objects.filter(
                user=user, is_active=True,
            ).first()
            if plan:
                snapshot["diet_goal"] = plan.goal
                snapshot["daily_calories"] = plan.daily_calories
        except Exception:
            pass

        return snapshot
