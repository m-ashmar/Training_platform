"""The second batch of `SIDE_BUGS.md` findings, one test per row.

Each test names the defect it closes and fails against the behaviour that was there:

    S-002  `completed_sets`/`target_sets` were writable, so a client could author
           their own compliance — a finished day with no set log behind it
    S-003  a set log's `date` was independent of its progress row's, so one set
           landed on two different days in two different figures
    S-004  `trainer/client-progress/{id}/` answered `client_info` where its own
           docstring promised `client`
    S-005  `IsSessionOwnerOrTrainerOrAdmin` carried a POST branch that never ran and
           said the opposite of the rule that does
    S-006  the exercise-image upload handed the client `str(exception)`
    S-009  the push table in `API_CONTRACT.md` had drifted from the FCM registry, and
           nothing checked it — this file is now that gate
    S-010  `.gitignore` matched `.venv/` but not the `.venv` SYMLINK a worktree gets
    S-016  `profile_picture` came back relative where everything else is absolute
    S-017  exercise media `content` accepted ANY http(s) host
    S-020  `CacheMiddleware` ASSIGNED `Vary`, discarding what was already there
    S-028  the privacy audit's app list omitted `mediakit`, exempting it silently
    S-031  two exercise routes hand-rolled an exercise dict, so `media_slots` — the
           field they exist to serve — was invisible from them
"""
import io
import json
import re
from pathlib import Path

import pytest
from django.utils import timezone

pytestmark = pytest.mark.django_db

JSON = "application/json"
REPO_ROOT = Path(__file__).resolve().parent.parent

#: An allowed media host: the platform's own object storage. See
#: `settings.EXERCISE_MEDIA_ALLOWED_HOSTS`.
OURS = "https://storage.googleapis.com/yalla-gym/demo.mp4"
THEIRS = "https://evil.example.net/demo.mp4"


@pytest.fixture(autouse=True)
def _our_bucket_is_allowed(settings):
    """The bucket `OURS` lives in is the project's own. Since SIDE_BUGS S-081 the
    allow-list names the project's BUCKET (`storage.googleapis.com/<GS_BUCKET_NAME>`),
    never the bare shared host every GCS bucket answers on, and this suite runs with no
    bucket configured — so the bucket is named here, as a deployment would name it."""
    settings.EXERCISE_MEDIA_ALLOWED_HOSTS = ("storage.googleapis.com/yalla-gym",)


# ------------------------------------------------------------------------ helpers
def _program(make_user, tag, *, sets=3, reps=10):
    """A trainer, an approved client, and a one-day routine the client is assigned."""
    from routine.models import Exercise, Routine, RoutineExercise
    from users.models import TrainerClientRelation

    trainer = make_user(f"r2_t_{tag}", user_type="trainer")
    client = make_user(f"r2_c_{tag}")
    TrainerClientRelation.objects.create(trainer=trainer, client=client,
                                         status="approved", initiated_by="client")
    routine = Routine.objects.create(name=f"r2 {tag}", created_by=trainer, days=1)
    exercise = Exercise.objects.create(name=f"r2 ex {tag}", created_by=trainer)
    RoutineExercise.objects.create(routine=routine, exercise=exercise, day=1, order=1,
                                   sets=sets, reps=reps)
    routine.assigned_to.add(client)
    return trainer, client, routine, exercise


def _png(name="shot.png"):
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (32, 32), (10, 120, 200)).save(buf, format="PNG")
    buf.seek(0)
    buf.name = name
    return buf


# ------------------------------- S-002: a client cannot author their own compliance
def test_s002_a_clients_claimed_sets_are_ignored_on_create(make_user, api):
    """`completed_sets` and `target_sets` are derived: the set logs and the coach's
    prescription decide them. Writable, two HTTP calls manufactured a finished day."""
    from routine.models import UserExerciseProgress

    trainer, client, routine, exercise = _program(make_user, "s002", sets=3)

    response = api(client).post("/api/routine/user-exercise-progress/", {
        "exercise": exercise.pk, "date": timezone.localdate().isoformat(),
        "completed_sets": 9, "target_sets": 9}, content_type=JSON)

    assert response.status_code == 201, response.content
    row = UserExerciseProgress.objects.get(pk=response.json()["id"])
    assert (row.completed_sets, row.target_sets) == (0, 0), \
        "a claim with no set log behind it was stored"
    assert (response.json()["completed_sets"], response.json()["target_sets"]) == (0, 0)


