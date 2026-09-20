"""What a set is allowed to become after it is written, and what the effort says next.

Four things, and two holes that were live in the tree:

  * a progress row cannot be carried onto another routine, day or date after it is
    written — the authorisation that allowed it was decided from exactly that key;
  * the exercise write path stores an upload through the same content check every other
    upload path uses, so a payload with an image header cannot land as a `.php`;
  * `WorkoutSession.effort_rating` (1-5, from the person who trained) and the
    `progression` code built from it with adherence and the strength trend;
  * the owner of a set may correct and delete it, and a delete recomputes everything the
    set was counted in — including a closed day's outcome, which can break a streak;
  * `POST set-logs/sync/`: a batch from a phone that was offline, idempotent per
    `client_uuid`, with partial success.

See routine/progression.py, routine/models.py `update_routine_progress_on_set_delete`,
routine/views.py `_sync_one_set`, and BACKEND_DELTA items 11, 14 and 15.
"""
import io
import json
import uuid
from datetime import datetime, time, timedelta

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.utils import timezone
from PIL import Image

pytestmark = pytest.mark.django_db

JSON = "application/json"


# --------------------------------------------------------------------------- helpers
@pytest.fixture
def owners_coaching_rules(settings):
    """The owner's numbers, as `tests/test_coaching_freeze.py` sets them. Requested by
    name, not autouse: the rest of this file has nothing to do with money."""
    from tests.test_coaching_months import OWNER_RATE

    settings.COACHING_COMMISSION_RATE = OWNER_RATE
    settings.COACHING_PERIOD_DAYS = 30
    settings.COACHING_RENEWAL_REMINDER_DAYS = 3
    settings.COACHING_FROZEN_REMINDER_DAYS = (3, 7, 14, 30)


def _codes(node):
    """Every `code` in an error body, whatever its nesting."""
    if isinstance(node, dict):
        found = [node["code"]] if "code" in node else []
        for value in node.values():
            found += _codes(value)
        return found
    if isinstance(node, list):
        return [code for item in node for code in _codes(item)]
    return []


def _program(make_user, tag, *, sets=3, reps=10, exercises=1, days=1):
    """A trainer, a client, a routine of `exercises` exercises on day 1, assigned."""
    from routine.models import Exercise, Routine, RoutineExercise

    trainer = make_user(f"rx_t_{tag}", user_type="trainer")
    client = make_user(f"rx_c_{tag}")
    routine = Routine.objects.create(name=f"rx {tag}", created_by=trainer, days=days)
    rows = []
    for index in range(exercises):
        exercise = Exercise.objects.create(name=f"rx ex {tag} {index}", created_by=trainer)
        rows.append(RoutineExercise.objects.create(routine=routine, exercise=exercise,
                                                   day=1, order=index + 1, sets=sets,
                                                   reps=reps))
    routine.assigned_to.add(client)
    return trainer, client, routine, rows


def _train(user, exercise, days, *, routine=None, sets=3, reps=8, today=None):
    """One training day per `(days_ago, weight, effort_or_None)`.

    With a `routine` each day also gets a completed `WorkoutSession` carrying the
    effort rating, because that is where `routine/progression.py` reads it from.
    """
    from routine.models import ExerciseSetLog, UserExerciseProgress, WorkoutSession

    today = today or timezone.localdate()
    for ago, weight, effort in days:
        date = today - timedelta(days=ago)
        progress = UserExerciseProgress.objects.create(user=user, exercise=exercise,
                                                       date=date, target_sets=sets)
        session = None
        if routine is not None:
            began = timezone.make_aware(datetime.combine(date, time(9, 0)))
            session = WorkoutSession.objects.create(
                user=user, routine=routine, day=1, status="completed",
                start_time=began, end_time=began + timedelta(hours=1),
                effort_rating=effort)
        for number in range(1, sets + 1):
            ExerciseSetLog.objects.create(user_exercise_progress=progress,
                                          workout_session=session, set_number=number,
                                          weight=weight, reps=reps, date=date)


