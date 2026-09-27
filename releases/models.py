"""Released builds of the app, one row per build, and which one is the newest.

What the in-app update prompt reads (`GET /api/v1/app/latest`, `releases/views.py`) and
what the direct-APK download on our own site links to (spec §5: "a published SHA-256
checksum, a version endpoint ... for in-app update prompts").

**Only the direct APK is a file here.** A Play or App Store build is updated by its
store, never by us: Google Play forbids an app from updating itself any other way, and
the iOS build must not point at anything to install. So a `play` or `ios` row carries a
version, a minimum supported build and, at most, the store's own page (`store_url`), and
the model refuses a file, a download URL or a checksum on it. What a build is TOLD
depends on the build asking (`X-App-Channel`) and on nothing else — rule zero.

**Where an APK lives.** Either uploaded here, into the media storage (`apk_file`: private,
handed out as a signed, expiring URL, `training_platform.media_storage.media_url`), or
hosted elsewhere over HTTPS — the hub domain, a CDN — and named by `download_url`. An
upload's checksum and size are computed by the server; an operator who also typed a
checksum is told when it does not match. A hosted file's checksum and size are what the
operator enters from the build pipeline. The console upload is bounded by
`MAX_REQUEST_BODY_BYTES` like every other request; a larger APK is hosted and named.

**A published release is a record.** Users have downloaded it and checked its checksum,
so once a row has been published its channel, version, build, file, URL, checksum and
size never change: a correction is a new build. What stays editable is what describes
the rollout — the release notes, the store page, the minimum supported build (raising
it later is how a security fix becomes a required update) and `is_published` itself
(withdrawing a bad build hands "latest" back to the one before it).

Console strings are plain English, like every other page of the console.
"""
from __future__ import annotations

import hashlib
import re
import uuid

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models, transaction
from django.utils import timezone

from policy.channels import Channel, parse_version
from training_platform.model_validation import RowValidationMixin


class ReleaseChannel(models.TextChoices):
    """The builds that are released. `web` is the hub, which is a site, not a build."""
    APK = Channel.APK.value, "Direct APK"
    PLAY = Channel.PLAY.value, "Google Play"
    IOS = Channel.IOS.value, "App Store"


#: The one channel whose release is a file this platform hands out.
DOWNLOAD_CHANNEL = ReleaseChannel.APK

#: Every APK is a ZIP archive, and every ZIP archive starts with these four bytes.
ZIP_MAGIC = b"PK\x03\x04"

_SHA256 = re.compile(r"^[0-9a-f]{64}$")

#: What a release that has ever been published may no longer change (see the module
#: docstring). `apk_file` is checked by its stored name, beside these.
FROZEN_ONCE_PUBLISHED = ("channel", "version_name", "build_number", "download_url",
                         "sha256", "size_bytes")


def apk_upload_to(instance, filename):
    """`app-releases/apk/<random>/yalla-gym-1.4.0-27.apk`.

    The random directory keeps two uploads of one build apart and the name unguessable;
    the file name is what the person's phone saves, so it says what it is."""
    version = re.sub(r"[^0-9.]", "", instance.version_name or "") or "0"
    return (f"app-releases/{instance.channel}/{uuid.uuid4().hex}/"
            f"yalla-gym-{version}-{int(instance.build_number or 0)}.apk")


def _is_https(url: str) -> bool:
    return url.lower().startswith("https://")


