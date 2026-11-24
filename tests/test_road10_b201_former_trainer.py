"""BACKEND_BUGS B-201, the manager's question (2026-11-24): what a FORMER trainer gets once
the coaching is gone — after `users.coaching.end_coaching`, and after the trainer's own
`POST /api/auth/trainer/unassign-client/` (both delete the relation). B-201 kept the
training record readable for a PAUSED coaching (the relation is still approved); this
proves it closes when the relation goes:

  every training-record read     refused, or the client's rows absent
  a programme write for them     refused for a NEW routine, `assign_to_client` and
                                 template `generate`; the author's edits of the routine
                                 already assigned to them still answer (xfail below,
                                 awaiting the manager's ruling)
  the chat thread                still readable by both, as their own conversation

The routines the trainer wrote for the client stay the client's (`routine/assignment.py`:
a routine is one client's private copy) and stay assigned to them.
"""
from datetime import timedelta

import pytest
from django.utils import timezone

from tests.test_coaching_freeze import _coached
from tests.test_coaching_months import _end_month

pytestmark = pytest.mark.django_db

JSON = "application/json"
R = "/api/routine/"


@pytest.fixture(autouse=True)
def coaching_rules(settings):
    settings.COACHING_RENEWAL_REMINDER_DAYS = 3
    settings.COACHING_FROZEN_REMINDER_DAYS = (3, 7, 14, 30)


@pytest.fixture(autouse=True)
def a_cold_cache():
    from training_platform.cache import private_cache

    private_cache().clear()
    yield
    private_cache().clear()


def _rows(resp):
    body = resp.json()
    if isinstance(body, dict):
        for key in ("results", "dashboard", "conversations", "messages"):
            if key in body:
                return body[key]
    return body


def _world(make_user, api, tag):
    """A coaching that trained: a session with a set, a template, a chat thread."""
    from chat.models import Message
    from chat.services import open_conversation
    from routine.models import (ExerciseSetLog, RoutineTemplate, RoutineTemplateExercise,
                                WorkoutSession)

    trainer, client, relation, routine, exercise, progress = _coached(make_user, api, tag)
    session = WorkoutSession.objects.create(user=client, routine=routine, day=1,
                                            status="completed", start_time=timezone.now(),
                                            end_time=timezone.now())
    log = ExerciseSetLog.objects.create(user_exercise_progress=progress, set_number=1,
                                        reps=10, weight=40, date=timezone.localdate(),
                                        workout_session=session)
    template = RoutineTemplate.objects.create(name=f"fmr {tag}", goal="strength",
                                              created_by=trainer, days=1)
    RoutineTemplateExercise.objects.create(template=template, exercise=exercise, day=1)
    conversation = open_conversation(trainer, client)
    said = Message.objects.create(conversation=conversation, sender=trainer, kind="text",
                                  body="See you Monday")
    return dict(trainer=trainer, client=client, relation=relation, routine=routine,
                exercise=exercise, progress=progress, session=session, log=log,
                template=template, conversation=conversation, said=said)


def _end(api, w, how):
    from django.db import transaction

    from users.coaching import end_coaching
    from users.models import TrainerClientRelation

    if how == "end_coaching":
        with transaction.atomic():
            end_coaching(TrainerClientRelation.objects.select_for_update()
                         .get(pk=w["relation"].pk), reason="unpaid")
    else:
        _end_month(w["relation"], ago=timedelta(days=2))
        resp = api(w["trainer"]).post("/api/auth/trainer/unassign-client/",
                                      {"client_id": w["client"].pk}, content_type=JSON)
        assert resp.status_code == 200, resp.content
    assert not TrainerClientRelation.objects.filter(trainer=w["trainer"],
                                                    client=w["client"]).exists()


def _reads(api, w):
    """(route, refused?) for every training-record read, as the former trainer."""
    t, client = api(w["trainer"]), w["client"]
    me = client.pk
    out = []
    for name in ("summary", "completion", "streaks", "adherence", "trends"):
        resp = t.get(f"{R}analytics/{name}/?user_id={me}")
        out.append((f"analytics/{name}/?user_id=", resp.status_code in (403, 404)))
    resp = t.get(f"{R}analytics/strength/?user_id={me}&exercise_id={w['exercise'].pk}")
    out.append(("analytics/strength/?user_id=", resp.status_code in (403, 404)))
    resp = t.get(f"{R}analytics/admin_dashboard/?user_id={me}&page_size=100")
    out.append(("analytics/admin_dashboard/",
                me not in [row["client_id"] for row in _rows(resp)]))
    sessions = [row["id"] for row in _rows(t.get(f"{R}workout-sessions/?page_size=100"))]
    out.append(("workout-sessions/", w["session"].pk not in sessions))
    out.append(("workout-sessions/?user=",
                _rows(t.get(f"{R}workout-sessions/?user={me}")) == []))
    resp = t.get(f"{R}workout-sessions/{w['session'].pk}/?expand=sets")
    out.append(("workout-sessions/{id}/?expand=sets", resp.status_code in (403, 404)))
    logs = [row["id"] for row in _rows(t.get(f"{R}set-logs/?page_size=100"))]
    out.append(("set-logs/", w["log"].pk not in logs))
    progress = [row["id"] for row in _rows(t.get(f"{R}user-exercise-progress/?page_size=100"))]
    out.append(("user-exercise-progress/", w["progress"].pk not in progress))
    resp = t.get(f"{R}trainer/client-progress/{me}/")
    out.append(("trainer/client-progress/{id}/", resp.status_code in (403, 404)))
    rows = _rows(t.get(f"{R}routine-progress/?user={me}"))
    out.append(("routine-progress/?user=", rows == []))
    return out


