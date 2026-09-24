"""The review of the routine-logging build (2026-09-24): three findings a reviewer and a
verifier both confirmed, each reproduced here before it was fixed.

  * The coach's review (`GET workout-sessions/{id}/?expand=sets`, B-106) read the
    routine's LIVE rows, so the coach editing the client's copy the next morning
    rewrote what the review said was planned — and contradicted the day's own
    `DayOutcome`, whose plan is frozen so that "an edit must not rewrite last week".
    A closed day is now reviewed against its frozen plan; an open day against the live
    one. The cost stays one query more than the plain detail.
  * A `duplicate` row of `set-logs/sync/` answered the stored verdict (B-110) beside a
    RECOMPUTED `previous_best` (B-112): an earlier set corrected or deleted between a
    lost answer and its retry turned the record card into «+(-10) kg», or a record with
    nothing beaten. What a record beat is now stored with the verdict, on the set.
  * The race branch of `_sync_one_set` (two sends of one key at once) answered the
    twin it found by `client_uuid` without the owner test step 1 applies, so another
    person's weight, reps and record could come back. It is now `idempotency_conflict`,
    exactly as step 1 answers.

See routine/views.py `_session_review`, `_sync_answer`, `_sync_one_set`,
`_celebrate_personal_record`, and routine/outcomes.py `plan_from_detail`.
"""
import json
import types
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
ROUTINE_EXERCISES = "/api/routine/routine-exercises/"


# --------------------------------------------------------------------------- helpers
def _program(make_user, tag, *, exercises=2, sets=4, reps=8, weight=82.5, rest=120):
    """A trainer, a client and a one-day routine of `exercises` exercises (4 × 8 at
    82.5 kg, 120 s rest by default), assigned. No relation yet."""
    from routine.models import Exercise, Routine, RoutineExercise

    trainer = make_user(f"rrl_t_{tag}", user_type="trainer")
    client = make_user(f"rrl_c_{tag}")
    routine = Routine.objects.create(name=f"rrl {tag}", created_by=trainer, days=1)
    rows = []
    for index in range(exercises):
        exercise = Exercise.objects.create(name=f"rrl ex {tag} {index}", created_by=trainer)
        rows.append(RoutineExercise.objects.create(
            routine=routine, exercise=exercise, day=1, order=index + 1, sets=sets,
            reps=reps, weight=weight, rest_time=rest, notes=f"cue {index}"))
    routine.assigned_to.add(client)
    return trainer, client, routine, rows


def _lift(user, exercise, days_ago, weight, reps, set_number=1):
    """One set of history, written straight to the table: it came BEFORE the write
    under test."""
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


def _trained_day(make_user, api, commit, tag, *, logged=2, exercises=2, finish=True):
    """The client trains the first `logged` exercises of the day in a session, 4 × 8
    at 82.5 kg each, through `sync/` as the phone does, and — when `finish` — finishes
    the session through the API, which closes the day and freezes its plan in a
    `DayOutcome`. Then the trainer's relation is approved, so they can review it."""
    from routine.models import WorkoutSession
    from users.models import TrainerClientRelation

    trainer, client, routine, rows = _program(make_user, tag, exercises=exercises)
    began = timezone.now() - timedelta(hours=1)
    session = WorkoutSession.objects.create(user=client, routine=routine, day=1,
                                            start_time=began, status="active")
    _sync(api, client, [
        _row(row.exercise, set_number=number, weight=82.5, reps=8, rest_time=120,
             workout_session_id=session.pk)
        for row in rows[:logged] for number in range(1, 5)], commit)
    if finish:
        with commit(execute=True):
            done = api(client).patch(f"{SESSIONS}{session.pk}/",
                                     json.dumps({"status": "completed"}),
                                     content_type=JSON)
        assert done.status_code == 200, done.content
    TrainerClientRelation.objects.create(trainer=trainer, client=client, status="approved")
    return trainer, client, routine, rows, session


def _coach_edits(api, trainer, row, commit, **fields):
    """The coach changes the client's copy in place, through the route the app uses."""
    with commit(execute=True):
        resp = api(trainer).patch(f"{ROUTINE_EXERCISES}{row.pk}/", json.dumps(fields),
                                  content_type=JSON)
    assert resp.status_code == 200, resp.content


