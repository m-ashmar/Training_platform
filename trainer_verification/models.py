"""A trainer's verification: one application, its documents, and every decision on it.

**States** (the design, `12-trainer-shell/TrainerVerify.html` and `TrainerLocked.html`):

    not_started ──submit──▶ pending ──approve──▶ verified
         ▲                     │                    │
         │                     └─reject─▶ rejected ◀┘ revoke
         └──(no row yet)                  │
                                          └──fix, then submit──▶ pending

`not_started` is also what a trainer with no row at all is (`services.status_for`).

**History — immutable decision rows, one live application.** Each trainer has ONE
application row, which holds the live state the app draws. What the operators decided is
never edited: every decision writes a `VerificationDecision` that cannot be updated
(`save()` refuses an existing row), carrying its outcome, reason, the round it closed,
who took it, and a snapshot of every document it covered with the state and reason it
gave that document. A resubmission after a rejection therefore starts a new ROUND on the
same application (`round` counts submissions) and erases nothing: the earlier decision
row is still there, and so is every document the trainer replaced — a replaced document
is not deleted but marked `replaced_at`, keeping the state and reason the operator gave
it, until its retention runs out (`tasks.purge_replaced_documents`). One row rather than
a row per round because everything the app and the console need is the CURRENT state,
and a history table is the honest place for the rest.

**`trainer_is_verified`** stays the boolean every existing payload reads, and it is
written in exactly one place: `services.decide` / `services.revoke`, in the same
transaction as the decision. The admin shows it read-only.
"""
import os
import secrets

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from . import slots
from .storage import document_storage


class ApplicationState(models.TextChoices):
    NOT_STARTED = "not_started", _("Not started")
    PENDING = "pending", _("In review")
    REJECTED = "rejected", _("Needs changes")
    VERIFIED = "verified", _("Verified")


class DocumentState(models.TextChoices):
    UPLOADED = "uploaded", _("Uploaded")
    ACCEPTED = "accepted", _("Accepted")
    REJECTED = "rejected", _("Rejected")


class DecisionOutcome(models.TextChoices):
    VERIFIED = "verified", _("Verified")
    REJECTED = "rejected", _("Rejected")
    #: A verified trainer whose verification an operator withdrew (a document found to
    #: be forged afterwards). The application goes back to `rejected`.
    REVOKED = "revoked", _("Revoked")


def _document_path(instance, filename):
    # A random name, and nothing about the person in it: a signed URL shows its path.
    ext = os.path.splitext(filename)[1].lower() or ".bin"
    return f"verification/{secrets.token_hex(16)}{ext}"


class VerificationApplication(models.Model):
    trainer = models.OneToOneField(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
        related_name="verification_application")
    state = models.CharField(max_length=16, choices=ApplicationState.choices,
                             default=ApplicationState.NOT_STARTED, db_index=True)
    #: How many times it has been submitted. The current round's decision, when there is
    #: one, is the `VerificationDecision` with this round.
    round = models.PositiveIntegerField(default=0)
    submitted_at = models.DateTimeField(null=True, blank=True, db_index=True)
    decided_at = models.DateTimeField(null=True, blank=True)
    decided_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
                                   null=True, blank=True, related_name="+")
    #: What the trainer is told when the application is rejected. Empty otherwise.
    rejection_reason = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        # The review queue: oldest submission first.
        ordering = ["submitted_at", "id"]
        verbose_name = "Verification application"
        verbose_name_plural = "Verification applications"
        indexes = [models.Index(fields=["state", "submitted_at"],
                                name="verification_queue_idx")]
        constraints = [
            models.CheckConstraint(
                condition=~models.Q(state=ApplicationState.PENDING)
                | models.Q(submitted_at__isnull=False),
                name="verification_pending_has_a_submission"),
            models.CheckConstraint(
                condition=~models.Q(state__in=[ApplicationState.VERIFIED,
                                               ApplicationState.REJECTED])
                | models.Q(decided_at__isnull=False),
                name="verification_decision_has_a_date"),
        ]

    def __str__(self):
        return f"{self.trainer} ({self.state})"


