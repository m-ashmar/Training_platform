import logging
import hashlib

from django.utils import timezone
from django.db import IntegrityError, transaction
from notifications.models import Notification, UserNotificationPreference
from notifications.utils.deduplication import DeduplicationService
from notifications.channels.fcm import FCMChannel
from users.models import CustomUser

logger = logging.getLogger(__name__)

class NotificationService:
    
    @classmethod
    def create_and_send(cls, recipient: CustomUser, event_type: str, related_object_id: str, metadata: dict, actor: CustomUser = None, event_id: str = None):
        """
        Core logic to create notification and dispatch to channels.
        Handles Layer 2 Deduplication (DB constraint).
        """
        
        # Preferences decide DELIVERY, not whether the thing happened: the in-app row is
        # always written, and `_dispatch_channels` asks the preferences before pushing
        # (UserNotificationPreference.is_channel_enabled). A muted type used to leave
        # no row at all, so a person who silenced a kind of push also lost the record of
        # it — a payment or a trainer's answer — from their inbox (BACKEND_BUGS B-014).

        # calculate dedup key
        dedup_key = cls._generate_dedup_key(recipient.id, event_type, related_object_id, event_id)
        
        # Layer 1: Redis Check
        # Same anchor as the DB key below: the EMISSION when we have one.
        dedup_scope = event_id or related_object_id
        if DeduplicationService.is_duplicate(recipient.id, event_type, dedup_scope):
            try:
                from notifications.metrics import notifications_deduplicated_total
                notifications_deduplicated_total.labels(source='redis', event_type=event_type).inc()
            except ImportError:
                # Optional side effect: swallowing this silently is what made the
                # surrounding failures invisible in logs. Control flow is unchanged.
                logger.debug('suppressed non-fatal error', exc_info=True)
            return None
        
        try:
            # The INSERT gets its own savepoint. Without one, the IntegrityError raised
            # by a duplicate poisons the ENTIRE surrounding transaction: every view
            # under @transaction.atomic that sends a notification would then fail with
            # "You can't execute queries until the end of the 'atomic' block", and the
            # user's real work — a completed workout, an assigned routine — would roll
            # back because a notification they had already received was suppressed.
            # Deduplication is a normal outcome here, not an error, and must cost the
            # caller nothing.
            with transaction.atomic():
                notification = Notification.objects.create(
                    recipient=recipient,
                    actor=actor,
                    event_type=event_type,
                    related_object_id=related_object_id,
                    metadata=metadata,
                    deduplication_key=dedup_key,
                    event_id=event_id
                )
            # Metrics
            try:
                from notifications.metrics import notifications_created_total
                notifications_created_total.labels(event_type=event_type).inc()
            except ImportError:
                # Optional side effect: swallowing this silently is what made the
                # surrounding failures invisible in logs. Control flow is unchanged.
                logger.debug('suppressed non-fatal error', exc_info=True)

            # Dispatch to channels
            # In a real system, you might check user preferences here
            cls._dispatch_channels(notification)

            # The Redis suppression window opens HERE, once the row exists — not at the
            # check, which is what made a retry a no-op.
            DeduplicationService.mark_sent(recipient.id, event_type, dedup_scope)

            return notification
            
        except IntegrityError:
            logger.warning(f"Duplicate notification (DB Layer): {recipient.id} - {event_type}. Attempting recovery/retry.")
            try:
                from notifications.metrics import notifications_deduplicated_total
                notifications_deduplicated_total.labels(source='db', event_type=event_type).inc()
            except ImportError:
                # Optional side effect: swallowing this silently is what made the
                # surrounding failures invisible in logs. Control flow is unchanged.
                logger.debug('suppressed non-fatal error', exc_info=True)
                
            # Idempotency / Recovery:
            # If notification exists, we should ensure it was sent.
            # Fetch existing and dispatch (channels handle idempotency)
            try:
                notification = Notification.objects.get(
                    recipient=recipient,
                    deduplication_key=dedup_key
                )
                cls._dispatch_channels(notification)
                return notification
            except Notification.DoesNotExist:
                # Should not happen given IntegrityError
                return None
        except Exception as e:
            # RAISED, not swallowed. Returning None here told the caller "nothing to
            # do" for a database error, an outage or a bug — so the dispatcher saw a
            # clean return, the task reported SUCCESS, and the notification was gone
            # with nothing retrying it and nothing recording it. The dead-letter queue
            # read empty while notifications were being destroyed.
            logger.error(f"Error creating notification: {e}", exc_info=True)
            raise

    @staticmethod
    def _generate_dedup_key(recipient_id, event_type, related_object_id, event_id=None):
        """What counts as "the same notification", for the permanent DB constraint.

        The EMISSION, when we know it. `event_id` is a fresh UUID per `emit_event`, so
        keying on it suppresses a duplicate delivery of one emission — a Celery
        redelivery, a double signal — which is what deduplication is for, and lets a
        genuinely new event through.

        Keying on the entity instead made the suppression permanent, because
        `UniqueConstraint(recipient, deduplication_key)` has no time component and
        every listener passes a stable id: a routine id for `routine_assignment`, an
        actor id for the four trainer-request events, a post id for `post_liked`. A
        trainer re-assigning the same routine in June with new sets produced no push, no
        unread badge, and the January text still sitting at its January position in the
        feed — while the dedup counter rose and looked like the feature working. A
        trainer whose invitation was declined could never invite that client again.

        Without an `event_id` the entity key is still the right answer: `session_reminder`
        passes the local DATE, and `progress_milestone` passes `streak-7`, both of which
        are meant to be once-only.
        """
        raw = (f"{recipient_id}:{event_type}:{event_id}" if event_id
               else f"{recipient_id}:{event_type}:{related_object_id}")
        return hashlib.sha256(raw.encode()).hexdigest()

    @classmethod
    def _dispatch_channels(cls, notification: Notification):
        """
        Send to enabled channels.
        """
        recipient = notification.recipient
        event_type = notification.event_type
        
        # FCM
        if UserNotificationPreference.is_channel_enabled(recipient, event_type, 'fcm'):
            try:
                FCMChannel.send(notification)
            except Exception as e:
                # RECORDED, so something can find it. `notification.status` is written
                # inside `FCMChannel.send` AFTER the sends, so a failure before that
                # point left `status = {}` — and no query anywhere reads
                # `status['fcm']['status'] == 'failed'`, nor does the admin offer a
                # resend. An FCM outage of any length cost every push issued during it,
                # permanently and invisibly, because the in-app row survives.
                logger.error(f"FCM dispatch failed for {notification.id}: {e}", exc_info=True)
                #
                # The earlier record replaced the whole `fcm` block, dropping
                # `sent_tokens` (so a retry pushed again to devices that already had
                # it) and `attempts` (so the drain's retry cap never advanced).
                try:
                    with transaction.atomic():
                        locked = type(notification).objects.select_for_update().get(
                            pk=notification.pk)
                        status = dict(locked.status or {})
                        previous = dict(status.get('fcm') or {})
                        previous.update({
                            'status': 'partial' if previous.get('sent_tokens') else 'failed',
                            'error': str(e)[:500],
                            'attempts': previous.get('attempts', 0) + 1,
                            'failed_at': timezone.now().isoformat(),
                        })
                        status['fcm'] = previous
                        type(notification).objects.filter(pk=notification.pk).update(
                            status=status)
                except Exception:
                    logger.debug("could not record the FCM failure", exc_info=True)
            
        # Email (Future)
        # if UserNotificationPreference.is_channel_enabled(recipient, event_type, 'email'):
        #     EmailChannel.send(notification)
