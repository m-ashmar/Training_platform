from django.test import TestCase, TransactionTestCase
from django.contrib.auth import get_user_model
from django.db import IntegrityError
from django.core.cache import cache
from notifications.domain.events import PostLikedEvent
from notifications.domain.dispatcher import emit_event
from notifications.models import Notification
from notifications.services import NotificationService
import threading
from unittest.mock import patch

User = get_user_model()

class NotificationIntegrationTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='recipient', email='rec@example.com', password='pw', phone_number='1111111111')
        self.actor = User.objects.create_user(username='actor', email='act@example.com', password='pw', phone_number='2222222222')
        # Clear cache for dedup
        cache.clear()

    def test_post_liked_flow(self):
        """Test full flow: Emit Event -> Listener -> Notification Created"""
        event = PostLikedEvent(
            actor_id=self.actor.id,
            target_post_id=100,
            post_author_id=self.user.id
        )
        
        # Emitting synchronously for test
        from notifications.domain.dispatcher import EventDispatcher
        EventDispatcher.dispatch(event)
        
        # Verify Notification Created
        self.assertEqual(Notification.objects.count(), 1)
        notif = Notification.objects.first()
        self.assertEqual(notif.recipient, self.user)
        self.assertEqual(notif.event_type, 'post_liked')
        self.assertEqual(notif.metadata['data']['post_id'], 100)

class NotificationConcurrencyTests(TransactionTestCase):
    # Use TransactionTestCase to test DB constraints with threads
    
    def setUp(self):
        self.user = User.objects.create_user(username='recipient', email='rec@example.com', password='pw', phone_number='3333333333')
        self.actor = User.objects.create_user(username='actor', email='act@example.com', password='pw', phone_number='4444444444')
        cache.clear()

    @patch('notifications.utils.deduplication.DeduplicationService.is_duplicate', return_value=False)
    def test_concurrent_deduplication(self, mock_redis):
        """Simulate 5 concurrent events to verify Layer 2 DB deduplication via Service"""
        
        def create_notification():
            try:
                # Use Service
                NotificationService.create_and_send(
                    recipient=self.user,
                    event_type='post_liked',
                    related_object_id='999',
                    metadata={}
                )
            except Exception as e:
                print(f"Error: {e}")

        threads = []
        for _ in range(5):
            t = threading.Thread(target=create_notification)
            threads.append(t)
            t.start()
            
        for t in threads:
            t.join()
            
        # Should only be 1 notification
        self.assertEqual(Notification.objects.count(), 1)

    def test_recovery_on_duplicate(self):
        """Test that if notification exists, Service returns it (idempotency)"""
        # First creation
        n1 = NotificationService.create_and_send(
            recipient=self.user,
            event_type='test_event',
            related_object_id='1',
            metadata={}
        )
        self.assertIsNotNone(n1)
        
        # Second creation (bypass Redis to force DB text)
        with patch('notifications.utils.deduplication.DeduplicationService.is_duplicate', return_value=False):
             n2 = NotificationService.create_and_send(
                recipient=self.user,
                event_type='test_event',
                related_object_id='1',
                metadata={}
            )
        
        # Should return same object
        self.assertIsNotNone(n2)
        self.assertEqual(n1.id, n2.id)
        self.assertEqual(Notification.objects.count(), 1)
