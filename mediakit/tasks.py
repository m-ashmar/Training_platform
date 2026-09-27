"""What a worker does to a video after it is stored.

The rule this module is built around: **an upload never fails because a transcoder is
missing.** The image installs ffmpeg (Dockerfile, 2026-09-27), but a worker machine may
still run from an image without it, and a coach in Damascus uploading a demonstration
clip must not see an error for that. So every path here ends with the asset `ready` —
with a poster and measurements when the tools are there, without them when they are not
(said ONCE per worker process in the log, `_tool`) — and only a file the tools ran on
and could not read is marked `failed`.

The frame-cutting helpers here are also the chat's (`chat.tasks.cut_video_poster`): one
ffmpeg invocation, one "logs once", one lazy download for both kinds of video.

HLS is deliberately not built here (see `fill_video_metadata`).
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import tempfile
from datetime import timedelta

from celery import shared_task
from django.core.files.base import ContentFile
from django.db import InterfaceError, OperationalError, transaction
from django.utils import timezone

logger = logging.getLogger(__name__)

TRANSIENT_ERRORS = (OperationalError, InterfaceError, ConnectionError, TimeoutError)

#: Seconds either binary may run. A worker slot held forever by a hung ffmpeg is the
#: failure mode this whole module exists to avoid repeating.
PROBE_TIMEOUT = 30
POSTER_TIMEOUT = 60

#: Where in the clip the poster frame is cut from. One second in, not frame zero: the
#: first frame of a phone recording is usually the lens still adjusting. A clip shorter
#: than that has no frame there, so `_poster_bytes` falls back to its first frame.
POSTER_AT_SECONDS = 1

#: How long an asset may sit `pending` before the sweep assumes its task was lost.
STUCK_AFTER_MINUTES = 15

#: The binaries this worker process has already said it does not have.
_MISSING_TOOLS: set = set()


def _tool(name):
    """The binary's path, or None. `shutil.which`, so a test can hide it.

    A missing binary is logged ONCE per worker process, as a warning an operator can
    act on (install ffmpeg on the worker), and then silently: every video that follows
    would say the same thing, and a log line per upload is noise that hides the one
    that matters. Nothing is raised — the video is stored and plays without a poster.
    """
    path = shutil.which(name)
    if path is None and name not in _MISSING_TOOLS:
        _MISSING_TOOLS.add(name)
        logger.warning("%s is not installed on this worker: videos get no poster frame "
                       "and no measured duration or size until it is (Dockerfile "
                       "installs ffmpeg)", name)
    return path


def local_copy_of(field_file):
    """`(path, temporary)`: a path on this machine for a stored file.

    The filesystem backend can answer with `.path`; object storage cannot, so the file
    is streamed to a temporary copy. Either way the caller gets a path and a cleanup
    flag — ffmpeg reads files, not Django storages.
    """
    storage_path = None
    try:
        storage_path = field_file.path
    except (NotImplementedError, AttributeError, ValueError):
        storage_path = None
    if storage_path and os.path.exists(storage_path):
        return storage_path, False

    suffix = os.path.splitext(field_file.name or "")[1] or ".mp4"
    handle = tempfile.NamedTemporaryFile(suffix=suffix, delete=False)
    try:
        with field_file.open("rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                handle.write(chunk)
    finally:
        handle.close()
    return handle.name, True


def _local_copy(asset):
    """A path on this machine for a media-kit asset's stored original."""
    return local_copy_of(asset.original)


class _LazyCopy:
    """The stored original as a local path, fetched the first time a tool needs it.

    The download used to happen before anything asked whether ffprobe or ffmpeg exist,
    and the production image installs neither: every uploaded video — up to 64 MB — was
    pulled from the bucket to the worker, which is billed egress, only for the asset to
    be marked `ready` with nothing filled in, and the hourly sweep did it again for
    anything stuck (SIDE_BUGS S-102). Each tool below finds its binary FIRST and only
    then asks for the path, so with no tools there is no download at all.

    `fetch` turns what it was given into `(path, temporary)`: `_local_copy` for a
    media-kit asset (the default), `local_copy_of` for a chat message's attachment.
    """

    def __init__(self, asset, fetch=None):
        self._asset = asset
        self._fetch = fetch
        self._path = None
        self._temporary = False

    def path(self):
        if self._path is None:
            # `_local_copy` looked up at CALL time, so a test can stand in for it.
            self._path, self._temporary = (self._fetch or _local_copy)(self._asset)
        return self._path

    def cleanup(self):
        if self._temporary and self._path:
            try:
                os.unlink(self._path)
            except OSError:
                logger.debug("could not remove the temporary video copy", exc_info=True)


#: The public name, for the chat's poster task (`chat.tasks`).
LazyCopy = _LazyCopy


def _path_of(source):
    """A plain path is used as given; a `_LazyCopy` is materialised now."""
    return source.path() if isinstance(source, _LazyCopy) else source


