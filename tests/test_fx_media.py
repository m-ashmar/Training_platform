"""Uploads and media: the rows the independent dive of 2026-09-23 found (SIDE_BUGS.md).

One test (or a small group) per row, each failing against the code that was there:

    S-053  the same sentence entered the catalogue twice, differing by a full stop
    S-079  an animated GIF was decoded frame by frame with no frame or pixel budget
    S-140  a 4096 px cap ran first, so the documented caps and codes never fired
    S-141  quarantine eviction ran on every refusal, two storage calls per sample
    S-142  dead upload helpers that read as vetted entry points
    S-143  a phone JPEG with an MPF secondary image (Pillow: MPO) was refused
    S-144  the MP4 box walk had no bound on the number of boxes
    S-064  the video task saved a stale row and deleted a re-uploaded video
    S-101  line (a) of the media rule served one coach's footage to another coach
    S-102  the video task downloaded the whole file before asking for ffprobe/ffmpeg
    S-103  a refused slot write left its files in storage and answered `invalid`
    S-104  the public media route served the promotional banner to store builds
    S-148  the boot check took "a secret store exists" as proof the GCS key exists
    S-081  the media host allow-list allowed every GCS bucket in the world
"""
import io
import json
import os
import secrets
import struct
import subprocess
import sys
from pathlib import Path

import pytest
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from PIL import Image

pytestmark = pytest.mark.django_db

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def media_root(settings, tmp_path):
    """Uploads and quarantined samples land in this test's own directory."""
    settings.MEDIA_ROOT = str(tmp_path)
    return tmp_path


# ---------------------------------------------------------------------------- builders
def _box(kind: bytes, payload: bytes) -> bytes:
    return struct.pack(">I", len(payload) + 8) + kind + payload


def _mvhd(duration_seconds, timescale=1000) -> bytes:
    body = (b"\x00\x00\x00\x00" + struct.pack(">I", 0) + struct.pack(">I", 0)
            + struct.pack(">I", timescale)
            + struct.pack(">I", int(duration_seconds * timescale))
            + b"\x00\x01\x00\x00" + b"\x01\x00" + b"\x00" * 10 + b"\x00" * 36
            + b"\x00" * 24 + struct.pack(">I", 2))
    return _box(b"mvhd", body)


def _ftyp() -> bytes:
    return _box(b"ftyp", b"isom" + struct.pack(">I", 512) + b"isom" + b"mp42")


def mp4_bytes(duration_seconds=4.0, filler=2048) -> bytes:
    return _ftyp() + _box(b"moov", _mvhd(duration_seconds)) + _box(b"mdat", b"\x00" * filler)


def video_upload(duration_seconds=4.0):
    return SimpleUploadedFile("clip.mp4", mp4_bytes(duration_seconds),
                              content_type="video/mp4")


def jpeg_upload(w, h, name="shot.jpg", mode="RGB"):
    buf = io.BytesIO()
    Image.new(mode, (w, h), 180 if mode == "L" else (200, 60, 30)).save(buf, format="JPEG")
    return SimpleUploadedFile(name, buf.getvalue(), content_type="image/jpeg")


def gif_upload(w, h, frames, name="anim.gif"):
    """An animated GIF whose consecutive frames differ, so Pillow keeps every one."""
    images = [Image.new("P", (w, h), n % 2) for n in range(frames)]
    for image in images:
        image.putpalette([0, 0, 0, 255, 255, 255] + [0] * 762)
    buf = io.BytesIO()
    images[0].save(buf, format="GIF", save_all=True, append_images=images[1:],
                   duration=40, loop=0)
    return SimpleUploadedFile(name, buf.getvalue(), content_type="image/gif")


def mpo_upload(w, h, name="phone.jpg"):
    """A JPEG with an MPF secondary image — what a phone writes with a depth or gain map."""
    primary = Image.new("RGB", (w, h), (200, 60, 30))
    secondary = Image.new("RGB", (w, h), (10, 10, 10))
    buf = io.BytesIO()
    primary.save(buf, format="MPO", save_all=True, append_images=[secondary])
    return SimpleUploadedFile(name, buf.getvalue(), content_type="image/jpeg")


def _stored_files(root: Path):
    kit = root / "media_kit"
    return sorted(p for p in kit.rglob("*") if p.is_file()) if kit.exists() else []


