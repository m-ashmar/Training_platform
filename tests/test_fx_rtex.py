"""Routine exercises, exercise images and media, progress rows, the exercise cache.

One test (or one small group) per SIDE_BUGS row closed on branch `fx/rtex`. Each fails
against the code that was there, for the reason named:

    S-056  `user-exercise-progress/` POST took ANY exercise id, and the list then nested
           the whole exercise — another trainer's private library, signed media included
    S-057  `create-with-image/` assigned a nameless ContentFile to `exercise.image`:
           every request with the main image answered 500, and the half-made exercise
           still committed because the broad `except` swallowed the error inside the
           atomic block
    S-070  `create-with-image/` stored the SIGNED url of each extra photo, which expires,
           and nothing ever removed the stored file
    S-109  `Exercise.save()` deleted the old image INLINE, before the row was written
    S-117  `create-with-image/` let a client or an agent in
    S-118  the exercise image routes dropped the upload guard's `code`
    S-125  `exercises/{id}/image/` answered 403 for another trainer's private exercise
           and 404 for a missing one, so ids could be probed
    S-042  bare English on the exercise media routes, and a bulk-complete error with no
           `code`
    S-055  two serializers imported by `routine/views.py` and used nowhere
    S-119  `routine-exercises/?routine=` answered 400 for a routine id that does not
           exist and an empty 200 for somebody else's
    S-112  a progress row (and a set on it) could be dated in the future, and a run of
           future days counted as the current streak
    S-138  `add-media/` writes did not invalidate the cached exercise reads
"""
import io
import json
import re
from datetime import date, timedelta

import pytest
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.core.files.uploadedfile import SimpleUploadedFile
from django.utils import timezone
from PIL import Image

pytestmark = pytest.mark.django_db

JSON = "application/json"
CREATE = "/api/routine/exercises/create-with-image/"
ARABIC = re.compile(r"[؀-ۿ]")
NOT_FOUND_BODY = {"error": "Exercise not found.", "detail": "Exercise not found.",
                  "code": "not_found"}


# ------------------------------------------------------------------------ helpers
def _png_bytes(colour=(10, 120, 200)):
    buf = io.BytesIO()
    Image.new("RGB", (32, 32), colour).save(buf, format="PNG")
    return buf.getvalue()


def _png(name="shot.png", colour=(10, 120, 200)):
    return SimpleUploadedFile(name, _png_bytes(colour), content_type="image/png")


def _not_an_image(name="shot.png"):
    return SimpleUploadedFile(name, b"this is plain text, not a picture at all" * 4,
                              content_type="image/png")


def _guard_code(upload):
    """The code the upload guard itself gives these bytes — what the route must pass on."""
    from django.core.exceptions import ValidationError

    from training_platform.file_security import process_uploaded_image

    try:
        process_uploaded_image(upload, max_bytes=5 * 1024 * 1024)
    except ValidationError as e:
        return getattr(e, "code", None)
    raise AssertionError("the guard accepted a file that is not an image")


def _approve(trainer, client):
    from users.models import TrainerClientRelation

    return TrainerClientRelation.objects.create(trainer=trainer, client=client,
                                                status="approved", initiated_by="client")


def _program(make_user, tag, *, sets=3, reps=10):
    """A trainer, an approved client, and a one-day routine the client has."""
    from routine.models import Exercise, Routine, RoutineExercise

    trainer = make_user(f"fxr_t_{tag}", user_type="trainer")
    client = make_user(f"fxr_c_{tag}")
    _approve(trainer, client)
    routine = Routine.objects.create(name=f"fxr {tag}", created_by=trainer, days=1)
    exercise = Exercise.objects.create(name=f"fxr ex {tag}", created_by=trainer)
    RoutineExercise.objects.create(routine=routine, exercise=exercise, day=1, order=1,
                                   sets=sets, reps=reps)
    routine.assigned_to.add(client)
    return trainer, client, routine, exercise


def _field_codes(body, field):
    return [e.get("code") for e in (body.get("field_errors") or {}).get(field, [])]


