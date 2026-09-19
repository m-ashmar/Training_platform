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


# ------------------------------------------------------------------------ B-003
def _session_setup(make_user, tag):
    from django.utils import timezone

    from routine.models import Exercise, Routine, RoutineExercise, UserExerciseProgress

    trainer = make_user(f"bb3_t_{tag}", user_type="trainer")
    client = make_user(f"bb3_c_{tag}")
    routine = Routine.objects.create(name=f"bb3 {tag}", created_by=trainer, days=1)
    exercise = Exercise.objects.create(name=f"bb3 ex {tag}", created_by=trainer)
    RoutineExercise.objects.create(routine=routine, exercise=exercise, day=1, sets=3, reps=10)
    routine.assigned_to.add(client)
    progress = UserExerciseProgress.objects.create(user=client, exercise=exercise,
                                                   date=timezone.localdate(), target_sets=3)
    return trainer, client, routine, progress


def test_b003_a_retried_session_and_set_answer_with_the_first_row(make_user, api):
    import uuid

    from routine.models import ExerciseSetLog, WorkoutSession

    trainer, client, routine, progress = _session_setup(make_user, "replay")
    key = str(uuid.uuid4())
    body = {"routine": routine.pk, "day": 1, "client_uuid": key}
    first = api(client).post("/api/routine/workout-sessions/", body, content_type=JSON)
    again = api(client).post("/api/routine/workout-sessions/", body, content_type=JSON)
    assert (first.status_code, again.status_code) == (201, 200), (first.content, again.content)
    assert first.json()["id"] == again.json()["id"]
    assert WorkoutSession.objects.filter(user=client).count() == 1

    set_key = str(uuid.uuid4())
    set_body = {"user_exercise_progress": progress.pk, "workout_session": first.json()["id"],
                "set_number": 1, "weight": 60, "reps": 8, "client_uuid": set_key}
    one = api(client).post("/api/routine/set-logs/", set_body, content_type=JSON)
    two = api(client).post("/api/routine/set-logs/", set_body, content_type=JSON)
    assert (one.status_code, two.status_code) == (201, 200), (one.content, two.content)
    assert ExerciseSetLog.objects.filter(client_uuid=set_key).count() == 1

    stranger = make_user("bb3_stranger")
    taken = api(stranger).post("/api/routine/set-logs/", set_body, content_type=JSON)
    assert (taken.status_code, taken.json()["code"]) == (409, "idempotency_conflict")


def test_b003_an_offline_workout_keeps_its_own_clock(make_user, api):
    from datetime import timedelta

    from django.utils import timezone

    from routine.models import WorkoutSession

    trainer, client, routine, progress = _session_setup(make_user, "clock")
    began = timezone.now() - timedelta(days=2, hours=3)
    resp = api(client).post("/api/routine/workout-sessions/", {
        "routine": routine.pk, "day": 1, "start_time": began.isoformat(),
        "end_time": (began + timedelta(minutes=50)).isoformat()}, content_type=JSON)
    assert resp.status_code == 201, resp.content
    session = WorkoutSession.objects.get(pk=resp.json()["id"])
    assert abs((session.start_time - began).total_seconds()) < 1
    assert resp.json()["duration"] == 50 * 60

    def refused(start, end=None):
        body = {"routine": routine.pk, "day": 1, "start_time": start.isoformat()}
        if end is not None:
            body["end_time"] = end.isoformat()
        r = api(client).post("/api/routine/workout-sessions/", body, content_type=JSON)
        assert r.status_code == 400, r.content
        return r.json()["field_errors"]

    now = timezone.now()
    # Finishing a backdated session online never books days of training.
    late = api(client).post("/api/routine/workout-sessions/", {
        "routine": routine.pk, "day": 1,
        "start_time": (now - timedelta(days=3)).isoformat()}, content_type=JSON).json()
    done = api(client).patch(f"/api/routine/workout-sessions/{late['id']}/",
                             {"status": "completed"}, content_type=JSON)
    assert done.status_code == 200, done.content
    assert done.json()["duration"] <= 12 * 3600
    assert refused(now + timedelta(hours=1))["start_time"][0]["code"] == "start_time_in_future"
    assert refused(now - timedelta(days=40))["start_time"][0]["code"] == "start_time_too_old"
    assert "end_time" in refused(now - timedelta(hours=2), now - timedelta(hours=3))


