"""What a worker does to a video after it is stored.

The rule this module is built around: **an upload never fails because a transcoder is
missing.** ffmpeg is not in the image today, the worker machine may be replaced by one
without it, and a coach in Damascus uploading a demonstration clip must not see an error
for either. So every path here ends with the asset `ready` — with a poster and
measurements when the tools are there, without them when they are not — and only a file
the tools ran on and could not read is marked `failed`.

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
from django.db import InterfaceError, OperationalError
from django.utils import timezone

logger = logging.getLogger(__name__)

TRANSIENT_ERRORS = (OperationalError, InterfaceError, ConnectionError, TimeoutError)

#: Seconds either binary may run. A worker slot held forever by a hung ffmpeg is the
#: failure mode this whole module exists to avoid repeating.
PROBE_TIMEOUT = 30
POSTER_TIMEOUT = 60

#: Where in the clip the poster frame is cut from. One second in, not frame zero: the
#: first frame of a phone recording is usually the lens still adjusting.
POSTER_AT_SECONDS = 1

#: How long an asset may sit `pending` before the sweep assumes its task was lost.
STUCK_AFTER_MINUTES = 15


def _tool(name):
    """The binary's path, or None. `shutil.which`, so a test can hide it."""
    return shutil.which(name)


def _local_copy(asset):
    """A path on this machine for the stored original.

    The filesystem backend can answer with `.path`; object storage cannot, so the file
    is streamed to a temporary copy. Either way the caller gets a path and a cleanup
    flag — ffmpeg reads files, not Django storages.
    """
    storage_path = None
    try:
        storage_path = asset.original.path
    except (NotImplementedError, AttributeError, ValueError):
        storage_path = None
    if storage_path and os.path.exists(storage_path):
        return storage_path, False

    suffix = os.path.splitext(asset.original.name or "")[1] or ".mp4"
    handle = tempfile.NamedTemporaryFile(suffix=suffix, delete=False)
    try:
        with asset.original.open("rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                handle.write(chunk)
    finally:
        handle.close()
    return handle.name, True


def _probe(path):
    """`{"duration": float|None, "width": int|None, "height": int|None}` or None.

    None means "ffprobe could not tell us" — a missing binary, a timeout, or output we
    did not understand. It is never an error the caller reports to anyone.
    """
    ffprobe = _tool("ffprobe")
    if not ffprobe:
        return None
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


def _poster_bytes(path, at_seconds=POSTER_AT_SECONDS):
    """One JPEG frame, or None when ffmpeg is absent or cannot produce one."""
    ffmpeg = _tool("ffmpeg")
    if not ffmpeg:
        return None
    out = tempfile.NamedTemporaryFile(suffix=".jpg", delete=False)
    out.close()
    try:
        result = subprocess.run(
            [ffmpeg, "-y", "-ss", str(at_seconds), "-i", path,
             "-frames:v", "1", "-q:v", "3", out.name],
            capture_output=True, timeout=POSTER_TIMEOUT, check=False,
        )
        if result.returncode != 0 or not os.path.getsize(out.name):
            return None
        with open(out.name, "rb") as handle:
            return handle.read()
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
    frame. Without them: nothing is filled, the asset still becomes `ready`, and the app
    plays `video_url` and paints its own placeholder — the visible cost of no
    transcoder, not an upload that failed.

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

    path, temporary = _local_copy(asset)
    try:
        facts = _probe(path)
        if facts is False:
            asset.processing_state = MediaAsset.FAILED
            asset.save(update_fields=["processing_state", "updated_at"])
            logger.warning("video %s is stored but not playable (ffprobe refused it)",
                           asset_id)
            return {"asset": asset_id, "state": asset.processing_state}

        fields = ["processing_state", "updated_at"]
        if facts:
            if facts["duration"]:
                asset.duration_seconds = round(facts["duration"], 3)
                fields.append("duration_seconds")
            if facts["width"] and facts["height"]:
                asset.width, asset.height = int(facts["width"]), int(facts["height"])
                fields += ["width", "height"]

        poster = _poster_bytes(path)
        if poster:
            # `save=False`: one UPDATE at the end, with the state, so a row is never
            # briefly `ready` with no poster or the other way round.
            asset.poster.save("poster.jpg", ContentFile(poster), save=False)
            fields.append("poster")

        asset.processing_state = MediaAsset.READY
        asset.save(update_fields=fields)
        return {"asset": asset_id, "state": asset.processing_state,
                "poster": bool(poster), "probed": bool(facts)}
    finally:
        if temporary:
            try:
                os.unlink(path)
            except OSError:
                logger.debug("could not remove the temporary video copy", exc_info=True)


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