def _photo(name="shot.jpg", *, fmt="JPEG", size=(64, 64), suffix=b""):
    buf = io.BytesIO()
    Image.new("RGB", size, (200, 60, 30)).save(buf, format=fmt)
    kind = "image/jpeg" if fmt == "JPEG" else "image/png"
    return SimpleUploadedFile(name, buf.getvalue() + suffix, content_type=kind)


def _progression(api, user, exercise_id):
    body = api(user).get(f"/api/routine/analytics/strength/?exercise_id={exercise_id}")
    assert body.status_code == 200, body.content
    return body.json()["progression"]


# ======================================================== A1. a row cannot be moved
def test_a_progress_row_cannot_be_moved_to_another_exercise_or_date(make_user, api):
    """`exercise` and `date` were writable after create, so a PATCH carried today's
    work onto an older date — rewriting the day outcomes, the streak and the
    achievements built on it.

    Asked as the admin, because that is who reaches this PATCH today: the viewset runs
    under `IsAdminOrOwnerOrReadOnly`, which refuses a client's write outright and a
    trainer's for want of an owner to compare against. The rule is in the SERIALIZER
    all the same — the row is its key, whoever is holding the pen.
    """
    from routine.models import UserExerciseProgress

    trainer, client, routine, rows = _program(make_user, "move", exercises=2)
    admin = make_user("rx_move_admin", user_type="admin")
    today = timezone.localdate()
    progress = UserExerciseProgress.objects.create(
        user=client, exercise=rows[0].exercise, date=today, target_sets=3)

    assert api(client).patch(f"/api/routine/user-exercise-progress/{progress.pk}/",
                             json.dumps({"date": "2020-01-01"}),
                             content_type=JSON).status_code == 403

    moved = api(admin).patch(f"/api/routine/user-exercise-progress/{progress.pk}/",
                             json.dumps({"date": (today - timedelta(days=5)).isoformat()}),
                             content_type=JSON)
    assert moved.status_code == 400, moved.content
    assert "progress_immutable" in _codes(moved.json()), moved.json()

    swapped = api(admin).patch(f"/api/routine/user-exercise-progress/{progress.pk}/",
                               json.dumps({"exercise": rows[1].exercise.pk}),
                               content_type=JSON)
    assert swapped.status_code == 400, swapped.content

    progress.refresh_from_db()
    assert (progress.date, progress.exercise_id) == (today, rows[0].exercise_id)


def test_re_sending_the_key_a_progress_row_already_holds_is_a_no_op(make_user, api):
    """A PUT of the whole object repeats `exercise` and `date`; that is not a move."""
    from routine.models import UserExerciseProgress

    trainer, client, routine, rows = _program(make_user, "noop")
    admin = make_user("rx_noop_admin", user_type="admin")
    today = timezone.localdate()
    progress = UserExerciseProgress.objects.create(
        user=client, exercise=rows[0].exercise, date=today, target_sets=3)

    same = api(admin).patch(f"/api/routine/user-exercise-progress/{progress.pk}/",
                            json.dumps({"exercise": rows[0].exercise.pk,
                                        "date": today.isoformat(), "skipped": True}),
                            content_type=JSON)
    assert same.status_code == 200, same.content
    progress.refresh_from_db()
    assert progress.skipped is True and progress.date == today


def test_a_client_cannot_move_a_days_progress_to_another_routine(make_user, api):
    """The same hole on `RoutineProgress`: `perform_update` re-checked
    `instance.routine`, never the one the PATCH was carrying the row to."""
    from routine.models import RoutineProgress

    trainer, client, routine, rows = _program(make_user, "rpmove")
    other = _program(make_user, "rpother")[2]
    row = RoutineProgress.objects.filter(user=client, routine=routine).first()
    assert row is not None

    for payload in ({"routine": other.pk}, {"day": 9},
                    {"date": (timezone.localdate() - timedelta(days=3)).isoformat()}):
        refused = api(client).patch(f"/api/routine/routine-progress/{row.pk}/",
                                    json.dumps(payload), content_type=JSON)
        assert refused.status_code == 400, (payload, refused.content)
        assert "progress_immutable" in _codes(refused.json()), refused.json()

    row.refresh_from_db()
    assert row.routine_id == routine.pk and row.day == 1


