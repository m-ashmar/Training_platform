"""Every change to a verification, in one place.

The trainer's side — upload, remove, submit — and the operator's — decide, revoke —
each run in one transaction that locks the TRAINER'S USER ROW first and the application
second. The user row is the mutex: it is the row `trainer_is_verified` lives on, and the
project's lock order puts user rows before anything hanging off them. So a decision and
an upload for the same trainer never interleave, and two operators deciding the same
application at once run one after the other — the second finds it no longer `pending`
and is refused (`ReviewConflict`), rather than both writing a decision.

Files are decoded BEFORE the transaction (`files.process`) so no lock is held while an
image is decoded, and written inside it; if the transaction fails, what was written is
deleted again, so a refusal never leaves an identity document in storage that no row
points at. Whenever a document is deleted for good — one nobody reviewed, replaced or
removed (`_retire`); the retention purge; an account's erasure — its files go FIRST and
its row only once they are gone (`delete_documents`), so a storage error keeps the row
that knows where the file is. None of that happens inside a transaction that could
still roll back: a trainer's replacement deletes after its commit.
"""
from __future__ import annotations

import logging
from datetime import timedelta
from functools import partial

from django.conf import settings
from django.core.files.base import ContentFile
from django.db import transaction
from django.utils import timezone
from django.utils.translation import gettext as _

from . import files, slots
from .models import (ApplicationState, DecisionOutcome, DocumentState,
                     VerificationApplication, VerificationDecision, VerificationDocument)
from .storage import document_storage, signed_url, url_ttl

logger = logging.getLogger(__name__)

#: The longest reason an operator may write; it is shown to the trainer and pushed.
MAX_REASON = 500


class VerificationError(Exception):
    """A trainer-side refusal. `code` is the app's; `status` the HTTP status."""

    def __init__(self, code: str, message: str, status: int = 400, **extra):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.extra = extra


class ReviewConflict(Exception):
    """An operator decision that cannot be taken on the application as it now is."""


def retention_days() -> int:
    return int(getattr(settings, "VERIFICATION_REPLACED_DOCUMENT_RETENTION_DAYS", 90))


# --------------------------------------------------------------------- reading
def application_of(trainer):
    return VerificationApplication.objects.filter(trainer=trainer).first()


_UNSET = object()


def status_for(trainer, application=_UNSET) -> str:
    """`not_started` | `pending` | `rejected` | `verified` — what the app draws.

    `trainer_is_verified` is what every other part of the platform reads (the paywall
    exemption, the roster's ranking, the `verified` mark), so it decides `verified`: a
    trainer an operator verified by hand before applications existed has no documents
    and is still verified. Otherwise the application's state; no application at all is
    `not_started`.
    """
    if getattr(trainer, "trainer_is_verified", False):
        return ApplicationState.VERIFIED
    if application is _UNSET:
        application = application_of(trainer)
    if application is not None and application.state != ApplicationState.VERIFIED:
        return application.state
    return ApplicationState.NOT_STARTED


def _current(application) -> dict:
    if application is None:
        return {}
    return {d.slot: d for d in application.documents.filter(replaced_at__isnull=True)}


def _can_change(status: str, slot, document) -> bool:
    if status == ApplicationState.VERIFIED:
        return False
    if status == ApplicationState.PENDING:
        # While in review, only an EMPTY optional slot may be filled (the design's
        # "أضف" on the nutrition certificate). A filled slot is what the operator is
        # looking at, and a required slot cannot be empty once submitted.
        return document is None and not slot.required
    return True


def _iso(value):
    return value.isoformat() if value else None


