"""Routine: set logs, sessions, day outcomes, analytics, the nightly tasks, progression.

One test per SIDE_BUGS row closed on branch `fx/rttr` (the independent dive of
2026-09-23). Each one fails against the code before the fix for the reason the row
gives, and says so in its docstring.
"""
import json
import re
import uuid
from datetime import datetime, time, timedelta

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

pytestmark = pytest.mark.django_db

JSON = "application/json"


# --------------------------------------------------------------------------- helpers
@pytest.fixture
def owners_coaching_rules(settings):
    """The owner's numbers, as `tests/test_coaching_freeze.py` sets them. Requested by
    name: most of this file has nothing to do with money."""
    from tests.test_coaching_months import OWNER_RATE

    settings.COACHING_COMMISSION_RATE = OWNER_RATE
    settings.COACHING_PERIOD_DAYS = 30
    settings.COACHING_RENEWAL_REMINDER_DAYS = 3
    settings.COACHING_FROZEN_REMINDER_DAYS = (3, 7, 14, 30)


def _program(make_user, tag, *, days=1, sets=3, reps=10, coached=False, assign=True):
    """A trainer, a client and a routine of `days` days, ONE distinct exercise a day.
    Returns `(trainer, client, routine, rows)` with `rows[d - 1]` the day-d row."""
    from routine.models import Exercise, Routine, RoutineExercise
    from users.models import TrainerClientRelation

    trainer = make_user(f"fr_t_{tag}", user_type="trainer")
    client = make_user(f"fr_c_{tag}")
    routine = Routine.objects.create(name=f"fr {tag}", created_by=trainer, days=days,
                                     start_date=timezone.localdate())
    rows = []
    for day in range(1, days + 1):
        exercise = Exercise.objects.create(name=f"fr ex {tag} {day}", created_by=trainer)
        rows.append(RoutineExercise.objects.create(routine=routine, exercise=exercise,
                                                   day=day, order=1, sets=sets, reps=reps))
    if coached:
        TrainerClientRelation.objects.create(trainer=trainer, client=client,
                                             status="approved")
    if assign:
        routine.assigned_to.add(client)
    return trainer, client, routine, rows


def _progress(user, row, date=None):
    from routine.models import UserExerciseProgress

    progress, _created = UserExerciseProgress.objects.get_or_create(
        user=user, exercise=row.exercise, date=date or timezone.localdate(),
        defaults={"target_sets": row.sets or 0})
    return progress


def _session(user, routine, *, day=1, status="active", began=None, ended=None):
    from routine.models import WorkoutSession

    return WorkoutSession.objects.create(user=user, routine=routine, day=day,
                                         status=status,
                                         start_time=began or timezone.now(),
                                         end_time=ended)


def _set(progress, *, number=1, session=None, weight=50, reps=8):
    from routine.models import ExerciseSetLog

    return ExerciseSetLog.objects.create(user_exercise_progress=progress,
                                         workout_session=session, set_number=number,
                                         weight=weight, reps=reps, date=progress.date)


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


def _queries(call):
    with CaptureQueriesContext(connection) as seen:
        response = call()
    return response, [q["sql"] for q in seen.captured_queries]


# ============================================================ S-067 session writes
def test_s067_a_trainer_cannot_finish_or_delete_a_clients_session_and_a_freeze_refuses_the_owner(
        make_user, api, owners_coaching_rules):
    """A trainer could PATCH and DELETE a client's session, and DELETE cascades to every
    set in it — so the rule "a trainer may not delete a client's set" was bypassed by
    deleting the session, and there was no destroy hook, so a frozen coaching did not
    refuse the owner either. Before the fix the trainer's PATCH answered 200."""
    from routine.models import ExerciseSetLog, WorkoutSession
    from tests.test_coaching_freeze import _coached, _freeze

    trainer, client, relation, routine, exercise, progress = _coached(
        make_user, api, "s067")
    session = _session(client, routine)
    log = _set(progress, session=session)

    finished = api(trainer).patch(f"/api/routine/workout-sessions/{session.pk}/",
                                  json.dumps({"status": "completed"}), content_type=JSON)
    assert finished.status_code == 403, finished.content
    removed = api(trainer).delete(f"/api/routine/workout-sessions/{session.pk}/")
    assert removed.status_code == 403, removed.content
    session.refresh_from_db()
    assert session.status == "active"
    assert ExerciseSetLog.objects.filter(pk=log.pk).exists(), "a trainer did not lift it"
    # Reads are unchanged.
    assert api(trainer).get(f"/api/routine/workout-sessions/{session.pk}/").status_code == 200

    # The owner's delete is a training write: refused while the coaching is frozen.
    _freeze(relation, ago=timedelta(days=2))
    frozen = api(client).delete(f"/api/routine/workout-sessions/{session.pk}/")
    assert frozen.status_code == 403, frozen.content
    assert frozen.json()["code"] == "coaching_frozen", frozen.json()
    assert WorkoutSession.objects.filter(pk=session.pk).exists()
    assert ExerciseSetLog.objects.filter(pk=log.pk).exists()


