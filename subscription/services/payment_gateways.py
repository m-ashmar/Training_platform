"""
Payment Gateway Service
=======================

Thin orchestrator over the active payment gateway. It selects and instantiates
the concrete gateway (via the registry) and exposes a stable interface to the
views: initiate, fetch/verify status, verify webhook, refund. All state changes
(completing a payment, activating a subscription) live in PaymentService, NOT
here and NOT in the views.
"""

import logging
import time
from typing import Dict, Any, Optional, Tuple
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.utils import timezone

from ..settings.gateway_config import (
    get_gateway_config, get_gateway_info, is_gateway_enabled,
    GATEWAY_MODE,
)

logger = logging.getLogger(__name__)


class PaymentGatewayError(Exception):
    """Base exception for payment gateway errors."""
    pass


class PaymentGatewayService:
    """Unified interface for handling the active payment gateway."""

    def __init__(self, gateway_name: str):
        self.gateway_name = gateway_name
        self.config = self._load_config()
        self.gateway = self._initialize_gateway()
        logger.info(f"Initialized {gateway_name} gateway in {GATEWAY_MODE} mode")

    def _load_config(self) -> Dict[str, Any]:
        try:
            return get_gateway_config(self.gateway_name)
        except ValueError as e:
            raise PaymentGatewayError(f"Configuration error: {str(e)}")

    def _initialize_gateway(self):
        try:
            module_base = self.gateway_name.split('_')[0]
            module_name = f"subscription.gateways.{module_base}"
            class_name = get_gateway_info(self.gateway_name)['class_name']
            module = __import__(module_name, fromlist=[class_name])
            gateway_class = getattr(module, class_name)
            return gateway_class(self.config)
        except (ImportError, AttributeError) as e:
            logger.error(f"Failed to initialize gateway {self.gateway_name}: {str(e)}")
            raise PaymentGatewayError(f"Gateway initialization failed: {str(e)}")

    # ------------------------------------------------------------------ #
    def initiate_payment(
        self,
        amount: Decimal,
        currency: str,
        user_data: Dict[str, Any],
        reference: str,
        metadata: Optional[Dict[str, Any]] = None,
        callback_url: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Initiate a payment. Caller supplies the reference (tied to the Payment row)."""
        try:
            self._validate_payment_request(amount, currency, user_data)
            payment_data = {
                'amount': float(amount),
                'currency': currency,
                'merchant_id': self.config.get('merchant_id'),
                'user_data': user_data,
                'metadata': metadata or {},
                'timestamp': int(time.time()),
                'reference': reference,
                'callback_url': callback_url,
                'description': (metadata or {}).get('description', 'Subscription payment'),
            }
            response = self.gateway.initiate_payment(payment_data)
            logger.info(f"Payment initiated via {self.gateway_name}, reference {reference}")
            return {
                'success': True,
                'gateway': self.gateway_name,
                'reference': reference,
                'response': response,
                'timestamp': timezone.now(),
            }
        except Exception as e:
            logger.error(f"Payment initiation failed: {str(e)}")
            raise PaymentGatewayError(f"Payment initiation failed: {str(e)}")

    def verify_webhook(self, payload: bytes, headers: Dict[str, str]) -> Tuple[bool, Dict[str, Any]]:
        """Delegate signature + freshness verification to the gateway."""
        try:
            is_valid, data = self.gateway.verify_webhook(payload, headers)
            if not is_valid:
                return False, {}
            if not self._validate_webhook_data(data):
                logger.warning(f"Webhook data incomplete for {self.gateway_name}")
                return False, {}
            return True, data
        except Exception as e:
            logger.error(f"Webhook verification failed: {str(e)}")
            raise PaymentGatewayError(f"Webhook verification failed: {str(e)}")

    def fetch_payment_status(self, reference: str, expected: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Authoritatively look up + verify a payment at the provider."""
        try:
            return self.gateway.fetch_payment_status(reference, expected=expected)
        except Exception as e:
            logger.error(f"Failed to fetch payment status: {str(e)}")
            raise PaymentGatewayError(f"Status check failed: {str(e)}")

    def refund(self, reference: str, amount=None) -> Dict[str, Any]:
        return self.gateway.refund(reference, amount)

    # ------------------------------------------------------------------ #
    def _validate_payment_request(self, amount: Decimal, currency: str, user_data: Dict[str, Any]):
        gateway_info = get_gateway_info(self.gateway_name)
        min_amount = Decimal(str(gateway_info['min_amount']))
        max_amount = Decimal(str(gateway_info['max_amount']))
        if amount < min_amount:
            raise ValidationError(f"Amount {amount} is below minimum {min_amount}")
        if amount > max_amount:
            raise ValidationError(f"Amount {amount} exceeds maximum {max_amount}")
        if currency not in gateway_info['supported_currencies']:
            raise ValidationError(f"Currency {currency} not supported by {self.gateway_name}")
        for field in ('email', 'phone'):
            if not user_data.get(field):
                raise ValidationError(f"Missing required field: {field}")

    @staticmethod
    def _validate_webhook_data(payment_data: Dict[str, Any]) -> bool:
        return all(payment_data.get(f) is not None for f in ('reference', 'status', 'amount'))

    def _generate_reference(self) -> str:
        import uuid
        return f"{self.gateway_name.upper()}_{int(time.time()*1000)}_{uuid.uuid4().hex[:8]}"


class PaymentGatewayManager:
    """High-level helpers across enabled gateways."""

    @staticmethod
    def get_available_gateways() -> Dict[str, Dict[str, Any]]:
        from ..settings.gateway_config import get_available_gateways, get_gateway_info
        available = {}
        for gateway_name in get_available_gateways():
            info = get_gateway_info(gateway_name)
            available[gateway_name] = {
                'name': info['name'],
                'supported_currencies': info['supported_currencies'],
                'min_amount': info['min_amount'],
                'max_amount': info['max_amount'],
                'enabled': True,
            }
        return available

    @staticmethod
    def get_gateway_service(gateway_name: str) -> 'PaymentGatewayService':
        if not is_gateway_enabled(gateway_name):
            raise PaymentGatewayError(f"Gateway {gateway_name} is not enabled")
        return PaymentGatewayService(gateway_name)
