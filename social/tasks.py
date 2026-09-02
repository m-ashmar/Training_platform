import logging
from celery import shared_task
from django.contrib.auth import get_user_model
from .firebase_service import FirebaseNotificationService
from users.models import DeviceToken
from .feed_cache import push_post_to_global_stream, get_redis_client
from django.db import OperationalError, InterfaceError

# Transient failures worth retrying. These tasks previously used a bare @shared_task:
# no bind, no autoretry, no self.retry() — so ANY exception (a DB blip, an FCM 503, a
# broker hiccup) lost the job permanently and silently. `autoretry_for` gives them a
# retry policy without changing any signature; permanent errors still fail fast.
TRANSIENT_ERRORS = (
    OperationalError,
    InterfaceError,
    ConnectionError,
    TimeoutError,
)


logger = logging.getLogger(__name__)
User = get_user_model()


@shared_task(
    autoretry_for=TRANSIENT_ERRORS,
    retry_backoff=True,
    retry_backoff_max=600,
    retry_jitter=True,
    max_retries=3,
)
def fan_out_post_root(author_id, post_id, timestamp):
    from .models import UserFollow
    
    follower_count = UserFollow.objects.filter(following_id=author_id).count()
    
    # HYBRID FAN-OUT: If > 10K followers, push globally.
    if follower_count > 10000:
        push_post_to_global_stream(post_id, timestamp)
        return

    # Chunk over followers to drop memory overhead
    followers_iter = UserFollow.objects.filter(following_id=author_id).values_list('follower_id', flat=True).iterator(chunk_size=1000)
    
    batch = [author_id]  # Push to their own feed
    for f_id in followers_iter:
        batch.append(f_id)
        if len(batch) >= 500:
            fan_out_batch.delay(batch, post_id, timestamp)
            batch = []
            
    if batch:
        fan_out_batch.delay(batch, post_id, timestamp)


@shared_task(
    autoretry_for=TRANSIENT_ERRORS,
    retry_backoff=True,
    retry_backoff_max=600,
    retry_jitter=True,
    max_retries=3,
)
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


@shared_task(
    autoretry_for=TRANSIENT_ERRORS,
    retry_backoff=True,
    retry_backoff_max=600,
    retry_jitter=True,
    max_retries=3,
)
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
        # Get user's ACTIVE device tokens only — inactive rows are soft-deleted
        # invalid tokens and sending to them just burns FCM quota.
        tokens = list(
            DeviceToken.objects.filter(user_id=user_id, is_active=True).values_list('token', flat=True)
        )

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

