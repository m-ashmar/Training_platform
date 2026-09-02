"""
notifications/channels/fcm.py — FCM delivery channel.

This is the TRANSLATION BOUNDARY for push notifications.

Template resolution happens HERE, inside LanguageContext.for_user_id(),
using the recipient's *current* preferred language (delivery-time policy).
"""

import logging
import importlib

from firebase_admin import messaging
from users.models import DeviceToken
from notifications.models import Notification
from django.db import transaction
from django.utils import timezone
from django.utils.translation import gettext as _
from django.conf import settings

from training_platform.i18n import LanguageContext
from notifications.template_resolver import NotificationTemplateResolver

logger = logging.getLogger(__name__)


# Map event_type strings to their event class module paths.
# Used to look up the typed template from the event class.
EVENT_CLASS_REGISTRY = {
    # Social events
    'post_liked': 'notifications.domain.events.PostLikedEvent',
    'comment_created': 'notifications.domain.events.CommentCreatedEvent',
    'user_followed': 'notifications.domain.events.UserFollowedEvent',
    'achievement_awarded': 'notifications.domain.events.AchievementAwardedEvent',
    'challenge_progress': 'notifications.domain.events.ChallengeProgressEvent',
    # Trainer-client events
    'trainer_assignment_request': 'notifications.domain.trainer_client_events.TrainerAssignmentRequestEvent',
    'trainer_unassignment': 'notifications.domain.trainer_client_events.TrainerUnassignmentEvent',
    'client_request_received': 'notifications.domain.trainer_client_events.ClientRequestReceivedEvent',
    'client_request_approved': 'notifications.domain.trainer_client_events.ClientRequestApprovedEvent',
    'client_request_rejected': 'notifications.domain.trainer_client_events.ClientRequestRejectedEvent',
    'client_request_cancelled': 'notifications.domain.trainer_client_events.ClientRequestCancelledEvent',
    'client_unassigned_trainer': 'notifications.domain.trainer_client_events.ClientUnassignedTrainerEvent',
    # Routine events. These keys are the notif_type strings passed to
    # routine.services.send_notification(); routine.models.Notification was dropped
    # (migration 0013), so there is no model to match against any more.
    # which routine.services.send_notification passes through as event_type).
    'routine_assignment': 'notifications.domain.routine_events.RoutineAssignmentEvent',
    'session_reminder': 'notifications.domain.routine_events.SessionReminderEvent',
    'session_completed': 'notifications.domain.routine_events.SessionCompletedEvent',
    'progress_milestone': 'notifications.domain.routine_events.ProgressMilestoneEvent',
    'custom': 'notifications.domain.routine_events.CustomRoutineNotificationEvent',
}


def _resolve_event_template(event_type: str):
    """
    Look up the event class and return its template.
    Returns None if not found (graceful degradation).
    """
    class_path = EVENT_CLASS_REGISTRY.get(event_type)
    if not class_path:
        return None
    try:
        module_path, class_name = class_path.rsplit('.', 1)
        module = importlib.import_module(module_path)
        event_class = getattr(module, class_name)
        return getattr(event_class, 'template', None)
    except (ImportError, AttributeError) as e:
        logger.error(f"Failed to resolve event template: {event_type}: {e}")
        return None