def read(trainer, request=None) -> dict:
    """The body of `GET /api/v1/trainer/verification/` (the view documents it)."""
    application = application_of(trainer)
    status = status_for(trainer, application)
    current = _current(application)
    documents = []
    for slot in slots.SLOTS:
        doc = current.get(slot.key)
        documents.append({
            "slot": slot.key,
            "label": str(slot.label),
            "required": slot.required,
            "accepts": list(slot.kinds),
            "state": doc.state if doc else "missing",
            "rejection_reason": (doc.rejection_reason or None) if doc else None,
            "kind": doc.kind if doc else None,
            "content_type": doc.content_type if doc else None,
            "size": doc.size if doc else None,
            "uploaded_at": _iso(doc.uploaded_at) if doc else None,
            "thumbnail_url": signed_url(doc.thumbnail, request) if doc else None,
            "file_url": signed_url(doc.file, request) if doc else None,
            "can_change": _can_change(status, slot, doc),
        })
    missing = [key for key in slots.REQUIRED if key not in current]
    needs_replacing = [slot.key for slot in slots.SLOTS
                       if slot.key in current
                       and current[slot.key].state == DocumentState.REJECTED]
    in_play = application is not None and status == application.state
    return {
        "status": status,
        "round": application.round if application else 0,
        "submitted_at": _iso(application.submitted_at) if in_play else None,
        "decided_at": _iso(application.decided_at) if in_play else None,
        "rejection_reason": (application.rejection_reason or None)
        if in_play and status == ApplicationState.REJECTED else None,
        "review_business_days": int(getattr(settings, "VERIFICATION_REVIEW_BUSINESS_DAYS", 3)),
        "missing": missing,
        "needs_replacing": needs_replacing,
        "can_submit": (status in (ApplicationState.NOT_STARTED, ApplicationState.REJECTED)
                       and not missing and not needs_replacing),
        "url_expires_in": url_ttl(),
        "documents": documents,
    }


# --------------------------------------------------------------- trainer side
def _lock_trainer(trainer_id):
    from users.models import CustomUser

    return CustomUser.objects.select_for_update().get(pk=trainer_id)


def _slot_or_refuse(slot_key):
    slot = slots.resolve(slot_key)
    if slot is None:
        raise VerificationError(
            "verification_unknown_slot", _("There is no such document to upload."),
            status=404, slots=[s.key for s in slots.SLOTS])
    return slot


def _refuse_if_closed(status: str):
    if status == ApplicationState.VERIFIED:
        raise VerificationError("verification_already_verified",
                                _("Your account is already verified."), status=409)


def _retire(document, now) -> None:
    """Take a document out of its slot.

    One an operator already decided on is KEPT — replaced, with the state and reason
    they gave it — until its retention runs out. One nobody has looked at yet has no
    history worth keeping and is deleted at once, files FIRST: here it is only marked
    replaced and due now, and once the transaction commits its files are deleted and
    then its row (`_delete_unreviewed`). Deleting the row here, as this did, left the
    files to the post-commit receiver of `training_platform/signals.py`, which can only
    log a storage error — and an identity photo stayed in the private bucket for good,
    with no row pointing at it (SIDE_BUGS S-209). Deleting the files here instead would
    be undone by nothing if the transaction rolled back. Now a storage error keeps the
    row, and the daily purge (`purge_replaced_documents`) tries again.
    """
    reviewed = document.was_reviewed
    document.replaced_at = now
    document.purge_after = now + timedelta(days=retention_days()) if reviewed else now
    document.save(update_fields=["replaced_at", "purge_after"])
    if not reviewed:
        transaction.on_commit(partial(_delete_unreviewed, document.pk), robust=True)


def _delete_unreviewed(document_id) -> None:
    """After the commit: delete a retired document nobody reviewed, files first. When
    storage refuses, its row is kept (replaced, due now) for the next purge."""
    document = VerificationDocument.objects.filter(pk=document_id,
                                                   replaced_at__isnull=False).first()
    if document is None or document.was_reviewed:
        return
    try:
        with transaction.atomic():
            delete_documents([document])
    except DocumentFilesNotDeleted:
        logger.warning("An unreviewed verification document's files could not be "
                       "deleted; its row is kept for the next purge", exc_info=True)


