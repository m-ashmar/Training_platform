"""Trainer verification (BACKEND_BUGS B-090, plan step 6, first half).

Before this, verification was one boolean an operator ticked in the console: no
documents, no review state, no reason, no record of what anyone looked at. The design
(`12-trainer-shell/TrainerVerify` and `TrainerLocked`) draws an application: the
identity card's two sides, a trainer certificate, an optional nutrition certificate and a
recent photo, each with its own state, and an application that goes not_started ->
pending -> verified, or rejected with a reason and back.

What is asserted here, in order:

  * every state transition, through the routes the app calls and the service the
    console calls;
  * every refusal `code` the three routes document;
  * the storage: a document is never under `/media/`, its URL is signed and dies, an
    edited or unsigned URL is a 404, another trainer and a client are never handed one,
    and a photo's metadata does not survive;
  * two operators deciding at once leave ONE decision (the threaded proof is in
    `tests/test_zz_trainer_verification_race.py`, because a threaded test truncates
    every table);
  * the two pushes, as `Notification` rows;
  * a resubmission after a rejection keeps what was decided before;
  * deletion — account erasure and the 90-day retention of replaced documents — takes
    the files with the rows;
  * the console: operator-only, the queue oldest first, and `trainer_is_verified`
    written nowhere else.
"""
import io
import itertools
import os
import re
import uuid
from pathlib import Path

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from PIL import Image

pytestmark = pytest.mark.django_db

BASE = "/api/v1/trainer/verification/"
ROOT = Path(__file__).resolve().parents[1]
REQUIRED = ["identity_front", "identity_back", "trainer_certificate", "photo"]


@pytest.fixture(autouse=True)
def storage_roots(settings, tmp_path):
    """Both roots in a temporary directory, and apart, as in production."""
    settings.MEDIA_ROOT = str(tmp_path / "media")
    settings.PRIVATE_MEDIA_ROOT = str(tmp_path / "private")
    return tmp_path


def _tag(prefix):
    return f"{prefix}_{uuid.uuid4().hex[:8]}"


# ---------------------------------------------------------------------------
# Builders. Every image differs from every other, because the same bytes in two
# slots is itself a refusal (`verification_duplicate_document`).
# ---------------------------------------------------------------------------
_seq = itertools.count(1)


def jpeg_bytes(w=900, h=600, exif=None):
    n = next(_seq)
    img = Image.new("RGB", (w, h), ((n * 37) % 256, (n * 91) % 256, (n * 53) % 256))
    for x in range(0, min(w, 64)):
        img.putpixel((x, (n * 7) % h), (255, 255, 255))
    buf = io.BytesIO()
    if exif is not None:
        img.save(buf, "JPEG", exif=exif)
    else:
        img.save(buf, "JPEG")
    return buf.getvalue()


def jpeg(name="photo.jpg", **kw):
    return SimpleUploadedFile(name, jpeg_bytes(**kw), content_type="image/jpeg")


def png(w=900, h=600, mode="RGBA"):
    n = next(_seq)
    img = Image.new(mode, (w, h), (n % 256, 10, 10, 128) if mode == "RGBA" else (n % 256, 10, 10))
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return SimpleUploadedFile("card.png", buf.getvalue(), content_type="image/png")


def pdf_bytes(catalog_extra=b"", trailer_extra=b""):
    n = next(_seq)
    return (b"%PDF-1.4\n1 0 obj\n<< /Type /Catalog /Pages 2 0 R " + catalog_extra
            + b" >>\nendobj\n2 0 obj\n<< /Type /Pages /Kids [] /Count 0 >>\nendobj\n"
            + b"3 0 obj\n<< /Length 12 >>\nstream\nq " + str(n).encode().rjust(8, b"0")
            + b" Q\nendstream\nendobj\ntrailer\n<< /Root 1 0 R " + trailer_extra
            + b" >>\n%%EOF\n")


def pdf(**kw):
    return SimpleUploadedFile("certificate.pdf", pdf_bytes(**kw),
                              content_type="application/pdf")


def post_document(client, slot, upload):
    return client.post(f"{BASE}documents/{slot}/", {"file": upload})


def fill_required(client):
    for slot in REQUIRED:
        upload = pdf() if slot == "trainer_certificate" else jpeg()
        response = post_document(client, slot, upload)
        assert response.status_code == 200, (slot, response.content)
    return response.json()


def _trainer(make_user, prefix="tv"):
    return make_user(_tag(prefix), user_type="trainer")


def _operator(make_user):
    return make_user(_tag("tv_op"), user_type="admin")


def _submitted(make_user, api):
    trainer = _trainer(make_user)
    client = api(trainer)
    fill_required(client)
    assert client.post(f"{BASE}submit/").status_code == 200
    return trainer, client


def _application(trainer):
    from trainer_verification.models import VerificationApplication
    return VerificationApplication.objects.get(trainer=trainer)


def _doc(body, slot):
    return next(d for d in body["documents"] if d["slot"] == slot)


# ===========================================================================
# 1. States
# ===========================================================================

def test_a_trainer_with_nothing_sent_is_not_started_and_every_slot_is_listed(make_user, api):
    body = api(_trainer(make_user)).get(BASE).json()

    assert body["status"] == "not_started"
    assert body["round"] == 0 and body["submitted_at"] is None
    assert [d["slot"] for d in body["documents"]] == [
        "identity_front", "identity_back", "trainer_certificate", "nutrition_certificate",
        "photo"]
    assert all(d["state"] == "missing" and d["file_url"] is None for d in body["documents"])
    assert body["missing"] == REQUIRED
    assert body["can_submit"] is False
    assert _doc(body, "nutrition_certificate")["required"] is False
    assert _doc(body, "trainer_certificate")["accepts"] == ["image", "pdf"]
    assert _doc(body, "photo")["accepts"] == ["image"]
    assert body["review_business_days"] == 3


