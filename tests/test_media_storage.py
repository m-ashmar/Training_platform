"""Media in production: object storage, signed URLs, and video in a slot.

Three things were true before this file existed, all of them only visible once the app
was deployed:

  * there was no object storage at all — uploads went to a 1 GB Fly volume that attaches
    to ONE machine, so a picture stored through machine A was a 404 through machine B;
  * nothing could be written to that volume anyway: it is mounted over /data at run
    time, root-owned, while the process runs as appuser (see `docker-entrypoint.sh`);
  * the product's core — a coach's exercise library — is images AND VIDEO, and video
    had no path in at all.

Nothing here touches the network. The GCS backend is chosen by `MEDIA_BACKEND`, and
these tests run on the filesystem backend exactly as a developer's machine does; the one
test that needs production settings runs them in a subprocess with a controlled
environment, and it never opens a socket either.
"""
import io
import json
import os
import re
import secrets
import struct
import subprocess
import sys
import time
from pathlib import Path

import pytest
from cryptography.fernet import Fernet
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from PIL import Image

pytestmark = pytest.mark.django_db

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def media_root(settings, tmp_path):
    """Uploads land in a temporary directory, not in the repository's media/."""
    settings.MEDIA_ROOT = str(tmp_path)
    return tmp_path


# ---------------------------------------------------------------------------
# Builders. Every "video" here is bytes we assembled ourselves: the point of the
# validator is that it reads the BYTES, so a test that handed it a real .mp4 from disk
# would prove less, not more.
# ---------------------------------------------------------------------------

def _box(kind: bytes, payload: bytes) -> bytes:
    return struct.pack(">I", len(payload) + 8) + kind + payload


def _mvhd(duration_seconds: float, timescale: int = 1000) -> bytes:
    """A version-0 movie header declaring a duration. 100 bytes of payload."""
    body = (
        b"\x00\x00\x00\x00"                                   # version 0 + flags
        + struct.pack(">I", 0)                                # creation time
        + struct.pack(">I", 0)                                # modification time
        + struct.pack(">I", timescale)
        + struct.pack(">I", int(duration_seconds * timescale))
        + b"\x00\x01\x00\x00"                                 # rate 1.0
        + b"\x01\x00"                                         # volume 1.0
        + b"\x00" * 10                                        # reserved
        + b"\x00" * 36                                        # matrix
        + b"\x00" * 24                                        # predefined
        + struct.pack(">I", 2)                                # next track id
    )
    return _box(b"mvhd", body)


def mp4_bytes(duration_seconds=4.0, filler=2048, brand=b"isom") -> bytes:
    """A structurally real MP4 head: `ftyp`, a `moov/mvhd` that declares a duration,
    and some `mdat` filler. Not decodable — no track data — which is exactly right for
    testing the guard, and the metadata task is exercised with its tools stubbed."""
    ftyp = _box(b"ftyp", brand + struct.pack(">I", 512) + b"isom" + b"mp42")
    moov = _box(b"moov", _mvhd(duration_seconds))
    mdat = _box(b"mdat", b"\x00" * filler)
    return ftyp + moov + mdat


def webm_bytes(filler=2048) -> bytes:
    return b"\x1a\x45\xdf\xa3" + b"\x01" * 28 + b"\x00" * filler


def upload(content: bytes, name="clip.mp4", content_type="video/mp4"):
    return SimpleUploadedFile(name, content, content_type=content_type)


def image_bytes(w=1280, h=720, fmt="JPEG") -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (w, h), (200, 60, 30)).save(buf, format=fmt)
    return buf.getvalue()


def image_upload(w=1280, h=720):
    return SimpleUploadedFile("shot.jpg", image_bytes(w, h), content_type="image/jpeg")


# ===========================================================================
# 1. The bytes decide. Not the filename, not the Content-Type.
# ===========================================================================

@pytest.mark.parametrize("name,content_type", [
    ("clip.mp4", "video/mp4"),            # every label a real client would send
    ("clip.mov", "video/quicktime"),
    ("clip.webm", "video/webm"),
    ("clip.mp4", "application/octet-stream"),
    ("clip.MP4", "video/mp4"),
])
def test_a_file_that_is_not_a_video_is_refused_however_it_is_labelled(name, content_type):
    """The whole point of validating by content. A PHP payload, an HTML page or a JPEG
    renamed `.mp4` and sent with `Content-Type: video/mp4` was the shape of the image
    hole this platform already closed once (B-002); video must not reopen it."""
    from training_platform.file_security import process_uploaded_video

    for payload in (b"<?php system($_GET['c']); ?>" + b"A" * 4096,
                    b"<html><body>not a video</body></html>" + b"B" * 4096,
                    image_bytes(),
                    b"MZ\x90\x00" + b"\x00" * 4096,
                    b"RIFF" + b"\x00" * 4 + b"AVI LIST" + b"\x00" * 4096):
        with pytest.raises(ValidationError) as refused:
            process_uploaded_video(upload(payload, name, content_type))
        assert refused.value.code == "media_not_a_video", payload[:8]


