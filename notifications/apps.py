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
        # Register listeners
        try:
            import notifications.listeners.social_listeners
        except ImportError:
            pass

        # Security Check: Initialize Firebase
        self._initialize_firebase()

    def _initialize_firebase(self):
        if hasattr(settings, 'FIREBASE_CREDENTIALS_PATH'):
            cred_path = settings.FIREBASE_CREDENTIALS_PATH
        else:
            # Fallback (or raise error if strict)
            cred_path = os.path.join(settings.BASE_DIR, 'yalla-gym-f6a67-firebase-adminsdk-fbsvc-fb99d3a33f.json')
        
        if not os.path.exists(cred_path):
            error_msg = f"CRITICAL: Firebase credentials not found at {cred_path}. Notifications will fail."
            logger.critical(error_msg)
            # In production, this should likely crash the app
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