def test_s002_even_staff_cannot_patch_the_derived_numbers(make_user, api):
    """Staff-only was the reason this was filed as latent, so the lock is tested
    there too: derived means derived, whoever is asking."""
    from routine.models import UserExerciseProgress

    trainer, client, routine, exercise = _program(make_user, "s002p", sets=3)
    admin = make_user("r2_a_s002p", user_type="admin")
    row = UserExerciseProgress.objects.create(user=client, exercise=exercise,
                                              date=timezone.localdate(), target_sets=3)

    response = api(admin).patch(f"/api/routine/user-exercise-progress/{row.pk}/",
                                {"completed_sets": 99, "target_sets": 1},
                                content_type=JSON)

    assert response.status_code == 200, response.content
    row.refresh_from_db()
    assert (row.completed_sets, row.target_sets) == (0, 3)


# --------------------------------------- S-003: one set belongs to exactly one day
def test_s003_a_set_dated_away_from_its_progress_row_is_refused(make_user, api):
    """`outcomes.day_work` counts a set by the LOG's date and
    `refresh_routine_progress` by the PROGRESS row's, so a mismatch put one workout on
    two days at once — and let a short day be completed by aiming a set at it."""
    from datetime import timedelta

    from routine.models import ExerciseSetLog, UserExerciseProgress

    trainer, client, routine, exercise = _program(make_user, "s003", sets=3)
    today = timezone.localdate()
    row = UserExerciseProgress.objects.create(user=client, exercise=exercise,
                                              date=today, target_sets=3)

    response = api(client).post("/api/routine/set-logs/", {
        "user_exercise_progress": row.pk, "set_number": 1, "weight": 40, "reps": 8,
        "date": (today - timedelta(days=3)).isoformat()}, content_type=JSON)

    assert response.status_code == 400, response.content
    assert response.json()["date"][0] == "set_date_mismatch" or \
        "set_date_mismatch" in json.dumps(response.json()), response.json()
    assert not ExerciseSetLog.objects.filter(user_exercise_progress=row).exists()


def test_s003_an_omitted_date_is_today_and_cannot_be_backdated(make_user, api):
    """An omitted date means TODAY, and must match its row like any other.

    Inheriting the row's day instead would silently backdate the set — and the coaching
    refusal is judged on that day, so a frozen client could log today's training by
    aiming an undated set at a progress row from a month they had paid for. That is the
    hole `tests/test_coaching_freeze.py::test_an_undated_set_is_judged_on_the_day_it_is_stored`
    exists to keep shut, and it outranks the convenience: what S-003 is really about —
    that one set never lands on two days — holds either way, because the mismatch is
    refused rather than resolved. Catching up yesterday's workout still works; the app
    sends the date, which every logging route already does."""
    from datetime import timedelta

    from routine.models import ExerciseSetLog, UserExerciseProgress

    trainer, client, routine, exercise = _program(make_user, "s003b", sets=3)
    trained = timezone.localdate() - timedelta(days=2)
    row = UserExerciseProgress.objects.create(user=client, exercise=exercise,
                                              date=trained, target_sets=3)

    undated = api(client).post("/api/routine/set-logs/", {
        "user_exercise_progress": row.pk, "set_number": 1, "weight": 40, "reps": 8},
        content_type=JSON)
    assert undated.status_code == 400, undated.content
    assert not ExerciseSetLog.objects.filter(user_exercise_progress=row).exists()

    # Say which day it was, and it lands on that day — the two dates still agree.
    dated = api(client).post("/api/routine/set-logs/", {
        "user_exercise_progress": row.pk, "set_number": 1, "weight": 40, "reps": 8,
        "date": trained.isoformat()}, content_type=JSON)
    assert dated.status_code == 201, dated.content
    assert ExerciseSetLog.objects.get(pk=dated.json()["id"]).date == trained


def test_s003_the_model_refuses_it_too_whatever_wrote_the_row(make_user):
    """Not only the API: `ExerciseSetLog.clean()` is where the rule lives, so a task,
    a command or the admin cannot leave the platform with two answers either."""
    from datetime import timedelta

    from django.core.exceptions import ValidationError

    from routine.models import Exercise, ExerciseSetLog, UserExerciseProgress

    client = make_user("r2_c_s003c")
    exercise = Exercise.objects.create(name="r2 ex s003c")
    row = UserExerciseProgress.objects.create(
        user=client, exercise=exercise, date=timezone.localdate(), target_sets=1)

    with pytest.raises(ValidationError) as refused:
        ExerciseSetLog.objects.create(
            user_exercise_progress=row, set_number=1, weight=20, reps=5,
            date=timezone.localdate() - timedelta(days=1))

    assert "set_date_mismatch" in str(refused.value.error_dict["date"][0].code)