# ================================================= S-068 correcting an earlier set
def test_s068_a_set_from_an_earlier_day_can_be_corrected(make_user, api):
    """`validate()` filled an omitted `date` with TODAY on an update too, so every PATCH
    of a set not dated today failed with 400 `set_date_mismatch`."""
    from routine.models import ExerciseSetLog

    trainer, client, routine, rows = _program(make_user, "s068")
    yesterday = timezone.localdate() - timedelta(days=1)
    log = _set(_progress(client, rows[0], yesterday))

    fixed = api(client).patch(f"/api/routine/set-logs/{log.pk}/",
                              json.dumps({"weight": 72.5, "reps": 6}), content_type=JSON)
    assert fixed.status_code == 200, fixed.content
    # Re-sending the stored date (a PUT-shaped body) is the same no-op.
    again = api(client).patch(f"/api/routine/set-logs/{log.pk}/",
                              json.dumps({"weight": 75, "date": yesterday.isoformat()}),
                              content_type=JSON)
    assert again.status_code == 200, again.content
    log = ExerciseSetLog.objects.get(pk=log.pk)
    assert (log.weight, log.reps, log.date) == (75.0, 6, yesterday)


# ======================================================= S-069 the nightly sweep
def test_s069_training_day_one_on_the_start_date_does_not_sweep_the_other_days(make_user):
    """Assignment writes a `not_started` row for EVERY day, all dated the start date, and
    the sweep called such a row "worked" if ANY set was logged that date. Training day 1
    on the start date therefore recorded days 2..N as `unclosed` misses."""
    from routine.models import DayOutcome, RoutineProgress
    from routine.tasks import close_open_training_days

    trainer, client, routine, rows = _program(make_user, "s069", days=3)
    today = timezone.localdate()
    assert RoutineProgress.objects.filter(user=client, routine=routine, date=today).count() == 3

    day_one = _session(client, routine, day=1)
    for number in (1, 2, 3):
        _set(_progress(client, rows[0]), number=number, session=day_one)
    # The control: a day with work of its OWN is still swept — one of three sets.
    day_two = _session(client, routine, day=2)
    _set(_progress(client, rows[1]), number=1, session=day_two)

    close_open_training_days(before=today + timedelta(days=1))

    assert not DayOutcome.objects.filter(user=client, routine=routine, day=3).exists(), \
        "nobody opened day 3: it is not a miss"
    swept = DayOutcome.objects.get(user=client, routine=routine, day=2)
    assert (swept.via, swept.logged_sets, swept.missed_sets) == ("unclosed", 1, 2)


# ============================================= S-071 log the whole day, duplicates
def test_s071_log_the_whole_day_survives_a_duplicated_set_number(make_user, api):
    """Only `client_uuid` is unique, so two single POSTs of "set 1" without it write two
    rows; `bulk-create`'s `update_or_create` then raised MultipleObjectsReturned (not an
    IntegrityError) and every later "log the whole day" for that day was a 500."""
    from routine.models import ExerciseSetLog

    trainer, client, routine, rows = _program(make_user, "s071")
    progress = _progress(client, rows[0])
    for _attempt in range(2):
        sent = api(client).post("/api/routine/set-logs/",
                                {"user_exercise_progress": progress.pk, "set_number": 1,
                                 "weight": 40, "reps": 8}, content_type=JSON)
        assert sent.status_code == 201, sent.content
    first, second = ExerciseSetLog.objects.filter(
        user_exercise_progress=progress, set_number=1).order_by("id")

    whole = api(client).post("/api/routine/set-logs/bulk-create/", {
        "routine_id": routine.pk, "day": 1, "date": timezone.localdate().isoformat(),
        "sets": 2, "weight": 50, "reps": 9}, content_type=JSON)
    assert whole.status_code == 200, whole.content
    assert whole.json()["errors"] == [], whole.json()
    first.refresh_from_db()
    assert (first.weight, first.reps) == (50.0, 9), "the oldest row takes the correction"
    assert ExerciseSetLog.objects.filter(user_exercise_progress=progress,
                                         set_number=2).count() == 1


