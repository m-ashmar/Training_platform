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
    """Hourly: move stored challenge status with the clock, and settle the winners of
    the challenges whose review window has closed (`settle_winners`).

    The API reads the phase from the dates; the admin reads the column. Without this
    they disagree from the moment a challenge starts.
    """
    from django.utils import timezone

    from .models import Challenge

    now = timezone.now()
    started, ended = Challenge.advance_stored_phases(now=now)
    if started or ended:
        logger.info("challenges started: %s, ended: %s", started, ended)
    # A streak is the CURRENT run of days (BACKEND_BUGS B-161): a day that ends without
    # a set breaks it with nothing logged to trigger a recount, so the clock does it.
    # Before the settlement, so a streak broken on the window's last day is settled on
    # its true number.
    from .challenge_progress import recount_streaks

    streaks = recount_streaks(now=now)
    # The Challenge Winner badge counts a first place only once the challenge is over
    # (SIDE_BUGS S-044), and since the owner's F20 (2026-09-24) only once the standing
    # has been open for review for `Challenge.REVIEW_HOURS` after the end.
    settled = settle_winners(now=now)
    return f"started={started} ended={ended} streaks={streaks} settled={settled}"


#: Challenges settled per hourly run. The rest wait an hour: a settlement is a re-rank
#: and a badge check, and a backlog must not hold the worker.
SETTLE_BATCH = 200


def settle_winners(now=None, challenge_ids=None) -> int:
    """Settle every challenge whose review window has closed and whose winner is not
    settled yet (`Challenge.winner_checked_at` null) — or only `challenge_ids`.

    Returns how many were settled. See `settle_winner` for what settling is.

    The batch takes the challenges with NO flagged participant first: a ranked
    challenge whose first place waits for an operator is looked at again every hour,
    and 200 of them left unreviewed at the front of an oldest-first queue would have
    stopped every later challenge from ever settling. An unranked challenge never
    waits (`settle_winner`).
    """
    from datetime import timedelta

    from django.utils import timezone

    from django.db.models import Exists, OuterRef

    from .models import Challenge, ChallengeParticipation

    now = now or timezone.now()
    flagged = ChallengeParticipation.objects.filter(
        challenge=OuterRef('pk'), review_state=ChallengeParticipation.REVIEW_FLAGGED)
    due = Challenge.objects.filter(
        winner_checked_at__isnull=True,
        end_date__lte=now - timedelta(hours=Challenge.REVIEW_HOURS))
    if challenge_ids is not None:
        due = due.filter(pk__in=[pk for pk in challenge_ids if pk is not None])
    settled = 0
    due = due.annotate(waiting=Exists(flagged)).order_by('waiting', 'end_date', 'id')
    for pk in due.values_list('pk', flat=True)[:SETTLE_BATCH]:
        try:
            if settle_winner(pk, now=now):
                settled += 1
        except Exception:
            # A badge is a reward: one challenge must not stop the others settling.
            logger.warning("could not settle the winner of challenge %s", pk,
                           exc_info=True)
    return settled


def settle_winner(challenge_id, now=None) -> bool:
    """Settle ONE challenge's winner, once; whether it settled (`_settle_winner`)."""
    return _settle_winner(challenge_id, now=now)[0]


