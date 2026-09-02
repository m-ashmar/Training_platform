"""
cost_tracker.py — Per-user and global AI usage cost tracking.

Aggregates token usage into daily cost records and provides
budget monitoring capabilities.
"""

import logging
from datetime import date
from decimal import Decimal
from django.conf import settings
from django.utils import timezone

logger = logging.getLogger(__name__)

# Approximate pricing per 1M tokens (gpt-4o-mini, as of early 2026)
# Update these when switching models
PRICING = {
    "gpt-4o-mini": {"input": Decimal("0.15"), "output": Decimal("0.60")},
    "gpt-4o": {"input": Decimal("2.50"), "output": Decimal("10.00")},
    "gpt-5-nano": {"input": Decimal("0.10"), "output": Decimal("0.40")},
}


class CostTracker:
    """Track and aggregate AI usage costs."""

    def __init__(self):
        config = getattr(settings, 'AI_ASSISTANT_CONFIG', {})
        self.model = config.get('MODEL', 'gpt-4o-mini')
        self.pricing = PRICING.get(self.model, PRICING["gpt-4o-mini"])

    def estimate_cost(self, input_tokens: int, output_tokens: int) -> Decimal:
        """Estimate cost in USD for a single interaction."""
        input_cost = Decimal(input_tokens) / Decimal(1_000_000) * self.pricing["input"]
        output_cost = Decimal(output_tokens) / Decimal(1_000_000) * self.pricing["output"]
        return input_cost + output_cost

    def record_usage(self, user, total_tokens: int, estimated_cost: Decimal):
        """
        Add to the user's daily usage aggregation.
        Creates or updates the UsageCost record.
        """
        from ai_assistant.models import UsageCost

        today = timezone.localdate()
        cost_record, created = UsageCost.objects.get_or_create(
            user=user,
            date=today,
            defaults={
                'total_tokens': total_tokens,
                'total_messages': 1,
                'estimated_cost_usd': estimated_cost,
            },
        )
        if not created:
            cost_record.total_tokens += total_tokens
            cost_record.total_messages += 1
            cost_record.estimated_cost_usd += estimated_cost
            cost_record.save(update_fields=[
                'total_tokens', 'total_messages', 'estimated_cost_usd',
            ])

    def update_session_cost(self, session, cost: Decimal, tokens: int):
        """Update session-level cost tracking."""
        session.total_tokens_used += tokens
        session.estimated_cost_usd += cost
        session.save(update_fields=['total_tokens_used', 'estimated_cost_usd'])

    def get_daily_total(self) -> Decimal:
        """Get today's global cost across all users."""
        from ai_assistant.models import UsageCost
        from django.db.models import Sum

        today = timezone.localdate()
        result = UsageCost.objects.filter(
            date=today,
        ).aggregate(total=Sum('estimated_cost_usd'))
        return result['total'] or Decimal('0')

    def check_alert_threshold(self) -> bool:
        """Check if daily spend exceeds the configured alert threshold."""
        config = getattr(settings, 'AI_ASSISTANT_CONFIG', {})
        threshold = Decimal(config.get('DAILY_COST_ALERT_USD', '50.00'))
        daily_total = self.get_daily_total()
        if daily_total > threshold:
            logger.critical(
                f"AI daily cost alert: ${daily_total} exceeds "
                f"threshold ${threshold}"
            )
            return True
        return False

    def is_over_daily_limit(self) -> bool:
        """True once the platform's spend for today passes the hard ceiling.

        `check_alert_threshold` only logs — nothing ever stopped spending, so a runaway
        loop or a burst of traffic could bill without bound. This is the kill-switch.
        """
        from django.conf import settings

        config = getattr(settings, 'AI_ASSISTANT_CONFIG', {})
        limit = Decimal(str(config.get('DAILY_COST_LIMIT_USD', '200.00')))
        if limit <= 0:
            return False
        return self.get_daily_total() >= limit
