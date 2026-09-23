"""
data_collector.py — Logs AI interactions as training data.

Captures complete interaction snapshots for future model fine-tuning.
Runs as a Celery task to avoid blocking the chat response.
"""

import logging
import time
from django.db import transaction
from django.utils import timezone

logger = logging.getLogger(__name__)


def has_training_consent(user, *, lock: bool = False) -> bool:
    """Whether this person has opted in to their AI chats being kept for training.

    Read from the database every time — never from the instance a caller holds, which
    may be a socket's user loaded at connect. `lock=True` takes the person's row
    `FOR UPDATE` (inside the caller's transaction), the same lock the withdrawal takes.
    Only the person sets the flag (`POST /api/ai/training-consent/`); it is off by
    default and nothing else writes it.
    """
    from users.models import CustomUser

    pk = getattr(user, "pk", None)
    if pk is None:
        return False
    rows = CustomUser.objects.filter(pk=pk)
    if lock:
        rows = rows.select_for_update()
    return bool(rows.values_list("ai_training_consent", flat=True).first())


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
        message=None,
        message_id=None,
        response_tokens: int = 0,
        response_latency_ms: int = 0,
    ):
        """
        Create an AITrainingData record with a full context snapshot.

        This method is called asynchronously via Celery so it doesn't
        block the chat response.
        """
        from django.conf import settings as dj_settings

        from ai_assistant.models import AITrainingData

        # Consent gate. The snapshot below carries injury/condition text, weight and
        # goals — special-category health data — so it is retained only when the user
        # has explicitly opted in. Previously every turn was stored unconditionally.
        # Read from the DATABASE, not from `user`: the socket authenticates once and
        # keeps its user instance for the whole connection, so the in-memory flag is
        # whatever it was at connect time, and a person who switched "help us improve"
        # off (`POST /api/ai/training-consent/`) would have gone on being recorded
        # until they reconnected (BACKEND_BUGS B-084). A cheap read first, so a person
        # who never opted in costs no snapshot; the decisive read is under the lock.
        if not has_training_consent(user):
            return

        # Build user context snapshot
        context = self._build_context_snapshot(user)

        retention_days = getattr(dj_settings, 'AI_TRAINING_RETENTION_DAYS', 365)

        try:
            with transaction.atomic():
                # The person's row, locked, and the consent read again under the lock.
                # Withdrawing takes the same lock before it deletes the rows already
                # kept (`ai_assistant.views.AITrainingConsentView`), so a turn answered
                # while the switch went off either lands first and is deleted with the
                # rest, or reads False here and writes nothing.
                if not has_training_consent(user, lock=True):
                    return
                self._write_row(AITrainingData, user, session, message, message_id,
                                retention_days, context, user_message, tools_called,
                                tool_results, ai_response, response_tokens,
                                response_latency_ms)
        except Exception as e:
            logger.exception(f"Failed to log training data: {e}")

    def _write_row(self, AITrainingData, user, session, message, message_id,
                   retention_days, context, user_message, tools_called, tool_results,
                   ai_response, response_tokens, response_latency_ms):
        from datetime import timedelta

        AITrainingData.objects.create(
            user=user,
            session=session,
            # Either the instance or its id. The caller has the id, because
            # `_save_messages` returns one; without this the FK stayed NULL and the
            # feedback path — which filters on it — matched nothing, for ever.
            **({"message": message} if message is not None
               else {"message_id": message_id}),
            consented=True,
            retain_until=timezone.now() + timedelta(days=retention_days),
            user_context_snapshot=context,
            user_message=user_message,
            tools_called=tools_called,
            tool_results=tool_results,
            ai_response=ai_response,
            response_tokens=response_tokens,
            response_latency_ms=response_latency_ms,
            # The tap may have landed before this background write.
            user_feedback=self._feedback_already_given(message, message_id),
        )

    @staticmethod
    def _feedback_already_given(message, message_id):
        from ai_assistant.models import ChatMessage

        if message is not None:
            return ChatMessage.objects.filter(pk=message.pk).values_list(
                'feedback', flat=True).first()
        if message_id is not None:
            return ChatMessage.objects.filter(pk=message_id).values_list(
                'feedback', flat=True).first()
        return None

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
            # The plan covering today, as the tools read it (SIDE_BUGS S-060).
            plan = DietPlan.governing(user).first()
            if plan:
                snapshot["diet_goal"] = plan.goal
                snapshot["daily_calories"] = plan.daily_calories
        except Exception:
            # Optional side effect: swallowing this silently is what made the
            # surrounding failures invisible in logs. Control flow is unchanged.
            logger.debug('suppressed non-fatal error', exc_info=True)

        return snapshot