class FCMChannel:
    CHUNK_SIZE = 500

    @classmethod
    def send(cls, notification: Notification):
        """
        Send notification via FCM with idempotency and metrics.

        TRANSLATION BOUNDARY: Template resolution happens here,
        using the recipient's current preferred language.
        """
        recipient = notification.recipient

        # --- Delivery-time language resolution ---
        with LanguageContext.for_user_id(recipient.id):
            # Reload notification to get latest status
            notification.refresh_from_db()
            current_status = notification.status.get('fcm', {})
            sent_tokens_history = set(current_status.get('sent_tokens', []))

            # Get active tokens
            tokens_qs = DeviceToken.objects.filter(user=recipient, is_active=True).values_list('token', flat=True)
            all_tokens = set(tokens_qs)

            # Filter out already successfully sent tokens (Idempotency)
            tokens_to_send = list(all_tokens - sent_tokens_history)

            if not tokens_to_send:
                logger.info(f"No new tokens to send for {recipient.id}. Completed: {len(sent_tokens_history)}")
                return

            # --- Template resolution at delivery boundary ---
            template = _resolve_event_template(notification.event_type)
            context = notification.metadata.get('context', {})

            title, body = NotificationTemplateResolver.render(
                event_type=notification.event_type,
                template=template,
                context=context,
                recipient_id=recipient.id,
            )

            data = notification.metadata.get('data', {})
            # Ensure data values are strings
            data = {k: str(v) for k, v in data.items()}

            total_success = 0
            total_failure = 0
            newly_sent_tokens = []

            # Chunk tokens
            for i in range(0, len(tokens_to_send), cls.CHUNK_SIZE):
                chunk = tokens_to_send[i:i + cls.CHUNK_SIZE]
                success, failure, success_tokens = cls._send_chunk(chunk, title, body, data)
                total_success += success
                total_failure += failure
                newly_sent_tokens.extend(success_tokens)

            # Update metrics
            try:
                from notifications.metrics import fcm_sent_total, fcm_failed_total
                if total_success > 0:
                    fcm_sent_total.labels(event_type=notification.event_type).inc(total_success)
                if total_failure > 0:
                    fcm_failed_total.labels(event_type=notification.event_type, error_code='mixed').inc(total_failure)
            except ImportError:
                # Optional side effect: swallowing this silently is what made the
                # surrounding failures invisible in logs. Control flow is unchanged.
                logger.debug('suppressed non-fatal error', exc_info=True)

            # Atomic update of notification status
            with transaction.atomic():
                # locking
                notification = Notification.objects.select_for_update().get(id=notification.id)
                status_data = notification.status.get('fcm', {})

                existing_sent = set(status_data.get('sent_tokens', []))
                updated_sent = list(existing_sent.union(set(newly_sent_tokens)))

                attempts = status_data.get('attempts', 0) + 1

                new_status = {
                    'status': 'sent' if total_success > 0 else 'failed',
                    'success_count': status_data.get('success_count', 0) + total_success,
                    'failure_count': status_data.get('failure_count', 0) + total_failure,
                    'sent_tokens': updated_sent,
                    'attempts': attempts,
                    'last_attempt': timezone.now().isoformat()
                }

                notification.status['fcm'] = new_status
                notification.save(update_fields=['status'])

    @classmethod
    def _send_chunk(cls, tokens, title, body, data):
        """
        Send a batch of messages.
        Returns (success_count, failure_count, success_tokens_list)
        """
        success_tokens = []
        try:
            message = messaging.MulticastMessage(
                tokens=tokens,
                notification=messaging.Notification(title=title, body=body),
                data=data
            )
            response = messaging.send_each_for_multicast(message)

            # Handle invalid tokens and track success
            invalid_tokens = []
            for idx, resp in enumerate(response.responses):
                if resp.success:
                    success_tokens.append(tokens[idx])
                else:
                    err_code = resp.exception.code
                    if err_code in ['registration-token-not-registered', 'invalid-registration-token']:
                        invalid_tokens.append(tokens[idx])

            if invalid_tokens:
                cls._mark_tokens_inactive(invalid_tokens)
                try:
                    from notifications.metrics import invalid_tokens_total
                    invalid_tokens_total.labels(platform='android').inc(len(invalid_tokens))
                except ImportError:
                    # Optional side effect: swallowing this silently is what made the
                    # surrounding failures invisible in logs. Control flow is unchanged.
                    logger.debug('suppressed non-fatal error', exc_info=True)

            return response.success_count, response.failure_count, success_tokens

        except Exception as e:
            logger.error(f"FCM Multicast/Chunk error: {e}", exc_info=True)
            return 0, len(tokens), []

    @classmethod
    def _mark_tokens_inactive(cls, tokens):
        """
        Soft delete invalid tokens.
        """
        if not tokens:
            return

        logger.info(f"Marking {len(tokens)} tokens as inactive")
        DeviceToken.objects.filter(token__in=tokens).update(
            is_active=False
        )