# ------------------------------------------------------------------------ B-004
def test_b004_failed_token_attempts_lock_the_address_with_a_code(make_user, api):
    user = make_user("bb4_lock")
    creds = {"email": user.email, "password": "wrong-password-1"}
    for _ in range(5):
        assert api().post("/api/auth/token/", creds, content_type=JSON).status_code == 401
    locked = api().post("/api/auth/token/", {**creds, "password": "Xx!23456"},
                        content_type=JSON)
    assert locked.status_code == 400
    body = locked.json()
    assert body["code"] == "too_many_attempts" and body["retry_after_seconds"] > 0
    assert body["non_field_errors"], "the old key stays"


# ------------------------------------------------------------------------ B-005
def test_b005_the_language_of_emails_and_pushes_can_be_set(make_user, api):
    user = make_user("bb5_lang")
    assert api(user).post("/api/auth/user/update/", {"preferred_language": "ar"},
                          content_type=JSON).status_code == 200
    user.refresh_from_db()
    assert user.preferred_language == "ar"
    bad = api(user).post("/api/auth/user/update/", {"preferred_language": "fr"},
                         content_type=JSON)
    assert bad.status_code == 400


# ------------------------------------------------------------------------ B-006
def test_b006_a_bearer_caller_is_limited_as_their_role(make_user):
    from django.test import RequestFactory
    from rest_framework_simplejwt.tokens import RefreshToken

    from training_platform.middleware import RateLimitMiddleware

    middleware = RateLimitMiddleware(lambda r: None)
    for user_type in ("client", "trainer"):
        user = make_user(f"bb6_{user_type}", user_type=user_type)
        token = RefreshToken.for_user(user).access_token
        request = RequestFactory().get("/api/routine/routines/",
                                       HTTP_AUTHORIZATION=f"Bearer {token}")
        assert middleware._get_user_type(request) == user_type
    assert middleware._get_user_type(RequestFactory().get("/")) == "anonymous"


# ------------------------------------------------------------------------ B-010
def test_b010_a_new_trainer_is_not_listed_until_they_open(api):
    from users.models import CustomUser

    resp = api().post("/api/auth/register/", {
        "username": "bb10_coach", "email": "bb10_coach@gate.test",
        "phone_number": "+963999999999", "password1": "Xx!23456long",
        "password2": "Xx!23456long", "user_type": "trainer", "date_of_birth": "1990-01-01"},
        content_type=JSON)
    assert resp.status_code == 201, resp.content
    assert CustomUser.objects.get(username="bb10_coach").trainer_is_available is False


# ------------------------------------------------------------------------ B-012
def test_b012_a_prescribed_working_weight_can_be_written_and_read(make_user, api):
    from routine.models import Exercise, Routine

    trainer = make_user("bb12_t", user_type="trainer")
    routine = Routine.objects.create(name="bb12", created_by=trainer, days=1)
    exercise = Exercise.objects.create(name="bb12 bench", created_by=trainer)
    made = api(trainer).post("/api/routine/routine-exercises/", {
        "routine": routine.pk, "exercise_id": exercise.pk, "sets": 3, "reps": 8,
        "day": 1, "order": 1, "weight": 62.5}, content_type=JSON)
    assert made.status_code == 201, made.content
    assert made.json()["weight"] == 62.5
    too_heavy = api(trainer).post("/api/routine/routine-exercises/", {
        "routine": routine.pk, "exercise_id": exercise.pk, "sets": 3, "reps": 8,
        "day": 1, "order": 2, "weight": 5000}, content_type=JSON)
    assert too_heavy.status_code == 400