# ================================================== S-072 adherence without N+1
def test_s072_adherence_does_not_load_each_days_detail_in_its_own_query(make_user, api):
    """The view deferred `detail` and then serialised it anyway: one extra query per
    DayOutcome row, so the cost grew with the period."""
    from routine.models import DayOutcome

    def closed_days(tag, count):
        trainer, client, routine, rows = _program(make_user, tag, assign=False)
        today = timezone.localdate()
        for ago in range(count):
            DayOutcome.objects.create(
                user=client, routine=routine, day=1, date=today - timedelta(days=ago),
                via="completed", complete=True, planned_sets=3, logged_sets=3,
                detail=[{"routine_exercise_id": rows[0].pk, "planned_sets": 3}])
        return client

    one, many = closed_days("s072a", 1), closed_days("s072b", 6)
    small, small_sql = _queries(
        lambda: api(one).get("/api/routine/analytics/adherence/?period=month"))
    large, large_sql = _queries(
        lambda: api(many).get("/api/routine/analytics/adherence/?period=month"))
    assert small.status_code == large.status_code == 200, (small.content, large.content)
    assert len(large.json()["by_day"]) == 6
    assert all("detail" not in row for row in large.json()["by_day"])
    assert len(large_sql) == len(small_sql), \
        f"{len(large_sql)} queries for 6 days against {len(small_sql)} for 1"


# ============================================== S-110 a day that completed itself
def test_s110_a_day_that_completed_itself_is_not_completed_once_its_sets_are_deleted(
        make_user, api, django_capture_on_commit_callbacks):
    """RoutineProgress never moved down from `completed`, even for a day that completed
    ITSELF from its sets (`via='auto'`): after the set was deleted the outcome said
    short while `routine-progress/`, analytics/completion and the console said done."""
    from routine.models import DayOutcome, RoutineProgress

    def logged_and_deleted(tag, *, close_by_hand):
        trainer, client, routine, rows = _program(make_user, tag, sets=1)
        session = _session(client, routine)
        with django_capture_on_commit_callbacks(execute=True):
            sent = api(client).post("/api/routine/set-logs/", {
                "user_exercise_progress": _progress(client, rows[0]).pk,
                "workout_session": session.pk, "set_number": 1, "weight": 40,
                "reps": 8}, content_type=JSON)
        assert sent.status_code == 201, sent.content
        day = RoutineProgress.objects.get(user=client, routine=routine, day=1)
        assert day.status == "completed", "one set of a one-set day completes it"
        if close_by_hand:
            closed = api(client).post(f"/api/routine/routines/{routine.pk}/update_progress/",
                                      {"day": 1, "status": "completed"}, content_type=JSON)
            assert closed.status_code == 200, closed.content
        via = DayOutcome.objects.get(user=client, routine=routine, day=1).via
        with django_capture_on_commit_callbacks(execute=True):
            gone = api(client).delete(f"/api/routine/set-logs/{sent.json()['id']}/")
        assert gone.status_code == 204, gone.content
        day.refresh_from_db()
        outcome = DayOutcome.objects.get(user=client, routine=routine, day=1)
        return via, day.status, outcome.complete

    via, status, complete = logged_and_deleted("s110auto", close_by_hand=False)
    assert via == "auto"
    assert complete is False
    assert status != "completed", "nobody closed this day; its sets are gone"

    # A day the PERSON closed stays closed; only its outcome turns short.
    via, status, complete = logged_and_deleted("s110hand", close_by_hand=True)
    assert via == "completed"
    assert (status, complete) == ("completed", False)