def _probe(source):
    """`{"duration": float|None, "width": int|None, "height": int|None}` or None.

    None means "ffprobe could not tell us" — a missing binary, a timeout, or output we
    did not understand. It is never an error the caller reports to anyone.
    """
    ffprobe = _tool("ffprobe")
    if not ffprobe:
        return None
    path = _path_of(source)
    try:
        result = subprocess.run(
            [ffprobe, "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width,height:format=duration",
             "-of", "json", path],
            capture_output=True, timeout=PROBE_TIMEOUT, check=False,
        )
    except (subprocess.SubprocessError, OSError):
        logger.warning("ffprobe could not be run", exc_info=True)
        return None
    if result.returncode != 0:
        return False  # it RAN and refused the file: that is a real failure
    try:
        data = json.loads(result.stdout or b"{}")
    except ValueError:
        return None
    stream = (data.get("streams") or [{}])[0]
    duration = (data.get("format") or {}).get("duration")
    try:
        duration = float(duration) if duration is not None else None
    except (TypeError, ValueError):
        duration = None
    return {"duration": duration,
            "width": stream.get("width"), "height": stream.get("height")}


def _poster_bytes(source, at_seconds=POSTER_AT_SECONDS, max_side=None):
    """One JPEG frame near the start, or None when ffmpeg is absent or cannot produce
    one.

    `at_seconds` in; a clip shorter than that has no frame there (ffmpeg 7 exits with
    an error, older ones write an empty file), so the first frame is cut instead —
    unless the first run TIMED OUT, which a second run would only repeat. `max_side`
    bounds the frame's longer side, keeping its shape (the chat's bubble does not need
    a 4K still). The JPEG carries no metadata (`-map_metadata -1`): nothing of the
    video's own tags — a phone's location among them — is copied into the still.
    """
    ffmpeg = _tool("ffmpeg")
    if not ffmpeg:
        return None
    path = _path_of(source)
    frame = _cut_frame(ffmpeg, path, at_seconds, max_side)
    if frame is None and at_seconds:
        frame = _cut_frame(ffmpeg, path, 0, max_side)
    return frame if frame is not _TIMED_OUT else None


#: `_cut_frame`'s answer when ffmpeg ran out of time: not worth a second run.
_TIMED_OUT = object()


def _cut_frame(ffmpeg, path, at_seconds, max_side):
    """One run of ffmpeg: the frame at `at_seconds` as JPEG bytes, None when there is
    none, `_TIMED_OUT` when ffmpeg did not finish in `POSTER_TIMEOUT`."""
    out = tempfile.NamedTemporaryFile(suffix=".jpg", delete=False)
    out.close()
    command = [ffmpeg, "-y", "-ss", str(at_seconds), "-i", path,
               "-frames:v", "1", "-map_metadata", "-1"]
    if max_side:
        side = int(max_side)
        command += ["-vf", f"scale='min({side},iw)':'min({side},ih)'"
                           ":force_original_aspect_ratio=decrease"]
    command += ["-q:v", "3", out.name]
    try:
        result = subprocess.run(command, capture_output=True, timeout=POSTER_TIMEOUT,
                                check=False)
        if result.returncode != 0 or not os.path.getsize(out.name):
            return None
        with open(out.name, "rb") as handle:
            return handle.read()
    except subprocess.TimeoutExpired:
        logger.warning("ffmpeg did not cut a poster frame within %ss", POSTER_TIMEOUT)
        return _TIMED_OUT
    except (subprocess.SubprocessError, OSError):
        logger.warning("ffmpeg could not cut a poster frame", exc_info=True)
        return None
    finally:
        try:
            os.unlink(out.name)
        except OSError:
            logger.debug("could not remove the temporary poster", exc_info=True)


