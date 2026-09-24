"""BACKEND_BUGS B-106, B-109, B-110, B-112: what wiring the train screens found
(2026-09-24), one section per row.

  * B-106 `GET workout-sessions/{id}/?expand=sets` answers the session with its sets
    inline, each with its exercise and the day's prescription beside what was done, in
    a number of queries that does not grow with the sets; the trainer reading it is
    authorised by the one approved-relation predicate. Without `expand` nothing moves.
  * B-109 `set-logs/sync/` keeps `rest_time`, as the single `POST set-logs/` does.
  * B-110 a sync `duplicate` row carries what the server HOLDS for that set: its
    weight, its reps and the record verdict the first answer gave.
  * B-112 a set answered `personal_record: true` names the best it beat,
    `previous_best: {weight, reps, date}` — `strength.personal_best(before=...)`.

See routine/views.py `_sync_one_set`, `ExerciseSetLogViewSet.sync`,
`WorkoutSessionViewSet.retrieve`, and routine/strength.py `record_beaten`.
"""
import json
import uuid
from datetime import timedelta

import pytest
from cachalot.api import cachalot_disabled
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

pytestmark = pytest.mark.django_db

JSON = "application/json"
SYNC = "/api/routine/set-logs/sync/"
SET_LOGS = "/api/routine/set-logs/"
SESSIONS = "/api/routine/workout-sessions/"


# --------------------------------------------------------------------------- helpers
def _program(make_user, tag, *, exercises=1, sets=3, reps=10, weight=None, rest=60):
    """A trainer, a client, and a one-day routine of `exercises` exercises, assigned.
    No relation yet: each test says whether the trainer is approved."""
    from routine.models import Exercise, Routine, RoutineExercise

    trainer = make_user(f"rl_t_{tag}", user_type="trainer")
    client = make_user(f"rl_c_{tag}")
    routine = Routine.objects.create(name=f"rl {tag}", created_by=trainer, days=1)
    rows = []
    for index in range(exercises):
        exercise = Exercise.objects.create(name=f"rl ex {tag} {index}", created_by=trainer)
        rows.append(RoutineExercise.objects.create(
            routine=routine, exercise=exercise, day=1, order=index + 1, sets=sets,
            reps=reps, weight=weight, rest_time=rest))
    routine.assigned_to.add(client)
    return trainer, client, routine, rows


def _lift(user, exercise, days_ago, weight, reps, set_number=1):
    """One set of history, `days_ago`, written straight to the table: it is what came
    BEFORE the write under test, not the write under test."""
    from routine.models import ExerciseSetLog, UserExerciseProgress

    date = timezone.localdate() - timedelta(days=days_ago)
    progress, _ = UserExerciseProgress.objects.get_or_create(
        user=user, exercise=exercise, date=date, defaults={"target_sets": 3})
    return ExerciseSetLog.objects.create(user_exercise_progress=progress,
                                         set_number=set_number, weight=weight,
                                         reps=reps, date=date)


def _row(exercise, **fields):
    return {"client_uuid": str(uuid.uuid4()), "exercise_id": exercise.pk,
            "set_number": 1, "reps": 8, **fields}


def _sync(api, user, rows, commit):
    with commit(execute=True):
        resp = api(user).post(SYNC, json.dumps({"sets": rows}), content_type=JSON)
    assert resp.status_code == 200, resp.content
    return resp.json()


def _by_key(body):
    return {row["client_uuid"]: row for row in body["results"]}


def _post_set(api, user, progress, commit, **fields):
    with commit(execute=True):
        return api(user).post(SET_LOGS, {"user_exercise_progress": progress.pk, **fields},
                              content_type=JSON)


