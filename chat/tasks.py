"""What a worker does for the chat after a message is stored: a video's poster frame,
and the length of a voice note or video whose container declared none.

Off the request, always. The send stores the file as it arrived and returns; once the
row commits it queues `cut_video_poster` for a video (`chat.media.queue_poster`), and a
worker cuts one JPEG near the start with ffmpeg — through the media kit's own helpers
(`mediakit.tasks`), so the chat and the media kit share one ffmpeg command, one lazy
download and one "ffmpeg is missing" line in the log. A voice note or video whose
container declared no length (a WebM a browser recorded live) is queued for
`fill_media_duration` the same way (`chat.media.queue_duration`, BACKEND_BUGS B-177),
which measures it with ffprobe.

The rule it keeps is the media kit's: a missing transcoder never fails anything. With no
ffmpeg or ffprobe on the worker the message is left as it is (`poster_url` and
`duration_seconds` null, the app's own placeholder), nothing is downloaded, and the
worker says so once per process.
"""
from __future__ import annotations

import logging
import math
import subprocess
import threading

from celery import shared_task
from django.core.files.base import ContentFile
from django.db import transaction

from mediakit.tasks import PROBE_TIMEOUT, TRANSIENT_ERRORS

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


@shared_task(
    name="chat.fill_media_duration",
    autoretry_for=TRANSIENT_ERRORS,
    retry_backoff=True,
    retry_backoff_max=600,
    retry_jitter=True,
    max_retries=3,
)
def fill_media_duration(message_id):
    """Measure and store the length of one voice note or video whose container declared
    none (BACKEND_BUGS B-177: a WebM a browser recorded live declares no Duration).
    Idempotent; never raises for a file it cannot read.

    Returns `{"message": id, "duration": seconds | None, "state": ...}` where `state`
    is `ready`, or says why nothing was written: `missing` (the row is gone), `skipped`
    (not a voice note or a video, taken back, or its length is known already),
    `unmeasured` (no ffprobe on the worker — and then nothing was downloaded — or
    ffprobe could not tell), `superseded` (the row changed while it was measured).

    Measured with ffprobe off a lazy local copy (`_measured_seconds`), and written onto
    the row as it is when the run ENDS, locked, and only if it is still the same
    untaken-back file with no length (`_store_duration`). Then the two people in the
    thread hear `message.updated` with the message as it now reads, and the client's
    home card carries the length (the save retires their cached home).

    A length measured past `MEDIA_VIDEO_MAX_SECONDS` is stored as measured: the message
    was delivered when it was sent, and a cap the container gave no way to read at the
    door is not enforced after it.
    """
    from mediakit import tasks as video

    from .models import KIND_VIDEO, KIND_VOICE, Message

    message = (Message.objects.filter(pk=message_id)
               .only('pk', 'kind', 'attachment', 'duration_seconds', 'deleted_at',
                     'conversation_id').first())
    if message is None:
        return {"message": message_id, "duration": None, "state": "missing"}
    if (message.kind not in (KIND_VOICE, KIND_VIDEO) or not message.attachment
            or message.duration_seconds is not None or message.deleted_at is not None):
        return {"message": message_id, "duration": None, "state": "skipped"}

    measured_from = message.attachment.name
    source = video.LazyCopy(message.attachment, fetch=video.local_copy_of)
    try:
        seconds = _measured_seconds(source)
    finally:
        source.cleanup()
    if not seconds:
        return {"message": message_id, "duration": None, "state": "unmeasured"}
    return _store_duration(message_id, measured_from, seconds)


def _measured_seconds(source):
    """The recording's length as ffprobe measures it, in seconds, or None — no ffprobe
    (said once per worker process, `mediakit.tasks._tool`, and then nothing is
    downloaded), a timeout, or nothing it could tell.

    `format=duration` first (`mediakit.tasks._probe`): what the container declares, or
    what ffprobe works out from its timestamps. A WebM written live has neither — it
    answers `N/A` — so then the end of the last packet is read (`_last_packet_end`): a
    demux of the packets' timestamps, never a decode.
    """
    from mediakit import tasks as video

    ffprobe = video._tool("ffprobe")
    if not ffprobe:
        return None
    facts = video._probe(source)
    declared = facts.get("duration") if facts else None
    if declared and math.isfinite(declared) and declared > 0:
        return round(declared, 3)
    return _last_packet_end(ffprobe, video._path_of(source))


def _last_packet_end(ffprobe, path):
    """Where the file's last packet ends, less where its first one starts, in seconds;
    None when ffprobe read no timestamp or ran out of time.

    `packet=pts_time,duration_time`, one line per packet, read as it is written: a long
    file costs time, bounded by `PROBE_TIMEOUT` (ffprobe is killed then, and a length
    read only part of the way is not stored), never memory.
    """
    command = [ffprobe, "-v", "error", "-show_entries", "packet=pts_time,duration_time",
               "-of", "csv=p=0", path]
    try:
        process = subprocess.Popen(command, stdout=subprocess.PIPE,
                                   stderr=subprocess.DEVNULL)
    except (OSError, ValueError, subprocess.SubprocessError):
        logger.warning("ffprobe could not be run", exc_info=True)
        return None
    timed_out = threading.Event()

    def _stop():
        timed_out.set()
        process.kill()

    timer = threading.Timer(PROBE_TIMEOUT, _stop)
    timer.start()
    first = last = None
    try:
        for line in process.stdout:
            fields = line.split(b",")
            try:
                start = float(fields[0])
                length = (float(fields[1]) if len(fields) > 1 and fields[1].strip()
                          else 0.0)
            except ValueError:
                continue
            if not (math.isfinite(start) and math.isfinite(length)):
                continue
            end = start + max(length, 0.0)
            first = start if first is None else min(first, start)
            last = end if last is None else max(last, end)
    finally:
        timer.cancel()
        process.stdout.close()
        process.wait()
    if timed_out.is_set():
        logger.warning("ffprobe did not read a chat recording's packets within %ss",
                       PROBE_TIMEOUT)
        return None
    if last is None:
        return None
    seconds = last - max(first, 0.0)
    return round(seconds, 3) if seconds > 0 else None


def _store_duration(message_id, measured_from, seconds):
    """Write the length onto the row as it is NOW, under its lock, then announce it.

    Nothing is written if the row went, was taken back, has a length already (a
    duplicate run) or no longer holds the file that was measured."""
    from .models import Message

    with transaction.atomic():
        fresh = Message.objects.select_for_update().filter(pk=message_id).first()
        if (fresh is None or fresh.deleted_at is not None
                or fresh.duration_seconds is not None
                or fresh.attachment.name != measured_from):
            return {"message": message_id, "duration": None, "state": "superseded"}
        fresh.duration_seconds = seconds
        fresh.save(update_fields=["duration_seconds"])
        transaction.on_commit(lambda: _announce_update(message_id, "length"))
    return {"message": message_id, "duration": seconds, "state": "ready"}


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
