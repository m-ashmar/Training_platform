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
    #: Transactions asked for per lookup. A page that comes back this full may have
    #: more behind it (SIDE_BUGS S-129).
    PAGE_LIMIT = 200
    #: Lookups one status check may spend splitting a full window before it gives up and
    #: answers 'unknown'. Bounds the traffic a single checkout can cause.
    MAX_LOOKUPS = 16
    #: Allowance for the gateway's clock against ours when the window starts at the
    #: moment the checkout opened.
    CLOCK_SLACK = timezone.timedelta(minutes=10)

    def fetch_payment_status(self, reference: str, expected: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """
        Verify by locating a matching transaction on the merchant account.
        Returns 'completed' only if a transaction matches amount + currency +
        reference within the lookback window. Never trusts a bare status flag.

        Three answers, and the third one matters: 'completed' (a matching transaction
        is on the account), 'pending' (the account was read IN FULL and nothing matches
        — so nobody has paid) and 'unknown' (the account could not be read, or could not
        be read in full, which is evidence of nothing).

        THE WHOLE WINDOW, NOT ONE PAGE (SIDE_BUGS S-129). This read one page of at most
        200 transactions from the last 72 h and answered 'pending' when the reference was
        not on it — and the poller expires a checkout on 'pending'. On an account busy
        enough to fill a page, a paid checkout whose transfer fell outside it was
        expired. Now:
          * the window starts when the checkout opened (`expected['since']`, less
            `CLOCK_SLACK`) when the caller knows it, capped at the lookback — a transfer
            cannot quote a reference before it existed;
          * a page that comes back full, or that says it has more, is split in two by
            time and each half read, up to `MAX_LOOKUPS`. Only the documented
            `start_at`/`end_at`/`limit` parameters are used: the pagination scheme is one
            of the things still to CONFIRM with ShamCash;
          * if the window still cannot be read in full, the answer is 'unknown', which
            closes nothing.
        """
        expected = expected or {}
        lookback_hours = int(self.config.get('reconcile_lookback_hours', 72))
        now = timezone.now()
        start = now - timezone.timedelta(hours=lookback_hours)
        since = expected.get('since')
        if since is not None:
            start = max(start, since - self.CLOCK_SLACK)
        if start >= now:
            start = now - self.CLOCK_SLACK

        exp_amount = None
        if expected.get('amount') is not None:
            exp_amount = Decimal(str(expected['amount'])).quantize(Decimal('0.01'))
        exp_currency = str(expected.get('currency', '')).upper() or None

        partial = []

        def visit(txns):
            match = self._match(txns, reference, exp_amount, exp_currency)
            if match is None and not partial:
                near = self._near_miss(txns, reference, exp_amount, exp_currency)
                if near is not None:
                    partial.append(near)
            return match

        found, _txns, truncated, error = self._read_window(start, now, visit=visit,
                                                           reference=reference)
        if found is not None:
            return found
        if error is not None:
            # 'unknown', not 'pending': a lookup that FAILED is not evidence that
            # nobody paid. Answering 'pending' made an outage indistinguishable from
            # an unpaid checkout, and the poller closes unpaid checkouts — so a few
            # minutes of gateway trouble would have expired every checkout in flight.
            return {'status': 'unknown', 'reference': reference, 'error': error}
        if partial:
            # The reference, with another amount or currency: money for THIS checkout,
            # not the money it asked for. Held for an operator (B-051), never dropped
            # and never completed as it is — even when the window was not read in full,
            # because an exact transfer found later still completes the checkout.
            return partial[0]
        if truncated:
            logger.warning("ShamCash: the account holds more transactions than %s lookups "
                           "can read for %s; answering unknown", self.MAX_LOOKUPS, reference)
            return {'status': 'unknown', 'reference': reference,
                    'error': 'the transaction window could not be read in full'}
        return {'status': 'pending', 'reference': reference}

    def list_transactions(self, start, end):
        """Every transaction on the merchant account between `start` and `end`, for the
        sweep that finds transfers no checkout claimed (`subscription.tasks.
        sweep_unmatched_transfers`). Returns `(transactions, complete)`: `complete` is
        False when the window could not be read in full or a lookup failed, and the
        caller must then treat what it did not see as unseen, never as absent.
        """
        _found, txns, truncated, error = self._read_window(start, end)
        return txns, not (truncated or error)

    def _read_window(self, start, end, *, visit=None, reference=''):
        """Read the account from `start` to `end`, splitting any page that comes back
        full, up to `MAX_LOOKUPS` lookups. `visit(page)` may return a result, which
        ends the read. Returns `(found, transactions, truncated, error)`."""
        windows = [(start, end)]
        seen = []
        lookups = 0
        truncated = False
        while windows:
            if lookups >= self.MAX_LOOKUPS:
                truncated = True
                break
            low, high = windows.pop()
            lookups += 1
            params = {
                'account_id': self.account_id,
                'start_at': low.isoformat(),
                'end_at': high.isoformat(),
                'limit': self.PAGE_LIMIT,
            }
            try:
                envelope = self._get('/transactions', params)
            except Exception as e:
                logger.warning(f"ShamCash status lookup failed for {reference or 'the sweep'}: {e}")
                return None, seen, truncated, str(e)

            txns = (envelope.get('data') or []) if isinstance(envelope, dict) else []
            txns = [t for t in txns if isinstance(t, dict)]
            if visit is not None:
                found = visit(txns)
                if found is not None:
                    return found, seen, truncated, None
            if len(txns) >= self.PAGE_LIMIT or self._says_there_is_more(envelope):
                if high - low <= timezone.timedelta(seconds=1):
                    truncated = True       # a second's worth of transfers fills a page
                    seen.extend(txns)
                    continue
                middle = low + (high - low) / 2
                windows.append((middle, high))
                windows.append((low, middle))
                continue
            seen.extend(txns)
        return None, seen, truncated, None

    @staticmethod
    def transaction_facts(txn) -> Dict[str, Any]:
        """The fields the review queue keeps, read off one transaction (CONFIRM the
        names against a real ShamCash transaction object)."""
        return {
            'transaction_id': str(txn.get('id') or txn.get('transaction_id') or ''),
            'amount': txn.get('amount'),
            'currency': str(txn.get('currency', '')).upper(),
            'note': str(txn.get('note') or txn.get('description') or '')[:500],
            'sender': str(txn.get('sender') or txn.get('from') or txn.get('sender_account')
                          or '')[:255],
            'occurred_at': txn.get('occurred_at'),
        }

    @classmethod
    def _near_miss(cls, txns, reference, exp_amount, exp_currency) -> Optional[Dict[str, Any]]:
        """A transaction quoting `reference` whose amount or currency is not the one
        asked for, as a 'needs_review' answer with its evidence — or None."""
        for txn in txns:
            note = str(txn.get('note') or txn.get('description') or '')
            if not reference or reference not in note:
                continue
            facts = cls.transaction_facts(txn)
            currency = facts['currency']
            if exp_currency and currency and currency != exp_currency:
                reason = 'currency_differs'
            else:
                try:
                    same = (exp_amount is None or Decimal(str(facts['amount']))
                            .quantize(Decimal('0.01')) == exp_amount)
                except Exception:
                    same = False
                if same:
                    continue          # an exact match: `_match` answers it
                reason = 'amount_differs'
            return {'status': 'needs_review', 'reason': reason, 'reference': reference,
                    **facts, 'event_id': facts['transaction_id'], 'gateway_data': txn}
        return None

    @staticmethod
    def _says_there_is_more(envelope) -> bool:
        """Whether the envelope itself says the page is not the end (CONFIRM the key).

        A short page is only proof of the end if the gateway does not cap pages below
        `PAGE_LIMIT`; any of the usual continuation markers is taken at its word."""
        if not isinstance(envelope, dict):
            return False
        for holder in (envelope, envelope.get('meta'), envelope.get('pagination')):
            if not isinstance(holder, dict):
                continue
            for key in ('has_more', 'next', 'next_page', 'next_page_url', 'next_cursor'):
                if holder.get(key):
                    return True
        return False

    @staticmethod
    def _match(txns, reference, exp_amount, exp_currency) -> Optional[Dict[str, Any]]:
        """The verified 'completed' answer for the first transaction that matches, or None."""
        for txn in txns:
            if not isinstance(txn, dict):
                continue
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
        return None

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
