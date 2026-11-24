"""The fix run's ACC-1, S-281 (2026-11-24): the trainer queue's `unanswered_message`
card is read as ONE row per thread, counted by the database. It read every waiting
message of every thread into Python and counted them in a loop, so a client who stacked
up thirty messages cost thirty rows for one card. The card itself is unchanged: the
first waiting message, and how many are waiting. The row-count test fails on the code
before; the card test holds what the card says.
"""
import uuid

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext

pytestmark = pytest.mark.django_db

QUEUE = "/api/v1/trainer/attention/"


def _name(tag):
    return f"s281_{tag}_{uuid.uuid4().hex[:6]}"


def _thread(make_user, trainer, n):
    from chat.models import Message
    from chat.services import open_conversation
    from users.models import TrainerClientRelation

    client = make_user(_name("c"))
    TrainerClientRelation.objects.create(trainer=trainer, client=client, status="approved",
                                         initiated_by="client")
    conversation = open_conversation(trainer, client)
    sent = [Message.objects.create(conversation=conversation, sender=client, kind="text",
                                   body=f"hello {i}") for i in range(n)]
    return client, conversation, sent


def _cards(api, trainer):
    resp = api(trainer).get(QUEUE, HTTP_X_APP_CHANNEL="apk")
    assert resp.status_code == 200, resp.content
    return [i for i in resp.json()["items"] if i["kind"] == "unanswered_message"]


def test_the_card_says_the_first_waiting_message_and_how_many(make_user, api):
    trainer = make_user(_name("t"), user_type="trainer")
    _c1, conv1, sent1 = _thread(make_user, trainer, 30)
    _c2, conv2, sent2 = _thread(make_user, trainer, 2)
    cards = {c["unanswered_message"]["conversation_id"]: c["unanswered_message"]
             for c in _cards(api, trainer)}
    assert cards[conv1.pk]["count"] == 30 and cards[conv1.pk]["message_id"] == sent1[0].pk
    assert cards[conv2.pk]["count"] == 2 and cards[conv2.pk]["message_id"] == sent2[0].pk


def test_thirty_waiting_messages_are_read_as_one_row(make_user, api):
    trainer = make_user(_name("t2"), user_type="trainer")
    _thread(make_user, trainer, 30)
    with CaptureQueriesContext(connection) as seen:
        assert len(_cards(api, trainer)) == 1
    # The query that reads the waiting messages counts them per thread and returns one
    # row per thread; run again by hand, it returns one row for the thirty messages.
    unanswered = [q["sql"] for q in seen.captured_queries
                  if 'FROM "chat_message"' in q["sql"] and "DISTINCT ON" in q["sql"]
                  and 'OVER (PARTITION BY "chat_message"."conversation_id")' in q["sql"]]
    assert len(unanswered) == 1, [q["sql"][:120] for q in seen.captured_queries]
    with connection.cursor() as cursor:
        cursor.execute(unanswered[0])
        assert len(cursor.fetchall()) == 1
