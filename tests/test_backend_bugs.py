"""Regression tests for rows of BACKEND_BUGS.md, the notebook the app session keeps.

Each test is named after the row it closes and fails on the code as the app found it.
"""
import io
import random
from decimal import Decimal

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from PIL import Image

pytestmark = pytest.mark.django_db

JSON = "application/json"


@pytest.fixture(autouse=True)
def media_root(settings, tmp_path):
    settings.MEDIA_ROOT = str(tmp_path)


def _photo_containing(pattern: bytes) -> bytes:
    """A real, noisy 512 px JPEG whose compressed bytes happen to contain `pattern`."""
    for seed in range(500):
        rng = random.Random(seed)
        img = Image.frombytes("RGB", (512, 512), bytes(rng.getrandbits(8) for _ in range(512 * 512 * 3)))
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=85)
        data = buf.getvalue()
        if pattern in data:
            return data
    raise AssertionError(f"no photo with {pattern!r} in 500 tries")


# ------------------------------------------------------------------------ B-002
# `<%` only: the old scanner looked for it anywhere in the file, and noisy JPEG data
# contains it by chance. (`MZ` was only refused at the very start, where a JPEG never has
# it, so it would pass on the old code too.)
@pytest.mark.parametrize("pattern", [b"<%"])
def test_b002_a_genuine_photo_is_not_refused_as_malicious(make_user, api, pattern):
    """The byte scan looked for `<%` and friends inside compressed JPEG data, where they
    occur by chance: 36 of 40 genuine 512 px photos were refused. The image is decoded
    and re-encoded from pixels, so nothing in its bytes can reach storage anyway."""
    user = make_user("bb_photo")
    upload = SimpleUploadedFile("me.jpg", _photo_containing(pattern), content_type="image/jpeg")
    resp = api(user).post("/api/auth/user/profile-picture/", {"profile_picture": upload})
    assert resp.status_code == 200, resp.content
    user.refresh_from_db()
    assert user.profile_picture


def test_b002_a_refused_upload_says_why_with_its_own_code(make_user, api):
    user = make_user("bb_photo_big")
    upload = SimpleUploadedFile("big.jpg", b"\xff\xd8\xff" + b"0" * (3 * 1024 * 1024),
                                content_type="image/jpeg")
    resp = api(user).post("/api/auth/user/profile-picture/", {"profile_picture": upload})
    assert resp.status_code == 400
    assert resp.json()["code"] == "file_too_large"


def test_b002_documents_are_still_scanned():
    from django.core.exceptions import ValidationError

    from training_platform.file_security import FileSecurityValidator

    fake = SimpleUploadedFile("cv.pdf", b"%PDF-1.4\n<script>alert(1)</script>",
                              content_type="application/pdf")
    with pytest.raises(ValidationError):
        FileSecurityValidator().validate_file(fake, "document")


# ------------------------------------------------------------------------ B-007
@pytest.mark.parametrize("channel,shown", [("apk", True), ("ios", False), ("", False)])
def test_b007_legacy_trainer_payloads_carry_no_coaching_price_where_the_build_hides_it(
        make_user, api, channel, shown):
    from users.models import TrainerClientRelation

    trainer = make_user("bb_price_t", user_type="trainer", trainer_hourly_rate=Decimal("12000"),
                        first_name="Pricey")
    client = make_user("bb_price_c")
    TrainerClientRelation.objects.create(trainer=trainer, client=client, status="pending",
                                         initiated_by="client", agreed_rate=Decimal("12000"))
    inviter = make_user("bb_price_inv", user_type="trainer", trainer_hourly_rate=Decimal("9000"))
    TrainerClientRelation.objects.create(trainer=inviter, client=client, status="pending",
                                         initiated_by="trainer", agreed_rate=Decimal("9000"))
    header = {"HTTP_X_APP_CHANNEL": channel}

    public = api().get("/api/auth/trainers/public/?search=Pricey", **header).json()
    listed = api(client).get("/api/auth/client/available-trainers/?search=Pricey", **header).json()
    status_ = api(client).get("/api/auth/client/request-status/", **header).json()
    invites = api(client).get("/api/auth/client/trainer-invites/", **header).json()

    prices = ([t["trainer_hourly_rate"] for t in public["results"]["available_trainers"]]
              + [t["trainer_hourly_rate"] for t in listed["results"]["available_trainers"]]
              + [r["trainer_hourly_rate"] for r in status_["requests"]]
              + [r["agreed_rate"] for r in status_["requests"]]
              + [i["agreed_rate"] for i in invites["invitations"]])
    assert prices, "nothing was listed"
    if shown:
        assert all(p is not None for p in prices)
    else:
        assert all(p is None for p in prices), "the key stays; the value is withheld"