def _refusal_code(excinfo):
    return getattr(excinfo.value, "code", None)


# =============================================================================== S-053
def _first_party_msgids():
    """{msgid: "file:line"} for every literal a first-party module hands to gettext."""
    import ast

    not_ours = {"tests", "_excluded", "mobile", "flutter", "yalla_gym"}
    names = {"_", "gettext", "gettext_lazy", "gettext_noop"}
    found = {}
    for package in sorted(p for p in ROOT.iterdir()
                          if p.is_dir() and p.name not in not_ours
                          and (p / "__init__.py").exists()):
        for source in sorted(package.rglob("*.py")):
            relative = source.relative_to(ROOT)
            if {"migrations", "tests"} & set(relative.parts):
                continue
            for node in ast.walk(ast.parse(source.read_text(encoding="utf-8"))):
                if not isinstance(node, ast.Call) or not node.args:
                    continue
                func = node.func
                name = (func.id if isinstance(func, ast.Name)
                        else func.attr if isinstance(func, ast.Attribute) else None)
                arg = node.args[0]
                if name in names and isinstance(arg, ast.Constant) \
                        and isinstance(arg.value, str) and arg.value:
                    found.setdefault(arg.value, f"{relative}:{node.lineno}")
    return found


#: Pairs that differ only in case and full stop ON PURPOSE. A notification TITLE is
#: title-cased beside its siblings ("Request Approved", "Request Cancelled"); the
#: sentence the approval route answers is a sentence. Two roles, and the Arabic of a
#: title may be shortened without the sentence following it.
DELIBERATE_PAIRS = {frozenset({"Request Rejected", "Request rejected."})}


def test_s053_one_sentence_has_one_catalogue_entry():
    """`"Empty file not allowed"` and `"Empty file not allowed."` are two msgids, so two
    Arabic entries to keep in step — and nine such pairs had drifted in. The catalogue
    cannot see that they are one sentence; the source can."""
    groups = {}
    for msgid, where in _first_party_msgids().items():
        groups.setdefault(msgid.strip().rstrip(".").lower(), {})[msgid] = where

    twins = [spellings for spellings in groups.values()
             if len(spellings) > 1 and frozenset(spellings) not in DELIBERATE_PAIRS]
    assert not twins, "one sentence, several msgids:\n" + "\n".join(
        " | ".join(f"{m!r} ({w})" for m, w in sorted(t.items())) for t in twins)


# =============================================================================== S-079
def test_s079_an_animation_pays_for_every_frame(monkeypatch):
    """Frames x canvas is what an animated GIF costs to re-encode, and only one canvas
    was checked. The pixel budget is lowered so the arithmetic is visible: five 50x50
    frames are 12,500 pixels, each frame alone is 2,500."""
    from training_platform import file_security

    monkeypatch.setattr(file_security, "MAX_IMAGE_PIXELS", 10_000)
    with pytest.raises(ValidationError) as refused:
        file_security.process_uploaded_image(gif_upload(50, 50, frames=5))
    assert _refusal_code(refused) == "image_too_many_pixels"

    # The same animation inside the budget is still an animation after the re-encode.
    monkeypatch.setattr(file_security, "MAX_IMAGE_PIXELS", 40_000_000)
    stored, ext = file_security.process_uploaded_image(gif_upload(50, 50, frames=5))
    assert ext == "gif"
    assert Image.open(io.BytesIO(stored.read())).n_frames == 5


def test_s079_a_tiny_canvas_cannot_carry_unbounded_frames():
    """A 2x2 canvas is nothing per frame, and thousands of frames of it are thousands of
    decoded images held at once. Refused on the frame count, before any is decoded."""
    from training_platform.file_security import process_uploaded_image

    with pytest.raises(ValidationError) as refused:
        process_uploaded_image(gif_upload(2, 2, frames=301))
    assert _refusal_code(refused) == "image_too_many_pixels"


