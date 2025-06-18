"""
Payment Gateway Configuration
============================

This module contains all configuration settings for payment gateways.
Supports both sandbox and production environments.
"""

import os
from typing import Dict, Any

# ========================
# Environment Configuration
# ========================
GATEWAY_MODE = os.getenv('GATEWAY_MODE', 'sandbox')  # 'sandbox' or 'production'
DEBUG_MODE = os.getenv('PAYMENT_DEBUG', 'True').lower() == 'true'

# ========================
# Gateway Base Configuration
# ========================
GATEWAY_TIMEOUT = 30  # seconds
GATEWAY_RETRY_ATTEMPTS = 3
GATEWAY_RETRY_DELAY = 2  # seconds

# ========================
# Syriatel Cash Configuration
# ========================
SYRIATEL_CONFIG = {
    'sandbox': {
        'api_key': os.getenv('SYRIATEL_SANDBOX_API_KEY', 'test_syriatel_key_123'),
        'api_secret': os.getenv('SYRIATEL_SANDBOX_API_SECRET', 'test_syriatel_secret_123'),
        'api_url': 'https://sandbox.syriatel.sy/api/v1',
        'webhook_secret': os.getenv('SYRIATEL_SANDBOX_WEBHOOK_SECRET', 'test_webhook_secret'),
        'merchant_id': os.getenv('SYRIATEL_SANDBOX_MERCHANT_ID', 'TEST_MERCHANT_001'),
        'currency': 'SYP',
        'timeout': GATEWAY_TIMEOUT,
        'retry_attempts': GATEWAY_RETRY_ATTEMPTS,
    },
    'production': {
        'api_key': os.getenv('SYRIATEL_PRODUCTION_API_KEY', ''),
        'api_secret': os.getenv('SYRIATEL_PRODUCTION_API_SECRET', ''),
        'api_url': 'https://api.syriatel.sy/v1',
        'webhook_secret': os.getenv('SYRIATEL_PRODUCTION_WEBHOOK_SECRET', ''),
        'merchant_id': os.getenv('SYRIATEL_PRODUCTION_MERCHANT_ID', ''),
        'currency': 'SYP',
        'timeout': GATEWAY_TIMEOUT,
        'retry_attempts': GATEWAY_RETRY_ATTEMPTS,
    }
}

# ========================
# Al-Baraka Bank Configuration
# ========================
BARAKA_CONFIG = {
    'sandbox': {
        'api_key': os.getenv('BARAKA_SANDBOX_API_KEY', 'test_baraka_key_123'),
        'api_secret': os.getenv('BARAKA_SANDBOX_API_SECRET', 'test_baraka_secret_123'),
        'api_url': 'https://sandbox.baraka.sy/api/v1',
        'webhook_secret': os.getenv('BARAKA_SANDBOX_WEBHOOK_SECRET', 'test_webhook_secret'),
        'merchant_id': os.getenv('BARAKA_SANDBOX_MERCHANT_ID', 'TEST_MERCHANT_002'),
        'currency': 'SYP',
        'timeout': GATEWAY_TIMEOUT,
        'retry_attempts': GATEWAY_RETRY_ATTEMPTS,
    },
    'production': {
        'api_key': os.getenv('BARAKA_PRODUCTION_API_KEY', ''),
        'api_secret': os.getenv('BARAKA_PRODUCTION_API_SECRET', ''),
        'api_url': 'https://api.baraka.sy/v1',
        'webhook_secret': os.getenv('BARAKA_PRODUCTION_WEBHOOK_SECRET', ''),
        'merchant_id': os.getenv('BARAKA_PRODUCTION_MERCHANT_ID', ''),
        'currency': 'SYP',
        'timeout': GATEWAY_TIMEOUT,
        'retry_attempts': GATEWAY_RETRY_ATTEMPTS,
    }
}

