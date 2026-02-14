from django.db import models
from django.conf import settings
import uuid
from django.utils import timezone

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
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['recipient', 'is_read']),
            models.Index(fields=['deduplication_key']),
            models.Index(fields=['recipient', '-created_at']),
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
        constraints = [
            models.UniqueConstraint(fields=['user', 'event_type'], name='unique_user_event_pref')
        ]
    
    @classmethod
    def is_channel_enabled(cls, user, event_type, channel='fcm'):
        # Check specific preference
        try:
            pref = cls.objects.get(user=user, event_type=event_type)
            if not pref.is_enabled:
                return False
            channel_pref = pref.channels.get(channel)
            if channel_pref is False:
                return False
            # If True or None, check 'all' preference?
        except cls.DoesNotExist:
            pass
            
        # Check global 'all' preference if specific not found or inconclusive? 
        # For simplicity, default is True
        return True

class NotificationFailure(models.Model):
    """Dead Letter Queue for failed notification events."""
    event_type = models.CharField(max_length=100)
    event_payload = models.JSONField(help_text="Original event data")
    error_message = models.TextField()
    stack_trace = models.TextField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    retry_count = models.IntegerField(default=0)
    is_resolved = models.BooleanField(default=False)
    
    def __str__(self):
        return f"Failure: {self.event_type} at {self.created_at}"
