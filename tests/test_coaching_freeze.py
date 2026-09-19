"""The owner's freeze (2026-09-19): every client is reminded 3 days before a paid month
ends; an unpaid month freezes the coaching — the client still sees the trainer's
routines and cannot start them — and so does the end of a coaching. Paying again
resumes it. A trainer who still coaches someone can never delete the account.
See users/coaching.py.
"""
import uuid
from datetime import timedelta
from unittest import mock

import pytest
from django.utils import timezone

from tests.test_coaching_months import OWNER_RATE, _accepted, _end_month, _fund

pytestmark = pytest.mark.django_db

JSON = "application/json"


@pytest.fixture(autouse=True)
def owners_rules(settings):
    settings.COACHING_COMMISSION_RATE = OWNER_RATE
    settings.COACHING_PERIOD_DAYS = 30
    settings.COACHING_RENEWAL_REMINDER_DAYS = 3
    settings.COACHING_FROZEN_REMINDER_DAYS = (3, 7, 14, 30)


def _coached(make_user, api, tag, *, funds="100.00"):
    """A client whose month 1 was paid by their first program, a routine with one
    exercise, and that exercise's progress row for today."""
    from routine.models import Exercise, Routine, RoutineExercise, UserExerciseProgress

    trainer, client, relation = _accepted(make_user, api, tag, funds=funds)
    routine = Routine.objects.create(name=f"cf {tag}", created_by=trainer, days=1)
    exercise = Exercise.objects.create(name=f"cf ex {tag}", created_by=trainer)
    RoutineExercise.objects.create(routine=routine, exercise=exercise, day=1, sets=3, reps=10)
    assigned = api(trainer).post(f"/api/routine/routines/{routine.pk}/assign_to_client/",
                                 {"client_id": client.pk}, content_type=JSON)
    assert assigned.status_code == 200, assigned.content
    progress = UserExerciseProgress.objects.create(user=client, exercise=exercise,
                                                   date=timezone.localdate(), target_sets=3)
    return trainer, client, relation, routine, exercise, progress


def _freeze(relation, **ago):
    """The month ended `ago`, and the hourly run froze it minutes later, as it would
    have: the test calls `renew` now, so its stamp is moved back to that moment."""
    from users.coaching import renew

    first = _end_month(relation, **ago)
    with mock.patch("notifications.domain.dispatcher.emit_event"):
        assert renew(relation.pk) == "frozen"
    type(first).objects.filter(pk=first.pk).update(
        frozen_at=min(timezone.now(), first.ends_at + timedelta(minutes=5)))
    first.refresh_from_db()
    return first


def _training_writes(api, client, routine, exercise, progress):
    """Every write that starts or logs training, as (name, response)."""
    today = timezone.localdate().isoformat()
    c = api(client)
    return [
        ("session", c.post("/api/routine/workout-sessions/",
                           {"routine": routine.pk, "day": 1}, content_type=JSON)),
        ("set", c.post("/api/routine/set-logs/",
                       {"user_exercise_progress": progress.pk, "set_number": 1,
                        "weight": 50, "reps": 8}, content_type=JSON)),
        ("bulk_sets", c.post("/api/routine/set-logs/bulk-create/",
                             {"routine_id": routine.pk, "day": 1, "date": today, "sets": 1,
                              "weight": 50, "reps": 8}, content_type=JSON)),
        ("update_progress", c.post(f"/api/routine/routines/{routine.pk}/update_progress/",
                                   {"day": 1, "status": "in_progress"}, content_type=JSON)),
        ("routine_progress", c.post("/api/routine/routine-progress/",
                                    {"routine": routine.pk, "day": 1, "status": "in_progress"},
                                    content_type=JSON)),
        ("exercise_progress", c.post("/api/routine/user-exercise-progress/",
                                     {"exercise": exercise.pk,
                                      "date": (timezone.localdate() - timedelta(days=0)).isoformat(),
                                      "target_sets": 3}, content_type=JSON)),
        ("bulk_complete", c.post("/api/routine/user-exercise-progress/bulk-complete/",
                                 {"routine_id": routine.pk, "day": 1, "date": today,
                                  "completed_sets": 3}, content_type=JSON)),
    ]


