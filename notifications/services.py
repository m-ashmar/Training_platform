import logging
import hashlib
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
        
        # Check User Preferences (Early Exit)
        # Check global disable
        try:
            pref = UserNotificationPreference.objects.get(user=recipient, event_type=event_type)
            if not pref.is_enabled:
                 logger.info(f"Notification suppressed by user preference: {recipient.id} - {event_type}")
                 return None
        except UserNotificationPreference.DoesNotExist:
            pass
        
        # calculate dedup key
        dedup_key = cls._generate_dedup_key(recipient.id, event_type, related_object_id)
        
        # Layer 1: Redis Check
        if DeduplicationService.is_duplicate(recipient.id, event_type, related_object_id):
            try:
                from notifications.metrics import notifications_deduplicated_total
                notifications_deduplicated_total.labels(source='redis', event_type=event_type).inc()
            except ImportError:
                pass
            return None
        
        try:
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
                pass

            # Dispatch to channels
            # In a real system, you might check user preferences here
            cls._dispatch_channels(notification)
            
            return notification
            
        except IntegrityError:
            logger.warning(f"Duplicate notification (DB Layer): {recipient.id} - {event_type}. Attempting recovery/retry.")
            try:
                from notifications.metrics import notifications_deduplicated_total
                notifications_deduplicated_total.labels(source='db', event_type=event_type).inc()
            except ImportError:
                pass
                
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
            logger.error(f"Error creating notification: {e}", exc_info=True)
            return None

    @staticmethod
    def _generate_dedup_key(recipient_id, event_type, related_object_id):
        raw = f"{recipient_id}:{event_type}:{related_object_id}"
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
                logger.error(f"FCM dispatch failed for {notification.id}: {e}", exc_info=True)
            
        # Email (Future)
        # if UserNotificationPreference.is_channel_enabled(recipient, event_type, 'email'):
        #     EmailChannel.send(notification)
