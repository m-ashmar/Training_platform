"""Putting a video in a slot.

The image path (`processing.py` + `services.store`) does everything on upload: decode,
grade, resize, encode. Video cannot: the cheapest useful operation on it — one poster
frame — needs ffmpeg, and an upload must never depend on a binary being installed on the
machine that received it. So this module does the part that MUST happen synchronously,
which is the part that protects the platform:

  * validate by content (`training_platform.file_security.process_uploaded_video`) —
    the bytes decide, never the filename or the Content-Type header;
  * store the original under an extension derived from the detected container;
  * mark the asset `pending` and hand the rest to `mediakit.tasks.fill_video_metadata`.

The asset is USABLE the moment this returns: `video_url` plays. The poster, the
dimensions and the duration appear when the worker has them, and if there is no
transcoder anywhere the asset still ends up `ready` — just without a poster.
"""
from __future__ import annotations

from django.core.exceptions import ValidationError
from django.core.files.base import File
from django.db import transaction
from django.utils.translation import gettext as _

from training_platform.file_security import process_uploaded_video

from .models import MediaAsset
from .services import (clean_before_writing, check_alt_text, discard_new_files,
                       owner_key, resolve_owner, stored_names)
from .slots import VIDEO, resolve


def store_video(*, owner_type, owner_id, slot_key, upload, uploaded_by=None,
                alt_ar=None, alt_en=None, person=None) -> MediaAsset:
    """Validate `upload` and put it in a video slot, replacing whatever was there.

    `person` is required for `trainer_exercise` (the trainer whose demonstration this
    is, half of that owner's key) and ignored for every other owner type.

    Raises `django.core.exceptions.ValidationError` carrying a `code`:

    | code | means |
    |---|---|
    | `media_unknown_slot` | no such slot, or not a slot this kind of owner has |
    | `media_unknown_owner` | nothing of that kind has that id |
    | `media_slot_is_not_video` | that slot holds a still image (or an SVG) |
    | `media_not_a_video` | the BYTES are not an MP4 or a WebM, whatever the name says |
    | `media_video_too_large` | over `MEDIA_VIDEO_MAX_BYTES` |
    | `media_video_too_long` | the container declares more than `MEDIA_VIDEO_MAX_SECONDS` |
    | `media_alt_too_long` | `alt_ar` or `alt_en` is longer than the column holds (200) |

    The replaced file is removed once the transaction commits
    (`training_platform/signals.py`), so a rollback never leaves a row without its file.
    The row is checked BEFORE the video is written and the video is deleted again if the
    write fails, so a refusal never leaves up to 64 MB in the bucket that no row points
    at (SIDE_BUGS S-103).
    """
    owner_id, person = resolve_owner(owner_type, owner_id, slot_key, person)
    slot = resolve(slot_key)
    if slot.kind != VIDEO:
        raise ValidationError(_("This slot does not take a video."),
                              code="media_slot_is_not_video")

    check_alt_text(alt_ar, alt_en)
    facts = process_uploaded_video(upload)
    key = owner_key(owner_type, owner_id, slot_key, person)

    asset, before = None, {}
    try:
        with transaction.atomic():
            asset = (MediaAsset.objects.select_for_update().filter(**key).first()
                     or MediaAsset(**key))
            before = stored_names(asset)
            asset.person = person
            asset.uploaded_by = uploaded_by
            if alt_ar is not None:
                asset.alt_ar = alt_ar
            if alt_en is not None:
                asset.alt_en = alt_en

            # A re-upload into a filled slot must not keep the previous video's poster
            # or measurements — they describe bytes that are about to be replaced.
            asset.poster = ""
            asset.hls_url = ""
            asset.width = asset.height = 0
            asset.duration_seconds = facts.duration_seconds
            asset.processing_state = MediaAsset.PENDING
            clean_before_writing(asset)
            # The name comes from the DETECTED container. `_upload_to` ignores
            # everything but the extension, and the client's filename never reaches
            # storage.
            upload.seek(0)
            asset.original.save(f"video.{facts.extension}", File(upload), save=False)
            asset.full_clean()
            asset.save()
    except Exception:
        if asset is not None:
            # `before` holds the poster the row had; it was blanked above, so only a
            # name this call wrote can differ from it — and the old poster stays.
            discard_new_files(asset, before)
        raise

    # After commit: the worker reads the row, and inside the transaction it would find
    # nothing (or, on a rollback, work on a video that no longer exists).
    def _enqueue():
        from .tasks import fill_video_metadata
        fill_video_metadata.delay(asset.pk)

    transaction.on_commit(_enqueue)
    return asset
