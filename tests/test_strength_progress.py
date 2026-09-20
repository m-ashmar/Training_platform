"""Getting stronger: the trend, the projection, the record, and the coach who notices.

The owner (2026-09-20): show a person where their training is heading, honestly, and let
the coach see who is slipping. Read from sets already logged and from the day outcomes;
nothing new is collected. See routine/strength.py and routine/outcomes.py.
"""
from datetime import timedelta

import pytest
from django.utils import timezone

pytestmark = pytest.mark.django_db

JSON = "application/json"


def _lift(user, exercise, weeks, *, sets=3, reps=8, today=None):
    """One training day per week, `weeks` = [weight, ...] oldest first."""
    from routine.models import ExerciseSetLog, UserExerciseProgress

    today = today or timezone.localdate()
    for index, weight in enumerate(weeks):
        date = today - timedelta(days=(len(weeks) - 1 - index) * 7)
        progress = UserExerciseProgress.objects.create(user=user, exercise=exercise,
                                                       date=date, target_sets=sets)
        for number in range(1, sets + 1):
            ExerciseSetLog.objects.create(user_exercise_progress=progress, set_number=number,
                                          weight=weight, reps=reps, date=date)


def _exercise(trainer, name):
    from routine.models import Exercise

    return Exercise.objects.create(name=name, created_by=trainer)


# ------------------------------------------------------------------ the trend
def test_the_trend_reads_the_sets_and_says_which_way_they_go(make_user, api):
    trainer = make_user("sp_t_trend", user_type="trainer")
    client = make_user("sp_c_trend")
    bench = _exercise(trainer, "sp bench")
    _lift(client, bench, [60, 62, 64, 66, 68, 70])

    body = api(client).get(f"/api/routine/analytics/strength/?exercise_id={bench.pk}").json()
    assert body["direction"] == "rising"
    assert body["sessions"] == 6
    assert body["change_percent"] == pytest.approx(16.6, abs=0.2)
    assert body["best"]["weight"] == 70.0 and body["best"]["reps"] == 8
    assert len(body["points"]) == 6
    assert body["points"][0]["estimated_1rm"] < body["points"][-1]["estimated_1rm"]
    assert body["share"]["kind"] == "rising_with_projection"


def test_a_projection_is_a_range_and_says_what_it_assumes(make_user, api):
    trainer = make_user("sp_t_proj", user_type="trainer")
    client = make_user("sp_c_proj")
    press = _exercise(trainer, "sp press")
    _lift(client, press, [40, 42, 44, 46, 48])

    body = api(client).get(f"/api/routine/analytics/strength/?exercise_id={press.pk}").json()
    projection = body["projection"]
    assert projection["available"] is True
    assert projection["horizon_days"] == 14
    assert projection["low"] < projection["high"], "a range, never one number"
    assert projection["low"] >= projection["from_estimated_1rm"], "a rising trend"
    assert projection["confidence"] in ("low", "medium", "high")
    assert "assumes_sets_done_percent" in projection


def test_too_little_training_says_so_instead_of_guessing(make_user, api):
    trainer = make_user("sp_t_thin", user_type="trainer")
    client = make_user("sp_c_thin")
    row = _exercise(trainer, "sp row")
    _lift(client, row, [50, 52])        # two days, one week apart

    body = api(client).get(f"/api/routine/analytics/strength/?exercise_id={row.pk}").json()
    assert body["projection"] == {"available": False, "reason": "insufficient_data",
                                  "needs": {"sessions": 1, "days": 7}, "sessions": 2}
    assert body["share"]["kind"] in ("rising", "holding")


def test_a_falling_trend_is_not_dressed_up_as_a_prediction(make_user, api):
    trainer = make_user("sp_t_down", user_type="trainer")
    client = make_user("sp_c_down")
    squat = _exercise(trainer, "sp squat")
    _lift(client, squat, [80, 78, 76, 74, 72])

    body = api(client).get(f"/api/routine/analytics/strength/?exercise_id={squat.pk}").json()
    assert body["direction"] == "easing"
    projection = body["projection"]
    assert projection["available"] is True and projection["direction"] == "easing"
    assert projection["high"] >= projection["from_estimated_1rm"], \
        "the range holds around where they are; it never promises a decline"
    assert body["share"]["kind"] == "holding"


