"""
tasks.py — Celery background tasks for the AI assistant.

Tasks:
  - close_idle_sessions: Deactivate sessions idle > threshold minutes
  - compute_all_user_insights: Refresh analyzer caches daily
  - check_daily_cost: Hourly cost alert check
"""

import logging
from datetime import timedelta
from decimal import Decimal

from celery import shared_task
from django.conf import settings
from django.utils import timezone
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


@shared_task(
    name='ai_assistant.tasks.close_idle_sessions',
    autoretry_for=TRANSIENT_ERRORS,
    retry_backoff=True,
    retry_backoff_max=600,
    retry_jitter=True,
    max_retries=3,
)
def close_idle_sessions(after=None):
    """
    Deactivate sessions that have been idle longer than the configured
    timeout. Generate session summaries for long-term memory.

    A page of sessions per invocation (`training_platform.batching`), the rest handed on
    to a fresh invocation: each close reads the session's messages to summarise them,
    and every idle session on the platform in one task under the 300 s soft limit is
    the shape the production-readiness audit (2026-09-25) found in the daily jobs. A
    closed session leaves the query, so a redelivered page closes nothing twice.
    """
    from ai_assistant.models import ChatSession, UserInsight
    from ai_assistant.services.memory_service import MemoryService
    from training_platform.batching import Batch

    config = getattr(settings, 'AI_ASSISTANT_CONFIG', {})
    timeout_minutes = config.get('SESSION_TIMEOUT_MINUTES', 30)
    cutoff = timezone.now() - timedelta(minutes=timeout_minutes)

    batch = Batch()
    idle_sessions = batch.page(ChatSession.objects.filter(
        is_active=True,
        updated_at__lt=cutoff,
    ).select_related('user'), after)

    memory = MemoryService()
    closed_count = 0

    for session in idle_sessions:
        if batch.spent():
            break
        batch.done(session.pk)
        try:
            # Generate summary for long-term memory
            summary = memory.generate_summary(session)
            if summary:
                UserInsight.objects.create(
                    user=session.user,
                    insight_type='chat_summary',
                    content={"session_id": str(session.session_id), "summary": summary},
                    confidence=0.5,
                    expires_at=None,  # Chat summaries never expire
                )

            session.is_active = False
            session.save(update_fields=['is_active'])
            closed_count += 1
        except Exception as e:
            logger.exception(f"Failed to close session {session.session_id}: {e}")

    if closed_count > 0:
        logger.info(f"Closed {closed_count} idle AI chat sessions")
    batch.hand_on(close_idle_sessions)

    return f"Closed {closed_count} sessions"


@shared_task(
    name='ai_assistant.tasks.compute_all_user_insights',
    autoretry_for=TRANSIENT_ERRORS,
    retry_backoff=True,
    retry_backoff_max=600,
    retry_jitter=True,
    max_retries=3,
)
def compute_all_user_insights(after=None):
    """
    Daily task to refresh analyzer caches for all users who have
    used the AI assistant.

    A page of users per invocation (`training_platform.batching`), the rest handed on to
    a fresh invocation of itself. It walked every user x three analyzers in one task
    under the 300 s soft limit, so past a few thousand users it was cut off part-way
    every day and the later ones were never refreshed (the production-readiness audit,
    2026-09-25). `update_or_create` per user and insight type, so a redelivered page
    writes nothing twice.
    """
    from ai_assistant.models import ChatSession, UserInsight
    from ai_assistant.analyzers.training_analyzer import TrainingAnalyzer
    from ai_assistant.analyzers.diet_analyzer import DietAnalyzer
    from ai_assistant.analyzers.behavior_profiler import BehaviorProfiler
    from training_platform.batching import Batch

    from django.contrib.auth import get_user_model
    User = get_user_model()
    # Only compute for users who have active sessions: a subquery, never a list of ids.
    batch = Batch()
    users = batch.page(User.objects.filter(
        id__in=ChatSession.objects.filter(is_active=True).values('user_id')), after)

    expires = timezone.now() + timedelta(hours=24)
    analyzers = [
        ('training_pattern', TrainingAnalyzer()),
        ('diet_pattern', DietAnalyzer()),
        ('behavior_profile', BehaviorProfiler()),
    ]

    updated = 0
    for user in users:
        if batch.spent():
            break
        batch.done(user.pk)
        for insight_type, analyzer in analyzers:
            try:
                result = analyzer.analyze(user)
                UserInsight.objects.update_or_create(
                    user=user,
                    insight_type=insight_type,
                    defaults={
                        'content': result,
                        'confidence': 0.7,
                        'expires_at': expires,
                    },
                )
                updated += 1
            except Exception as e:
                logger.warning(f"Analyzer {insight_type} failed for user {user.id}: {e}")

    logger.info(f"Computed {updated} insights for {batch.handled} users")
    batch.hand_on(compute_all_user_insights)
    return f"Updated {updated} insights"


@shared_task(
    name='ai_assistant.tasks.check_daily_cost',
    autoretry_for=TRANSIENT_ERRORS,
    retry_backoff=True,
    retry_backoff_max=600,
    retry_jitter=True,
    max_retries=3,
)
def check_daily_cost():
    """Hourly check: alert if daily AI costs exceed threshold."""
    from ai_assistant.services.cost_tracker import CostTracker

    tracker = CostTracker()
    exceeded = tracker.check_alert_threshold()

    if exceeded:
        daily_total = tracker.get_daily_total()
        logger.critical(f"AI cost alert triggered: ${daily_total}")
        # TODO: Send notification to admin (email/Slack)

    return f"Cost check complete. Alert: {exceeded}"


@shared_task(
    name='ai_assistant.tasks.purge_expired_training_data',
    autoretry_for=TRANSIENT_ERRORS,
    retry_backoff=True,
    retry_backoff_max=600,
    retry_jitter=True,
    max_retries=3,
)
def purge_expired_training_data():
    """Delete every AI training copy whose `retain_until` has passed.

    Scheduled daily (`CELERY_BEAT_SCHEDULE['purge-expired-ai-training-data']`). Every
    copy is written with a deadline at most `AI_TRAINING_RETENTION_MAX_DAYS` (365) days
    out, and the column is NOT NULL, so no copy can outlive it (the owner, 2026-09-24:
    the second safeguard behind "help us improve" off). The copies have no FK and nothing
    points at them, so this is one bulk DELETE.
    """
    from django.utils import timezone

    from ai_assistant.models import AITrainingData

    deleted, _detail = AITrainingData.objects.filter(
        retain_until__lt=timezone.now()).delete()
    if deleted:
        logger.info("Purged %s expired AI training records", deleted)
    return f"Purged {deleted} expired training records"