# ================================================ A2. the exercise image write path
def test_an_image_that_is_not_an_image_is_refused_on_the_exercise_write_path(make_user, api):
    """`POST`/`PATCH /api/routine/exercises/` stored the upload with no content check:
    DRF's ImageField only asks Pillow whether the bytes open, so a real JPEG with a PHP
    payload after it, named `.php`, was decoded happily and written to media under the
    CLIENT's filename."""
    from routine.models import Exercise

    trainer = make_user("rx_img_t", user_type="trainer")

    shell = api(trainer).post("/api/routine/exercises/", {
        "name": "rx shell", "description": "d", "target_muscle": "Biceps",
        "image": _photo("shell.php", suffix=b"<?php system($_GET['c']); ?>")})
    assert shell.status_code == 400, shell.content
    assert not Exercise.objects.filter(name="rx shell").exists()

    junk = api(trainer).post("/api/routine/exercises/", {
        "name": "rx junk", "description": "d", "target_muscle": "Biceps",
        "image": SimpleUploadedFile("x.jpg", b"<?php echo 1; ?>",
                                    content_type="image/jpeg")})
    assert junk.status_code == 400, junk.content
    assert not Exercise.objects.filter(name="rx junk").exists()


def test_a_photo_written_through_the_exercise_viewset_is_re_encoded(make_user, api):
    """The part only `process_uploaded_image` does, and the reason a polyglot matters:
    a VALID jpeg with a payload after it passes every extension and Pillow check, and
    the bytes were stored exactly as they arrived — payload, EXIF, GPS and all. What
    is stored now is the image decoded and written again."""
    from django.core.files.storage import default_storage

    from routine.models import Exercise

    trainer = make_user("rx_img_ok", user_type="trainer")
    created = api(trainer).post("/api/routine/exercises/", {
        "name": "rx photo", "description": "d", "target_muscle": "Biceps",
        "image": _photo("holiday.jpg", suffix=b"<script>alert(1)</script>")})
    assert created.status_code == 201, created.content

    exercise = Exercise.objects.get(name="rx photo")
    assert exercise.image.name.endswith(".jpg")
    with default_storage.open(exercise.image.name) as stored:
        assert b"<script" not in stored.read()


def test_the_same_content_check_runs_on_an_update(make_user, api):
    from django.core.files.storage import default_storage

    from routine.models import Exercise

    from django.test.client import BOUNDARY, MULTIPART_CONTENT, encode_multipart

    trainer = make_user("rx_img_patch", user_type="trainer")
    exercise = Exercise.objects.create(name="rx patched", created_by=trainer)
    url = f"/api/routine/exercises/{exercise.pk}/"

    def _patch(upload):
        return api(trainer).patch(url, encode_multipart(BOUNDARY, {"image": upload}),
                                  content_type=MULTIPART_CONTENT)

    refused = _patch(SimpleUploadedFile("x.jpg", b"<?php echo 1; ?>",
                                        content_type="image/jpeg"))
    assert refused.status_code == 400, refused.content
    exercise.refresh_from_db()
    assert not exercise.image

    accepted = _patch(_photo("holiday.jpg", suffix=b"<script>alert(1)</script>"))
    assert accepted.status_code == 200, accepted.content
    exercise.refresh_from_db()
    with default_storage.open(exercise.image.name) as stored:
        assert b"<script" not in stored.read()