def test_uploading_every_required_document_makes_it_submittable(make_user, api):
    body = fill_required(api(_trainer(make_user)))

    assert body["status"] == "not_started"
    assert body["missing"] == [] and body["can_submit"] is True
    front = _doc(body, "identity_front")
    assert front["state"] == "uploaded" and front["kind"] == "image"
    assert front["content_type"] == "image/jpeg"
    assert front["thumbnail_url"] and front["file_url"]
    cert = _doc(body, "trainer_certificate")
    assert cert["kind"] == "pdf" and cert["thumbnail_url"] is None and cert["file_url"]
    assert _doc(body, "nutrition_certificate")["state"] == "missing"


def test_submit_then_approve_verifies_the_trainer(make_user, api, django_capture_on_commit_callbacks):
    from trainer_verification import services
    from trainer_verification.models import VerificationDecision

    trainer, client = _submitted(make_user, api)
    pending = client.get(BASE).json()
    assert pending["status"] == "pending" and pending["round"] == 1
    assert pending["submitted_at"] and pending["decided_at"] is None
    assert pending["can_submit"] is False

    operator = _operator(make_user)
    with django_capture_on_commit_callbacks(execute=True):
        services.decide(_application(trainer).pk, operator, approve=True)

    trainer.refresh_from_db()
    assert trainer.trainer_is_verified is True
    body = client.get(BASE).json()
    assert body["status"] == "verified" and body["decided_at"]
    assert all(_doc(body, s)["state"] == "accepted" for s in REQUIRED)
    decision = VerificationDecision.objects.get(application__trainer=trainer)
    assert decision.outcome == "verified" and decision.decided_by == operator
    assert {d["slot"] for d in decision.documents} == set(REQUIRED)


def test_a_rejection_names_its_reason_and_the_refused_document(
        make_user, api, django_capture_on_commit_callbacks):
    from trainer_verification import services

    trainer, client = _submitted(make_user, api)
    application = _application(trainer)
    photo = application.documents.get(slot="photo")
    with django_capture_on_commit_callbacks(execute=True):
        services.decide(application.pk, _operator(make_user), approve=False,
                        reason="الصورة لا تطابق الهوية",
                        document_reasons={photo.pk: "صورة غير واضحة"})

    trainer.refresh_from_db()
    assert trainer.trainer_is_verified is False
    body = client.get(BASE).json()
    assert body["status"] == "rejected"
    assert body["rejection_reason"] == "الصورة لا تطابق الهوية"
    assert _doc(body, "photo")["state"] == "rejected"
    assert _doc(body, "photo")["rejection_reason"] == "صورة غير واضحة"
    assert _doc(body, "identity_front")["state"] == "accepted"
    assert body["needs_replacing"] == ["photo"]
    assert body["can_submit"] is False
    assert all(d["can_change"] for d in body["documents"])


def test_a_resubmission_after_a_rejection_keeps_what_was_decided_before(
        make_user, api, django_capture_on_commit_callbacks):
    from trainer_verification import services
    from trainer_verification.models import VerificationDecision, VerificationDocument

    trainer, client = _submitted(make_user, api)
    application = _application(trainer)
    old_photo = application.documents.get(slot="photo")
    operator = _operator(make_user)
    with django_capture_on_commit_callbacks(execute=True):
        services.decide(application.pk, operator, approve=False, reason="blurred photo",
                        document_reasons={old_photo.pk: "blurred"})

    # Replacing the refused photo resets THAT document to uploaded...
    body = post_document(client, "photo", jpeg()).json()
    assert _doc(body, "photo")["state"] == "uploaded"
    assert body["needs_replacing"] == [] and body["can_submit"] is True
    # ...and the refused one is kept, with the operator's decision on it.
    old_photo.refresh_from_db()
    assert old_photo.replaced_at is not None and old_photo.purge_after is not None
    assert old_photo.state == "rejected" and old_photo.rejection_reason == "blurred"
    assert os.path.exists(old_photo.file.path)

    assert client.post(f"{BASE}submit/").json()["round"] == 2
    with django_capture_on_commit_callbacks(execute=True):
        services.decide(application.pk, operator, approve=True)

    decisions = list(VerificationDecision.objects.filter(application=application)
                     .order_by("round"))
    assert [(d.round, d.outcome) for d in decisions] == [(1, "rejected"), (2, "verified")]
    first = {d["slot"]: d for d in decisions[0].documents}
    assert first["photo"]["state"] == "rejected" and first["photo"]["reason"] == "blurred"
    assert first["photo"]["document_id"] == old_photo.pk
    assert decisions[0].reason == "blurred photo"
    # Two photo rows: the refused one (replaced) and the accepted one (current).
    photos = VerificationDocument.objects.filter(application=application, slot="photo")
    assert sorted((p.state, p.replaced_at is None) for p in photos) == [
        ("accepted", True), ("rejected", False)]


def test_a_decision_row_cannot_be_edited(make_user, api, django_capture_on_commit_callbacks):
    from trainer_verification import services
    from trainer_verification.models import ImmutableDecision

    trainer, _client = _submitted(make_user, api)
    with django_capture_on_commit_callbacks(execute=True):
        decision = services.decide(_application(trainer).pk, _operator(make_user),
                                   approve=False, reason="first")
    decision.reason = "rewritten"
    with pytest.raises(ImmutableDecision):
        decision.save()