# ------------------------------------------- S-004: the key the contract promised
def test_s004_client_progress_answers_the_key_its_docstring_promises(make_user, api):
    """The docstring said `client`, the code answered `client_info`, and the app
    session was writing the wrong model from the contract. Additive: both."""
    trainer, client, routine, exercise = _program(make_user, "s004")

    body = api(trainer).get(
        f"/api/routine/trainer/client-progress/{client.pk}/").json()

    assert "client" in body, body.keys()
    assert body["client"] == body["client_info"]
    assert body["client"]["id"] == client.pk


# ----------------------------------------------- S-005: no branch that never runs
def test_s005_the_session_permission_no_longer_pretends_to_decide_creates():
    """`WorkoutSessionViewSet.get_permissions` routes every POST to
    `IsRoutineOwnerOrAssigned`, so this class's POST branch never ran — it refused
    every non-trainer and read like the rule. A client starting their own workout is
    the ordinary case, and the class must not say otherwise."""
    import inspect

    from django.contrib.auth.models import AnonymousUser
    from django.test import RequestFactory

    from routine.permissions import IsSessionOwnerOrTrainerOrAdmin
    from routine.views import WorkoutSessionViewSet

    source = inspect.getsource(IsSessionOwnerOrTrainerOrAdmin.has_permission) \
        + inspect.getsource(IsSessionOwnerOrTrainerOrAdmin.has_object_permission)
    assert "'POST'" not in source and '"POST"' not in source, \
        "the dead POST branch is back"

    request = RequestFactory().post("/api/routine/workout-sessions/")
    request.user = AnonymousUser()
    assert IsSessionOwnerOrTrainerOrAdmin().has_permission(request, None) is False

    # And the class that DOES decide a create is named where it is used.
    assert "IsRoutineOwnerOrAssigned" in inspect.getsource(
        WorkoutSessionViewSet.get_permissions)


def test_s005_a_client_starting_their_own_workout_is_not_refused(make_user, api):
    """The behaviour the dead branch contradicted, pinned."""
    trainer, client, routine, exercise = _program(make_user, "s005")

    response = api(client).post("/api/routine/workout-sessions/",
                                {"routine": routine.pk, "day": 1}, content_type=JSON)

    assert response.status_code == 201, response.content


# ------------------------------------- S-006: no internal exception text on the wire
def test_s006_a_failed_image_upload_says_nothing_about_the_server(make_user, api,
                                                                  monkeypatch):
    """`f'Upload failed: {str(e)}'` put storage paths, bucket names and SQL in the
    response body of whoever posted the file."""
    from routine.models import Exercise

    trainer = make_user("r2_t_s006", user_type="trainer")
    exercise = Exercise.objects.create(name="r2 ex s006", created_by=trainer)

    def explode(*args, **kwargs):
        raise RuntimeError("/srv/secrets/gcs-key.json is unreadable")

    monkeypatch.setattr("routine.views.process_uploaded_image", explode, raising=False)
    monkeypatch.setattr("training_platform.file_security.process_uploaded_image",
                        explode)

    response = api(trainer).post(
        f"/api/routine/exercises/{exercise.pk}/image/", {"image": _png()})

    assert response.status_code == 500, response.content
    body = response.content.decode()
    assert "gcs-key.json" not in body and "RuntimeError" not in body, body
    assert response.json()["code"] == "image_upload_failed"


# ------------------------------------------ S-009: the push table cannot drift again
def _documented_push_types():
    """The `data.type` column of §4's table in API_CONTRACT.md."""
    text = (REPO_ROOT / "API_CONTRACT.md").read_text(encoding="utf-8")
    section = text.split("## 4. Push notifications", 1)[1].split("\n## ", 1)[0]
    return {m.group(1) for m in re.finditer(r"^\|\s*`([a-z0-9_]+)`\s*\|", section, re.M)}