# ------------------------------------ S-056: a progress row names a visible exercise
def test_s056_a_progress_row_cannot_name_another_trainers_private_exercise(make_user,
                                                                           api):
    """A client with no relation to trainer A could POST a progress row on A's private
    exercise, then read the whole exercise — name, description, signed media — back out
    of the list. The id must answer exactly as an id that does not exist."""
    from routine.models import Exercise, UserExerciseProgress

    owner = make_user("fxr_t_s056_owner", user_type="trainer")
    trainer, client, routine, mine = _program(make_user, "s056")
    private = Exercise.objects.create(name="fxr A's secret press", created_by=owner)
    missing = Exercise.objects.order_by("-pk").first().pk + 10_000
    today = timezone.localdate().isoformat()
    c = api(client)

    theirs = c.post("/api/routine/user-exercise-progress/",
                    {"exercise": private.pk, "date": today}, content_type=JSON)
    nothing = c.post("/api/routine/user-exercise-progress/",
                     {"exercise": missing, "date": today}, content_type=JSON)

    assert theirs.status_code == 400, theirs.content
    assert (theirs.status_code, theirs.json()["code"], _field_codes(theirs.json(), "exercise")) \
        == (nothing.status_code, nothing.json()["code"], _field_codes(nothing.json(), "exercise"))
    assert _field_codes(theirs.json(), "exercise") == ["does_not_exist"]
    assert not UserExerciseProgress.objects.filter(user=client, exercise=private).exists()
    listed = c.get("/api/routine/user-exercise-progress/").json()["results"]
    assert "secret press" not in json.dumps(listed)

    # What the client can see is still theirs to log.
    ok = c.post("/api/routine/user-exercise-progress/",
                {"exercise": mine.pk, "date": today}, content_type=JSON)
    assert ok.status_code == 201, ok.content


def test_s056_an_exercise_already_trained_can_be_logged_again(make_user, api):
    """The history is the exception: a person who has lifted it already knows its name,
    and an id cannot be guessed into one because the history has to exist."""
    from routine.models import Exercise, UserExerciseProgress

    former = make_user("fxr_t_s056_former", user_type="trainer")
    client = make_user("fxr_c_s056_former")
    exercise = Exercise.objects.create(name="fxr former coach move", created_by=former)
    UserExerciseProgress.objects.create(user=client, exercise=exercise,
                                        date=timezone.localdate() - timedelta(days=3))

    again = api(client).post("/api/routine/user-exercise-progress/",
                             {"exercise": exercise.pk,
                              "date": timezone.localdate().isoformat()},
                             content_type=JSON)
    assert again.status_code == 201, again.content


# ------------------------------------------- S-057: create-with-image with the image
def test_s057_the_main_image_is_stored_and_the_exercise_answered(make_user, api):
    """The re-encoded image is a ContentFile with no name; assigning it made Django
    refuse the save, so every request that sent `image` — what this route is for —
    answered 500."""
    from routine.models import Exercise

    trainer = make_user("fxr_t_s057", user_type="trainer")
    resp = api(trainer).post(CREATE, {
        "name": "fxr s057", "description": "d", "target_muscle": "Biceps",
        "image": _png("front.png")})

    assert resp.status_code == 201, resp.content
    rows = list(Exercise.objects.filter(created_by=trainer))
    assert len(rows) == 1, rows
    assert rows[0].image and rows[0].image.name.endswith(".png"), rows[0].image
    assert default_storage.exists(rows[0].image.name)
    assert resp.json()["exercise"]["image"], resp.json()


def test_s057_a_failure_after_the_row_is_written_leaves_nothing_behind(make_user, api,
                                                                       monkeypatch):
    """The broad `except` answered 500 from INSIDE the atomic block, so the block
    committed: every failed attempt left an exercise behind, and every retry another."""
    from routine.models import Exercise, ExerciseMedia

    trainer = make_user("fxr_t_s057rb", user_type="trainer")

    def boom(*args, **kwargs):
        raise RuntimeError("storage is down")

    monkeypatch.setattr(ExerciseMedia.objects, "create", boom)
    resp = api(trainer).post(CREATE, {
        "name": "fxr s057rb", "description": "d", "target_muscle": "Biceps",
        "media_texts": "Brace, then sit down."})

    assert resp.status_code == 500, resp.content
    assert resp.json()["code"] == "exercise_create_failed"
    assert not Exercise.objects.filter(created_by=trainer).exists()