def test_a_revoked_verification_goes_back_to_rejected_with_its_reason(
        make_user, api, django_capture_on_commit_callbacks):
    from notifications.models import Notification
    from trainer_verification import services

    trainer, client = _submitted(make_user, api)
    application = _application(trainer)
    operator = _operator(make_user)
    with django_capture_on_commit_callbacks(execute=True):
        services.decide(application.pk, operator, approve=True)
    with django_capture_on_commit_callbacks(execute=True):
        services.revoke(application.pk, operator, "الشهادة مزوّرة")

    trainer.refresh_from_db()
    assert trainer.trainer_is_verified is False
    body = client.get(BASE).json()
    assert body["status"] == "rejected" and body["rejection_reason"] == "الشهادة مزوّرة"
    assert application.decisions.filter(outcome="revoked").count() == 1
    row = Notification.objects.get(recipient=trainer,
                                   event_type="trainer_verification_rejected")
    assert row.metadata["data"]["reason"] == "الشهادة مزوّرة"
    # Only a verified application can be revoked.
    with pytest.raises(services.ReviewConflict):
        services.revoke(application.pk, operator, "again")


def test_while_pending_only_an_empty_optional_slot_can_be_filled(make_user, api):
    """The design's pending card offers "أضف" on the nutrition certificate: the rest is
    what the operator is looking at."""
    _trainer_, client = _submitted(make_user, api)

    body = client.get(BASE).json()
    assert _doc(body, "nutrition_certificate")["can_change"] is True
    assert _doc(body, "photo")["can_change"] is False

    added = post_document(client, "nutrition_certificate", pdf())
    assert added.status_code == 200
    assert _doc(added.json(), "nutrition_certificate")["state"] == "uploaded"
    assert added.json()["status"] == "pending"


def test_a_trainer_verified_by_hand_before_applications_is_still_verified(make_user, api):
    """The data migration gives them an application and a decision row; the status reads
    `verified` either way, and an operator can revoke it from the console."""
    import importlib

    from django.apps import apps as live_apps

    from trainer_verification import services
    from trainer_verification.models import VerificationApplication

    trainer = make_user(_tag("tv_hand"), user_type="trainer", trainer_is_verified=True)
    body = api(trainer).get(BASE).json()
    assert body["status"] == "verified" and body["can_submit"] is False
    assert all(d["can_change"] is False for d in body["documents"])

    migration = importlib.import_module(
        "trainer_verification.migrations.0002_trainers_verified_by_hand")
    migration.forwards(live_apps, None)
    application = VerificationApplication.objects.get(trainer=trainer)
    assert application.state == "verified" and application.round == 0
    assert application.decisions.get().reason == migration.NOTE
    migration.forwards(live_apps, None)     # twice changes nothing
    assert VerificationApplication.objects.filter(trainer=trainer).count() == 1

    services.revoke(application.pk, _operator(make_user), "no documents on file")
    trainer.refresh_from_db()
    assert trainer.trainer_is_verified is False


def test_the_own_profile_reads_carry_the_verification_status(make_user, api):
    trainer = _trainer(make_user)
    client = api(trainer)
    assert client.get("/api/auth/trainer/profile/").json()["verification_status"] == "not_started"
    assert client.get("/api/auth/user/update/").json()["verification_status"] == "not_started"
    fill_required(client)
    client.post(f"{BASE}submit/")
    assert client.get("/api/auth/trainer/profile/").json()["verification_status"] == "pending"
    # Still there, unchanged, for every build that reads it.
    assert client.get("/api/auth/trainer/profile/").json()["trainer_is_verified"] is False

    member = api(make_user(_tag("tv_client")))
    assert member.get("/api/auth/user/update/").json()["verification_status"] is None


def test_the_flag_cannot_be_written_through_the_profile(make_user, api):
    trainer = _trainer(make_user)
    client = api(trainer)
    client.post("/api/auth/trainer/profile/", {"trainer_is_verified": True},
                content_type="application/json")
    client.post("/api/auth/user/update/", {"trainer_is_verified": True},
                content_type="application/json")
    trainer.refresh_from_db()
    assert trainer.trainer_is_verified is False


# ===========================================================================
# 2. Every refusal names its code
# ===========================================================================

def test_a_client_is_refused_with_not_a_trainer_on_every_route(make_user, api):
    client = api(make_user(_tag("tv_c")))
    for response in (client.get(BASE),
                     post_document(client, "photo", jpeg()),
                     client.delete(f"{BASE}documents/photo/"),
                     client.post(f"{BASE}submit/")):
        assert response.status_code == 403
        assert response.json()["code"] == "not_a_trainer"
    assert api(None).get(BASE).status_code == 401


@pytest.mark.parametrize("build,code", [
    (lambda: None, "verification_file_required"),
    (lambda: SimpleUploadedFile("id.jpg", b"<?php echo 1; ?>" * 20,
                                content_type="image/jpeg"), "verification_unsupported_format"),
    (lambda: SimpleUploadedFile("id.jpg", b"\x00\x00\x00\x18ftypheic\x00\x00\x00\x00mif1heic"
                                + b"\x00" * 64, content_type="image/jpeg"),
     "verification_unsupported_format"),
    (lambda: SimpleUploadedFile("id.jpg", b"", content_type="image/jpeg"),
     "verification_unreadable_file"),
    (lambda: SimpleUploadedFile("id.jpg", jpeg_bytes()[:200], content_type="image/jpeg"),
     "verification_unreadable_file"),
    (lambda: jpeg(w=300, h=900), "verification_image_too_small"),
    (lambda: pdf(), "verification_unsupported_format"),   # a PDF where a photo belongs
])
def test_the_identity_slot_refuses_by_content(make_user, api, build, code):
    client = api(_trainer(make_user))
    upload = build()
    data = {"file": upload} if upload is not None else {}
    response = client.post(f"{BASE}documents/identity_front/", data)
    assert response.status_code == 400, response.content
    assert response.json()["code"] == code
    if code == "verification_unsupported_format":
        assert response.json()["accepted"] == ["image"]


