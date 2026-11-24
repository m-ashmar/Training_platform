from django.db import models
from django.conf import settings
import uuid
from django.utils import timezone
import logging

logger = logging.getLogger(__name__)

class Notification(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    recipient = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='app_notifications')
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='triggered_notifications')
    event_type = models.CharField(max_length=100)
    event_id = models.UUIDField(null=True, blank=True, help_text="Correlation ID from Domain Event")
    related_object_id = models.CharField(max_length=255, null=True, blank=True)
    metadata = models.JSONField(default=dict, blank=True)
    is_read = models.BooleanField(default=False)
    status = models.JSONField(default=dict, blank=True, help_text="Channel delivery status (e.g. {'fcm': {'status': 'sent'}})")
    deduplication_key = models.CharField(max_length=255, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        # `id` tiebreaker: a single non-unique sort column is not a total order, so LIMIT/OFFSET
        # paging repeated and hid rows whenever two records shared the value.
        ordering = ['-created_at', '-id']
        indexes = [
            models.Index(fields=['recipient', 'is_read']),
            models.Index(fields=['deduplication_key']),
            models.Index(fields=['recipient', '-created_at']),
            # Matches the real access pattern: WHERE <owner>=? ORDER BY created_at DESC, id DESC.
            # A single-column created_at index cannot serve that; this one can.
            models.Index(fields=['recipient', '-created_at', '-id'], name='notification_owner_recent_idx'),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=['recipient', 'deduplication_key'],
                name='unique_notification_dedup'
            )
        ]

    def __str__(self):
        return f"{self.event_type} for {self.recipient}"

class UserNotificationPreference(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='notification_preferences')
    event_type = models.CharField(max_length=100, help_text="Event type or 'all'")
    is_enabled = models.BooleanField(default=True)
    channels = models.JSONField(default=dict, help_text="{'fcm': True, 'email': False}. Empty means all default.")
    
    class Meta:
        # Deterministic total order. Without it Postgres returns rows in whatever order it
        # likes and LIMIT/OFFSET paging silently repeats and hides rows between pages.
        ordering = ['-id']
        constraints = [
            models.UniqueConstraint(fields=['user', 'event_type'], name='unique_user_event_pref')
        ]
    
    @classmethod
    def is_channel_enabled(cls, user, event_type, channel='fcm'):
        """May this kind of notification be PUSHED to `user` on `channel`?

        The rule (BACKEND_BUGS B-014): the `all` row is a master switch — off, or its
        channel off, and nothing is pushed; then the type's own row may turn its pushes
        off. Nothing here ever stops the in-app row being written: settings govern
        delivery, the inbox keeps the record. No row means on.
        """
        rows = {p.event_type: p for p in cls.objects.filter(user=user,
                                                             event_type__in=('all', event_type))}
        for key in ('all', event_type):
            pref = rows.get(key)
            if pref is None:
                continue
            if not pref.is_enabled or (pref.channels or {}).get(channel) is False:
                return False
        return True

class EmailDelivery(models.Model):
    """One email the notification system owes a person — and whether it left.

    The email channel (`notifications/channels/email.py`; the payments spec, section 7:
    the receipt on a confirmed payment, the 7-day plan notice, the expired plan, and a
    trainer's withdrawal requested / paid / failed). The row is written where the event
    happens, and the worker sends it (`notifications.send_email`): never the request.

    `dedup_key` is what makes it once-only, per recipient, for good: a redelivered event,
    the reminder job's next run, `payment_completed` sent again for an owed payout — each
    finds its row and sends nothing twice. `context` holds the facts the email is written
    from (ids, a number, a date), never a rendered sentence: the text is written at send
    time in the recipient's saved language, like a push.

    `status`: queued -> sending -> sent, or `skipped` (the account cannot or does not
    want to receive it any more; `error` says which), or `failed` (the mail server
    refused it for good, or every retry failed; reported once). No address is stored:
    it is read from the account when the email is sent.
    """

    QUEUED = 'queued'
    SENDING = 'sending'
    SENT = 'sent'
    SKIPPED = 'skipped'
    FAILED = 'failed'
    STATUSES = [(QUEUED, 'Queued'), (SENDING, 'Sending'), (SENT, 'Sent'),
                (SKIPPED, 'Skipped'), (FAILED, 'Failed')]

    recipient = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
                                  related_name='email_deliveries')
    #: The inbox row this email went with, when it went with one. The receipt and the
    #: 7-day notice are queued on their own, so theirs is null; the push
    #: `payment_confirmed` writes its own inbox row (BACKEND_BUGS B-168).
    notification = models.ForeignKey(Notification, on_delete=models.SET_NULL, null=True,
                                     blank=True, related_name='emails')
    kind = models.CharField(max_length=50)
    dedup_key = models.CharField(max_length=255)
    context = models.JSONField(default=dict, blank=True)
    status = models.CharField(max_length=16, choices=STATUSES, default=QUEUED,
                              db_index=True)
    attempts = models.PositiveSmallIntegerField(default=0)
    #: The language it was written in, when it was sent.
    language = models.CharField(max_length=10, blank=True, default='')
    #: Why it was skipped or failed: a reason code or an exception's class name, never
    #: an address or a message body.
    error = models.CharField(max_length=255, blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    last_attempt_at = models.DateTimeField(null=True, blank=True)
    sent_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-created_at', '-id']
        constraints = [
            models.UniqueConstraint(fields=['recipient', 'dedup_key'],
                                    name='unique_email_delivery_dedup'),
        ]
        indexes = [
            models.Index(fields=['status', 'created_at'], name='email_delivery_status_idx'),
        ]

    def __str__(self):
        return f"{self.kind} email for user {self.recipient_id} ({self.status})"


class NotificationFailure(models.Model):
    """Dead Letter Queue for failed notification events."""
    event_type = models.CharField(max_length=100)
    event_payload = models.JSONField(help_text="Original event data")
    error_message = models.TextField()
    stack_trace = models.TextField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    retry_count = models.IntegerField(default=0)
    is_resolved = models.BooleanField(default=False)
    
    def __str__(self):
        return f"Failure: {self.event_type} at {self.created_at}"

    class Meta:
        # Deterministic total order. Without it Postgres returns rows in whatever order it
        # likes and LIMIT/OFFSET paging silently repeats and hides rows between pages.
        ordering = ['-created_at', '-id']
