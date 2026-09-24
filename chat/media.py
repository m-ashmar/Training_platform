"""The bytes of a chat photo, voice note or video: checked, named, stored, served.

No pipeline of its own. Each kind goes through the platform's one upload guard for it —
the same functions the media kit's uploads run (`training_platform/file_security.py`):

    image   `process_uploaded_image`   magic bytes, MIME, the 5 MB image cap, pixel and
                                       dimension caps, a decode, and a RE-ENCODE: only
                                       decoded pixels are stored, so the EXIF and GPS a
                                       phone photo carries never reach the other person
    video   `process_uploaded_video`   the container from its own bytes (MP4, MOV,
                                       WebM), `MEDIA_VIDEO_MAX_BYTES` (64 MB) and
                                       `MEDIA_VIDEO_MAX_SECONDS` (90 s) — the media kit's
                                       caps, read the same way
    voice   `process_uploaded_audio`   the audio twin of the video guard (M4A, Ogg,
                                       WebM), under the same two caps

The file is stored on the default storage — the media kit's backend: GCS in production,
the signed filesystem in development and tests — under the extension the guard DETECTED
(`chat.models._attachment_upload_to`), and it is only ever handed out through
`training_platform.media_storage.media_url()`: signed, expiring, and only inside an
answer that the two people in the conversation receive.

A video's poster frame and a WebM's duration need ffmpeg, which the production image does
not ship (`mediakit/tasks.py`). So a chat video has no poster, and `duration_seconds` is
what the container declares or null; the app draws its own placeholder, as the media
kit's own videos do while they are `pending`.
"""
from __future__ import annotations

import io
from dataclasses import dataclass

from django.core.exceptions import ValidationError
from django.core.files.base import File
from django.utils.translation import gettext as _

from .models import KIND_IMAGE, KIND_VIDEO, KIND_VOICE


@dataclass
class Prepared:
    """What the guard made of one upload, ready to be written onto a `Message`."""
    content: File
    extension: str
    duration_seconds: float | None = None
    width: int | None = None
    height: int | None = None


def prepare(kind: str, upload) -> Prepared:
    """Run `upload` through the guard for `kind`. Raises `ValidationError` with a code.

    Nothing is stored here: the caller writes the row and the file together, and
    removes the file again if the row cannot be written.
    """
    from training_platform.file_security import (process_uploaded_audio,
                                                  process_uploaded_image,
                                                  process_uploaded_video)

    if upload is None:
        raise ValidationError(_("Attach the file as `file`."), code="media_file_required")

    if kind == KIND_IMAGE:
        content, extension = process_uploaded_image(upload)
        width, height = _pixel_size(content)
        return Prepared(content=content, extension=extension, width=width, height=height)
    if kind == KIND_VIDEO:
        facts = process_uploaded_video(upload)
    elif kind == KIND_VOICE:
        facts = process_uploaded_audio(upload)
    else:
        raise ValidationError(_("Send a photo, a voice note or a video."),
                              code="invalid_kind")
    upload.seek(0)
    return Prepared(content=File(upload), extension=facts.extension,
                    duration_seconds=facts.duration_seconds)


def _pixel_size(content):
    """The stored photo's width and height, read from its header (no decode)."""
    from PIL import Image

    content.seek(0)
    try:
        with Image.open(io.BytesIO(content.read())) as probe:
            return probe.size
    except Exception:       # noqa: BLE001 — it was just encoded; a size is a courtesy
        return None, None
    finally:
        content.seek(0)


def store(message, prepared: Prepared) -> None:
    """Write the file onto `message` (storage now, the row when the caller saves it)."""
    # The name the client gave the file never reaches storage: only the detected
    # extension does (`_attachment_upload_to` keeps nothing else of it).
    message.attachment.save(f"{message.kind}.{prepared.extension}", prepared.content,
                            save=False)
    message.duration_seconds = prepared.duration_seconds
    message.width, message.height = prepared.width, prepared.height


def discard(message) -> None:
    """Remove a stored file whose row was never written. Never raises."""
    import logging

    name = message.attachment.name if message.attachment else ''
    if not name:
        return
    try:
        message.attachment.storage.delete(name)
    except Exception:       # noqa: BLE001
        logging.getLogger(__name__).warning(
            "could not remove an orphaned chat upload %s", name, exc_info=True)


def payload(message, request=None) -> dict | None:
    """The `media` object of a message: None for text and for anything taken back.

        {"url": "<signed, expires>", "duration_seconds": 18.4 | null,
         "width": 1080 | null, "height": 1350 | null}

    `url` is private and short-lived (`media_url()`: a V4 signature of an hour on GCS, a
    bucketed HMAC the serve view checks on the filesystem). Use it now; never store it.
    """
    if message.kind not in (KIND_IMAGE, KIND_VOICE, KIND_VIDEO):
        return None
    if message.deleted_at is not None or not message.attachment:
        return None
    from training_platform.media_storage import media_url

    return {
        'url': media_url(message.attachment, request),
        'duration_seconds': message.duration_seconds,
        'width': message.width,
        'height': message.height,
    }