# ------------------------------------------------------------------------ B-013
def test_b013_a_heavier_set_than_ever_before_is_a_record(make_user, api):
    from datetime import timedelta

    from django.utils import timezone

    from routine.models import Exercise, ExerciseSetLog, UserExerciseProgress

    client = make_user("bb13_c")
    today = timezone.localdate()
    bench = Exercise.objects.create(name="bb13 bench")
    squat = Exercise.objects.create(name="bb13 squat")
    old = UserExerciseProgress.objects.create(user=client, exercise=bench,
                                              date=today - timedelta(days=20))
    ExerciseSetLog.objects.create(user_exercise_progress=old, set_number=1, weight=50,
                                  reps=5, date=today - timedelta(days=20))
    new = UserExerciseProgress.objects.create(user=client, exercise=bench, date=today)
    ExerciseSetLog.objects.create(user_exercise_progress=new, set_number=1, weight=55,
                                  reps=5, date=today)
    first_squat = UserExerciseProgress.objects.create(user=client, exercise=squat, date=today)
    ExerciseSetLog.objects.create(user_exercise_progress=first_squat, set_number=1,
                                  weight=80, reps=5, date=today)   # a first log: a baseline
    body = api(client).get("/api/routine/analytics/summary/?period=week").json()
    assert body["prs"] == 1


def test_b013_progress_inside_the_window_counts_even_for_a_first_month(make_user, api):
    from datetime import timedelta

    from django.utils import timezone

    from routine.models import Exercise, ExerciseSetLog, UserExerciseProgress

    client = make_user("bb13_new")
    today = timezone.localdate()
    press = Exercise.objects.create(name="bb13 press")
    for days_ago, weight in ((5, 40), (2, 45)):     # both inside the week, first ever at 40
        progress = UserExerciseProgress.objects.create(
            user=client, exercise=press, date=today - timedelta(days=days_ago))
        ExerciseSetLog.objects.create(user_exercise_progress=progress, set_number=1,
                                      weight=weight, reps=5,
                                      date=today - timedelta(days=days_ago))
    assert api(client).get("/api/routine/analytics/summary/?period=week").json()["prs"] == 1


# ------------------------------------------------------------------------ B-014
def test_b014_an_unknown_notification_type_is_refused(make_user, api):
    user = make_user("bb14_u")
    bad = api(user).post("/api/notifications/preferences/",
                         {"event_type": "post_likd", "is_enabled": False}, content_type=JSON)
    assert bad.status_code == 400
    assert bad.json()["field_errors"]["event_type"][0]["code"] == "unknown_event_type"
    good = api(user).post("/api/notifications/preferences/",
                          {"event_type": "post_liked", "is_enabled": False}, content_type=JSON)
    assert good.status_code in (200, 201), good.content


# ------------------------------------------------------------------------ B-016
def test_b016_notifications_written_in_the_same_instant_page_without_repeats(make_user, api):
    from notifications.models import Notification

    user = make_user("bb16_u")
    rows = [Notification.objects.create(recipient=user, event_type="custom",
                                        deduplication_key=f"bb16-{i}") for i in range(4)]
    Notification.objects.filter(pk__in=[r.pk for r in rows]).update(
        created_at=rows[0].created_at)
    seen, url = [], "/api/social/notifications/?page_size=1"
    while url:
        page = api(user).get(url).json()
        seen += [n["id"] for n in page["results"]]
        url = page["next"]
    # Every row once, in the tie-break's order — not merely the right set.
    expected = [str(pk) for pk in Notification.objects.filter(recipient=user)
                .order_by("-created_at", "-id").values_list("pk", flat=True)]
    assert seen == expected


def test_b016_comments_page_by_id_after_time():
    from social.views import CommentViewSet
    assert CommentViewSet.ordering == ['-created_at', '-id']


