"""
ShamCash Payment Gateway
========================

ShamCash (https://shamcash-api.com) — Syrian online payment provider.
Base API: Bearer-token auth, JSON envelope ``{status, code, message, data}``.

Integration model
------------------
ShamCash's public API documents account/balance/**transaction** retrieval
(``GET /transactions``). Payment *initiation* and *webhook* specifics are only
provided after merchant approval, so this gateway is built to work in two modes,
selected by config, without changing any calling code:

* **reconcile mode** (default): we create a pending payment carrying a unique
  reference; the user pays the merchant ShamCash account including that
  reference; we then VERIFY by querying ``GET /transactions`` and matching
  amount + currency + reference within a recent window.
* **hosted mode** (when ``initiate_path`` is configured post-approval): we POST
  to ShamCash's initiation endpoint and return its hosted ``payment_url``.

Webhooks are only honored when ``webhook_secret`` is configured; otherwise
``verify_webhook`` returns invalid (fail-closed) and completion happens via the
verified reconcile path. Confirm the exact header/field names with ShamCash at
onboarding — they are all config-driven below (marked CONFIRM).
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
from decimal import Decimal
from typing import Any, Dict, Optional, Tuple

from django.utils import timezone

from .base import PaymentGateway

logger = logging.getLogger(__name__)


class ShamCashGateway(PaymentGateway):
    """ShamCash gateway implementing the common PaymentGateway contract."""

    def __init__(self, config: Dict[str, Any]):
        super().__init__(config)
        self.base_url = str(config.get('api_url', 'https://api.shamcash-api.com/v1')).rstrip('/')
        self.api_token = config.get('api_key') or config.get('api_token', '')
        self.account_id = config.get('merchant_id', '')
        self.webhook_secret = config.get('webhook_secret', '')
        self.initiate_path = config.get('initiate_path', '')  # set post-approval for hosted mode
        self.timeout = int(config.get('timeout', 30))
        # CONFIRM these header names with ShamCash at onboarding:
        self.sig_header = config.get('webhook_signature_header', 'X-ShamCash-Signature')
        self.ts_header = config.get('webhook_timestamp_header', 'X-ShamCash-Timestamp')
        self.signature_expiry = int(config.get('signature_expiry', 300))

    # ------------------------------------------------------------------ #
    def _headers(self) -> Dict[str, str]:
        return {
            'Authorization': f'Bearer {self.api_token}',
            'Content-Type': 'application/json',
            'Accept': 'application/json',
        }

    def _get(self, path: str, params: Dict[str, Any]) -> Dict[str, Any]:
        import requests
        url = f"{self.base_url}{path}"
        resp = requests.get(url, headers=self._headers(), params=params, timeout=self.timeout)
        resp.raise_for_status()
        return resp.json()

    def _post(self, path: str, data: Dict[str, Any]) -> Dict[str, Any]:
        import requests
        url = f"{self.base_url}{path}"
        resp = requests.post(url, headers=self._headers(), json=data, timeout=self.timeout)
        resp.raise_for_status()
        return resp.json()

    # ------------------------------------------------------------------ #
    def initiate_payment(self, payment_data: Dict[str, Any]) -> Dict[str, Any]:
        reference = payment_data['reference']
        amount = payment_data['amount']
        currency = payment_data['currency']

        # Hosted mode — only when ShamCash gives us an initiation endpoint.
        if self.initiate_path:
            try:
                body = {
                    'account_id': self.account_id,
                    'amount': float(amount),
                    'currency': currency,
                    'reference': reference,
                    'callback_url': payment_data.get('callback_url'),
                    'description': payment_data.get('description', 'Subscription payment'),
                }
                envelope = self._post(self.initiate_path, body)
                data = envelope.get('data', {}) if isinstance(envelope, dict) else {}
                return {
                    'success': True,
                    'reference': reference,
                    'status': 'pending',
                    'payment_url': data.get('payment_url'),       # CONFIRM field name
                    'transaction_id': data.get('transaction_id'),  # CONFIRM field name
                    'gateway_response': envelope,
                }
            except Exception as e:
                logger.error(f"ShamCash initiate (hosted) failed: {e}")
                raise

        # Reconcile mode — instruct the user to pay the merchant account with the reference.
        return {
            'success': True,
            'reference': reference,
            'status': 'pending',
            'payment_url': None,
            'instructions': {
                'account_id': self.account_id,
                'amount': str(amount),
                'currency': currency,
                'note': reference,  # user must include this reference in the transfer note
            },
            'gateway_response': {'mode': 'reconcile'},
        }

    # ------------------------------------------------------------------ #
    def fetch_payment_status(self, reference: str, expected: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """
        Verify by locating a matching transaction on the merchant account.
        Returns 'completed' only if a transaction matches amount + currency +
        reference within the lookback window. Never trusts a bare status flag.
        """
        expected = expected or {}
        lookback_hours = int(self.config.get('reconcile_lookback_hours', 72))
        now = timezone.now()
        params = {
            'account_id': self.account_id,
            'start_at': (now - timezone.timedelta(hours=lookback_hours)).isoformat(),
            'end_at': now.isoformat(),
            'limit': 200,
        }
        try:
            envelope = self._get('/transactions', params)
        except Exception as e:
            logger.warning(f"ShamCash status lookup failed for {reference}: {e}")
            return {'status': 'pending', 'reference': reference}

        txns = (envelope.get('data') or []) if isinstance(envelope, dict) else []
        exp_amount = None
        if expected.get('amount') is not None:
            exp_amount = Decimal(str(expected['amount'])).quantize(Decimal('0.01'))
        exp_currency = str(expected.get('currency', '')).upper() or None

        for txn in txns:
            # CONFIRM these field names against a real ShamCash transaction object.
            note = str(txn.get('note') or txn.get('description') or '')
            if reference not in note:
                continue
            txn_amount = txn.get('amount')
            txn_currency = str(txn.get('currency', '')).upper()
            if exp_amount is not None:
                try:
                    if Decimal(str(txn_amount)).quantize(Decimal('0.01')) != exp_amount:
                        continue
                except Exception:
                    continue
            if exp_currency and txn_currency and txn_currency != exp_currency:
                continue
            return {
                'status': 'completed',
                'reference': reference,
                'amount': txn_amount,
                'currency': txn_currency or exp_currency,
                'transaction_id': str(txn.get('id') or txn.get('transaction_id') or ''),
                'event_id': str(txn.get('id') or txn.get('transaction_id') or ''),
                'occurred_at': txn.get('occurred_at'),
                'gateway_data': txn,
            }

        return {'status': 'pending', 'reference': reference}

    # ------------------------------------------------------------------ #
    def verify_webhook(self, payload: bytes, headers: Dict[str, str]) -> Tuple[bool, Dict[str, Any]]:
        # Fail-closed until ShamCash provides (and we configure) a webhook secret.
        if not self.webhook_secret:
            logger.warning("ShamCash webhook received but no webhook_secret configured — rejecting")
            return False, {}

        signature = self.header(headers, self.sig_header)
        timestamp = self.header(headers, self.ts_header)
        if not signature or not timestamp:
            logger.warning("ShamCash webhook missing signature/timestamp header")
            return False, {}

        # Replay/freshness window.
        try:
            if abs(int(time.time()) - int(timestamp)) > self.signature_expiry:
                logger.warning("ShamCash webhook timestamp outside freshness window")
                return False, {}
        except (ValueError, TypeError):
            return False, {}

        # Signature is over "<timestamp>.<raw_body>" (CONFIRM scheme with ShamCash).
        signed = f"{timestamp}.".encode('utf-8') + payload
        expected_sig = hmac.new(self.webhook_secret.encode('utf-8'), signed, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected_sig):
            logger.warning("ShamCash webhook signature mismatch")
            return False, {}

        try:
            body = json.loads(payload.decode('utf-8'))
        except Exception:
            return False, {}
        data = body.get('data', body) if isinstance(body, dict) else {}
        return True, {
            'status': str(data.get('status', '')).lower(),
            'amount': data.get('amount'),
            'currency': data.get('currency'),
            'reference': data.get('reference'),
            'transaction_id': str(data.get('transaction_id') or data.get('id') or ''),
            'event_id': str(data.get('event_id') or data.get('id') or ''),
            'gateway_data': body,
        }