def upload_document(trainer, slot_key, upload) -> VerificationDocument:
    """Put `upload` in the trainer's slot, replacing what was there.

    Raises `VerificationError` (the view answers it) or `files.DocumentRefused`.
    """
    slot = _slot_or_refuse(slot_key)
    processed = files.process(upload, slot)

    written = []
    try:
        with transaction.atomic():
            locked = _lock_trainer(trainer.pk)
            application, _created = (VerificationApplication.objects.select_for_update()
                                     .get_or_create(trainer=locked))
            status = status_for(locked, application)
            _refuse_if_closed(status)
            current = (application.documents.select_for_update()
                       .filter(slot=slot.key, replaced_at__isnull=True).first())
            if status == ApplicationState.PENDING and not _can_change(status, slot, current):
                raise VerificationError(
                    "verification_in_review",
                    _("Your documents are being reviewed. You can change them if we ask "
                      "you to."), status=409)
            twin = (application.documents.filter(replaced_at__isnull=True,
                                                 sha256=processed.sha256)
                    .exclude(slot=slot.key).first())
            if twin is not None:
                raise VerificationError(
                    "verification_duplicate_document",
                    _("You already uploaded this same file for another document."),
                    slot=twin.slot)

            now = timezone.now()
            if current is not None:
                _retire(current, now)
            document = VerificationDocument(
                application=application, slot=slot.key, kind=processed.kind,
                content_type=processed.content_type, size=processed.size,
                sha256=processed.sha256, width=processed.width, height=processed.height,
                uploaded_by=locked, uploaded_at=now)
            document.file.save(f"document.{processed.extension}",
                               ContentFile(processed.content), save=False)
            written.append(document.file.name)
            if processed.thumbnail:
                document.thumbnail.save("thumbnail.jpg", ContentFile(processed.thumbnail),
                                        save=False)
                written.append(document.thumbnail.name)
            document.save()
            application.save(update_fields=["updated_at"])
    except Exception:
        storage = document_storage()
        for name in written:
            try:
                storage.delete(name)
            except Exception:
                logger.warning("could not remove an orphaned verification file", exc_info=True)
        raise
    return document


def remove_document(trainer, slot_key) -> bool:
    """Empty one slot. True when something was removed; an empty slot is not an error."""
    slot = _slot_or_refuse(slot_key)
    with transaction.atomic():
        locked = _lock_trainer(trainer.pk)
        application = (VerificationApplication.objects.select_for_update()
                       .filter(trainer=locked).first())
        status = status_for(locked, application)
        _refuse_if_closed(status)
        if status == ApplicationState.PENDING:
            raise VerificationError(
                "verification_in_review",
                _("Your documents are being reviewed. You can change them if we ask you to."),
                status=409)
        if application is None:
            return False
        current = (application.documents.select_for_update()
                   .filter(slot=slot.key, replaced_at__isnull=True).first())
        if current is None:
            return False
        _retire(current, timezone.now())
        application.save(update_fields=["updated_at"])
        return True


def submit(trainer) -> VerificationApplication:
    """Send the application for review. Raises `VerificationError` (409s)."""
    with transaction.atomic():
        locked = _lock_trainer(trainer.pk)
        application = (VerificationApplication.objects.select_for_update()
                       .filter(trainer=locked).first())
        status = status_for(locked, application)
        _refuse_if_closed(status)
        if status == ApplicationState.PENDING:
            raise VerificationError(
                "verification_already_pending",
                _("Your application is already being reviewed."), status=409)
        current = _current(application)
        missing = [key for key in slots.REQUIRED if key not in current]
        if missing:
            raise VerificationError(
                "verification_documents_missing",
                _("Upload every required document before you send the application."),
                status=409, missing=missing)
        rejected = [s.key for s in slots.SLOTS
                    if s.key in current and current[s.key].state == DocumentState.REJECTED]
        if rejected:
            raise VerificationError(
                "verification_documents_rejected",
                _("Replace the documents we could not accept before you send the "
                  "application again."), status=409, rejected=rejected)
        application.state = ApplicationState.PENDING
        application.round += 1
        application.submitted_at = timezone.now()
        application.decided_at = None
        application.decided_by = None
        application.rejection_reason = ""
        application.save()
        return application