def _coach_deletes(api, trainer, row, commit):
    with commit(execute=True):
        resp = api(trainer).delete(f"{ROUTINE_EXERCISES}{row.pk}/")
    assert resp.status_code == 204, resp.content


def _review(api, reader, session):
    resp = api(reader).get(f"{SESSIONS}{session.pk}/?expand=sets")
    assert resp.status_code == 200, resp.content
    return resp.json()


# ================================== 1. the review reads the plan the day closed with
def test_a_closed_day_is_reviewed_against_the_plan_it_closed_with(
        make_user, api, django_capture_on_commit_callbacks):
    """The verifier's scenario. The day prescribed 4 × 8 at 82.5 kg on two exercises
    (and a third nobody trained); the client trained the first two and finished. The
    next morning the coach raised bench to 5 × 6 at 90 kg, deleted the second exercise
    and the third. The review must still say what the day asked for — what its
    `DayOutcome` says — and not today's plan."""
    from routine.models import DayOutcome

    commit = django_capture_on_commit_callbacks
    trainer, client, routine, rows, session = _trained_day(
        make_user, api, commit, "closed", logged=2, exercises=3)
    bench, press, untouched = rows
    outcome = DayOutcome.objects.get(user=client, routine=routine, day=1)
    assert (outcome.planned_sets, outcome.logged_sets) == (12, 8)

    _coach_edits(api, trainer, bench, commit, sets=5, reps=6, weight=90)
    _coach_deletes(api, trainer, press, commit)
    _coach_deletes(api, trainer, untouched, commit)

    body = _review(api, trainer, session)
    as_planned = [(p["id"], p["exercise"]["id"], p["sets"], p["reps"], p["weight"],
                   p["order"]) for p in body["planned"]]
    assert as_planned == [
        (bench.pk, bench.exercise.pk, 4, 8, 82.5, 1),
        (press.pk, press.exercise.pk, 4, 8, 82.5, 2),
        (untouched.pk, untouched.exercise.pk, 4, 8, 82.5, 3)], \
        "the coach's edit the next morning rewrote what the review says was planned"
    # The review agrees with the day's own record of it.
    assert sum(p["sets"] for p in body["planned"]) == outcome.planned_sets
    # Names come with every row, the deleted ones included, in the reader's language.
    assert [p["exercise"]["name"] for p in body["planned"]] == [
        bench.exercise.name, press.exercise.name, untouched.exercise.name]
    # What the snapshot does not hold is read live while the row exists, and is
    # empty for a row that is gone.
    assert [(p["rest_time"], p["notes"]) for p in body["planned"]] == [
        (120, "cue 0"), (None, ""), (None, "")]

    by_exercise = {}
    for done in body["sets"]:
        by_exercise.setdefault(done["exercise"]["id"], []).append(done)
    assert set(by_exercise) == {bench.exercise.pk, press.exercise.pk}
    for row in (bench, press):
        prescriptions = {json.dumps(done["prescription"], sort_keys=True)
                         for done in by_exercise[row.exercise.pk]}
        assert len(prescriptions) == 1, prescriptions
        prescription = json.loads(prescriptions.pop())
        assert (prescription["id"], prescription["sets"], prescription["reps"],
                prescription["weight"]) == (row.pk, 4, 8, 82.5), \
            "a set was measured against a plan written after it was lifted"
    # Grouped in the day's order, as before.
    assert [done["exercise"]["id"] for done in body["sets"]] == \
        [bench.exercise.pk] * 4 + [press.exercise.pk] * 4