def test_a_real_container_is_accepted_and_its_extension_comes_from_the_bytes():
    """The stored extension is derived from the detected container, never from the
    client's filename — the rule that keeps a `.php` out of storage."""
    from training_platform.file_security import process_uploaded_video

    facts = process_uploaded_video(upload(mp4_bytes(3.0), name="anything.php"))
    assert (facts.container, facts.extension) == ("iso", "mp4")
    assert facts.duration_seconds == pytest.approx(3.0)

    quicktime = process_uploaded_video(upload(mp4_bytes(1.0, brand=b"qt  "), name="x.txt"))
    assert quicktime.extension == "mov"

    matroska = process_uploaded_video(upload(webm_bytes(), name="x.mp4"))
    assert (matroska.container, matroska.extension) == ("matroska", "webm")
    # WebM keeps its duration where reading it means parsing EBML; unknown is honest.
    assert matroska.duration_seconds is None


def test_an_iso_file_that_is_not_a_video_is_still_refused():
    """`ftyp` alone is not enough: JPEG 2000 and HEIF are ISO BMFF too."""
    from training_platform.file_security import process_uploaded_video

    heif = b"\x00\x00\x00\x18" + b"ftyp" + b"heic" + b"\x00" * 4096
    with pytest.raises(ValidationError) as refused:
        process_uploaded_video(upload(heif))
    assert refused.value.code == "media_not_a_video"


# ===========================================================================
# 2. Size and duration
# ===========================================================================

def test_an_oversized_video_is_refused(settings):
    from training_platform.file_security import process_uploaded_video

    settings.MEDIA_VIDEO_MAX_BYTES = 64 * 1024
    with pytest.raises(ValidationError) as refused:
        process_uploaded_video(upload(mp4_bytes(2.0, filler=200 * 1024)))
    assert refused.value.code == "media_video_too_large"

    # And the cap is on the FILE, so the one just under it passes.
    assert process_uploaded_video(upload(mp4_bytes(2.0, filler=1024))).size < 64 * 1024


def test_an_oversized_body_never_reaches_the_view():
    """The size check inside the view only runs once Django has spooled the whole body
    to FILE_UPLOAD_TEMP_DIR — on the deploy, the same 1 GB volume as all user media. The
    middleware refuses it on Content-Length first. The video route has its own, larger
    ceiling; it is a ceiling, not an exemption."""
    from django.conf import settings as live
    from django.test import RequestFactory

    from training_platform.middleware import RequestSizeLimitMiddleware

    middleware = RequestSizeLimitMiddleware(lambda request: "reached the view")
    cap = live.LARGE_UPLOAD_PATHS["/api/v1/content/media/video/"]

    request = RequestFactory().post("/api/v1/content/media/video/")
    request.META["CONTENT_LENGTH"] = str(cap + 1)
    refused = middleware(request)
    assert refused.status_code == 413
    assert json.loads(refused.content)["code"] == "payload_too_large"

    # A video-sized body on any OTHER route is still refused at 15 MB: the bigger
    # ceiling belongs to one exact path, never to a prefix.
    elsewhere = RequestFactory().post("/api/auth/user/profile-picture/")
    elsewhere.META["CONTENT_LENGTH"] = str(live.MAX_REQUEST_BODY_BYTES + 1)
    assert getattr(middleware(elsewhere), "status_code", None) == 413

    allowed = RequestFactory().post("/api/v1/content/media/video/")
    allowed.META["CONTENT_LENGTH"] = str(live.MAX_REQUEST_BODY_BYTES + 1)
    assert middleware(allowed) == "reached the view"


def test_a_video_longer_than_the_cap_is_refused(settings):
    """A slot video is a demonstration loop, not a lesson. The duration is read out of
    the container's own header — no decoding, no ffmpeg — so it is refused at the door
    rather than after the bytes are stored."""
    from training_platform.file_security import process_uploaded_video

    settings.MEDIA_VIDEO_MAX_SECONDS = 10
    with pytest.raises(ValidationError) as refused:
        process_uploaded_video(upload(mp4_bytes(45.0)))
    assert refused.value.code == "media_video_too_long"

    assert process_uploaded_video(upload(mp4_bytes(9.0))).duration_seconds == 9.0


def test_a_crafted_header_cannot_hang_the_parser():
    """A box that declares its own length as 0 or as something absurd is a loop or an
    OOM if the walker trusts it."""
    from training_platform.file_security import probe_declared_duration

    for header in (b"\x00\x00\x00\x00" + b"moov" + b"\x00" * 64,
                   b"\xff\xff\xff\xff" + b"moov" + b"\x00" * 64,
                   b"\x00\x00\x00\x02" + b"moov" + b"\x00" * 64):
        assert probe_declared_duration(io.BytesIO(header), "iso") is None


# ===========================================================================
# 3. Signed URLs: short-lived, and not guessable
# ===========================================================================

