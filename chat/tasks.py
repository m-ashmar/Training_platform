"""What a worker does for the chat after a message is stored: a video's poster frame.

Off the request, always. The send stores the video as it arrived and returns; once the
row commits it queues `cut_video_poster` (`chat.media.queue_poster`), and a worker cuts
one JPEG near the start with ffmpeg — through the media kit's own helpers
(`mediakit.tasks`), so the chat and the media kit share one ffmpeg command, one lazy
download and one "ffmpeg is missing" line in the log.

The rule it keeps is the media kit's: a missing transcoder never fails anything. With no
ffmpeg on the worker the message is left as it is (`poster_url` null, the app's own
placeholder), nothing is downloaded, and the worker says so once per process.
"""
from __future__ import annotations

import logging

from celery import shared_task
from django.core.files.base import ContentFile
from django.db import transaction

from mediakit.tasks import TRANSIENT_ERRORS

logger = logging.getLogger(__name__)

#: The longest side of a chat poster, in pixels. The bubble is drawn about 210 x 262
#: points (`screens/05-chat-utility/Chat.png`); 720 covers it at 3x, and a phone's 4K
#: frame would be megabytes of still for a thumbnail.
POSTER_MAX_SIDE = 720


@shared_task(
    name="chat.cut_video_poster",
    autoretry_for=TRANSIENT_ERRORS,
    retry_backoff=True,
    retry_backoff_max=600,
    retry_jitter=True,
    max_retries=3,
)
def cut_video_poster(message_id):
    """Cut and store the poster of one chat video. Idempotent; never raises for a
    video it cannot read.

    Returns `{"message": id, "poster": bool, "state": ...}` where `state` says why no
    poster was written when none was: `missing` (the row is gone), `skipped` (not a
    video, taken back, or it has one already), `no_frame` (no ffmpeg, or ffmpeg could
    not read it), `superseded` (the row changed while the frame was cut).

    The frame is written onto the row as it is when the run ENDS, locked, and only if
    it is still the same untaken-back video with no poster (`_store`). Afterwards the
    two people in the thread hear `message.updated` with the message as it now reads.
    """
    from mediakit import tasks as video

    from .models import KIND_VIDEO, Message

    message = (Message.objects.filter(pk=message_id)
               .only('pk', 'kind', 'attachment', 'poster', 'deleted_at',
                     'conversation_id').first())
    if message is None:
        return {"message": message_id, "poster": False, "state": "missing"}
    if (message.kind != KIND_VIDEO or not message.attachment or message.poster
            or message.deleted_at is not None):
        return {"message": message_id, "poster": False, "state": "skipped"}

    cut_from = message.attachment.name
    source = video.LazyCopy(message.attachment, fetch=video.local_copy_of)
    try:
        jpeg = video._poster_bytes(source, max_side=POSTER_MAX_SIDE)
    finally:
        source.cleanup()
    if not jpeg:
        return {"message": message_id, "poster": False, "state": "no_frame"}
    return _store(message_id, cut_from, jpeg)


def _store(message_id, cut_from, jpeg):
    """Write the frame onto the row as it is NOW, under its lock, then announce it.

    Nothing is written if the row went, was taken back, already has a poster (a
    duplicate run) or no longer holds the video the frame was cut from. If the row
    cannot be saved after the file was written, the file is removed again, so no
    poster sits in the bucket without a row pointing at it.
    """
    from .models import Message

    with transaction.atomic():
        fresh = Message.objects.select_for_update().filter(pk=message_id).first()
        if (fresh is None or fresh.deleted_at is not None or fresh.poster
                or fresh.attachment.name != cut_from):
            return {"message": message_id, "poster": False, "state": "superseded"}
        fresh.poster.save("poster.jpg", ContentFile(jpeg), save=False)
        try:
            fresh.save(update_fields=["poster"])
        except Exception:
            _discard(fresh)
            raise
        transaction.on_commit(lambda: _announce_poster(message_id))
    return {"message": message_id, "poster": True, "state": "ready"}


def _discard(message):
    name = message.poster.name if message.poster else ''
    if not name:
        return
    try:
        message.poster.storage.delete(name)
    except Exception:       # noqa: BLE001
        logger.warning("could not remove an orphaned chat poster %s", name, exc_info=True)


def _announce_poster(message_id):
    """`message.updated` for a poster just stored (`_announce_update`)."""
    _announce_update(message_id, "poster")


def _announce_update(message_id, what):
    """`message.updated` to everyone with the thread open: the message as it now reads,
    the same object REST returns — through `chat.services.frame_payload`, the one
    builder of a socket frame's message, so a program card keeps its card on this frame
    too (BACKEND_BUGS B-150; it was serialized with no reader, and `program` was always
    null here). Serialized without a request, as `message.new` is, so on the filesystem
    backend its URLs are relative to the API host. A message taken back is not
    announced. Never raises: what the worker stored stands whatever the socket layer
    does."""
    try:
        from .events import broadcast
        from .models import Message
        from .services import frame_payload

        message = (Message.objects.select_related('exercise', 'routine')
                   .filter(pk=message_id).first())
        if message is None or message.deleted_at is not None:
            return
        broadcast(message.conversation_id, 'message.updated',
                  {'message': frame_payload(message)})
    except Exception:       # noqa: BLE001
        logger.warning("could not announce the %s of chat message %s", what, message_id,
                       exc_info=True)