# ============================================================ B-109 rest on a sync row
def test_b109_a_synced_set_keeps_its_rest_time_as_the_single_post_does(
        make_user, api, django_capture_on_commit_callbacks):
    from routine.models import ExerciseSetLog, UserExerciseProgress
    from routine.views import ExerciseSetLogViewSet

    trainer, client, routine, rows = _program(make_user, "rest")
    exercise = rows[0].exercise
    synced = _row(exercise, weight=40, rest_time=95)
    body = _sync(api, client, [synced], django_capture_on_commit_callbacks)
    assert body["created"] == 1, body
    assert ExerciseSetLog.objects.get(client_uuid=synced["client_uuid"]).rest_time == 95, \
        "a set sent in a batch lost its rest"

    # The single POST, for comparison: the same column, the same number.
    progress = UserExerciseProgress.objects.get(user=client, exercise=exercise,
                                                date=timezone.localdate())
    single = _post_set(api, client, progress, django_capture_on_commit_callbacks,
                       set_number=2, weight=40, reps=8, rest_time=95)
    assert single.status_code == 201, single.content
    assert single.json()["rest_time"] == 95

    # The app is built from the docstring: it names the field.
    assert "rest_time" in ExerciseSetLogViewSet.sync.__doc__


def test_b109_a_rest_the_single_post_refuses_is_refused_on_its_own_row(
        make_user, api, django_capture_on_commit_callbacks):
    from routine.models import ExerciseSetLog, UserExerciseProgress

    trainer, client, routine, rows = _program(make_user, "restbad")
    exercise = rows[0].exercise
    batch = [_row(exercise, set_number=1, rest_time=60),
             _row(exercise, set_number=2, rest_time="a minute"),
             _row(exercise, set_number=3, rest_time=12.5),
             _row(exercise, set_number=4)]
    body = _sync(api, client, batch, django_capture_on_commit_callbacks)

    assert (body["created"], body["errors"]) == (2, 2), body
    assert [row.get("code") for row in body["results"]] == [
        None, "rest_time_out_of_range", "rest_time_out_of_range", None]
    stored = dict(ExerciseSetLog.objects.filter(
        client_uuid__in=[batch[0]["client_uuid"], batch[3]["client_uuid"]])
        .values_list("set_number", "rest_time"))
    assert stored == {1: 60, 4: None}, "no rest sent means none stored, not a guess"

    # The single POST refuses the same two values on the same field.
    progress = UserExerciseProgress.objects.get(user=client, exercise=exercise,
                                                date=timezone.localdate())
    for bad in ("a minute", 12.5):
        refused = _post_set(api, client, progress, django_capture_on_commit_callbacks,
                            set_number=9, weight=40, reps=8, rest_time=bad)
        assert refused.status_code == 400, (bad, refused.content)
        assert "rest_time" in json.dumps(refused.json()), refused.json()


# ============================================ B-110 a duplicate says what landed
def test_b110_a_re_sent_batch_learns_the_stored_numbers_and_the_record(
        make_user, api, django_capture_on_commit_callbacks):
    from notifications.models import Notification

    trainer, client, routine, rows = _program(make_user, "dup")
    exercise = rows[0].exercise
    _lift(client, exercise, 3, 50, 8)
    record = _row(exercise, set_number=1, weight=60, reps=6)
    plain = _row(exercise, set_number=2, weight=40, reps=10)

    first = _by_key(_sync(api, client, [record, plain], django_capture_on_commit_callbacks))
    assert first[record["client_uuid"]]["personal_record"] is True
    assert first[plain["client_uuid"]]["personal_record"] is False

    # The answer was lost. The phone re-sends the batch — and its own copy has drifted
    # from what the server stored.
    drifted = {**record, "weight": 57.5, "reps": 5}
    again = _by_key(_sync(api, client, [drifted, plain], django_capture_on_commit_callbacks))
    assert {row["status"] for row in again.values()} == {"duplicate"}
    beaten = again[record["client_uuid"]]
    assert (beaten["weight"], beaten["reps"], beaten["personal_record"]) == (60.0, 6, True), \
        "a duplicate must say what the server holds, and the record the first answer gave"
    assert beaten["previous_best"] == first[record["client_uuid"]]["previous_best"]
    other = again[plain["client_uuid"]]
    assert (other["weight"], other["reps"], other["personal_record"]) == (40.0, 10, False)
    assert other["previous_best"] is None
    # Told, not celebrated again.
    assert Notification.objects.filter(recipient=client,
                                       event_type="personal_record").count() == 1


