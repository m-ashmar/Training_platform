"""SIDE_BUGS S-210 and S-209, each proved here.

  S-210 [HIGH]  the platform's image guard (`file_security.process_uploaded_image`, behind
                the profile picture, chat photos, exercise images, social posts and the
                media kit) capped an image at 40 MP whatever its format, and decoded it
                whole: a 106 KB WebP of 6320 x 6320 added 983 MB, a 40 MP colour JPEG
                243 MB, and the media kit then decoded the upload again and copied it
                three times over. One small upload could OOM-kill a 512 MB web machine.
                Now every decode is priced from the header before a pixel is decoded, by
                the one estimator verification documents use too
                (`training_platform/image_budget.py`).
  S-209         a verification document nobody reviewed was deleted row first when the
                trainer replaced or removed it, and its files after the commit by a
                receiver that can only log a storage error: the identity photo stayed in
                the bucket for good, with no row pointing at it.

Every test here was run against the code before the fix (20418df) and failed there,
but one: `test_s209_a_replacement_that_rolls_back_deletes_nothing` passes there too, as it
should — it guards the fix's own risk (deleting files inside a transaction that can still
roll back), which the old row-first order did not have.

Resident memory is measured as the verification review measured it (`ru_maxrss` of a
fresh child). On macOS under memory pressure the compressor shrinks the pages of these
flat-colour images, so a number read there is a floor; on Linux (glibc, no compressor)
the worst accepted input of every kind measured 129 MB through the guard and 119 MB
through the media kit.
"""
import io
import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]

#: What one upload may add to a web process at its peak (fly.toml: 512 MB machines, a
#: Django process already at about 110 MB).
MAX_EXTRA_RSS = 150 * 1024 * 1024