# ================================================== S-111 the effort window
def test_s111_an_old_rating_behind_unrated_sessions_is_not_enough_data(make_user, api):
    """The documented rule is `not_enough_data` when none of the last EFFORT_SESSIONS
    sessions holding the exercise was rated; the code read the last three RATED sessions,
    so one rating from seven weeks ago still decided the suggestion today."""
    from tests.test_routine_extras import _progression, _train

    trainer, client, routine, rows = _program(make_user, "s111")
    exercise = rows[0].exercise
    _train(client, exercise, [(49, 60, 2), (21, 60, None), (14, 60, None), (7, 60, None)],
           routine=routine)

    answer = _progression(api, client, exercise.pk)
    assert answer["code"] == "not_enough_data", answer
    assert answer["effort"]["rated_sessions"] == 0, answer

    # The control: a rating among the last three is read.
    _train(client, exercise, [(1, 60, 2)], routine=routine)
    answer = _progression(api, client, exercise.pk)
    assert answer["effort"]["rated_sessions"] == 1, answer
    assert answer["code"] != "not_enough_data", answer


# ============================================ S-113 the next action, in Arabic
def test_s113_the_next_suggested_action_is_in_the_readers_language(make_user, api):
    """`next_suggested_action` was a hard-coded English sentence on every row."""
    trainer, client, routine, rows = _program(make_user, "s113")

    body = api(client).get("/api/routine/routine-progress/",
                           HTTP_ACCEPT_LANGUAGE="ar").json()
    (row,) = body["results"]
    assert row["next_suggested_action"] == "هل أنت مستعد؟ ابدأ تمرين اليوم 1 الآن.", row


# ========================================= S-114 a trainer logging a client's set
def test_s114_a_trainer_cannot_log_a_set_for_a_client(make_user, api):
    """The serializer let an approved trainer name a client's progress row, and the
    coaching refusal was asked about the trainer, who is never refused: a coach could
    fill in a client's day, frozen or not. Before the fix this answered 201."""
    from routine.models import ExerciseSetLog

    trainer, client, routine, rows = _program(make_user, "s114", coached=True)
    progress = _progress(client, rows[0])
    session = _session(client, routine)

    for extra in ({}, {"workout_session": session.pk}):
        refused = api(trainer).post("/api/routine/set-logs/", {
            "user_exercise_progress": progress.pk, "set_number": 1, "weight": 100,
            "reps": 5, **extra}, content_type=JSON)
        assert refused.status_code == 403, refused.content
        assert refused.json()["code"] == "set_not_yours", refused.json()
    assert not ExerciseSetLog.objects.filter(user_exercise_progress=progress).exists()

    mine = api(client).post("/api/routine/set-logs/", {
        "user_exercise_progress": progress.pk, "set_number": 1, "weight": 100,
        "reps": 5}, content_type=JSON)
    assert mine.status_code == 201, mine.content


# ================================================== S-115 a set of zero reps
def test_s115_a_set_of_zero_reps_is_refused_on_create_and_on_edit(make_user, api):
    """The single POST and PATCH took `reps=0` while bulk-create and sync require 1-1000;
    a set of nothing counted toward the day and could complete it."""
    from routine.models import ExerciseSetLog

    trainer, client, routine, rows = _program(make_user, "s115")
    progress = _progress(client, rows[0])

    zero = api(client).post("/api/routine/set-logs/", {
        "user_exercise_progress": progress.pk, "set_number": 1, "weight": 40,
        "reps": 0}, content_type=JSON)
    assert zero.status_code == 400, zero.content
    assert "reps" in zero.json()["field_errors"], zero.json()
    assert not ExerciseSetLog.objects.filter(user_exercise_progress=progress).exists()

    log = _set(progress)
    edited = api(client).patch(f"/api/routine/set-logs/{log.pk}/",
                               json.dumps({"reps": 0}), content_type=JSON)
    assert edited.status_code == 400, edited.content
    log.refresh_from_db()
    assert log.reps == 8


# ==================================================== S-116 a session's duration
def test_s116_an_abandoned_or_forgotten_session_has_no_duration(make_user, api):
    """`duration` was now - start for EVERY session without an end, so an abandoned or a
    forgotten workout reported lasting days, and the number grew for ever."""
    trainer, client, routine, rows = _program(make_user, "s116")
    now = timezone.now()
    abandoned = _session(client, routine, status="abandoned", began=now - timedelta(days=3))
    forgotten = _session(client, routine, began=now - timedelta(hours=13))
    running = _session(client, routine, began=now - timedelta(minutes=10))

    def duration(session):
        body = api(client).get(f"/api/routine/workout-sessions/{session.pk}/")
        assert body.status_code == 200, body.content
        return body.json()["duration"]

    assert duration(abandoned) is None
    assert duration(forgotten) is None
    assert 9 * 60 <= duration(running) <= 11 * 60


