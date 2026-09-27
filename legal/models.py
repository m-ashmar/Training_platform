"""The privacy policy and terms of use, and requests to delete an account made on the web.

**Legal documents** (spec B7: "Privacy policy and terms reachable in the app and on the
store page, in both languages"). One row per (kind, language, version). An operator
writes a draft in the console and publishes it; the public page (`legal/views.py`)
serves the published version with the highest number. A published version is a record
of what people were shown and agreed to, so it never changes and is never deleted: the
next wording is a new version (the console's "start a new draft" copies one). The URLs
reach the app through the capabilities answer (`legal/links.py`), so they can move
without a release.

**Account-deletion requests** (spec B6; Google Play's Data safety form asks for a web
page where someone can request deletion without the app). The page explains how to
delete in the app and takes a request from anyone who cannot: an address and an
optional reason, queued for an operator. Nothing here deletes anything, and nothing the
requester is shown depends on whether an account uses the address. The operator checks
that the requester owns the address, erases the account through the existing erasure,
and records the outcome here. The request is personal data with a retention
(`training_platform/privacy/sources.py`): it goes 180 days after it was handled, and an
erasure of the matching account keeps the record of the request but not the address or
the words.

Console strings (help texts, validation messages) are plain English, like every page of
the console; what the public reads is in the templates and goes through gettext.
"""
from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models, transaction
from django.db.models import Max
from django.utils import timezone

from training_platform.model_validation import RowValidationMixin


class DocumentKind(models.TextChoices):
    PRIVACY = "privacy", "Privacy policy"
    TERMS = "terms", "Terms of use"


class DocumentLanguage(models.TextChoices):
    AR = "ar", "Arabic"
    EN = "en", "English"


#: Arabic first: it is the platform's primary language, and the page's default.
LANGUAGES = (DocumentLanguage.AR, DocumentLanguage.EN)

#: What a published version may no longer change.
FROZEN_ONCE_PUBLISHED = ("kind", "language", "version", "title", "body")


class LegalDocument(RowValidationMixin, models.Model):
    kind = models.CharField(max_length=8, choices=DocumentKind.choices)
    language = models.CharField(max_length=2, choices=DocumentLanguage.choices)
    #: 1, 2, 3 ... per (kind, language), given when the row is created.
    version = models.PositiveIntegerField(default=0, editable=False)
    title = models.CharField(max_length=200)
    body = models.TextField(
        max_length=100_000,
        help_text="Plain text. A blank line starts a paragraph; a line starting with "
                  "'## ' is a heading; lines starting with '- ' are a list. Web and "
                  "email addresses become links. No HTML: it is shown as typed. Every "
                  "build opens this page, the App Store and Google Play builds too: "
                  "write no prices and no links or instructions to pay.")
    is_published = models.BooleanField(
        default=False,
        help_text="Publishing makes this version the one the public page shows, and "
                  "it can no longer be edited. The next wording is a new version.")
    published_at = models.DateTimeField(null=True, blank=True)
    #: The operator who published it. NULL when a migration or a shell did.
    published_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
        related_name="legal_documents_published")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["kind", "language", "-version", "-id"]
        constraints = [
            models.UniqueConstraint(fields=["kind", "language", "version"],
                                    name="uniq_legal_document_version"),
        ]
        verbose_name = "Legal document"
        verbose_name_plural = "Legal documents"

    def __str__(self):
        state = "published" if self.is_published else "draft"
        return f"{self.get_kind_display()} ({self.language}) v{self.version}, {state}"

    @classmethod
    def current(cls, kind: str, language: str):
        """The published version with the highest number, or None."""
        return (cls.objects.filter(kind=kind, language=language, is_published=True)
                .order_by("-version", "-id").first())

    def clean(self):
        if self.pk is None:
            return
        stored = (type(self).objects.filter(pk=self.pk)
                  .values(*FROZEN_ONCE_PUBLISHED, "is_published").first())
        if not stored or not stored["is_published"]:
            return
        errors = {}
        if not self.is_published:
            errors["is_published"] = ("A published version stays published. Publish a "
                                      "new version to replace it.")
        for field in FROZEN_ONCE_PUBLISHED:
            if getattr(self, field) != stored[field]:
                errors.setdefault(field, "A published version cannot be edited. Start a "
                                         "new draft from it instead.")
        if errors:
            raise ValidationError(errors)

    def save(self, *args, published_by=None, **kwargs):
        self.validate_row()
        with transaction.atomic():
            was_published = False
            if self.pk is not None:
                was_published = bool(type(self).objects.select_for_update()
                                     .filter(pk=self.pk)
                                     .values_list("is_published", flat=True).first())
            if self._state.adding and not self.version:
                highest = (type(self).objects.filter(kind=self.kind, language=self.language)
                           .aggregate(top=Max("version"))["top"]) or 0
                self.version = highest + 1
            if self.is_published and not was_published:
                self.published_at = self.published_at or timezone.now()
                self.published_by = published_by or self.published_by
            super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        if self.is_published:
            raise PermissionError("A published legal document is a record and cannot be "
                                  "deleted.")
        return super().delete(*args, **kwargs)

    def new_draft(self):
        """An unpublished copy of this version, numbered after the newest one."""
        return type(self).objects.create(kind=self.kind, language=self.language,
                                         title=self.title, body=self.body)


class DeletionRequestStatus(models.TextChoices):
    OPEN = "open", "Open"
    ERASED = "erased", "Account erased"
    NO_ACCOUNT = "no_account", "No account uses this address"
    DECLINED = "declined", "Declined (ownership not confirmed, or spam)"


#: The longest reason the form accepts.
REASON_MAX_LENGTH = 1000


class AccountDeletionRequest(models.Model):
    """One request made on `/legal/delete-account/`. Written by the page, closed by an
    operator; never tied to an account by a key, because the page must not look one up.
    """
    #: Lower-cased and stripped by the page. Emptied when the matching account is erased.
    email = models.EmailField(max_length=254, blank=True, db_index=True)
    reason = models.TextField(blank=True, max_length=REASON_MAX_LENGTH)
    status = models.CharField(max_length=12, choices=DeletionRequestStatus.choices,
                              default=DeletionRequestStatus.OPEN, db_index=True)
    operator_note = models.TextField(
        blank=True, help_text="For operators only. Never shown to the requester.")
    handled_at = models.DateTimeField(null=True, blank=True)
    handled_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
        related_name="deletion_requests_handled")
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        # The queue: oldest first, so nobody waits behind a newer request.
        ordering = ["created_at", "id"]
        constraints = [
            # One open request per address, so two posts at the same instant cannot
            # queue it twice. An erased requester's rows have no address left and are
            # not held to it.
            models.UniqueConstraint(
                fields=["email"],
                condition=models.Q(status="open") & ~models.Q(email=""),
                name="uniq_open_deletion_request_per_email"),
        ]
        verbose_name = "Account deletion request"
        verbose_name_plural = "Account deletion requests"

    def __str__(self):
        return f"Deletion request #{self.pk} ({self.get_status_display()})"