# ==================================================== B. effort rating + progression
def test_only_the_person_who_trained_says_how_hard_it_felt(make_user, api):
    from routine.models import WorkoutSession
    from users.models import TrainerClientRelation

    trainer, client, routine, rows = _program(make_user, "effort")
    TrainerClientRelation.objects.create(trainer=trainer, client=client, status="approved")
    session = WorkoutSession.objects.create(user=client, routine=routine, day=1)

    mine = api(client).patch(f"/api/routine/workout-sessions/{session.pk}/",
                             json.dumps({"effort_rating": 4}), content_type=JSON)
    assert mine.status_code == 200, mine.content
    assert mine.json()["effort_rating"] == 4

    theirs = api(trainer).patch(f"/api/routine/workout-sessions/{session.pk}/",
                                json.dumps({"effort_rating": 1}), content_type=JSON)
    assert theirs.status_code == 400, theirs.content
    assert "effort_not_yours" in _codes(theirs.json()), theirs.json()

    out_of_range = api(client).patch(f"/api/routine/workout-sessions/{session.pk}/",
                                     json.dumps({"effort_rating": 9}), content_type=JSON)
    assert out_of_range.status_code == 400, out_of_range.content
    assert "effort_out_of_range" in _codes(out_of_range.json()), out_of_range.json()

    session.refresh_from_db()
    assert session.effort_rating == 4


def test_the_database_refuses_an_effort_outside_one_to_five(make_user, api):
    """The serializer can be bypassed (a management command, the admin, a shell); the
    check constraint cannot."""
    from django.db import IntegrityError, transaction

    from routine.models import WorkoutSession

    trainer, client, routine, rows = _program(make_user, "effortdb")
    with pytest.raises(IntegrityError):
        with transaction.atomic():
            WorkoutSession.objects.create(user=client, routine=routine, day=1,
                                          effort_rating=0)


def test_an_easy_session_suggests_more_weight_and_says_what_it_read(make_user, api):
    trainer, client, routine, rows = _program(make_user, "prog_add")
    exercise = rows[0].exercise
    _train(client, exercise, [(14, 60, 2), (7, 60, 2), (0, 60, 2)], routine=routine)

    answer = _progression(api, client, exercise.pk)
    assert answer["code"] == "add_weight", answer
    assert answer["effort"] == {"typical": 2.0, "rated_sessions": 3}
    assert answer["sessions"] == 3
    assert answer["latest"] == {"weight": 60.0, "reps": 8}
    assert answer["next"] == {"weight_kg": 62.5}
    assert answer["needs"] is None


def test_a_moderate_session_suggests_one_more_rep(make_user, api):
    trainer, client, routine, rows = _program(make_user, "prog_reps")
    exercise = rows[0].exercise
    _train(client, exercise, [(14, 50, 3), (7, 50, 3), (0, 50, 3)], routine=routine)

    answer = _progression(api, client, exercise.pk)
    assert answer["code"] == "add_reps", answer
    assert answer["next"] == {"reps": 9}


def test_a_hard_session_holds_and_a_maximal_one_deloads(make_user, api):
    trainer, client, routine, rows = _program(make_user, "prog_hard")
    hard = rows[0].exercise
    _train(client, hard, [(14, 80, 4), (7, 80, 4), (0, 80, 4)], routine=routine)
    assert _progression(api, client, hard.pk)["code"] == "hold"

    trainer2, client2, routine2, rows2 = _program(make_user, "prog_deload")
    maximal = rows2[0].exercise
    _train(client2, maximal, [(14, 100, 5), (7, 100, 5), (0, 100, 5)], routine=routine2)
    answer = _progression(api, client2, maximal.pk)
    assert answer["code"] == "deload", answer
    # A tenth off, rounded DOWN to a loadable step: a deload that rounds up is not one.
    assert answer["next"] == {"weight_kg": 90.0}


