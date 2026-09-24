"""
data_collector.py — keeps an anonymous copy of an answered turn for training.

Only while the person has "help us improve" on, and only in the anonymous form the
owner decided on 2026-09-24 (`ai_assistant/anonymise.py`): no account, chat or message
link, a profile of bands and codes, text without direct identifiers, keyed by a pseudonym
that only the server secret produces, and deleted after `AI_TRAINING_RETENTION_DAYS`.
"""

import logging

from django.db import transaction

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
        """Keep an anonymous copy of one answered turn, if the person opted in.

        `session` is accepted for the caller's convenience and is NOT stored: a copy has
        no link to the chat. `message`/`message_id` (the assistant reply) is stored only
        as its keyed pseudonym, `reply_key`, so a later 👍/👎 reaches the copy.
        """
        from ai_assistant.models import AITrainingData

        # Consent gate. Read from the DATABASE, not from `user`: the socket authenticates
        # once and keeps its user instance for the whole connection, so the in-memory
        # flag is whatever it was at connect time, and a person who switched "help us
        # improve" off (`POST /api/ai/training-consent/`) would have gone on being
        # recorded until they reconnected (BACKEND_BUGS B-084). A cheap read first, so a
        # person who never opted in costs no snapshot; the decisive read is under the
        # lock.
        if not has_training_consent(user):
            return

        context = self._build_context_snapshot(user)

        try:
            with transaction.atomic():
                # The person's row, locked, and the consent read again under the lock.
                # Withdrawing takes the same lock before it deletes the copies already
                # kept (`ai_assistant.views.AITrainingConsentView`), so a turn answered
                # while the switch went off either lands first and is deleted with the
                # rest, or reads False here and writes nothing.
                if not has_training_consent(user, lock=True):
                    return
                self._write_row(AITrainingData, user, message, message_id, context,
                                user_message, tools_called, tool_results, ai_response,
                                response_tokens, response_latency_ms)
        except Exception as e:
            logger.exception(f"Failed to log training data: {e}")

    def _write_row(self, AITrainingData, user, message, message_id, context,
                   user_message, tools_called, tool_results, ai_response,
                   response_tokens, response_latency_ms):
        from ai_assistant.anonymise import (anonymise_turn, person_names,
                                            reply_key_for, subject_key_for)
        from ai_assistant.models import training_copy_deadline

        reply = message if message is not None else message_id
        AITrainingData.objects.create(
            subject_key=subject_key_for(user),
            # The feedback path finds the copy by this; without it a 👍 given after the
            # copy is written would label nothing.
            reply_key=reply_key_for(reply) if reply is not None else None,
            consented=True,
            retain_until=training_copy_deadline(),
            user_context_snapshot=context,
            **anonymise_turn(person_names(user), user_message=user_message,
                             ai_response=ai_response, tools_called=tools_called,
                             tool_results=tool_results),
            response_tokens=response_tokens,
            response_latency_ms=response_latency_ms,
            # The tap may have landed before this write.
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
        """The person's state at the time of the turn, as bands and codes only
        (`anonymise.SNAPSHOT_KEYS`): never an id, a name, a measurement or free text.
        The routines are counted, not named (a trainer names a client's routine)."""
        from ai_assistant.anonymise import anonymous_profile, calorie_band

        snapshot = anonymous_profile(user)

        try:
            from routine.models import Routine
            snapshot["active_routines"] = Routine.objects.filter(
                assigned_to=user, is_active=True).count()
        except Exception:
            logger.debug('suppressed non-fatal error', exc_info=True)

        try:
            from diet.models import DietPlan
            # The plan covering today, as the tools read it (SIDE_BUGS S-060).
            plan = DietPlan.governing(user).first()
            if plan:
                snapshot["diet_goal"] = DietPlan.normalise_goal(plan.goal, default=None)
                snapshot["daily_calories_band"] = calorie_band(plan.daily_calories)
        except Exception:
            # Optional side effect: swallowing this silently is what made the
            # surrounding failures invisible in logs. Control flow is unchanged.
            logger.debug('suppressed non-fatal error', exc_info=True)

        return snapshot
