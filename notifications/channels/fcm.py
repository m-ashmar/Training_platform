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
    # Monthly coaching (users/coaching.py).
    'coaching_renewed': 'notifications.domain.trainer_client_events.CoachingRenewedEvent',
    'coaching_renewal_soon': 'notifications.domain.trainer_client_events.CoachingRenewalSoonEvent',
    'coaching_payment_due': 'notifications.domain.trainer_client_events.CoachingPaymentDueEvent',
    'coaching_ended': 'notifications.domain.trainer_client_events.CoachingEndedEvent',
    'coaching_finished': 'notifications.domain.trainer_client_events.CoachingFinishedEvent',
    # Platform plan (subscription/tasks.py send_renewal_reminders).
    'subscription_expiring': 'notifications.domain.subscription_events.SubscriptionExpiringEvent',
    'subscription_expired': 'notifications.domain.subscription_events.SubscriptionExpiredEvent',
    'checkout_expired': 'notifications.domain.subscription_events.CheckoutExpiredEvent',
    # The review queue (subscription.services.payment_service, B-051).
    'payment_needs_review': 'notifications.domain.subscription_events.PaymentNeedsReviewEvent',
    'payment_rejected': 'notifications.domain.subscription_events.PaymentRejectedEvent',
    # Trainer-client chat (chat/events.py). One nudge until the thread is opened.
    'chat_message': 'notifications.domain.chat_events.ChatMessageEvent',
    # Progress (routine/strength.py, routine/tasks.py). Fact-only, no claims.
    'personal_record': 'notifications.domain.progress_events.PersonalRecordEvent',
    'weekly_recap': 'notifications.domain.progress_events.WeeklyRecapEvent',
    'trainer_weekly_digest': 'notifications.domain.progress_events.TrainerWeeklyDigestEvent',
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
                # Leaving a 'failed' or 'partial' status in place here is what kept
                # the hourly drain re-selecting a push nothing can deliver any more: its
                # tokens were deactivated as dead, so this returned early every hour and
                # the row stayed 'failed' for ever, crowding real outage failures out of
                # the drain's window. Settle it: delivered to at least one device, or to
                # none that still exists.
                if current_status.get('status') in cls.RETRYABLE_STATUSES:
                    cls._settle(notification, 'sent' if sent_tokens_history else 'undeliverable')
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
            dead_tokens = []

            # Chunk tokens
            for i in range(0, len(tokens_to_send), cls.CHUNK_SIZE):
                chunk = tokens_to_send[i:i + cls.CHUNK_SIZE]
                success, failure, success_tokens, chunk_dead = cls._send_chunk(chunk, title, body, data)
                total_success += success
                total_failure += failure
                newly_sent_tokens.extend(success_tokens)
                dead_tokens.extend(chunk_dead)

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

                # 'partial' when some device failed for a reason that was not a dead
                # token: those tokens are still active and not in sent_tokens, so a
                # retry reaches exactly them. Recording 'sent' on the first success hid a
                # multi-device user's transient failure from the drain for good.
                if total_failure > len(dead_tokens):
                    outcome = 'partial' if updated_sent else 'failed'
                else:
                    outcome = 'sent' if updated_sent else 'undeliverable'
                new_status = {
                    'status': outcome,
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
        Returns (success_count, failure_count, success_tokens_list, dead_tokens_list)
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
                elif cls._token_is_dead(resp.exception):
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

            return response.success_count, response.failure_count, success_tokens, invalid_tokens

        except Exception as e:
            logger.error(f"FCM Multicast/Chunk error: {e}", exc_info=True)
            return 0, len(tokens), [], []

    #: Statuses the hourly drain retries. 'undeliverable' (no active device left) and
    #: 'sent' are final.
    RETRYABLE_STATUSES = frozenset({'failed', 'partial'})

    #: Code strings for a dead token, consulted ONLY for an exception that did not come
    #: from the Firebase SDK (a stubbed transport, or an older SDK's vocabulary).
    #:
    #: A real SDK exception is judged by its class alone. The generic canonical codes are
    #: not token-specific: `firebase_admin` turns ANY 403 without an FCM error detail
    #: (an IAM denial on the service account) into PermissionDeniedError with code
    #: PERMISSION_DENIED, any 400 (a bad data payload) into InvalidArgumentError with
    #: INVALID_ARGUMENT, and a 404 for a wrong project into NotFoundError with NOT_FOUND.
    #: Matching those codes deactivated every token of every recipient on the first push
    #: after a credential or payload mistake, and nobody received anything again until
    #: each app re-registered.
    DEAD_TOKEN_CODES = frozenset({
        "NOT_FOUND", "UNREGISTERED", "SENDER_ID_MISMATCH",
        "registration-token-not-registered", "invalid-registration-token",
        "messaging/registration-token-not-registered",
        "messaging/invalid-registration-token",
    })

    @classmethod
    def _token_is_dead(cls, exception) -> bool:
        """Is this per-token failure permanent, or worth retrying later?

        The SDK guarantees the exception CLASS: UNREGISTERED and SENDER_ID_MISMATCH are
        the only token-specific FCM errors and have their own classes. Everything else
        the SDK raises (auth, quota, payload, transport) says nothing about the token and
        must not deactivate it.
        """
        if exception is None:
            return False
        try:
            from firebase_admin import exceptions as _fb_exceptions
            from firebase_admin import messaging as _messaging
        except Exception:
            logger.debug("could not consult firebase exception types", exc_info=True)
        else:
            dead_types = tuple(
                t for t in (getattr(_messaging, "UnregisteredError", None),
                            getattr(_messaging, "SenderIdMismatchError", None))
                if t is not None)
            if dead_types and isinstance(exception, dead_types):
                return True
            if isinstance(exception, _fb_exceptions.FirebaseError):
                return False
        return getattr(exception, "code", None) in cls.DEAD_TOKEN_CODES

    @classmethod
    def _settle(cls, notification, outcome):
        with transaction.atomic():
            locked = Notification.objects.select_for_update().get(id=notification.id)
            fcm = dict(locked.status.get('fcm', {}))
            if fcm.get('status') not in cls.RETRYABLE_STATUSES:
                return
            fcm['status'] = outcome
            fcm['last_attempt'] = timezone.now().isoformat()
            locked.status['fcm'] = fcm
            locked.save(update_fields=['status'])

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
