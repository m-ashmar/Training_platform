from django.apps import AppConfig
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
import firebase_admin
from firebase_admin import credentials
import os
import logging

logger = logging.getLogger(__name__)

class NotificationsConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'notifications'

    def ready(self):
        # NOT guarded, for the same reason as `wallet/apps.py`: these modules ARE the
        # notification system. An ImportError swallowed at DEBUG level starts a platform
        # that quietly sends nothing, and says so in a line production does not log.
        import notifications.listeners.social_listeners  # noqa: F401
        import notifications.listeners.trainer_client_listeners  # noqa: F401

        # Security Check: Initialize Firebase
        self._initialize_firebase()

    def _initialize_firebase(self):
        cred_path = getattr(settings, 'FIREBASE_CREDENTIALS_PATH', '') or ''

        # Not configured → Firebase is an OPTIONAL integration: disable push and
        # continue booting (matches deploy-pipeline.md). Do NOT crash the app.
        if not cred_path:
            logger.warning("FIREBASE_CREDENTIALS_PATH not set — push notifications disabled.")
            return

        # Configured but the file is missing → a real misconfiguration (they
        # intended Firebase but it's wrong): fail closed in production.
        if not os.path.exists(cred_path):
            error_msg = f"Firebase credentials path is set but not found at {cred_path}."
            logger.critical(error_msg)
            if not settings.DEBUG:
                raise ImproperlyConfigured(error_msg)
            return

        try:
            # Check if app is already initialized
            try:
                firebase_admin.get_app()
            except ValueError:
                cred = credentials.Certificate(cred_path)
                firebase_admin.initialize_app(cred)
                logger.info("Firebase Admin SDK initialized successfully.")
        except Exception as e:
            logger.error(f"Failed to initialize Firebase: {e}")
            if not settings.DEBUG:
                raise ImproperlyConfigured(f"Firebase init failed: {e}")