def test_a_photo_too_large_to_open_is_refused_before_it_is_decoded(make_user, api):
    client = api(_trainer(make_user))
    img = Image.new("L", (10_001, 400), 128)
    buf = io.BytesIO()
    img.save(buf, "PNG")
    response = post_document(client, "photo", SimpleUploadedFile("big.png", buf.getvalue()))
    assert response.status_code == 400
    assert response.json()["code"] == "verification_image_too_large"


def test_a_file_over_the_cap_is_refused(make_user, api, settings):
    settings.VERIFICATION_DOCUMENT_MAX_BYTES = 2048
    response = post_document(api(_trainer(make_user)), "photo", jpeg())
    assert response.status_code == 400
    assert response.json()["code"] == "verification_file_too_large"
    assert response.json()["max_bytes"] == 2048


@pytest.mark.parametrize("kw,code", [
    ({"trailer_extra": b"/Encrypt 4 0 R"}, "verification_pdf_encrypted"),
    ({"catalog_extra": b"/OpenAction << /S /JavaScript /JS (app.alert(1)) >>"},
     "verification_pdf_active_content"),
    ({"catalog_extra": b"/OpenAction << /S /J#61vaScript /J#53 (x) >>"},
     "verification_pdf_active_content"),
    ({"catalog_extra": b"/Names << /EmbeddedFiles 5 0 R >>"},
     "verification_pdf_active_content"),
])
def test_a_certificate_pdf_that_does_something_is_refused(make_user, api, kw, code):
    response = post_document(api(_trainer(make_user)), "trainer_certificate", pdf(**kw))
    assert response.status_code == 400
    assert response.json()["code"] == code


def test_a_pdf_whose_stream_happens_to_spell_a_script_name_is_accepted(make_user, api):
    """Compressed bytes are data, not structure: an honest certificate is not refused
    because its page drawing contains `/JS` by chance."""
    raw = pdf_bytes().replace(b"stream\nq ", b"stream\n/JS /Launch q ")
    response = post_document(api(_trainer(make_user)), "trainer_certificate",
                             SimpleUploadedFile("c.pdf", raw))
    assert response.status_code == 200, response.content


def test_a_broken_pdf_is_unreadable(make_user, api):
    raw = pdf_bytes().replace(b"%%EOF", b"")
    response = post_document(api(_trainer(make_user)), "trainer_certificate",
                             SimpleUploadedFile("c.pdf", raw))
    assert response.json()["code"] == "verification_unreadable_file"


def test_an_unknown_slot_is_a_404_with_its_code(make_user, api):
    response = post_document(api(_trainer(make_user)), "passport", jpeg())
    assert response.status_code == 404
    assert response.json()["code"] == "verification_unknown_slot"
    assert "identity_front" in response.json()["slots"]


def test_the_same_file_in_two_slots_is_refused(make_user, api):
    client = api(_trainer(make_user))
    raw = jpeg_bytes()
    assert post_document(client, "identity_front",
                         SimpleUploadedFile("a.jpg", raw)).status_code == 200
    twin = post_document(client, "identity_back", SimpleUploadedFile("b.jpg", raw))
    assert twin.status_code == 400
    assert twin.json()["code"] == "verification_duplicate_document"
    assert twin.json()["slot"] == "identity_front"
    # The same file into the SAME slot is a replacement, not a duplicate.
    assert post_document(client, "identity_front",
                         SimpleUploadedFile("a.jpg", raw)).status_code == 200


def test_submitting_with_a_required_document_missing_lists_them(make_user, api):
    client = api(_trainer(make_user))
    post_document(client, "identity_front", jpeg())
    response = client.post(f"{BASE}submit/")
    assert response.status_code == 409
    assert response.json()["code"] == "verification_documents_missing"
    assert response.json()["missing"] == ["identity_back", "trainer_certificate", "photo"]
    # And with nothing uploaded at all, no row is created by the refusal.
    other = api(_trainer(make_user))
    assert other.post(f"{BASE}submit/").json()["missing"] == REQUIRED


def test_submitting_twice_and_changing_while_pending_are_refused(make_user, api):
    _t, client = _submitted(make_user, api)

    again = client.post(f"{BASE}submit/")
    assert again.status_code == 409 and again.json()["code"] == "verification_already_pending"
    replace = post_document(client, "photo", jpeg())
    assert replace.status_code == 409 and replace.json()["code"] == "verification_in_review"
    remove = client.delete(f"{BASE}documents/photo/")
    assert remove.status_code == 409 and remove.json()["code"] == "verification_in_review"


def test_resubmitting_a_refused_document_unchanged_is_refused(
        make_user, api, django_capture_on_commit_callbacks):
    from trainer_verification import services

    trainer, client = _submitted(make_user, api)
    application = _application(trainer)
    back = application.documents.get(slot="identity_back")
    with django_capture_on_commit_callbacks(execute=True):
        services.decide(application.pk, _operator(make_user), approve=False,
                        reason="the back is cut off", document_reasons={back.pk: "cut off"})

    response = client.post(f"{BASE}submit/")
    assert response.status_code == 409
    assert response.json()["code"] == "verification_documents_rejected"
    assert response.json()["rejected"] == ["identity_back"]


