"""Trainer verification, the review of 2026-09-24: six confirmed findings, each proved here.

An adversarial reviewer found them and a verifier failed to refute them. In order:

  F1 [HIGH]   the PDF check's stream-body regex was quadratic, and a 10 MB upload of
              unterminated `stream` markers held the GIL for hours;
  F2          a 170 KB PNG took a web machine past its 512 MB: every image was decoded at
              up to 40 MP and then copied three times at full size;
  F3          a revoke skipped the "documents changed since the page was opened" guard, so
              a stale console page could revoke a verification granted since;
  F4          a revoke left every document `accepted`, so the one it was revoked for was
              neither flagged nor blocked, and could be sent again unchanged;
  F5          the purge (and account erasure) deleted the row before the file, so a
              storage error orphaned an identity document for ever;
  F6          the design promises the optional nutrition certificate "raises your ranking";
              nothing does, and the contract did not say so.

Each test was run against the code before its fix and failed there.
"""
import io
import json
import os
import re
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from PIL import Image

from tests.test_trainer_verification import (
    BASE,
    _application,
    _operator,
    _submitted,
    _tag,
    _trainer,
    jpeg,
    jpeg_bytes,
    pdf,
    png,
    post_document,
)

pytestmark = pytest.mark.django_db

ROOT = Path(__file__).resolve().parents[1]
CHANGE = "/dj-admin/trainer_verification/verificationapplication/{}/change/"


@pytest.fixture(autouse=True)
def storage_roots(settings, tmp_path):
    """Both roots in a temporary directory, and apart, as in production."""
    settings.MEDIA_ROOT = str(tmp_path / "media")
    settings.PRIVATE_MEDIA_ROOT = str(tmp_path / "private")
    return tmp_path


def _console(user):
    from django.test import Client
    client = Client()
    client.force_login(user)
    return client


