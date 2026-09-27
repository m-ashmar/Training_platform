"""The bytes of a chat photo, voice note or video: checked, named, stored, served.

No pipeline of its own. Each kind goes through the platform's one upload guard for it —
the same functions the media kit's uploads run (`training_platform/file_security.py`):

    image   `process_uploaded_image`   the 5 MB image cap FIRST, then magic bytes,
                                       MIME, pixel and dimension caps, a decode, and a
                                       RE-ENCODE: only decoded pixels are stored, so the
                                       EXIF and GPS a phone photo carries never reach
                                       the other person. With `quarantine=False`: a
                                       refused photo is not sampled into `quarantine/`,
                                       where no erasure would reach its EXIF and GPS
    video   `process_uploaded_video`   the container from its own bytes (MP4, MOV,
                                       WebM), `MEDIA_VIDEO_MAX_BYTES` (64 MB) and
                                       `MEDIA_VIDEO_MAX_SECONDS` (90 s) — the media kit's
                                       caps, read the same way
    voice   `process_uploaded_audio`   the audio twin of the video guard (M4A, Ogg,
                                       WebM), `CHAT_VOICE_MAX_BYTES` (10 MB) and
                                       `MEDIA_VIDEO_MAX_SECONDS` (90 s)

Before any of that, `require_media_quota`: a sender's photos, voice notes and videos
over a rolling 24 hours are capped in number and in bytes (`CHAT_MEDIA_DAILY_COUNT`,
`CHAT_MEDIA_DAILY_BYTES`), so one account cannot fill the bucket at the hourly rate
limit. It is decided from the upload's size, before a byte is decoded or stored.

The files leave storage with their rows, but not through a per-row delete receiver —
that would cost Django's fast delete on this table (`chat.models.Message`). The two bulk
paths that delete messages sweep the files instead (`delete_conversation_files`, and
the erasure step in `training_platform/privacy/sources.py`), both through
`delete_files_on_commit`, which hands them to a Celery task once the delete commits.

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
import logging
from dataclasses import dataclass
from datetime import timedelta

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.files.base import File
from django.db import DEFAULT_DB_ALIAS
from django.utils import timezone
from django.utils.translation import gettext as _

from .models import KIND_IMAGE, KIND_VIDEO, KIND_VOICE, MEDIA_KINDS, Message

logger = logging.getLogger(__name__)

#: The window the daily quota is measured over.
QUOTA_WINDOW = timedelta(hours=24)


@dataclass
class Prepared:
    """What the guard made of one upload, ready to be written onto a `Message`."""
    content: File
    extension: str
    duration_seconds: float | None = None
    width: int | None = None
    height: int | None = None
    #: What the stored file will weigh: the re-encode for a photo, the upload itself
    #: for a voice note or a video. Written to `Message.size_bytes`.
    size_bytes: int | None = None


def require_media_quota(sender, upload) -> None:
    """Refuse the upload (429 `media_quota_exceeded`) if it would take `sender` past
    their daily allowance. Reads the upload's SIZE only — nothing is decoded or stored.

    Counted over the rolling 24 hours before now, per SENDER, from their own media
    rows: every photo, voice note and video they sent, taken-back ones included,
    because a tombstone keeps its file. The file about to be sent counts too, by its
    upload size. One indexed aggregate (`chat_msg_media_quota_idx`).

    Not a lock: two uploads racing at the edge can both pass, which overshoots by one
    file. The bound it exists for — one account filling the bucket — holds.

    Returns nothing when there is no upload: `prepare` refuses that as
    `media_file_required` right after.
    """
    from django.db.models import Count, Min, Sum

    from .exceptions import MediaQuotaExceeded

    if upload is None:
        return
    now = timezone.now()
    used = (Message.objects
            .filter(sender=sender, kind__in=MEDIA_KINDS,
                    created_at__gte=now - QUOTA_WINDOW)
            .aggregate(files=Count('id'), size=Sum('size_bytes'),
                       oldest=Min('created_at')))
    files, size = used['files'] or 0, used['size'] or 0
    incoming = int(getattr(upload, 'size', 0) or 0)
    if (files + 1 > settings.CHAT_MEDIA_DAILY_COUNT
            or size + incoming > settings.CHAT_MEDIA_DAILY_BYTES):
        oldest = used['oldest']
        wait = ((oldest + QUOTA_WINDOW - now).total_seconds() if oldest is not None
                else QUOTA_WINDOW.total_seconds())
        raise MediaQuotaExceeded(wait)


def prepare(kind: str, upload) -> Prepared:
    """Run `upload` through the guard for `kind`. Raises `ValidationError` with a code.

    Nothing is stored here: the caller writes the row and the file together, and
    removes the file again if the row cannot be written.
    """
    from training_platform.file_security import (FileSecurityValidator,
                                                  process_uploaded_audio,
                                                  process_uploaded_image,
                                                  process_uploaded_video)

    if upload is None:
        raise ValidationError(_("Attach the file as `file`."), code="media_file_required")

    if kind == KIND_IMAGE:
        # `max_bytes` first, so an oversize photo is refused before the validator reads
        # it; `quarantine=False`, so no refusal keeps a sample of a private photo.
        content, extension = process_uploaded_image(
            upload, max_bytes=FileSecurityValidator.MAX_IMAGE_SIZE, quarantine=False)
        width, height = _pixel_size(content)
        return Prepared(content=content, extension=extension, width=width, height=height,
                        size_bytes=content.size)
    if kind == KIND_VIDEO:
        facts = process_uploaded_video(upload)
    elif kind == KIND_VOICE:
        facts = process_uploaded_audio(upload, max_bytes=settings.CHAT_VOICE_MAX_BYTES)
    else:
        raise ValidationError(_("Send a photo, a voice note or a video."),
                              code="invalid_kind")
    upload.seek(0)
    return Prepared(content=File(upload), extension=facts.extension,
                    duration_seconds=facts.duration_seconds, size_bytes=facts.size)


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
    message.size_bytes = prepared.size_bytes


def discard(message) -> None:
    """Remove a stored file whose row was never written. Never raises."""
    name = message.attachment.name if message.attachment else ''
    if not name:
        return
    try:
        message.attachment.storage.delete(name)
    except Exception:       # noqa: BLE001
        logger.warning("could not remove an orphaned chat upload %s", name, exc_info=True)


# --------------------------------------------------------------------------- deletion
def attachment_names(messages) -> list[str]:
    """The stored file names behind `messages` (a Message queryset), read as one column
    — never as rows, which is the point: the rows then go as a plain DELETE."""
    return list(messages.exclude(attachment='').order_by()
                .values_list('attachment', flat=True))


#: The field whose storage holds a chat file (`training_platform.storage_cleanup`).
CHAT_FILES = "chat.Message.attachment"


def delete_files_on_commit(names, using=DEFAULT_DB_ALIAS) -> None:
    """Delete these chat files once the surrounding transaction COMMITS. On a rollback
    they stay, with the rows that still point at them.

    Through `training_platform.storage_cleanup.delete_after_commit`: after the commit
    the names go to the `delete_stored_files` task, off the request, which retries what
    storage refuses. They used to be deleted one storage call at a time inside the
    request that removed the rows — an erasure of a coach with thousands of photos
    spent that many round trips in the DELETE (the production-readiness audit,
    2026-09-25). Sweeps of one transaction still merge into one callback where a
    rollback cannot tell them apart, so an erasure — whose conversation receiver and
    messages step both reach the person's own files — hands each file over once.
    """
    from training_platform.storage_cleanup import delete_after_commit

    delete_after_commit(CHAT_FILES, names, using=using)


def delete_conversation_files(sender, instance, using=None, **kwargs):
    """`pre_delete` on `Conversation` (connected in `chat.apps.ChatConfig.ready`).

    The conversation's messages cascade as a plain DELETE (Message has no delete
    receiver, so it keeps Django's fast path); their files are read here, as one
    column, while the rows still exist, and deleted once the delete commits. It runs
    however a conversation goes: its own delete, an erasure, the cascade from a deleted
    account.
    """
    using = using or DEFAULT_DB_ALIAS
    delete_files_on_commit(
        attachment_names(Message.objects.using(using).filter(conversation_id=instance.pk)),
        using=using)


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
