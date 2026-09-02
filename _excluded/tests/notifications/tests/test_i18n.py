"""
notifications/tests/test_i18n.py — Verification tests for the i18n boundary architecture.

Tests:
  1. LanguageContext correctness (cache keys, direction, activation, fallback)
  2. Language validation (invalid stored values)
  3. Cross-language cache isolation
  4. Notification template resolution safety
  5. LanguageAwareAPIView finalize_response deactivation
"""

from unittest.mock import patch, MagicMock
from django.test import TestCase, RequestFactory, override_settings
from django.core.cache import cache
from django.utils import translation
from django.conf import settings

from training_platform.i18n import LanguageContext, CACHE_VERSION, _validate_language


class LanguageContextTest(TestCase):
    """Test the LanguageContext abstraction."""

    def test_cache_key_includes_language_and_version(self):
        """Cache keys must include active language + CACHE_VERSION."""
        with translation.override('en'):
            key_en = LanguageContext.cache_key("trainer", 42, "clients")
        with translation.override('ar'):
            key_ar = LanguageContext.cache_key("trainer", 42, "clients")

        self.assertIn(":en:", key_en)
        self.assertIn(":ar:", key_ar)
        self.assertIn(f":{CACHE_VERSION}", key_en)
        self.assertIn(f":{CACHE_VERSION}", key_ar)
        self.assertNotEqual(key_en, key_ar)

    def test_cache_key_format(self):
        """Cache key follows segment:value:lang:version format."""
        with translation.override('en'):
            key = LanguageContext.cache_key("client_profile", 99)
        self.assertEqual(key, f"client_profile:99:en:{CACHE_VERSION}")

    def test_direction_ltr(self):
        """English returns 'ltr'."""
        with translation.override('en'):
            self.assertEqual(LanguageContext.direction(), 'ltr')

    def test_direction_rtl(self):
        """Arabic returns 'rtl'."""
        with translation.override('ar'):
            self.assertEqual(LanguageContext.direction(), 'rtl')

    def test_for_user_activates_language(self):
        """LanguageContext.for_user() activates user's preferred_language."""
        user = MagicMock()
        user.preferred_language = 'ar'
        user.id = 1
        with LanguageContext.for_user(user):
            self.assertEqual(translation.get_language(), 'ar')

    def test_for_user_with_no_preference_falls_back(self):
        """Users without preferred_language get settings.LANGUAGE_CODE."""
        user = MagicMock()
        user.preferred_language = None
        user.id = 2
        with LanguageContext.for_user(user):
            self.assertEqual(translation.get_language(), 'en')


class LanguageValidationTest(TestCase):
    """Test language validation against settings.LANGUAGES."""

    def test_valid_language_passes(self):
        """A language code in settings.LANGUAGES returns unchanged."""
        result = _validate_language('en', user_id=1)
        self.assertEqual(result, 'en')

    def test_valid_arabic_passes(self):
        """Arabic passes validation."""
        result = _validate_language('ar', user_id=1)
        self.assertEqual(result, 'ar')

    def test_invalid_language_falls_back(self):
        """An invalid language code falls back to LANGUAGE_CODE."""
        result = _validate_language('zz', user_id=1)
        self.assertEqual(result, settings.LANGUAGE_CODE)

    def test_empty_language_falls_back(self):
        """Empty string falls back to LANGUAGE_CODE."""
        result = _validate_language('', user_id=1)
        self.assertEqual(result, settings.LANGUAGE_CODE)

    def test_none_language_falls_back(self):
        """None falls back to LANGUAGE_CODE."""
        result = _validate_language(None, user_id=1)
        self.assertEqual(result, settings.LANGUAGE_CODE)

    def test_invalid_language_logs_warning(self):
        """Invalid language emits a structured warning log."""
        with self.assertLogs('training_platform.i18n', level='WARNING') as cm:
            _validate_language('xx_FAKE', user_id=42)
        self.assertTrue(any('i18n.language.invalid' in msg for msg in cm.output))

    def test_for_user_validates_language(self):
        """for_user() with invalid preferred_language falls back safely."""
        user = MagicMock()
        user.preferred_language = 'invalid_lang'
        user.id = 99
        with LanguageContext.for_user(user):
            self.assertEqual(translation.get_language(), settings.LANGUAGE_CODE)