# ======================================================= S-120 a re-sent batch
def test_s120_a_resent_batch_recomputes_what_its_first_send_left_stale(
        make_user, api, django_capture_on_commit_callbacks):
    """`set-logs/sync/` commits each set on its own and recomputes at the end; a request
    that died in between left the sets written and uncounted, and the retry answered
    them `duplicate` with nothing to recompute, so they stayed uncounted."""
    from routine.models import UserExerciseProgress

    trainer, client, routine, rows = _program(make_user, "s120")
    batch = {"sets": [{"client_uuid": str(uuid.uuid4()), "exercise_id": rows[0].exercise_id,
                       "routine_id": routine.pk, "day": 1, "set_number": number,
                       "weight": 40, "reps": 8} for number in (1, 2)]}
    with django_capture_on_commit_callbacks(execute=True):
        first = api(client).post("/api/routine/set-logs/sync/", json.dumps(batch),
                                 content_type=JSON)
    assert first.status_code == 200 and first.json()["created"] == 2, first.content
    progress = UserExerciseProgress.objects.get(user=client, exercise=rows[0].exercise)
    assert progress.completed_sets == 2

    # The first request died after its writes and before its recount.
    UserExerciseProgress.objects.filter(pk=progress.pk).update(completed_sets=0,
                                                               total_repetitions=0)
    with django_capture_on_commit_callbacks(execute=True):
        again = api(client).post("/api/routine/set-logs/sync/", json.dumps(batch),
                                 content_type=JSON)
    assert again.status_code == 200 and again.json()["duplicates"] == 2, again.content
    progress.refresh_from_db()
    assert (progress.completed_sets, progress.total_repetitions) == (2, 16)


# ================================================ S-121 my-progress, newest first
def test_s121_my_progress_lists_the_newest_sets_first(make_user, api):
    """Paginated in the model's order ('set_number', 'id'), page 1 was every "set 1" the
    person ever logged, oldest first."""
    trainer, client, routine, rows = _program(make_user, "s121")
    today = timezone.localdate()
    old = _progress(client, rows[0], today - timedelta(days=5))
    _set(old, number=1)
    _set(old, number=2)
    newest = _set(_progress(client, rows[0], today), number=1)

    body = api(client).get("/api/routine/set-logs/my-progress/?page_size=2").json()
    assert body["count"] == 3, body
    assert body["results"][0]["id"] == newest.pk, body["results"]
    dates = [row["date"] for row in body["results"]]
    assert dates == sorted(dates, reverse=True), dates


# ======================================================= S-122 status transitions
def test_s122_a_finished_or_abandoned_session_stays_finished(make_user, api):
    """`status` was freely writable: an abandoned session finished days later was given
    a twelve-hour workout, and a completed one toggled to `active` and back ran the
    whole finish again (notifications twice)."""
    from routine.models import WorkoutSession

    trainer, client, routine, rows = _program(make_user, "s122")
    now = timezone.now()
    abandoned = _session(client, routine, status="abandoned", began=now - timedelta(days=2))

    revived = api(client).patch(f"/api/routine/workout-sessions/{abandoned.pk}/",
                                json.dumps({"status": "completed"}), content_type=JSON)
    assert revived.status_code == 400, revived.content
    assert "session_status_final" in _codes(revived.json()), revived.json()
    abandoned.refresh_from_db()
    assert (abandoned.status, abandoned.end_time) == ("abandoned", None)

    done = _session(client, routine, status="completed", began=now - timedelta(hours=2),
                    ended=now - timedelta(hours=1))
    reopened = api(client).patch(f"/api/routine/workout-sessions/{done.pk}/",
                                 json.dumps({"status": "active"}), content_type=JSON)
    assert reopened.status_code == 400, reopened.content
    assert "session_status_final" in _codes(reopened.json()), reopened.json()

    # What stays allowed: repeating the status it has, and rating it afterwards.
    rated = api(client).patch(f"/api/routine/workout-sessions/{done.pk}/",
                              json.dumps({"status": "completed", "effort_rating": 3}),
                              content_type=JSON)
    assert rated.status_code == 200, rated.content
    assert WorkoutSession.objects.get(pk=done.pk).status == "completed"


