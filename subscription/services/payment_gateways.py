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

from celery.exceptions import SoftTimeLimitExceeded
from django.core.exceptions import ValidationError
from django.utils import timezone

from ..settings.gateway_config import (
    get_gateway_config, get_gateway_info, get_amount_limits, is_gateway_enabled,
    GATEWAY_MODE,
)

logger = logging.getLogger(__name__)


class PaymentGatewayError(Exception):
    """Base exception for payment gateway errors."""
    pass


class CurrencyNotChargeable(PaymentGatewayError):
    """This gateway is not configured to charge in this currency (no amount bounds).

    Its own class so a view can answer the app a `code` instead of the generic "request
    could not be completed": a buyer whose currency the gateway cannot take is not a
    gateway fault, and nothing they retry will change it (SIDE_BUGS S-052).
    """

    code = 'currency_not_chargeable'


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
        # Checked BEFORE the broad wrapper below, which turns every failure into a bare
        # PaymentGatewayError and would hide why this one was refused.
        self._require_chargeable_currency(currency)
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
        self._start_read()
        try:
            result = self.gateway.fetch_payment_status(reference, expected=expected)
        except SoftTimeLimitExceeded:
            # The calling task's time ran out; nothing is known about the gateway, so
            # nothing is recorded, and it is not wrapped: a `PaymentGatewayError` here
            # read to the poller as an unreachable gateway, and its run went on
            # (the wave-1 review, R-0).
            raise
        except Exception as e:
            logger.error(f"Failed to fetch payment status: {str(e)}")
            self._record_read(raised=True)
            raise PaymentGatewayError(f"Status check failed: {str(e)}")
        self._record_read()
        return result

    def list_transactions(self, start, end):
        """`(transactions, complete)` in a window (`BaseGateway.list_transactions`)."""
        self._start_read()
        try:
            listed = self.gateway.list_transactions(start, end)
        except SoftTimeLimitExceeded:
            raise                         # the task's time, not the gateway: see above
        except Exception as e:
            logger.error(f"Failed to list transactions: {str(e)}")
            self._record_read(raised=True)
            raise PaymentGatewayError(f"Transaction listing failed: {str(e)}")
        self._record_read()
        return listed

    def _start_read(self) -> None:
        if hasattr(self.gateway, 'last_read_error'):
            self.gateway.last_read_error, self.gateway.reads_answered = None, 0

    def _record_read(self, raised: bool = False) -> None:
        """Write what a read of the merchant account came to into the gateway's health
        (`subscription.gateway_health`): the state every payment screen shows as
        `gateway_state`. Only a gateway that reports its reads (`last_read_error`,
        `reads_answered`: the ShamCash adapter) is recorded, and only by what those say:
        a lookup that could not reach the gateway is a failure, one that answered a
        success. A call that raised for another reason (a fault of ours) says nothing
        about the gateway. Never decides anything and never raises."""
        from ..gateway_health import record_failure, record_success

        gateway = self.gateway
        if not hasattr(gateway, 'last_read_error'):
            return
        if gateway.last_read_error:
            record_failure(self.gateway_name, gateway.last_read_error)
        elif getattr(gateway, 'reads_answered', 0) and not raised:
            record_success(self.gateway_name)

    def transaction_facts(self, txn) -> Dict[str, Any]:
        return self.gateway.transaction_facts(txn)

    def refund(self, reference: str, amount=None) -> Dict[str, Any]:
        return self.gateway.refund(reference, amount)

    # ------------------------------------------------------------------ #
    def _require_chargeable_currency(self, currency: str) -> None:
        """Refuse, with `CurrencyNotChargeable`, a currency this gateway cannot charge in:
        one it does not support, or one it has no configured amount bounds for."""
        gateway_info = get_gateway_info(self.gateway_name)
        code = str(currency).upper()
        if (code not in gateway_info['supported_currencies']
                or not get_amount_limits(self.gateway_name, code)):
            raise CurrencyNotChargeable(
                f"{self.gateway_name} is not configured to charge in {code}")

    def _validate_payment_request(self, amount: Decimal, currency: str, user_data: Dict[str, Any]):
        gateway_info = get_gateway_info(self.gateway_name)
        currency = str(currency).upper()
        if currency not in gateway_info['supported_currencies']:
            raise ValidationError(f"Currency {currency} not supported by {self.gateway_name}")

        # Bounds are looked up FOR THIS CURRENCY. A single min/max documented in SYP
        # used to be applied to every currency, so a plan priced 10.00 was rejected as
        # "below minimum 100" against a Syrian-pound floor. No bounds means the gateway
        # is not configured to charge in this currency, which is a refusal, not a pass.
        limits = get_amount_limits(self.gateway_name, currency)
        if not limits:
            raise ValidationError(
                f"{self.gateway_name} has no configured amount limits for {currency}"
            )
        if amount < limits['min']:
            raise ValidationError(f"Amount {amount} {currency} is below minimum {limits['min']}")
        if amount > limits['max']:
            raise ValidationError(f"Amount {amount} {currency} exceeds maximum {limits['max']}")
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
    def _wire_number(value: Decimal):
        """Render a Decimal as the same JSON type the old int constants produced.

        `min_amount` and `max_amount` were plain ints on the wire. A Flutter client
        reading them into an `int` would throw on `100.0`, so an integral bound must
        stay an integer even though it is now stored as a Decimal.
        """
        return int(value) if value == value.to_integral_value() else float(value)

    @staticmethod
    def get_available_gateways() -> Dict[str, Dict[str, Any]]:
        from ..settings.gateway_config import (
            get_available_gateways, get_gateway_config, get_gateway_info,
        )
        available = {}
        for gateway_name in get_available_gateways():
            info = get_gateway_info(gateway_name)
            limits = info.get('amount_limits') or {}
            # The legacy flat pair describes the gateway's own settlement currency,
            # which is what those numbers always meant. `amount_limits` carries the
            # full per-currency truth for clients that can read it.
            settlement = str(get_gateway_config(gateway_name).get('currency', '')).upper()
            settlement_limits = limits.get(settlement) or {}
            entry = {
                'name': info['name'],
                'supported_currencies': info['supported_currencies'],
                'settlement_currency': settlement,
                'amount_limits': {
                    cur: {
                        'min': PaymentGatewayManager._wire_number(b['min']),
                        'max': PaymentGatewayManager._wire_number(b['max']),
                    }
                    for cur, b in limits.items()
                },
                'enabled': True,
            }
            if settlement_limits:
                entry['min_amount'] = PaymentGatewayManager._wire_number(settlement_limits['min'])
                entry['max_amount'] = PaymentGatewayManager._wire_number(settlement_limits['max'])
            available[gateway_name] = entry
        return available

    @staticmethod
    def get_gateway_service(gateway_name: str) -> 'PaymentGatewayService':
        if not is_gateway_enabled(gateway_name):
            raise PaymentGatewayError(f"Gateway {gateway_name} is not enabled")
        return PaymentGatewayService(gateway_name)

    @staticmethod
    def get_service_for_initiation(gateway_name: str) -> 'PaymentGatewayService':
        """A gateway that can actually give the customer something to act on.

        `get_gateway_service` asks only whether the API token is set, which is the wrong
        question for a transfer/reconcile deployment where the customer pays into
        `merchant_id` by hand. `can_initiate_payments` accepts either credential — and
        refuses when there is neither, which is the deploy that told every customer to
        transfer 5,000 SYP to account "".
        """
        from subscription.settings.gateway_config import can_initiate_payments

        if not can_initiate_payments(gateway_name):
            raise PaymentGatewayError(
                f"Gateway {gateway_name} is not configured to take payments")
        return PaymentGatewayService(gateway_name)