# ------------------------------------------------------------------------ B-017
def test_b017_a_password_reset_ends_the_lockout(make_user, api):
    from users.models import PasswordResetToken

    user = make_user("bb17_u")
    for _ in range(5):
        api().post("/api/auth/token/", {"email": user.email, "password": "nope-nope-1"},
                   content_type=JSON)
    from users.utils import check_login_lockout
    assert check_login_lockout(user.email)[0] is True, "the address must be locked first"
    _token, raw = PasswordResetToken.issue(user)
    reset = api().post("/api/auth/forgot-password/confirm/",
                       {"reset_token": raw, "new_password": "Fresh!Pass123"}, content_type=JSON)
    assert reset.status_code == 200, reset.content
    signed_in = api().post("/api/auth/token/",
                           {"email": user.email, "password": "Fresh!Pass123"}, content_type=JSON)
    assert signed_in.status_code == 200, signed_in.content


# ------------------------------------------------------------------------ B-018
def test_b018_yesterdays_advice_is_not_todays(make_user, api):
    from datetime import timedelta

    from django.utils import timezone

    from diet.models import DailyAdvice
    from tests.test_regression_gate import _grant_diet_access

    user = make_user("bb18_u")
    _grant_diet_access(user)
    today = timezone.localdate()
    DailyAdvice.objects.create(user=user, text="old news", for_date=today - timedelta(days=3),
                               context_data={})
    assert api(user).get("/api/diet/v1/advice/latest/").json()["advice"] is None
    DailyAdvice.objects.create(user=user, text="today", for_date=today, context_data={})
    assert api(user).get("/api/diet/v1/advice/latest/").json()["advice"]["text"] == "today"


def test_b018_advice_dated_yesterday_by_the_server_clock_still_shows(make_user, api):
    """The sweep runs at 06:00 in Damascus, the evening before for anyone far west."""
    from datetime import timedelta

    from django.utils import timezone

    from diet.models import DailyAdvice
    from tests.test_regression_gate import _grant_diet_access

    user = make_user("bb18_west")
    user.preferred_timezone = "America/New_York"
    user.save(update_fields=["preferred_timezone"])
    _grant_diet_access(user)
    local_today = timezone.localtime(timezone.now(), __import__("zoneinfo").ZoneInfo(
        "America/New_York")).date()
    DailyAdvice.objects.create(user=user, text="made last night", context_data={},
                               for_date=local_today - timedelta(days=1))
    assert api(user).get("/api/diet/v1/advice/latest/").json()["advice"]["text"] == \
        "made last night"


def test_b003_a_workout_synced_already_finished_is_treated_as_finished(make_user, api):
    """Created with status completed: the same notifications and milestones as a PATCH."""
    from datetime import timedelta
    from unittest import mock

    from django.utils import timezone

    trainer, client, routine, progress = _session_setup(make_user, "done")
    began = timezone.now() - timedelta(hours=5)
    with mock.patch("routine.views.send_notification") as notified:
        resp = api(client).post("/api/routine/workout-sessions/", {
            "routine": routine.pk, "day": 1, "status": "completed",
            "start_time": began.isoformat(),
            "end_time": (began + timedelta(minutes=45)).isoformat()}, content_type=JSON)
    assert resp.status_code == 201, resp.content
    assert notified.called, "finishing offline must notify like finishing online"


def test_b003_a_retry_racing_its_first_attempt_is_not_told_the_id_is_taken(make_user, api):
    """No uniqueness validator on `client_uuid`: the index decides and the answer is
    the first row, whatever order the two attempts reach the database in."""
    from routine.serializers import ExerciseSetLogSerializer, WorkoutSessionSerializer

    for serializer in (ExerciseSetLogSerializer(), WorkoutSessionSerializer()):
        assert serializer.fields["client_uuid"].validators == []


def test_b006_a_cache_outage_does_not_fail_signed_in_requests(make_user, monkeypatch):
    from django.test import RequestFactory
    from rest_framework_simplejwt.tokens import RefreshToken

    import training_platform.middleware as mw

    class Down:
        def get(self, *a, **k):
            raise ConnectionError("redis is down")
        set = incr = get

    monkeypatch.setattr(mw, "ratelimit_cache", lambda: Down())
    user = make_user("bb6_outage", user_type="trainer")
    token = RefreshToken.for_user(user).access_token
    request = RequestFactory().get("/api/routine/routines/", HTTP_AUTHORIZATION=f"Bearer {token}")
    middleware = mw.RateLimitMiddleware(lambda r: None)
    assert middleware._get_user_type(request) == "trainer"
    assert middleware.process_request(request) is None, "the limiter fails open"