def test_a_media_url_is_signed_unguessable_and_expires(make_user, api, settings, monkeypatch):
    """Media is served without per-request auth — the mobile client cannot attach a JWT
    to an image load — so the URL itself is the credential. It has to be unforgeable and
    it has to die."""
    from mediakit.services import store
    from training_platform import media_storage

    settings.MEDIA_URL_SIGNING = True
    settings.MEDIA_URL_TTL = 300

    trainer = make_user("mstore_sign_t", user_type="trainer")
    asset = store(owner_type="trainer", owner_id=trainer.pk,
                  slot_key="trainer.portrait", upload=image_upload(1000, 1250))

    url = media_storage.media_url(asset.file_1x)
    path, _sep, query = url.partition("?")
    assert query.startswith("s="), "an unsigned media URL is a permanent public link"

    visitor = api()
    assert visitor.get(url).status_code == 200

    # Not guessable: the path alone, a made-up token, and a token for a DIFFERENT file
    # are all refused.
    assert visitor.get(path).status_code == 404
    assert visitor.get(f"{path}?s=deadbeefdeadbeef").status_code == 404
    other = media_storage.media_url(asset.file_2x).partition("?")[2]
    assert visitor.get(f"{path}?{other}").status_code == 404

    # And it expires. Two windows on, the signature the API handed out is dead — the
    # previous window is honoured so a URL issued in the last second of one still works.
    clock = time.time() + settings.MEDIA_URL_TTL * 3
    monkeypatch.setattr(media_storage.time, "time", lambda: clock)
    assert visitor.get(url).status_code == 404
    assert visitor.get(media_storage.media_url(asset.file_1x)).status_code == 200


def test_only_the_apps_own_pictures_may_ever_be_public(make_user, settings, api):
    """The intro story is the same picture for every visitor and is fetched before
    sign-in, so an expiring signature on it only defeats caching. A person's media is
    never public, whatever the flag says — and on the filesystem backend, which has no
    public URL to give, the flag changes nothing at all."""
    from mediakit.services import media_for, store
    from mediakit.slots import APP_OWNER_ID

    settings.MEDIA_PUBLIC_APP_CONTENT = True
    trainer = make_user("mstore_public_t", user_type="trainer")
    store(owner_type="app", owner_id=APP_OWNER_ID, slot_key="app.story.1",
          upload=image_upload(1080, 1920))
    store(owner_type="trainer", owner_id=trainer.pk, slot_key="trainer.portrait",
          upload=image_upload(1000, 1250))

    app_url = media_for("app", APP_OWNER_ID)["app.story.1"]["url_1x"]
    person_url = media_for("trainer", trainer.pk)["trainer.portrait"]["url_1x"]

    # The filesystem backend signs both: it has no public_url to fall back to, so the
    # flag is inert here and the deployed behaviour is the bucket's to provide.
    assert "s=" in person_url, "a person's picture must never lose its signature"
    assert api().get(app_url).status_code == 200

    # The flag reaches the payload only for app-owned slots. Proved on the call itself,
    # because the filesystem backend cannot show the difference in the URL.
    import mediakit.services as services

    seen = []
    real = services.media_url
    services.media_url = lambda f, r=None, **kw: (seen.append(kw.get("public")), real(f, r))[1]
    try:
        media_for("app", APP_OWNER_ID)
        media_for("trainer", trainer.pk)
    finally:
        services.media_url = real
    assert seen[:2] == [True, True], "app content did not ask for a public URL"
    assert seen[2:] == [False, False], "a trainer's picture asked for a public URL"


def test_the_url_logic_lives_in_one_function():
    """Every backend signs differently, so a serializer that builds a URL by hand hands
    out one that either never expires or never works. `media_url()` is the single place,
    and the media payload is built from it."""
    import inspect

    from mediakit import services

    source = inspect.getsource(services)
    assert "media_url(" in source
    assert "settings.MEDIA_URL" not in source, "a URL is being assembled by hand again"


def test_the_gcs_backend_is_chosen_by_the_environment_and_by_nothing_else():
    """No code branch on "am I in production": MEDIA_BACKEND picks the backend, and the
    GCS module is imported only when it says so — which is why this test suite runs with
    django-storages absent and never reaches the network."""
    base = (ROOT / "training_platform" / "settings_base.py").read_text()
    assert "MEDIA_BACKEND" in base
    assert "training_platform.gcs_storage.SignedGoogleCloudStorage" in base

    gcs = (ROOT / "training_platform" / "gcs_storage.py").read_text()
    # The credential is fetched through the secret helpers, never written down.
    assert "get_secret" in gcs
    assert "BEGIN PRIVATE KEY" not in gcs
    assert re.search(r"def signed_url", gcs), "the GCS backend does not sign"

    from django.conf import settings as live
    assert live.MEDIA_BACKEND == "local", "the test suite must not use object storage"
    assert live.MEDIA_SIGNED_URL_TTL == 3600


# ===========================================================================
# 4. Production refuses the local backend
# ===========================================================================

