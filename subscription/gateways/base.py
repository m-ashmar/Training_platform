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

    @staticmethod
    def header(headers: Dict[str, str], name: str) -> Optional[str]:
        """Case-insensitive header lookup.

        HTTP header names are case-insensitive, but a plain dict is not. Gateways used
        to read `headers.get('X-ShamCash-Signature') or headers.get(<lowercased>)`,
        which could never match: the view builds the mapping with
        `dict(request.headers)`, and Django title-cases every segment, so the key is
        always `X-Shamcash-Signature` with a lowercase 'c'. No spelling a client could
        send would help, because WSGI/ASGI folds them all to the same normalized form.
        Every webhook was therefore rejected with "missing signature/timestamp header"
        and no payment could ever be confirmed.

        Doing the fold here rather than at the call site means a gateway stays correct
        whatever kind of mapping it is handed.
        """
        if not headers:
            return None
        target = name.lower()
        for key, value in headers.items():
            if str(key).lower() == target:
                return value
        return None

    def refund(self, reference: str, amount=None) -> Dict[str, Any]:
        """Optional. Gateways that don't support refunds leave this unimplemented."""
        raise NotImplementedError("Refunds are not supported by this gateway")