def test_the_overview_ranks_what_moved(make_user, api):
    trainer = make_user("sp_t_many", user_type="trainer")
    client = make_user("sp_c_many")
    rising = _exercise(trainer, "sp rising")
    flat = _exercise(trainer, "sp flat")
    _lift(client, rising, [50, 55, 60, 65])
    _lift(client, flat, [30, 30, 30, 30])

    body = api(client).get("/api/routine/analytics/strength/").json()
    assert body["tracked_count"] == 2 and body["rising_count"] == 1
    assert body["exercises"][0]["exercise_name"] == "sp rising"
    assert [row["exercise_name"] for row in body["top_movers"]] == ["sp rising"]
    assert [row["exercise_name"] for row in body["plateaus"]] == ["sp flat"]
    assert body["headline"]["kind"].startswith("rising")
    assert "points" not in body["exercises"][0], "the day series belongs to one exercise"


def test_a_time_or_bodyweight_exercise_is_left_out_of_strength(make_user, api):
    from routine.models import ExerciseSetLog, UserExerciseProgress

    trainer = make_user("sp_t_run", user_type="trainer")
    client = make_user("sp_c_run")
    run = _exercise(trainer, "sp treadmill")
    progress = UserExerciseProgress.objects.create(user=client, exercise=run,
                                                   date=timezone.localdate(), target_sets=1)
    ExerciseSetLog.objects.create(user_exercise_progress=progress, set_number=1,
                                  weight=0, reps=1, date=timezone.localdate())
    body = api(client).get("/api/routine/analytics/strength/").json()
    assert body["tracked_count"] == 0 and body["headline"]["kind"] == "no_data"


def test_only_the_person_their_trainer_or_an_admin_may_look(make_user, api):
    from users.models import TrainerClientRelation

    trainer = make_user("sp_t_priv", user_type="trainer")
    client = make_user("sp_c_priv")
    stranger = make_user("sp_stranger", user_type="trainer")
    bench = _exercise(trainer, "sp private bench")
    _lift(client, bench, [60, 65, 70])
    TrainerClientRelation.objects.create(trainer=trainer, client=client, status="approved")

    assert api(trainer).get(
        f"/api/routine/analytics/strength/?user_id={client.pk}").status_code == 200
    assert api(stranger).get(
        f"/api/routine/analytics/strength/?user_id={client.pk}").status_code == 403
    assert api(client).get("/api/routine/analytics/strength/?user_id=999999").status_code == 403


# ------------------------------------------------------------------ the record
def test_beating_your_best_is_announced_once(make_user, api,
                                             django_capture_on_commit_callbacks):
    from unittest import mock

    from routine.models import UserExerciseProgress

    trainer = make_user("sp_t_pr", user_type="trainer")
    client = make_user("sp_c_pr")
    bench = _exercise(trainer, "sp pr bench")
    # A history to beat, ending a week ago so today's progress row is free.
    _lift(client, bench, [60, 62], today=timezone.localdate() - timedelta(days=7))

    progress = UserExerciseProgress.objects.create(user=client, exercise=bench,
                                                   date=timezone.localdate(), target_sets=3)
    with mock.patch("notifications.domain.dispatcher.emit_event") as sent, \
            django_capture_on_commit_callbacks(execute=True):
        beaten = api(client).post("/api/routine/set-logs/", {
            "user_exercise_progress": progress.pk, "set_number": 1,
            "weight": 80, "reps": 8}, content_type=JSON)
    assert beaten.status_code == 201, beaten.content
    assert beaten.json()["personal_record"] is True
    (event,) = [call.args[0] for call in sent.call_args_list]
    assert type(event).__name__ == "PersonalRecordEvent"
    assert (event.recipient_id, event.exercise_id, event.weight) == (client.pk, bench.pk, "80")

    with mock.patch("notifications.domain.dispatcher.emit_event") as again, \
            django_capture_on_commit_callbacks(execute=True):
        lighter = api(client).post("/api/routine/set-logs/", {
            "user_exercise_progress": progress.pk, "set_number": 2,
            "weight": 70, "reps": 8}, content_type=JSON)
    assert lighter.json()["personal_record"] is False
    assert not again.call_args_list, "only beating the best is news"


def test_the_record_reaches_the_inbox(make_user, api, django_capture_on_commit_callbacks):
    """Not just emitted: a listener must write it, or the push exists only in a mock."""
    from notifications.models import Notification
    from routine.models import UserExerciseProgress

    trainer = make_user("sp_t_inbox", user_type="trainer")
    client = make_user("sp_c_inbox")
    bench = _exercise(trainer, "sp inbox bench")
    _lift(client, bench, [50, 55], today=timezone.localdate() - timedelta(days=7))

    progress = UserExerciseProgress.objects.create(user=client, exercise=bench,
                                                   date=timezone.localdate(), target_sets=1)
    with django_capture_on_commit_callbacks(execute=True):
        beaten = api(client).post("/api/routine/set-logs/", {
            "user_exercise_progress": progress.pk, "set_number": 1,
            "weight": 75, "reps": 5}, content_type=JSON)
    assert beaten.status_code == 201, beaten.content
    notice = Notification.objects.get(recipient=client, event_type="personal_record")
    assert notice.metadata["context"]["exercise"] == "sp inbox bench"
    assert notice.metadata["data"]["type"] == "personal_record"
    assert notice.related_object_id == str(beaten.json()["id"])