# -------------------------------------------------------------- operator side
def _snapshot(documents) -> list:
    return [{"document_id": d.pk, "slot": d.slot, "state": d.state,
             "reason": d.rejection_reason, "sha256": d.sha256,
             "uploaded_at": _iso(d.uploaded_at)} for d in documents]


def _trainer_id_of(application_id):
    trainer_id = (VerificationApplication.objects.filter(pk=application_id)
                  .values_list("trainer_id", flat=True).first())
    if trainer_id is None:
        raise ReviewConflict("There is no such application.")
    return trainer_id


def _document_reasons(document_reasons) -> dict:
    """`{document_id: reason}`, the empty ones dropped, each cut to `MAX_REASON`."""
    reasons = {}
    for key, value in (document_reasons or {}).items():
        text = str(value or "").strip()
        if text:
            reasons[int(key)] = text[:MAX_REASON]
    return reasons


def _current_for_decision(application, reasons, seen_document_ids, expected_round):
    """The application's current documents, locked — once the page the operator decided
    on is known to show them.

    The console page posts the ids of the documents it showed and the round it showed.
    If the trainer changed a document since, or the application has been sent again
    since (a new round, even with the same files — another operator may have rejected
    or revoked it in between, and approved it again), the decision is refused rather
    than taken on a state nobody looked at.
    """
    documents = list(application.documents.select_for_update()
                     .filter(replaced_at__isnull=True).order_by("slot", "id"))
    ids = {d.pk for d in documents}
    if seen_document_ids is not None and {int(i) for i in seen_document_ids} != ids:
        raise ReviewConflict("The trainer changed the documents while you were "
                             "reviewing them. Open the application again.")
    if expected_round is not None and int(expected_round) != application.round:
        raise ReviewConflict(
            f"The application changed while you were reviewing it: it is at round "
            f"{application.round} now, not {expected_round}. Open it again.")
    if set(reasons) - ids:
        raise ReviewConflict("A document reason names a document that is not part of "
                             "this application.")
    return documents


def decide(application_id, operator, *, approve: bool, reason: str = "",
           document_reasons: dict | None = None,
           seen_document_ids=None, expected_round=None) -> VerificationDecision:
    """Approve or reject a pending application. Operator-only; the console calls it.

    `document_reasons` is `{document_id: reason}` for the documents being refused (a
    rejection only); every other current document is accepted. `reason` is required for
    a rejection — it is what the trainer reads and what the push carries.
    `seen_document_ids` and `expected_round`, when given, are what the operator was
    looking at: the documents and the round. If the trainer changed a document since,
    or the application was decided and sent again since, the decision is refused rather
    than taken on files nobody reviewed.

    Raises `ReviewConflict` when the application is not pending — including when
    another operator decided it first: the trainer's user row and then the application
    are locked, so the second decision waits for the first and then finds it closed.
    """
    trainer_id = _trainer_id_of(application_id)
    reason = (reason or "").strip()
    reasons = _document_reasons(document_reasons)
    if len(reason) > MAX_REASON:
        raise ReviewConflict(f"The reason is too long: at most {MAX_REASON} characters.")

    with transaction.atomic():
        trainer = _lock_trainer(trainer_id)
        application = VerificationApplication.objects.select_for_update().get(
            pk=application_id)
        if application.state != ApplicationState.PENDING:
            raise ReviewConflict(
                f"This application is {application.state}, not pending: it was decided "
                f"already.")
        documents = _current_for_decision(application, reasons, seen_document_ids,
                                          expected_round)
        now = timezone.now()
        if approve:
            if reasons:
                raise ReviewConflict("An approval cannot refuse a document: reject the "
                                     "application instead.")
            present = {d.slot for d in documents}
            if any(key not in present for key in slots.REQUIRED):
                raise ReviewConflict("A required document is missing: this application "
                                     "cannot be approved.")
            for doc in documents:
                doc.state, doc.rejection_reason = DocumentState.ACCEPTED, ""
                doc.reviewed_in_round = application.round
                doc.save(update_fields=["state", "rejection_reason", "reviewed_in_round"])
            application.state = ApplicationState.VERIFIED
            application.rejection_reason = ""
            outcome = DecisionOutcome.VERIFIED
        else:
            if not reason:
                raise ReviewConflict("Say why the application is rejected: the trainer "
                                     "is shown this reason.")
            for doc in documents:
                refused = reasons.get(doc.pk)
                doc.state = DocumentState.REJECTED if refused else DocumentState.ACCEPTED
                doc.rejection_reason = refused or ""
                doc.reviewed_in_round = application.round
                doc.save(update_fields=["state", "rejection_reason", "reviewed_in_round"])
            application.state = ApplicationState.REJECTED
            application.rejection_reason = reason
            outcome = DecisionOutcome.REJECTED
        application.decided_at = now
        application.decided_by = operator
        application.save(update_fields=["state", "rejection_reason", "decided_at",
                                        "decided_by", "updated_at"])
        # The ONE place this boolean is written (with `revoke` below).
        trainer.trainer_is_verified = bool(approve)
        trainer.save(update_fields=["trainer_is_verified"])
        decision = VerificationDecision.objects.create(
            application=application, round=application.round, outcome=outcome,
            reason=reason, documents=_snapshot(documents), decided_by=operator,
            decided_at=now)
        _notify_on_commit(outcome, trainer_id, application.pk, reason)
        return decision


