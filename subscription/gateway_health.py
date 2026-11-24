"""Whether the server can read a payment gateway right now (the spec, section 6: "ShamCash
had a multi-hour outage in March 2026. Every payment screen needs a 'ShamCash is not
responding, your payment is safe, we will keep checking' state").

Recorded by every read of the merchant account (`PaymentGatewayService.fetch_payment_status`
and `list_transactions`: the 60-second poller, the late-money poller, the ten-minute
sweep, a customer's "check my payment"), into one `GatewayHealth` row per gateway:

* a read that ANSWERED — in full or not, matching or not — is a success: the gateway is
  there. It clears the streak and moves `last_success_at` (written at most every
  `_SUCCESS_WRITE_SECONDS` while nothing changes, so a busy poller does not rewrite the
  row on every lookup);
* a read that could not REACH the gateway (a timeout, a refused connection, a 5xx) is a
  failure: `consecutive_failures` grows, and the first one of a streak sets
  `failing_since`.

`state_for(gateway)` answers what the payment reads carry as `gateway_state`:

    {"state": "ok" | "degraded", "since": <iso> | null, "last_success_at": <iso> | null}

`degraded` once the gateway has failed at least `PAYMENT_GATEWAY_DEGRADED_AFTER_FAILURES`
reads in a row over at least `PAYMENT_GATEWAY_DEGRADED_AFTER_SECONDS` — one blip, or one
poll run's burst of lookups against a gateway that hiccupped for a second, is not an
outage. `since` is when that streak began. A gateway never read is `ok`.

Nothing DECIDES on this. It is what the screens say; the money is decided by reads that
answered. A checkout is never expired, failed or completed because a read failed
(`poll_open_payments` treats an unanswered read as evidence of nothing).
"""
from __future__ import annotations

import logging
from datetime import timedelta

from celery.exceptions import SoftTimeLimitExceeded
from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import F, Q
from django.utils import timezone

logger = logging.getLogger(__name__)

#: While a gateway keeps answering, `last_success_at` is rewritten at most this often.
_SUCCESS_WRITE_SECONDS = 30


def _failures_needed() -> int:
    return max(1, int(getattr(settings, 'PAYMENT_GATEWAY_DEGRADED_AFTER_FAILURES', 2)))


def _seconds_needed() -> int:
    return max(0, int(getattr(settings, 'PAYMENT_GATEWAY_DEGRADED_AFTER_SECONDS', 60)))


def _row(gateway: str):
    """The gateway's row, created on first use. Safe under a concurrent first use."""
    from .models import GatewayHealth

    row = GatewayHealth.objects.filter(gateway=gateway).first()
    if row is not None:
        return row
    try:
        with transaction.atomic():
            return GatewayHealth.objects.create(gateway=gateway)
    except IntegrityError:
        return GatewayHealth.objects.get(gateway=gateway)


def record_success(gateway: str, now=None) -> None:
    """A read of `gateway`'s merchant account answered."""
    from .models import GatewayHealth

    if not gateway:
        return
    now = now or timezone.now()
    try:
        _row(gateway)
        GatewayHealth.objects.filter(gateway=gateway).filter(
            Q(consecutive_failures__gt=0) | Q(last_success_at__isnull=True)
            | Q(last_success_at__lt=now - timedelta(seconds=_SUCCESS_WRITE_SECONDS))
        ).update(last_success_at=now, consecutive_failures=0, failing_since=None,
                 updated_at=now)
    except SoftTimeLimitExceeded:
        # The task's own deadline, which Celery raises INSIDE whatever runs when it
        # overruns — a subclass of Exception, so the line below swallowed it and the
        # overrunning poller went on (SIDE_BUGS S-260). It is the task's to handle.
        raise
    except Exception:                    # noqa: BLE001 — a record never stops a read
        logger.exception("Could not record a successful read of %s", gateway)


def record_failure(gateway: str, error: str = '', now=None) -> None:
    """A read of `gateway`'s merchant account could not reach it."""
    from .models import GatewayHealth

    if not gateway:
        return
    now = now or timezone.now()
    try:
        _row(gateway)
        # One UPDATE, so two workers failing at once both count: `F()` adds in the
        # database, and the streak's start is kept by whichever wrote it first.
        GatewayHealth.objects.filter(gateway=gateway, failing_since__isnull=True).update(
            failing_since=now)
        GatewayHealth.objects.filter(gateway=gateway).update(
            consecutive_failures=F('consecutive_failures') + 1, last_failure_at=now,
            last_error=str(error or '')[:500], updated_at=now)
    except SoftTimeLimitExceeded:
        raise                            # the task's deadline, as in `record_success`
    except Exception:                    # noqa: BLE001
        logger.exception("Could not record a failed read of %s", gateway)


def is_degraded(row, now=None) -> bool:
    if row is None or row.failing_since is None:
        return False
    now = now or timezone.now()
    return (row.consecutive_failures >= _failures_needed()
            and now - row.failing_since >= timedelta(seconds=_seconds_needed()))


def state_for(gateway: str | None, now=None) -> dict:
    """`{state, since, last_success_at}` for the gateway a payment is paid through."""
    from .models import GatewayHealth

    row = GatewayHealth.objects.filter(gateway=gateway).first() if gateway else None
    degraded = is_degraded(row, now)
    return {
        'state': 'degraded' if degraded else 'ok',
        'since': row.failing_since if degraded else None,
        'last_success_at': row.last_success_at if row is not None else None,
    }