# ------------------------------------------------------------------ the reminder
def test_every_client_is_told_three_days_before_the_month_ends(make_user, api,
                                                               django_capture_on_commit_callbacks):
    from users.coaching import remind_upcoming

    trainer, client, relation, *_ = _coached(make_user, api, "remind", funds="100.00")
    period = relation.periods.get()
    now = period.ends_at - timedelta(days=3) + timedelta(minutes=5)

    assert remind_upcoming(now=period.ends_at - timedelta(days=4)) == 0, "too early"
    with mock.patch("notifications.domain.dispatcher.emit_event") as sent, \
            django_capture_on_commit_callbacks(execute=True):
        assert remind_upcoming(now=now) == 1     # the wallet is empty: told anyway
        assert remind_upcoming(now=now + timedelta(hours=1)) == 0
    (event,) = [c.args[0] for c in sent.call_args_list]
    assert (type(event).__name__, event.recipient_id, event.days) == \
        ("CoachingRenewalSoonEvent", client.pk, 3)

    shown = api(client).get("/api/v1/coaching/current/").json()["coaching"]
    assert shown["renewal_reminder"] is False, "a month with 30 days left is not due"


def test_a_coaching_that_is_ending_gets_no_renewal_reminder(make_user, api):
    from users.coaching import remind_upcoming

    trainer, client, relation, *_ = _coached(make_user, api, "ending_rm")
    relation.ends_at_period_end = True
    relation.save(update_fields=["ends_at_period_end"])
    period = relation.periods.get()
    with mock.patch("notifications.domain.dispatcher.emit_event"):
        assert remind_upcoming(now=period.ends_at - timedelta(days=1)) == 0


def test_the_app_shows_the_reminder_in_the_last_three_days(make_user, api):
    trainer, client, relation, *_ = _coached(make_user, api, "remind_view", funds="1000.00")
    _end_month(relation, ago=-timedelta(days=2))    # ends in two days
    shown = api(client).get("/api/v1/coaching/current/").json()["coaching"]
    assert (shown["status"], shown["renewal_reminder"]) == ("active", True)


# --------------------------------------------------------------------- frozen
def test_a_frozen_client_sees_the_routine_and_cannot_train(make_user, api):
    trainer, client, relation, routine, exercise, progress = _coached(make_user, api, "frz")
    # Ended two days ago: training DATED the day a paid month ran is still accepted.
    _freeze(relation, ago=timedelta(days=2))

    for name, resp in _training_writes(api, client, routine, exercise, progress):
        assert resp.status_code == 403, (name, resp.content)
        body = resp.json()
        assert (body["code"], body["trainer_id"]) == ("coaching_frozen", trainer.pk), name

    c = api(client)
    for path in (f"/api/routine/routines/{routine.pk}/", "/api/routine/routines/",
                 "/api/routine/workout-sessions/", "/api/routine/set-logs/",
                 "/api/routine/user-exercise-progress/"):
        assert c.get(path).status_code == 200, path
    shown = c.get("/api/v1/coaching/current/").json()["coaching"]
    assert shown["status"] == "frozen"
    ios = c.get("/api/v1/coaching/current/", HTTP_X_APP_CHANNEL="ios").json()["coaching"]
    assert "coaching_price" not in ios


def test_training_done_while_paid_still_syncs_and_finishes(make_user, api):
    """The app logs offline. A session begun before the freeze can be finished, and a
    workout dated inside the paid month still syncs after it."""
    from routine.models import WorkoutSession

    trainer, client, relation, routine, exercise, progress = _coached(make_user, api, "sync")
    begun = api(client).post("/api/routine/workout-sessions/", {"routine": routine.pk, "day": 1},
                             content_type=JSON)
    assert begun.status_code == 201, begun.content
    first = _freeze(relation, ago=timedelta(hours=2))
    # Begun inside the paid month (the helper moved the month's end two hours back).
    WorkoutSession.objects.filter(pk=begun.json()["id"]).update(
        start_time=first.ends_at - timedelta(minutes=30))

    finished = api(client).patch(f"/api/routine/workout-sessions/{begun.json()['id']}/",
                                 {"status": "completed"}, content_type=JSON)
    assert finished.status_code == 200, finished.content

    offline = api(client).post("/api/routine/workout-sessions/", {
        "routine": routine.pk, "day": 1, "client_uuid": str(uuid.uuid4()),
        "start_time": (first.ends_at - timedelta(hours=3)).isoformat()}, content_type=JSON)
    assert offline.status_code == 201, offline.content
    late = api(client).post("/api/routine/workout-sessions/", {
        "routine": routine.pk, "day": 1,
        "start_time": (first.ends_at + timedelta(hours=1)).isoformat()}, content_type=JSON)
    assert (late.status_code, late.json()["code"]) == (403, "coaching_frozen")
    assert WorkoutSession.objects.filter(user=client).count() == 2

    # The rest of that workout, as the app syncs it: its progress row, dated the day
    # the paid month ran, and its sets, filed under the session begun while paid.
    progress.delete()
    row = api(client).post("/api/routine/user-exercise-progress/", {
        "exercise": exercise.pk, "date": timezone.localdate(first.ends_at).isoformat(),
        "target_sets": 3}, content_type=JSON)
    assert row.status_code == 201, row.content
    logged = api(client).post("/api/routine/set-logs/", {
        "user_exercise_progress": row.json()["id"], "workout_session": begun.json()["id"],
        "set_number": 1, "weight": 50, "reps": 8,
        "date": timezone.localdate(first.ends_at).isoformat()}, content_type=JSON)
    assert logged.status_code == 201, logged.content