class VerificationQueueEntry(VerificationApplication):
    """The console's review queue: the same rows, listed pending-only and oldest
    first (`admin.VerificationQueueAdmin`). A proxy, so it is its own page."""

    class Meta:
        proxy = True
        ordering = ["submitted_at", "id"]
        verbose_name = "Verification queue entry"
        verbose_name_plural = "Verification queue (pending, oldest first)"


class VerificationDocument(models.Model):
    application = models.ForeignKey(VerificationApplication, on_delete=models.CASCADE,
                                     related_name="documents")
    slot = models.CharField(max_length=32, choices=slots.CHOICES)
    file = models.FileField(storage=document_storage, upload_to=_document_path,
                            max_length=255)
    thumbnail = models.FileField(storage=document_storage, upload_to=_document_path,
                                 max_length=255, blank=True)
    kind = models.CharField(max_length=8, choices=[(slots.IMAGE, "image"),
                                                   (slots.PDF, "pdf")])
    content_type = models.CharField(max_length=64)
    size = models.PositiveIntegerField(default=0)
    #: Of the upload as received: the same file sent to two slots is refused.
    sha256 = models.CharField(max_length=64, db_index=True)
    width = models.PositiveIntegerField(default=0)
    height = models.PositiveIntegerField(default=0)
    state = models.CharField(max_length=16, choices=DocumentState.choices,
                             default=DocumentState.UPLOADED)
    rejection_reason = models.TextField(blank=True, default="")
    #: The round whose decision gave this document its state; null until one did.
    reviewed_in_round = models.PositiveIntegerField(null=True, blank=True)
    uploaded_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
                                    null=True, blank=True, related_name="+")
    uploaded_at = models.DateTimeField(default=timezone.now)
    #: Set when the trainer replaced (or removed) a document an operator had already
    #: decided on. The row keeps that decision; its files go at `purge_after`.
    replaced_at = models.DateTimeField(null=True, blank=True, db_index=True)
    purge_after = models.DateTimeField(null=True, blank=True, db_index=True)

    class Meta:
        ordering = ["application_id", "slot", "-uploaded_at", "-id"]
        verbose_name = "Verification document"
        verbose_name_plural = "Verification documents"
        constraints = [
            # One file per slot: at most one CURRENT document per slot.
            models.UniqueConstraint(fields=["application", "slot"],
                                    condition=models.Q(replaced_at__isnull=True),
                                    name="one_current_verification_document_per_slot"),
        ]

    def __str__(self):
        return f"{self.slot} ({self.state})"

    @property
    def is_current(self) -> bool:
        return self.replaced_at is None

    @property
    def was_reviewed(self) -> bool:
        """An operator's decision covered it. Usually that shows in its state; a revoke
        that names no document sends every one back to `uploaded`, and those still
        carry the round that reviewed them (`services.revoke`)."""
        return (self.reviewed_in_round is not None
                or self.state in (DocumentState.ACCEPTED, DocumentState.REJECTED))


class ImmutableDecision(Exception):
    pass


class VerificationDecision(models.Model):
    """One operator decision, never edited. See the module docstring."""
    application = models.ForeignKey(VerificationApplication, on_delete=models.CASCADE,
                                    related_name="decisions")
    round = models.PositiveIntegerField()
    outcome = models.CharField(max_length=16, choices=DecisionOutcome.choices)
    #: Shown to the trainer for a rejection or a revocation; an operator's note
    #: otherwise.
    reason = models.TextField(blank=True, default="")
    #: `[{document_id, slot, state, reason, sha256, uploaded_at}]` — every document the
    #: decision covered, as it left them. Survives the documents themselves.
    documents = models.JSONField(default=list, blank=True)
    decided_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
                                   null=True, blank=True, related_name="+")
    decided_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["-decided_at", "-id"]
        verbose_name = "Verification decision"
        verbose_name_plural = "Verification decisions"

    def __str__(self):
        return f"round {self.round}: {self.outcome}"

    def save(self, *args, **kwargs):
        if self.pk is not None and not self._state.adding:
            raise ImmutableDecision("a verification decision is never edited")
        super().save(*args, **kwargs)

    def clean(self):
        if self.outcome in (DecisionOutcome.REJECTED, DecisionOutcome.REVOKED) \
                and not (self.reason or "").strip():
            raise ValidationError({"reason": _("Say why, so the trainer can fix it.")})
