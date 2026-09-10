from django.apps import AppConfig
import logging

logger = logging.getLogger(__name__)


class WalletConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "wallet"
    verbose_name = "Wallet"

    def ready(self):
        # NOT guarded. `wallet.signals` connects `create_wallet_and_agent` — every new
        # client and trainer gets their wallet there — and `handle_payment_completed`,
        # which is how a trainer is paid. Swallowing an ImportError at DEBUG level meant
        # a typo in that module started the platform successfully with no wallets being
        # created and no trainer ever being paid, and said so in a log line
        # `settings_production` does not emit. If this module cannot be imported, the
        # process must not come up.
        import wallet.signals  # noqa: F401