def _production_boot(**overrides):
    """Import settings_production in a clean subprocess and run its safety gate.

    A subprocess because importing that module mutates shared settings objects (it
    edits REST_FRAMEWORK in place) and the gate has to see a whole, real environment.
    Nothing is reachable from it: no database is opened, no secret store is configured,
    and `enforce_production_safety` only reads what is already in memory.
    """
    env = {
        "PATH": os.environ.get("PATH", ""),
        "DJANGO_ALLOWED_HOSTS": "api.example.com",
        "DB_NAME": "tp", "DB_USER": "tp", "DB_PASSWORD": "x" * 20,
        "DB_HOST": "db.internal",
        "REDIS_URL": "redis://redis.internal:6379",
        "CELERY_BROKER_URL": "redis://redis.internal:6379/6",
        "EMAIL_HOST_USER": "a@example.com", "EMAIL_HOST_PASSWORD": "y" * 12,
        # Deliberate contract change (fx/prod-services, 2026-09-27): production no longer
        # hard-codes Gmail, so the SMTP host and the From address are required from the
        # environment (tests/test_prod_services.py). This environment passes every other
        # check so that the only thing these tests can fail on is the media backend.
        "EMAIL_HOST": "smtp.mail-provider.test",
        "DEFAULT_FROM_EMAIL": "Yalla Gym <no-reply@yallagym.test>",
        "CORS_ALLOWED_ORIGINS": "https://app.example.com",
        "CSRF_TRUSTED_ORIGINS": "https://app.example.com",
        "DJANGO_SECRET_KEY": "s" * 64,
        # Never parsed at import — settings only reads them into SIMPLE_JWT — so a
        # placeholder is enough and no key material belongs in a test file.
        "JWT_PRIVATE_KEY": "-----BEGIN PRIVATE KEY-----\nnot-a-key\n-----END PRIVATE KEY-----",
        "JWT_PUBLIC_KEY": "-----BEGIN PUBLIC KEY-----\nnot-a-key\n-----END PUBLIC KEY-----",
        # A real Fernet key, so the gate's own check passes and the only thing these
        # tests can be failing on is the media backend. GENERATED, never a literal: a
        # key-shaped string in the repository is what `detect-secrets` exists to catch.
        "FIELD_ENCRYPTION_KEY": Fernet.generate_key().decode(),
        # Required since the owner's decision of 2026-09-24: anonymous AI training
        # copies are keyed with it, and production refuses to boot without it. Generated,
        # for the same reason as the key above.
        "AI_TRAINING_PSEUDONYM_KEY": secrets.token_hex(32),
    }
    env.update(overrides)
    code = ("import training_platform.settings_production as s; "
            "s.enforce_production_safety(); print('BOOTED')")
    return subprocess.run([sys.executable, "-c", code], cwd=str(ROOT), env=env,
                          capture_output=True, text=True, timeout=120)


@pytest.mark.django_db(transaction=False)
def test_production_refuses_the_local_media_backend():
    """The filesystem backend in production is a Fly volume: it attaches to ONE machine
    while `auto_start_machines` runs several, it is 1 GB, and it also holds the upload
    spool. Uploads that vanish when the load balancer picks another machine are not a
    thing to discover from a support message."""
    refused = _production_boot(MEDIA_BACKEND="local")

    assert refused.returncode != 0, refused.stdout
    assert "MEDIA_BACKEND" in refused.stderr
    assert "BOOTED" not in refused.stdout

    # The default is local, so leaving it UNSET must be refused too — this cannot be a
    # check you pass by forgetting to set anything.
    unset = _production_boot()
    assert unset.returncode != 0 and "MEDIA_BACKEND" in unset.stderr


#: A service-account key's SHAPE: the two fields a V4 signature is made from. The boot
#: check reads the key and requires them since SIDE_BUGS S-148 — `"{}"` passed it, and
#: so did a secret store that held no key at all, and both booted a deploy on which
#: every media URL failed to sign. A placeholder, like the JWT keys above: nothing
#: parses it as key material at boot.
GCS_KEY_JSON = json.dumps({
    "type": "service_account", "client_email": "media@example.iam.gserviceaccount.com",
    "private_key": "-----BEGIN PRIVATE KEY-----\nnot-a-key\n-----END PRIVATE KEY-----\n",
})


@pytest.mark.django_db(transaction=False)
def test_production_boots_on_object_storage_and_needs_a_bucket_and_a_key():
    booted = _production_boot(MEDIA_BACKEND="gcs", GS_BUCKET_NAME="yalla-gym-media",
                              GS_CREDENTIALS_JSON=GCS_KEY_JSON)
    assert booted.returncode == 0, booted.stderr
    assert "BOOTED" in booted.stdout

    no_bucket = _production_boot(MEDIA_BACKEND="gcs", GS_CREDENTIALS_JSON=GCS_KEY_JSON)
    assert no_bucket.returncode != 0 and "GS_BUCKET_NAME" in no_bucket.stderr

    # Signing a V4 URL needs the service account's private key; without it every media
    # URL raises at serialization time, which is a 500 on screens that work locally.
    no_key = _production_boot(MEDIA_BACKEND="gcs", GS_BUCKET_NAME="yalla-gym-media")
    assert no_key.returncode != 0 and "GS_CREDENTIALS_JSON" in no_key.stderr