def _subprocess_env():
    env = dict(os.environ)
    env.setdefault("DJANGO_SETTINGS_MODULE", "training_platform.settings_local")
    env["PYTHONPATH"] = str(ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    return env


# ===========================================================================
# S-210 — the inputs, made in a child process so this one never holds a 40 MP canvas
# ===========================================================================

_MAKE_IMAGES = textwrap.dedent("""
    import sys
    from PIL import Image

    out = sys.argv[1]
    rgb, rgba, cmyk = (40, 60, 80), (40, 60, 80, 128), (10, 20, 30, 40)

    def exif(orientation=None, gps=False):
        tags = Image.Exif()
        if orientation:
            tags[0x0112] = orientation
        if gps:
            tags[0x8825] = {1: "N", 2: (33.0, 30.0, 0.0), 3: "E", 4: (36.0, 17.0, 0.0)}
        return tags.tobytes()

    def save(name, img, fmt, **kw):
        img.save(f"{out}/{name}", fmt, **kw)
        img.close()

    def animation(name, side, frames):
        # Black and white in turn, so every frame differs and Pillow keeps each one.
        pictures = [Image.new("P", (side, side), n % 2) for n in range(frames)]
        for picture in pictures:
            picture.putpalette([0, 0, 0, 255, 255, 255] + [0] * 762)
        pictures[0].save(f"{out}/{name}", "GIF", save_all=True,
                         append_images=pictures[1:], duration=100, loop=0)

    # The guard: the worst each kind of file may send and still be taken.
    save("jpg_rgb_full_max.jpg", Image.new("RGB", (5060, 3788), rgb), "JPEG")
    save("jpg_rgb_40.jpg", Image.new("RGB", (7300, 5475), rgb), "JPEG",
         exif=exif(gps=True))
    save("jpg_l_40.jpg", Image.new("L", (7300, 5475), 40), "JPEG")
    save("jpg_cmyk_full_max.jpg", Image.new("CMYK", (4000, 3050), cmyk), "JPEG")
    save("jpg_cmyk_40.jpg", Image.new("CMYK", (7300, 5475), cmyk), "JPEG")
    save("jpg_prog420_max.jpg", Image.new("RGB", (3640, 3640), rgb), "JPEG",
         progressive=True, subsampling=2)
    save("jpg_prog444_max.jpg", Image.new("RGB", (3200, 3200), rgb), "JPEG",
         progressive=True, subsampling=0)
    save("jpg_prog_l_max.jpg", Image.new("L", (6000, 4400), 40), "JPEG", progressive=True)
    save("jpg_prog_cmyk_max.jpg", Image.new("CMYK", (2650, 2650), cmyk), "JPEG",
         progressive=True)
    save("png_rgba_max.png", Image.new("RGBA", (6700, 5000), rgba), "PNG")
    save("png_la_max.png", Image.new("LA", (3340, 3340), (40, 128)), "PNG")
    save("png_i16_max.png", Image.new("I;16", (3660, 3660), 400), "PNG")
    save("png_p_40.png", Image.new("P", (7300, 5475), 0), "PNG", transparency=0)
    save("webp_rgb_max.webp", Image.new("RGB", (2590, 2590), rgb), "WEBP",
         quality=20, method=0)
    save("webp_rgba_max.webp", Image.new("RGBA", (2189, 2189), rgba), "WEBP",
         quality=20, method=0)
    save("webp_rgba_lossless_max.webp", Image.new("RGBA", (2189, 2189), rgba), "WEBP",
         lossless=True)
    save("gif_still_40.gif", Image.new("P", (7300, 5475), 0), "GIF")
    animation("gif_anim2_max.gif", 2364, 2)
    animation("gif_anim300_max.gif", 365, 300)

    # The media kit, after the guard: its own decode, turn, grade and renditions.
    save("kit_poster_jpg_40.jpg", Image.new("RGB", (8432, 4743), rgb), "JPEG")
    save("kit_poster_jpg_full_max_o6.jpg", Image.new("RGB", (2508, 4458), rgb), "JPEG",
         exif=exif(orientation=6))
    save("kit_poster_cmyk_40.jpg", Image.new("CMYK", (8432, 4743), cmyk), "JPEG")
    save("kit_poster_prog420_max.jpg", Image.new("RGB", (3980, 2239), rgb), "JPEG",
         progressive=True, subsampling=2)
    save("kit_poster_png_la_max.png", Image.new("LA", (4458, 2508), (40, 128)), "PNG")
    save("kit_poster_webp_max.webp", Image.new("RGB", (3152, 1773), rgb), "WEBP",
         quality=20, method=0)
    save("kit_cutout_png_rgba_max.png", Image.new("RGBA", (2896, 3862), rgba), "PNG")
    save("kit_cutout_png_p_max.png", Image.new("P", (2896, 3862), 0), "PNG",
         transparency=0)
    save("kit_cutout_webp_rgba_max.webp", Image.new("RGBA", (1896, 2528), rgba), "WEBP",
         quality=20, method=0)
    save("kit_dish_png_max.png", Image.new("RGB", (2990, 3738), rgb), "PNG")

    # The reviewer's inputs, and the first size over each kind's limit.
    save("review_webp_6320.webp", Image.new("RGB", (6320, 6320), rgb), "WEBP",
         quality=20, method=0)
    save("review_png_rgba_40.png", Image.new("RGBA", (7300, 5475), rgba), "PNG")
    save("review_jpg_prog444_40.jpg", Image.new("RGB", (7300, 5475), rgb), "JPEG",
         progressive=True, subsampling=0)
    animation("review_gif_anim2_20.gif", 4472, 2)
    save("over_png_rgba.png", Image.new("RGBA", (6700, 5010), rgba), "PNG")
    save("over_webp_rgba.webp", Image.new("RGBA", (2190, 2190), rgba), "WEBP",
         quality=20, method=0)
    save("over_webp_rgb.webp", Image.new("RGB", (2600, 2600), rgb), "WEBP",
         quality=20, method=0)
    animation("over_gif_anim2.gif", 2366, 2)
""")

#: (file, what is stored): its size, and for an animation its frame count.
GUARD_ACCEPTED = {
    "jpg_rgb_full_max.jpg": (5060, 3788),     # the most a colour JPEG decodes at full size
    "jpg_rgb_40.jpg": (3650, 2738),           # 40 MP: decoded at half size (`draft`)
    "jpg_l_40.jpg": (7300, 5475),             # grey costs a third: full size
    "jpg_cmyk_full_max.jpg": (4000, 3050),
    "jpg_cmyk_40.jpg": (3650, 2738),
    "jpg_prog420_max.jpg": (3640, 3640),      # progressive: its coefficients at full size
    "jpg_prog444_max.jpg": (3200, 3200),
    "jpg_prog_l_max.jpg": (6000, 4400),
    "jpg_prog_cmyk_max.jpg": (2650, 2650),
    "png_rgba_max.png": (6700, 5000),
    "png_la_max.png": (3340, 3340),           # converted to RGBA on the way
    "png_i16_max.png": (3660, 3660),
    "png_p_40.png": (7300, 5475),
    "webp_rgb_max.webp": (2590, 2590),        # the decoder's canvases, and the encoder's
    "webp_rgba_max.webp": (2189, 2189),       # alpha plane
    "webp_rgba_lossless_max.webp": (2189, 2189),
    "gif_still_40.gif": (7300, 5475),
    "gif_anim300_max.gif": (365, 365, 300),   # every frame held, and the canvas per frame
    # The largest two-frame animation the budget takes. Pillow's GIF writer then refuses
    # it (`unreadable_image`: an animation over 512 x 512 has an RGBA palette it cannot
    # put on its background frame, SIDE_BUGS S-211), after every frame was decoded —
    # which is what is measured here. None: only the budget's verdict is asserted.
    "gif_anim2_max.gif": None,
}

#: (file, slot): what the media kit stores.
KIT_ACCEPTED = {
    ("kit_poster_jpg_40.jpg", "exercise.poster"): (4216, 2372),
    ("kit_poster_jpg_full_max_o6.jpg", "exercise.poster"): (4458, 2508),   # turned upright
    ("kit_poster_cmyk_40.jpg", "exercise.poster"): (4216, 2372),
    ("kit_poster_prog420_max.jpg", "exercise.poster"): (3980, 2239),
    ("kit_poster_png_la_max.png", "exercise.poster"): (4458, 2508),
    ("kit_poster_webp_max.webp", "exercise.poster"): (3152, 1773),
    ("kit_cutout_png_rgba_max.png", "trainer.cutout"): (2896, 3862),
    ("kit_cutout_png_p_max.png", "trainer.cutout"): (2896, 3862),
    ("kit_cutout_webp_rgba_max.webp", "trainer.cutout"): (1896, 2528),
    ("kit_dish_png_max.png", "dish.top"): (2990, 3738),
}

REVIEWER = ["review_webp_6320.webp", "review_png_rgba_40.png",
            "review_jpg_prog444_40.jpg", "review_gif_anim2_20.gif"]
OVER = ["over_png_rgba.png", "over_webp_rgba.webp", "over_webp_rgb.webp",
        "over_gif_anim2.gif"]


@pytest.fixture(scope="module")
def big_images(tmp_path_factory):
    out = tmp_path_factory.mktemp("guard_images")
    subprocess.run([sys.executable, "-c", _MAKE_IMAGES, str(out)], check=True,
                   timeout=600)
    return out


_MEASURE = textwrap.dedent("""
    import io, json, resource, sys
    import django
    django.setup()
    from django.core.exceptions import ValidationError
    from django.core.files import File
    from training_platform import file_security
    from mediakit.processing import process_upload
    from mediakit.slots import resolve
    import PIL.Image, PIL.GifImagePlugin, PIL.JpegImagePlugin, PIL.MpoImagePlugin, \\
        PIL.PngImagePlugin, PIL.WebPImagePlugin  # noqa: loaded before the baseline

    def peak():
        used = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        return used if sys.platform == "darwin" else used * 1024

    path, target = sys.argv[1], sys.argv[2]
    result = {"file": path.rsplit("/", 1)[-1]}
    base = peak()
    with open(path, "rb") as handle:
        upload = File(handle, name=result["file"])
        try:
            if target == "guard":
                stored, _ext = file_security.process_uploaded_image(
                    upload, max_bytes=5 * 1024 * 1024, quarantine=False)
                result["peak_extra"] = peak() - base
                with PIL.Image.open(io.BytesIO(stored.read())) as out:
                    result["stored"] = list(out.size)
                    result["frames"] = getattr(out, "n_frames", 1)
                    result["exif"] = len(out.getexif())
            else:
                done = process_upload(upload, resolve(target))
                result["peak_extra"] = peak() - base
                result["stored"] = [done.width, done.height]
        except ValidationError as exc:
            result["peak_extra"] = peak() - base
            result["code"] = exc.code
    print(json.dumps(result))
""")


def _measure(path, target="guard"):
    run = subprocess.run([sys.executable, "-c", _MEASURE, str(path), target], cwd=ROOT,
                         env=_subprocess_env(), capture_output=True, text=True,
                         timeout=180)
    assert run.returncode == 0, run.stderr[-3000:]
    return json.loads(run.stdout.strip().splitlines()[-1])


def _report(lines):
    return ", ".join(f"{line['file']}: {line['peak_extra'] // 2**20} MB" for line in lines)


def test_s210_the_worst_accepted_image_of_every_kind_adds_under_150_mb_to_the_guard(
        big_images):
    """Peak resident memory one upload adds through the guard, each input in a fresh
    process: the largest image of each kind (format, and mode where the mode changes
    the cost) that is still taken. Before the fix a 40 MP colour JPEG added 243 MB, a
    40 MP CMYK one 386 MB and a WebP of 106 KB 983 MB; none may add 150 MB now."""
    lines = [_measure(big_images / name) for name in GUARD_ACCEPTED]
    assert max(line["peak_extra"] for line in lines) < MAX_EXTRA_RSS, _report(lines)
    assert not [line for line in lines if line.get("code") == "image_too_many_pixels"]
    for line in lines:
        expected = GUARD_ACCEPTED[line["file"]]
        if expected is None:
            continue
        assert "code" not in line, line
        assert tuple(line["stored"]) == expected[:2], line
        assert line["frames"] == (expected[2] if len(expected) > 2 else 1), line
        assert line["exif"] == 0, line      # the re-encode still carries no metadata


def test_s210_the_worst_accepted_image_of_every_kind_adds_under_150_mb_to_the_media_kit(
        big_images):
    """The media kit runs the guard and then decodes the upload again, turns it upright,
    grades it and cuts two renditions. It held the image four times over at full size;
    now it prices its own decode too and holds one working copy."""
    lines = []
    for (name, slot), size in KIT_ACCEPTED.items():
        line = _measure(big_images / name, slot)
        assert "code" not in line, line
        assert tuple(line["stored"]) == size, line
        lines.append(line)
    assert max(line["peak_extra"] for line in lines) < MAX_EXTRA_RSS, _report(lines)


def test_s210_the_reviewers_inputs_are_refused_and_cost_nothing(big_images):
    """The reviewer's 106 KB WebP of 6320 x 6320, a 170 KB RGBA PNG of 40 MP, a 40 MP
    progressive JPEG and two 20 MP frames of GIF — and the first size past each kind's
    limit — are refused with the documented code, having added next to nothing."""
    lines = [_measure(big_images / name) for name in REVIEWER + OVER]
    assert all(line.get("code") == "image_too_many_pixels" for line in lines), lines
    assert max(line["peak_extra"] for line in lines) < 16 * 1024 * 1024, _report(lines)


@pytest.fixture
def decoding_forbidden(monkeypatch):
    """Any decode fails the test; so does opening a WebP (Pillow creates libwebp's
    decoder, canvases and all, at `Image.open`)."""
    from PIL import ImageFile, WebPImagePlugin

    def decoded(self, *args, **kwargs):
        raise AssertionError(f"decoded {self.size}")

    opened = []
    original = WebPImagePlugin.WebPImageFile._open

    def webp_open(self):
        opened.append(True)
        return original(self)

    monkeypatch.setattr(ImageFile.ImageFile, "load", decoded)
    monkeypatch.setattr(Image.Image, "load", decoded)
    monkeypatch.setattr(WebPImagePlugin.WebPImageFile, "_open", webp_open)
    return opened


@pytest.mark.parametrize("name", REVIEWER + OVER)
def test_s210_the_reviewers_inputs_are_refused_before_decoding(
        big_images, decoding_forbidden, name):
    from training_platform.file_security import MAX_IMAGE_DIMENSION, process_uploaded_image

    upload = SimpleUploadedFile(name, (big_images / name).read_bytes())
    with pytest.raises(ValidationError) as refused:
        process_uploaded_image(upload, max_bytes=5 * 1024 * 1024, quarantine=False)
    assert refused.value.code == "image_too_many_pixels"
    assert 0 < refused.value.params["max_pixels"] < 40_000_000
    assert refused.value.params["max_side"] == MAX_IMAGE_DIMENSION
    assert str(refused.value.params["max_pixels"] // 1_000_000) in refused.value.messages[0]
    assert decoding_forbidden == [], "a WebP was opened before it was refused"


@pytest.mark.django_db
@pytest.mark.parametrize("name", ["review_webp_6320.webp", "review_png_rgba_40.png"])
def test_s210_the_profile_picture_route_answers_the_reviewers_upload_with_its_code(
        big_images, make_user, api, name):
    user = make_user(f"s210_{name.split('.')[0]}")
    upload = SimpleUploadedFile(name, (big_images / name).read_bytes())
    response = api(user).post("/api/auth/user/profile-picture/",
                              {"profile_picture": upload})
    assert response.status_code == 400, response.content
    assert response.json()["code"] == "image_too_many_pixels"
    user.refresh_from_db()
    assert not user.profile_picture


def test_s210_one_estimator_prices_every_decode():
    """The guard, the media kit and verification documents price a decode with the same
    code, so a lesson learned on one path — a decoder's hidden buffers — reaches all
    three (verification kept its own copy until now)."""
    import inspect

    from mediakit import processing
    from trainer_verification import files
    from training_platform import file_security, image_budget

    for module in (file_security, processing, files):
        assert "image_budget.check(" in inspect.getsource(module), module.__name__
    assert files.DECODE_BUDGET_BYTES == image_budget.DECODE_BUDGET_BYTES
    assert files.WEBP_EXTRA_BYTES_PER_PIXEL == image_budget.WEBP_EXTRA_BYTES_PER_PIXEL
    assert not hasattr(files, "_coefficient_bytes")
    assert image_budget.DECODE_BUDGET_BYTES <= 128 * 1024 * 1024


@pytest.mark.parametrize("mode,options", [
    ("RGB", {"quality": 20}),                       # VP8: a lossy key frame
    ("RGB", {"lossless": True}),                    # VP8L
    ("RGBA", {"lossless": True}),                   # VP8L with alpha
    ("RGBA", {"quality": 20}),                      # VP8X: alpha
    ("RGB", {"quality": 20, "exif": b"Exif\x00\x00II*\x00\x08\x00\x00\x00\x00\x00"}),
])
@pytest.mark.parametrize("size", [(1, 1), (641, 479), (16383, 2)])
def test_s210_a_webp_canvas_is_read_from_its_header(mode, options, size):
    """What Pillow would open the file as — its size and mode — from 30 bytes."""
    from training_platform.image_budget import webp_header

    out = io.BytesIO()
    Image.new(mode, size, (1, 2, 3, 0) if mode == "RGBA" else (1, 2, 3)).save(
        out, "WEBP", **options)
    width, height, alpha = webp_header(out.getvalue()[:32])
    with Image.open(io.BytesIO(out.getvalue())) as opened:
        assert (width, height) == size == opened.size
        assert ("RGBA" if alpha else "RGB") == opened.mode == mode
    assert webp_header(b"\x89PNG\r\n\x1a\n" + bytes(24)) is None
    assert webp_header(out.getvalue()[:20]) is None


# ===========================================================================
# S-209 — a document nobody reviewed goes files first, and never inside a rollback
# ===========================================================================

@pytest.fixture
def storage_roots(settings, tmp_path):
    settings.MEDIA_ROOT = str(tmp_path / "media")
    settings.PRIVATE_MEDIA_ROOT = str(tmp_path / "private")
    return tmp_path


@pytest.fixture
def storage(monkeypatch):
    """The documents' storage, which the test can make refuse every delete — as GCS or a
    full disk can — and heal. Each refusal notes whether the row still existed."""
    from trainer_verification.models import VerificationDocument
    from trainer_verification.storage import PrivateDocumentStorage

    original = PrivateDocumentStorage.delete

    class Switch:
        def __init__(self):
            self.attempts = []
            self.rows_at_delete = []

        def break_(self):
            def refuse(_storage, name):
                self.attempts.append(name)
                raise OSError("storage unavailable")
            monkeypatch.setattr(PrivateDocumentStorage, "delete", refuse)

        def watch(self, document_id):
            def delete(storage_, name):
                self.rows_at_delete.append(
                    VerificationDocument.objects.filter(pk=document_id).exists())
                return original(storage_, name)
            monkeypatch.setattr(PrivateDocumentStorage, "delete", delete)

        def heal(self):
            monkeypatch.setattr(PrivateDocumentStorage, "delete", original)

    return Switch()


def _photo(make_user, api):
    from tests.test_trainer_verification import _tag, jpeg, post_document
    from trainer_verification.models import VerificationDocument

    trainer = make_user(_tag("s209"), user_type="trainer")
    client = api(trainer)
    assert post_document(client, "photo", jpeg()).status_code == 200
    document = VerificationDocument.objects.get(application__trainer=trainer)
    assert not document.was_reviewed
    return trainer, client, document


def _names(document):
    return [document.file.name, document.thumbnail.name]


def _replace(client):
    from tests.test_trainer_verification import jpeg, post_document
    return post_document(client, "photo", jpeg())


def _remove(client):
    from tests.test_trainer_verification import BASE
    return client.delete(f"{BASE}documents/photo/")


@pytest.mark.django_db
@pytest.mark.parametrize("change", [_replace, _remove], ids=["replaced", "removed"])
def test_s209_a_storage_error_keeps_the_row_so_the_purge_retries(
        make_user, api, django_capture_on_commit_callbacks, storage_roots, storage,
        change):
    """Row first, the files after the commit by a receiver that only logs: a storage
    error lost the only record of where the identity photo was. Files first, the row
    is kept when storage refuses, and the daily purge deletes both once it can."""
    from datetime import timedelta

    from django.utils import timezone

    from trainer_verification import services
    from trainer_verification.models import VerificationDocument

    _trainer, client, document = _photo(make_user, api)
    names = _names(document)
    storage.break_()
    with django_capture_on_commit_callbacks(execute=True):
        assert change(client).status_code == 200
    storage.heal()

    assert storage.attempts
    kept = VerificationDocument.objects.filter(pk=document.pk).first()
    assert kept is not None, "the row went while its files stayed"
    assert kept.replaced_at is not None and kept.purge_after <= timezone.now()
    assert all((storage_roots / "private" / n).exists() for n in names)

    with django_capture_on_commit_callbacks(execute=True):
        assert services.purge_replaced_documents(
            now=timezone.now() + timedelta(seconds=1)) == 1
    assert not VerificationDocument.objects.filter(pk=document.pk).exists()
    assert not any((storage_roots / "private" / n).exists() for n in names)


@pytest.mark.django_db
@pytest.mark.parametrize("change", [_replace, _remove], ids=["replaced", "removed"])
def test_s209_the_files_go_before_the_row_and_both_go_at_once(
        make_user, api, django_capture_on_commit_callbacks, storage_roots, storage,
        change):
    from trainer_verification.models import VerificationDocument

    _trainer, client, document = _photo(make_user, api)
    names = _names(document)
    storage.watch(document.pk)
    with django_capture_on_commit_callbacks(execute=True):
        assert change(client).status_code == 200
    assert storage.rows_at_delete == [True, True], storage.rows_at_delete
    assert not VerificationDocument.objects.filter(pk=document.pk).exists()
    assert not any((storage_roots / "private" / n).exists() for n in names)


@pytest.mark.django_db
def test_s209_a_replacement_that_rolls_back_deletes_nothing(
        make_user, api, django_capture_on_commit_callbacks, storage_roots, monkeypatch):
    """Nothing is deleted inside the transaction: a replacement that fails after the
    old document was retired leaves it current, with its files."""
    from tests.test_trainer_verification import jpeg
    from trainer_verification import services, slots
    from trainer_verification.models import VerificationDocument

    trainer, _client, document = _photo(make_user, api)
    names = _names(document)
    original = VerificationDocument.save

    def fail_the_new_row(self, *args, **kwargs):
        if self.pk is None:
            raise RuntimeError("the database went away")
        return original(self, *args, **kwargs)

    monkeypatch.setattr(VerificationDocument, "save", fail_the_new_row)
    with django_capture_on_commit_callbacks(execute=True):
        with pytest.raises(RuntimeError):
            services.upload_document(trainer, slots.BY_KEY["photo"].key, jpeg())
    monkeypatch.setattr(VerificationDocument, "save", original)

    document.refresh_from_db()
    assert document.replaced_at is None
    assert all((storage_roots / "private" / n).exists() for n in names)
