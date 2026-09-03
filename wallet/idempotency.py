"""Claiming an idempotency key, in one place, correctly.

Four money endpoints each carried their own copy of the same eight lines, and every
copy had the same two holes.

**The key was global.** `IdempotencyKey.key` was `unique=True` across the whole table
and nothing compared the row's owner to the caller. Clients choose their own keys, so
two clients picking `1` was enough: the second one was handed the first one's stored
response — reference id and both wallet balances — while their own transfer never
happened and still answered 200. Keys are now unique per caller, and a key belonging
to someone else is simply not found.

**Nothing checked that the replay was the same request.** `request_hash` was written
by all four endpoints and read by none, so a key replayed with a different amount
returned the earlier receipt and moved no money. A key now carries a fingerprint of
the request that claimed it, and a second request with the same key and different
content is refused rather than silently answered with the first one's result.

Legacy rows stored the key as its own hash, which fingerprints nothing. Migration 0004
blanks those, and an empty fingerprint means "written before this existed" — replayable,
but never compared.
"""
import hashlib
import json


class IdempotencyConflict(Exception):
    """The key exists but this request is not the one that claimed it.

    `in_flight` distinguishes the two reasons: a first attempt that has not finished
    (retry later) from a genuinely different request reusing the key (never retry).
    """

    def __init__(self, message, in_flight=False):
        super().__init__(message)
        self.in_flight = in_flight


def fingerprint(**fields) -> str:
    """A stable digest of the fields that make this request the request it is.

    Sorted keys and `default=str` so a Decimal amount and its string spelling hash
    alike; a caller passing the same money twice must land on the same digest.
    """
    canonical = json.dumps(fields, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def reserve(user, key: str, digest: str):
    """Claim `key` for `user`, bound to `digest`.

    Returns `(record, replay)`. `replay` is the stored response when this is a true
    repeat of a finished request, and None when the caller should go and do the work.
    Raises `IdempotencyConflict` when the key is held by a different request, or by an
    attempt that has not finished.
    """
    from wallet.models import IdempotencyKey

    record, created = IdempotencyKey.objects.get_or_create(
        created_by=user,
        key=key,
        defaults={"request_hash": digest},
    )
    if created:
        return record, None

    # A blank hash is a row from before fingerprints existed; comparing it would
    # reject replays that were legitimate when they were written.
    if record.request_hash and record.request_hash != digest:
        raise IdempotencyConflict(
            "This idempotency key was used for a different request.", in_flight=False
        )

    if record.processed and record.response_snapshot:
        return record, record.response_snapshot

    raise IdempotencyConflict(
        "A request with this idempotency key is still in progress.", in_flight=True
    )


def complete(record, response: dict) -> None:
    """Store the response so a later replay of the same request returns it."""
    record.processed = True
    record.response_snapshot = response
    record.save(update_fields=["processed", "response_snapshot"])


def release(record) -> None:
    """Give the key back after work that produced no side effect, so a retry can use it."""
    record.delete()