def test_a_session_opened_before_the_renewal_ran_can_be_finished(make_user, api):
    """Between a month's end and the hourly renewal the coaching is still paid; a
    workout opened then is finished after the renewal froze it."""
    from users.coaching import renew

    trainer, client, relation, routine, exercise, progress = _coached(make_user, api, "window")
    _end_month(relation, ago=timedelta(minutes=30))
    opened = api(client).post("/api/routine/workout-sessions/", {"routine": routine.pk, "day": 1},
                              content_type=JSON)
    assert opened.status_code == 201, opened.content
    with mock.patch("notifications.domain.dispatcher.emit_event"):
        assert renew(relation.pk) == "frozen"
    done = api(client).patch(f"/api/routine/workout-sessions/{opened.json()['id']}/",
                             {"status": "completed"}, content_type=JSON)
    assert done.status_code == 200, done.content


def test_an_undated_set_is_judged_on_the_day_it_is_stored(make_user, api):
    """A set without a date is stored today; judging it by an older progress row's date
    let a frozen client log today's training."""
    from routine.models import UserExerciseProgress

    trainer, client, relation, routine, exercise, progress = _coached(make_user, api, "undated")
    first = _freeze(relation, ago=timedelta(days=2))
    UserExerciseProgress.objects.filter(pk=progress.pk).update(
        date=timezone.localdate(first.ends_at) - timedelta(days=1))
    resp = api(client).post("/api/routine/set-logs/", {
        "user_exercise_progress": progress.pk, "set_number": 1, "weight": 50, "reps": 8},
        content_type=JSON)
    assert (resp.status_code, resp.json()["code"]) == (403, "coaching_frozen")


def test_a_month_support_returned_is_not_called_ended(make_user, api):
    from users.coaching import training_refusal

    trainer, client, relation, routine, *_ = _coached(make_user, api, "refunded")
    _end_month(relation, ago=timedelta(days=2))
    relation.periods.update(status="refunded")
    assert training_refusal(client, routine) == "coaching_unpaid"


def test_an_idle_frozen_coaching_is_not_locked_every_hour(make_user, api):
    from users.coaching import due_relation_ids

    trainer, client, relation, *_ = _coached(make_user, api, "idle")
    first = _freeze(relation, ago=timedelta(hours=1))
    assert relation.pk not in due_relation_ids(), "nothing to do: no money, no reminder due"
    assert relation.pk in due_relation_ids(now=first.frozen_at + timedelta(days=3, hours=1))
    _fund(client, "100.00")
    assert relation.pk in due_relation_ids()


def test_paying_again_resumes_training_from_the_payment(make_user, api):
    from users.coaching import renew

    trainer, client, relation, routine, exercise, progress = _coached(make_user, api, "resume")
    _freeze(relation, ago=timedelta(days=3))
    _fund(client, "100.00")
    with mock.patch("notifications.domain.dispatcher.emit_event"):
        assert renew(relation.pk) == "renewed"
    second = relation.periods.get(number=2)
    assert abs((second.starts_at - timezone.now()).total_seconds()) < 60
    resp = api(client).post("/api/routine/workout-sessions/", {"routine": routine.pk, "day": 1},
                            content_type=JSON)
    assert resp.status_code == 201, resp.content
    assert api(client).get("/api/v1/coaching/current/").json()["coaching"]["status"] == "active"


def test_the_trainers_scheduled_end_also_ends_a_frozen_coaching(make_user, api):
    from users.coaching import renew
    from users.models import TrainerClientRelation

    trainer, client, relation, *_ = _coached(make_user, api, "sched_frz")
    _freeze(relation)
    TrainerClientRelation.objects.filter(pk=relation.pk).update(ends_at_period_end=True)
    with mock.patch("notifications.domain.dispatcher.emit_event"):
        assert renew(relation.pk) == "ended"
    assert not TrainerClientRelation.objects.filter(pk=relation.pk).exists()