def test_a_day_still_open_is_reviewed_against_the_live_plan(
        make_user, api, django_capture_on_commit_callbacks):
    """No outcome yet: nothing has been frozen, and the day's plan is the routine's."""
    from routine.models import DayOutcome

    commit = django_capture_on_commit_callbacks
    trainer, client, routine, rows, session = _trained_day(
        make_user, api, commit, "open", logged=1, finish=False)
    bench, press = rows
    assert not DayOutcome.objects.filter(user=client, routine=routine).exists()

    _coach_edits(api, trainer, bench, commit, sets=5, reps=6, weight=90)
    body = _review(api, trainer, session)
    assert [(p["id"], p["sets"], p["reps"], p["weight"]) for p in body["planned"]] == [
        (bench.pk, 5, 6, 90.0), (press.pk, 4, 8, 82.5)]
    assert {(s["prescription"]["sets"], s["prescription"]["weight"])
            for s in body["sets"]} == {(5, 90.0)}


def test_the_frozen_plan_costs_no_query_per_set_or_exercise(
        make_user, api, django_capture_on_commit_callbacks):
    """B-106's budget holds on the closed path: one query more than the plain detail,
    whatever the sets — also after the coach edited one row and deleted one that was
    trained (its name comes with the session's own sets)."""
    commit = django_capture_on_commit_callbacks
    trainer, client, routine, rows, session = _trained_day(
        make_user, api, commit, "budget", logged=3, exercises=3)
    _coach_edits(api, trainer, rows[0], commit, sets=5, reps=6, weight=90)
    _coach_deletes(api, trainer, rows[1], commit)

    reader = api(trainer)
    reader.get(f"{SESSIONS}{session.pk}/?expand=sets")     # warm the per-process caches
    with cachalot_disabled(), CaptureQueriesContext(connection) as plain:
        reader.get(f"{SESSIONS}{session.pk}/")
    with cachalot_disabled(), CaptureQueriesContext(connection) as expanded:
        body = reader.get(f"{SESSIONS}{session.pk}/?expand=sets").json()
    assert len(body["sets"]) == 12
    assert [p["sets"] for p in body["planned"]] == [4, 4, 4]
    assert len(expanded) <= len(plain) + 1, \
        "the frozen plan costs more than the one read of the day's prescription"


# ================== 2. a duplicate answers the best the record beat when it was judged
def test_a_duplicate_answers_the_best_its_record_beat_not_a_recomputed_one(
        make_user, api, django_capture_on_commit_callbacks):
    """The prior best is 90 × 8. A sync of 100 × 5 is a record over it, and the answer
    is lost. The client then corrects the earlier set to 110 kg — and later deletes it.
    Each re-send of the batch must still say what the first answer said."""
    from routine.models import ExerciseSetLog

    commit = django_capture_on_commit_callbacks
    trainer, client, routine, rows = _program(make_user, "prevstored", exercises=1)
    exercise = rows[0].exercise
    earlier = _lift(client, exercise, 3, 90, 8)
    record = _row(exercise, set_number=1, weight=100, reps=5)

    first = _by_key(_sync(api, client, [record], commit))[record["client_uuid"]]
    assert first["personal_record"] is True
    told = {"weight": 90.0, "reps": 8, "date": earlier.date.isoformat()}
    assert first["previous_best"] == told

    with commit(execute=True):
        corrected = api(client).patch(f"{SET_LOGS}{earlier.pk}/",
                                      json.dumps({"weight": 110}), content_type=JSON)
    assert corrected.status_code == 200, corrected.content
    again = _by_key(_sync(api, client, [record], commit))[record["client_uuid"]]
    assert again["status"] == "duplicate"
    assert (again["personal_record"], again["previous_best"]) == (True, told), \
        "the duplicate recomputed what the record beat: «+(-10) kg»"

    with commit(execute=True):
        gone = api(client).delete(f"{SET_LOGS}{earlier.pk}/")
    assert gone.status_code == 204, gone.content
    third = _by_key(_sync(api, client, [record], commit))[record["client_uuid"]]
    assert (third["personal_record"], third["previous_best"]) == (True, told), \
        "a true verdict answered with nothing beaten"

    # It is kept on the set, beside the verdict, as it was judged.
    stored = ExerciseSetLog.objects.get(client_uuid=record["client_uuid"])
    assert (stored.personal_record, stored.previous_best) == (True, told)