# ========================
# BEMO Bank Configuration
# ========================
BEMO_CONFIG = {
    'sandbox': {
        'api_key': os.getenv('BEMO_SANDBOX_API_KEY', 'test_bemo_key_123'),
        'api_secret': os.getenv('BEMO_SANDBOX_API_SECRET', 'test_bemo_secret_123'),
        'api_url': 'https://sandbox.bemo.sy/api/v1',
        'webhook_secret': os.getenv('BEMO_SANDBOX_WEBHOOK_SECRET', 'test_webhook_secret'),
        'merchant_id': os.getenv('BEMO_SANDBOX_MERCHANT_ID', 'TEST_MERCHANT_003'),
        'currency': 'SYP',
        'timeout': GATEWAY_TIMEOUT,
        'retry_attempts': GATEWAY_RETRY_ATTEMPTS,
    },
    'production': {
        'api_key': os.getenv('BEMO_PRODUCTION_API_KEY', ''),
        'api_secret': os.getenv('BEMO_PRODUCTION_API_SECRET', ''),
        'api_url': 'https://api.bemo.sy/v1',
        'webhook_secret': os.getenv('BEMO_PRODUCTION_WEBHOOK_SECRET', ''),
        'merchant_id': os.getenv('BEMO_PRODUCTION_MERCHANT_ID', ''),
        'currency': 'SYP',
        'timeout': GATEWAY_TIMEOUT,
        'retry_attempts': GATEWAY_RETRY_ATTEMPTS,
    }
}

# ========================
# Gateway Registry
# ========================
GATEWAY_REGISTRY = {
    'syriatel_cash': {
        'name': 'Syriatel Cash',
        'config': SYRIATEL_CONFIG,
        'class_name': 'SyriatelCashGateway',
        'supported_currencies': ['SYP', 'USD'],
        'min_amount': 100,  # SYP
        'max_amount': 1000000,  # SYP
    },
    'baraka_bank': {
        'name': 'Al-Baraka Bank',
        'config': BARAKA_CONFIG,
        'class_name': 'BarakaBankGateway',
        'supported_currencies': ['SYP', 'USD'],
        'min_amount': 100,  # SYP
        'max_amount': 5000000,  # SYP
    },
    'bemo_bank': {
        'name': 'BEMO Bank',
        'config': BEMO_CONFIG,
        'class_name': 'BemoBankGateway',
        'supported_currencies': ['SYP', 'USD'],
        'min_amount': 100,  # SYP
        'max_amount': 3000000,  # SYP
    }
}

# ========================
# Utility Functions
# ========================
def get_gateway_config(gateway_name: str) -> Dict[str, Any]:
    """
    Get configuration for a specific gateway based on current environment.
    
    Args:
        gateway_name: Name of the gateway (e.g., 'syriatel_cash')
    
    Returns:
        Dictionary containing gateway configuration
    """
    if gateway_name not in GATEWAY_REGISTRY:
        raise ValueError(f"Unknown gateway: {gateway_name}")
    
    gateway_info = GATEWAY_REGISTRY[gateway_name]
    config = gateway_info['config'].get(GATEWAY_MODE)
    
    if not config:
        raise ValueError(f"No configuration found for {gateway_name} in {GATEWAY_MODE} mode")
    
    return config

def get_gateway_info(gateway_name: str) -> Dict[str, Any]:
    """
    Get gateway information including supported features.
    
    Args:
        gateway_name: Name of the gateway
    
    Returns:
        Dictionary containing gateway information
    """
    if gateway_name not in GATEWAY_REGISTRY:
        raise ValueError(f"Unknown gateway: {gateway_name}")
    
    return GATEWAY_REGISTRY[gateway_name]

def is_gateway_enabled(gateway_name: str) -> bool:
    """
    Check if a gateway is enabled and configured.
    
    Args:
        gateway_name: Name of the gateway
    
    Returns:
        True if gateway is enabled, False otherwise
    """
    try:
        config = get_gateway_config(gateway_name)
        return bool(config.get('api_key') and config.get('api_secret'))
    except (ValueError, KeyError):
        return False

def get_available_gateways() -> list:
    """
    Get list of available and enabled gateways.
    
    Returns:
        List of enabled gateway names
    """
    return [name for name in GATEWAY_REGISTRY.keys() if is_gateway_enabled(name)]

# ========================
# Webhook Configuration
# ========================
WEBHOOK_CONFIG = {
    'timeout': 10,  # seconds
    'max_retries': 3,
    'retry_delay': 5,  # seconds
    'log_payloads': DEBUG_MODE,
    'verify_signatures': True,
}

# ========================
# Security Configuration
# ========================
SECURITY_CONFIG = {
    'webhook_signature_header': 'X-Gateway-Signature',
    'webhook_timestamp_header': 'X-Gateway-Timestamp',
    'signature_expiry': 300,  # 5 minutes
    'rate_limit_requests': 100,  # requests per minute
    'rate_limit_window': 60,  # seconds
} 