def test_the_first_set_of_an_exercise_is_not_a_record(make_user, api,
                                                      django_capture_on_commit_callbacks):
    from unittest import mock

    from routine.models import UserExerciseProgress

    trainer = make_user("sp_t_first", user_type="trainer")
    client = make_user("sp_c_first")
    curl = _exercise(trainer, "sp curl")
    progress = UserExerciseProgress.objects.create(user=client, exercise=curl,
                                                   date=timezone.localdate(), target_sets=1)
    with mock.patch("notifications.domain.dispatcher.emit_event") as sent, \
            django_capture_on_commit_callbacks(execute=True):
        first = api(client).post("/api/routine/set-logs/", {
            "user_exercise_progress": progress.pk, "set_number": 1,
            "weight": 20, "reps": 10}, content_type=JSON)
    assert first.status_code == 201 and first.json()["personal_record"] is False
    assert not sent.call_args_list


# ------------------------------------------------------------------ the coach
def _coached(make_user, api, tag, *, sets=4, reps=10):
    """An approved client with a routine of one exercise, and their trainer."""
    from routine.models import Routine, RoutineExercise
    from users.models import TrainerClientRelation

    trainer = make_user(f"sp_t_{tag}", user_type="trainer")
    client = make_user(f"sp_c_{tag}")
    TrainerClientRelation.objects.create(trainer=trainer, client=client, status="approved")
    routine = Routine.objects.create(name=f"sp {tag}", created_by=trainer, days=1)
    exercise = _exercise(trainer, f"sp ex {tag}")
    RoutineExercise.objects.create(routine=routine, exercise=exercise, day=1, order=1,
                                   sets=sets, reps=reps)
    routine.assigned_to.add(client)
    return trainer, client, routine, exercise


def test_the_coach_sees_who_needs_a_look(make_user, api):
    from routine.outcomes import record_day

    trainer, quiet_client, routine, exercise = _coached(make_user, api, "quiet")
    # A second client of the same trainer who trained today with nothing missing.
    from routine.models import ExerciseSetLog, Routine, RoutineExercise, UserExerciseProgress
    from users.models import TrainerClientRelation
    fine_client = make_user("sp_c_fine")
    TrainerClientRelation.objects.create(trainer=trainer, client=fine_client, status="approved")
    fine_routine = Routine.objects.create(name="sp fine", created_by=trainer, days=1)
    RoutineExercise.objects.create(routine=fine_routine, exercise=exercise, day=1, order=1,
                                   sets=2, reps=10)
    fine_routine.assigned_to.add(fine_client)
    today = timezone.localdate()
    progress = UserExerciseProgress.objects.create(user=fine_client, exercise=exercise,
                                                   date=today, target_sets=2)
    for number in (1, 2):
        ExerciseSetLog.objects.create(user_exercise_progress=progress, set_number=number,
                                      weight=30, reps=10, date=today)
    record_day(fine_client, fine_routine, 1, today, via='completed')

    # The quiet one closed a day short a fortnight ago and has not trained since.
    long_ago = today - timedelta(days=14)
    record_day(quiet_client, routine, 1, long_ago, via='confirmed_unlogged',
               confirmed_unlogged=True)

    body = api(trainer).get("/api/routine/trainer/client-progress/attention/").json()
    listed = {row["client_id"]: row for row in body["clients"]}
    assert quiet_client.pk in listed, "a client who stopped must surface"
    assert fine_client.pk not in listed, "a client with nothing to report is not noise"
    reasons = {reason["code"] for reason in listed[quiet_client.pk]["reasons"]}
    assert "quiet" in reasons
    assert listed[quiet_client.pk]["client_name"]

    stranger = make_user("sp_other_t", user_type="trainer")
    other = api(stranger).get("/api/routine/trainer/client-progress/attention/").json()
    assert other["count"] == 0, "a trainer sees only their own clients"
    assert api(quiet_client).get(
        "/api/routine/trainer/client-progress/attention/").status_code == 403