def test_an_easy_session_still_holds_while_the_plan_is_not_being_finished(make_user, api):
    """The lever the person controls is the plan, not the bar: under 80% of the
    planned sets done, nothing is added however light it felt."""
    from routine.models import DayOutcome

    trainer, client, routine, rows = _program(make_user, "prog_adh")
    exercise = rows[0].exercise
    _train(client, exercise, [(14, 40, 2), (7, 40, 2), (0, 40, 2)], routine=routine)
    today = timezone.localdate()
    DayOutcome.objects.create(user=client, routine=routine, day=1, date=today - timedelta(days=1),
                              via="confirmed_unlogged", planned_sets=10, logged_sets=3,
                              missed_sets=7, complete=False)

    answer = _progression(api, client, exercise.pk)
    assert answer["code"] == "hold", answer
    assert answer["adherence_percent"] == 30.0


def test_without_a_rating_or_enough_days_the_answer_is_not_enough_data(make_user, api):
    trainer, client, routine, rows = _program(make_user, "prog_none")
    exercise = rows[0].exercise
    # Three training days, none of them rated.
    _train(client, exercise, [(14, 60, None), (7, 60, None), (0, 60, None)], routine=routine)

    answer = _progression(api, client, exercise.pk)
    assert answer["code"] == "not_enough_data", answer
    assert answer["needs"] == {"sessions": 0, "rated_sessions": 1}
    assert answer["next"] is None


def test_the_coach_reads_the_same_progression_on_the_client_screen(make_user, api):
    from users.models import TrainerClientRelation

    trainer, client, routine, rows = _program(make_user, "prog_coach")
    TrainerClientRelation.objects.create(trainer=trainer, client=client, status="approved")
    exercise = rows[0].exercise
    _train(client, exercise, [(14, 60, 2), (7, 60, 2), (0, 60, 2)], routine=routine)

    body = api(trainer).get(f"/api/routine/trainer/client-progress/{client.pk}/")
    assert body.status_code == 200, body.content
    strength = body.json()["strength"]
    codes = [row["code"] for row in strength["progression"]]
    assert codes == ["add_weight"], strength["progression"]
    assert strength["top_movers"] == [] or \
        strength["top_movers"][0]["progression"]["code"] == "add_weight"
    # And the coach sees how hard the client said it was.
    assert body.json()["recent_activity"][0]["effort_rating"] == 2


# ============================================== C. editing and deleting a saved set
def _log_one_set(api, client, progress, commit, *, weight=50, reps=8, date=None):
    body = {"user_exercise_progress": progress.pk, "set_number": 1,
            "weight": weight, "reps": reps}
    if date is not None:
        body["date"] = date.isoformat()
    with commit(execute=True):
        resp = api(client).post("/api/routine/set-logs/", body, content_type=JSON)
    assert resp.status_code in (200, 201), resp.content
    return resp.json()["id"]


def test_the_owner_of_a_set_may_correct_it_and_a_trainer_may_not(
        make_user, api, django_capture_on_commit_callbacks):
    from routine.models import ExerciseSetLog, UserExerciseProgress
    from users.models import TrainerClientRelation

    trainer, client, routine, rows = _program(make_user, "edit")
    TrainerClientRelation.objects.create(trainer=trainer, client=client, status="approved")
    progress = UserExerciseProgress.objects.create(
        user=client, exercise=rows[0].exercise, date=timezone.localdate(), target_sets=3)
    log_id = _log_one_set(api, client, progress, django_capture_on_commit_callbacks)

    mine = api(client).patch(f"/api/routine/set-logs/{log_id}/",
                             json.dumps({"weight": 55, "reps": 6, "notes": "fixed",
                                         "rpe": 8}), content_type=JSON)
    assert mine.status_code == 200, mine.content
    log = ExerciseSetLog.objects.get(pk=log_id)
    assert (log.weight, log.reps, log.notes, log.rpe) == (55.0, 6, "fixed", 8)

    theirs = api(trainer).patch(f"/api/routine/set-logs/{log_id}/",
                                json.dumps({"weight": 200}), content_type=JSON)
    assert theirs.status_code == 403, theirs.content
    assert api(trainer).delete(f"/api/routine/set-logs/{log_id}/").status_code == 403
    log.refresh_from_db()
    assert log.weight == 55.0, "a trainer did not lift it"


