from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase
from django.contrib.auth import get_user_model
from unittest.mock import patch, MagicMock
from social.firebase_service import FirebaseNotificationService
from users.models import DeviceToken
from social.models import Post, Comment, Challenge

User = get_user_model()

class FirebaseNotificationTests(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username='testuser', email='test@example.com', password='password123', phone_number='1234567890'
        )
        self.other_user = User.objects.create_user(
            username='otheruser', email='other@example.com', password='password123', phone_number='0987654321'
        )
        self.token = DeviceToken.objects.create(user=self.user, token='fake_token_123')
        self.client.force_authenticate(user=self.other_user)

    @patch('social.firebase_service.messaging')
    def test_firebase_service_send_to_token(self, mock_messaging):
        """Test direct send to token via service"""
        service = FirebaseNotificationService()
        mock_messaging.send.return_value = 'projects/test/messages/123'
        
        result = service.send_to_token(
            token='fake_token',
            title='Test',
            body='Body',
            data={'key': 'value'}
        )
        
        self.assertTrue(result)
        mock_messaging.send.assert_called_once()

    @patch('social.tasks.FirebaseNotificationService')
    def test_follow_notification_trigger(self, MockService):
        """Test that following a user triggers FCM notification task"""
        mock_instance = MockService.return_value
        mock_instance.send_each_for_multicast.return_value = MagicMock(success_count=1, failure_count=0)
        
        url = reverse('social:follow-follow-user')
        data = {'user_id': self.user.id}
        
        # We need to run celery tasks synchronously for investigation, 
        # but here we are mocking the service inside the task or mocking the task itself.
        # Let's patch the task directly in the view to verify it's called.
        with patch('social.views.dispatch_notification') as mock_task:
            response = self.client.post(url, data)
            self.assertEqual(response.status_code, status.HTTP_201_CREATED)
            mock_task.assert_called_once()
            
            # Verify arguments
            call_kwargs = mock_task.call_args[1]
            self.assertEqual(call_kwargs['user_id'], self.user.id)
            self.assertEqual(call_kwargs['title'], 'New Follower')
            self.assertEqual(call_kwargs['data']['type'], 'follow')

    @patch('social.views.dispatch_notification')
    def test_like_notification_trigger(self, mock_task):
        """Test that liking a post triggers FCM notification"""
        post = Post.objects.create(author=self.user, content="Test Post", visibility='public')
        
        url = reverse('social:post-like', kwargs={'pk': post.id})
        response = self.client.post(url)
        
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        mock_task.assert_called_once()
        
        call_kwargs = mock_task.call_args[1]
        self.assertEqual(call_kwargs['user_id'], self.user.id)
        self.assertEqual(call_kwargs['title'], 'Post Liked')

    @patch('social.views.dispatch_notification')
    def test_comment_notification_trigger(self, mock_task):
        """Test that commenting on a post triggers FCM notification"""
        post = Post.objects.create(author=self.user, content="Test Post", visibility='public')
        
        url = reverse('social:comment-list')
        data = {
            'post': post.id,
            'content': 'Nice post!'
        }
        response = self.client.post(url, data)
        
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        mock_task.assert_called_once()
        
        call_kwargs = mock_task.call_args[1]
        self.assertEqual(call_kwargs['user_id'], self.user.id)
        self.assertEqual(call_kwargs['title'], 'New Comment')

class DeviceTokenTests(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username='testuser', email='test@example.com', password='password123', phone_number='1234567890'
        )
        self.client.force_authenticate(user=self.user)
        self.url = reverse('users:fcm_token_manage')

    def test_register_token(self):
        """Test registering a new device token"""
        data = {'token': 'new_token_123'}
        response = self.client.post(self.url, data)
        
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertTrue(DeviceToken.objects.filter(user=self.user, token='new_token_123').exists())

    def test_register_existing_token(self):
        """Test registering an existing token returns 200"""
        DeviceToken.objects.create(user=self.user, token='existing_token')
        data = {'token': 'existing_token'}
        response = self.client.post(self.url, data)
        
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_unregister_token(self):
        """Test unregistering a token"""
        DeviceToken.objects.create(user=self.user, token='delete_me')
        data = {'token': 'delete_me'}
        response = self.client.delete(self.url, data)
        
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertFalse(DeviceToken.objects.filter(token='delete_me').exists())

    def test_register_reassign_token(self):
        """Test registering a token that belongs to another user (reassignment)"""
        # Create token for another user
        other_user = User.objects.create_user(
            username='previous_owner', email='prev@example.com', password='password123', phone_number='0987654321'
        )
        DeviceToken.objects.create(user=other_user, token='shared_device_token')
        
        # Try to register same token with current user
        data = {'token': 'shared_device_token'}
        response = self.client.post(self.url, data)
        
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        
        # Verify token is now assigned to current user
        token_obj = DeviceToken.objects.get(token='shared_device_token')
        self.assertEqual(token_obj.user, self.user)
        self.assertNotEqual(token_obj.user, other_user)
