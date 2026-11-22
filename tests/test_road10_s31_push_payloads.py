"""Road to 10 S3.1, the code side of phone notifications.

Every push carries the ids the app's page opens with, and the trainer's events reach the
trainer: a client's request (with the request to answer), a request cancelled, a client
leaving, coaching ended, and a finished session with its id. Each test asserts the
`Notification` row the trainer (or client) receives, not that an event was emitted, and
one test asserts what FCM is actually handed.

On the code before this change the request pushes carry no `request_id`, the coaching
endings no `client_id` / `trainer_id`, and a finished session no `session_id`.
"""
import types
import uuid

import pytest
from django.utils import timezone

pytestmark = pytest.mark.django_db

JSON = "application/json"
AUTH = "/api/auth"


def _name(tag):
    return f"s31_{tag}_{uuid.uuid4().hex[:6]}"


def _row(recipient, event_type):
    from notifications.models import Notification

    rows = list(Notification.objects.filter(recipient=recipient, event_type=event_type))
    assert len(rows) == 1, [r.event_type for r in
                            Notification.objects.filter(recipient=recipient)]
    return rows[0]


def _data(recipient, event_type):
    return _row(recipient, event_type).metadata["data"]


def test_a_clients_request_reaches_the_trainer_with_the_request_to_answer(
        make_user, api, django_capture_on_commit_callbacks):
    from users.models import TrainerClientRelation

    trainer = make_user(_name("t"), user_type="trainer", first_name="Rami")
    client = make_user(_name("c"), first_name="Lina")
    with django_capture_on_commit_callbacks(execute=True):
        sent = api(client).post(f"{AUTH}/client/request-trainer/", {"trainer_id": trainer.pk},
                                content_type=JSON)
    assert sent.status_code == 200, sent.content
    relation = TrainerClientRelation.objects.get(trainer=trainer, client=client)

    data = _data(trainer, "client_request_received")
    assert data["type"] == "client_request_received"
    assert data["client_id"] == client.pk
    assert data["request_id"] == relation.pk

    # The id is the one the answer route takes.
    answered = api(trainer).post(f"{AUTH}/trainer/respond-to-request/",
                                 {"request_id": data["request_id"], "action": "reject"},
                                 content_type=JSON)
    assert answered.status_code == 200, answered.content


def test_a_trainers_invitation_reaches_the_client_with_the_request_to_answer(
        make_user, api, django_capture_on_commit_callbacks):
    from users.models import TrainerClientRelation

    trainer = make_user(_name("t2"), user_type="trainer", first_name="Rami")
    client = make_user(_name("c2"), first_name="Lina")
    with django_capture_on_commit_callbacks(execute=True):
        sent = api(trainer).post(f"{AUTH}/trainer/assign-client/", {"client_id": client.pk},
                                 content_type=JSON)
    assert sent.status_code == 200, sent.content
    relation = TrainerClientRelation.objects.get(trainer=trainer, client=client)

    data = _data(client, "trainer_assignment_request")
    assert data["trainer_id"] == trainer.pk
    assert data["request_id"] == relation.pk
    answered = api(client).post(f"{AUTH}/client/respond-to-invite/",
                                {"request_id": data["request_id"], "action": "reject"},
                                content_type=JSON)
    assert answered.status_code == 200, answered.content


def test_a_cancelled_request_and_a_client_leaving_reach_the_trainer(
        make_user, api, django_capture_on_commit_callbacks):
    from users.models import TrainerClientRelation

    trainer = make_user(_name("t3"), user_type="trainer", first_name="Rami")
    client = make_user(_name("c3"), first_name="Lina")
    with django_capture_on_commit_callbacks(execute=True):
        assert api(client).post(f"{AUTH}/client/request-trainer/", {"trainer_id": trainer.pk},
                                content_type=JSON).status_code == 200
        cancelled = api(client).post(f"{AUTH}/client/cancel-trainer-request/", {},
                                     content_type=JSON)
    assert cancelled.status_code == 200, cancelled.content
    assert _data(trainer, "client_request_cancelled")["client_id"] == client.pk

    leaver = make_user(_name("c4"), first_name="Omar")
    TrainerClientRelation.objects.create(trainer=trainer, client=leaver, status="approved",
                                         initiated_by="client", responded_at=timezone.now())
    type(leaver).objects.filter(pk=leaver.pk).update(assigned_trainer=trainer)
    leaver.refresh_from_db()
    with django_capture_on_commit_callbacks(execute=True):
        left = api(leaver).post(f"{AUTH}/client/unassign-trainer/", {}, content_type=JSON)
    assert left.status_code == 200, left.content
    assert _data(trainer, "client_unassigned_trainer")["client_id"] == leaver.pk