def _subprocess_env():
    env = dict(os.environ)
    env.setdefault("DJANGO_SETTINGS_MODULE", "training_platform.settings_local")
    env["PYTHONPATH"] = str(ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    return env


# ===========================================================================
# F1 — the PDF scan is linear
# ===========================================================================

_TIMED_PDF = textwrap.dedent("""
    import json, sys, time
    from django.core.files import File
    from trainer_verification import files, slots

    with open(sys.argv[1], "rb") as handle:
        upload = File(handle, name="certificate.pdf")
        started = time.perf_counter()
        try:
            result = files.process(upload, slots.BY_KEY["trainer_certificate"])
            outcome = result.kind
        except files.DocumentRefused as exc:
            outcome = exc.code
        elapsed = time.perf_counter() - started
    print(json.dumps({"elapsed": elapsed, "outcome": outcome, "size": upload.size}))
""")


def test_f1_ten_megabytes_of_unterminated_stream_markers_are_answered_at_once(tmp_path):
    """The reviewer's file: `%PDF-1.7`, then `stream\\n` over and over, then `%%EOF`.

    With `stream\\r?\\n.*?endstream` and no `endstream` anywhere, every marker scanned to
    the end of the data: 280 KB took 36 s, and the 10 MB cap about twelve hours, inside
    one C call that holds the GIL — the whole Daphne process stopped answering. Run in a
    child process so the old code fails this test on a timeout instead of hanging it.
    """
    from trainer_verification import files

    limit = files.max_bytes()
    head, tail, marker = b"%PDF-1.7\n", b"%%EOF\n", b"stream\n"
    count = (limit - len(head) - len(tail)) // len(marker)
    path = tmp_path / "markers.pdf"
    path.write_bytes(head + marker * count + tail)
    assert limit - 7 < path.stat().st_size <= limit

    try:
        run = subprocess.run([sys.executable, "-c", _TIMED_PDF, str(path)], cwd=ROOT,
                             env=_subprocess_env(), capture_output=True, text=True,
                             timeout=90)
    except subprocess.TimeoutExpired:
        pytest.fail("the PDF check was still scanning a 10 MB file after 90 s")
    assert run.returncode == 0, run.stderr[-2000:]
    answer = json.loads(run.stdout.strip().splitlines()[-1])
    assert answer["size"] == path.stat().st_size
    # Well under a second on a laptop; 2 s leaves room for a slow CI runner.
    assert answer["elapsed"] < 2.0, answer
    # Nothing in it names active content, as before: the file is still taken.
    assert answer["outcome"] == "pdf", answer


#: The expression the scan replaced, kept as the oracle: the linear scan must strip
#: exactly what it stripped, so no certificate is refused or accepted differently.
_OLD_STREAM_BODY = re.compile(rb"stream\r?\n.*?endstream", re.S)


@pytest.mark.parametrize("sample", [
    b"%PDF-1.4\n1 0 obj\n<< /Length 5 >>\nstream\n/JS x\nendstream\nendobj\n%%EOF\n",
    b"<< >>\nstream\r\n/Launch\r\nendstream\r\n<< /Type /Page >>\nstream\nabc\nendstream\n",
    b"stream without an end of line /JS endstream",           # not a stream keyword
    b"/A stream\n/JS no end at all /Launch",                  # unterminated: nothing stripped
    b"stream\nfirst\nendstream stream\nsecond /JS\nendstream tail /EmbeddedFile",
    b"endstream\nstream\nbody\nendstream",                    # a stray endstream first
    b"stream\n\nendstream",                                   # an empty body
    b"stream\rx endstream",                                   # a bare CR is not an EOL
    b"streamstream\nx\nendstreamendstream\n/XFA",
    b"",
])
def test_f1_the_linear_scan_strips_exactly_what_the_regex_did(sample):
    from trainer_verification import files

    assert files._without_stream_bodies(sample) == _OLD_STREAM_BODY.sub(
        b"stream endstream", sample)


def test_f1_active_content_outside_a_stream_is_still_refused_after_an_unterminated_one(
        make_user, api):
    """An unterminated `stream` hides nothing: what follows it is read as structure."""
    raw = (b"%PDF-1.4\n1 0 obj\n<< /Length 9 >>\nstream\nq 1 0 0 1\n"
           b"2 0 obj << /OpenAction << /S /JavaScript /JS (x) >> >>\n%%EOF\n")
    response = post_document(api(_trainer(make_user)), "trainer_certificate",
                             SimpleUploadedFile("c.pdf", raw))
    assert response.status_code == 400
    assert response.json()["code"] == "verification_pdf_active_content"


# ===========================================================================
# F2 — an image is never decoded past what a 512 MB machine can hold
# ===========================================================================

_MAKE_IMAGES = textwrap.dedent("""
    import sys
    from PIL import Image

    out = sys.argv[1]

    def exif(orientation):
        tags = Image.Exif()
        tags[0x0112] = orientation
        return tags.tobytes()

    def save(name, img, fmt, **kw):
        img.save(f"{out}/{name}", fmt, **kw)
        img.close()

    rgb, rgba, cmyk = (40, 60, 80), (40, 60, 80, 128), (10, 20, 30, 40)
    # Accepted: the worst of each kind.
    save("png_rgba_16.png", Image.new("RGBA", (4000, 4000), rgba), "PNG")
    save("png_p_16.png", Image.new("P", (4000, 4000), 0), "PNG", transparency=0)
    save("jpg_16_o6.jpg", Image.new("RGB", (4000, 4000), rgb), "JPEG", exif=exif(6))
    save("jpg_24_o6.jpg", Image.new("RGB", (5712, 4284), rgb), "JPEG", exif=exif(6))
    save("jpg_40.jpg", Image.new("RGB", (7300, 5475), rgb), "JPEG")
    save("jpg_cmyk_16_o6.jpg", Image.new("CMYK", (4000, 4000), cmyk), "JPEG", exif=exif(6))
    save("jpg_prog420_max_o6.jpg", Image.new("RGB", (3400, 3400), rgb), "JPEG",
         progressive=True, subsampling=2, exif=exif(6))
    save("jpg_prog444_max.jpg", Image.new("RGB", (3000, 3000), rgb), "JPEG",
         progressive=True, subsampling=0)
    save("jpg_cmyk_prog_max.jpg", Image.new("CMYK", (2800, 2800), cmyk), "JPEG",
         progressive=True)
    save("webp_rgb_max_o6.webp", Image.new("RGB", (2580, 2580), rgb), "WEBP",
         quality=20, method=0, exif=exif(6))
    save("webp_rgba_max.webp", Image.new("RGBA", (2580, 2580), rgba), "WEBP",
         quality=20, method=0)
    # Refused: over the cap for their kind.
    save("png_over.png", Image.new("RGB", (4001, 4000), rgb), "PNG")
    save("webp_over.webp", Image.new("RGB", (2600, 2600), rgb), "WEBP", quality=20, method=0)
    save("jpg_prog444_over.jpg", Image.new("RGB", (4000, 4000), rgb), "JPEG",
         progressive=True, subsampling=0)
    save("jpg_prog420_over.jpg", Image.new("RGB", (6000, 5000), rgb), "JPEG",
         progressive=True, subsampling=2)
""")

ACCEPTED = ["png_rgba_16.png", "png_p_16.png", "jpg_16_o6.jpg", "jpg_24_o6.jpg",
            "jpg_40.jpg", "jpg_cmyk_16_o6.jpg", "jpg_prog420_max_o6.jpg",
            "jpg_prog444_max.jpg", "jpg_cmyk_prog_max.jpg", "webp_rgb_max_o6.webp",
            "webp_rgba_max.webp"]
REFUSED = ["png_over.png", "webp_over.webp", "jpg_prog444_over.jpg", "jpg_prog420_over.jpg"]


@pytest.fixture(scope="module")
def big_images(tmp_path_factory):
    """Made in a child process, so this one never holds a 40 MP canvas."""
    out = tmp_path_factory.mktemp("verification_images")
    subprocess.run([sys.executable, "-c", _MAKE_IMAGES, str(out)], check=True,
                   timeout=300)
    return out


_MEASURE = textwrap.dedent("""
    import json, resource, sys
    import django
    django.setup()
    from django.core.files import File
    from trainer_verification import files, slots
    import PIL.Image, PIL.JpegImagePlugin, PIL.MpoImagePlugin, PIL.PngImagePlugin, \\
        PIL.WebPImagePlugin  # noqa: loaded before the baseline

    def peak():
        used = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        return used if sys.platform == "darwin" else used * 1024

    path = sys.argv[1]
    base = peak()
    with open(path, "rb") as handle:
        try:
            done = files.process(File(handle, name="photo"), slots.BY_KEY["photo"])
            outcome = [done.width, done.height]
        except files.DocumentRefused as exc:
            outcome = exc.code
    print(json.dumps({"file": path.rsplit("/", 1)[-1], "outcome": outcome,
                      "peak_extra": peak() - base}))
""")

#: What one upload may add to a web process at its peak (fly.toml: 512 MB machines, a
#: Django process already at about 110 MB).
MAX_EXTRA_RSS = 150 * 1024 * 1024


def test_f2_the_worst_accepted_image_of_every_kind_stays_under_150_mb(big_images):
    """Peak resident memory one upload adds, each input in a fresh process, for the worst
    input each kind of file may send. Before the fix a 16 MP WebP alone added 268 MB and
    a 40 MP RGBA PNG of 170 KB 534 MB; after it, none adds more than about 135 MB."""
    lines = []
    for name in ACCEPTED:
        run = subprocess.run([sys.executable, "-c", _MEASURE, str(big_images / name)],
                             cwd=ROOT, env=_subprocess_env(), capture_output=True,
                             text=True, timeout=120)
        assert run.returncode == 0, run.stderr[-3000:]
        lines.append(json.loads(run.stdout.strip().splitlines()[-1]))
    report = ", ".join(f"{line['file']}: {line['peak_extra'] // 2**20} MB" for line in lines)
    refused = [line for line in lines if not isinstance(line["outcome"], list)]
    assert not refused, refused
    assert max(line["peak_extra"] for line in lines) < MAX_EXTRA_RSS, report


@pytest.mark.parametrize("name,size", [
    ("jpg_24_o6.jpg", (2142, 2856)),    # an iPhone's 24 MP default, portrait: half size
    ("jpg_40.jpg", (3650, 2738)),       # the platform's 40 MP: half size
    ("jpg_16_o6.jpg", (4000, 4000)),    # at the cap: full size
])
def test_f2_a_camera_jpeg_over_the_cap_is_decoded_and_stored_at_half_size(
        big_images, name, size):
    """A baseline JPEG is decoded straight to half (or a quarter) of its size by libjpeg
    (`draft`), so a phone photo over the cap is taken, not refused, and never held at
    full size."""
    from trainer_verification import files, slots

    data = (big_images / name).read_bytes()
    done = files.process(SimpleUploadedFile("p.jpg", data), slots.BY_KEY["photo"])
    assert (done.width, done.height) == size
    with Image.open(io.BytesIO(done.content)) as stored:
        assert stored.size == size and stored.mode == "RGB"


@pytest.mark.parametrize("name", REFUSED)
def test_f2_a_png_webp_or_progressive_jpeg_over_its_cap_is_refused_before_decoding(
        big_images, name):
    """None of these can be decoded at a reduced size — a PNG and a WebP have no such
    mode, and a progressive JPEG keeps its whole coefficient table at full size — so each
    is refused from its header, with the limit that applied."""
    from trainer_verification import files, slots

    data = (big_images / name).read_bytes()
    with pytest.raises(files.DocumentRefused) as refused:
        files.process(SimpleUploadedFile("x", data), slots.BY_KEY["photo"])
    assert refused.value.code == "verification_image_too_large"
    assert 0 < refused.value.extra["max_pixels"] <= files.DOCUMENT_MAX_PIXELS


def test_f2_the_document_caps():
    from trainer_verification import files
    from training_platform.file_security import MAX_IMAGE_PIXELS

    assert files.DOCUMENT_MAX_PIXELS <= 24_000_000 < MAX_IMAGE_PIXELS
    assert files.DECODE_BUDGET_BYTES <= 128 * 1024 * 1024


@pytest.mark.parametrize("build", [
    lambda: jpeg_bytes(w=1200, h=900, exif=_orientation(6)),     # rotated
    lambda: jpeg_bytes(w=1200, h=900),                             # already upright
    lambda: png(w=1200, h=900).read(),                             # RGBA, flattened
    lambda: png(w=1200, h=900, mode="RGB").read(),
])
def test_f2_no_full_size_copy_is_made(monkeypatch, build):
    """The old path copied the decoded image three times over: `exif_transpose` (a copy
    even when nothing turns), `convert("RGB")` on an RGB image (a copy), and
    `img.copy()` for the thumbnail. None of them is needed."""
    from trainer_verification import files, slots

    copies = []
    original = Image.Image.copy

    def spy(self):
        copies.append(self.size)
        return original(self)

    monkeypatch.setattr(Image.Image, "copy", spy)
    done = files.process(SimpleUploadedFile("p", build()), slots.BY_KEY["photo"])
    assert copies == []
    assert {done.width, done.height} == {1200, 900}
    with Image.open(io.BytesIO(done.thumbnail)) as thumb:
        assert max(thumb.size) == files.THUMBNAIL_SIDE


def _orientation(value):
    tags = Image.Exif()
    tags[0x0112] = value
    return tags.tobytes()


def test_f2_the_codes_and_the_limit_are_in_the_contract():
    from trainer_verification import views

    for text in (views.DocumentView.__doc__,
                 (ROOT / "API_CONTRACT.md").read_text(encoding="utf-8")):
        assert "`max_pixels`" in text


# ===========================================================================
# F3 — a decision taken on a stale console page is refused, revoke included
# ===========================================================================

def _page(console, application):
    html = console.get(CHANGE.format(application.pk)).content.decode()
    seen = re.search(r'name="seen" value="([0-9,]*)"', html).group(1)
    round_ = re.search(r'name="round" value="([0-9]+)"', html)
    return {"seen": seen, **({"round": round_.group(1)} if round_ else {})}, html


def test_f3_a_revoke_from_a_page_opened_before_the_documents_changed_is_refused(
        make_user, api, django_capture_on_commit_callbacks):
    """The reviewer's sequence: A opens a verified application; B revokes it; the
    trainer replaces the certificate and sends again; C approves. A's revoke, from the
    page that showed the OLD certificate, used to go through — and told the trainer the
    new certificate was forged."""
    from trainer_verification import services

    trainer, client = _submitted(make_user, api)
    application = _application(trainer)
    with django_capture_on_commit_callbacks(execute=True):
        services.decide(application.pk, _operator(make_user), approve=True)

    stale_console = _console(_operator(make_user))
    stale, _html = _page(stale_console, application)

    b = _console(_operator(make_user))
    fresh, _html = _page(b, application)
    certificate = application.documents.get(slot="trainer_certificate",
                                            replaced_at__isnull=True)
    with django_capture_on_commit_callbacks(execute=True):
        b.post(CHANGE.format(application.pk),
               {"action": "revoke", "reason": "certificate forged",
                f"doc_reason_{certificate.pk}": "forged", **fresh})
    application.refresh_from_db()
    assert application.state == "rejected"
    assert post_document(client, "trainer_certificate", pdf()).status_code == 200
    assert client.post(f"{BASE}submit/").status_code == 200
    with django_capture_on_commit_callbacks(execute=True):
        services.decide(application.pk, _operator(make_user), approve=True)

    with django_capture_on_commit_callbacks(execute=True):
        answer = stale_console.post(CHANGE.format(application.pk),
                                    {"action": "revoke", "reason": "certificate forged",
                                     **stale})
    assert answer.status_code == 302
    application.refresh_from_db()
    trainer.refresh_from_db()
    assert application.state == "verified" and trainer.trainer_is_verified is True
    assert application.decisions.filter(outcome="revoked").count() == 1
    shown = stale_console.get(answer["Location"]).content.decode()
    assert "changed" in shown


def test_f3_a_revoke_from_a_page_of_an_earlier_round_is_refused_with_the_same_files(
        make_user, api, django_capture_on_commit_callbacks):
    """The same, with no file changed: the page also carries the round it showed, so a
    verification granted in a later round is not withdrawn from an earlier one's page."""
    from trainer_verification import services

    trainer, client = _submitted(make_user, api)
    application = _application(trainer)
    with django_capture_on_commit_callbacks(execute=True):
        services.decide(application.pk, _operator(make_user), approve=True)
    stale_console = _console(_operator(make_user))
    stale, html = _page(stale_console, application)

    with django_capture_on_commit_callbacks(execute=True):
        services.revoke(application.pk, _operator(make_user), "look again")
    assert client.post(f"{BASE}submit/").status_code == 200
    with django_capture_on_commit_callbacks(execute=True):
        services.decide(application.pk, _operator(make_user), approve=True)

    with django_capture_on_commit_callbacks(execute=True):
        stale_console.post(CHANGE.format(application.pk),
                           {"action": "revoke", "reason": "stale", **stale})
    application.refresh_from_db()
    assert application.state == "verified" and application.round == 2
    assert stale.get("round") == "1"


def test_f3_the_service_refuses_a_stale_revoke_and_a_stale_decision(
        make_user, api, django_capture_on_commit_callbacks):
    from trainer_verification import services

    trainer, client = _submitted(make_user, api)
    application = _application(trainer)
    ids = list(application.documents.values_list("pk", flat=True))
    operator = _operator(make_user)

    # A decision on round 1's page, taken once round 2 is waiting, is refused even
    # though the same files are on it.
    with django_capture_on_commit_callbacks(execute=True):
        services.decide(application.pk, operator, approve=False, reason="read it again")
    assert client.post(f"{BASE}submit/").status_code == 200
    with pytest.raises(services.ReviewConflict):
        services.decide(application.pk, operator, approve=True, seen_document_ids=ids,
                        expected_round=1)
    with django_capture_on_commit_callbacks(execute=True):
        services.decide(application.pk, operator, approve=True, seen_document_ids=ids,
                        expected_round=2)

    with pytest.raises(services.ReviewConflict):
        services.revoke(application.pk, operator, "x", seen_document_ids=ids[:-1])
    with pytest.raises(services.ReviewConflict):
        services.revoke(application.pk, operator, "x", seen_document_ids=ids,
                        expected_round=1)
    application.refresh_from_db()
    assert application.state == "verified"
    with django_capture_on_commit_callbacks(execute=True):
        services.revoke(application.pk, operator, "x", seen_document_ids=ids,
                        expected_round=2)
    application.refresh_from_db()
    assert application.state == "rejected"


# ===========================================================================
# F4 — a revoke says which document it is for, and that document is blocked
# ===========================================================================

def test_f4_a_revoke_rejects_the_document_it_names_and_blocks_resending_it(
        make_user, api, django_capture_on_commit_callbacks):
    from trainer_verification import services

    trainer, client = _submitted(make_user, api)
    application = _application(trainer)
    operator = _operator(make_user)
    with django_capture_on_commit_callbacks(execute=True):
        services.decide(application.pk, operator, approve=True)
    certificate = application.documents.get(slot="trainer_certificate")
    with django_capture_on_commit_callbacks(execute=True):
        decision = services.revoke(application.pk, operator, "الشهادة مزوّرة",
                                   document_reasons={certificate.pk: "مزوّرة"})

    certificate.refresh_from_db()
    assert certificate.state == "rejected" and certificate.rejection_reason == "مزوّرة"
    assert certificate.reviewed_in_round == application.round
    snapshot = {d["slot"]: d for d in decision.documents}
    assert snapshot["trainer_certificate"]["state"] == "rejected"
    assert snapshot["trainer_certificate"]["reason"] == "مزوّرة"
    assert snapshot["photo"]["state"] == "accepted"

    body = client.get(BASE).json()
    assert body["status"] == "rejected"
    assert body["needs_replacing"] == ["trainer_certificate"]
    assert body["can_submit"] is False
    by_slot = {d["slot"]: d for d in body["documents"]}
    assert by_slot["trainer_certificate"]["state"] == "rejected"
    assert by_slot["trainer_certificate"]["rejection_reason"] == "مزوّرة"

    refused = client.post(f"{BASE}submit/")
    assert refused.status_code == 409
    assert refused.json()["code"] == "verification_documents_rejected"
    assert refused.json()["rejected"] == ["trainer_certificate"]

    # Replaced, it goes: the forged one is kept out of sight for comparison, and the new
    # one is reviewed as new.
    assert post_document(client, "trainer_certificate", pdf()).status_code == 200
    assert client.post(f"{BASE}submit/").status_code == 200
    certificate.refresh_from_db()
    assert certificate.replaced_at is not None
    page = _console(operator).get(CHANGE.format(application.pk)).content.decode()
    current = page.split("<h2>Current documents</h2>")[1].split("</table>")[0]
    assert "uploaded" in current


def test_f4_a_revoke_that_names_no_document_sends_every_one_back_to_review(
        make_user, api, django_capture_on_commit_callbacks):
    """Nothing stays `accepted` after a revoke: named or not, the next round reviews
    the files again. A document an operator decided on is still kept 90 days when the
    trainer replaces it."""
    from trainer_verification import services

    trainer, client = _submitted(make_user, api)
    application = _application(trainer)
    operator = _operator(make_user)
    with django_capture_on_commit_callbacks(execute=True):
        services.decide(application.pk, operator, approve=True)
    with django_capture_on_commit_callbacks(execute=True):
        decision = services.revoke(application.pk, operator, "we will look again")

    assert {d["state"] for d in decision.documents} == {"uploaded"}
    body = client.get(BASE).json()
    assert {d["state"] for d in body["documents"] if d["state"] != "missing"} == {"uploaded"}
    assert body["needs_replacing"] == [] and body["can_submit"] is True

    photo = application.documents.get(slot="photo", replaced_at__isnull=True)
    assert post_document(client, "photo", jpeg()).status_code == 200
    photo.refresh_from_db()
    assert photo.replaced_at is not None and photo.purge_after is not None


def test_f4_a_revoke_reason_must_name_a_document_of_this_application(
        make_user, api, django_capture_on_commit_callbacks):
    from trainer_verification import services

    trainer, _client = _submitted(make_user, api)
    other, _other_client = _submitted(make_user, api)
    application = _application(trainer)
    operator = _operator(make_user)
    with django_capture_on_commit_callbacks(execute=True):
        services.decide(application.pk, operator, approve=True)
    stranger = _application(other).documents.first()
    with pytest.raises(services.ReviewConflict):
        services.revoke(application.pk, operator, "x",
                        document_reasons={stranger.pk: "not yours"})
    application.refresh_from_db()
    assert application.state == "verified"


def test_f4_the_console_offers_a_reason_per_document_on_revoke_and_passes_it(
        make_user, api, django_capture_on_commit_callbacks):
    from trainer_verification import services

    trainer, _client = _submitted(make_user, api)
    application = _application(trainer)
    operator = _operator(make_user)
    with django_capture_on_commit_callbacks(execute=True):
        services.decide(application.pk, operator, approve=True)
    console = _console(operator)
    fields, html = _page(console, application)
    front = application.documents.get(slot="identity_front")
    assert f'name="doc_reason_{front.pk}"' in html

    with django_capture_on_commit_callbacks(execute=True):
        console.post(CHANGE.format(application.pk),
                     {"action": "revoke", "reason": "the card is not his",
                      f"doc_reason_{front.pk}": "another person's card", **fields})
    front.refresh_from_db()
    assert front.state == "rejected" and front.rejection_reason == "another person's card"


def test_f4_the_revoke_is_in_the_app_contract():
    from trainer_verification import views

    doc = views.MyVerificationView.__doc__
    assert "revoke" in doc and "needs_replacing" in doc


# ===========================================================================
# F5 — the files go first, and the row only once they are gone
# ===========================================================================

@pytest.fixture
def storage(monkeypatch):
    """The documents' storage, which the test can make refuse every delete — as GCS or a
    full disk can — and then heal."""
    from trainer_verification.storage import PrivateDocumentStorage

    original = PrivateDocumentStorage.delete

    class Switch:
        def __init__(self):
            self.attempts = []

        def break_(self):
            def refuse(_storage, name):
                self.attempts.append(name)
                raise OSError("storage unavailable")
            monkeypatch.setattr(PrivateDocumentStorage, "delete", refuse)

        def heal(self):
            monkeypatch.setattr(PrivateDocumentStorage, "delete", original)

    return Switch()


def _replaced_photo(make_user, api, django_capture_on_commit_callbacks):
    """A trainer whose refused photo was replaced, its retention already over."""
    from datetime import timedelta

    from django.utils import timezone

    from trainer_verification import services
    from trainer_verification.models import VerificationDocument

    trainer, client = _submitted(make_user, api)
    application = _application(trainer)
    photo = application.documents.get(slot="photo")
    with django_capture_on_commit_callbacks(execute=True):
        services.decide(application.pk, _operator(make_user), approve=False,
                        reason="retake", document_reasons={photo.pk: "blurred"})
    post_document(client, "photo", jpeg())
    VerificationDocument.objects.filter(pk=photo.pk).update(
        purge_after=timezone.now() - timedelta(seconds=1))
    photo.refresh_from_db()
    return trainer, photo


def _stored(trainer):
    from trainer_verification.models import VerificationDocument

    names = []
    for doc in VerificationDocument.objects.filter(application__trainer=trainer):
        names += [doc.file.name] + ([doc.thumbnail.name] if doc.thumbnail else [])
    return names


def test_f5_a_storage_error_keeps_the_row_so_the_next_purge_retries(
        make_user, api, django_capture_on_commit_callbacks, storage_roots, storage):
    from trainer_verification import services
    from trainer_verification.models import VerificationDocument

    _trainer_, photo = _replaced_photo(make_user, api, django_capture_on_commit_callbacks)
    names = [photo.file.name, photo.thumbnail.name]
    assert all((storage_roots / "private" / n).exists() for n in names)

    storage.break_()
    with django_capture_on_commit_callbacks(execute=True):
        removed = services.purge_replaced_documents()
    storage.heal()

    assert storage.attempts
    assert removed == 0
    assert VerificationDocument.objects.filter(pk=photo.pk).exists(), \
        "the row went while its file stayed: nothing will ever find the file again"
    assert all((storage_roots / "private" / n).exists() for n in names)

    with django_capture_on_commit_callbacks(execute=True):
        assert services.purge_replaced_documents() == 1
    assert not VerificationDocument.objects.filter(pk=photo.pk).exists()
    assert not any((storage_roots / "private" / n).exists() for n in names)


def test_f5_a_storage_error_refuses_the_erasure_and_erases_nothing(
        make_user, api, django_capture_on_commit_callbacks, storage_roots, storage):
    from training_platform.privacy import ErasureRefused, erase_user_data
    from trainer_verification.models import VerificationApplication, VerificationDocument

    trainer, _photo = _replaced_photo(make_user, api, django_capture_on_commit_callbacks)
    email = trainer.email
    names = _stored(trainer)
    assert len(names) == 9      # five documents; the four photos carry a thumbnail

    storage.break_()
    with django_capture_on_commit_callbacks(execute=True):
        with pytest.raises(ErasureRefused) as refused:
            erase_user_data(trainer)
    storage.heal()
    assert refused.value.code == "erasure_refused"

    trainer.refresh_from_db()
    assert trainer.email == email and trainer.is_active
    assert VerificationDocument.objects.filter(application__trainer=trainer).count() == 5
    assert all((storage_roots / "private" / n).exists() for n in names)

    with django_capture_on_commit_callbacks(execute=True):
        report = erase_user_data(trainer)
    assert report["verification_documents"]["rows"] == 5
    assert not VerificationApplication.objects.filter(trainer=trainer).exists()
    leftover = [n for n in names if (storage_roots / "private" / n).exists()]
    assert not leftover, leftover


def test_f5_the_erase_route_answers_erasure_refused_with_a_reason_about_documents(
        make_user, api, django_capture_on_commit_callbacks, storage_roots, storage):
    trainer, client = _submitted(make_user, api)
    names = _stored(trainer)

    storage.break_()
    with django_capture_on_commit_callbacks(execute=True):
        response = client.delete("/api/privacy/erase/?confirm=ERASE")
    storage.heal()

    assert response.status_code == 409, response.content
    body = response.json()
    assert body["code"] == "erasure_refused"
    assert "payment" not in body["error"].lower()
    assert storage.attempts
    trainer.refresh_from_db()
    assert trainer.is_active
    assert all((storage_roots / "private" / n).exists() for n in names)


# ===========================================================================
# F6 — the nutrition certificate ranks nothing, and the contract says so
# ===========================================================================

NO_RANKING = "no effect on roster or match order"


def test_f6_the_contract_says_the_nutrition_certificate_changes_no_ranking():
    """The design draws "اختيارية · ترفع ترتيبك" ("optional · raises your ranking") on
    that row; nothing ranks on it, so the app must not promise it (an owner decision)."""
    from trainer_verification import views

    doc = " ".join(views.MyVerificationView.__doc__.split())
    assert "nutrition_certificate" in doc and NO_RANKING in doc
    contract = (ROOT / "API_CONTRACT.md").read_text(encoding="utf-8")
    section = contract.split("### Trainer verification")[1].split("\n### ")[0]
    assert NO_RANKING in " ".join(section.split())


def test_f6_an_accepted_nutrition_certificate_moves_no_trainer_up(make_user, api):
    """Pins the sentence above to what the code does: two trainers alike in everything
    the roster and the matches read, the later one with an accepted nutrition
    certificate, keep id order in both."""
    from django.utils import timezone

    from trainer_verification.models import VerificationApplication, VerificationDocument

    family = _tag("rank")
    first, second = (make_user(_tag("tv_rank"), user_type="trainer", email_verified=True,
                               trainer_is_available=True, trainer_is_verified=True,
                               trainer_experience_years=5, first_name="Rank",
                               last_name=family) for _ in range(2))
    assert first.pk < second.pk
    now = timezone.now()
    application = VerificationApplication.objects.create(
        trainer=second, state="verified", round=1, submitted_at=now, decided_at=now)
    VerificationDocument.objects.create(
        application=application, slot="nutrition_certificate", kind="pdf",
        content_type="application/pdf", size=10, sha256="0" * 64, state="accepted",
        reviewed_in_round=1, file="verification/nutrition.pdf")

    roster = api().get("/api/v1/trainers/", {"search": family}).json()["results"]
    assert [card["id"] for card in roster] == [first.pk, second.pk]

    client = make_user(_tag("tv_rank_client"))
    matches = api(client).get("/api/v1/trainers/matches/", {"limit": 10}).json()["results"]
    ours = [m for m in matches if m["trainer"]["id"] in (first.pk, second.pk)]
    assert [m["trainer"]["id"] for m in ours] == [first.pk, second.pk]