def test_the_volume_mount_is_made_writable_before_the_app_drops_privileges():
    """The reason no upload could be written on the deployed app: Fly mounts the volume
    over /data AFTER the image is built, so the image's chown applied to a directory the
    mount then hid, and appuser met a root-owned mount point."""
    dockerfile = (ROOT / "Dockerfile").read_text()
    entrypoint = (ROOT / "docker-entrypoint.sh").read_text()

    assert "docker-entrypoint.sh" in dockerfile
    assert re.search(r"^ENTRYPOINT", dockerfile, re.M)
    # The image must NOT fix the user itself — the chown has to happen after the mount,
    # which means as root, which means the entrypoint drops privileges instead.
    assert not re.search(r"^USER ", dockerfile, re.M), \
        "USER in the image means the entrypoint cannot chown the mounted volume"

    assert "chown" in entrypoint
    assert "setpriv" in entrypoint or "gosu" in entrypoint, \
        "the entrypoint would run the app as root"
    assert 'exec "$@"' in entrypoint


# ===========================================================================
# 5. The metadata task
# ===========================================================================

@pytest.fixture
def no_transcoder(monkeypatch):
    """No ffmpeg and no ffprobe anywhere, which is the production image today."""
    from mediakit import tasks

    monkeypatch.setattr(tasks, "_tool", lambda name: None)
    return tasks


def _video_asset(make_user, slot="exercise.loop", owner=None):
    from mediakit.video import store_video
    from routine.models import Exercise

    if owner is None:
        owner = make_user("mstore_task_t", user_type="trainer")
    exercise = Exercise.objects.create(name="Task Press", created_by=owner)
    asset = store_video(owner_type="exercise", owner_id=exercise.pk, slot_key=slot,
                        upload=upload(mp4_bytes(6.0)), uploaded_by=owner)
    return owner, exercise, asset


def test_the_metadata_task_marks_an_asset_ready_with_no_ffmpeg_installed(
        make_user, no_transcoder):
    """An upload must never fail because a transcoder is missing. Without the binaries
    there is no poster and no measurements — and the asset is still `ready`, so the app
    plays the video and paints its own placeholder."""
    from mediakit.models import MediaAsset

    _owner, _exercise, asset = _video_asset(make_user)
    assert asset.processing_state == MediaAsset.PENDING

    result = no_transcoder.fill_video_metadata(asset.pk)

    asset.refresh_from_db()
    assert asset.processing_state == MediaAsset.READY
    assert result["state"] == MediaAsset.READY
    assert not asset.poster
    assert asset.hls_url == "", "nothing writes an HLS playlist yet"
    # The duration the container declared on upload survives — it came from the bytes,
    # not from a tool, so a missing tool cannot take it away.
    assert asset.duration_seconds == pytest.approx(6.0)


def test_the_metadata_task_fills_what_the_tools_report(make_user, monkeypatch):
    from mediakit import tasks
    from mediakit.models import MediaAsset

    _owner, _exercise, asset = _video_asset(make_user)
    monkeypatch.setattr(tasks, "_probe",
                        lambda path: {"duration": 8.25, "width": 1280, "height": 720})
    monkeypatch.setattr(tasks, "_poster_bytes", lambda path: image_bytes(640, 360))

    tasks.fill_video_metadata(asset.pk)

    asset.refresh_from_db()
    assert asset.processing_state == MediaAsset.READY
    assert (asset.width, asset.height) == (1280, 720)
    assert asset.duration_seconds == pytest.approx(8.25)
    assert asset.poster, "a poster was produced and not stored"
    assert Image.open(asset.poster).format == "JPEG"


def test_a_file_the_prober_refuses_is_marked_failed(make_user, monkeypatch):
    """`failed` is reserved for bytes a tool actually looked at and could not read —
    not for a tool that is absent. An operator needs to be able to tell those apart."""
    from mediakit import tasks
    from mediakit.models import MediaAsset

    _owner, _exercise, asset = _video_asset(make_user)
    monkeypatch.setattr(tasks, "_probe", lambda path: False)

    tasks.fill_video_metadata(asset.pk)

    asset.refresh_from_db()
    assert asset.processing_state == MediaAsset.FAILED


def test_the_sweep_picks_up_a_video_whose_task_was_lost(make_user, no_transcoder,
                                                        monkeypatch):
    """Celery is at-least-once: a worker that dies between the commit and the ack drops
    the message, and the asset would sit `pending` for ever."""
    from datetime import timedelta

    from django.utils import timezone

    from mediakit.models import MediaAsset

    _owner, _exercise, asset = _video_asset(make_user)
    MediaAsset.objects.filter(pk=asset.pk).update(
        updated_at=timezone.now() - timedelta(hours=2))

    queued = []
    monkeypatch.setattr(no_transcoder.fill_video_metadata, "delay", queued.append)
    assert no_transcoder.sweep_pending_videos()["requeued"] == 1
    assert queued == [asset.pk]

    # A ready asset is not swept again.
    MediaAsset.objects.filter(pk=asset.pk).update(processing_state=MediaAsset.READY)
    queued.clear()
    assert no_transcoder.sweep_pending_videos()["requeued"] == 0