def test_s079_the_kit_refuses_a_gif_before_decoding_it(monkeypatch):
    """The media kit never takes a GIF, and it used to decode every frame through the
    platform guard only to refuse the file afterwards."""
    from mediakit.processing import process_upload
    from mediakit.slots import resolve
    from training_platform import file_security

    called = []
    monkeypatch.setattr(file_security, "process_uploaded_image",
                        lambda *a, **k: called.append(a))
    with pytest.raises(ValidationError) as refused:
        process_upload(gif_upload(64, 64, frames=3), resolve("app.story.1"))
    assert _refusal_code(refused) == "media_unsupported_format"
    assert called == [], "the GIF was decoded before it was refused"


# =============================================================================== S-140
def test_s140_the_documented_size_caps_are_the_ones_that_decide():
    """`validate_file` refused anything over 4096 px as `image_too_large` before
    `process_uploaded_image` ran, so its own caps — and the codes the profile-picture
    route documents — never fired, and a 24 MP phone photo was refused."""
    from training_platform.file_security import process_uploaded_image

    stored, ext = process_uploaded_image(jpeg_upload(5712, 4284, mode="L"))
    assert ext == "jpg"
    assert Image.open(io.BytesIO(stored.read())).size == (5712, 4284)

    with pytest.raises(ValidationError) as too_wide:
        process_uploaded_image(jpeg_upload(10_500, 1_500, mode="L"))
    assert _refusal_code(too_wide) == "image_dimensions_too_large"

    with pytest.raises(ValidationError) as too_many:
        process_uploaded_image(jpeg_upload(7_000, 6_000, mode="L"))
    assert _refusal_code(too_many) == "image_too_many_pixels"


# =============================================================================== S-141
def _php_upload():
    return SimpleUploadedFile("payload.php", b"<?php echo 1; ?>",
                              content_type="application/x-php")


def test_s141_eviction_runs_once_a_window_not_once_a_refusal(monkeypatch):
    """Every refused upload walked the whole quarantine. Once it held thousands of
    samples, each later refusal — an honest user's included — paid for thousands of
    storage calls inside its request."""
    from django.core.cache import cache

    from training_platform.file_security import FileSecurityValidator

    cache.clear()
    passes = []
    monkeypatch.setattr(FileSecurityValidator, "_evict_quarantine",
                        lambda self: passes.append(1))
    for _ in range(3):
        with pytest.raises(ValidationError):
            FileSecurityValidator().validate_file(_php_upload(), file_type="image")
    assert len(passes) == 1, f"{len(passes)} eviction passes for 3 refusals in a window"
    cache.clear()


class _Blob:
    def __init__(self, name, updated, size):
        self.name, self.updated, self.size = name, updated, size


class _FakeBucketStorage:
    """The GCS backend's shape: a bucket whose listing carries each object's metadata,
    and per-object `size()`/`get_modified_time()` that are one HTTP request each."""

    def __init__(self, blobs):
        self._blobs = blobs
        self.deleted = []
        self.per_object_calls = 0
        self.bucket = self

    def list_blobs(self, prefix=None, delimiter=None):
        return [b for b in self._blobs if b.name.startswith(prefix)]

    def _normalize_name(self, name):
        return f"loc/{name}"

    def size(self, name):
        self.per_object_calls += 1
        raise AssertionError("size() is an HTTP request per sample")

    def get_modified_time(self, name):
        self.per_object_calls += 1
        raise AssertionError("get_modified_time() is an HTTP request per sample")

    def delete(self, name):
        self.deleted.append(name)


def test_s141_one_listing_and_a_bound_on_how_many_samples_are_kept(monkeypatch):
    """Sizes and times come from ONE listing, and tiny samples are capped by count:
    1030 one-byte samples are 1 KB against a 64 MB byte cap and used to stay the full
    seven days."""
    from datetime import timedelta

    from django.utils import timezone

    from training_platform.file_security import FileSecurityValidator

    now = timezone.now()
    stale = _Blob("loc/quarantine/quarantine_old_1", now - timedelta(days=8), 10)
    fresh = [_Blob(f"loc/quarantine/quarantine_f{n:04d}_1",
                   now - timedelta(minutes=2000 - n), 1) for n in range(1030)]
    storage = _FakeBucketStorage([stale] + fresh)
    monkeypatch.setattr(FileSecurityValidator, "quarantine_storage",
                        staticmethod(lambda: storage))

    FileSecurityValidator()._evict_quarantine()

    assert storage.per_object_calls == 0
    assert "quarantine/quarantine_old_1" in storage.deleted
    oldest = [f"quarantine/{b.name.rsplit('/', 1)[1]}" for b in fresh[:6]]
    assert set(oldest) <= set(storage.deleted), storage.deleted[:10]
    assert len(storage.deleted) == 7, len(storage.deleted)


