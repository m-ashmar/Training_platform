default_app_config = "wallet.apps.WalletConfig"




def currency_for(user) -> str:
    """The currency this person trades in. See `wallet.models.currency_for`.

    Re-exported from the package so callers outside the wallet app can ask the
    question without importing a models module and inheriting its import order.
    """
    from .models import currency_for as _currency_for

    return _currency_for(user)