# --------------------------------------------------------- batch 3: owner's answers
@pytest.mark.parametrize("path", ["/api/auth/token/", "/api/auth/login/"])
def test_b004_a_suspended_account_is_told_so_only_after_its_password(make_user, api, path):
    user = make_user("bb4_banned")
    user.is_active = False
    user.save(update_fields=["is_active"])
    right = api().post(path, {"email": user.email, "password": "Xx!23456"}, content_type=JSON)
    assert (right.status_code, right.json()["code"]) == (403, "account_disabled")
    wrong = api().post(path, {"email": user.email, "password": "nope-nope-1"},
                       content_type=JSON)
    assert wrong.json().get("code") != "account_disabled"


def test_b011_completing_a_day_with_unlogged_sets_needs_the_warning_first(make_user, api):
    from routine.models import ExerciseSetLog, RoutineProgress

    trainer, client, routine, progress = _session_setup(make_user, "warn")
    ExerciseSetLog.objects.create(user_exercise_progress=progress, set_number=1, weight=40,
                                  reps=8)
    url = f"/api/routine/routines/{routine.pk}/update_progress/"
    warned = api(client).post(url, {"day": 1, "status": "completed"}, content_type=JSON)
    assert warned.status_code == 409
    assert {k: warned.json()[k] for k in ("code", "prescribed", "logged", "unlogged")} == \
        {"code": "unlogged_sets", "prescribed": 3, "logged": 1, "unlogged": 2}
    assert not RoutineProgress.objects.filter(user=client, routine=routine, day=1,
                                              status="completed").exists()
    confirmed = api(client).post(url, {"day": 1, "status": "completed",
                                       "confirm_unlogged": True}, content_type=JSON)
    assert confirmed.status_code == 200, confirmed.content


def test_b011_log_a_whole_day_marks_only_that_routines_day(make_user, api):
    from django.utils import timezone

    from routine.models import Exercise, Routine, RoutineExercise, RoutineProgress

    trainer = make_user("bb11_t", user_type="trainer")
    client = make_user("bb11_c")
    squat = Exercise.objects.create(name="bb11 squat", created_by=trainer)
    posted = Routine.objects.create(name="bb11 posted", created_by=trainer, days=1)
    other = Routine.objects.create(name="bb11 other", created_by=trainer, days=1)
    for routine in (posted, other):
        RoutineExercise.objects.create(routine=routine, exercise=squat, day=1, sets=3, reps=5)
        routine.assigned_to.add(client)
    resp = api(client).post("/api/routine/user-exercise-progress/bulk-complete/", {
        "routine_id": posted.pk, "day": 1, "date": timezone.localdate().isoformat(),
        "completed_sets": 3}, content_type=JSON)
    assert resp.status_code == 200, resp.content
    today = timezone.localdate()
    assert RoutineProgress.objects.get(user=client, routine=posted, day=1, date=today).status \
        == "completed"
    assert not RoutineProgress.objects.filter(user=client, routine=other, date=today,
                                              status="completed").exists()


def test_b014_a_muted_type_keeps_its_inbox_row_and_all_mutes_every_push(make_user):
    from unittest import mock

    from notifications.models import Notification, UserNotificationPreference
    from notifications.services import NotificationService

    user = make_user("bb14_mute")
    UserNotificationPreference.objects.create(user=user, event_type="custom", is_enabled=False)
    with mock.patch("notifications.services.FCMChannel.send") as pushed:
        made = NotificationService.create_and_send(
            recipient=user, event_type="custom", related_object_id="a", metadata={})
    assert made is not None and Notification.objects.filter(pk=made.pk).exists()
    assert not pushed.called, "a muted type is not pushed"

    other = make_user("bb14_master")
    UserNotificationPreference.objects.create(user=other, event_type="all", is_enabled=False)
    with mock.patch("notifications.services.FCMChannel.send") as pushed:
        NotificationService.create_and_send(
            recipient=other, event_type="post_liked", related_object_id="b", metadata={})
    assert not pushed.called, "`all` off stops every push"
    assert UserNotificationPreference.is_channel_enabled(make_user("bb14_default"), "custom")


