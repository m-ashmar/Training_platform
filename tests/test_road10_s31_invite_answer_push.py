"""Road to 10 S3.1, the review of phase 3: the answer to a request reaches the one who
asked with both ids and the name of the one who answered.

`client_request_approved` / `client_request_rejected` go both ways: to a CLIENT when a
trainer answers the client's request, and to a TRAINER when a client answers the
trainer's invitation. On the second path the trainer was handed the CLIENT's id as
`trainer_id` (the event's actor) and no `client_id`, and the text named the trainer
themself («Your request to trainer Rami has been approved», sent to Rami). Now
`trainer_id` is the relation's trainer and `client_id` its client on both paths, and the
text names the person who answered.
"""
import uuid

import pytest

pytestmark = pytest.mark.django_db

JSON = "application/json"
AUTH = "/api/auth"


def _name(tag):
    return f"s31r_{tag}_{uuid.uuid4().hex[:6]}"


def _row(recipient, event_type):
    from notifications.models import Notification

    rows = list(Notification.objects.filter(recipient=recipient, event_type=event_type))
    assert len(rows) == 1, [r.event_type for r in
                            Notification.objects.filter(recipient=recipient)]
    return rows[0]


def _inbox_message(api, recipient, event_type):
    body = api(recipient).get("/api/social/notifications/").json()
    rows = body["results"] if isinstance(body, dict) else body
    found = [r for r in rows if r["notification_type"] == event_type]
    assert len(found) == 1, rows
    return found[0]


@pytest.mark.parametrize("action,event_type", [("approve", "client_request_approved"),
                                               ("reject", "client_request_rejected")])
def test_a_client_answering_an_invitation_tells_the_trainer_with_both_ids(
        make_user, api, django_capture_on_commit_callbacks, action, event_type):
    trainer = make_user(_name("t"), user_type="trainer", first_name="Rami",
                        last_name="Saleh")
    client = make_user(_name("c"), first_name="Lina", last_name="Haddad")
    with django_capture_on_commit_callbacks(execute=True):
        sent = api(trainer).post(f"{AUTH}/trainer/assign-client/", {"client_id": client.pk},
                                 content_type=JSON)
    assert sent.status_code == 200, sent.content
    request_id = _row(client, "trainer_assignment_request").metadata["data"]["request_id"]
    with django_capture_on_commit_callbacks(execute=True):
        answered = api(client).post(f"{AUTH}/client/respond-to-invite/",
                                    {"request_id": request_id, "action": action},
                                    content_type=JSON)
    assert answered.status_code == 200, answered.content

    data = _row(trainer, event_type).metadata["data"]
    assert data["trainer_id"] == trainer.pk
    assert data["client_id"] == client.pk
    row = _inbox_message(api, trainer, event_type)
    assert "Lina Haddad" in row["message"], row
    assert "Rami" not in row["message"], row


@pytest.mark.parametrize("action,event_type", [("approve", "client_request_approved"),
                                               ("reject", "client_request_rejected")])
def test_a_trainer_answering_a_request_tells_the_client_with_both_ids(
        make_user, api, django_capture_on_commit_callbacks, action, event_type):
    trainer = make_user(_name("t2"), user_type="trainer", first_name="Rami",
                        last_name="Saleh")
    client = make_user(_name("c2"), first_name="Lina", last_name="Haddad")
    with django_capture_on_commit_callbacks(execute=True):
        sent = api(client).post(f"{AUTH}/client/request-trainer/", {"trainer_id": trainer.pk},
                                content_type=JSON)
    assert sent.status_code == 200, sent.content
    request_id = _row(trainer, "client_request_received").metadata["data"]["request_id"]
    with django_capture_on_commit_callbacks(execute=True):
        answered = api(trainer).post(f"{AUTH}/trainer/respond-to-request/",
                                     {"request_id": request_id, "action": action},
                                     content_type=JSON)
    assert answered.status_code == 200, answered.content

    data = _row(client, event_type).metadata["data"]
    assert data["trainer_id"] == trainer.pk
    assert data["client_id"] == client.pk
    row = _inbox_message(api, client, event_type)
    assert "Rami Saleh" in row["message"], row


def test_a_notice_queued_before_keeps_its_trainer_id():
    """An event serialised before the new fields existed still renders and carries the
    id it had (its actor), with `client_id` 0."""
    from notifications.domain.trainer_client_events import ClientRequestApprovedEvent

    old = ClientRequestApprovedEvent.from_dict(
        {"actor_id": 7, "recipient_id": 8, "trainer_name": "Rami"})
    assert old.trainer_id == 0 and old.client_id == 0 and old.answered_by_name == ""