def test_a_saved_set_cannot_be_moved_to_another_day_or_progress_row(
        make_user, api, django_capture_on_commit_callbacks):
    from routine.models import ExerciseSetLog, UserExerciseProgress

    trainer, client, routine, rows = _program(make_user, "setmove", exercises=2)
    today = timezone.localdate()
    progress = UserExerciseProgress.objects.create(
        user=client, exercise=rows[0].exercise, date=today, target_sets=3)
    other = UserExerciseProgress.objects.create(
        user=client, exercise=rows[1].exercise, date=today, target_sets=3)
    log_id = _log_one_set(api, client, progress, django_capture_on_commit_callbacks)

    for payload in ({"date": (today - timedelta(days=4)).isoformat()},
                    {"set_number": 9}, {"user_exercise_progress": other.pk}):
        refused = api(client).patch(f"/api/routine/set-logs/{log_id}/",
                                    json.dumps(payload), content_type=JSON)
        assert refused.status_code == 400, (payload, refused.content)
        assert "set_immutable" in _codes(refused.json()), refused.json()

    log = ExerciseSetLog.objects.get(pk=log_id)
    assert (log.date, log.set_number, log.user_exercise_progress_id) == (today, 1, progress.pk)


def test_deleting_a_set_drops_the_days_logged_count_and_breaks_the_streak(
        make_user, api, django_capture_on_commit_callbacks):
    """Nothing recomputed on delete before this: the day kept claiming work that no
    longer existed, so a streak survived on sets its owner had deleted."""
    from routine.models import DayOutcome, UserExerciseProgress

    trainer, client, routine, rows = _program(make_user, "del", sets=1, exercises=1)
    progress = UserExerciseProgress.objects.create(
        user=client, exercise=rows[0].exercise, date=timezone.localdate(), target_sets=1)
    log_id = _log_one_set(api, client, progress, django_capture_on_commit_callbacks)

    closed = api(client).post(f"/api/routine/routines/{routine.pk}/update_progress/",
                              {"day": 1, "status": "completed"}, content_type=JSON)
    assert closed.status_code == 200, closed.content
    outcome = DayOutcome.objects.get(user=client, routine=routine, day=1)
    assert (outcome.logged_sets, outcome.complete) == (1, True)
    assert api(client).get("/api/routine/analytics/streaks/").json()["current_streak"] == 1

    with django_capture_on_commit_callbacks(execute=True):
        gone = api(client).delete(f"/api/routine/set-logs/{log_id}/")
    assert gone.status_code == 204, gone.content

    outcome.refresh_from_db()
    assert (outcome.logged_sets, outcome.missed_sets) == (0, 1)
    assert outcome.complete is False, "a day cannot stay complete on a deleted set"
    progress.refresh_from_db()
    assert progress.completed_sets == 0
    streaks = api(client).get("/api/routine/analytics/streaks/").json()
    assert streaks["current_streak"] == 0
    assert streaks["last_break"]["missed_sets"] == 1


def test_a_frozen_coaching_refuses_the_edit_and_the_delete(
        make_user, api, owners_coaching_rules, django_capture_on_commit_callbacks):
    from tests.test_coaching_freeze import _coached, _freeze

    trainer, client, relation, routine, exercise, progress = _coached(
        make_user, api, "rxfreeze")
    log_id = _log_one_set(api, client, progress, django_capture_on_commit_callbacks)
    # Two days, not one: training DATED a day a paid month ran is still accepted, so a
    # month that ended today would cover today's set.
    _freeze(relation, ago=timedelta(days=2))

    edited = api(client).patch(f"/api/routine/set-logs/{log_id}/",
                               json.dumps({"weight": 90}), content_type=JSON)
    assert edited.status_code == 403, edited.content
    assert edited.json()["code"] == "coaching_frozen", edited.json()

    removed = api(client).delete(f"/api/routine/set-logs/{log_id}/")
    assert removed.status_code == 403, removed.content
    assert removed.json()["code"] == "coaching_frozen", removed.json()