def revoke(application_id, operator, reason: str, *, document_reasons: dict | None = None,
           seen_document_ids=None, expected_round=None) -> VerificationDecision:
    """Withdraw a verification (a document found forged afterwards). The application
    goes back to `rejected` with `reason`, and the trainer can fix it and resubmit.

    `document_reasons` is `{document_id: reason}` for the documents it is withdrawn
    for: each becomes `rejected` with its reason, so the app lists it in
    `needs_replacing` and it cannot be sent again unchanged
    (`verification_documents_rejected`); the others stay `accepted`. A revoke that
    names none sends EVERY document back to `uploaded`: nothing stays marked accepted
    by the decision just withdrawn, and the next round reviews them all afresh. Either
    way the documents keep `reviewed_in_round`, so one the trainer then replaces is
    kept for comparison (`_retire`) rather than deleted.

    `seen_document_ids` and `expected_round` guard a stale console page, as in
    `decide`: a verification granted since the page was opened is not withdrawn by it.
    """
    trainer_id = _trainer_id_of(application_id)
    reason = (reason or "").strip()
    reasons = _document_reasons(document_reasons)
    if not reason:
        raise ReviewConflict("Say why the verification is withdrawn: the trainer is "
                             "shown this reason.")
    if len(reason) > MAX_REASON:
        raise ReviewConflict(f"The reason is too long: at most {MAX_REASON} characters.")
    with transaction.atomic():
        trainer = _lock_trainer(trainer_id)
        application = VerificationApplication.objects.select_for_update().get(
            pk=application_id)
        if application.state != ApplicationState.VERIFIED:
            raise ReviewConflict(f"This application is {application.state}: only a "
                                 f"verified one can be revoked.")
        documents = _current_for_decision(application, reasons, seen_document_ids,
                                          expected_round)
        for doc in documents:
            refused = reasons.get(doc.pk)
            if refused:
                doc.state, doc.rejection_reason = DocumentState.REJECTED, refused
            elif not reasons:
                doc.state, doc.rejection_reason = DocumentState.UPLOADED, ""
            doc.reviewed_in_round = application.round
            doc.save(update_fields=["state", "rejection_reason", "reviewed_in_round"])
        now = timezone.now()
        application.state = ApplicationState.REJECTED
        application.rejection_reason = reason
        application.decided_at = now
        application.decided_by = operator
        application.save(update_fields=["state", "rejection_reason", "decided_at",
                                        "decided_by", "updated_at"])
        trainer.trainer_is_verified = False
        trainer.save(update_fields=["trainer_is_verified"])
        decision = VerificationDecision.objects.create(
            application=application, round=application.round,
            outcome=DecisionOutcome.REVOKED, reason=reason,
            documents=_snapshot(documents), decided_by=operator, decided_at=now)
        _notify_on_commit(DecisionOutcome.REJECTED, trainer_id, application.pk, reason)
        return decision