def test_b001_clients_never_see_each_other_on_a_routine(make_user, api):
    from routine.models import Routine

    trainer = make_user("bb1_t", user_type="trainer")
    me, other = make_user("bb1_me"), make_user("bb1_other")
    routine = Routine.objects.create(name="bb1 shared", created_by=trainer, days=1)
    routine.assigned_to.add(me, other)    # data from before one-client-per-routine
    for body in (api(me).get("/api/routine/routines/").json()["results"],
                 [api(me).get(f"/api/routine/routines/{routine.pk}/").json()]):
        row = next(r for r in body if r["id"] == routine.pk)
        assert row["assigned_usernames"] == [me.username]
        assert row["client_count"] == 1
        assert all(other.username not in s for s in row["assigned_to"])
    mine = api(trainer).get(f"/api/routine/routines/{routine.pk}/").json()
    assert sorted(mine["assigned_usernames"]) == sorted([me.username, other.username])


def test_b001_a_routine_belongs_to_one_client(make_user, api):
    from routine.models import Routine
    from users.models import TrainerClientRelation

    trainer = make_user("bb1_owner", user_type="trainer")
    first, second = make_user("bb1_first"), make_user("bb1_second")
    for c in (first, second):
        TrainerClientRelation.objects.create(trainer=trainer, client=c, status="approved",
                                             initiated_by="client")
    routine = Routine.objects.create(name="bb1 private", created_by=trainer, days=1)
    url = f"/api/routine/routines/{routine.pk}/assign_to_client/"
    assert api(trainer).post(url, {"client_id": first.pk}, content_type=JSON).status_code == 200
    again = api(trainer).post(url, {"client_id": second.pk}, content_type=JSON)
    assert (again.status_code, again.json()["code"]) == (409, "routine_belongs_to_another_client")


def test_b001_a_trainer_shows_a_few_public_routines_on_their_page(make_user, api):
    from routine.models import RoutineTemplate

    trainer = make_user("bb1_pub", user_type="trainer")
    for i in range(8):
        RoutineTemplate.objects.create(name=f"bb1 public {i}", goal="Strength", days=3,
                                       created_by=trainer, is_public=True)
    RoutineTemplate.objects.create(name="bb1 private tpl", goal="Strength", days=3,
                                   created_by=trainer, is_public=False)
    body = api().get(f"/api/v1/trainers/{trainer.pk}/").json()
    names = [r["name"] for r in body["public_routines"]]
    assert len(names) == 6 and "bb1 private tpl" not in names and names[0] == "bb1 public 7"


def test_b006_signed_in_clients_get_the_owners_1500_an_hour(settings):
    from training_platform.middleware import RateLimitMiddleware

    settings.DEBUG = False
    assert RateLimitMiddleware(lambda r: None).rate_limits["client"]["requests"] == 1500


# --------------------------------------------------------- batch 3: review fixes
def test_b014_a_muted_reminder_is_not_written_at_all(make_user, monkeypatch):
    """A reminder is a nudge, not a record: muting it stops the daily inbox row too."""
    import datetime as _dt

    from django.utils import timezone as _tz

    from notifications.models import Notification, UserNotificationPreference
    from notifications.tasks import send_workout_reminders
    from routine.models import Routine, RoutineProgress

    trainer = make_user("bb14r_t", user_type="trainer")
    routine = Routine.objects.create(name="bb14 reminders", created_by=trainer, days=3,
                                     start_date=_dt.date(2026, 3, 1))
    people = []
    for name in ("bb14r_on", "bb14r_off"):
        user = make_user(name)
        user.preferred_timezone, user.workout_reminder_hour = "UTC", 20
        user.save()
        routine.assigned_to.add(user)
        RoutineProgress.objects.get_or_create(user=user, routine=routine, day=1,
                                              date=routine.start_date,
                                              defaults={"status": "not_started"})
        people.append(user)
    UserNotificationPreference.objects.create(user=people[1], event_type="session_reminder",
                                              is_enabled=False)
    monkeypatch.setattr(_tz, "now", lambda: _dt.datetime(2026, 3, 10, 20, 5, tzinfo=_dt.timezone.utc))
    send_workout_reminders()
    counts = [Notification.objects.filter(recipient=u, event_type="session_reminder").count()
              for u in people]
    assert counts == [1, 0]


