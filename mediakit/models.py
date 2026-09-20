"""One filled media slot: the graded master, its renditions and what the app needs to
draw it (see `mediakit/slots.py` for the slots themselves).

An owner is named by type and id rather than a foreign key, because five kinds of row
own media and one — the app itself — is not a row at all. The one owner that is a
person, a trainer, is ALSO linked through `person`, so the personal-data registry can
find a trainer's photographs and erasure deletes them with the account.
"""
import os
import secrets

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.utils.translation import gettext_lazy as _

from .slots import OwnerType, resolve


def _upload_to(instance, filename):
    # Content-addressed by a random token: a replaced picture gets a new URL, so a
    # stored URL can be cached forever and never shows the old image.
    ext = os.path.splitext(filename)[1].lower() or ".bin"
    return (f"media_kit/{instance.owner_type}/{instance.owner_id}/"
            f"{instance.slot}-{secrets.token_hex(6)}{ext}")


class MediaAsset(models.Model):
    owner_type = models.CharField(max_length=16, choices=OwnerType.CHOICES, db_index=True)
    owner_id = models.PositiveBigIntegerField()
    slot = models.CharField(max_length=40)
    #: The trainer, when the owner is one. Personal data: their photographs.
    person = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, null=True, blank=True,
        related_name="media_assets")

    original = models.FileField(upload_to=_upload_to, max_length=255,
                                help_text="The graded master, full size — or, in a video "
                                          "slot, the video exactly as it was uploaded.")
    # Blank in a video slot: a video has no 1x/2x still. Blank is validation-only, so
    # this widens what `full_clean()` accepts and changes no column.
    file_1x = models.FileField(upload_to=_upload_to, max_length=255, blank=True)
    file_2x = models.FileField(upload_to=_upload_to, max_length=255, blank=True)
    blur = models.TextField(blank=True, default="",
                            help_text="A tiny WebP data URI painted before the image loads.")
    focal_x = models.FloatField(default=0.5, validators=[MinValueValidator(0.0),
                                                         MaxValueValidator(1.0)])
    focal_y = models.FloatField(default=0.5, validators=[MinValueValidator(0.0),
                                                         MaxValueValidator(1.0)])
    brightness = models.FloatField(default=0.0, validators=[MinValueValidator(0.0),
                                                            MaxValueValidator(1.0)])
    width = models.PositiveIntegerField(default=0)
    height = models.PositiveIntegerField(default=0)
    alt_ar = models.CharField(max_length=200, blank=True, default="")
    alt_en = models.CharField(max_length=200, blank=True, default="")

    # --- video (the fields below are empty for every image slot) -------------------
    #: The frame the app paints before the video plays, cut by the metadata task. The
    #: COLUMN is a file, not a URL: a signed URL expires within the hour, so storing one
    #: would hand the app a dead link the moment the row is a day old. The API serves it
    #: as `poster_url`, resolved through `media_url()` at serialization time.
    poster = models.FileField(upload_to=_upload_to, max_length=255, blank=True)
    #: The HLS playlist, once something writes one. Nothing does yet — this pass stores
    #: and serves the original file and leaves transcoding to a later one
    #: (`mediakit/tasks.py`), so this stays empty and the app plays `video_url`.
    hls_url = models.TextField(blank=True, default="")
    duration_seconds = models.FloatField(null=True, blank=True)

    PENDING, READY, FAILED = "pending", "ready", "failed"
    PROCESSING_STATES = [(PENDING, _("Pending")), (READY, _("Ready")), (FAILED, _("Failed"))]
    #: Where the asset is in its pipeline. `ready` is the truthful default rather than a
    #: backfill that invents one: an image IS finished the moment `store()` returns —
    #: every rendition is written inside that call — and only a video starts `pending`,
    #: because its poster and dimensions come from a worker afterwards.
    processing_state = models.CharField(max_length=10, choices=PROCESSING_STATES,
                                        default=READY, db_index=True)

    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="+")
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["owner_type", "owner_id", "slot", "id"]
        verbose_name = "Media slot"
        verbose_name_plural = "Media slots"
        constraints = [
            models.UniqueConstraint(fields=["owner_type", "owner_id", "slot"],
                                    name="one_asset_per_owner_slot"),
            models.CheckConstraint(
                condition=(models.Q(owner_type=OwnerType.TRAINER, person__isnull=False)
                           | (~models.Q(owner_type=OwnerType.TRAINER)
                              & models.Q(person__isnull=True))),
                name="media_person_is_set_exactly_for_trainers"),
        ]
        indexes = [models.Index(fields=["owner_type", "owner_id"], name="media_owner_idx")]

    def __str__(self):
        return f"{self.owner_type}:{self.owner_id} {self.slot}"

    @property
    def kind(self):
        """`image`, `video` or `svg` — whatever the slot says. One source, `slots.py`."""
        slot = resolve(self.slot)
        return slot.kind if slot is not None else None

    def clean(self):
        slot = resolve(self.slot)
        if slot is None or slot.owner_type != self.owner_type:
            raise ValidationError({"slot": _("This slot does not belong to this kind of owner.")})
        if self.owner_type == OwnerType.TRAINER and self.person_id != self.owner_id:
            # Not keyed on `person`: no form edits it, and a form cannot show an error on a
            # field it does not have (Django raises instead).
            raise ValidationError(_("A trainer's media must name that trainer."))
