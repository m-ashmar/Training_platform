"""
Payment Gateway Configuration
============================

Single active gateway: ShamCash. Secrets are loaded from the environment with
NO usable hardcoded fallbacks; production readiness is enforced at startup by
`enforce_production_safety()` in settings_production.py.
"""

import os
from typing import Dict, Any

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
        'api_url': os.getenv('SHAMCASH_SANDBOX_API_URL', 'https://api.shamcash-api.com/v1'),
        'webhook_secret': os.getenv('SHAMCASH_SANDBOX_WEBHOOK_SECRET', ''),
        'merchant_id': os.getenv('SHAMCASH_SANDBOX_ACCOUNT_ID', ''),
        'initiate_path': os.getenv('SHAMCASH_SANDBOX_INITIATE_PATH', ''),  # set post-approval for hosted mode
        'currency': 'SYP',
        'timeout': GATEWAY_TIMEOUT,
        'retry_attempts': GATEWAY_RETRY_ATTEMPTS,
    },
    'production': {
        'api_key': os.getenv('SHAMCASH_API_TOKEN', ''),
        'api_url': os.getenv('SHAMCASH_API_URL', 'https://api.shamcash-api.com/v1'),
        'webhook_secret': os.getenv('SHAMCASH_WEBHOOK_SECRET', ''),
        'merchant_id': os.getenv('SHAMCASH_ACCOUNT_ID', ''),
        'initiate_path': os.getenv('SHAMCASH_INITIATE_PATH', ''),
        'currency': 'SYP',
        'timeout': GATEWAY_TIMEOUT,
        'retry_attempts': GATEWAY_RETRY_ATTEMPTS,
    },
}

# ========================
# Gateway Registry
# ========================
GATEWAY_REGISTRY = {
    'shamcash': {
        'name': 'ShamCash',
        'config': SHAMCASH_CONFIG,
        'class_name': 'ShamCashGateway',
        'supported_currencies': ['SYP', 'USD'],
        'min_amount': 100,      # SYP
        'max_amount': 5000000,  # SYP
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


def is_gateway_enabled(gateway_name: str) -> bool:
    """A gateway is enabled only if its API token is configured."""
    try:
        config = GATEWAY_REGISTRY[gateway_name]['config'].get(GATEWAY_MODE) or {}
        return bool(config.get('api_key'))
    except (ValueError, KeyError):
        return False


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
# Security Configuration (CONFIRM header names with ShamCash at onboarding)
# ========================
SECURITY_CONFIG = {
    'webhook_signature_header': os.getenv('SHAMCASH_SIGNATURE_HEADER', 'X-ShamCash-Signature'),
    'webhook_timestamp_header': os.getenv('SHAMCASH_TIMESTAMP_HEADER', 'X-ShamCash-Timestamp'),
    'signature_expiry': 300,       # 5 minutes replay window
    'rate_limit_requests': 100,
    'rate_limit_window': 60,
}
