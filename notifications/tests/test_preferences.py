from django.test import TestCase
from django.contrib.auth import get_user_model
from unittest.mock import patch
from notifications.models import Notification, UserNotificationPreference
from notifications.services import NotificationService
from notifications.channels.fcm import FCMChannel

User = get_user_model()

class UserPreferenceTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='prefuser', email='pref@test.com', password='pw', phone_number='6666666666')
        self.actor = User.objects.create_user(username='actor', email='act@test.com', password='pw', phone_number='7777777777')

    def test_global_disable(self):
        """Test that notification is NOT created if event type is disabled"""
        UserNotificationPreference.objects.create(
            user=self.user,
            event_type='post_liked',
            is_enabled=False
        )
        
        notif = NotificationService.create_and_send(
            recipient=self.user,
            event_type='post_liked',
            related_object_id='123',
            metadata={}
        )
        
        self.assertIsNone(notif)
        self.assertEqual(Notification.objects.count(), 0)

    @patch('notifications.channels.fcm.FCMChannel.send')
    def test_channel_disable(self, mock_fcm_send):
        """Test that notification IS created but FCM is skipped if channel disabled"""
        UserNotificationPreference.objects.create(
            user=self.user,
            event_type='post_liked',
            is_enabled=True,
            channels={'fcm': False}
        )
        
        notif = NotificationService.create_and_send(
            recipient=self.user,
            event_type='post_liked',
            related_object_id='123',
            metadata={}
        )
        
        self.assertIsNotNone(notif)
        self.assertEqual(Notification.objects.count(), 1)
        
        # Verify FCM send was NOT called
        mock_fcm_send.assert_not_called()

    @patch('notifications.channels.fcm.FCMChannel.send')
    def test_default_enable(self, mock_fcm_send):
        """Test default behavior (enabled)"""
        notif = NotificationService.create_and_send(
            recipient=self.user,
            event_type='post_liked',
            related_object_id='123',
            metadata={}
        )
        
        self.assertIsNotNone(notif)
        # Verify FCM send WAS called
        mock_fcm_send.assert_called_once()