def _writes(api, w):
    """(route, refused?) for every programme write for that client."""
    from routine.models import Routine, RoutineExercise

    t, client, routine = api(w["trainer"]), w["client"], w["routine"]
    out = []
    created = t.post(f"{R}routines/", {"name": "for a former client", "days": 1,
                                       "assigned_to": [client.pk]}, content_type=JSON)
    out.append(("POST routines/ assigned_to", created.status_code >= 400
                and not Routine.objects.filter(name="for a former client").exists()))
    spare = Routine.objects.create(name="spare", created_by=w["trainer"], days=1)
    RoutineExercise.objects.create(routine=spare, exercise=w["exercise"], day=1, sets=3,
                                   reps=10)
    assigned = t.post(f"{R}routines/{spare.pk}/assign_to_client/",
                      {"client_id": client.pk}, content_type=JSON)
    out.append(("routines/{id}/assign_to_client/", assigned.status_code >= 400
                and not spare.assigned_to.filter(pk=client.pk).exists()))
    generated = t.post(f"{R}templates/{w['template'].pk}/generate/",
                       {"client_id": client.pk, "assign": True}, content_type=JSON)
    out.append(("templates/{id}/generate/ client_id", generated.status_code >= 400))
    renamed = t.patch(f"{R}routines/{routine.pk}/", {"name": "edited after the end"},
                      content_type=JSON)
    out.append(("PATCH routines/{id}/ (the client's)", renamed.status_code >= 400))
    added = t.post(f"{R}routine-exercises/", {"routine": routine.pk,
                                              "exercise_id": w["exercise"].pk, "day": 1,
                                              "order": 2, "sets": 3, "reps": 10},
                   content_type=JSON)
    out.append(("POST routine-exercises/ (the client's routine)", added.status_code >= 400))
    row = routine.routine_exercises.first()
    changed = t.patch(f"{R}routine-exercises/{row.pk}/", {"sets": 5}, content_type=JSON)
    out.append(("PATCH routine-exercises/{id}/ (the client's routine)",
                changed.status_code >= 400))
    Routine.objects.filter(pk=routine.pk).update(days=14)
    week = t.post(f"{R}routines/{routine.pk}/copy_week/", {"from_week": 1, "to_week": 2},
                  content_type=JSON)
    out.append(("POST routines/{id}/copy_week/ (the client's)", week.status_code >= 400))
    extra = RoutineExercise.objects.create(routine=routine, exercise=w["exercise"], day=1,
                                           order=9, sets=1, reps=1)
    gone = t.delete(f"{R}routine-exercises/{extra.pk}/")
    out.append(("DELETE routine-exercises/{id}/ (the client's routine)",
                gone.status_code >= 400))
    return out


@pytest.mark.parametrize("how", ["end_coaching", "unassign"])
def test_a_former_trainer_reads_nothing_of_the_clients_training(make_user, api, how):
    w = _world(make_user, api, f"fr{how[:3]}r")
    assert all(refused is False for _name, refused in _reads(api, w)), \
        "the coaching is running: every read answers before the end"
    _end(api, w, how)
    still = [name for name, refused in _reads(api, w) if not refused]
    assert still == [], f"still answering a former trainer: {still}"


@pytest.mark.xfail(strict=True, reason=(
    "AWAITING THE MANAGER'S RULING (2026-11-24): the author of a routine still edits it "
    "after the coaching is gone — PATCH routines/{id}/, routine-exercises/ POST, PATCH "
    "and DELETE on it, routines/{id}/copy_week/. New routines, assign_to_client and "
    "generate are refused. Strict: it fails loudly the day those writes are closed."))
@pytest.mark.parametrize("how", ["end_coaching", "unassign"])
def test_a_former_trainer_writes_nothing_of_the_clients_programme(make_user, api, how):
    w = _world(make_user, api, f"fr{how[:3]}w")
    _end(api, w, how)
    still = [name for name, refused in _writes(api, w) if not refused]
    assert still == [], f"a programme write still answers a former trainer: {still}"


@pytest.mark.parametrize("how", ["end_coaching", "unassign"])
def test_the_chat_thread_stays_readable_by_both(make_user, api, how):
    w = _world(make_user, api, f"fr{how[:3]}c")
    _end(api, w, how)
    for who in (w["trainer"], w["client"]):
        threads = [row["id"] for row in _rows(api(who).get("/api/v1/chat/conversations/"))]
        assert w["conversation"].pk in threads, who
        messages = api(who).get(f"/api/v1/chat/conversations/{w['conversation'].pk}/messages/")
        assert messages.status_code == 200, messages.content
        assert w["said"].pk in [row["id"] for row in _rows(messages)]


@pytest.mark.parametrize("how", ["end_coaching", "unassign"])
def test_the_clients_programme_stays_theirs(make_user, api, how):
    w = _world(make_user, api, f"fr{how[:3]}p")
    _end(api, w, how)
    assert w["routine"].assigned_to.filter(pk=w["client"].pk).exists()
    resp = api(w["client"]).get(f"{R}routines/{w['routine'].pk}/")
    assert resp.status_code == 200, resp.content