def test_b110_a_key_repeated_inside_one_batch_answers_its_twins_verdict(
        make_user, api, django_capture_on_commit_callbacks):
    trainer, client, routine, rows = _program(make_user, "twin")
    exercise = rows[0].exercise
    _lift(client, exercise, 2, 50, 8)
    record = _row(exercise, weight=60, reps=6)

    created, duplicate = _sync(api, client, [record, dict(record)],
                               django_capture_on_commit_callbacks)["results"]
    assert (created["status"], duplicate["status"]) == ("created", "duplicate")
    assert duplicate["id"] == created["id"]
    assert created["personal_record"] is True
    assert (duplicate["weight"], duplicate["reps"], duplicate["personal_record"]) == \
        (60.0, 6, True), "the same set answered two verdicts in one response"
    assert duplicate["previous_best"] == created["previous_best"]


# ================================================ B-112 the best a record beat
def test_b112_a_record_on_a_single_post_names_the_best_it_beat(
        make_user, api, django_capture_on_commit_callbacks):
    from routine.models import UserExerciseProgress

    trainer, client, routine, rows = _program(make_user, "prev")
    exercise = rows[0].exercise
    _lift(client, exercise, 3, 50, 8)
    best = _lift(client, exercise, 2, 50, 10)       # heaviest, and among equals most reps
    _lift(client, exercise, 1, 45, 12)
    progress = UserExerciseProgress.objects.create(user=client, exercise=exercise,
                                                   date=timezone.localdate(), target_sets=3)

    beaten = _post_set(api, client, progress, django_capture_on_commit_callbacks,
                       set_number=1, weight=52.5, reps=6)
    assert beaten.status_code == 201, beaten.content
    assert beaten.json()["personal_record"] is True
    assert beaten.json()["previous_best"] == {"weight": 50.0, "reps": 10,
                                              "date": best.date.isoformat()}

    lighter = _post_set(api, client, progress, django_capture_on_commit_callbacks,
                        set_number=2, weight=40, reps=8)
    assert lighter.status_code == 201, lighter.content
    assert lighter.json()["personal_record"] is False
    assert lighter.json()["previous_best"] is None

    # A list read judges nothing, so it names nothing either.
    listed = api(client).get(f"{SET_LOGS}?date={timezone.localdate().isoformat()}").json()
    assert {row["previous_best"] for row in listed["results"]} == {None}


def test_b112_the_first_set_of_an_exercise_beat_nothing(
        make_user, api, django_capture_on_commit_callbacks):
    from routine.models import UserExerciseProgress

    trainer, client, routine, rows = _program(make_user, "prevfirst")
    progress = UserExerciseProgress.objects.create(user=client, exercise=rows[0].exercise,
                                                   date=timezone.localdate(), target_sets=3)
    first = _post_set(api, client, progress, django_capture_on_commit_callbacks,
                      set_number=1, weight=20, reps=10)
    assert first.status_code == 201, first.content
    assert (first.json()["personal_record"], first.json()["previous_best"]) == (False, None)


def test_b112_a_synced_record_names_exactly_the_set_it_beat(
        make_user, api, django_capture_on_commit_callbacks):
    """Inside a batch the earlier, lighter set of the same batch IS what the top set
    beat: a record is judged against everything written before it
    (`strength.personal_best(before=...)`), and `previous_best` is that same answer."""
    from routine.strength import personal_best

    trainer, client, routine, rows = _program(make_user, "prevsync")
    exercise = rows[0].exercise
    _lift(client, exercise, 2, 50, 8)
    warm = _row(exercise, set_number=1, weight=52, reps=8)
    top = _row(exercise, set_number=2, weight=60, reps=5)

    body = _by_key(_sync(api, client, [warm, top], django_capture_on_commit_callbacks))
    top_row, warm_row = body[top["client_uuid"]], body[warm["client_uuid"]]
    assert top_row["personal_record"] is True
    assert top_row["previous_best"] == {"weight": 52.0, "reps": 8,
                                        "date": timezone.localdate().isoformat()}
    expected = personal_best(client, exercise.pk, before=top_row["id"])
    assert top_row["previous_best"] == {"weight": expected["weight"],
                                        "reps": expected["reps"],
                                        "date": expected["date"].isoformat()}
    assert (warm_row["personal_record"], warm_row["previous_best"]) == (False, None)
    # A `created` row says what was stored, as a `duplicate` does (B-110).
    assert (top_row["weight"], top_row["reps"]) == (60.0, 5)


