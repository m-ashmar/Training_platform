from celery import shared_task
from django.contrib.auth import get_user_model
from .models import Notification
import logging

logger = logging.getLogger(__name__)

@shared_task
def send_async_notification(user_id, notif_type, message, related_object_id=None, related_object_type=None):
    """
    Asynchronously create a notification.
    """
    User = get_user_model()
    try:
        user = User.objects.get(pk=user_id)
        Notification.objects.create(
            user=user,
            notif_type=notif_type,
            message=message,
            related_object_id=related_object_id,
            related_object_type=related_object_type
        )
        logger.info(f"Notification sent to user {user_id} (type: {notif_type})")
    except User.DoesNotExist:
        logger.error(f"Failed to send notification: User {user_id} not found")
    except Exception as e:
        logger.error(f"Error sending notification to user {user_id}: {str(e)}")