def test_a_verified_trainer_changes_nothing(make_user, api, django_capture_on_commit_callbacks):
    from trainer_verification import services

    trainer, client = _submitted(make_user, api)
    with django_capture_on_commit_callbacks(execute=True):
        services.decide(_application(trainer).pk, _operator(make_user), approve=True)

    for response in (post_document(client, "nutrition_certificate", pdf()),
                     client.delete(f"{BASE}documents/photo/"),
                     client.post(f"{BASE}submit/")):
        assert response.status_code == 409
        assert response.json()["code"] == "verification_already_verified"


def test_every_code_the_routes_answer_is_documented_in_the_views_and_the_contract():
    from trainer_verification import views

    source = (ROOT / "trainer_verification" / "services.py").read_text() \
        + (ROOT / "trainer_verification" / "files.py").read_text() \
        + (ROOT / "trainer_verification" / "views.py").read_text()
    raised = set(re.findall(r'"(verification_[a-z_]+|not_a_trainer)"', source))
    documented = " ".join(v.__doc__ for v in (views.MyVerificationView, views.DocumentView,
                                              views.SubmitView))
    contract = (ROOT / "API_CONTRACT.md").read_text()
    missing_doc = sorted(c for c in raised if f"`{c}`" not in documented)
    missing_contract = sorted(c for c in raised if f"`{c}`" not in contract)
    assert not missing_doc, missing_doc
    assert not missing_contract, missing_contract


# ===========================================================================
# 3. Storage: private, signed, short-lived, and only ever handed to the owner
# ===========================================================================

def _stored_names(trainer):
    from trainer_verification.models import VerificationDocument
    names = []
    for doc in VerificationDocument.objects.filter(application__trainer=trainer):
        names.append(doc.file.name)
        if doc.thumbnail:
            names.append(doc.thumbnail.name)
    return names


def test_a_document_is_stored_outside_media_and_its_url_is_signed(make_user, api, storage_roots):
    trainer = _trainer(make_user)
    body = fill_required(api(trainer))

    for name in _stored_names(trainer):
        assert (storage_roots / "private" / name).is_file()
        assert not (storage_roots / "media" / name).exists()
        # Nothing about the person in the path: a random token and the extension.
        assert re.fullmatch(r"verification/[0-9a-f]{32}\.(jpg|pdf)", name), name

    url = _doc(body, "identity_front")["file_url"]
    assert "/private-media/verification/" in url and "?e=" in url and "&s=" in url
    assert body["url_expires_in"] == 300


def test_the_signed_url_serves_the_file_and_nothing_else_does(make_user, api):
    from django.test import Client
    from training_platform.media_storage import sign_path

    trainer = _trainer(make_user)
    body = fill_required(api(trainer))
    url = _doc(body, "identity_front")["file_url"].replace("http://testserver", "")
    path, query = url.split("?", 1)
    anonymous = Client()

    ok = anonymous.get(url)
    assert ok.status_code == 200
    assert ok["Content-Type"] == "image/jpeg"
    assert ok["X-Content-Type-Options"] == "nosniff"
    assert "no-store" in ok["Cache-Control"]
    # The signed path never leaves the origin in a Referer (the platform's own header,
    # `SecurityHeadersMiddleware`, decides it for every response).
    assert ok["Referrer-Policy"] in ("no-referrer", "strict-origin-when-cross-origin")
    assert b"".join(ok.streaming_content)[:3] == b"\xff\xd8\xff"

    params = dict(p.split("=", 1) for p in query.split("&"))
    assert anonymous.get(path).status_code == 404                               # unsigned
    assert anonymous.get(f"{path}?e={params['e']}&s=forged").status_code == 404  # forged
    later = int(params["e"]) + 3600
    assert anonymous.get(f"{path}?e={later}&s={params['s']}").status_code == 404  # extended
    # The media route never reaches it, even with a valid MEDIA signature.
    name = path.split("/private-media/", 1)[1]
    assert anonymous.get(f"/media/{name}?s={sign_path(name)}").status_code == 404


def test_an_expired_url_is_refused(make_user, api, monkeypatch):
    import time as _time

    body = fill_required(api(_trainer(make_user)))
    url = _doc(body, "photo")["file_url"].replace("http://testserver", "")
    real = _time.time
    monkeypatch.setattr(_time, "time", lambda: real() + 301)
    from django.test import Client
    assert Client().get(url).status_code == 404


def test_another_trainer_and_a_client_are_never_handed_a_documents_url(make_user, api):
    owner = _trainer(make_user)
    fill_required(api(owner))
    names = _stored_names(owner)
    assert names

    other = api(_trainer(make_user, "tv_other")).get(BASE)
    assert other.status_code == 200
    member = api(make_user(_tag("tv_member"))).get(BASE)
    assert member.status_code == 403
    for response in (other, member):
        for name in names:
            assert name.encode() not in response.content
    # And nothing public about the owner mentions the application.
    public = api(None).get(f"/api/v1/trainers/{owner.pk}/")
    for name in names:
        assert name.encode() not in public.content


def test_there_is_no_public_url_for_a_document(make_user, api):
    from trainer_verification.models import VerificationDocument
    from training_platform.media_storage import media_url

    trainer = _trainer(make_user)
    fill_required(api(trainer))
    doc = VerificationDocument.objects.filter(application__trainer=trainer).first()
    with pytest.raises(PermissionError):
        media_url(doc.file, public=True)
    # Even a stray `.url` is signed and expiring.
    assert "?e=" in doc.file.url and "&s=" in doc.file.url


