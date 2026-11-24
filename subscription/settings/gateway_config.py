"""
Payment Gateway Configuration
============================

Single active gateway: ShamCash. Secrets are loaded from the environment with
NO usable hardcoded fallbacks; production readiness is enforced at startup by
`enforce_production_safety()` in settings_production.py.

The API's base URL has no fallback either (the owner, 2026-11-24). The gateway waits for
ShamCash's own official access: until `SHAMCASH_API_URL` is set, nothing reads a
merchant account and no payment API is ever called (`api_reads_configured`), whatever
token is set. Checkouts then take the manual transfer a person reviews, when the
merchant account is set (`gateway_kind` 'reviewed'), or are refused
(`gateway_unavailable`).
"""

import logging
import os
from decimal import Decimal, InvalidOperation
from typing import Dict, Any, Optional

logger = logging.getLogger(__name__)

# ========================
# Environment Configuration
# ========================
# Secure defaults: production mode, debug off. Local dev must opt into sandbox.
GATEWAY_MODE = os.getenv('GATEWAY_MODE', 'production')  # 'sandbox' or 'production'
DEBUG_MODE = os.getenv('PAYMENT_DEBUG', 'False').lower() == 'true'

# ========================
# Gateway Base Configuration
# ========================
GATEWAY_TIMEOUT = 30  # seconds
GATEWAY_RETRY_ATTEMPTS = 3
GATEWAY_RETRY_DELAY = 2  # seconds

# ========================
# ShamCash Configuration (secrets from env only — no hardcoded fallbacks)
# ========================
SHAMCASH_CONFIG = {
    'sandbox': {
        'api_key': os.getenv('SHAMCASH_SANDBOX_API_TOKEN', ''),
        'api_url': os.getenv('SHAMCASH_SANDBOX_API_URL', ''),     # no default: see above
        'webhook_secret': os.getenv('SHAMCASH_SANDBOX_WEBHOOK_SECRET', ''),
        'merchant_id': os.getenv('SHAMCASH_SANDBOX_ACCOUNT_ID', ''),
        'initiate_path': os.getenv('SHAMCASH_SANDBOX_INITIATE_PATH', ''),  # set post-approval for hosted mode
        'currency': 'SYP',
        'timeout': GATEWAY_TIMEOUT,
        'retry_attempts': GATEWAY_RETRY_ATTEMPTS,
    },
    'production': {
        'api_key': os.getenv('SHAMCASH_API_TOKEN', ''),
        'api_url': os.getenv('SHAMCASH_API_URL', ''),     # no default: see above
        'webhook_secret': os.getenv('SHAMCASH_WEBHOOK_SECRET', ''),
        'merchant_id': os.getenv('SHAMCASH_ACCOUNT_ID', ''),
        'initiate_path': os.getenv('SHAMCASH_INITIATE_PATH', ''),
        'currency': 'SYP',
        'timeout': GATEWAY_TIMEOUT,
        'retry_attempts': GATEWAY_RETRY_ATTEMPTS,
    },
}

def _amount_limits_from_env(prefix: str, currency: str) -> Optional[Dict[str, Decimal]]:
    """`{'min', 'max'}` for one currency from `<PREFIX>_<CUR>_MIN_AMOUNT` / `_MAX_AMOUNT`,
    or None — never a guessed number.

    Both must be set, both must be positive decimals and min must not exceed max; any
    other state is "this gateway does not charge in that currency", which is the
    fail-closed default. A malformed value is logged rather than raised, so a typo in
    one variable refuses dollar checkouts instead of taking the whole process down.
    """
    raw_min = os.getenv(f'{prefix}_{currency}_MIN_AMOUNT', '').strip()
    raw_max = os.getenv(f'{prefix}_{currency}_MAX_AMOUNT', '').strip()
    if not raw_min and not raw_max:
        return None
    try:
        low, high = Decimal(raw_min), Decimal(raw_max)
    except (InvalidOperation, ValueError):
        logger.error("%s_%s_MIN_AMOUNT/_MAX_AMOUNT are not numbers; %s checkouts stay "
                     "refused", prefix, currency, currency)
        return None
    if not (low.is_finite() and high.is_finite()) or low <= 0 or high < low:
        logger.error("%s_%s amount limits %s..%s are not a usable range; %s checkouts "
                     "stay refused", prefix, currency, raw_min, raw_max, currency)
        return None
    return {'min': low, 'max': high}


_SHAMCASH_USD_LIMITS = _amount_limits_from_env('SHAMCASH', 'USD')

# ========================
# Gateway Registry
# ========================
GATEWAY_REGISTRY = {
    'shamcash': {
        'name': 'ShamCash',
        'config': SHAMCASH_CONFIG,
        'class_name': 'ShamCashGateway',
        'supported_currencies': ['SYP', 'USD'],
        # Bounds are PER CURRENCY. A single pair of numbers was documented as SYP and
        # then compared against amounts of any currency, so a plan priced in USD was
        # measured against a Syrian-pound floor. A currency with no entry here cannot
        # be charged at all — fail closed rather than borrow another currency's bounds.
        #
        # USD has NO built-in bounds (SIDE_BUGS S-052): they are the owner's numbers and
        # nobody has given them yet, so every dollar checkout is refused with
        # `currency_not_chargeable` until SHAMCASH_USD_MIN_AMOUNT and
        # SHAMCASH_USD_MAX_AMOUNT are both set. No default is invented here.
        'amount_limits': {
            'SYP': {'min': Decimal('100'), 'max': Decimal('5000000')},
            **({'USD': _SHAMCASH_USD_LIMITS} if _SHAMCASH_USD_LIMITS else {}),
        },
    },
}