def test_the_sweep_is_actually_scheduled():
    """A task written to run on a timer and on no timer never runs once — the failure
    that hid `generate-daily-advice` and the retention purge."""
    from training_platform.celery import app

    assert "mediakit.sweep_pending_videos" in app.tasks
    assert any(entry["task"] == "mediakit.sweep_pending_videos"
               for entry in app.conf.beat_schedule.values())


# ===========================================================================
# 6. Video in the coach's library, under the library's own visibility rule
# ===========================================================================

NOT_FOUND_BODY = {"error": "Exercise not found.", "detail": "Exercise not found.",
                  "code": "not_found"}


def media_url_for(exercise):
    return f"/api/v1/content/media/exercise/{getattr(exercise, 'id', exercise)}/"


UPLOAD_URL = "/api/v1/content/media/video/"


@pytest.fixture
def library(db, make_user):
    """One trainer's private exercise carrying BOTH a picture and a video, a stranger
    trainer, and the shared catalogue."""
    from mediakit.services import store
    from mediakit.video import store_video
    from routine.models import Exercise

    owner = make_user("mstore_owner", user_type="trainer")
    stranger = make_user("mstore_stranger", user_type="trainer")
    admin = make_user("mstore_admin", user_type="admin")

    private = Exercise.objects.create(name="Owner Only Press", created_by=owner)
    store(owner_type="exercise", owner_id=private.pk, slot_key="exercise.poster",
          upload=image_upload(1280, 720))
    store_video(owner_type="exercise", owner_id=private.pk, slot_key="exercise.loop",
                upload=upload(mp4_bytes(5.0)), uploaded_by=owner)

    catalogue = Exercise.objects.create(name="Catalogue Squat")
    assert private.is_global is False and catalogue.is_global is True
    return {"owner": owner, "stranger": stranger, "admin": admin,
            "private": private, "catalogue": catalogue}


def test_the_owner_reads_their_exercises_video_and_its_picture(library, api):
    response = api(library["owner"]).get(media_url_for(library["private"]))

    assert response.status_code == 200, response.content
    media = response.json()["media"]
    assert set(media) == {"exercise.poster", "exercise.loop"}
    assert media["exercise.poster"]["url_1x"]
    video = media["exercise.loop"]
    assert video["state"] == "pending" and video["video_url"]
    assert video["duration_seconds"] == pytest.approx(5.0)
    # Absent, never null, until something produces them.
    assert "poster_url" not in video and "hls_url" not in video


def test_another_trainers_private_video_answers_404_exactly_as_its_pictures_do(
        library, api):
    """The finding this route had to not repeat: `add-media/` resolved ids for itself
    and so had no rule at all, serving any trainer's private media to anyone who walked
    integer ids. Video is not an exception to the library's visibility rule."""
    from routine.models import Exercise

    stranger = api(library["stranger"])
    invisible = stranger.get(media_url_for(library["private"]))
    missing = stranger.get(media_url_for(Exercise.objects.order_by("-id").first().id + 500))

    assert invisible.status_code == missing.status_code == 404
    assert invisible.json() == missing.json() == NOT_FOUND_BODY
    assert "video" not in invisible.content.decode().lower()


def test_an_approved_client_sees_their_coachs_video(library, api, make_user):
    """The feature itself: a client watches their own trainer perform the move."""
    from users.models import TrainerClientRelation

    client_user = make_user("mstore_client", user_type="client")
    TrainerClientRelation.objects.create(trainer=library["owner"], client=client_user,
                                         status="approved", initiated_by="client")

    response = api(client_user).get(media_url_for(library["private"]))

    assert response.status_code == 200, response.content
    assert response.json()["media"]["exercise.loop"]["video_url"]


def test_reading_an_exercises_media_needs_a_signed_in_caller(library, api):
    response = api().get(media_url_for(library["private"]))
    assert response.status_code in (401, 403)


def test_a_stranger_uploading_to_a_private_exercise_is_answered_as_missing(library, api):
    """404, not 403: a 403 would confirm the exercise exists just as well as reading it
    would, and the caller is not entitled to that either."""
    from mediakit.models import MediaAsset

    before = MediaAsset.objects.get(owner_type="exercise", slot="exercise.loop").pk
    response = api(library["stranger"]).post(UPLOAD_URL, {
        "owner_type": "exercise", "owner_id": library["private"].pk,
        "slot": "exercise.cue", "file": upload(mp4_bytes(2.0))})

    assert response.status_code == 404, response.content
    assert response.json() == NOT_FOUND_BODY
    assert MediaAsset.objects.filter(owner_type="exercise").count() == 2
    assert MediaAsset.objects.get(slot="exercise.loop").pk == before


def test_seeing_an_exercise_is_not_uploading_to_it(library, api, make_user):
    from mediakit.models import MediaAsset
    from users.models import TrainerClientRelation

    client_user = make_user("mstore_reader", user_type="client")
    TrainerClientRelation.objects.create(trainer=library["owner"], client=client_user,
                                         status="approved", initiated_by="client")

    response = api(client_user).post(UPLOAD_URL, {
        "owner_type": "exercise", "owner_id": library["private"].pk,
        "slot": "exercise.cue", "file": upload(mp4_bytes(2.0))})

    assert response.status_code == 403, response.content
    assert not MediaAsset.objects.filter(slot="exercise.cue").exists()