def test_s009_every_registered_push_event_is_in_the_contract():
    """The route table is diffed against the URLconf and this one was not, so it had
    drifted by eleven event types: the app was built from a short list and ignored
    pushes the server sends. This test is the gate that stops it happening again."""
    from notifications.channels.fcm import EVENT_CLASS_REGISTRY

    documented = _documented_push_types()
    registered = set(EVENT_CLASS_REGISTRY)

    missing = sorted(registered - documented)
    phantom = sorted(documented - registered)
    assert not missing, (
        "event types the server sends that API_CONTRACT.md §4 does not list: "
        + ", ".join(missing))
    assert not phantom, (
        "API_CONTRACT.md §4 lists event types that are not registered: "
        + ", ".join(phantom))


# ------------------------------------------------- S-010: the worktree's .venv symlink
def test_s010_gitignore_covers_the_bare_venv_spelling():
    """A worktree gets `.venv` as a SYMLINK, which `.venv/` does not match, so
    `git add -A` there would have committed it."""
    lines = {line.strip() for line in
             (REPO_ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()}
    assert ".venv" in lines and ".venv/" in lines, sorted(l for l in lines if "venv" in l)


# ------------------------------------------ S-016: one shape for one field
def test_s016_the_roster_answers_an_absolute_profile_picture(make_user, api):
    """Every other endpoint returns `profile_picture` absolute (B-032 was this exact
    bug on `exercise.image`); the trainer's roster returned `/media/...`."""
    from users.models import CustomUser

    trainer, client, routine, exercise = _program(make_user, "s016")
    CustomUser.objects.filter(pk=client.pk).update(
        profile_picture="profile_pictures/s016.png")

    body = api(trainer).get("/api/routine/trainer/client-progress/recent/").json()

    pictures = [row["client_info"]["profile_picture"]
                for row in body["recent_activity"]]
    assert pictures and all(p and p.startswith("http") for p in pictures), pictures


# --------------------------------------- S-017: whose host the player is pointed at
def test_s017_a_third_party_media_host_is_refused(make_user, api):
    """Any http(s) URL was accepted, so one coach could aim every one of their
    clients' players at a host nobody here controls."""
    from routine.models import Exercise, ExerciseMedia

    trainer = make_user("r2_t_s017", user_type="trainer")
    exercise = Exercise.objects.create(name="r2 ex s017", created_by=trainer)

    response = api(trainer).post(
        f"/api/routine/exercises/{exercise.pk}/add-media/",
        json.dumps({"media_items": [
            {"media_type": "video", "content": THEIRS, "title": "nope"}]}),
        content_type=JSON)

    assert response.status_code == 207, response.content
    codes = [row["code"] for row in response.json()["errors_detail"]]
    assert codes == ["media_host_not_allowed"], response.json()
    assert not ExerciseMedia.objects.filter(exercise=exercise).exists()


def test_s017_the_platforms_own_media_host_is_still_accepted(make_user, api):
    from routine.models import Exercise, ExerciseMedia

    trainer = make_user("r2_t_s017ok", user_type="trainer")
    exercise = Exercise.objects.create(name="r2 ex s017ok", created_by=trainer)

    response = api(trainer).post(
        f"/api/routine/exercises/{exercise.pk}/add-media/",
        json.dumps({"media_items": [
            {"media_type": "video", "content": OURS, "title": "demo"}]}),
        content_type=JSON)

    assert response.status_code == 201, response.content
    assert ExerciseMedia.objects.filter(exercise=exercise).count() == 1


def test_s017_the_other_route_that_writes_a_video_url_is_held_to_it_too(make_user,
                                                                        api):
    """`exercises/create-with-image/` creates the SAME `ExerciseMedia` video rows
    from the same caller-supplied URLs, behind a regex that only checks the shape.
    Closing one door and not the other closes nothing."""
    from routine.models import ExerciseMedia

    trainer = make_user("r2_t_s017two", user_type="trainer")

    response = api(trainer).post("/api/routine/exercises/create-with-image/", {
        "name": "r2 s017two", "description": "d", "target_muscle": "Biceps",
        "media_videos": THEIRS})

    assert response.status_code == 400, response.content
    assert response.json()["code"] == "media_host_not_allowed", response.json()
    assert not ExerciseMedia.objects.filter(content=THEIRS).exists()


def test_s017_a_text_tip_is_not_a_url_and_is_untouched(make_user, api):
    """The rule is about where a PLAYER is pointed; a form cue is prose."""
    from routine.models import Exercise, ExerciseMedia

    trainer = make_user("r2_t_s017txt", user_type="trainer")
    exercise = Exercise.objects.create(name="r2 ex s017txt", created_by=trainer)

    response = api(trainer).post(
        f"/api/routine/exercises/{exercise.pk}/add-media/",
        json.dumps({"media_items": [
            {"media_type": "text", "content": "Brace, then sit down."}]}),
        content_type=JSON)

    assert response.status_code == 201, response.content
    assert ExerciseMedia.objects.filter(exercise=exercise).count() == 1


# ---------------------------------------- S-020: a Vary header that keeps what it had
def test_s020_the_cache_middleware_keeps_a_vary_already_set():
    """`response['Vary'] = '...'` threw away `Cookie` from the session middleware and
    `Accept-Encoding` from gzip on exactly the four cached routes, so a proxy was free
    to serve one user's variant to the next caller."""
    from django.http import JsonResponse
    from django.test import RequestFactory

    from training_platform.middleware import CacheMiddleware

    request = RequestFactory().get("/api/subscription/v1/plans/")
    response = JsonResponse({"results": []})
    response["Vary"] = "Cookie, Accept-Encoding"

    out = CacheMiddleware(lambda r: response).process_response(request, response)

    varies = {part.strip().lower() for part in out["Vary"].split(",")}
    assert {"cookie", "accept-encoding"} <= varies, out["Vary"]
    assert {"accept-language", "authorization", "x-app-channel",
            "x-app-version"} <= varies, out["Vary"]


# ------------------------------------- S-028: no app is silently outside the audit
def test_s028_the_privacy_audit_walks_mediakit_too():
    """The app list was a literal and `mediakit` was never added, so a model there
    holding a user FK would have been exempt from the "every personal-data model is
    registered" gate — which is the rot that function exists to catch. Proven by
    un-registering the one model it holds: the audit must notice."""
    # By its full path: `training_platform.privacy` re-exports the `registry` DICT
    # under that name, so `from ... import registry` hands over the dict, not the
    # module.
    from training_platform.privacy.registry import audit_coverage, registry

    removed = registry.pop("mediakit.MediaAsset")
    try:
        assert "mediakit.MediaAsset" in audit_coverage()
    finally:
        registry["mediakit.MediaAsset"] = removed

    assert audit_coverage() == [], "the registry itself has a gap"


def test_s028_every_first_party_app_is_inside_the_audit():
    """Derived from what is installed, so adding an app is enough to be covered."""
    from pathlib import Path as _Path

    from django.apps import apps
    from django.conf import settings

    root = str(_Path(settings.BASE_DIR).resolve())
    ours = {config.label for config in apps.get_app_configs()
            if str(_Path(config.path).resolve()).startswith(root + "/")}
    assert {"mediakit", "chat", "moderation", "policy"} <= ours, sorted(ours)


# -------------------------------------- S-031: one exercise shape, from the serializer
def test_s031_reading_an_exercises_media_answers_the_whole_exercise(make_user, api):
    """The route that exists to serve an exercise's media could not show
    `media_slots`, because it built its answer from two columns by hand."""
    from routine.models import Exercise

    trainer = make_user("r2_t_s031", user_type="trainer")
    exercise = Exercise.objects.create(name="r2 ex s031", created_by=trainer)

    body = api(trainer).get(
        f"/api/routine/exercises/{exercise.pk}/add-media/").json()

    assert "exercise" in body, body.keys()
    for field in ("id", "name", "image", "media", "media_slots", "media_source",
                  "media_trainer_id"):
        assert field in body["exercise"], (field, body["exercise"].keys())
    # The legacy keys are still there: the contract is additive.
    assert body["exercise_id"] == exercise.pk and body["exercise_name"] == exercise.name


def test_s031_the_image_upload_answers_the_whole_exercise(make_user, api):
    from routine.models import Exercise

    trainer = make_user("r2_t_s031b", user_type="trainer")
    exercise = Exercise.objects.create(name="r2 ex s031b", created_by=trainer)

    response = api(trainer).post(
        f"/api/routine/exercises/{exercise.pk}/image/", {"image": _png()})

    assert response.status_code == 200, response.content
    body = response.json()["exercise"]
    assert "media_slots" in body and "media_source" in body, body.keys()
    assert body["image"] and body["image"].startswith("http"), body["image"]