def _notify_on_commit(outcome, trainer_id, application_id, reason) -> None:
    """The push and the inbox row, once the decision is committed.

    `robust=True`: the decision stands whatever happens to the notification; a broker
    outage is the notification pipeline's to retry, not a reason to show the operator
    an error for a decision that was saved.
    """
    from notifications.domain.dispatcher import emit_event
    from notifications.domain.verification_events import (
        TrainerVerificationApprovedEvent, TrainerVerificationRejectedEvent)

    if outcome == DecisionOutcome.VERIFIED:
        event = TrainerVerificationApprovedEvent(recipient_id=trainer_id,
                                                 application_id=application_id)
    else:
        event = TrainerVerificationRejectedEvent(recipient_id=trainer_id,
                                                 application_id=application_id,
                                                 reason=reason)
    transaction.on_commit(lambda: emit_event(event), robust=True)


# ------------------------------------------------------------------ retention
class DocumentFilesNotDeleted(Exception):
    """Storage refused to delete a document's file. Its row was kept, so the deletion can
    be tried again: the row is the only thing that knows where the file is."""


def delete_document_files(document) -> None:
    """Delete the document's stored files — the file and its thumbnail — NOW.

    A file already gone counts as deleted (both backends say so). Clears the names on
    the instance, so the post-commit receiver of `training_platform/signals.py` has
    nothing left to do when the row goes. Raises `DocumentFilesNotDeleted`, leaving the
    name of what could not be deleted on the instance.
    """
    for stored in (document.file, document.thumbnail):
        if stored:
            try:
                stored.delete(save=False)
            except Exception as exc:
                raise DocumentFilesNotDeleted(
                    f"verification document {document.pk}: {stored.name}") from exc


def delete_documents(documents) -> int:
    """Delete documents for good: each one's files FIRST, and its row only after them.

    The other order — the row, then the files after the commit — is what Django's
    receivers do for every other upload, and it lost identity documents: when storage
    failed, the post-commit deletion could only log it, the row that knew where the
    file was had gone, and nothing would ever find the file again (the review of
    2026-09-24). Raises `DocumentFilesNotDeleted` at the first failure; the documents
    not reached keep their rows and files, and the caller decides what that means.
    """
    removed = 0
    for document in documents:
        delete_document_files(document)
        document.delete()
        removed += 1
    return removed


def purge_replaced_documents(now=None) -> int:
    """Delete replaced documents whose retention ran out, files included.

    A document the trainer replaced after an operator decided on it is kept
    `VERIFICATION_REPLACED_DOCUMENT_RETENTION_DAYS` (90) days — long enough to compare
    it with its replacement at the next review — and never while that review is still
    waiting (a `pending` application keeps them until it is decided). What the operator
    decided about it survives in the `VerificationDecision` snapshot.

    The files go before the row (`delete_documents`). When storage refuses, the row is
    KEPT, logged, and the next daily run tries again; the count is of what went. A
    document nobody reviewed is due the moment it is replaced (`_retire`), so this is
    also where one whose files storage refused after that commit is tried again.
    """
    now = now or timezone.now()
    expired = (VerificationDocument.objects
               .filter(replaced_at__isnull=False, purge_after__lt=now)
               .exclude(application__state=ApplicationState.PENDING)
               .order_by("id"))
    removed = 0
    for document in expired.iterator():
        try:
            with transaction.atomic():
                removed += delete_documents([document])
        except DocumentFilesNotDeleted:
            logger.warning("A replaced verification document's files could not be "
                           "deleted; its row is kept for the next purge", exc_info=True)
    return removed