def test_a_photos_metadata_is_removed_and_it_is_stored_upright(make_user, api):
    from trainer_verification.models import VerificationDocument

    exif = Image.Exif()
    exif[0x010F] = "SpyCam"      # Make
    exif[0x0112] = 6             # Orientation: rotate 90
    trainer = _trainer(make_user)
    response = post_document(api(trainer), "photo", jpeg(w=900, h=600, exif=exif.tobytes()))
    assert response.status_code == 200

    doc = VerificationDocument.objects.get(application__trainer=trainer, slot="photo")
    with Image.open(doc.file.path) as stored:
        assert stored.size == (600, 900), "the camera's orientation was not applied"
        assert not stored.getexif(), "EXIF survived into the stored file"
    assert b"SpyCam" not in Path(doc.file.path).read_bytes()


def test_a_transparent_png_is_flattened_to_a_jpeg(make_user, api):
    from trainer_verification.models import VerificationDocument

    trainer = _trainer(make_user)
    assert post_document(api(trainer), "identity_back", png()).status_code == 200
    doc = VerificationDocument.objects.get(application__trainer=trainer)
    assert doc.content_type == "image/jpeg" and doc.file.name.endswith(".jpg")


def test_the_gcs_backend_keeps_documents_private_and_short_lived(settings, monkeypatch):
    pytest.importorskip("storages.backends.gcloud")
    from datetime import timedelta

    import training_platform.gcs_storage as gcs

    monkeypatch.setattr(gcs, "_credentials", lambda: object())
    settings.GS_BUCKET_NAME = "yalla-media"
    settings.VERIFICATION_GS_BUCKET_NAME = "yalla-identity"
    storage = gcs.PrivateDocumentGoogleCloudStorage()
    assert storage.bucket_name == "yalla-identity"
    assert storage.location == "private"
    assert storage.expiration == timedelta(seconds=300)
    with pytest.raises(PermissionError):
        storage.public_url("verification/x.jpg")

    settings.VERIFICATION_GS_BUCKET_NAME = ""
    assert gcs.PrivateDocumentGoogleCloudStorage().bucket_name == "yalla-media"
    base = (ROOT / "training_platform" / "settings_base.py").read_text()
    assert "training_platform.gcs_storage.PrivateDocumentGoogleCloudStorage" in base


@pytest.mark.django_db(transaction=False)
def test_production_refuses_identity_documents_in_a_public_media_bucket():
    from tests.test_media_storage import GCS_KEY_JSON, _production_boot

    shared = _production_boot(MEDIA_BACKEND="gcs", GS_BUCKET_NAME="yalla-media",
                              GS_CREDENTIALS_JSON=GCS_KEY_JSON,
                              MEDIA_PUBLIC_APP_CONTENT="True")
    assert shared.returncode != 0 and "VERIFICATION_GS_BUCKET_NAME" in shared.stderr

    apart = _production_boot(MEDIA_BACKEND="gcs", GS_BUCKET_NAME="yalla-media",
                             GS_CREDENTIALS_JSON=GCS_KEY_JSON,
                             MEDIA_PUBLIC_APP_CONTENT="True",
                             VERIFICATION_GS_BUCKET_NAME="yalla-identity")
    assert apart.returncode == 0, apart.stderr


# ===========================================================================
# 4. Two operators at once
# ===========================================================================

def test_the_second_operator_to_decide_is_refused_and_one_decision_stands(
        make_user, api, django_capture_on_commit_callbacks):
    from trainer_verification import services
    from trainer_verification.models import VerificationDecision

    trainer, _client = _submitted(make_user, api)
    application = _application(trainer)
    first, second = _operator(make_user), _operator(make_user)
    with django_capture_on_commit_callbacks(execute=True):
        services.decide(application.pk, first, approve=True)
    with pytest.raises(services.ReviewConflict):
        services.decide(application.pk, second, approve=False, reason="too late")

    assert VerificationDecision.objects.filter(application=application).count() == 1
    trainer.refresh_from_db()
    assert trainer.trainer_is_verified is True


def test_a_decision_on_documents_that_changed_since_the_page_opened_is_refused(
        make_user, api):
    from trainer_verification import services

    trainer, client = _submitted(make_user, api)
    application = _application(trainer)
    seen = list(application.documents.values_list("pk", flat=True))
    post_document(client, "nutrition_certificate", pdf())     # allowed while pending

    with pytest.raises(services.ReviewConflict):
        services.decide(application.pk, _operator(make_user), approve=True,
                        seen_document_ids=seen)
    application.refresh_from_db()
    assert application.state == "pending"


def test_a_rejection_needs_a_reason_and_an_approval_cannot_refuse_a_document(make_user, api):
    from trainer_verification import services

    trainer, _client = _submitted(make_user, api)
    application = _application(trainer)
    photo = application.documents.get(slot="photo")
    operator = _operator(make_user)
    with pytest.raises(services.ReviewConflict):
        services.decide(application.pk, operator, approve=False, reason="  ")
    with pytest.raises(services.ReviewConflict):
        services.decide(application.pk, operator, approve=True,
                        document_reasons={photo.pk: "blurred"})
    application.refresh_from_db()
    assert application.state == "pending"


# ===========================================================================
# 5. The pushes
# ===========================================================================