@shared_task(
    name="mediakit.fill_video_metadata",
    autoretry_for=TRANSIENT_ERRORS,
    retry_backoff=True,
    retry_backoff_max=600,
    retry_jitter=True,
    max_retries=3,
)
def fill_video_metadata(asset_id):
    """Fill a stored video's poster, duration and dimensions; mark it `ready` either way.

    Enqueued by `mediakit.video.store_video` after the transaction commits, and by the
    sweep below for anything that never got picked up.

    With ffprobe/ffmpeg present: `duration_seconds`, `width`, `height` and a poster
    frame. Without them: nothing is filled, NOTHING IS DOWNLOADED (`_LazyCopy`), the
    asset still becomes `ready`, and the app plays `video_url` and paints its own
    placeholder — the visible cost of no transcoder, not an upload that failed.

    The answer is written onto the row as it is when the run ends, and only if it still
    holds the bytes the run measured (`_finish`): a re-upload during the run wins.

    `failed` is reserved for a file ffprobe RAN on and refused. That is the one case
    where the bytes we stored are not playable, and an operator needs to see it.

    **`hls_url` is left empty on purpose.** Filling it means a real transcode —
    360p/540p/720p renditions plus a playlist, written back to the same bucket, by the
    GCP Transcoder API or by ffmpeg on a worker that is allowed to spend minutes of CPU
    (BACKEND_DELTA.md "Media slots"). That job writes the playlist's URL into `hls_url`
    — a URL, because the serializer hands that field to the app unchanged, and an HLS
    player fetches the segments itself with no chance to ask us to sign them. Until it
    exists the serializer omits the key and the app plays `video_url`.
    """
    from .models import MediaAsset

    asset = MediaAsset.objects.filter(pk=asset_id).first()
    if asset is None:
        logger.info("video metadata: asset %s is gone", asset_id)
        return {"asset": asset_id, "state": "missing"}
    if asset.kind != "video" or not asset.original:
        return {"asset": asset_id, "state": asset.processing_state}

    # WHICH bytes this run measures. Everything below takes minutes at worst, and the
    # coach may re-upload the slot meanwhile; the answer is written only if the row
    # still holds these bytes when it is (`_finish`).
    probed_name = asset.original.name
    source = _LazyCopy(asset)
    try:
        facts = _probe(source)
        if facts is False:
            logger.warning("video %s is stored but not playable (ffprobe refused it)",
                           asset_id)
            return _finish(asset_id, probed_name, MediaAsset.FAILED)
        poster = _poster_bytes(source)
        return _finish(asset_id, probed_name, MediaAsset.READY, facts=facts, poster=poster)
    finally:
        source.cleanup()


def _finish(asset_id, probed_name, state, *, facts=None, poster=None):
    """Write one run's answer onto the row — the row as it is NOW, locked.

    The instance loaded at the start of the task is stale by the time the tools are
    done. Saving it wrote its `original` back into the pre_save comparison
    (`training_platform/signals.py` `delete_replaced_files_on_update`), so if the coach
    had re-uploaded the slot in the meantime the NEW video was scheduled for deletion
    while the row kept pointing at it — a video_url that 404s for good, on a row marked
    `ready` with the old video's poster (SIDE_BUGS S-064). So: re-read under
    `select_for_update`, and if the row no longer holds the bytes this run measured,
    write nothing. The re-upload queued its own run (`mediakit.video.store_video`).
    """
    from .models import MediaAsset

    with transaction.atomic():
        fresh = MediaAsset.objects.select_for_update().filter(pk=asset_id).first()
        if fresh is None:
            logger.info("video metadata: asset %s is gone", asset_id)
            return {"asset": asset_id, "state": "missing"}
        if fresh.original.name != probed_name:
            logger.info("video metadata: asset %s was re-uploaded during the run; "
                        "leaving it to its own run", asset_id)
            return {"asset": asset_id, "state": fresh.processing_state,
                    "superseded": True}

        fields = ["processing_state", "updated_at"]
        if state == MediaAsset.READY and facts:
            if facts["duration"]:
                fresh.duration_seconds = round(facts["duration"], 3)
                fields.append("duration_seconds")
            if facts["width"] and facts["height"]:
                fresh.width, fresh.height = int(facts["width"]), int(facts["height"])
                fields += ["width", "height"]
        if state == MediaAsset.READY and poster:
            # `save=False`: one UPDATE at the end, with the state, so a row is never
            # briefly `ready` with no poster or the other way round.
            fresh.poster.save("poster.jpg", ContentFile(poster), save=False)
            fields.append("poster")

        fresh.processing_state = state
        fresh.save(update_fields=fields)
    return {"asset": asset_id, "state": state,
            "poster": bool(poster) and state == MediaAsset.READY, "probed": bool(facts)}


@shared_task(
    name="mediakit.sweep_pending_videos",
    autoretry_for=TRANSIENT_ERRORS,
    retry_backoff=True,
    max_retries=3,
)
def sweep_pending_videos():
    """Finish videos whose metadata task never ran.

    Celery is at-least-once, not exactly-once: a worker that dies between the commit and
    the ack loses the message, and the asset would sit `pending` forever — playable, but
    with no poster and marked as if something were still coming. Anything older than
    `STUCK_AFTER_MINUTES` is processed again; `fill_video_metadata` is idempotent, so a
    duplicate run just rewrites the same values.
    """
    from .models import MediaAsset

    cutoff = timezone.now() - timedelta(minutes=STUCK_AFTER_MINUTES)
    stuck = list(MediaAsset.objects
                 .filter(processing_state=MediaAsset.PENDING, updated_at__lt=cutoff)
                 .values_list("pk", flat=True)[:200])
    for asset_id in stuck:
        fill_video_metadata.delay(asset_id)
    if stuck:
        logger.info("video metadata: re-queued %d stuck asset(s)", len(stuck))
    return {"requeued": len(stuck)}
