import logging
from celery import shared_task
from django.contrib.auth import get_user_model
from .firebase_service import FirebaseNotificationService
from users.models import DeviceToken
from .feed_cache import push_post_to_global_stream, get_redis_client

logger = logging.getLogger(__name__)
User = get_user_model()


@shared_task
def fan_out_post_root(author_id, post_id, timestamp):
    from .models import Follow
    
    follower_count = Follow.objects.filter(following_id=author_id).count()
    
    # HYBRID FAN-OUT: If > 10K followers, push globally.
    if follower_count > 10000:
        push_post_to_global_stream(post_id, timestamp)
        return

    # Chunk over followers to drop memory overhead
    followers_iter = Follow.objects.filter(following_id=author_id).values_list('follower_id', flat=True).iterator(chunk_size=1000)
    
    batch = [author_id]  # Push to their own feed
    for f_id in followers_iter:
        batch.append(f_id)
        if len(batch) >= 500:
            fan_out_batch.delay(batch, post_id, timestamp)
            batch = []
            
    if batch:
        fan_out_batch.delay(batch, post_id, timestamp)


@shared_task
def fan_out_batch(user_ids, post_id, timestamp):
    redis_cli = get_redis_client()
    if not redis_cli:
        return
        
    pipeline = redis_cli.pipeline()
    for uid in user_ids:
        key = f"feed:v1:{uid}"
        pipeline.zadd(key, {str(post_id): timestamp})
        pipeline.zremrangebyrank(key, 0, -501)
        pipeline.expire(key, 86400)  # 24h expiration
        
    try:
        pipeline.execute()
    except Exception as e:
        logger.error(f"Redis pipeline batch execute failed: {e}")


@shared_task
def send_firebase_notification(user_id, title, body, data=None):
    """
    Send a Firebase notification to a specific user asynchronously.
    
    Args:
        user_id: ID of the user to send to.
        title: Notification title.
        body: Notification body.
        data: Optional data payload.
    """
    try:
        # Get user's device tokens
        tokens = list(DeviceToken.objects.filter(user_id=user_id).values_list('token', flat=True))
        
        if not tokens:
            logger.info(f"No device tokens found for user {user_id}. Skipping FCM notification.")
            return False
            
        service = FirebaseNotificationService()
        success_count = service.send_multicast(tokens, title, body, data)
        
        logger.info(f"FCM notification sent to user {user_id}: {title} ({success_count}/{len(tokens)} success)")
        return success_count > 0
        
    except Exception as e:
        logger.error(f"Error in send_firebase_notification for user {user_id}: {e}")
        return False

@shared_task
def send_bulk_notifications(user_ids, title, body, data=None):
    """
    Send a Firebase notification to multiple users asynchronously.
    
    Args:
        user_ids: List of user IDs.
        title: Notification title.
        body: Notification body.
        data: Optional data payload.
    """
    try:
        # Get all tokens for these users
        tokens = list(DeviceToken.objects.filter(user_id__in=user_ids).values_list('token', flat=True))
        
        if not tokens:
            logger.info("No device tokens found for bulk notification.")
            return False
            
        service = FirebaseNotificationService()
        success_count = service.send_multicast(tokens, title, body, data)
        
        logger.info(f"Bulk FCM notification sent: {title} ({success_count}/{len(tokens)} success)")
        return success_count
        
    except Exception as e:
        logger.error(f"Error in send_bulk_notifications: {e}")
        return False


def dispatch_notification(user_id, title, body, data=None):
    """
    Resilient notification dispatch: tries Celery async first, falls back
    to synchronous execution if the broker is unreachable.
    
    Use this instead of calling send_firebase_notification.delay() directly.
    """
    try:
        send_firebase_notification.delay(user_id=user_id, title=title, body=body, data=data)
    except Exception as e:
        logger.warning(f"Celery broker unavailable, sending FCM synchronously: {e}")
        send_firebase_notification(user_id=user_id, title=title, body=body, data=data)


def dispatch_bulk_notifications(user_ids, title, body, data=None):
    """
    Resilient bulk notification dispatch: tries Celery async first, falls
    back to synchronous execution if the broker is unreachable.
    """
    try:
        send_bulk_notifications.delay(user_ids=user_ids, title=title, body=body, data=data)
    except Exception as e:
        logger.warning(f"Celery broker unavailable, sending bulk FCM synchronously: {e}")
        send_bulk_notifications(user_ids=user_ids, title=title, body=body, data=data)