def test_after_the_coaching_ends_the_routine_is_readable_not_trainable(make_user, api):
    """Leaving used to keep the routines trainable for ever: the freeze could be escaped
    by ending the coaching."""
    trainer, client, relation, routine, exercise, progress = _coached(make_user, api, "left")
    _freeze(relation, ago=timedelta(days=2))
    left = api(client).post("/api/auth/client/unassign-trainer/", {}, content_type=JSON)
    assert left.status_code == 200, left.content

    for name, resp in _training_writes(api, client, routine, exercise, progress):
        assert (resp.status_code, resp.json().get("code")) == (403, "coaching_ended"), \
            (name, resp.content)
    assert api(client).get(f"/api/routine/routines/{routine.pk}/").status_code == 200


def test_a_routine_from_before_paid_months_is_not_affected(make_user, api):
    """No paid month for the pair (free, legacy, or data from before months existed):
    nothing to freeze."""
    from routine.models import Routine

    from routine.models import Exercise, RoutineExercise

    trainer = make_user("cf_legacy_t", user_type="trainer")
    client = make_user("cf_legacy_c")
    routine = Routine.objects.create(name="legacy", created_by=trainer, days=1)
    RoutineExercise.objects.create(routine=routine, day=1, sets=3, reps=5,
                                   exercise=Exercise.objects.create(name="cf legacy ex",
                                                                    created_by=trainer))
    routine.assigned_to.add(client)
    resp = api(client).post("/api/routine/workout-sessions/", {"routine": routine.pk, "day": 1},
                            content_type=JSON)
    assert resp.status_code == 201, resp.content


def test_a_trainer_may_still_prepare_work_for_a_frozen_client(make_user, api):
    trainer, client, relation, routine, *_ = _coached(make_user, api, "prep")
    _freeze(relation)
    edited = api(trainer).patch(f"/api/routine/routines/{routine.pk}/", {"name": "renamed"},
                                content_type=JSON)
    assert edited.status_code == 200, edited.content


# ------------------------------------------------------- a trainer's own account
def test_a_trainer_with_a_running_month_is_refused_until_it_ends(make_user, api):
    from users.coaching import renew

    trainer, client, relation, *_ = _coached(make_user, api, "erase_run", funds="1000.00")
    refused = api(trainer).delete("/api/privacy/erase/?confirm=ERASE")
    assert (refused.status_code, refused.json()["code"]) == (409, "trainer_has_active_clients")

    scheduled = api(trainer).post("/api/auth/trainer/unassign-client/",
                                  {"client_id": client.pk, "at_period_end": True},
                                  content_type=JSON)
    assert scheduled.status_code == 200, scheduled.content
    assert api(trainer).delete("/api/privacy/erase/?confirm=ERASE").status_code == 409

    _end_month(relation)
    with mock.patch("notifications.domain.dispatcher.emit_event"):
        assert renew(relation.pk) == "ended"
    assert api(trainer).delete("/api/privacy/erase/?confirm=ERASE").status_code == 200


def test_a_trainer_ends_a_frozen_coaching_and_can_then_leave(make_user, api):
    trainer, client, relation, *_ = _coached(make_user, api, "erase_frz")
    _freeze(relation)
    assert api(trainer).delete("/api/privacy/erase/?confirm=ERASE").status_code == 409
    ended = api(trainer).post("/api/auth/trainer/unassign-client/", {"client_id": client.pk},
                              content_type=JSON)
    assert ended.status_code == 200, ended.content
    assert api(trainer).delete("/api/privacy/erase/?confirm=ERASE").status_code == 200


def test_pending_requests_do_not_stop_a_trainer_leaving(make_user, api):
    from users.models import TrainerClientRelation

    trainer = make_user("cf_pending_t", user_type="trainer")
    TrainerClientRelation.objects.create(trainer=trainer, client=make_user("cf_pending_c"),
                                         status="pending")
    assert api(trainer).delete("/api/privacy/erase/?confirm=ERASE").status_code == 200


def test_a_trainer_cannot_be_hard_deleted_from_the_admin():
    from django.contrib.admin.sites import site

    from users.admin import CustomUserAdmin
    from users.models import CustomUser

    assert CustomUserAdmin(CustomUser, site).has_delete_permission(None) is False