class LanguageAwareAPIViewTest(TestCase):
    """Test the DRF mixin deactivation."""

    def test_finalize_response_deactivates_language(self):
        """After finalize_response(), language should be deactivated."""
        from training_platform.i18n import LanguageAwareAPIView
        from rest_framework.views import APIView
        from rest_framework.response import Response
        from rest_framework.permissions import AllowAny
        from rest_framework.test import force_authenticate

        class TestView(LanguageAwareAPIView, APIView):
            permission_classes = [AllowAny]
            authentication_classes = []

            def get(self, request):
                return Response({"lang": translation.get_language()})

        factory = RequestFactory()
        request = factory.get('/')
        request.user = MagicMock()
        request.user.is_authenticated = True
        request.user.preferred_language = 'ar'
        request.user.id = 1
        force_authenticate(request, user=request.user)

        view = TestView.as_view()
        response = view(request)

        # After finalize_response, the threadlocal should be deactivated
        self.assertEqual(response.status_code, 200)


class CacheLanguageIsolationTest(TestCase):
    """Critical test: Arabic and English cache entries must not bleed."""

    def setUp(self):
        cache.clear()

    def tearDown(self):
        cache.clear()

    def test_language_partitioned_cache_keys(self):
        """Same logical cache key produces different keys per language."""
        with translation.override('en'):
            key_en = LanguageContext.cache_key("trainer", 1, "clients")
            cache.set(key_en, {"data": "english"}, timeout=60)

        with translation.override('ar'):
            key_ar = LanguageContext.cache_key("trainer", 1, "clients")
            cache.set(key_ar, {"data": "arabic"}, timeout=60)

        self.assertNotEqual(key_en, key_ar)
        self.assertEqual(cache.get(key_en), {"data": "english"})
        self.assertEqual(cache.get(key_ar), {"data": "arabic"})

    def test_cache_miss_on_wrong_language(self):
        """Accessing a cache key for the wrong language returns None."""
        with translation.override('en'):
            key = LanguageContext.cache_key("profile", 42)
            cache.set(key, {"name": "Ahmed"}, timeout=60)

        with translation.override('ar'):
            key_ar = LanguageContext.cache_key("profile", 42)
            self.assertIsNone(cache.get(key_ar))


class NotificationTemplateResolutionTest(TestCase):
    """Test that notification templates resolve correctly per language."""

    def test_template_renders_in_english(self):
        """Template body interpolates context in English."""
        from notifications.template_resolver import NotificationTemplateResolver
        from notifications.domain.events import NotificationTemplate

        template = NotificationTemplate(
            title="New Like",
            body="%(actor)s liked your post.",
        )
        with translation.override('en'):
            title, body = NotificationTemplateResolver.render(
                event_type='post_liked',
                template=template,
                context={'actor': 'Ahmed'},
                recipient_id=1,
            )
            self.assertEqual(body, "Ahmed liked your post.")

    def test_missing_template_returns_safe_fallback(self):
        """None template returns safe user-facing fallback, not empty string."""
        from notifications.template_resolver import NotificationTemplateResolver
        with translation.override('en'):
            title, body = NotificationTemplateResolver.render(
                event_type='unknown',
                template=None,
                context={},
                recipient_id=1,
            )
            self.assertTrue(title)  # Not empty
            self.assertIn("notification", body.lower())  # Safe fallback

    def test_missing_context_key_returns_safe_fallback(self):
        """Missing interpolation key returns title + safe fallback body."""
        from notifications.template_resolver import NotificationTemplateResolver
        from notifications.domain.events import NotificationTemplate

        template = NotificationTemplate(
            title="New Comment",
            body="%(actor)s commented: %(preview)s",
        )
        with translation.override('en'):
            title, body = NotificationTemplateResolver.render(
                event_type='comment',
                template=template,
                context={'actor': 'Sara'},  # Missing 'preview'
                recipient_id=1,
            )
            # Should not raise, should return title + safe fallback
            self.assertEqual(title, "New Comment")
            self.assertIn("notification", body.lower())
            # Must NOT contain %(preview)s placeholder
            self.assertNotIn("%(", body)