# =============================================================================== S-142
def test_s142_the_dead_upload_helpers_are_gone():
    """`validate_uploaded_image` validated WITHOUT re-encoding, so the next caller to
    adopt it would have stored the original bytes, EXIF and polyglot payload included.
    Nothing called any of these."""
    from training_platform import file_security

    for name in ("SecureFileUploadMixin", "validate_uploaded_image",
                 "validate_uploaded_document", "secure_file_upload_path",
                 "delete_file_field"):
        assert not hasattr(file_security, name), name
    assert "content_type" not in file_security.VideoFacts.__slots__


# =============================================================================== S-143
def test_s143_a_phone_jpeg_with_a_secondary_image_is_a_jpeg():
    """libmagic says `image/jpeg`, Pillow says `MPO`, and the guard refused it as
    `unsupported_format` on every image route."""
    from training_platform.file_security import process_uploaded_image

    stored, ext = process_uploaded_image(mpo_upload(640, 480))
    assert ext == "jpg"
    decoded = Image.open(io.BytesIO(stored.read()))
    assert decoded.format == "JPEG"
    assert getattr(decoded, "n_frames", 1) == 1, "the secondary image was kept"


def test_s143_the_media_kit_takes_it_too():
    from mediakit.services import store
    from mediakit.slots import resolve

    width, height = resolve("app.story.1").min_size
    asset = store(owner_type="app", owner_id=0, slot_key="app.story.1",
                  upload=mpo_upload(width, height))
    assert asset.file_1x and asset.original.name.endswith(".jpg")


# =============================================================================== S-144
class _CountingFile(io.BytesIO):
    def __init__(self, data):
        super().__init__(data)
        self.reads = 0

    def read(self, *args):
        self.reads += 1
        return super().read(*args)


def test_s144_the_box_walk_stops_at_a_bound():
    """64 MB of eight-byte boxes is eight million seeks and reads inside the request."""
    from training_platform.file_security import probe_declared_duration

    data = _ftyp() + _box(b"free", b"") * 20_000 + _box(b"moov", _mvhd(5.0))
    fp = _CountingFile(data)

    assert probe_declared_duration(fp, "iso") is None
    assert fp.reads <= 10_010, f"{fp.reads} reads for one duration"

    # A real file, boxes and all, still declares its duration.
    assert probe_declared_duration(io.BytesIO(mp4_bytes(6.0)), "iso") == 6.0


# =============================================================================== S-064
def test_s064_a_reupload_during_the_video_task_survives_it(
        make_user, monkeypatch, django_capture_on_commit_callbacks):
    """The task saved the instance it loaded at the start. If the coach re-uploaded the
    slot meanwhile, the pre_save receiver compared that stale `original` with the new
    one and deleted the NEW video, while the row kept pointing at it — a 404 for good."""
    from django.core.files.storage import default_storage

    from mediakit import tasks
    from mediakit.models import MediaAsset
    from mediakit.video import store_video
    from routine.models import Exercise

    owner = make_user("fxm_s064", user_type="trainer")
    exercise = Exercise.objects.create(name="fxm s064", created_by=owner)
    first = store_video(owner_type="exercise", owner_id=exercise.pk,
                        slot_key="exercise.loop", upload=video_upload(4.0),
                        uploaded_by=owner)
    monkeypatch.setattr(tasks.fill_video_metadata, "delay", lambda *a, **k: None)

    reuploaded = {}

    def probe_while_the_coach_reuploads(_source):
        if not reuploaded:
            again = store_video(owner_type="exercise", owner_id=exercise.pk,
                                slot_key="exercise.loop", upload=video_upload(7.0),
                                uploaded_by=owner)
            reuploaded["name"] = again.original.name
        return {"duration": 4.0, "width": 640, "height": 360}

    monkeypatch.setattr(tasks, "_probe", probe_while_the_coach_reuploads)
    monkeypatch.setattr(tasks, "_poster_bytes", lambda _source: None)

    with django_capture_on_commit_callbacks(execute=True):
        tasks.fill_video_metadata(first.pk)

    row = MediaAsset.objects.get(pk=first.pk)
    assert row.original.name == reuploaded["name"]
    assert default_storage.exists(row.original.name), "the re-uploaded video was deleted"
    # The run measured the OLD bytes; nothing it found is written onto the new ones.
    assert row.processing_state == MediaAsset.PENDING
    assert row.duration_seconds == pytest.approx(7.0)
    assert (row.width, row.height) == (0, 0)