# ======================================================= S-123 completion trend
def test_s123_completion_trend_is_bucketed_by_the_training_date(make_user, api):
    """The completion trend truncated `updated_at` (auto_now), so a day's completion
    landed on whatever day its row was last written."""
    from routine.models import RoutineProgress

    trainer, client, routine, rows = _program(make_user, "s123", assign=False)
    trained = timezone.localdate() - timedelta(days=10)
    RoutineProgress.objects.create(user=client, routine=routine, day=1, date=trained,
                                   status="completed")

    body = api(client).get("/api/routine/analytics/trends/?period=day").json()
    assert body["completion_trend"] == [
        {"period": trained.isoformat(), "completed": 1, "total": 1}], body


# ============================================ S-124 the console's client dashboard
def test_s124_the_console_dashboard_pages_its_clients_and_loads_no_history(make_user, api):
    """It prefetched every RoutineProgress row of every client and never read one, and
    for an admin the client list was every client on the platform in one response."""
    from routine.models import RoutineProgress
    from users.models import TrainerClientRelation

    trainer, client, routine, rows = _program(make_user, "s124", coached=True)
    for index in range(2):
        other = make_user(f"fr_c_s124_{index}")
        TrainerClientRelation.objects.create(trainer=trainer, client=other,
                                             status="approved")
        routine.assigned_to.add(other)
    assert RoutineProgress.objects.filter(routine=routine).count() == 3

    response, sql = _queries(
        lambda: api(trainer).get("/api/routine/analytics/admin_dashboard/?page_size=2"))
    assert response.status_code == 200, response.content
    body = response.json()
    assert len(body["dashboard"]) == 2, body
    assert body["count"] >= 3 and body["next"], body
    loaded = [q for q in sql if '"routine_routineprogress"."completion_time"' in q]
    assert loaded == [], "a RoutineProgress row was loaded whole: the prefetch is back"

    rest = api(trainer).get(body["next"]).json()
    seen = {row["client_id"] for row in body["dashboard"] + rest["dashboard"]}
    assert len(seen) == min(body["count"], 4), (body, rest)


# ============================================== S-126 the coach's client screen
def test_s126_the_coach_screen_reads_routines_in_one_query_and_in_the_coachs_language(
        make_user, api):
    """`trainer/client-progress/{id}/` read `session.routine` per session (one query
    each), prefetched the client's whole RoutineProgress history and never read it, and
    answered the routine's default-language name to an Arabic coach."""
    from users.models import TrainerClientRelation

    trainer, client, routine, rows = _program(make_user, "s126", coached=True)
    trainer.preferred_language = "ar"
    trainer.save(update_fields=["preferred_language"])
    routine.translations = {"ar": {"name": "روتين القوة"}}
    routine.save(update_fields=["translations"])
    second = make_user("fr_c_s126_b")
    TrainerClientRelation.objects.create(trainer=trainer, client=second, status="approved")
    routine.assigned_to.add(second)

    now = timezone.now()

    def trained(user, count):
        for ago in range(count):
            began = now - timedelta(days=ago + 1)
            _session(user, routine, status="completed", began=began,
                     ended=began + timedelta(hours=1))

    trained(client, 1)
    trained(second, 4)

    def lookups(user):
        response, sql = _queries(
            lambda: api(trainer).get(f"/api/routine/trainer/client-progress/{user.pk}/"))
        assert response.status_code == 200, response.content
        per_row = [q for q in sql if re.search(
            r'FROM "routine_routine" WHERE "routine_routine"\."id" = ', q)]
        whole = [q for q in sql if '"routine_routineprogress"."completion_time"' in q]
        return response.json(), per_row, whole

    one, one_lookups, one_history = lookups(client)
    four, four_lookups, four_history = lookups(second)
    assert len(four["recent_activity"]) == 4
    assert {row["routine_name"] for row in four["recent_activity"]} == {"روتين القوة"}
    assert len(four_lookups) == len(one_lookups), \
        f"{len(four_lookups)} routine reads for 4 sessions, {len(one_lookups)} for 1"
    assert four_history == one_history == [], "the RoutineProgress history is not read"
