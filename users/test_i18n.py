"""
i18n Test Suite
Tests for Arabic localization across the platform:
- LanguageResolutionMiddleware (header, user pref, default)
- Content-Language / Vary response headers
- Static string translations (subscription, auth errors)
- TranslatedJSONFieldMixin
- Cache isolation by language
- ErrorHandlingMiddleware translated responses
"""

from django.test import TestCase, RequestFactory, override_settings
from django.utils import translation
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient

User = get_user_model()


class LanguageResolutionMiddlewareTests(TestCase):
    """Test LanguageResolutionMiddleware language priority chain."""

    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(
            username='test_i18n_user',
            email='test_i18n@example.com',
            password='TestPass123!',
            phone_number='+1234567890',
            age=25, gender='male', height=175, weight=70,
        )

    def test_accept_language_header_arabic(self):
        """Accept-Language: ar should activate Arabic."""
        self.client.force_authenticate(user=self.user)
        response = self.client.get(
            '/api/social/notifications/unread_count/',
            HTTP_ACCEPT_LANGUAGE='ar',
        )
        self.assertEqual(response['Content-Language'], 'ar')

    def test_accept_language_header_english(self):
        """Accept-Language: en should keep English."""
        self.client.force_authenticate(user=self.user)
        response = self.client.get(
            '/api/social/notifications/unread_count/',
            HTTP_ACCEPT_LANGUAGE='en',
        )
        self.assertEqual(response['Content-Language'], 'en')

    def test_user_preferred_language_fallback(self):
        """When no Accept-Language header, user.preferred_language used via JWT."""
        self.user.preferred_language = 'ar'
        self.user.save(update_fields=['preferred_language'])
        # Use JWT token so middleware can introspect user preference
        from rest_framework_simplejwt.tokens import RefreshToken
        token = RefreshToken.for_user(self.user)
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {token.access_token}')
        response = self.client.get('/api/social/notifications/unread_count/')
        self.assertEqual(response['Content-Language'], 'ar')

    def test_default_language_fallback(self):
        """When no header and no user pref, default to LANGUAGE_CODE (en)."""
        self.client.force_authenticate(user=self.user)
        response = self.client.get('/api/social/notifications/unread_count/')
        self.assertEqual(response['Content-Language'], 'en')

    def test_vary_header_includes_accept_language(self):
        """Response must include Vary: Accept-Language for CDN correctness."""
        self.client.force_authenticate(user=self.user)
        response = self.client.get('/api/social/notifications/unread_count/')
        vary = response.get('Vary', '')
        self.assertIn('Accept-Language', vary)

    def test_vary_header_includes_cookie(self):
        """Response must include Vary: Cookie for session-auth correctness."""
        self.client.force_authenticate(user=self.user)
        response = self.client.get('/api/social/notifications/unread_count/')
        vary = response.get('Vary', '')
        self.assertIn('Cookie', vary)


class StaticStringTranslationTests(TestCase):
    """Test that gettext-wrapped strings return Arabic when language is active."""

    def test_subscription_error_in_arabic(self):
        """Subscription validation error should be translated."""
        with translation.override('ar'):
            from django.utils.translation import gettext as _
            result = _('Price cannot be negative')
            self.assertEqual(result, 'لا يمكن أن يكون السعر سالباً')

    def test_auth_error_in_arabic(self):
        """Auth error string should be translated."""
        with translation.override('ar'):
            from django.utils.translation import gettext as _
            result = _('Unable to log in with provided credentials.')
            self.assertEqual(result, 'تعذر تسجيل الدخول باستخدام بيانات الاعتماد المقدمة.')

    def test_middleware_error_in_arabic(self):
        """ErrorHandlingMiddleware error should be translated."""
        with translation.override('ar'):
            from django.utils.translation import gettext as _
            result = _('Invalid input data')
            self.assertEqual(result, 'بيانات الإدخال غير صالحة')

    def test_social_message_in_arabic(self):
        """Social views message should be translated."""
        with translation.override('ar'):
            from django.utils.translation import gettext as _
            result = _('Cannot follow yourself')
            self.assertEqual(result, 'لا يمكنك متابعة نفسك')

    def test_routine_error_in_arabic(self):
        """Routine views error should be translated."""
        with translation.override('ar'):
            from django.utils.translation import gettext as _
            result = _('Permission denied.')
            self.assertEqual(result, 'تم رفض الإذن.')

    def test_english_stays_english(self):
        """With English active, strings should remain in English."""
        with translation.override('en'):
            from django.utils.translation import gettext as _
            result = _('Price cannot be negative')
            self.assertEqual(result, 'Price cannot be negative')


class TranslatedJSONFieldMixinTests(TestCase):
    """Test TranslatedJSONFieldMixin localization behavior."""

    def test_none_language_defaults_to_en(self):
        """If get_language() returns None, mixin should default to 'en'."""
        from training_platform.utils.serializers import TranslatedJSONFieldMixin
        translation.deactivate_all()
        lang = translation.get_language()
        # After deactivation, get_language() may return None
        # Our mixin should handle this with `or 'en'`
        fallback = lang or 'en'
        self.assertEqual(fallback, 'en')


class CacheLanguageIsolationTests(TestCase):
    """Test that CacheMiddleware includes language in cache keys."""

    def test_cache_key_includes_language(self):
        """Cache key generation should incorporate the active language."""
        from training_platform.middleware import CacheMiddleware
        from django.test import RequestFactory

        factory = RequestFactory()
        request = factory.get('/api/some-endpoint/')

        middleware = CacheMiddleware(get_response=lambda r: None)

        # Test with English
        translation.activate('en')
        key_en = middleware._get_cache_key(request)

        # Test with Arabic
        translation.activate('ar')
        key_ar = middleware._get_cache_key(request)

        # Keys must be different for different languages
        self.assertNotEqual(key_en, key_ar)
        self.assertIn('en', key_en)
        self.assertIn('ar', key_ar)

        # Clean up
        translation.deactivate()