def test_a_set_that_beat_nothing_stores_nothing_beaten(
        make_user, api, django_capture_on_commit_callbacks):
    from routine.models import ExerciseSetLog

    commit = django_capture_on_commit_callbacks
    trainer, client, routine, rows = _program(make_user, "prevnone", exercises=1)
    exercise = rows[0].exercise
    _lift(client, exercise, 2, 90, 8)
    lighter = _row(exercise, set_number=1, weight=80, reps=8)

    body = _by_key(_sync(api, client, [lighter], commit))
    assert (body[lighter["client_uuid"]]["personal_record"],
            body[lighter["client_uuid"]]["previous_best"]) == (False, None)
    again = _by_key(_sync(api, client, [lighter], commit))[lighter["client_uuid"]]
    assert (again["status"], again["personal_record"], again["previous_best"]) == \
        ("duplicate", False, None)
    stored = ExerciseSetLog.objects.get(client_uuid=lighter["client_uuid"])
    assert (stored.personal_record, stored.previous_best) == (False, None)


# ======================== 3. the race branch never answers with someone else's set
def _catalogue_exercise(tag):
    """An exercise of the shared catalogue: every client may read it, so the only thing
    that decides below is who owns the key."""
    from routine.models import Exercise
    return Exercise.objects.create(name=f"rrl catalogue {tag}", created_by=None)


def test_a_raced_key_that_is_someone_elses_is_a_conflict_not_their_set(
        make_user, api, django_capture_on_commit_callbacks):
    """B's batch was read (`known`) before A's row with the same key committed, so B
    reaches the create, and the unique index refuses it. What B is answered must be
    what step 1 answers for that key: `idempotency_conflict`, and nothing of A's."""
    from routine.models import ExerciseSetLog
    from routine.views import _sync_one_set

    commit = django_capture_on_commit_callbacks
    owner = make_user("rrl_c_race_owner")
    other = make_user("rrl_c_race_other")
    exercise = _catalogue_exercise("race")
    _lift(owner, exercise, 3, 90, 8)
    key = uuid.uuid4()
    mine = {"client_uuid": str(key), "exercise_id": exercise.pk, "set_number": 1,
            "weight": 120, "reps": 3}
    written = _by_key(_sync(api, owner, [mine], commit))[str(key)]
    assert written["status"] == "created" and written["personal_record"] is True

    # The normal path, for comparison: the key is known, and it is not B's.
    normal = _by_key(_sync(api, other, [mine], commit))[str(key)]
    assert (normal["status"], normal["code"]) == ("error", "idempotency_conflict")

    # The race: `known` was read before A's row existed.
    request = types.SimpleNamespace(user=other)
    today = timezone.localdate()
    answer, touched, log = _sync_one_set(request, dict(mine), key, {}, today)
    assert (answer["status"], answer.get("code")) == ("error", "idempotency_conflict"), \
        f"the race branch answered another person's set: {answer}"
    for leaked in ("id", "weight", "reps", "personal_record", "previous_best"):
        assert leaked not in answer, (leaked, answer)
    assert (touched, log) == (None, None)
    # A's set is untouched, and nothing was written for B.
    assert ExerciseSetLog.objects.filter(client_uuid=key).count() == 1
    assert not ExerciseSetLog.objects.filter(
        user_exercise_progress__user=other).exists()


def test_a_raced_key_that_is_your_own_still_answers_your_set(
        make_user, api, django_capture_on_commit_callbacks):
    """The race branch exists for the owner's own double send: that still answers the
    row the first send wrote, with its stored numbers."""
    from routine.views import _sync_one_set

    commit = django_capture_on_commit_callbacks
    owner = make_user("rrl_c_race_self")
    exercise = _catalogue_exercise("self")
    key = uuid.uuid4()
    mine = {"client_uuid": str(key), "exercise_id": exercise.pk, "set_number": 1,
            "weight": 60, "reps": 5}
    written = _by_key(_sync(api, owner, [mine], commit))[str(key)]

    answer, touched, log = _sync_one_set(types.SimpleNamespace(user=owner), dict(mine),
                                         key, {}, timezone.localdate())
    assert (answer["status"], answer["id"], answer["weight"], answer["reps"]) == \
        ("duplicate", written["id"], 60.0, 5)
    assert log is None and touched is not None
