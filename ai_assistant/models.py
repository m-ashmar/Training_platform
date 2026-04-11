"""
models.py — AI Assistant Data Models

Six models for chat, training data collection, behavior tracking, cached insights,
and usage cost monitoring. Designed to serve as both operational storage and a
training data pipeline for a future custom fitness AI model.
"""

import uuid
from django.db import models
from django.conf import settings
from django.utils import timezone


class ChatSession(models.Model):
    """Groups chat messages into a conversation. Tracks token usage and cost."""

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='chat_sessions',
    )
    session_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    title = models.CharField(max_length=200, blank=True, help_text="Auto-generated from first message")
    total_tokens_used = models.IntegerField(default=0)
    total_messages = models.IntegerField(default=0)
    estimated_cost_usd = models.DecimalField(max_digits=10, decimal_places=6, default=0)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Chat Session"
        verbose_name_plural = "Chat Sessions"
        ordering = ['-updated_at']
        indexes = [
            models.Index(fields=['user', 'is_active']),
            models.Index(fields=['updated_at', 'is_active']),
        ]

    def __str__(self):
        title = self.title or "Untitled"
        return f"[{self.user.username}] {title} ({self.total_messages} msgs)"


class ChatMessage(models.Model):
    """Individual message in a chat session (user, assistant, or system role)."""

    ROLE_CHOICES = [
        ('user', 'User'),
        ('assistant', 'Assistant'),
        ('system', 'System'),
    ]

    session = models.ForeignKey(
        ChatSession, on_delete=models.CASCADE, related_name='messages',
    )
    role = models.CharField(max_length=10, choices=ROLE_CHOICES)
    content = models.TextField()
    tool_calls = models.JSONField(
        default=list, blank=True,
        help_text="List of tool calls GPT made for this turn",
    )
    tool_results = models.JSONField(
        default=list, blank=True,
        help_text="Data returned by each tool call",
    )
    tokens_used = models.IntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Chat Message"
        verbose_name_plural = "Chat Messages"
        ordering = ['created_at']
        indexes = [
            models.Index(fields=['session', 'created_at']),
        ]

    def __str__(self):
        preview = self.content[:60] + ("..." if len(self.content) > 60 else "")
        return f"[{self.role}] {preview}"


class AITrainingData(models.Model):
    """
    Complete interaction snapshot for future model training.

    Every chat turn is logged here with full context, tools used,
    and user feedback.  Outcome-based fields are deferred to v2.
    """

    FEEDBACK_CHOICES = [
        ('positive', '👍'),
        ('negative', '👎'),
    ]

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='ai_training_data',
    )
    session = models.ForeignKey(
        ChatSession, on_delete=models.CASCADE, related_name='training_data',
    )
    # Snapshot of user profile/state at interaction time
    user_context_snapshot = models.JSONField(
        help_text="Profile + plan state at interaction time",
    )
    user_message = models.TextField()
    tools_called = models.JSONField(default=list)
    tool_results = models.JSONField(default=list)
    ai_response = models.TextField()
    response_tokens = models.IntegerField(default=0)
    response_latency_ms = models.IntegerField(default=0)
    # User quality signal (v1: manual feedback only)
    user_feedback = models.CharField(
        max_length=10,
        null=True,
        blank=True,
        choices=FEEDBACK_CHOICES,
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "AI Training Data"
        verbose_name_plural = "AI Training Data"
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['user', 'created_at']),
            models.Index(fields=['user_feedback']),
        ]

    def __str__(self):
        fb = self.user_feedback or "no feedback"
        return f"[{self.user.username}] {fb} — {self.response_tokens} tokens"


class UserBehaviorEvent(models.Model):
    """
    Tracks user actions across the entire platform via Django signals.

    Creates the behavioral dataset for pattern learning and proactive
    engagement (future model training).
    """

    EVENT_TYPES = [
        ('workout_completed', 'Workout Completed'),
        ('workout_abandoned', 'Workout Abandoned'),
        ('set_logged', 'Set Logged'),
        ('routine_day_completed', 'Routine Day Completed'),
        ('meal_completed', 'Meal Completed'),
        ('plan_generated', 'Plan Generated'),
        ('chat_opened', 'Chat Opened'),
        ('achievement_earned', 'Achievement Earned'),
    ]

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='behavior_events',
    )
    event_type = models.CharField(max_length=50, choices=EVENT_TYPES)
    event_data = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "User Behavior Event"
        verbose_name_plural = "User Behavior Events"
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['user', 'created_at']),
            models.Index(fields=['event_type', 'created_at']),
        ]

    def __str__(self):
        return f"[{self.user.username}] {self.event_type} @ {self.created_at:%Y-%m-%d %H:%M}"


class UserInsight(models.Model):
    """
    Cached analytical insights computed by the Intelligence Layer.

    Types:
      - chat_summary: compressed session summary for long-term memory
      - training_pattern: from TrainingAnalyzer
      - diet_pattern: from DietAnalyzer
      - behavior_profile: from BehaviorProfiler
    """

    INSIGHT_TYPES = [
        ('chat_summary', 'Chat Summary'),
        ('training_pattern', 'Training Pattern'),
        ('diet_pattern', 'Diet Pattern'),
        ('behavior_profile', 'Behavior Profile'),
    ]

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='ai_insights',
    )
    insight_type = models.CharField(max_length=50, choices=INSIGHT_TYPES)
    content = models.JSONField(help_text="Structured insight data")
    confidence = models.FloatField(
        default=0.0,
        help_text="0.0–1.0 confidence score",
    )
    expires_at = models.DateTimeField(
        null=True, blank=True,
        help_text="Null = never expires (e.g. chat summaries)",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "User Insight"
        verbose_name_plural = "User Insights"
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['user', 'insight_type']),
            models.Index(fields=['expires_at']),
        ]

    @property
    def is_expired(self):
        if self.expires_at is None:
            return False
        return timezone.now() > self.expires_at

    def __str__(self):
        exp = "expired" if self.is_expired else "valid"
        return f"[{self.user.username}] {self.insight_type} ({exp})"


class UsageCost(models.Model):
    """Daily per-user aggregation for budget monitoring and cost alerting."""

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='ai_costs',
    )
    date = models.DateField()
    total_tokens = models.IntegerField(default=0)
    total_messages = models.IntegerField(default=0)
    estimated_cost_usd = models.DecimalField(max_digits=10, decimal_places=6, default=0)

    class Meta:
        verbose_name = "Usage Cost"
        verbose_name_plural = "Usage Costs"
        unique_together = ['user', 'date']
        ordering = ['-date']
        indexes = [
            models.Index(fields=['date', 'estimated_cost_usd']),
        ]

    def __str__(self):
        return f"[{self.user.username}] {self.date} — ${self.estimated_cost_usd}"