def test_b011_a_day_logged_as_a_whole_is_not_warned_about(make_user, api):
    from django.utils import timezone

    trainer, client, routine, progress = _session_setup(make_user, "whole")
    logged = api(client).post("/api/routine/user-exercise-progress/bulk-complete/", {
        "routine_id": routine.pk, "day": 1, "date": timezone.localdate().isoformat(),
        "completed_sets": 3}, content_type=JSON)
    assert logged.status_code == 200, logged.content
    done = api(client).post(f"/api/routine/routines/{routine.pk}/update_progress/",
                            {"day": 1, "status": "completed"}, content_type=JSON)
    assert done.status_code == 200, done.content


def test_b011_an_exercise_with_no_set_count_still_asks_for_one(make_user, api):
    from routine.models import Exercise, RoutineExercise

    trainer, client, routine, progress = _session_setup(make_user, "noset")
    RoutineExercise.objects.create(routine=routine, day=1, sets=None, reps=None,
                                   exercise=Exercise.objects.create(name="bb11 treadmill",
                                                                    created_by=trainer))
    warned = api(client).post(f"/api/routine/routines/{routine.pk}/update_progress/",
                              {"day": 1, "status": "completed"}, content_type=JSON)
    assert warned.status_code == 409 and warned.json()["prescribed"] == 4


def test_b011_a_confirmed_completed_day_stays_completed(make_user, api):
    from routine.models import ExerciseSetLog, RoutineProgress, WorkoutSession

    trainer, client, routine, progress = _session_setup(make_user, "stays")
    api(client).post(f"/api/routine/routines/{routine.pk}/update_progress/",
                     {"day": 1, "status": "completed", "confirm_unlogged": True},
                     content_type=JSON)
    session = WorkoutSession.objects.create(user=client, routine=routine, day=1)
    from routine.models import refresh_routine_progress
    ExerciseSetLog.objects.create(user_exercise_progress=progress, workout_session=session,
                                  set_number=1, weight=40, reps=8)
    progress.refresh_from_db()
    refresh_routine_progress(progress)
    assert RoutineProgress.objects.get(user=client, routine=routine, day=1,
                                       date=progress.date).status == "completed"


def test_b001_a_trainer_copies_a_routine_for_another_client(make_user, api):
    from routine.models import Exercise, Routine, RoutineExercise

    trainer = make_user("bb1_copier", user_type="trainer")
    client = make_user("bb1_copy_c")
    routine = Routine.objects.create(name="bb1 program", created_by=trainer, days=2)
    RoutineExercise.objects.create(routine=routine, day=2, sets=4, reps=6, weight=70,
                                   exercise=Exercise.objects.create(name="bb1 row",
                                                                    created_by=trainer))
    routine.assigned_to.add(client)
    made = api(trainer).post(f"/api/routine/routines/{routine.pk}/copy/")
    assert made.status_code == 201, made.content
    copy = Routine.objects.get(pk=made.json()["id"])
    assert copy.pk != routine.pk and copy.assigned_to.count() == 0
    assert list(copy.routine_exercises.values_list("day", "sets", "weight")) == [(2, 4, 70.0)]
    stranger = make_user("bb1_not_mine", user_type="trainer")
    assert api(stranger).post(f"/api/routine/routines/{routine.pk}/copy/").status_code in (403, 404)
