"""The fix run's ACC-2 (2026-11-24): what an erasure does to the notices that name the
erased person in OTHER people's inboxes.

  S-206  a like, a comment or a follow notice stores the person's username; erasure
         deletes the like, the comment and the follow, so the notice is an orphan that
         still named them. It goes with them, as their chat notices do (S-146).
  S-208  a trainer's own words to a client (`custom`) go with the trainer, as their chat
         notices do (the manager's ruling A).
  S-207  a coaching notice is a record of the other person's coaching: it STAYS, with its
         date, and the erased person's name reads "Deleted account" in the reader's
         language (the manager's ruling A) — `actor`'s name in a coaching notice, and a
         client's in their trainer's finished-session notice.

Every test fails on the code before.
"""
import uuid

import pytest

pytestmark = pytest.mark.django_db

INBOX = "/api/social/notifications/"


def _name(tag):
    return f"acc2_{tag}_{uuid.uuid4().hex[:6]}"


def _notice(recipient, actor, event_type, context, data=None):
    from notifications.models import Notification

    return Notification.objects.create(
        recipient=recipient, actor=actor, event_type=event_type,
        related_object_id=uuid.uuid4().hex, deduplication_key=uuid.uuid4().hex,
        metadata={"context": context, "data": data or {"type": event_type}})


def _inbox(api, person, language="en"):
    resp = api(person).get(INBOX, HTTP_ACCEPT_LANGUAGE=language)
    assert resp.status_code == 200, resp.content
    body = resp.json()
    return {row["id"]: row for row in (body["results"] if isinstance(body, dict) else body)}


def _erase(person):
    from training_platform.privacy import erase_user_data

    erase_user_data(person)


@pytest.mark.parametrize("event_type, context", [
    ("post_liked", {"actor": "SOMEONE"}),
    ("comment_created", {"actor": "SOMEONE", "preview": "nice one"}),
    ("user_followed", {"actor": "SOMEONE"}),
])
def test_a_social_notice_goes_with_the_person_it_names(make_user, event_type, context):
    from notifications.models import Notification

    author, fan = make_user(_name("author")), make_user(_name("fan"))
    row = _notice(author, fan, event_type, {**context, "actor": fan.username})
    other = _notice(author, make_user(_name("stays")), event_type, context)

    _erase(fan)
    assert not Notification.objects.filter(pk=row.pk).exists()
    assert Notification.objects.filter(pk=other.pk).exists()


def test_a_trainers_own_words_go_with_the_trainer(make_user):
    from notifications.models import Notification

    trainer, client = make_user(_name("t"), user_type="trainer"), make_user(_name("c"))
    row = _notice(client, trainer, "custom", {"message": "Sleep early before leg day."},
                  {"type": "custom", "trainer_id": str(trainer.pk)})

    _erase(trainer)
    assert not Notification.objects.filter(pk=row.pk).exists()


def test_a_coaching_notice_stays_and_names_a_deleted_account(make_user, api):
    from notifications.models import Notification

    trainer = make_user(_name("t2"), user_type="trainer", first_name="Rami")
    client = make_user(_name("c2"))
    row = _notice(client, trainer, "coaching_fee_held", {"name": "Rami"},
                  {"type": "coaching_fee_held", "trainer_id": trainer.pk})
    created = row.created_at

    _erase(trainer)
    row = Notification.objects.get(pk=row.pk)
    assert row.created_at == created
    assert "Rami" not in str(row.metadata)
    # Nothing in it points at the person: listed like a system notice.
    assert row.actor_id is None

    english = _inbox(api, client)[str(row.pk)]["message"]
    assert "Deleted account" in english and "Rami" not in english
    arabic = _inbox(api, client, "ar")[str(row.pk)]["message"]
    assert "حساب محذوف" in arabic and "Rami" not in arabic


def test_a_finished_session_notice_stops_naming_the_erased_client(make_user, api):
    from notifications.models import Notification

    trainer = make_user(_name("t3"), user_type="trainer")
    client = make_user(_name("c3"), first_name="Sami")
    row = _notice(trainer, None, "session_completed",
                  {"variant": "trainer", "name": "Sami", "routine": "Legs A",
                   "message": "Sami finished a workout on Legs A."},
                  {"type": "session_completed", "client_id": client.pk, "session_id": 1})

    _erase(client)
    row = Notification.objects.get(pk=row.pk)
    assert "Sami" not in str(row.metadata)
    shown = _inbox(api, trainer)[str(row.pk)]["message"]
    assert shown == "Deleted account finished a workout on Legs A.", shown