# ============================================ B-106 one read for the coach's review
def _reviewed_session(make_user, api, commit, tag, *, approved=True, extra_exercises=1,
                      done=((82.5, 8, 90), (82.5, 8, 120), (82.5, 7, 150), (80, 8, 100))):
    """A client's finished day of a routine prescribing 4 × 8 at 82.5 kg with 120 s
    rest: the first exercise done as `done` (weight, reps, rest), logged through
    `sync/` the way the phone logs it, on top of last week's 80 × 8."""
    from routine.models import WorkoutSession
    from users.models import TrainerClientRelation

    trainer, client, routine, rows = _program(make_user, tag, exercises=1 + extra_exercises,
                                              sets=4, reps=8, weight=82.5, rest=120)
    bench = rows[0]
    _lift(client, bench.exercise, 7, 80, 8)
    began = timezone.now() - timedelta(hours=1)
    session = WorkoutSession.objects.create(user=client, routine=routine, day=1,
                                            start_time=began, status="completed",
                                            end_time=began + timedelta(minutes=47))
    _sync(api, client, [
        _row(bench.exercise, set_number=number, weight=weight, reps=reps, rest_time=rest,
             workout_session_id=session.pk)
        for number, (weight, reps, rest) in enumerate(done, start=1)], commit)
    if approved:
        TrainerClientRelation.objects.create(trainer=trainer, client=client,
                                             status="approved")
    return trainer, client, routine, rows, session


def test_b106_expand_sets_places_what_was_planned_beside_what_was_done(
        make_user, api, django_capture_on_commit_callbacks):
    trainer, client, routine, rows, session = _reviewed_session(
        make_user, api, django_capture_on_commit_callbacks, "review")
    bench, press = rows

    resp = api(trainer).get(f"{SESSIONS}{session.pk}/?expand=sets")
    assert resp.status_code == 200, resp.content
    body = resp.json()
    sets = body["sets"]
    assert [(s["set_number"], s["weight"], s["reps"], s["rest_time"]) for s in sets] == [
        (1, 82.5, 8, 90), (2, 82.5, 8, 120), (3, 82.5, 7, 150), (4, 80.0, 8, 100)]
    assert {json.dumps(s["exercise"], sort_keys=True) for s in sets} == {
        json.dumps({"id": bench.exercise.pk, "name": bench.exercise.name}, sort_keys=True)}
    planned = {"id": bench.pk, "sets": 4, "reps": 8, "weight": 82.5, "rest_time": 120,
               "order": 1}
    assert all(s["prescription"] == planned for s in sets), sets
    # The record the client was told about, and only that one.
    assert [s["personal_record"] for s in sets] == [True, False, False, False]
    # The whole day's prescription, the exercise nobody touched included.
    assert [(p["exercise"]["id"], p["sets"], p["reps"], p["weight"], p["rest_time"])
            for p in body["planned"]] == [
        (bench.exercise.pk, 4, 8, 82.5, 120), (press.exercise.pk, 4, 8, 82.5, 120)]
    assert body["routine_name"] == routine.name

    # The client reads their own session the same way.
    own = api(client).get(f"{SESSIONS}{session.pk}/?expand=sets")
    assert own.status_code == 200, own.content
    assert [s["id"] for s in own.json()["sets"]] == [s["id"] for s in sets]


def test_b106_without_expand_the_answer_is_unchanged(
        make_user, api, django_capture_on_commit_callbacks):
    trainer, client, routine, rows, session = _reviewed_session(
        make_user, api, django_capture_on_commit_callbacks, "plain")

    plain = api(trainer).get(f"{SESSIONS}{session.pk}/")
    assert plain.status_code == 200, plain.content
    assert set(plain.json()) == {"id", "user", "routine", "day", "start_time", "end_time",
                                 "status", "duration", "client_uuid", "effort_rating"}
    expanded = api(trainer).get(f"{SESSIONS}{session.pk}/?expand=sets").json()
    assert {key: expanded[key] for key in plain.json()} == plain.json(), \
        "expanding must add keys, never change one"
    # The list is not expanded: the review is one session.
    listed = api(trainer).get(f"{SESSIONS}?expand=sets").json()["results"]
    assert listed and all("sets" not in row for row in listed)