def test_the_owner_uploads_a_cue_video_and_the_slot_answers_it(library, api):
    from mediakit.models import MediaAsset

    response = api(library["owner"]).post(UPLOAD_URL, {
        "owner_type": "exercise", "owner_id": library["private"].pk,
        "slot": "exercise.cue", "file": upload(mp4_bytes(7.0), name="whatever.bin"),
        "alt_ar": "شرح الحركة"})

    assert response.status_code == 201, response.content
    body = response.json()
    assert body["slot"] == "exercise.cue"
    assert body["media"]["state"] == "pending" and body["media"]["video_url"]
    asset = MediaAsset.objects.get(slot="exercise.cue")
    assert asset.alt_ar == "شرح الحركة"
    # Stored under the detected container's extension, not the name that was sent.
    assert asset.original.name.endswith(".mp4")
    assert asset.uploaded_by_id == library["owner"].pk


@pytest.mark.parametrize("payload,code,status_code", [
    ({"slot": "exercise.loop", "owner_type": "exercise"}, "media_file_required", 400),
    ({"slot": "exercise.poster", "owner_type": "exercise"}, "media_slot_is_not_video", 400),
    ({"slot": "trainer.loop", "owner_type": "exercise"}, "media_unknown_slot", 400),
    ({"slot": "no.such.slot", "owner_type": "exercise"}, "media_unknown_slot", 400),
])
def test_the_upload_route_refuses_with_a_code(library, api, payload, code, status_code):
    body = dict(payload, owner_id=library["private"].pk)
    if code != "media_file_required":
        body["file"] = upload(mp4_bytes(2.0))

    response = api(library["owner"]).post(UPLOAD_URL, body)

    assert response.status_code == status_code, response.content
    assert response.json()["code"] == code


def test_an_upload_that_is_not_a_video_is_refused_by_the_route(library, api):
    from mediakit.models import MediaAsset

    response = api(library["owner"]).post(UPLOAD_URL, {
        "owner_type": "exercise", "owner_id": library["private"].pk,
        "slot": "exercise.cue",
        "file": SimpleUploadedFile("clip.mp4", image_bytes(), content_type="video/mp4")})

    assert response.status_code == 400, response.content
    assert response.json()["code"] == "media_not_a_video"
    assert not MediaAsset.objects.filter(slot="exercise.cue").exists()


def test_a_trainer_uploads_their_own_intro_and_not_someone_elses(library, api):
    from mediakit.models import MediaAsset

    owner_api = api(library["owner"])
    mine = owner_api.post(UPLOAD_URL, {
        "owner_type": "trainer", "owner_id": library["owner"].pk,
        "slot": "trainer.intro", "file": upload(mp4_bytes(12.0))})
    assert mine.status_code == 201, mine.content
    assert MediaAsset.objects.get(owner_type="trainer").person_id == library["owner"].pk

    theirs = owner_api.post(UPLOAD_URL, {
        "owner_type": "trainer", "owner_id": library["stranger"].pk,
        "slot": "trainer.intro", "file": upload(mp4_bytes(12.0))})
    assert theirs.status_code == 403, theirs.content
    assert MediaAsset.objects.filter(owner_type="trainer").count() == 1


def test_app_content_is_admin_only(library, api):
    from mediakit.models import MediaAsset

    refused = api(library["owner"]).post(UPLOAD_URL, {
        "owner_type": "app", "owner_id": 0, "slot": "trainer.loop",
        "file": upload(mp4_bytes(3.0))})
    assert refused.status_code == 400  # wrong owner for that slot, before anything else

    # An admin is the one who curates shared content, and an exercise video is theirs
    # to replace too.
    allowed = api(library["admin"]).post(UPLOAD_URL, {
        "owner_type": "exercise", "owner_id": library["catalogue"].pk,
        "slot": "exercise.loop", "file": upload(mp4_bytes(3.0))})
    assert allowed.status_code == 201, allowed.content
    assert MediaAsset.objects.filter(owner_id=library["catalogue"].pk).exists()


def test_a_replaced_video_does_not_keep_the_old_posters_measurements(library, api,
                                                                     make_user):
    """A re-upload into a filled slot describes new bytes. Keeping the previous poster
    or duration would show the app one video and describe another."""
    from mediakit.models import MediaAsset
    from mediakit.video import store_video

    asset = MediaAsset.objects.get(slot="exercise.loop")
    MediaAsset.objects.filter(pk=asset.pk).update(
        width=1920, height=1080, duration_seconds=5.0,
        processing_state=MediaAsset.READY, hls_url="something/old.m3u8")

    store_video(owner_type="exercise", owner_id=library["private"].pk,
                slot_key="exercise.loop", upload=upload(webm_bytes()),
                uploaded_by=library["owner"])

    asset.refresh_from_db()
    assert asset.processing_state == MediaAsset.PENDING
    assert (asset.width, asset.height) == (0, 0)
    assert asset.duration_seconds is None      # WebM declares none where we can read it
    assert asset.hls_url == ""
    assert asset.original.name.endswith(".webm")
    assert MediaAsset.objects.filter(slot="exercise.loop").count() == 1