# =============================================================================== S-102
def test_s102_no_tools_means_no_download(make_user, monkeypatch):
    """The production image has neither ffprobe nor ffmpeg, and every video — up to
    64 MB — was still pulled from the bucket to the worker, only to be marked ready."""
    from mediakit import tasks
    from mediakit.models import MediaAsset
    from mediakit.video import store_video
    from routine.models import Exercise

    owner = make_user("fxm_s102", user_type="trainer")
    exercise = Exercise.objects.create(name="fxm s102", created_by=owner)
    asset = store_video(owner_type="exercise", owner_id=exercise.pk,
                        slot_key="exercise.loop", upload=video_upload(), uploaded_by=owner)

    downloads = []
    monkeypatch.setattr(tasks, "_tool", lambda name: None)
    monkeypatch.setattr(tasks, "_local_copy",
                        lambda a: downloads.append(a.pk) or ("/nonexistent.mp4", False))

    result = tasks.fill_video_metadata(asset.pk)

    assert downloads == [], "the video was downloaded with no tool to read it"
    asset.refresh_from_db()
    assert asset.processing_state == MediaAsset.READY
    assert result["state"] == MediaAsset.READY


# =============================================================================== S-101
@pytest.fixture
def two_coaches(make_user):
    """Client C was coached by A, who wrote C's routine and filmed its exercise; C is
    now coached by B, whose trainer list therefore includes A's routine."""
    from mediakit.services import store
    from mediakit.slots import OwnerType, resolve
    from routine.models import Exercise, Routine, RoutineExercise
    from users.models import TrainerClientRelation

    old = make_user("fxm_s101_old", user_type="trainer")
    new = make_user("fxm_s101_new", user_type="trainer")
    client = make_user("fxm_s101_client")
    TrainerClientRelation.objects.create(trainer=new, client=client, status="approved",
                                         initiated_by="client")

    exercise = Exercise.objects.create(name="fxm s101 squat")
    width, height = resolve("exercise.poster").min_size
    store(owner_type=OwnerType.TRAINER_EXERCISE, owner_id=exercise.pk,
          slot_key="exercise.poster", upload=jpeg_upload(width, height),
          person=old, uploaded_by=old)
    routine = Routine.objects.create(name="fxm s101 program", created_by=old)
    routine.assigned_to.add(client)
    RoutineExercise.objects.create(routine=routine, exercise=exercise, day=1, order=1)
    return {"old": old, "new": new, "client": client, "exercise": exercise,
            "routine": routine}


def test_s101_a_coach_never_sees_another_coachs_private_footage(two_coaches, api):
    from mediakit.resolution import resolve_exercise_media

    old, new, exercise = two_coaches["old"], two_coaches["new"], two_coaches["exercise"]

    answer = resolve_exercise_media(new, [exercise.pk],
                                    routine_author_id=old.pk)[exercise.pk]
    assert answer["media_trainer_id"] != old.pk, answer
    assert answer["media_source"] != "routine_author"

    # And through the route the new coach actually reads.
    body = api(new).get(f"/api/routine/routines/{two_coaches['routine'].pk}/")
    assert body.status_code == 200, body.content
    row = body.json()["routine_exercises"][0]["exercise"]
    assert row["media_trainer_id"] != old.pk, row
    assert "exercise.poster" not in row["media_slots"]


def test_s101_the_client_and_the_author_still_get_line_a(two_coaches):
    """The rule is unchanged for the people it is for: the client the routine was
    prescribed to (their old coach demonstrates their old programme), and its author."""
    from mediakit.resolution import resolve_exercise_media

    old, exercise = two_coaches["old"], two_coaches["exercise"]
    for viewer in (two_coaches["client"], old):
        answer = resolve_exercise_media(viewer, [exercise.pk],
                                        routine_author_id=old.pk)[exercise.pk]
        assert answer["media_source"] in ("routine_author", "my_trainer"), answer
        assert answer["media_trainer_id"] == old.pk


