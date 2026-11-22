"""Road to 10 S2.3: the trainer's queue knows about messages and first programmes.

`GET /api/v1/trainer/attention/` had no card for a client's message waiting for an
answer, and showed a client whose first month is held for want of a programme as an
idle client who «never started»; the roster had no unread count and neither the roster
nor the queue said WHICH session was the last. Now: `unanswered_message`,
`awaiting_first_program`, `unread_count` and `last_session.id` / `last_workout.session_id`.
Each fails on the code before this change.
"""
import pytest

pytestmark = pytest.mark.django_db

QUEUE = "/api/v1/trainer/attention/"
ROSTER = "/api/routine/trainer/client-progress/recent/"
JSON = "application/json"


def _queue(api, trainer):
    resp = api(trainer).get(QUEUE, HTTP_X_APP_CHANNEL="apk")
    assert resp.status_code == 200, resp.content
    return resp.json()["items"]


def test_a_client_waiting_for_the_first_programme_is_not_idle(make_user, api):
    from tests.test_coaching_months import _accepted

    trainer, client, relation = _accepted(make_user, api, "s23_first")
    items = _queue(api, trainer)
    kinds = [(item["kind"], item["client"]["id"]) for item in items]
    assert ("awaiting_first_program", client.pk) in kinds
    assert ("idle_client", client.pk) not in kinds
    card = next(i for i in items if i["kind"] == "awaiting_first_program")
    detail = card["awaiting_first_program"]
    assert detail["relation_id"] == relation.pk
    assert detail["held_amount"] is not None and detail["held_currency"]


def test_a_message_waiting_for_an_answer_is_a_card_until_answered(make_user, api):
    from chat.models import Conversation, Message
    from tests.test_coaching_months import _accepted, _deliver_first_program

    trainer, client, _relation = _accepted(make_user, api, "s23_msg")
    _deliver_first_program(api, trainer, client)
    conversation, _ = Conversation.objects.get_or_create(trainer=trainer, client=client)
    first = Message.objects.create(conversation=conversation, sender=client, body="hi")
    Message.objects.create(conversation=conversation, sender=client, body="are you there")

    cards = [i for i in _queue(api, trainer) if i["kind"] == "unanswered_message"]
    assert len(cards) == 1
    detail = cards[0]["unanswered_message"]
    assert (detail["conversation_id"], detail["message_id"], detail["count"]) == \
        (conversation.pk, first.pk, 2)

    Message.objects.create(conversation=conversation, sender=trainer, body="yes")
    assert not [i for i in _queue(api, trainer) if i["kind"] == "unanswered_message"]


def test_the_roster_counts_unread_messages_and_names_the_last_session(make_user, api):
    from django.utils import timezone

    from chat.models import Conversation, Message
    from routine.models import Routine, WorkoutSession
    from tests.test_coaching_months import _accepted, _deliver_first_program

    trainer, client, _relation = _accepted(make_user, api, "s23_roster")
    _deliver_first_program(api, trainer, client)
    routine = Routine.objects.filter(created_by=trainer).first()
    session = WorkoutSession.objects.create(user=client, routine=routine, day=1,
                                            status="completed",
                                            end_time=timezone.now())
    conversation, _ = Conversation.objects.get_or_create(trainer=trainer, client=client)
    Message.objects.create(conversation=conversation, sender=client, body="one")
    Message.objects.create(conversation=conversation, sender=client, body="two")

    rows = api(trainer).get(ROSTER).json()["recent_activity"]
    row = next(r for r in rows if r["client_info"]["id"] == client.pk)
    assert row["unread_count"] == 2
    assert row["last_session"]["id"] == session.pk