# --------------------------------------------- S-070: a photo is stored by its name
def test_s070_an_uploaded_photo_is_stored_by_name_and_signed_when_read(make_user, api):
    """The SIGNED url was stored, so the photo was a dead link once the signature ran
    out (an hour on GCS). The name is stored; the url is made when it is served."""
    from routine.models import ExerciseMedia

    trainer = make_user("fxr_t_s070", user_type="trainer")
    resp = api(trainer).post(CREATE, {
        "name": "fxr s070", "description": "d", "target_muscle": "Biceps",
        "media_photos": [_png("side.png")]})
    assert resp.status_code == 201, resp.content

    media = ExerciseMedia.objects.get(exercise__created_by=trainer, media_type="photo")
    assert not media.content.startswith(("http://", "https://", "/")), media.content
    assert "?" not in media.content, media.content
    assert default_storage.exists(media.content)

    served = resp.json()["exercise"]["media"][0]["content"]
    assert served.startswith("http") and media.content in served, served

    c = api(trainer)
    detail = c.get(f"/api/routine/exercises/{media.exercise_id}/").json()
    assert detail["media"][0]["content"].startswith("http"), detail["media"]
    assert media.content in detail["media"][0]["content"]
    assert detail["thumbnail"] == detail["media"][0]["content"]
    listed = c.get(f"/api/routine/exercises/{media.exercise_id}/add-media/").json()
    assert media.content in listed["media_items"][0]["content"], listed
    assert listed["media_items"][0]["content"].startswith("http")


def test_s070_removing_the_photo_or_the_exercise_removes_the_file(
        make_user, api, django_capture_on_commit_callbacks):
    """Deleting the media row or the exercise never removed the stored file."""
    from routine.models import ExerciseMedia

    trainer = make_user("fxr_t_s070rm", user_type="trainer")
    c = api(trainer)
    made = c.post(CREATE, {"name": "fxr s070rm", "description": "d",
                           "target_muscle": "Biceps",
                           "media_photos": [_png("a.png"), _png("b.png", (1, 2, 3))]})
    assert made.status_code == 201, made.content
    exercise_id = made.json()["exercise"]["id"]
    folder = f"exercise_media/{exercise_id}"
    assert len(default_storage.listdir(folder)[1]) == 2

    first = ExerciseMedia.objects.filter(exercise_id=exercise_id).order_by("order").first()
    with django_capture_on_commit_callbacks(execute=True):
        gone = c.delete(f"/api/routine/exercises/{exercise_id}/add-media/",
                        json.dumps({"media_ids": [first.pk]}), content_type=JSON)
    assert gone.status_code == 200, gone.content
    assert len(default_storage.listdir(folder)[1]) == 1

    with django_capture_on_commit_callbacks(execute=True):
        dropped = c.delete(f"/api/routine/exercises/{exercise_id}/")
    assert dropped.status_code == 204, dropped.content
    assert default_storage.listdir(folder)[1] == []


# --------------------------------------------- S-109: the old image survives a failure
def test_s109_a_failed_save_does_not_destroy_the_old_image(
        make_user, django_capture_on_commit_callbacks):
    """The old file was deleted BEFORE `validate_row()` and the UPDATE, so a save that
    then failed left the row pointing at a file that no longer existed."""
    from django.core.exceptions import ValidationError

    from routine.models import Exercise

    trainer = make_user("fxr_t_s109", user_type="trainer")
    exercise = Exercise.objects.create(name="fxr s109", created_by=trainer)
    exercise.image.save("old.png", ContentFile(_png_bytes()), save=True)
    old_name = exercise.image.name
    assert default_storage.exists(old_name)

    exercise.image.save("new.png", ContentFile(_png_bytes((9, 9, 9))), save=False)
    exercise.difficulty_level = "not-a-level"
    with pytest.raises(ValidationError):
        exercise.save()
    assert default_storage.exists(old_name), "the old image went with a save that failed"

    # A save that succeeds still removes the replaced file — after the commit.
    exercise = Exercise.objects.get(pk=exercise.pk)
    with django_capture_on_commit_callbacks(execute=True):
        exercise.image.save("newer.png", ContentFile(_png_bytes((5, 5, 5))), save=True)
    assert not default_storage.exists(old_name)
    assert default_storage.exists(exercise.image.name)


