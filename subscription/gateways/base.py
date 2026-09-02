"""
Payment gateway interface.
=========================

Every gateway implements this contract; views/services depend on the interface,
never on a concrete gateway. Verification returns a normalized dict consumed by
PaymentService.complete_payment:

    {
      'status':        'completed' | 'pending' | 'failed',
      'amount':        <number>,
      'currency':      <str>,
      'reference':     <our payment reference>,     # optional
      'transaction_id':<gateway txn id>,            # optional
      'event_id':      <unique event id>,           # optional (webhook idempotency)
      'gateway_data':  <raw provider payload>,      # optional
    }
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Dict, Optional, Tuple


class PaymentGateway(ABC):
    """Abstract base every payment gateway must implement."""

    def __init__(self, config: Dict[str, Any]):
        self.config = config

    @abstractmethod
    def initiate_payment(self, payment_data: Dict[str, Any]) -> Dict[str, Any]:
        """Create a payment at the provider. Returns at least {'reference', 'status'}
        and optionally {'payment_url', 'instructions', 'transaction_id'}."""
        raise NotImplementedError

    @abstractmethod
    def fetch_payment_status(self, reference: str, expected: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Authoritatively look up a payment at the provider and return a normalized,
        VERIFIED dict (see module docstring). Must not report 'completed' unless the
        provider transaction matches the expected amount/currency/reference."""
        raise NotImplementedError

    @abstractmethod
    def verify_webhook(self, payload: bytes, headers: Dict[str, str]) -> Tuple[bool, Dict[str, Any]]:
        """Verify a webhook's signature + freshness and return (is_valid, normalized_data)."""
        raise NotImplementedError

    def refund(self, reference: str, amount=None) -> Dict[str, Any]:
        """Optional. Gateways that don't support refunds leave this unimplemented."""
        raise NotImplementedError("Refunds are not supported by this gateway")
