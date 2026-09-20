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

from .slots import OwnerType, resolve, slot_accepts


def _upload_to(instance, filename):
    # Content-addressed by a random token: a replaced picture gets a new URL, so a
    # stored URL can be cached forever and never shows the old image.
    ext = os.path.splitext(filename)[1].lower() or ".bin"
    return (f"media_kit/{instance.owner_type}/{instance.owner_id}/"
            f"{instance.slot}-{secrets.token_hex(6)}{ext}")


class MediaAsset(models.Model):
    owner_type = models.CharField(max_length=16, choices=OwnerType.CHOICES, db_index=True)
    #: The row the media hangs on: the trainer, the exercise, the program, the dish —
    #: or, for `trainer_exercise`, the EXERCISE, with the trainer in `person`. Two ids
    #: are needed there and a generic owner has room for one, so the person column
    #: carries the second half of the key.
    owner_id = models.PositiveBigIntegerField()
    slot = models.CharField(max_length=40)
    #: The person this row belongs to: the trainer themselves (`trainer`), or the
    #: trainer who shot this demonstration of a shared exercise (`trainer_exercise`).
    #: Personal data either way, and the registry finds a trainer's media through it.
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
            # One row per slot, for every owner that IS one row. `trainer_exercise` is
            # two rows — a trainer AND an exercise — so it is carved out here and gets
            # its own key below; without the carve-out the first coach to film the
            # bench press would own `exercise.loop` for that exercise and no other
            # coach could ever add theirs.
            models.UniqueConstraint(
                fields=["owner_type", "owner_id", "slot"],
                condition=~models.Q(owner_type=OwnerType.TRAINER_EXERCISE),
                name="one_asset_per_owner_slot"),
            models.UniqueConstraint(
                fields=["owner_type", "owner_id", "person", "slot"],
                condition=models.Q(owner_type=OwnerType.TRAINER_EXERCISE),
                name="one_trainer_asset_per_exercise_slot"),
            # `person` is set for exactly the owner types that belong to a person, and
            # for no others. This is what the privacy registry leans on: it finds a
            # trainer's media through `person`, so a person-owned row that left the
            # column null would survive an erasure. Widened from `trainer` alone
            # rather than dropped — the rule is the same, the set of types grew.
            models.CheckConstraint(
                condition=(models.Q(owner_type__in=sorted(OwnerType.PERSON_OWNED),
                                    person__isnull=False)
                           | (~models.Q(owner_type__in=sorted(OwnerType.PERSON_OWNED))
                              & models.Q(person__isnull=True))),
                name="media_person_is_set_exactly_for_people"),
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
        if not slot_accepts(slot, self.owner_type):
            raise ValidationError({"slot": _("This slot does not belong to this kind of owner.")})
        if self.owner_type == OwnerType.TRAINER and self.person_id != self.owner_id:
            # Not keyed on `person`: no form edits it, and a form cannot show an error on a
            # field it does not have (Django raises instead).
            raise ValidationError(_("A trainer's media must name that trainer."))
        if self.owner_type == OwnerType.TRAINER_EXERCISE and self.person_id is None:
            # Here `owner_id` is the EXERCISE, so the trainer is only in `person` and
            # the equality check above cannot stand in for this one. A row without it
            # would be a demonstration belonging to nobody — invisible to the
            # resolution rule and unreachable by erasure.
            raise ValidationError(_("A trainer's demonstration must name that trainer."))