# --------------------------------------------------- S-117: trainers and admins only
@pytest.mark.parametrize("role", ["client", "agent"])
def test_s117_only_a_trainer_or_an_admin_may_create_an_exercise_here(make_user, api,
                                                                     role):
    """`POST exercises/` refuses a client; this sibling route let one in."""
    from routine.models import Exercise

    user = make_user(f"fxr_{role}_s117", user_type=role)
    resp = api(user).post(CREATE, {"name": "fxr s117", "description": "d",
                                   "target_muscle": "Biceps",
                                   "media_photos": [_png("p.png")]})

    assert resp.status_code == 403, resp.content
    assert resp.json()["code"] == "permission_denied"
    assert not Exercise.objects.filter(created_by=user).exists()


# ------------------------------------------------ S-118: the upload guard's own code
def test_s118_the_image_routes_pass_the_upload_guards_code_on(make_user, api):
    """The app branches on `code`. These routes answered `bad_request` for every
    refusal, so it could not tell a file that is too large from one that is not an
    image."""
    from routine.models import Exercise

    expected = _guard_code(_not_an_image())
    assert expected not in (None, "bad_request")

    trainer = make_user("fxr_t_s118", user_type="trainer")
    exercise = Exercise.objects.create(name="fxr s118", created_by=trainer)
    c = api(trainer)

    upload = c.post(f"/api/routine/exercises/{exercise.pk}/image/",
                    {"image": _not_an_image()})
    main = c.post(CREATE, {"name": "fxr s118 main", "description": "d",
                           "target_muscle": "Biceps", "image": _not_an_image()})
    extra = c.post(CREATE, {"name": "fxr s118 extra", "description": "d",
                            "target_muscle": "Biceps",
                            "media_photos": [_png("ok.png"), _not_an_image()]})

    for name, resp in (("image", upload), ("create main", main), ("create extra", extra)):
        assert resp.status_code == 400, (name, resp.content)
        assert resp.json()["code"] == expected, (name, resp.json())


# ------------------------------------ S-125: the image route obeys the visibility rule
@pytest.mark.parametrize("method", ["post", "delete"])
def test_s125_another_trainers_exercise_answers_like_a_missing_one(make_user, api,
                                                                   method):
    """403 for a private exercise and 404 for a missing id told a caller which ids
    were real — the probe the four sibling media routes already refuse."""
    from routine.models import Exercise

    owner = make_user(f"fxr_t_s125_owner_{method}", user_type="trainer")
    stranger = make_user(f"fxr_t_s125_stranger_{method}", user_type="trainer")
    private = Exercise.objects.create(name="fxr s125", created_by=owner)
    missing = private.pk + 10_000
    c = api(stranger)

    def call(exercise_id):
        path = f"/api/routine/exercises/{exercise_id}/image/"
        return c.post(path, {"image": _png()}) if method == "post" else c.delete(path)

    theirs, nothing = call(private.pk), call(missing)
    assert (theirs.status_code, theirs.json()) == (404, NOT_FOUND_BODY), theirs.content
    assert (nothing.status_code, nothing.json()) == (404, NOT_FOUND_BODY), nothing.content

    # The owner is unaffected.
    assert api(owner).post(f"/api/routine/exercises/{private.pk}/image/",
                           {"image": _png()}).status_code == 200


