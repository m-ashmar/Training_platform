"""The manager's ruling on the S-207 scrub (2026-11-24): every notice the scrub touches
gains `data.person_erased: "true"`, so the app has one rule for every kind — a null
sender says nothing on a `session_completed`, which has no actor even while the client
lives. And the question behind it: what the trainer's taps on such a notice answer once
the client is erased.

  GET /api/routine/workout-sessions/{session_id}/?expand=sets   404 (the client's
                                                                 sessions are erased)
  GET /api/auth/trainer/client-profile/{client_id}/             403 `not_your_client`
                                                                 (the account is kept,
                                                                 anonymised, with no
                                                                 coaching left)

The first test fails on the code before; the others hold the rule and the answer.
"""
import uuid

import pytest

pytestmark = pytest.mark.django_db

JSON = "application/json"
INBOX = "/api/social/notifications/"


def _name(tag):
    return f"pef_{tag}_{uuid.uuid4().hex[:6]}"


def _notice(recipient, actor, event_type, context, data):
    from notifications.models import Notification

    return Notification.objects.create(
        recipient=recipient, actor=actor, event_type=event_type,
        related_object_id=uuid.uuid4().hex, deduplication_key=uuid.uuid4().hex,
        metadata={"context": context, "data": data})


def _erase(person):
    from training_platform.privacy import erase_user_data

    erase_user_data(person)


def _data(api, reader, row):
    rows = api(reader).get(INBOX).json()
    rows = rows["results"] if isinstance(rows, dict) else rows
    (found,) = [r for r in rows if r["id"] == str(row.pk)]
    return found["data"]


def test_each_family_of_scrubbed_notice_carries_the_flag(make_user, api):
    trainer = make_user(_name("t"), user_type="trainer", first_name="Rami")
    client = make_user(_name("c"), first_name="Sami")
    other_client = make_user(_name("c2"))
    # Coaching-month money, a trainer-client answer: both name the trainer (`actor`).
    fee = _notice(client, trainer, "coaching_fee_held", {"name": "Rami"},
                  {"type": "coaching_fee_held", "trainer_id": str(trainer.pk)})
    answer = _notice(other_client, trainer, "client_request_approved", {"name": "Rami"},
                     {"type": "client_request_approved", "trainer_id": str(trainer.pk)})
    _erase(trainer)
    for reader, row in ((client, fee), (other_client, answer)):
        data = _data(api, reader, row)
        assert data["person_erased"] == "true", data
        assert data["trainer_id"] == str(trainer.pk)        # the ids stay as they were

    # A trainer's finished-session notice about the client: no actor, `data.client_id`.
    coach = make_user(_name("t2"), user_type="trainer")
    athlete = make_user(_name("c3"), first_name="Sami")
    session = _notice(coach, None, "session_completed",
                      {"variant": "trainer", "name": "Sami", "routine": "Legs A",
                       "message": "Sami finished a workout on Legs A."},
                      {"type": "session_completed", "client_id": athlete.pk,
                       "session_id": 7})
    _erase(athlete)
    assert _data(api, coach, session)["person_erased"] == "true"


def test_a_living_persons_notice_never_carries_it(make_user, api):
    trainer = make_user(_name("t3"), user_type="trainer")
    client = make_user(_name("c4"))
    erased_one = make_user(_name("t4"), user_type="trainer")
    living = _notice(client, trainer, "coaching_renewed", {"name": "Rami"},
                     {"type": "coaching_renewed", "trainer_id": str(trainer.pk)})
    _notice(client, erased_one, "coaching_renewed", {"name": "Omar"},
            {"type": "coaching_renewed", "trainer_id": str(erased_one.pk)})
    _erase(erased_one)
    assert "person_erased" not in _data(api, client, living)


def test_a_suspended_accounts_notices_never_carry_it(make_user):
    from notifications.models import Notification

    trainer = make_user(_name("t5"), user_type="trainer")
    client = make_user(_name("c5"))
    row = _notice(client, trainer, "coaching_ended", {"name": "Rami"},
                  {"type": "coaching_ended", "trainer_id": str(trainer.pk)})
    type(trainer).objects.filter(pk=trainer.pk).update(is_active=False)   # suspended
    row = Notification.objects.get(pk=row.pk)
    assert "person_erased" not in row.metadata["data"]
    assert row.actor_id == trainer.pk


def test_what_the_trainers_taps_answer_once_the_client_is_erased(
        make_user, api, django_capture_on_commit_callbacks):
    """The question: the notice's tap opens the session (`/t/sessions/{session_id}`) or
    the client's file. After the erasure the session is gone (404) and the file is
    refused (403 `not_your_client`): neither reads, so the app opens the list."""
    from tests.test_day_outcomes import _program
    from users.models import TrainerClientRelation

    trainer, client, routine, _rows = _program(make_user, f"pef{uuid.uuid4().hex[:4]}")
    TrainerClientRelation.objects.create(trainer=trainer, client=client, status="approved",
                                         initiated_by="client")
    started = api(client).post("/api/routine/workout-sessions/",
                               {"routine": routine.pk, "day": 1}, content_type=JSON)
    assert started.status_code in (200, 201), started.content
    session = started.json()["id"]
    with django_capture_on_commit_callbacks(execute=True):
        done = api(client).patch(f"/api/routine/workout-sessions/{session}/",
                                 {"status": "completed"}, content_type=JSON)
    assert done.status_code == 200, done.content

    review = f"/api/routine/workout-sessions/{session}/?expand=sets"
    file = f"/api/auth/trainer/client-profile/{client.pk}/"
    assert api(trainer).get(review).status_code == 200
    assert api(trainer).get(file).status_code == 200

    erased = api(client).delete("/api/privacy/erase/?confirm=ERASE")
    assert erased.status_code == 200, erased.content
    assert api(trainer).get(review).status_code == 404
    refused = api(trainer).get(file)
    assert (refused.status_code, refused.json()["code"]) == (403, "not_your_client")