def test_b106_an_unknown_expansion_is_named_not_ignored(
        make_user, api, django_capture_on_commit_callbacks):
    trainer, client, routine, rows, session = _reviewed_session(
        make_user, api, django_capture_on_commit_callbacks, "badexp")
    refused = api(trainer).get(f"{SESSIONS}{session.pk}/?expand=setz")
    assert refused.status_code == 400, refused.content
    assert refused.json()["code"] == "bad_expand"
    assert refused.json()["field"] == "expand"


def test_b106_the_expansion_does_not_query_per_set_or_per_exercise(
        make_user, api, django_capture_on_commit_callbacks):
    from routine.models import WorkoutSession

    trainer, client, routine, rows, small = _reviewed_session(
        make_user, api, django_capture_on_commit_callbacks, "queries", extra_exercises=2,
        done=((60, 8, 90),))
    began = timezone.now() - timedelta(minutes=30)
    large = WorkoutSession.objects.create(user=client, routine=routine, day=1,
                                          start_time=began, status="completed",
                                          end_time=began + timedelta(minutes=25))
    # The approved relation is in place now, and `_require_can_train` would ask about a
    # coaching month: these twelve sets are history, written straight to the table.
    from routine.models import ExerciseSetLog, UserExerciseProgress
    for row in rows:
        progress, _ = UserExerciseProgress.objects.get_or_create(
            user=client, exercise=row.exercise, date=timezone.localdate(),
            defaults={"target_sets": 4})
        for number in range(1, 5):
            ExerciseSetLog.objects.create(user_exercise_progress=progress,
                                          workout_session=large, set_number=number + 10,
                                          weight=50, reps=8, date=timezone.localdate())

    reader = api(trainer)
    reader.get(f"{SESSIONS}{small.pk}/?expand=sets")      # warm the per-process caches
    with cachalot_disabled(), CaptureQueriesContext(connection) as plain:
        reader.get(f"{SESSIONS}{large.pk}/")
    with cachalot_disabled(), CaptureQueriesContext(connection) as one_set:
        few = reader.get(f"{SESSIONS}{small.pk}/?expand=sets")
    with cachalot_disabled(), CaptureQueriesContext(connection) as twelve_sets:
        many = reader.get(f"{SESSIONS}{large.pk}/?expand=sets")
    assert (len(few.json()["sets"]), len(many.json()["sets"])) == (1, 12)
    assert len({s["exercise"]["id"] for s in many.json()["sets"]}) == 3
    assert len(twelve_sets) == len(one_set), "the review costs a query per set or exercise"
    assert len(twelve_sets) <= len(plain) + 1, \
        "expanding costs more than the one read of the day's prescription"


def test_b106_only_the_approved_relation_opens_a_clients_session(
        make_user, api, django_capture_on_commit_callbacks):
    """The trainer is authorised by the one predicate (`can_access_user_data` /
    `accessible_user_ids`, the approved relation) — never by the `assigned_trainer`
    display pointer, and never by a request still pending."""
    from users.models import CustomUser, TrainerClientRelation

    trainer, client, routine, rows, session = _reviewed_session(
        make_user, api, django_capture_on_commit_callbacks, "authz", approved=False)
    url = f"{SESSIONS}{session.pk}/?expand=sets"

    pointer_only = make_user("rl_t_authz_pointer", user_type="trainer")
    CustomUser.objects.filter(pk=client.pk).update(assigned_trainer=pointer_only)
    pending = make_user("rl_t_authz_pending", user_type="trainer")
    TrainerClientRelation.objects.create(trainer=pending, client=client, status="pending")
    stranger = make_user("rl_c_authz_stranger")

    for outsider in (trainer, pointer_only, pending, stranger):
        refused = api(outsider).get(url)
        assert refused.status_code == 404, (outsider.username, refused.content)
        assert "sets" not in refused.json()

    TrainerClientRelation.objects.create(trainer=trainer, client=client, status="approved")
    allowed = api(trainer).get(url)
    assert allowed.status_code == 200, allowed.content
    assert len(allowed.json()["sets"]) == 4
