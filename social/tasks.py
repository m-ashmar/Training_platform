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
        
    from .feed_cache import FEED_CARRIERS_TTL_SECONDS, FEED_TTL_SECONDS, feed_carriers_key

    pipeline = redis_cli.pipeline()
    carriers = feed_carriers_key(post_id)
    for uid in user_ids:
        key = f"feed:v1:{uid}"
        pipeline.zadd(key, {str(post_id): timestamp})
        pipeline.zremrangebyrank(key, 0, -501)
        pipeline.expire(key, FEED_TTL_SECONDS)
        # Remember where the post went, so deleting it cleans every feed it reached.
        pipeline.sadd(carriers, key)
    pipeline.expire(carriers, FEED_CARRIERS_TTL_SECONDS)
        
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


@shared_task(
    name="social.advance_challenge_phases",
    autoretry_for=TRANSIENT_ERRORS,
    retry_backoff=True,
    retry_backoff_max=600,
    retry_jitter=True,
    max_retries=3,
)
def advance_challenge_phases():
    """Hourly: move stored challenge status with the clock.

    The API reads the phase from the dates; the admin reads the column. Without this
    they disagree from the moment a challenge starts.
    """
    from django.utils import timezone

    from .models import Challenge

    now = timezone.now()
    # The challenges this run closes, read BEFORE the UPDATE closes them: their winners
    # are checked for the Challenge Winner badge below, which counts only a challenge
    # that is over (SIDE_BUGS S-044). The same predicate `advance_stored_phases` ends.
    closing = list(Challenge.objects
                   .filter(status__in=('upcoming', 'active'), end_date__lt=now)
                   .values_list('pk', flat=True))
    started, ended = Challenge.advance_stored_phases(now=now)
    if started or ended:
        logger.info("challenges started: %s, ended: %s", started, ended)
    award_challenge_winners(closing)
    return f"started={started} ended={ended}"


def award_challenge_winners(challenge_ids) -> int:
    """Run the badge check for whoever finished first in each of these challenges.

    `challenge_wins` (`achievements/engine.py`) counts a first place only once the
    challenge is over, so the check has to run when it closes — the hourly phase task
    above, or a person closing it (`ChallengeViewSet.perform_update`). Nothing else
    re-runs a winner's check at that moment: the badge was otherwise earned only if
    some unrelated event happened to trigger one later.

    The winner is the participation with `rank=1` that reached the target
    (`status='completed'`), the definition the metric counts — and never an account
    that is not active (the owner's decision 8, 2026-09-24): a suspended one holds no
    place while the challenge runs (`social/presence.py` re-ranks at the suspension),
    and one suspended after the end keeps its stored rank as the record but is not
    awarded. Returns how many were checked. Never raises: a badge is a reward, and must
    not fail the close.
    """
    ids = [pk for pk in challenge_ids or () if pk is not None]
    if not ids:
        return 0
    from users.visibility import public_q

    from .models import ChallengeParticipation

    checked = 0
    for row in (ChallengeParticipation.objects
                .filter(public_q('user'), challenge_id__in=ids, rank=1, status='completed')
                .select_related('user').order_by('challenge_id', 'id')):
        try:
            from achievements.engine import trigger_achievement_check

            trigger_achievement_check(row.user, 'challenge_completed',
                                      challenge_id=row.challenge_id)
            checked += 1
        except Exception:
            logger.warning("could not check the winner of challenge %s",
                           row.challenge_id, exc_info=True)
    return checked