class AppRelease(RowValidationMixin, models.Model):
    channel = models.CharField(max_length=8, choices=ReleaseChannel.choices)
    version_name = models.CharField(
        max_length=17, help_text="What the user sees, like 1.4.0 (up to three numbers).")
    build_number = models.PositiveIntegerField(
        help_text="The build's own number (Android versionCode, iOS build). One per "
                  "channel; the highest published one is the latest.")
    min_supported_build = models.PositiveIntegerField(
        default=0,
        help_text="Builds below this number are told an update is REQUIRED. 0 means "
                  "no build is. Read from the latest published release.")
    release_notes_ar = models.TextField(blank=True, max_length=2000)
    release_notes_en = models.TextField(blank=True, max_length=2000)

    # --- the direct APK only -------------------------------------------------------
    apk_file = models.FileField(
        upload_to=apk_upload_to, blank=True, max_length=255,
        help_text="Upload the APK here (checksum and size are computed), OR host it "
                  "and fill in the download URL below — not both.")
    download_url = models.URLField(
        max_length=500, blank=True,
        help_text="HTTPS address of an APK hosted elsewhere (the hub domain, a CDN).")
    sha256 = models.CharField(
        max_length=64, blank=True,
        help_text="SHA-256 of the APK, 64 hex characters. Computed for an upload; "
                  "required for a hosted file.")
    size_bytes = models.PositiveBigIntegerField(
        null=True, blank=True,
        help_text="Size of the APK in bytes. Computed for an upload; required for a "
                  "hosted file.")

    # --- the store builds only -----------------------------------------------------
    store_url = models.URLField(
        max_length=500, blank=True,
        help_text="Play or App Store build only: the store's own page for the app, "
                  "which the update prompt opens.")

    is_published = models.BooleanField(
        default=False,
        help_text="Off until the build is really available. Unticking a published "
                  "release withdraws it: the previous one becomes the latest again.")
    published_at = models.DateTimeField(null=True, blank=True)
    #: The operator who last published it. NULL when it came from a migration or shell.
    published_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
        related_name="app_releases_published")

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["channel", "-build_number", "-id"]
        constraints = [
            models.UniqueConstraint(fields=["channel", "build_number"],
                                    name="uniq_app_release_channel_build"),
        ]
        indexes = [
            models.Index(fields=["channel", "is_published", "-build_number"],
                         name="app_release_latest_idx"),
        ]
        verbose_name = "App release"
        verbose_name_plural = "App releases"

    def __str__(self):
        return f"{self.channel} {self.version_name} ({self.build_number})"

    # ------------------------------------------------------------------ reading
    @classmethod
    def latest(cls, channel: str):
        """The newest published release of a channel, or None."""
        return (cls.objects.filter(channel=channel, is_published=True)
                .order_by("-build_number", "-id").first())

    @property
    def is_download(self) -> bool:
        return self.channel == DOWNLOAD_CHANNEL

    # ------------------------------------------------------------------ validation
    def _stored(self):
        if self.pk is None:
            return None
        return (type(self).objects.filter(pk=self.pk)
                .values(*FROZEN_ONCE_PUBLISHED, "apk_file", "published_at").first())

    def _uploaded(self):
        """The file just assigned to `apk_file` and not yet written to storage, or None."""
        if self.apk_file and not getattr(self.apk_file, "_committed", True):
            return self.apk_file.file
        return None

    def _digest_upload(self, upload) -> tuple[str, int]:
        """`(sha256, size)` of an upload, read once per upload however often clean runs."""
        cached = getattr(self, "_upload_digest", None)
        if cached and cached[0] is upload:
            return cached[1], cached[2]
        digest, size = hashlib.sha256(), 0
        for chunk in upload.chunks():
            digest.update(chunk)
            size += len(chunk)
        upload.seek(0)
        self._upload_digest = (upload, digest.hexdigest(), size)
        return self._upload_digest[1], self._upload_digest[2]

    def clean(self):
        errors: dict[str, str] = {}

        parsed = parse_version(self.version_name)
        if parsed is None:
            errors["version_name"] = "Use up to three dotted numbers, like 1.4.0"
        else:
            self.version_name = ".".join(str(p) for p in parsed)

        if (self.build_number is not None and self.min_supported_build is not None
                and self.min_supported_build > self.build_number):
            errors["min_supported_build"] = (
                "The minimum supported build cannot be above this release's own build.")

        self.sha256 = (self.sha256 or "").strip().lower()
        self.download_url = (self.download_url or "").strip()
        self.store_url = (self.store_url or "").strip()
        for field in ("download_url", "store_url"):
            value = getattr(self, field)
            if value and not _is_https(value):
                errors[field] = "Use an https:// address."

        upload = self._uploaded()
        if self.channel == DOWNLOAD_CHANNEL:
            if self.store_url:
                errors["store_url"] = "The direct APK has no store page."
            if self.apk_file and self.download_url:
                errors["download_url"] = ("Upload a file OR name where it is hosted, "
                                          "not both.")
            if upload is not None:
                name = (getattr(upload, "name", "") or "").lower()
                head = upload.read(len(ZIP_MAGIC))
                upload.seek(0)
                if not name.endswith(".apk") or head != ZIP_MAGIC:
                    errors["apk_file"] = "This is not an APK file."
                else:
                    sha, size = self._digest_upload(upload)
                    if self.sha256 and self.sha256 != sha:
                        errors["sha256"] = (
                            "This does not match the uploaded file, whose SHA-256 is "
                            f"{sha}. Check that the right APK was uploaded.")
                    else:
                        self.sha256, self.size_bytes = sha, size
            if self.sha256 and not _SHA256.match(self.sha256):
                errors["sha256"] = "A SHA-256 checksum is 64 hexadecimal characters."
            if self.is_published:
                if not self.apk_file and not self.download_url:
                    errors.setdefault("download_url", (
                        "A published APK needs a file: upload it or name where it is "
                        "hosted."))
                if not self.sha256:
                    errors.setdefault("sha256", "A published APK needs its checksum.")
                if not self.size_bytes:
                    errors.setdefault("size_bytes", "A published APK needs its size.")
        else:
            for field in ("apk_file", "download_url", "sha256", "size_bytes"):
                if getattr(self, field):
                    errors[field] = ("Only the direct APK is a file here. A store build "
                                     "is updated by its store.")

        stored = self._stored()
        if stored and stored["published_at"] is not None:
            for field in FROZEN_ONCE_PUBLISHED:
                if getattr(self, field) != stored[field]:
                    errors.setdefault(field, (
                        "This release has been published, so this can no longer "
                        "change. Publish a new build instead."))
            if upload is not None or (self.apk_file.name or "") != (stored["apk_file"] or ""):
                errors.setdefault("apk_file", (
                    "This release has been published, so its file can no longer "
                    "change. Publish a new build instead."))

        if errors:
            raise ValidationError(errors)

    # ------------------------------------------------------------------ writing
    def save(self, *args, published_by=None, **kwargs):
        """Validate, stamp the publication, write. `published_by` is the operator."""
        self.validate_row()
        with transaction.atomic():
            was_published = False
            if self.pk is not None:
                was_published = bool(type(self).objects.select_for_update()
                                     .filter(pk=self.pk)
                                     .values_list("is_published", flat=True).first())
            if self.is_published and not was_published:
                self.published_at = timezone.now()
                self.published_by = published_by
            super().save(*args, **kwargs)