@pytest.mark.parametrize("reason, event_type", [("unpaid", "coaching_ended"),
                                                 ("stopped", "coaching_finished")])
def test_coaching_ended_reaches_both_with_each_side_by_role(
        make_user, django_capture_on_commit_callbacks, reason, event_type):
    from django.db import transaction

    from users.coaching import end_coaching
    from users.models import TrainerClientRelation

    trainer = make_user(_name("t5"), user_type="trainer", first_name="Rami")
    client = make_user(_name("c5"), first_name="Lina")
    relation = TrainerClientRelation.objects.create(
        trainer=trainer, client=client, status="approved", initiated_by="client",
        responded_at=timezone.now())
    with django_capture_on_commit_callbacks(execute=True):
        with transaction.atomic():
            end_coaching(TrainerClientRelation.objects.select_for_update().get(pk=relation.pk),
                         reason=reason)

    to_trainer = _data(trainer, event_type)
    to_client = _data(client, event_type)
    assert to_trainer["user_id"] == client.pk and to_client["user_id"] == trainer.pk
    for data in (to_trainer, to_client):
        assert (data["client_id"], data["trainer_id"]) == (client.pk, trainer.pk)


def test_a_finished_session_reaches_the_trainer_with_its_id(
        make_user, api, django_capture_on_commit_callbacks):
    from routine.models import WorkoutSession
    from tests.test_home import _program
    from users.models import TrainerClientRelation

    trainer, client, routine, _rows = _program(make_user, _name("sess"))
    TrainerClientRelation.objects.create(trainer=trainer, client=client, status="approved",
                                         initiated_by="client", responded_at=timezone.now())
    session = WorkoutSession.objects.create(user=client, routine=routine, day=1)
    with django_capture_on_commit_callbacks(execute=True):
        done = api(client).patch(f"/api/routine/workout-sessions/{session.pk}/",
                                 {"status": "completed"}, content_type=JSON)
    assert done.status_code == 200, done.content

    for person in (trainer, client):
        data = _data(person, "session_completed")
        assert data["session_id"] == str(session.pk)
        assert data["client_id"] == str(client.pk)
        assert data["routine_id"] == str(routine.pk)
        # The generic keys stay as they were.
        assert data["related_object_id"] == str(session.pk)
        assert data["related_object_type"] == "WorkoutSession"

    # The id opens the trainer's review.
    review = api(trainer).get(f"/api/routine/workout-sessions/{session.pk}/?expand=sets")
    assert review.status_code == 200, review.content


def test_an_assigned_routine_names_the_routine(make_user, django_capture_on_commit_callbacks):
    from routine.services import notify_assignment
    from tests.test_home import _program

    _trainer, client, routine, _rows = _program(make_user, _name("assign"))
    with django_capture_on_commit_callbacks(execute=True):
        notify_assignment(client, routine)
    assert _data(client, "routine_assignment")["routine_id"] == str(routine.pk)


def test_the_phone_is_handed_the_ids_as_strings(make_user, api, monkeypatch,
                                                django_capture_on_commit_callbacks):
    """What FCM receives, not only the stored row: `data` with `type` and the ids, every
    value a string."""
    from firebase_admin import messaging

    from users.models import DeviceToken, TrainerClientRelation

    sent = []

    def send_each_for_multicast(message, dry_run=False):
        sent.append(dict(message.data))
        return types.SimpleNamespace(
            responses=[types.SimpleNamespace(success=True, exception=None)
                       for _ in message.tokens],
            success_count=len(message.tokens), failure_count=0)

    monkeypatch.setattr(messaging, "send_each_for_multicast", send_each_for_multicast)
    trainer = make_user(_name("t6"), user_type="trainer", first_name="Rami")
    client = make_user(_name("c6"), first_name="Lina")
    DeviceToken.objects.create(user=trainer, token=f"tok-{uuid.uuid4().hex}", is_active=True)
    with django_capture_on_commit_callbacks(execute=True):
        assert api(client).post(f"{AUTH}/client/request-trainer/", {"trainer_id": trainer.pk},
                                content_type=JSON).status_code == 200
    relation = TrainerClientRelation.objects.get(trainer=trainer, client=client)

    pushed = [d for d in sent if d.get("type") == "client_request_received"]
    assert pushed, sent
    assert pushed[0]["client_id"] == str(client.pk)
    assert pushed[0]["request_id"] == str(relation.pk)
    assert all(isinstance(v, str) for v in pushed[0].values())
