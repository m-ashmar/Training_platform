from django.apps import AppConfig
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
import firebase_admin
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
        import notifications.listeners.progress_listeners  # noqa: F401
        import notifications.listeners.chat_listeners  # noqa: F401
        import notifications.listeners.verification_listeners  # noqa: F401

        # Security Check: Initialize Firebase
        self._initialize_firebase()

    def _initialize_firebase(self):
        """Start the SDK when credentials are configured; refuse to boot when they are
        configured and wrong.

        Where the credentials come from is decided in
        `training_platform.firebase_credentials`: the JSON itself
        (FIREBASE_CREDENTIALS_JSON, what production can actually be given) or a path
        (local development). This used to read the path only, and
        `social.firebase_service` disagreed with it about what "configured" meant.
        """
        from training_platform.firebase_credentials import (
            FirebaseCredentialsError, build_certificate)

        try:
            certificate = build_certificate(settings)
        except FirebaseCredentialsError as exc:
            # Configured but unusable: a real misconfiguration (they intended Firebase
            # and got it wrong). Fail closed in production.
            logger.critical(str(exc))
            if not settings.DEBUG:
                raise ImproperlyConfigured(str(exc)) from exc
            return

        # Not configured → Firebase is an OPTIONAL integration: disable push and
        # continue booting (matches deploy-pipeline.md). Do NOT crash the app.
        if certificate is None:
            logger.warning(
                "Neither FIREBASE_CREDENTIALS_JSON nor FIREBASE_CREDENTIALS_PATH is set "
                "— push notifications disabled.")
            return

        try:
            try:
                firebase_admin.get_app()
            except ValueError:
                firebase_admin.initialize_app(certificate)
                logger.info("Firebase Admin SDK initialized successfully.")
        except Exception as e:
            logger.error(f"Failed to initialize Firebase: {e}")
            if not settings.DEBUG:
                raise ImproperlyConfigured(f"Firebase init failed: {e}")