# =============================================================================== S-103
def test_s103_a_refused_write_leaves_nothing_in_storage(make_user, media_root):
    """Alt text over the column's 200 characters was caught by the `full_clean()` that
    ran AFTER the video was written: the row rolled back, the bytes stayed, and the
    view answered `invalid`, which no table documents."""
    from mediakit.services import store
    from mediakit.slots import resolve
    from mediakit.video import store_video
    from routine.models import Exercise

    owner = make_user("fxm_s103", user_type="trainer")
    exercise = Exercise.objects.create(name="fxm s103", created_by=owner)

    with pytest.raises(ValidationError) as video:
        store_video(owner_type="exercise", owner_id=exercise.pk, slot_key="exercise.loop",
                    upload=video_upload(), uploaded_by=owner, alt_ar="ا" * 201)
    assert _refusal_code(video) == "media_alt_too_long"

    width, height = resolve("exercise.poster").min_size
    with pytest.raises(ValidationError) as image:
        store(owner_type="exercise", owner_id=exercise.pk, slot_key="exercise.poster",
              upload=jpeg_upload(width, height), alt_en="x" * 201)
    assert _refusal_code(image) == "media_alt_too_long"

    assert _stored_files(media_root) == [], "a refused write left bytes behind"


def test_s103_a_write_that_fails_after_storing_takes_its_files_back(
        make_user, media_root, monkeypatch):
    """Whatever fails after the bytes are stored — a model check, the database — the
    row rolls back, and the bytes must go with it."""
    from mediakit.models import MediaAsset
    from mediakit.services import store
    from mediakit.slots import resolve
    from mediakit.video import store_video
    from routine.models import Exercise

    real = MediaAsset.full_clean

    def fails_once_the_files_exist(self, exclude=None, **kwargs):
        if not exclude:
            raise ValidationError("refused after storing", code="late")
        return real(self, exclude=exclude, **kwargs)

    monkeypatch.setattr(MediaAsset, "full_clean", fails_once_the_files_exist)
    owner = make_user("fxm_s103b", user_type="trainer")
    exercise = Exercise.objects.create(name="fxm s103b", created_by=owner)

    with pytest.raises(ValidationError):
        store_video(owner_type="exercise", owner_id=exercise.pk, slot_key="exercise.loop",
                    upload=video_upload(), uploaded_by=owner)
    width, height = resolve("exercise.poster").min_size
    with pytest.raises(ValidationError):
        store(owner_type="exercise", owner_id=exercise.pk, slot_key="exercise.poster",
              upload=jpeg_upload(width, height))

    assert _stored_files(media_root) == []
    assert not MediaAsset.objects.filter(owner_id=exercise.pk).exists()


def test_s103_the_route_answers_the_documented_code(make_user, api, media_root):
    from routine.models import Exercise

    owner = make_user("fxm_s103c", user_type="trainer")
    exercise = Exercise.objects.create(name="fxm s103c", created_by=owner)

    response = api(owner).post(
        "/api/v1/content/media/video/",
        {"file": video_upload(), "slot": "exercise.loop", "owner_type": "exercise",
         "owner_id": str(exercise.pk), "alt_en": "x" * 201})

    assert response.status_code == 400, response.content
    assert response.json()["code"] == "media_alt_too_long"
    assert _stored_files(media_root) == []


# =============================================================================== S-104
def test_s104_the_banner_follows_the_same_store_rule_as_home(api):
    """Home removes `app.banner` on any build that may not show prices, because nothing
    on the row says whether it advertises a purchase. The public media route served the
    same picture to every build, `play` and `ios` included."""
    from mediakit.services import store
    from mediakit.slots import resolve

    width, height = resolve("app.banner").min_size
    store(owner_type="app", owner_id=0, slot_key="app.banner",
          upload=jpeg_upload(width, height))
    width, height = resolve("app.story.1").min_size
    store(owner_type="app", owner_id=0, slot_key="app.story.1",
          upload=jpeg_upload(width, height))

    def media(**headers):
        response = api().get("/api/v1/content/media/", **headers)
        assert response.status_code == 200, response.content
        return response

    for store_build in ("play", "ios"):
        answer = media(HTTP_X_APP_CHANNEL=store_build).json()["media"]
        assert "app.banner" not in answer, store_build
        assert "app.story.1" in answer, "only the banner is build-dependent"
    # `conftest.speaks_as_the_direct_apk` declares `apk` unless told otherwise; an empty
    # header is how a test says "this caller declares no build".
    assert "app.banner" not in media(HTTP_X_APP_CHANNEL="").json()["media"], \
        "no channel is the strictest"

    direct = media(HTTP_X_APP_CHANNEL="apk")
    assert "app.banner" in direct.json()["media"]
    vary = {v.strip().lower() for v in direct["Vary"].split(",")}
    assert {"x-app-channel", "x-app-version"} <= vary