def test_an_image_slot_still_refuses_a_video_and_the_image_payload_is_unchanged(library):
    """Adding video must not move anything the app already reads."""
    from mediakit.services import media_for

    payload = media_for("exercise", library["private"].pk)["exercise.poster"]
    assert set(payload) == {"url_1x", "url_2x", "blur", "focal", "brightness",
                            "alt_ar", "alt_en"}


def test_a_video_upload_through_the_image_path_is_still_refused(library):
    """`store()` is the image pipeline and knows nothing about containers; a video slot
    reaching it keeps answering `media_kind_not_supported_yet`, which is what the app
    has been told that code means."""
    from mediakit.services import store

    with pytest.raises(ValidationError) as refused:
        store(owner_type="exercise", owner_id=library["private"].pk,
              slot_key="exercise.cue", upload=image_upload(1280, 720))
    assert refused.value.code == "media_kind_not_supported_yet"


# ===========================================================================
# 7. Wiring
# ===========================================================================

def test_the_upload_route_answers_with_and_without_the_trailing_slash(library, api):
    """Not cosmetic: the size middleware runs FIRST and matches the exact path, so the
    unslashed spelling would be measured against the 15 MB cap and refused before
    anything could redirect it — and a redirected POST arrives with no body."""
    from django.conf import settings as live

    from mediakit.models import MediaAsset

    # Both spellings of every large-upload route. The per-trainer exercise library
    # joined the set on 2026-09-20 (BACKEND_DELTA item 7): a coach's own demonstration
    # is the same video through the same pipeline, so it needs the same ceiling. The
    # assertion stays exact rather than becoming a subset check — the point of it is
    # that no route quietly acquires a 65 MB body limit. The chat's media route joined it
    # on 2026-09-24 (BACKEND_BUGS B-091): a client's form-check video goes through the
    # same video guard with the same caps (chat/media.py), so it needs the same ceiling,
    # deliberately and by name.
    assert set(live.LARGE_UPLOAD_PATHS) == {"/api/v1/content/media/video",
                                            "/api/v1/content/media/video/",
                                            "/api/v1/trainer/exercise-media",
                                            "/api/v1/trainer/exercise-media/",
                                            "/api/v1/chat/media",
                                            "/api/v1/chat/media/"}

    response = api(library["owner"]).post("/api/v1/content/media/video", {
        "owner_type": "exercise", "owner_id": library["private"].pk,
        "slot": "exercise.cue", "file": upload(mp4_bytes(4.0))})

    assert response.status_code == 201, response.content
    assert MediaAsset.objects.filter(slot="exercise.cue").exists()


def test_every_new_route_is_in_the_contract():
    contract = (ROOT / "API_CONTRACT.md").read_text()
    for path in ("/api/v1/content/media/video/",
                 "/api/v1/content/media/exercise/{id}/"):
        assert path in contract, f"{path} is not in API_CONTRACT.md"


def test_every_refusal_code_the_video_path_can_answer_is_documented():
    """The app branches on `code` and on nothing else, so a code that exists only in the
    source is a branch the client cannot write."""
    contract = (ROOT / "API_CONTRACT.md").read_text()
    for code in ("media_file_required", "media_unknown_slot", "media_unknown_owner",
                 "media_slot_is_not_video", "media_not_a_video",
                 "media_video_too_large", "media_video_too_long"):
        assert code in contract, f"{code} is not documented"


def test_the_arabic_translation_is_compiled_and_covers_the_new_strings():
    """Arabic is the primary language: an untranslated refusal is an English sentence in
    the middle of an Arabic screen."""
    from django.utils import translation

    po = (ROOT / "locale" / "ar" / "LC_MESSAGES" / "django.po").read_text()
    assert (ROOT / "locale" / "ar" / "LC_MESSAGES" / "django.mo").exists()

    for english in ("This file is not a video. Upload an MP4 or a WebM.",
                    "This slot does not take a video.",
                    "Attach the video as `file`."):
        assert f'msgid "{english}"' in po, f"{english!r} has no Arabic entry"
        with translation.override("ar"):
            assert str(translation.gettext(english)) != english, \
                f"{english!r} is not translated in the compiled catalogue"


def test_a_trainers_video_is_personal_data_the_registry_can_erase(make_user):
    """A trainer's photographs are registered; their footage is the same row and the
    same promise. Erasure has to take the video with it."""
    from mediakit.video import store_video
    from training_platform.privacy.registry import registry

    trainer = make_user("mstore_erase", user_type="trainer")
    store_video(owner_type="trainer", owner_id=trainer.pk, slot_key="trainer.intro",
                upload=upload(mp4_bytes(4.0)), uploaded_by=trainer)

    source = registry["mediakit.MediaAsset"]
    assert source.on_erase == "delete"
    assert source.queryset_for(trainer).filter(slot="trainer.intro").exists()
