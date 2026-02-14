from django.test import TestCase
from django.contrib.auth import get_user_model
from unittest.mock import patch, MagicMock, ANY
from notifications.channels.fcm import FCMChannel
from notifications.models import Notification
from users.models import DeviceToken

User = get_user_model()

class FCMChannelTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='fcmuser', email='fcm@test.com', password='pw', phone_number='5555555555')
        self.notification = Notification.objects.create(
            recipient=self.user,
            event_type='test_event',
            deduplication_key='test_key'
        )

    @patch('notifications.channels.fcm.messaging.send_each_for_multicast')
    def test_chunking(self, mock_send):
        """Test that tokens are chunked in batches of 500"""
        # Create 1050 tokens
        tokens = [DeviceToken(user=self.user, token=f'token_{i}') for i in range(1050)]
        DeviceToken.objects.bulk_create(tokens)
        
        # Mock response side effect to handle different chunk sizes
        def side_effect(message):
            count = len(message.tokens)
            resp = MagicMock()
            resp.success_count = count
            resp.failure_count = 0
            resp.responses = [MagicMock(success=True) for _ in range(count)]
            return resp
            
        mock_send.side_effect = side_effect
        
        # Mock _send_chunk to return success_tokens list
        # Wait, I am mocking messaging.send_each_for_multicast, not _send_chunk directly.
        # But _send_chunk calls mocked method.
        # The test verifies what send_each_for_multicast receives.
        
        FCMChannel.send(self.notification)
        
        # Should be called 3 times: 500, 500, 50
        self.assertEqual(mock_send.call_count, 3)
        # Arguments are inside MulticastMessage object
        # call_args[0][0] is the MulticastMessage
        self.assertEqual(len(mock_send.call_args_list[0][0][0].tokens), 500)
        self.assertEqual(len(mock_send.call_args_list[1][0][0].tokens), 500)
        self.assertEqual(len(mock_send.call_args_list[2][0][0].tokens), 50)

    @patch('notifications.channels.fcm.messaging.send_each_for_multicast')
    def test_idempotency(self, mock_send):
        """Test that already sent tokens are skipped on retry"""
        # Create 3 tokens
        t1 = DeviceToken.objects.create(user=self.user, token='token_1')
        t2 = DeviceToken.objects.create(user=self.user, token='token_2')
        t3 = DeviceToken.objects.create(user=self.user, token='token_3')
        
        # Simulate previous partial success (token_1 already sent)
        self.notification.status = {
            'fcm': {
                'status': 'failed',
                'sent_tokens': ['token_1'],
                'attempts': 1
            }
        }
        self.notification.save()
        
        # Mock response for remaining tokens (token_2, token_3)
        mock_response = MagicMock()
        mock_response.success_count = 2
        mock_response.failure_count = 0
        mock_response.responses = [MagicMock(success=True), MagicMock(success=True)]
        mock_send.return_value = mock_response
        
        FCMChannel.send(self.notification)
        
        # Verify call
        self.assertEqual(mock_send.call_count, 1)
        sent_tokens = mock_send.call_args[0][0].tokens
        self.assertEqual(len(sent_tokens), 2)
        self.assertIn('token_2', sent_tokens)
        self.assertIn('token_3', sent_tokens)
        self.assertNotIn('token_1', sent_tokens) # Should span skipped

    @patch('notifications.channels.fcm.messaging.send_each_for_multicast')
    def test_invalid_token_cleanup(self, mock_send):
        """Test that invalid tokens are marked inactive"""
        token1 = DeviceToken.objects.create(user=self.user, token='valid_token')
        token2 = DeviceToken.objects.create(user=self.user, token='invalid_token')
        
        # Mock response: 1 success, 1 failure (invalid)
        mock_response = MagicMock()
        mock_response.success_count = 1
        mock_response.failure_count = 1
        
        resp1 = MagicMock(success=True)
        resp2 = MagicMock(success=False)
        resp2.exception.code = 'registration-token-not-registered'
        
        mock_response.responses = [resp1, resp2]
        mock_send.return_value = mock_response
        
        FCMChannel.send(self.notification)
        
        # Verify call
        self.assertEqual(mock_send.call_count, 1)
        # Verify logic
        token1.refresh_from_db()
        token2.refresh_from_db()
        
        self.assertTrue(token1.is_active)
        self.assertFalse(token2.is_active)  # Should be soft deleted