# --------------------------------------------------- S-042: Arabic, and a code to use
def test_s042_the_exercise_media_answers_are_arabic_for_an_arabic_client(make_user, api):
    from routine.models import Exercise, ExerciseMedia

    trainer = make_user("fxr_t_s042", user_type="trainer")
    own = Exercise.objects.create(name="fxr s042", created_by=trainer)
    catalogue = Exercise.objects.create(name="fxr s042 catalogue")
    c = api(trainer)
    ar = {"HTTP_ACCEPT_LANGUAGE": "ar"}
    item = {"media_items": [{"media_type": "text", "content": "Brace."}]}

    added = c.post(f"/api/routine/exercises/{own.pk}/add-media/", json.dumps(item),
                   content_type=JSON, **ar)
    assert added.status_code == 201, added.content
    assert ARABIC.search(added.json()["message"]), added.json()

    media_id = ExerciseMedia.objects.get(exercise=own).pk
    removed = c.delete(f"/api/routine/exercises/{own.pk}/add-media/",
                       json.dumps({"media_ids": [media_id]}), content_type=JSON, **ar)
    assert removed.status_code == 200, removed.content
    assert ARABIC.search(removed.json()["message"]), removed.json()

    for resp in (
        c.post(f"/api/routine/exercises/{catalogue.pk}/add-media/", json.dumps(item),
               content_type=JSON, **ar),
        c.delete(f"/api/routine/exercises/{catalogue.pk}/add-media/",
                 json.dumps({"media_ids": [1]}), content_type=JSON, **ar),
        c.post(f"/api/routine/exercises/{catalogue.pk}/image/", {"image": _png()}, **ar),
        c.delete(f"/api/routine/exercises/{catalogue.pk}/image/", **ar),
    ):
        assert resp.status_code == 403, resp.content
        assert resp.json()["code"] == "permission_denied"
        assert ARABIC.search(resp.json()["error"]), resp.json()


def test_s042_a_bulk_complete_row_error_carries_a_code(make_user, api, monkeypatch):
    """The row-level errors of `bulk-complete` had no `code` and the duplicate one was
    English whatever the client spoke."""
    from django.db import IntegrityError

    from routine.models import UserExerciseProgress

    trainer, client, routine, exercise = _program(make_user, "s042bc")

    def duplicate(*args, **kwargs):
        raise IntegrityError('duplicate key value violates unique constraint "x"')

    monkeypatch.setattr(UserExerciseProgress.objects, "update_or_create", duplicate)
    resp = api(client).post("/api/routine/user-exercise-progress/bulk-complete/",
                            {"routine_id": routine.pk, "day": 1,
                             "date": timezone.localdate().isoformat()},
                            content_type=JSON, HTTP_ACCEPT_LANGUAGE="ar")

    assert resp.status_code == 200, resp.content
    errors = resp.json()["errors"]
    assert [e.get("code") for e in errors] == ["progress_exists"], errors
    assert ARABIC.search(errors[0]["error"]), errors


# --------------------------------------------------------------- S-055: dead imports
def test_s055_the_unused_serializers_are_not_imported_by_the_views():
    from routine import views as views_module

    assert not hasattr(views_module, "WorkoutSessionDetailSerializer")
    assert not hasattr(views_module, "ClientProfileViewSerializer")


# ------------------------------------------ S-119: the routine filter is not an oracle
def test_s119_a_missing_routine_and_someone_elses_answer_alike(make_user, api):
    """The auto filter validated the id against EVERY routine: a missing one answered
    400 `Select a valid choice`, someone else's an empty 200."""
    from routine.models import Routine

    trainer, client, routine, exercise = _program(make_user, "s119")
    stranger = make_user("fxr_t_s119_stranger", user_type="trainer")
    theirs = Routine.objects.create(name="fxr s119 theirs", created_by=stranger, days=1)
    missing = theirs.pk + 10_000
    c = api(client)

    a = c.get(f"/api/routine/routine-exercises/?routine={theirs.pk}")
    b = c.get(f"/api/routine/routine-exercises/?routine={missing}")
    assert (a.status_code, a.json()["count"]) == (200, 0), a.content
    assert (b.status_code, b.json()["count"]) == (200, 0), b.content

    mine = c.get(f"/api/routine/routine-exercises/?routine={routine.pk}&day=1")
    assert (mine.status_code, mine.json()["count"]) == (200, 1), mine.content
    assert c.get(f"/api/routine/routine-exercises/?routine={routine.pk}&day=2"
                 ).json()["count"] == 0