# ================================================================ D. offline batch
def _sync(api, user, rows, commit=None):
    import contextlib

    with (commit(execute=True) if commit else contextlib.nullcontext()):
        resp = api(user).post("/api/routine/set-logs/sync/",
                              json.dumps({"sets": rows}), content_type=JSON)
    return resp


def test_a_batch_is_logged_against_an_exercise_the_phone_never_had_an_id_for(
        make_user, api, django_capture_on_commit_callbacks):
    from routine.models import ExerciseSetLog, UserExerciseProgress

    trainer, client, routine, rows = _program(make_user, "sync", sets=3)
    exercise = rows[0].exercise
    today = timezone.localdate()
    batch = [{"client_uuid": str(uuid.uuid4()), "exercise_id": exercise.pk,
              "date": today.isoformat(), "set_number": number, "weight": 40, "reps": 10,
              "routine_id": routine.pk, "day": 1}
             for number in (1, 2, 3)]

    resp = _sync(api, client, batch, django_capture_on_commit_callbacks)
    assert resp.status_code == 200, resp.content
    body = resp.json()
    assert (body["created"], body["duplicates"], body["errors"]) == (3, 0, 0)
    assert {row["status"] for row in body["results"]} == {"created"}

    progress = UserExerciseProgress.objects.get(user=client, exercise=exercise, date=today)
    assert ExerciseSetLog.objects.filter(user_exercise_progress=progress).count() == 3
    # The server resolved the row AND the coach's prescription for it.
    assert progress.target_sets == 3
    assert progress.completed_sets == 3, "the recomputation ran"


def test_a_re_sent_batch_writes_nothing_new_and_answers_the_same_ids(
        make_user, api, django_capture_on_commit_callbacks):
    from routine.models import ExerciseSetLog

    trainer, client, routine, rows = _program(make_user, "syncidem")
    exercise = rows[0].exercise
    batch = [{"client_uuid": str(uuid.uuid4()), "exercise_id": exercise.pk,
              "set_number": number, "weight": 30, "reps": 12}
             for number in (1, 2)]

    first = _sync(api, client, batch, django_capture_on_commit_callbacks).json()
    written = ExerciseSetLog.objects.count()
    again = _sync(api, client, batch, django_capture_on_commit_callbacks).json()

    assert ExerciseSetLog.objects.count() == written, "a retry logged the sets twice"
    assert (again["created"], again["duplicates"]) == (0, 2)
    assert [row["id"] for row in again["results"]] == [row["id"] for row in first["results"]]


def test_one_bad_row_never_fails_the_batch(make_user, api,
                                           django_capture_on_commit_callbacks):
    trainer, client, routine, rows = _program(make_user, "syncpartial")
    exercise = rows[0].exercise
    today = timezone.localdate()
    batch = [
        {"client_uuid": str(uuid.uuid4()), "exercise_id": exercise.pk,
         "set_number": 1, "weight": 40, "reps": 10},
        {"client_uuid": str(uuid.uuid4()), "exercise_id": exercise.pk,
         "set_number": 2},                                      # no reps
        {"client_uuid": str(uuid.uuid4()), "exercise_id": 9_999_999,
         "set_number": 1, "reps": 5},                           # no such exercise
        {"client_uuid": str(uuid.uuid4()), "exercise_id": exercise.pk,
         "set_number": 3, "reps": 5,
         "date": (today + timedelta(days=1)).isoformat()},      # tomorrow
        {"client_uuid": str(uuid.uuid4()), "exercise_id": exercise.pk,
         "set_number": 4, "reps": 5,
         "date": (today - timedelta(days=400)).isoformat()},    # past the backlog cap
        {"exercise_id": exercise.pk, "set_number": 5, "reps": 5},   # no retry key
        {"client_uuid": str(uuid.uuid4()), "exercise_id": exercise.pk,
         "set_number": 6, "reps": 5, "weight": 5000},           # not a real weight
    ]

    body = _sync(api, client, batch, django_capture_on_commit_callbacks).json()
    assert (body["created"], body["errors"]) == (1, 6), body
    assert [row.get("code") for row in body["results"][1:]] == [
        "reps_required", "exercise_not_found", "date_in_future", "date_too_old",
        "client_uuid_required", "weight_out_of_range"]