def test_approval_and_rejection_each_leave_a_notification_row(
        make_user, api, django_capture_on_commit_callbacks):
    from notifications.models import Notification
    from trainer_verification import services

    approved, _c1 = _submitted(make_user, api)
    rejected, _c2 = _submitted(make_user, api)
    operator = _operator(make_user)
    with django_capture_on_commit_callbacks(execute=True):
        services.decide(_application(approved).pk, operator, approve=True)
    with django_capture_on_commit_callbacks(execute=True):
        services.decide(_application(rejected).pk, operator, approve=False,
                        reason="ارفع صورة أوضح للهوية")

    yes = Notification.objects.get(recipient=approved,
                                   event_type="trainer_verification_approved")
    assert yes.metadata["data"] == {"type": "trainer_verification_approved",
                                    "application_id": str(_application(approved).pk)}
    no = Notification.objects.get(recipient=rejected,
                                  event_type="trainer_verification_rejected")
    assert no.metadata["data"]["type"] == "trainer_verification_rejected"
    assert no.metadata["data"]["reason"] == "ارفع صورة أوضح للهوية"
    assert no.metadata["context"]["reason"] == "ارفع صورة أوضح للهوية"
    assert not Notification.objects.filter(recipient=approved,
                                           event_type="trainer_verification_rejected").exists()


def test_the_rejection_push_reads_in_arabic_with_the_reason():
    from django.utils import translation

    from notifications.domain.verification_events import TrainerVerificationRejectedEvent
    from notifications.template_resolver import NotificationTemplateResolver

    with translation.override("ar"):
        title, body = NotificationTemplateResolver.render(
            "trainer_verification_rejected", TrainerVerificationRejectedEvent.template,
            {"reason": "صورة غير واضحة"})
    assert title == "طلب التوثيق يحتاج إلى تعديل"
    assert body == "لم نتمكن من توثيق حسابك: صورة غير واضحة"


# ===========================================================================
# 6. Deletion and retention
# ===========================================================================

def test_erasing_the_account_deletes_the_rows_and_the_files(
        make_user, api, django_capture_on_commit_callbacks, storage_roots):
    from training_platform.privacy import erase_user_data
    from trainer_verification import services
    from trainer_verification.models import (VerificationApplication, VerificationDecision,
                                             VerificationDocument)

    trainer, client = _submitted(make_user, api)
    application = _application(trainer)
    with django_capture_on_commit_callbacks(execute=True):
        services.decide(application.pk, _operator(make_user), approve=False,
                        reason="retake the photo",
                        document_reasons={application.documents.get(slot="photo").pk: "x"})
    post_document(client, "photo", jpeg())   # a replaced document too
    names = _stored_names(trainer)
    assert len(names) >= 8 and all((storage_roots / "private" / n).exists() for n in names)

    with django_capture_on_commit_callbacks(execute=True):
        report = erase_user_data(trainer)

    assert report["verification_documents"]["rows"] == 5
    assert not VerificationApplication.objects.filter(pk=application.pk).exists()
    assert not VerificationDocument.objects.filter(application_id=application.pk).exists()
    assert not VerificationDecision.objects.filter(application_id=application.pk).exists()
    leftover = [n for n in names if (storage_roots / "private" / n).exists()]
    assert not leftover, leftover


def test_the_export_lists_the_application_but_not_the_files_or_the_operator(make_user, api):
    from training_platform.privacy import export_user_data

    trainer, _client = _submitted(make_user, api)
    sections = export_user_data(trainer)["sections"]
    assert sections["verification_application"][0]["state"] == "pending"
    assert {row["slot"] for row in sections["verification_documents"]} == set(REQUIRED)
    assert all("file" not in row for row in sections["verification_documents"])


def test_the_registry_covers_the_new_models():
    from training_platform.privacy import audit_coverage, registry, validate_sources

    assert not [m for m in audit_coverage() if m.startswith("trainer_verification.")]
    assert not [p for p in validate_sources() if "verification" in p]
    for model in ("VerificationApplication", "VerificationDocument", "VerificationDecision"):
        assert registry[f"trainer_verification.{model}"].on_erase == "delete"


def test_a_replaced_document_goes_after_its_retention_and_never_while_pending(
        make_user, api, django_capture_on_commit_callbacks, storage_roots):
    from datetime import timedelta

    from django.utils import timezone

    from trainer_verification import services
    from trainer_verification.models import VerificationDocument
    from trainer_verification.tasks import purge_replaced_documents

    trainer, client = _submitted(make_user, api)
    application = _application(trainer)
    photo = application.documents.get(slot="photo")
    with django_capture_on_commit_callbacks(execute=True):
        services.decide(application.pk, _operator(make_user), approve=False,
                        reason="retake", document_reasons={photo.pk: "blurred"})
    post_document(client, "photo", jpeg())
    photo.refresh_from_db()
    assert photo.purge_after - photo.replaced_at == timedelta(days=90)
    files = [photo.file.name, photo.thumbnail.name]

    # Not yet due: kept.
    assert services.purge_replaced_documents() == 0
    # Due, but the replacement is waiting for review: kept, so the operator can compare.
    client.post(f"{BASE}submit/")
    later = timezone.now() + timedelta(days=91)
    assert services.purge_replaced_documents(now=later) == 0
    # Decided: it goes, files and all; the current documents stay.
    with django_capture_on_commit_callbacks(execute=True):
        services.decide(application.pk, _operator(make_user), approve=True)
    VerificationDocument.objects.filter(pk=photo.pk).update(
        purge_after=timezone.now() - timedelta(seconds=1))
    with django_capture_on_commit_callbacks(execute=True):
        assert purge_replaced_documents() == 1
    assert not VerificationDocument.objects.filter(pk=photo.pk).exists()
    assert not any((storage_roots / "private" / n).exists() for n in files)
    assert VerificationDocument.objects.filter(application=application,
                                               replaced_at__isnull=True).count() == 4
    # The decision still says what was decided about it.
    first = application.decisions.get(round=1)
    assert any(d["document_id"] == photo.pk and d["state"] == "rejected"
               for d in first.documents)