# ------------------------------------------------- S-112: training is not in the future
def test_s112_a_progress_row_is_dated_inside_the_training_window(make_user, api):
    from routine.models import UserExerciseProgress

    trainer, client, routine, exercise = _program(make_user, "s112")
    today = timezone.localdate()
    c = api(client)

    ahead = c.post("/api/routine/user-exercise-progress/",
                   {"exercise": exercise.pk,
                    "date": (today + timedelta(days=1)).isoformat()}, content_type=JSON)
    stale = c.post("/api/routine/user-exercise-progress/",
                   {"exercise": exercise.pk,
                    "date": (today - timedelta(days=400)).isoformat()}, content_type=JSON)

    assert ahead.status_code == 400, ahead.content
    assert _field_codes(ahead.json(), "date") == ["date_in_future"], ahead.json()
    assert stale.status_code == 400, stale.content
    assert _field_codes(stale.json(), "date") == ["date_too_old"], stale.json()
    assert not UserExerciseProgress.objects.filter(user=client).exists()

    yesterday = c.post("/api/routine/user-exercise-progress/",
                       {"exercise": exercise.pk,
                        "date": (today - timedelta(days=1)).isoformat()},
                       content_type=JSON)
    assert yesterday.status_code == 201, yesterday.content


def test_s112_a_set_is_not_logged_on_a_future_day(make_user, api):
    """A progress row dated ahead (written before this rule, or by a path that did not
    have it) must not become a way to log tomorrow's training today."""
    from routine.models import ExerciseSetLog, UserExerciseProgress

    trainer, client, routine, exercise = _program(make_user, "s112set")
    tomorrow = timezone.localdate() + timedelta(days=1)
    row = UserExerciseProgress.objects.create(user=client, exercise=exercise,
                                              date=tomorrow)

    resp = api(client).post("/api/routine/set-logs/", {
        "user_exercise_progress": row.pk, "set_number": 1, "weight": 50, "reps": 8,
        "date": tomorrow.isoformat()}, content_type=JSON)

    assert resp.status_code == 400, resp.content
    assert _field_codes(resp.json(), "date") == ["date_in_future"], resp.json()
    assert not ExerciseSetLog.objects.filter(user_exercise_progress=row).exists()


def test_s112_a_future_day_does_not_make_a_streak_current():
    from routine.outcomes import streak_from

    today = date(2026, 9, 23)
    day = timedelta(days=1)
    assert streak_from({today + day}, today=today) == (0, 0)
    assert streak_from({today - day, today, today + day, today + 2 * day},
                       today=today) == (2, 2)
    assert streak_from({today - day, today}, today=today) == (2, 2)


# --------------------------------------- S-138: a media write refreshes cached reads
def test_s138_add_media_writes_invalidate_the_cached_exercise_reads(make_user, api):
    """`/api/routine/exercises/` is cached per user for five minutes and versioned on
    EXERCISE, and `ExerciseMedia` did not bump it — so a coach added a tip and did not
    see it on their own screen."""
    from routine.models import Exercise
    from training_platform.cache import private_cache

    trainer = make_user("fxr_t_s138", user_type="trainer")
    exercise = Exercise.objects.create(name="fxr s138", created_by=trainer)
    private_cache().clear()
    c = api(trainer)
    media_path = f"/api/routine/exercises/{exercise.pk}/add-media/"
    detail_path = f"/api/routine/exercises/{exercise.pk}/"

    assert c.get(media_path).json()["media_count"] == 0
    assert c.get(detail_path).json()["media"] == []

    added = c.post(media_path, json.dumps(
        {"media_items": [{"media_type": "text", "content": "Brace."}]}),
        content_type=JSON)
    assert added.status_code == 201, added.content

    assert c.get(media_path).json()["media_count"] == 1
    assert len(c.get(detail_path).json()["media"]) == 1

    media_id = added.json()["created_media"][0]["id"]
    assert c.delete(media_path, json.dumps({"media_ids": [media_id]}),
                    content_type=JSON).status_code == 200
    assert c.get(media_path).json()["media_count"] == 0
    assert c.get(detail_path).json()["media"] == []