def test_a_batch_uses_the_coachs_reps_when_it_is_asked_to(
        make_user, api, django_capture_on_commit_callbacks):
    from routine.models import ExerciseSetLog

    trainer, client, routine, rows = _program(make_user, "syncpresc", sets=3, reps=12)
    key = str(uuid.uuid4())
    body = _sync(api, client, [{"client_uuid": key, "exercise_id": rows[0].exercise.pk,
                                "set_number": 1, "routine_id": routine.pk, "day": 1,
                                "as_prescribed": True}],
                 django_capture_on_commit_callbacks).json()
    assert body["created"] == 1, body
    assert ExerciseSetLog.objects.get(client_uuid=key).reps == 12


def test_a_batch_is_bounded_and_needs_a_list(make_user, api):
    trainer, client, routine, rows = _program(make_user, "syncbounds")
    too_many = [{"client_uuid": str(uuid.uuid4()), "exercise_id": rows[0].exercise.pk,
                 "reps": 5} for _ in range(201)]
    over = api(client).post("/api/routine/set-logs/sync/",
                            json.dumps({"sets": too_many}), content_type=JSON)
    assert (over.status_code, over.json()["code"]) == (400, "batch_too_large")

    shapeless = api(client).post("/api/routine/set-logs/sync/",
                                 json.dumps({"sets": "all of them"}), content_type=JSON)
    assert (shapeless.status_code, shapeless.json()["code"]) == (400, "sets_required")

    # A JSON array body is the batch itself, not a 500 (the gate's shape sweep).
    empty = api(client).post("/api/routine/set-logs/sync/", "[]", content_type=JSON)
    assert empty.status_code == 200, empty.content


def test_a_synced_set_cannot_be_hung_on_someone_elses_id(
        make_user, api, django_capture_on_commit_callbacks):
    trainer, client, routine, rows = _program(make_user, "syncsteal")
    stranger = make_user("rx_sync_stranger")
    key = str(uuid.uuid4())
    row = {"client_uuid": key, "exercise_id": rows[0].exercise.pk, "reps": 5,
           "set_number": 1}
    assert _sync(api, client, [row], django_capture_on_commit_callbacks).json()["created"] == 1

    # The stranger cannot see the exercise either, but the retry key is checked first
    # and says nothing about whose row it is.
    body = _sync(api, stranger, [row], django_capture_on_commit_callbacks).json()
    assert body["results"][0]["code"] == "idempotency_conflict", body


def test_a_frozen_coaching_refuses_only_its_own_rows_of_a_batch(
        make_user, api, owners_coaching_rules, django_capture_on_commit_callbacks):
    """One paused coaching must not throw away the sets that belong to another
    trainer's routine, so the refusal is reported per row."""
    from tests.test_coaching_freeze import _coached, _freeze

    from routine.models import Exercise

    trainer, client, relation, routine, exercise, progress = _coached(
        make_user, api, "rxsyncfreeze")
    free = Exercise.objects.create(name="rx free lift")     # the shared catalogue
    _freeze(relation, ago=timedelta(days=2))

    body = _sync(api, client, [
        {"client_uuid": str(uuid.uuid4()), "exercise_id": exercise.pk, "reps": 5,
         "set_number": 1, "routine_id": routine.pk, "day": 1},
        {"client_uuid": str(uuid.uuid4()), "exercise_id": free.pk, "reps": 5,
         "set_number": 1},
    ], django_capture_on_commit_callbacks).json()

    assert body["results"][0]["code"] == "coaching_frozen", body
    assert body["results"][0]["trainer_id"] == trainer.pk
    assert body["results"][1]["status"] == "created", body