# ========================
# Utility Functions
# ========================
def get_gateway_config(gateway_name: str) -> Dict[str, Any]:
    """Get configuration for a specific gateway based on the current environment."""
    if gateway_name not in GATEWAY_REGISTRY:
        raise ValueError(f"Unknown gateway: {gateway_name}")
    config = GATEWAY_REGISTRY[gateway_name]['config'].get(GATEWAY_MODE)
    if not config:
        raise ValueError(f"No configuration found for {gateway_name} in {GATEWAY_MODE} mode")
    # Merge webhook header config so the gateway can read it from one place.
    merged = dict(config)
    merged.setdefault('webhook_signature_header', SECURITY_CONFIG['webhook_signature_header'])
    merged.setdefault('webhook_timestamp_header', SECURITY_CONFIG['webhook_timestamp_header'])
    merged.setdefault('signature_expiry', SECURITY_CONFIG['signature_expiry'])
    return merged


def get_gateway_info(gateway_name: str) -> Dict[str, Any]:
    """Get gateway information including supported features."""
    if gateway_name not in GATEWAY_REGISTRY:
        raise ValueError(f"Unknown gateway: {gateway_name}")
    return GATEWAY_REGISTRY[gateway_name]


def get_amount_limits(gateway_name: str, currency: str) -> Dict[str, Decimal]:
    """Min/max chargeable amount for one gateway in one currency.

    Returns None when the currency has no configured bounds, which callers must
    treat as "cannot charge in this currency".
    """
    info = get_gateway_info(gateway_name)
    return (info.get('amount_limits') or {}).get(str(currency).upper())


def api_reads_configured(config: Dict[str, Any]) -> bool:
    """Whether the server may call this gateway's API: a token AND the base URL. The
    URL has no default (the module docstring), so a token alone calls nothing."""
    return bool(config.get('api_key') and config.get('api_url'))


def is_gateway_enabled(gateway_name: str) -> bool:
    """A gateway is enabled only if its API can be called: its token and its base URL
    are both configured (`api_reads_configured`)."""
    try:
        config = GATEWAY_REGISTRY[gateway_name]['config'].get(GATEWAY_MODE) or {}
        return api_reads_configured(config)
    except (ValueError, KeyError):
        return False


def can_initiate_payments(gateway_name: str) -> bool:
    """Can this gateway produce instructions a customer can actually act on?

    Not the same question as `is_gateway_enabled`, which asks only about the API token.
    ShamCash has two shapes: HOSTED, which needs the token and an initiate path, and
    TRANSFER/reconcile, where the customer moves money to `merchant_id` by hand and we
    reconcile it later — no token involved. Keying initiation on the token alone would
    switch off a deployment that is working correctly.

    What must never happen is what a deploy missing every ShamCash variable did: answer
    200 and tell every customer to transfer 5,000 SYP to account `""`. Either credential
    makes the gateway usable; neither makes it unusable. The API counts only with its base
    URL as well (`api_reads_configured`): a token with no URL reads nothing.
    """
    try:
        config = GATEWAY_REGISTRY[gateway_name]['config'].get(GATEWAY_MODE) or {}
    except (ValueError, KeyError):
        return False
    return bool(api_reads_configured(config) or config.get('merchant_id'))


def gateway_kind(gateway_name: str) -> Optional[str]:
    """How this gateway's payments are confirmed, or None when it can take none.

    `automatic` — the API token and its base URL are set (`api_reads_configured`), so the
    server reads the merchant account itself (a hosted checkout, or the reconcile poller
    matching the transfer's note) and confirms the payment with no one involved.
    `reviewed` — the merchant account id, and no API the server may call: the customer
    transfers to it and nothing can read the account, so a person confirms the transfer
    in the console (`PAYMENT_REVIEW_HOURS`). The same two
    credentials `can_initiate_payments` accepts, so a gateway has a kind exactly when it
    can take a payment.
    """
    try:
        config = GATEWAY_REGISTRY[gateway_name]['config'].get(GATEWAY_MODE) or {}
    except (ValueError, KeyError):
        return None
    if api_reads_configured(config):
        return 'automatic'
    if config.get('merchant_id'):
        return 'reviewed'
    return None


def get_available_gateways() -> list:
    """List of enabled gateway names."""
    return [name for name in GATEWAY_REGISTRY.keys() if is_gateway_enabled(name)]


# ========================
# Webhook Configuration
# ========================
WEBHOOK_CONFIG = {
    'timeout': 10,       # seconds
    'max_retries': 3,
    'retry_delay': 5,    # seconds
    'log_payloads': DEBUG_MODE,
    'verify_signatures': True,
}

# ========================
# Security Configuration (CONFIRM header names against ShamCash's official access)
# ========================
SECURITY_CONFIG = {
    'webhook_signature_header': os.getenv('SHAMCASH_SIGNATURE_HEADER', 'X-ShamCash-Signature'),
    'webhook_timestamp_header': os.getenv('SHAMCASH_TIMESTAMP_HEADER', 'X-ShamCash-Timestamp'),
    'signature_expiry': 300,       # 5 minutes replay window
    'rate_limit_requests': 100,
    'rate_limit_window': 60,
}