def test_a_document_nobody_reviewed_is_deleted_at_once_when_replaced(
        make_user, api, django_capture_on_commit_callbacks, storage_roots):
    from trainer_verification.models import VerificationDocument

    trainer = _trainer(make_user)
    client = api(trainer)
    post_document(client, "photo", jpeg())
    first = VerificationDocument.objects.get(application__trainer=trainer)
    names = [first.file.name, first.thumbnail.name]
    with django_capture_on_commit_callbacks(execute=True):
        post_document(client, "photo", jpeg())
    assert not VerificationDocument.objects.filter(pk=first.pk).exists()
    assert not any((storage_roots / "private" / n).exists() for n in names)
    with django_capture_on_commit_callbacks(execute=True):
        assert client.delete(f"{BASE}documents/photo/").json()["documents"][4]["state"] == "missing"
    assert not VerificationDocument.objects.filter(application__trainer=trainer).exists()


def test_the_purge_is_scheduled():
    from training_platform.celery import app

    tasks = {entry["task"] for entry in app.conf.beat_schedule.values()}
    assert "trainer_verification.purge_replaced_documents" in tasks


# ===========================================================================
# 7. The console
# ===========================================================================

def _console(user):
    from django.test import Client
    client = Client()
    client.force_login(user)
    return client


def test_the_queue_is_pending_only_oldest_first(make_user, api):
    from datetime import timedelta

    from django.utils import timezone

    from trainer_verification.models import VerificationApplication

    older, _a = _submitted(make_user, api)
    newer, _b = _submitted(make_user, api)
    draft = _trainer(make_user)
    fill_required(api(draft))
    VerificationApplication.objects.filter(trainer=older).update(
        submitted_at=timezone.now() - timedelta(days=2))

    page = _console(_operator(make_user)).get(
        "/dj-admin/trainer_verification/verificationqueueentry/")
    assert page.status_code == 200
    text = page.content.decode()
    assert older.email in text and newer.email in text and draft.email not in text
    assert text.index(older.email) < text.index(newer.email)


def test_a_staff_account_that_is_not_an_operator_sees_nothing(make_user, api):
    trainer, _client = _submitted(make_user, api)
    staff = make_user(_tag("tv_staff"), is_staff=True)
    console = _console(staff)
    application = _application(trainer)
    for url in ("/dj-admin/trainer_verification/verificationqueueentry/",
                f"/dj-admin/trainer_verification/verificationapplication/{application.pk}/change/"):
        assert console.get(url).status_code == 403
    refused = console.post(
        f"/dj-admin/trainer_verification/verificationapplication/{application.pk}/change/",
        {"action": "approve"})
    assert refused.status_code == 403
    application.refresh_from_db()
    assert application.state == "pending"


def test_an_operator_reviews_with_signed_links_and_decides_from_the_page(
        make_user, api, django_capture_on_commit_callbacks):
    from django.contrib.admin.models import LogEntry

    trainer, _client = _submitted(make_user, api)
    application = _application(trainer)
    photo = application.documents.get(slot="photo")
    operator = _operator(make_user)
    console = _console(operator)
    url = f"/dj-admin/trainer_verification/verificationapplication/{application.pk}/change/"

    page = console.get(url)
    assert page.status_code == 200
    html = page.content.decode()
    assert "/private-media/verification/" in html and "?e=" in html
    seen = re.search(r'name="seen" value="([0-9,]+)"', html).group(1)

    with django_capture_on_commit_callbacks(execute=True):
        decided = console.post(url, {"action": "reject", "reason": "the photo is blurred",
                                     f"doc_reason_{photo.pk}": "blurred", "seen": seen})
    assert decided.status_code == 302
    application.refresh_from_db()
    photo.refresh_from_db()
    assert application.state == "rejected" and application.decided_by == operator
    assert photo.state == "rejected" and photo.rejection_reason == "blurred"
    assert LogEntry.objects.filter(object_id=str(application.pk)).exists()


def test_the_flag_is_read_only_in_the_user_admin_and_the_bulk_action_is_gone(make_user):
    from django.contrib.admin.sites import site

    from users.admin import CustomUserAdmin
    from users.models import CustomUser

    admin_class = CustomUserAdmin(CustomUser, site)
    assert "trainer_is_verified" in admin_class.readonly_fields
    assert "trainer_is_verified" not in admin_class.list_editable
    names = [getattr(a, "__name__", a) for a in admin_class.actions]
    assert "make_verified" not in names

    trainer = make_user(_tag("tv_form"), user_type="trainer")
    operator = _operator(make_user)
    console = _console(operator)
    form = console.get(f"/dj-admin/users/customuser/{trainer.pk}/change/")
    assert form.status_code == 200
    assert b'name="trainer_is_verified"' not in form.content


def test_nothing_but_the_decision_writes_the_flag():
    """A source scan: `trainer_is_verified` is ASSIGNED only in
    trainer_verification/services.py (and the data migration only reads it)."""
    offenders = []
    for path in ROOT.rglob("*.py"):
        rel = path.relative_to(ROOT)
        if {"tests", "_excluded", ".venv", "migrations", ".claude", "mobile",
                "flutter", "yalla_gym"} & set(rel.parts) or "site-packages" in str(rel):
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        # An attribute assignment, or the flag handed to a writer as a keyword.
        for match in re.finditer(
                r"\.trainer_is_verified\s*=(?!=)"
                r"|(?:update|bulk_apply|create|update_or_create|get_or_create)\("
                r"[^)]*\btrainer_is_verified\s*=", text):
            offenders.append(f"{rel}:{text.count(chr(10), 0, match.start()) + 1}")
    allowed = {p for p in offenders if p.startswith("trainer_verification/services.py")}
    assert set(offenders) - allowed == set(), sorted(set(offenders) - allowed)