# =============================================================================== S-148
def _prod_env():
    """A production environment that passes every other check of the gate."""
    from cryptography.fernet import Fernet

    return {
        "DJANGO_ALLOWED_HOSTS": "api.example.com",
        "DB_NAME": "tp", "DB_USER": "tp", "DB_PASSWORD": "x" * 20, "DB_HOST": "db.internal",
        "REDIS_URL": "redis://redis.internal:6379",
        "CELERY_BROKER_URL": "redis://redis.internal:6379/6",
        "EMAIL_HOST_USER": "a@example.com", "EMAIL_HOST_PASSWORD": "y" * 12,
        # Deliberate contract change (fx/prod-services, 2026-09-27): the SMTP host and the
        # From address come from the environment now that Gmail is not hard-coded
        # (tests/test_prod_services.py); required, so this environment carries them.
        "EMAIL_HOST": "smtp.mail-provider.test",
        "DEFAULT_FROM_EMAIL": "Yalla Gym <no-reply@yallagym.test>",
        "CORS_ALLOWED_ORIGINS": "https://app.example.com",
        "CSRF_TRUSTED_ORIGINS": "https://app.example.com",
        "DJANGO_SECRET_KEY": "s" * 64,
        "JWT_PRIVATE_KEY": "-----BEGIN PRIVATE KEY-----\nnot-a-key\n-----END PRIVATE KEY-----",
        "JWT_PUBLIC_KEY": "-----BEGIN PUBLIC KEY-----\nnot-a-key\n-----END PUBLIC KEY-----",
        "FIELD_ENCRYPTION_KEY": Fernet.generate_key().decode(),
        # Required since the owner's decision of 2026-09-24 (anonymous AI training
        # copies are keyed with it); generated, never a literal.
        "AI_TRAINING_PSEUDONYM_KEY": secrets.token_hex(32),
        "MEDIA_BACKEND": "gcs", "GS_BUCKET_NAME": "yalla-gym-media",
        # Deliberate contract change (fx/w1-privacy, BACKEND_BUGS B-174, the owner's
        # decision of 2026-09-28): production refuses to boot without an https base for
        # the public pages; required, so this environment carries one.
        "PUBLIC_PAGES_BASE_URL": "https://yallagym.example",
    }


def _boot(env, store=None):
    """Import settings_production in a clean process and run its gate.

    `store` stands in for AWS Secrets Manager: `get_secret` memoises the fetched secret
    dict on itself, so seeding that dict IS the store, with no network and no boto3.
    """
    seed = ""
    if store is not None:
        seed = ("import training_platform.settings_secrets as ss; "
                f"ss.get_secret._aws_secrets = {store!r}; ")
    code = (seed + "import training_platform.settings_production as s; "
            "s.enforce_production_safety(); print('BOOTED')")
    return subprocess.run([sys.executable, "-c", code], cwd=str(ROOT),
                          env={"PATH": os.environ.get("PATH", ""), **env},
                          capture_output=True, text=True, timeout=120)


GCS_KEY = json.dumps({"type": "service_account",
                      "client_email": "media@example.iam.gserviceaccount.com",
                      "private_key": "-----BEGIN PRIVATE KEY-----\nnot-a-key\n"
                                     "-----END PRIVATE KEY-----\n"})


