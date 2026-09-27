"""Stored files leave storage AFTER the transaction that removed their rows commits, and
OFF the request path.

Every path that removes a row holding a file — a delete, a replaced upload, a
conversation, an account erasure — used to delete the bytes itself, in an `on_commit`
callback that ran inside the request: one storage call per file, each a network round
trip once media is on object storage. An erasure of a coach with thousands of chat
photos spent that many round trips in the DELETE request after the database work was
done (the production-readiness audit, 2026-09-25).

Now those paths name the files (`delete_after_commit`) and, once the transaction
commits, the names are handed to the `training_platform.privacy.delete_stored_files`
Celery task in chunks of `STORAGE_DELETE_BATCH`. The task deletes what it can and
retries ONLY the names storage refused, with backoff. A rollback drops the callback, so
a row that survives keeps its file. If the broker cannot take the message, the names
are deleted right there as before — a slower request is better than a file nothing
will ever find again.

The files are named by the FIELD that stores them (`"chat.Message.attachment"`), because
a task message carries text and each field says which storage it writes to.

What does NOT come here: a trainer's identity documents are deleted from storage BEFORE
their rows, synchronously, and an erasure is refused when storage refuses
(`trainer_verification.services.delete_documents`, `erasure_refused`) — a handful of
files whose loss would matter more than a slow request.
"""
from __future__ import annotations

import logging

from django.conf import settings
from django.db import DEFAULT_DB_ALIAS, connections, transaction

logger = logging.getLogger(__name__)

DEFAULT_DELETE_BATCH = 100


def delete_batch() -> int:
    return max(1, int(getattr(settings, "STORAGE_DELETE_BATCH", DEFAULT_DELETE_BATCH)))


def field_label(field) -> str:
    """`"app_label.Model.field"`: where a file field's storage can be found again."""
    return f"{field.model._meta.label}.{field.name}"


def storage_for(label: str):
    """The storage the field named by `field_label` writes to."""
    from django.apps import apps

    model_label, _dot, field_name = label.rpartition(".")
    return apps.get_model(model_label)._meta.get_field(field_name).storage


def delete_now(label: str, names) -> list[str]:
    """Delete each name from the field's storage, here and now. Returns the names
    storage refused; never raises. A name already gone counts as deleted: every
    storage backend in use treats a missing name as nothing to do."""
    storage = storage_for(label)
    refused = []
    for name in names:
        try:
            storage.delete(name)
        except Exception:       # noqa: BLE001 — storage may be unreachable
            logger.warning("could not delete stored file %s (%s)", name, label,
                           exc_info=True)
            refused.append(name)
    return refused


def hand_over(label: str, names) -> None:
    """Give `names` to the delete task, a chunk per message. Runs after the commit."""
    from training_platform.privacy.tasks import delete_stored_files

    names = list(names)
    size = delete_batch()
    for start in range(0, len(names), size):
        chunk = names[start:start + size]
        try:
            delete_stored_files.delay(label, chunk)
        except Exception:       # noqa: BLE001 — the broker, or an eager task that gave up
            logger.warning("the file-delete task could not be queued; deleting %d "
                           "file(s) of %s in the request", len(chunk), label,
                           exc_info=True)
            delete_now(label, chunk)


class _StorageSweep:
    """An `on_commit` callback holding the files one transaction removed, by field. Sweeps
    of one transaction merge into one where a rollback cannot tell them apart
    (`delete_after_commit`), so a file two paths both reach is handed over once."""

    def __init__(self):
        self.names: dict[str, dict] = {}

    def add(self, label: str, names) -> None:
        self.names.setdefault(label, {}).update(dict.fromkeys(names))

    def __call__(self):
        for label, names in self.names.items():
            hand_over(label, list(names))


def delete_after_commit(label: str, names, using: str = DEFAULT_DB_ALIAS) -> None:
    """Delete these stored files of the field `label` once the surrounding transaction
    COMMITS, through the delete task. On a rollback they stay, with the rows that still
    point at them. Outside a transaction the hand-over is immediate.

    Inside a transaction, a sweep already waiting under EVERY savepoint that is open now
    takes the names instead of a second callback, so an erasure — whose conversation
    receiver and messages step both reach the person's own chat files — hands each file
    over once. That merge is safe because Django drops an `on_commit` callback when a
    savepoint it was registered under rolls back: a sweep registered under all the open
    savepoints is dropped by every rollback that would drop these names. (The other
    savepoints in its set were released already and cannot roll back; `None` marks an
    `atomic(savepoint=False)`, which never does.) Anything else gets a callback of its
    own: a second delete of a name is a no-op, a missed one is not.
    """
    names = [name for name in names if name]
    if not names:
        return
    connection = connections[using]
    if connection.in_atomic_block:
        open_now = {sid for sid in connection.savepoint_ids if sid}
        for entry in connection.run_on_commit:
            savepoints, callback = entry[0], entry[1]
            if isinstance(callback, _StorageSweep) and open_now <= set(savepoints):
                callback.add(label, names)
                return
    sweep = _StorageSweep()
    sweep.add(label, names)
    transaction.on_commit(sweep, using=using)
