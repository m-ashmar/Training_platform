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

logger = logging.getLogger(__name__)


@shared_task(name='ai_assistant.tasks.close_idle_sessions')
def close_idle_sessions():
    """
    Deactivate sessions that have been idle longer than the configured
    timeout. Generate session summaries for long-term memory.
    """
    from ai_assistant.models import ChatSession, UserInsight
    from ai_assistant.services.memory_service import MemoryService

    config = getattr(settings, 'AI_ASSISTANT_CONFIG', {})
    timeout_minutes = config.get('SESSION_TIMEOUT_MINUTES', 30)
    cutoff = timezone.now() - timedelta(minutes=timeout_minutes)

    idle_sessions = ChatSession.objects.filter(
        is_active=True,
        updated_at__lt=cutoff,
    ).select_related('user')

    memory = MemoryService()
    closed_count = 0

    for session in idle_sessions:
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

    return f"Closed {closed_count} sessions"


@shared_task(name='ai_assistant.tasks.compute_all_user_insights')
def compute_all_user_insights():
    """
    Daily task to refresh analyzer caches for all users who have
    used the AI assistant.
    """
    from ai_assistant.models import ChatSession, UserInsight
    from ai_assistant.analyzers.training_analyzer import TrainingAnalyzer
    from ai_assistant.analyzers.diet_analyzer import DietAnalyzer
    from ai_assistant.analyzers.behavior_profiler import BehaviorProfiler

    # Only compute for users who have active sessions
    user_ids = ChatSession.objects.filter(
        is_active=True,
    ).values_list('user_id', flat=True).distinct()

    from django.contrib.auth import get_user_model
    User = get_user_model()
    users = User.objects.filter(id__in=user_ids)

    expires = timezone.now() + timedelta(hours=24)
    analyzers = [
        ('training_pattern', TrainingAnalyzer()),
        ('diet_pattern', DietAnalyzer()),
        ('behavior_profile', BehaviorProfiler()),
    ]

    updated = 0
    for user in users:
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

    logger.info(f"Computed {updated} insights for {len(user_ids)} users")
    return f"Updated {updated} insights"


@shared_task(name='ai_assistant.tasks.check_daily_cost')
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
