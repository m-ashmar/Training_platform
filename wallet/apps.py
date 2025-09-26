from django.apps import AppConfig


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
            pass


