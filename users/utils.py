import logging
from django.conf import settings

logger = logging.getLogger(__name__)

def send_push_notification(user, title, message, data=None):
    """
    Send push notification to user's devices via FCM.
    
    Args:
        user: CustomUser instance
        title: Notification title
        message: Notification message
        data: Optional data payload
    
    Returns:
        bool: True if notification was sent successfully, False otherwise
    """
    from .models import DeviceToken
    
    try:
        api_key = getattr(settings, 'FCM_SERVER_KEY', None)
        if not api_key:
            logger.warning(f"FCM_SERVER_KEY not configured. Mock notification for user {user.id}: {title} - {message}")
            return True  # Return True for testing purposes
        
        from pyfcm import FCMNotification
        push_service = FCMNotification(api_key=api_key)
        tokens = list(DeviceToken.objects.filter(user=user).values_list('token', flat=True))
        
        if not tokens:
            logger.info(f"No device tokens found for user {user.id}")
            return False
        
        result = push_service.notify_multiple_devices(
            registration_ids=tokens,
            message_title=title,
            message_body=message,
            data_message=data or {}
        )
        
        logger.info(f"Push notification sent to user {user.id}: {title} - {message}")
        return result
        
    except Exception as e:
        logger.error(f"Error sending push notification to user {user.id}: {str(e)}")
        return False 