"""
models.py — AI Assistant Data Models

Six models for chat, training data collection, behavior tracking, cached insights,
and usage cost monitoring. Only `AITrainingData` is training data: anonymous copies
written while the person has "help us improve" on (off by default). Nothing else here
is ever used to train a model.
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
        # `id` tiebreaker: a single non-unique sort column is not a total order, so LIMIT/OFFSET
        # paging repeated and hid rows whenever two records shared the value.
        ordering = ['-updated_at', '-id']
        indexes = [
            models.Index(fields=['user', 'is_active']),
            models.Index(fields=['updated_at', 'is_active']),
            # Matches the real access pattern: WHERE <owner>=? ORDER BY created_at DESC, id DESC.
            # A single-column created_at index cannot serve that; this one can.
            models.Index(fields=['user', '-created_at', '-id'], name='chatsession_owner_recent_idx'),
            # Matches the real access pattern: WHERE user=? ORDER BY updated_at DESC, id DESC.
            # Measured on a power user with 5,050 rows: 0.682 ms -> 0.089 ms, because
            # the planner stops sorting their entire history to return 25 rows.
            models.Index(fields=['user', '-updated_at', '-id'], name='chatsession_recent_idx'),
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
    # The user's 👍/👎 on an assistant reply. It belongs to the reply, not to the
    # training row: training rows exist only for users who opted into training-data
    # consent (off by default), and are written by a background task that may run AFTER
    # the tap. Stored only there, feedback from every default user matched zero rows and
    # answered "Could not save feedback."
    feedback = models.CharField(
        max_length=10, null=True, blank=True,
        choices=[('positive', '👍'), ('negative', '👎')],
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Chat Message"
        verbose_name_plural = "Chat Messages"
        # `id` tiebreaker: a single non-unique sort column is not a total order, so LIMIT/OFFSET
        # paging repeated and hid rows whenever two records shared the value.
        ordering = ['created_at', 'id']
        indexes = [
            models.Index(fields=['session', 'created_at']),
        ]

    def __str__(self):
        preview = self.content[:60] + ("..." if len(self.content) > 60 else "")
        return f"[{self.role}] {preview}"

    def record_feedback(self, user, feedback):
        """Store `user`'s 👍/👎 on this reply, and mirror it onto the training row.

        ONE implementation for both surfaces. The socket stored it on the message; the
        REST route (`POST /api/ai/feedback/`) updated only `AITrainingData`, which
        exists only for the few who opted into training data, and answered "Feedback
        saved" for everyone else with nothing saved (SIDE_BUGS S-061).

        Returns how many training copies were updated (0 for a user without training
        consent, or before the copy is written, which copies the value from the message
        when it is written).
        """
        self.feedback = feedback
        self.save(update_fields=['feedback'])
        # By the two keyed pseudonyms: the copy has no FK to the reply or the person
        # (the owner's decision of 2026-09-24).
        from ai_assistant.anonymise import reply_key_for

        updated = AITrainingData.copies_of(user).filter(
            reply_key=reply_key_for(self)).update(user_feedback=feedback)
        return updated


def training_copy_deadline():
    """When a training copy written now must be gone: `AI_TRAINING_RETENTION_DAYS` from
    now, never more than `AI_TRAINING_RETENTION_MAX_DAYS` (365)."""
    from datetime import timedelta

    return timezone.now() + timedelta(days=settings.AI_TRAINING_RETENTION_DAYS)


class AITrainingData(models.Model):
    """An anonymous copy of one answered turn, kept to improve the coach.

    Written only while the person has "help us improve" on
    (`POST /api/ai/training-consent/`), by `services/data_collector.py`. The owner's
    decision of 2026-09-24: a copy holds nothing that identifies the person
    (`ai_assistant/anonymise.py` says exactly what it holds):

    * no user, chat or message FK. `subject_key` is the only link to the person: a
      keyed pseudonym (HMAC-SHA256 over the user id with a server secret that is not in
      the database). It is here so that switching "help us improve" off, `DELETE
      /api/ai/data/`, account erasure and deleting the account can find the copies and
      delete them. `reply_key` is the same kind of key for the reply, so a later 👍/👎
      reaches the copy.
    * `user_context_snapshot` holds bands and codes (`anonymise.SNAPSHOT_KEYS`), never a
      measurement, a name or free text.
    * the text and the tool output have direct identifiers removed.
    * `retain_until` is always set, and the copy is deleted when it passes
      (`tasks.purge_expired_training_data`, daily).
    """

    FEEDBACK_CHOICES = [
        ('positive', '👍'),
        ('negative', '👎'),
    ]

    #: The keyed pseudonym of the person (`anonymise.subject_key_for`).
    subject_key = models.CharField(max_length=64, db_index=True)
    #: The keyed pseudonym of the reply this copy records (`anonymise.reply_key_for`);
    #: null for a copy written before replies were keyed.
    reply_key = models.CharField(max_length=64, null=True, blank=True, db_index=True)
    # Written only when the person had opted in.
    consented = models.BooleanField(
        default=False, db_index=True,
        help_text="User opted in to their data being retained for model training.",
    )
    retain_until = models.DateTimeField(
        default=training_copy_deadline, db_index=True,
        help_text="Purge date. Rows past this are deleted by purge_expired_training_data.",
    )
    # Bands and codes only (`anonymise.SNAPSHOT_KEYS`).
    user_context_snapshot = models.JSONField(
        help_text="Profile bands and codes at interaction time",
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
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        verbose_name = "AI Training Data"
        verbose_name_plural = "AI Training Data"
        # `id` tiebreaker: a single non-unique sort column is not a total order, so LIMIT/OFFSET
        # paging repeated and hid rows whenever two records shared the value.
        ordering = ['-created_at', '-id']
        indexes = [
            models.Index(fields=['user_feedback']),
        ]

    @classmethod
    def copies_of(cls, user):
        """Every training copy kept for `user` (a user or a user id), by `subject_key`.

        The only way to find a person's copies. Switching "help us improve" off, `GET` and
        `DELETE /api/ai/data/`, account erasure and deleting the account all use it."""
        from ai_assistant.anonymise import subject_key_for

        return cls.objects.filter(subject_key=subject_key_for(user))

    def __str__(self):
        fb = self.user_feedback or "no feedback"
        return f"[training copy {self.pk}] {fb} — {self.response_tokens} tokens"


class UserBehaviorEvent(models.Model):
    """
    Tracks user actions across the entire platform via Django signals.

    What the AI coach's notes about a person's habits are computed from (`UserInsight`,
    proactive engagement), and nothing more: these rows are NOT training data and never
    feed a model. Training uses only the anonymous `AITrainingData` copies, written
    while the person has opted in (SIDE_BUGS S-225; the privacy registry's
    `behaviour_events` purpose says the same).
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
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        verbose_name = "User Behavior Event"
        verbose_name_plural = "User Behavior Events"
        # `id` tiebreaker: a single non-unique sort column is not a total order, so LIMIT/OFFSET
        # paging repeated and hid rows whenever two records shared the value.
        ordering = ['-created_at', '-id']
        indexes = [
            models.Index(fields=['user', 'created_at']),
            models.Index(fields=['event_type', 'created_at']),
            # Matches the real access pattern: WHERE <owner>=? ORDER BY created_at DESC, id DESC.
            # A single-column created_at index cannot serve that; this one can.
            models.Index(fields=['user', '-created_at', '-id'], name='ubehavevent_recent_idx'),
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
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        verbose_name = "User Insight"
        verbose_name_plural = "User Insights"
        # `id` tiebreaker: a single non-unique sort column is not a total order, so LIMIT/OFFSET
        # paging repeated and hid rows whenever two records shared the value.
        ordering = ['-created_at', '-id']
        indexes = [
            models.Index(fields=['user', 'insight_type']),
            models.Index(fields=['expires_at']),
            # Matches the real access pattern: WHERE <owner>=? ORDER BY created_at DESC, id DESC.
            # A single-column created_at index cannot serve that; this one can.
            models.Index(fields=['user', '-created_at', '-id'], name='userinsight_owner_recent_idx'),
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
        # `id` tiebreaker: a single non-unique sort column is not a total order, so LIMIT/OFFSET
        # paging repeated and hid rows whenever two records shared the value.
        ordering = ['-date', '-id']
        indexes = [
            models.Index(fields=['date', 'estimated_cost_usd']),
        ]

    def __str__(self):
        return f"[{self.user.username}] {self.date} — ${self.estimated_cost_usd}"
