"""Periodic work over every user, in bounded batches that hand the rest on.

A beat task that walks every client in one invocation has ONE soft time limit for all of
them (`CELERY_TASK_SOFT_TIME_LIMIT`, 300 s): past a few thousand users it is cut off
part-way, every day, and whoever sorts later is silently skipped (the production-readiness
audit, 2026-09-25: `generate_daily_advice`, `compute_all_user_insights` and the rest). So
such a task takes one page at a time and hands the rest to a fresh invocation of itself:

    batch = Batch()
    for row in batch.page(queryset, after):    # at most PERIODIC_BATCH_SIZE rows, pk > after
        if batch.spent():                      # PERIODIC_BATCH_SECONDS, well inside the limit
            break
        batch.done(row.pk)
        handle(row)                            # idempotent: a redelivered page changes nothing
    batch.hand_on(task, **carry)               # the rest: the same task, `after` the last pk

Each invocation holds one page of rows and is short. The next page is a new message at
the back of the queue, so the jobs that move money (the payment poller runs every
minute) get a worker slot between pages instead of waiting behind a whole sweep. A retry
of an invocation re-runs its own page from its own `after`, which is safe because every
handler here is idempotent per row — that is a requirement of using this, not a detail.

Keyset paging on the primary key — not OFFSET, and not a server-side cursor: it costs the
same at any depth, it works through any connection pooler (a named cursor does not
survive transaction pooling), and a row added mid-walk is met once or not at all, never
twice.

Settings (all optional, read on every use): `PERIODIC_BATCH_SIZE` rows a page (200),
`PERIODIC_BATCH_SECONDS` a page may spend (120; the production boot check refuses a value
that does not leave room under the soft limit), `PERIODIC_SCAN_SIZE` rows a page for scans
whose per-row work is a comparison, not a query (5000).
"""
from __future__ import annotations

import logging
import time
from datetime import date, datetime

from django.conf import settings

logger = logging.getLogger(__name__)

DEFAULT_BATCH_SIZE = 200
DEFAULT_BATCH_SECONDS = 120.0
DEFAULT_SCAN_SIZE = 5000


def batch_size() -> int:
    return max(1, int(getattr(settings, "PERIODIC_BATCH_SIZE", DEFAULT_BATCH_SIZE)))


def batch_seconds() -> float:
    return float(getattr(settings, "PERIODIC_BATCH_SECONDS", DEFAULT_BATCH_SECONDS))


def scan_size() -> int:
    return max(1, int(getattr(settings, "PERIODIC_SCAN_SIZE", DEFAULT_SCAN_SIZE)))


class Batch:
    """One invocation's share of a walk: at most `size` rows, for at most `seconds`.

    `page()` reads the next rows after a cursor, `done()` records each one taken,
    `spent()` says the time is up (never before one row is done, so every invocation
    makes progress), and `cut` is True when rows remain for another invocation —
    `hand_on()` enqueues it with `after` set to the last row done.
    """

    def __init__(self, *, size: int | None = None, seconds: float | None = None):
        self.size = batch_size() if size is None else max(1, int(size))
        self.deadline = time.monotonic() + (batch_seconds() if seconds is None
                                            else float(seconds))
        self.handled = 0          # rows done in this invocation, across every page
        self.cut = False
        self.last = None          # the key of the last row done in the CURRENT page

    def page(self, queryset, after=None, key: str = "pk") -> list:
        """Up to the rows this invocation has left, with `key` > `after`, ascending."""
        self.last = after
        left = self.size - self.handled
        if left <= 0:
            self.cut = True
            return []
        ordered = queryset.order_by(key)
        if after is not None:
            ordered = ordered.filter(**{f"{key}__gt": after})
        rows = list(ordered[: left + 1])
        if len(rows) > left:
            self.cut = True
            rows = rows[:left]
        return rows

    def done(self, key_value) -> None:
        self.handled += 1
        self.last = key_value

    def spent(self) -> bool:
        """The time is up. Checked before each row; once true, the rest is handed on."""
        if self.handled and time.monotonic() >= self.deadline:
            self.cut = True
            return True
        return False

    def hand_on(self, task, **kwargs) -> bool:
        """Enqueue `task` for the rest, `after` the last row done, if anything remains.
        `kwargs` is what the next invocation needs besides the cursor (a phase, the
        moment the walk began), JSON-serialisable."""
        if not self.cut:
            return False
        kwargs = {**kwargs, "after": self.last}
        logger.info("%s: %d done in this batch; the rest continues after %s",
                    getattr(task, "name", task), self.handled, self.last)
        task.apply_async(kwargs=kwargs)
        return True


def delete_in_pages(queryset, batch: Batch) -> int:
    """Delete `queryset` a page of primary keys at a time, until it is empty or the
    batch's time is spent (then `batch.cut` is True and the caller hands on). Each page
    is its own DELETE, so what Django collects for it — a model with cascades or
    receivers is loaded row by row — is bounded by the page, never by the backlog."""
    model = queryset.model
    removed = 0
    while True:
        if batch.spent():
            return removed
        ids = list(queryset.order_by().values_list("pk", flat=True)[: batch.size])
        if not ids:
            return removed
        count, _detail = model._default_manager.filter(pk__in=ids).delete()
        batch.handled += 1
        removed += count
        if not count:
            # Matching rows and nothing deleted: something protects them. Stop rather
            # than spin; the next run sees the same and says so again.
            logger.warning("delete_in_pages made no progress on %s; stopping",
                           model.__name__)
            return removed


def as_datetime(value):
    """A datetime carried through a task's kwargs as ISO text, read back. None stays
    None, and a datetime passes through (a direct call)."""
    if value is None or isinstance(value, datetime):
        return value
    return datetime.fromisoformat(str(value))


def as_date(value):
    """A date carried through a task's kwargs as ISO text, read back. None stays None,
    and a date passes through (a direct call)."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value))


def carry(value):
    """`value` as a task kwarg: a datetime or date as ISO text, anything else as it is."""
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return value
