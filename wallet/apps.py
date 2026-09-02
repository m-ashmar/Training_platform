from django.apps import AppConfig
import logging

logger = logging.getLogger(__name__)


class WalletConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "wallet"
    verbose_name = "Wallet"

    def ready(self):
        # Import signals on app ready
        try:
            import wallet.signals  # noqa: F401
        except Exception:
            # Signals import should not break app startup
            # Optional side effect: swallowing this silently is what made the
            # surrounding failures invisible in logs. Control flow is unchanged.
            logger.debug('suppressed non-fatal error', exc_info=True)