def test_s148_a_secret_store_without_the_key_does_not_boot():
    """`AWS_SECRET_NAME` being set was taken as proof the key exists. A store without it
    booted green and then every media URL failed to sign — a 500 on every screen with
    media."""
    env = _prod_env()
    store = dict(env)                      # the store holds every secret but the key
    refused = _boot({**env, "AWS_SECRET_NAME": "prod/app"}, store=store)
    assert refused.returncode != 0, refused.stdout
    assert "GS_CREDENTIALS_JSON" in refused.stderr
    assert "BOOTED" not in refused.stdout

    booted = _boot({**env, "AWS_SECRET_NAME": "prod/app"},
                   store={**store, "GS_CREDENTIALS_JSON": GCS_KEY})
    assert booted.returncode == 0, booted.stderr

    # A value that is there but cannot sign is refused the same way, from either source.
    empty = _boot({**env, "GS_CREDENTIALS_JSON": "{}"})
    assert empty.returncode != 0 and "private_key" in empty.stderr


def test_s148_the_storage_refuses_to_run_without_a_signing_key(settings):
    """It logged a warning and fell back to ambient credentials, which cannot sign."""
    from django.core.exceptions import ImproperlyConfigured

    from training_platform import gcs_storage

    settings.GS_CREDENTIALS_SECRET_NAME = "FXM_NO_SUCH_GCS_KEY"
    os.environ.pop("FXM_NO_SUCH_GCS_KEY", None)
    with pytest.raises(ImproperlyConfigured):
        gcs_storage._credentials()


# =============================================================================== S-081
def test_s081_the_shared_gcs_host_is_not_on_the_list():
    """`storage.googleapis.com` answers for EVERY bucket, so allowing the host allowed
    any coach's own public bucket."""
    from django.conf import settings

    assert "storage.googleapis.com" not in settings.EXERCISE_MEDIA_ALLOWED_HOSTS

    from training_platform.settings_base import _exercise_media_allowed_hosts

    assert _exercise_media_allowed_hosts("", "yalla-gym") == (
        "storage.googleapis.com/yalla-gym", "yalla-gym.storage.googleapis.com")
    assert _exercise_media_allowed_hosts("", "") == ()
    assert _exercise_media_allowed_hosts("cdn.example.com", "b") == (
        "cdn.example.com", "storage.googleapis.com/b", "b.storage.googleapis.com")


@pytest.mark.parametrize("url,allowed", [
    ("https://storage.googleapis.com/yalla-gym/demo.mp4", True),
    ("https://yalla-gym.storage.googleapis.com/demo.mp4", True),
    ("https://storage.googleapis.com/evil-bucket/demo.mp4", False),
    ("https://storage.googleapis.com/yalla-gym-evil/demo.mp4", False),
    ("https://storage.googleapis.com/yalla-gym", False),
    ("https://storage.googleapis.com/yalla-gym/../evil-bucket/demo.mp4", False),
    ("https://storage.googleapis.com/yalla-gym/%2e%2e/evil-bucket/demo.mp4", False),
    ("https://storage.googleapis.com/yalla-gym/..%5Cevil-bucket/demo.mp4", False),
    ("https://evil.storage.googleapis.com/demo.mp4", False),
    ("https://storage.googleapis.com@evil.example.net/yalla-gym/demo.mp4", False),
])
def test_s081_only_the_projects_bucket_is_allowed(settings, url, allowed):
    from routine.views import _media_host_allowed

    settings.EXERCISE_MEDIA_ALLOWED_HOSTS = (
        "storage.googleapis.com/yalla-gym", "yalla-gym.storage.googleapis.com")
    assert _media_host_allowed(url) is allowed


def test_s081_a_coach_cannot_point_clients_at_their_own_bucket(make_user, api):
    """Through the route, with the allow-list this deployment actually computed."""
    from routine.models import Exercise, ExerciseMedia

    trainer = make_user("fxm_s081", user_type="trainer")
    exercise = Exercise.objects.create(name="fxm s081", created_by=trainer)

    response = api(trainer).post(
        f"/api/routine/exercises/{exercise.pk}/add-media/",
        json.dumps({"media_items": [
            {"media_type": "video", "title": "mine",
             "content": "https://storage.googleapis.com/coach-own-bucket/cue.mp4"}]}),
        content_type="application/json")

    assert response.status_code == 207, response.content
    assert [row["code"] for row in response.json()["errors_detail"]] == \
        ["media_host_not_allowed"]
    assert not ExerciseMedia.objects.filter(exercise=exercise).exists()