# ------------------------------------------------------------------------ B-008
def test_b008_each_request_refusal_has_its_own_code(make_user, api):
    from wallet.models import Wallet

    client = make_user("bb_req_c")
    closed = make_user("bb_req_closed", user_type="trainer", trainer_is_available=False)
    pricey = make_user("bb_req_pricey", user_type="trainer", trainer_hourly_rate=Decimal("500"))
    Wallet.objects.get_or_create(owner=client, defaults={"owner_type": "client"})

    def ask(**body):
        return api(client).post("/api/auth/client/request-trainer/", body, content_type=JSON)

    assert ask().json()["code"] == "trainer_id_required"
    assert ask(trainer_id=closed.pk).json()["code"] == "trainer_not_accepting"
    short = ask(trainer_id=pricey.pk)
    assert (short.status_code, short.json()["code"]) == (402, "insufficient_balance")
    assert ask(trainer_id=closed.pk, note="x" * 501).json()["code"] == "note_too_long"
    assert ask(trainer_id=closed.pk, note=["not", "text"]).json()["code"] == "invalid_note"


def test_b008_the_note_reaches_the_trainer_and_a_resent_request_replaces_it(make_user, api):
    from users.models import TrainerClientRelation

    trainer = make_user("bb_note_t", user_type="trainer")
    client = make_user("bb_note_c")
    sent = api(client).post("/api/auth/client/request-trainer/",
                            {"trainer_id": trainer.pk, "message": "Knee surgery last year."},
                            content_type=JSON)
    assert sent.status_code == 200, sent.content
    (row,) = api(trainer).get("/api/auth/trainer/pending-requests/").json()["pending_requests"]
    assert row["note"] == "Knee surgery last year."
    mine = api(client).get("/api/auth/client/request-status/").json()["requests"][0]
    assert mine["note"] == "Knee surgery last year." and mine["responded_at"] is None

    relation = TrainerClientRelation.objects.get(trainer=trainer, client=client)
    api(trainer).post("/api/auth/trainer/respond-to-request/",
                      {"request_id": relation.pk, "action": "reject"}, content_type=JSON)
    api(client).post("/api/auth/client/request-trainer/", {"trainer_id": trainer.pk},
                     content_type=JSON)
    (row,) = api(trainer).get("/api/auth/trainer/pending-requests/").json()["pending_requests"]
    assert row["note"] == "", "a declined request's note does not travel with the next one"
    relation.refresh_from_db()
    assert relation.requested_at > relation.created_at, "the re-sent request has its own time"


def test_b008_the_note_is_encrypted_at_rest(make_user, api):
    from django.db import connection

    from users.models import TrainerClientRelation

    trainer = make_user("bb_enc_t", user_type="trainer")
    client = make_user("bb_enc_c")
    api(client).post("/api/auth/client/request-trainer/",
                     {"trainer_id": trainer.pk, "note": "Lower back pain"}, content_type=JSON)
    relation = TrainerClientRelation.objects.get(trainer=trainer, client=client)
    with connection.cursor() as cur:
        cur.execute("SELECT client_note FROM users_trainerclientrelation WHERE id = %s",
                    [relation.pk])
        assert "back" not in cur.fetchone()[0]


# ------------------------------------------------------------------------ B-015
def test_b015_a_phone_number_in_arabic_digits_is_stored_in_western_digits(api):
    from users.models import CustomUser

    resp = api().post("/api/auth/register/", {
        "username": "bb_digits", "email": "bb_digits@gate.test",
        "phone_number": "+٩٦٣٩٩٩٩٩٩٩٩٩", "password1": "Xx!23456long",
        "password2": "Xx!23456long", "date_of_birth": "1995-06-15"}, content_type=JSON)
    assert resp.status_code == 201, resp.content
    assert CustomUser.objects.get(username="bb_digits").phone_number == "+963999999999"


def test_b015_a_code_typed_in_arabic_digits_is_the_same_code(make_user):
    from users.utils import _hash_otp

    assert _hash_otp("١٢٣٤٥٦") == _hash_otp("123456") == _hash_otp("۱۲۳۴۵۶")


def test_b002_an_exercise_photo_is_stored_re_encoded_under_a_server_name(make_user, api):
    """The review found one caller that kept the ORIGINAL upload after validating it —
    so with the byte scan gone, a JPEG with a script appended, named `x.html`, would have
    been stored as it came. The re-encoded image is what is stored now."""
    from django.core.files.storage import default_storage

    from routine.models import ExerciseMedia

    trainer = make_user("bb_ex_t", user_type="trainer")
    buf = io.BytesIO()
    Image.new("RGB", (64, 64), (10, 20, 30)).save(buf, format="JPEG")
    payload = buf.getvalue() + b"<script>alert(1)</script>"
    resp = api(trainer).post("/api/routine/exercises/create-with-image/", {
        "name": "bb squat", "description": "d", "target_muscle": "Biceps",
        "media_photos": [SimpleUploadedFile("x.html", payload, content_type="image/jpeg")],
    })
    assert resp.status_code in (200, 201), resp.content
    media = ExerciseMedia.objects.get(exercise__name="bb squat", media_type="photo")
    path = media.content.split("/media/", 1)[1].split("?", 1)[0]
    assert path.endswith(".jpg") and "x.html" not in path
    with default_storage.open(path) as stored:
        assert b"<script" not in stored.read()