def _settle_winner(challenge_id, now=None):
    """Settle ONE challenge's winner, once (owner, 2026-09-24, F20).

    Only a challenge that ended at least `Challenge.REVIEW_HOURS` ago and is not
    settled yet. Under the challenge's row lock: the standing is re-ranked one last
    time (`write_places(settling=True)`, so a participant disqualified during the
    review holds no place), then the participant at place 1 decides:

    * FLAGGED and not yet decided by an operator, in a RANKED challenge that is not
      cancelled: nothing is settled. The hourly task looks again; an operator's "clear
      review" or "disqualify" settles it at once. A challenge that can award nothing
      (unranked or cancelled) settles at once, whoever is first.
    * otherwise the challenge is settled (`winner_checked_at` = now, written once), and
      the Challenge Winner badge is checked for that person only if the challenge is
      `ranked` (auto-counted, created by a trainer or staff), not cancelled, and they
      are `clear` and reached the target (`status='completed'`).

    **At least `MIN_PARTICIPANTS_FOR_WINNER` (5) active participants** (the owner's
    decision of 2026-09-24, `social/standing.py active_participants`: in the standing
    and with a counted number above zero). Fewer, and the challenge can award nothing:
    it settles at once (`winner_checked_at` written, `winner_final` true, even when its
    first place is flagged) and no badge is checked. Every settlement records the count
    it decided on (`Challenge.settled_active_participants`, in the same UPDATE as
    `winner_checked_at`), and the badge engine reads that record, never a recount.

    A participant whose account is not publicly visible at the settlement (suspended or
    erased, the owner's decision 8, 2026-09-24) holds no place in it, exactly like a
    disqualified one (`social/standing.py in_standing`): place 1 is the first visible
    participant. After the settlement the stored ranks are the record, and a later
    suspension moves nothing.

    Returns `(settled, winner_checked)`. The badge check runs after the settlement is
    written (`achievements/engine.py challenge_wins` counts only a settled challenge).
    """
    from datetime import timedelta

    from django.db import transaction
    from django.utils import timezone

    from .models import Challenge, ChallengeParticipation
    from . import standing
    from .standing import STANDING_ORDER, active_participants, in_standing, write_places

    now = now or timezone.now()
    winner = None
    with transaction.atomic():
        challenge = (Challenge.objects.select_for_update(of=('self',))
                     .select_related('creator')
                     .filter(pk=challenge_id, winner_checked_at__isnull=True,
                             end_date__lte=now - timedelta(hours=Challenge.REVIEW_HOURS))
                     .first())
        if challenge is None:
            return False, False
        write_places(challenge, settling=True)
        first = (in_standing(ChallengeParticipation.objects.filter(challenge=challenge))
                 .select_related('user').order_by(*STANDING_ORDER).first())
        can_award = challenge.ranked and challenge.status != 'cancelled'
        # Too few people actually took part for a first place to mean anything (owner,
        # 2026-09-24). Counted after the last re-rank, under the same lock, by the rule
        # the challenge row serves as `active_participants`, and RECORDED with the
        # settlement: the badge engine reads the recorded number, so nothing that
        # happens to other people's rows later (an erasure deletes them) moves it. Read
        # off the module so the one constant is the one place the number lives.
        active = active_participants(ChallengeParticipation.objects
                                     .filter(challenge=challenge)).count()
        if active < standing.MIN_PARTICIPANTS_FOR_WINNER:
            can_award = False
        if (can_award and first is not None
                and first.review_state == ChallengeParticipation.REVIEW_FLAGGED):
            return False, False
        Challenge.objects.filter(pk=challenge.pk).update(
            winner_checked_at=now, settled_active_participants=active)
        if (first is not None and can_award and first.status == 'completed'
                and first.review_state == ChallengeParticipation.REVIEW_CLEAR):
            winner = first
    if winner is not None:
        try:
            from achievements.engine import trigger_achievement_check

            trigger_achievement_check(winner.user, 'challenge_completed',
                                      challenge_id=winner.challenge_id)
        except Exception:
            logger.warning("could not check the winner of challenge %s",
                           winner.challenge_id, exc_info=True)
    return True, winner is not None


def award_challenge_winners(challenge_ids) -> int:
    """The name this lived under before F20: settle these challenges if their review
    window has closed (`_settle_winner`); nothing before that. Returns how many winners
    were checked for the badge, as it always did (not how many challenges settled: one
    whose first place holds no place or wins nothing settles with no winner checked).
    Never raises."""
    ids = sorted({pk for pk in challenge_ids or () if pk is not None})
    checked = 0
    for pk in ids:
        try:
            checked += int(_settle_winner(pk)[1])
        except Exception:
            logger.warning("could not settle challenge %s", pk, exc_info=True)
    return checked