def test_a_frozen_coaching_is_flagged_to_the_coach(make_user, api):
    """A real frozen month, not a stubbed rule: the coach's list reads the same coaching
    state the training refusals do (users/coaching.py `refusals_for`)."""
    import uuid

    from users.models import CoachingPeriod

    trainer, client, routine, exercise = _coached(make_user, api, "frozenflag")
    now = timezone.now()
    CoachingPeriod.objects.create(
        trainer=trainer, client=client, number=1, price="100.00", commission_rate="0.20",
        status="settled", hold_reference=str(uuid.uuid4()),
        starts_at=now - timedelta(days=40), ends_at=now - timedelta(days=10),
        frozen_at=now - timedelta(days=10))

    body = api(trainer).get("/api/routine/trainer/client-progress/attention/").json()
    mine = [row for row in body["clients"] if row["client_id"] == client.pk]
    assert mine, "a frozen client must surface"
    assert {reason["code"] for reason in mine[0]["reasons"]} == {"frozen"}, \
        "the platform refuses their training; they are not also 'slipping'"


def test_the_coach_screens_carry_the_trend(make_user, api):
    trainer, client, routine, exercise = _coached(make_user, api, "coachtrend")
    _lift(client, exercise, [40, 45, 50, 55])

    one = api(trainer).get(f"/api/routine/trainer/client-progress/{client.pk}/").json()
    assert one["strength"]["rising_count"] == 1
    assert one["strength"]["headline"]["kind"].startswith("rising")

    roster = api(trainer).get("/api/routine/trainer/client-progress/recent/").json()
    mine = [row for row in roster["recent_activity"]
            if row["client_info"]["id"] == client.pk]
    assert mine and mine[0]["this_week"]["trend"]["rising"] == 1


def test_the_home_week_carries_the_headline(make_user, api):
    from training_platform.cache import private_cache

    trainer = make_user("sp_t_home", user_type="trainer")
    client = make_user("sp_c_home")
    bench = _exercise(trainer, "sp home bench")
    _lift(client, bench, [50, 55, 60, 65])
    private_cache().delete(f"recent_progress:{client.pk}")

    body = api(client).get("/api/routine/v1/analytics/recent-progress/").json()
    assert body["strength"]["tracked_count"] == 1
    assert body["strength"]["headline"]["exercise_name"] == "sp home bench"


# ------------------------------------------------------------------ the week
def test_the_week_is_told_to_both_of_them(make_user, api):
    from notifications.models import Notification
    from routine.outcomes import record_day
    from routine.tasks import send_weekly_progress

    trainer, client, routine, exercise = _coached(make_user, api, "weekly", sets=2)
    from routine.models import ExerciseSetLog, UserExerciseProgress
    today = timezone.localdate()
    progress = UserExerciseProgress.objects.create(user=client, exercise=exercise,
                                                   date=today, target_sets=2)
    ExerciseSetLog.objects.create(user_exercise_progress=progress, set_number=1,
                                  weight=30, reps=10, date=today)
    record_day(client, routine, 1, today, via='confirmed_unlogged', confirmed_unlogged=True)

    result = send_weekly_progress()
    assert result["clients"] == 1 and result["trainers"] == 1

    recap = Notification.objects.get(recipient=client, event_type="weekly_recap")
    assert recap.metadata["context"] == {"logged": 1, "planned": 2, "perfect": 0}
    digest = Notification.objects.get(recipient=trainer, event_type="trainer_weekly_digest")
    assert digest.metadata["context"]["clients"] == 1
    assert digest.metadata["context"]["trained"] == 1

    # Twice in one week changes nothing.
    send_weekly_progress()
    assert Notification.objects.filter(recipient=client, event_type="weekly_recap").count() == 1


def test_a_paused_coaching_gets_no_recap(make_user, api):
    from unittest import mock

    from notifications.models import Notification
    from routine.outcomes import record_day
    from routine.tasks import send_weekly_progress

    trainer, client, routine, exercise = _coached(make_user, api, "weekfrozen", sets=2)
    record_day(client, routine, 1, timezone.localdate(), via='confirmed_unlogged',
               confirmed_unlogged=True)
    with mock.patch("users.coaching.training_refusal", return_value="coaching_frozen"):
        result = send_weekly_progress()
    assert result["clients"] == 0
    assert not Notification.objects.filter(recipient=client, event_type="weekly_recap").exists()


def test_the_weekly_texts_carry_no_price_and_no_claim():
    from django.utils import translation

    from notifications.channels.fcm import _resolve_event_template

    for event_type, context in (
            ("personal_record", {"exercise": "Bench", "weight": "80", "reps": 8}),
            ("weekly_recap", {"logged": 18, "planned": 24, "perfect": 3}),
            ("trainer_weekly_digest", {"clients": 5, "trained": 4, "attention": 2})):
        template = _resolve_event_template(event_type)
        assert template is not None, event_type
        for language in ("en", "ar"):
            with translation.override(language):
                text = f"{template.title} {template.body % context}"
            for forbidden in ("http", "SYP", "calorie", "سعرة", "muscle", "fat"):
                assert forbidden not in text, (event_type, language, text)
