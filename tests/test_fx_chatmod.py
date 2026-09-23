"""The chat, moderation and notification defects of the 2026-09-23 dive (SIDE_BUGS).

One test per row, each asserting the behaviour the row says was missing, through the
real route or socket and the real rows — never a mock that was called:

    S-062  the chat socket re-checks the credential on every frame
    S-063  a block closes the chat: list, start, send, socket
    S-092  the chat socket spends the same hourly allowance as HTTP
    S-093  a socket error is in the reader's language
    S-094  a deleted message stops being quoted in the recipient's inbox
    S-095  one push per unread burst however far the notification worker lags
    S-096  an ENDED paid coaching reads `coaching_ended`, and its thread can be opened
    S-065  a challenge can be reported, and an operator can hide it
    S-105  a message in the trainer-client chat can be reported (`coach_message`)
    S-106  the block docstring says what a block does not hide
    S-066  `notifications/message-client/` obeys blocks and the coaching freeze
    S-107  a hidden or deleted comment stops being quoted in the post author's inbox
"""
import json
import uuid
from datetime import timedelta
from decimal import Decimal

import pytest
from asgiref.sync import async_to_sync
from django.test.utils import override_settings
from django.utils import timezone

pytestmark = pytest.mark.django_db

CHAT = '/api/v1/chat'
IN_MEMORY_LAYER = {'default': {'BACKEND': 'channels.layers.InMemoryChannelLayer'}}


# --------------------------------------------------------------------------- setup
def _name(prefix):
    return f"{prefix}_{uuid.uuid4().hex[:6]}"


@pytest.fixture
def pair(make_user):
    """An approved trainer and client, with no coaching month bought yet."""
    from users.models import TrainerClientRelation

    trainer = make_user(_name("fx_trainer"), user_type="trainer")
    client = make_user(_name("fx_client"), user_type="client")
    TrainerClientRelation.objects.create(trainer=trainer, client=client,
                                         status="approved", initiated_by="client")
    return trainer, client


@pytest.fixture
def conversation(pair):
    from chat.services import open_conversation

    return open_conversation(*pair)


def _month(trainer, client, relation=None, *, frozen=False, days_ago=31):
    """One settled coaching month that has ENDED — frozen when `frozen`."""
    from users.models import CoachingPeriod

    now = timezone.now()
    return CoachingPeriod.objects.create(
        relation=relation, trainer=trainer, client=client, number=1,
        price=Decimal("100.00"), commission_rate=Decimal("0.2000"), status='settled',
        hold_reference=str(uuid.uuid4()),
        starts_at=now - timedelta(days=days_ago), ends_at=now - timedelta(days=1),
        frozen_at=(now - timedelta(days=1)) if frozen else None)


def _send(api, user, conversation, body="hello", **extra):
    return api(user).post(f"{CHAT}/conversations/{conversation.pk}/messages/",
                          {'body': body, **extra}, content_type="application/json")


def _row(api, user, conversation):
    rows = api(user).get(f"{CHAT}/conversations/").json()['results']
    return next(r for r in rows if r['id'] == conversation.pk)


@pytest.fixture
def socket_keeps_the_test_connection(monkeypatch):
    """The same shim `tests/test_chat.py` uses: Channels sweeps "old" connections
    around every `database_sync_to_async`, and the one it finds in a test is the one
    holding the test's transaction."""
    monkeypatch.setattr('channels.db.close_old_connections', lambda: None)


def _socket(conversation, user):
    from channels.routing import URLRouter
    from channels.testing import WebsocketCommunicator
    from rest_framework_simplejwt.tokens import RefreshToken

    import chat.routing

    token = str(RefreshToken.for_user(user).access_token)
    return WebsocketCommunicator(URLRouter(chat.routing.websocket_urlpatterns),
                                 f"/ws/chat/{conversation.pk}/?token={token}")


class _Queue(list):
    """A list that can also be drained."""


@pytest.fixture
def queued_events(monkeypatch):
    """Hold every emitted event in a list instead of running the worker at once.

    `CELERY_TASK_ALWAYS_EAGER` makes the notification worker run inside `emit_event`,
    which hides every race between a send and the worker's write. Production does not
    run it eagerly; this puts the lag back, and `drain()` runs what was queued through
    the real task.
    """
    from notifications.tasks import process_event_task

    queued = _Queue()
    monkeypatch.setattr(process_event_task, 'delay',
                        lambda path, data: queued.append((path, data)))

    def drain():
        while queued:
            path, data = queued.pop(0)
            process_event_task.apply(args=(path, data))

    queued.drain = drain
    return queued


# ----------------------------------------------------------------------- S-062
@override_settings(CHANNEL_LAYERS=IN_MEMORY_LAYER)
def test_s062_a_suspended_account_cannot_keep_writing_over_an_open_socket(
        pair, conversation, socket_keeps_the_test_connection):
    """The socket checked the token once, at connect. Suspending the account (the
    operator's moderation action) must stop the NEXT frame, as the REST route does."""
    from channels.db import database_sync_to_async

    from chat.models import Message
    from users.models import CustomUser

    _trainer, client = pair
    comm = _socket(conversation, client)

    async def scenario():
        connected, _sub = await comm.connect()
        assert connected
        await database_sync_to_async(
            lambda: CustomUser.objects.filter(pk=client.pk).update(is_active=False))()
        await comm.send_to(text_data=json.dumps(
            {'type': 'message.send', 'body': 'still here?'}))
        frame = json.loads(await comm.receive_from(timeout=5))
        closed = await comm.receive_output(timeout=5)
        await comm.disconnect()
        return frame, closed

    frame, closed = async_to_sync(scenario)()
    assert frame['type'] == 'error' and frame['code'] == 'auth', frame
    assert closed == {'type': 'websocket.close', 'code': 4001}, closed
    assert not Message.objects.filter(conversation=conversation).exists(), \
        "a suspended account wrote a message over a socket it opened before"


# ----------------------------------------------------------------------- S-063
@override_settings(CHANNEL_LAYERS=IN_MEMORY_LAYER)
def test_s063_a_block_closes_the_chat_both_ways(api, pair, conversation,
                                                socket_keeps_the_test_connection):
    from chat.models import Message
    from moderation.models import Block
    from notifications.models import Notification

    trainer, client = pair
    assert _send(api, trainer, conversation, "before the block").status_code == 201
    Block.objects.create(blocker=client, blocked=trainer)

    # The list says why the composer is closed, on both sides.
    for who in (client, trainer):
        row = _row(api, who, conversation)
        assert (row['can_send'], row['refusal']) == (False, 'blocked'), row
        # The thread stays readable.
        history = api(who).get(f"{CHAT}/conversations/{conversation.pk}/messages/")
        assert history.status_code == 200

    # Neither side can write — the one who blocked, nor the one who was blocked.
    for who in (trainer, client):
        refused = _send(api, who, conversation, "can you hear me")
        assert (refused.status_code, refused.json()['code']) == (403, 'blocked')

    # Nor start a thread: the same 404 a stranger gets.
    started = api(trainer).post(f"{CHAT}/conversations/start/",
                                {'client_id': client.pk}, content_type="application/json")
    assert (started.status_code, started.json()['code']) == (404, 'not_found')

    # Nor over the socket.
    comm = _socket(conversation, trainer)

    async def scenario():
        connected, _sub = await comm.connect()
        assert connected
        await comm.send_to(text_data=json.dumps({'type': 'message.send', 'body': 'x'}))
        frame = json.loads(await comm.receive_from(timeout=5))
        await comm.disconnect()
        return frame

    frame = async_to_sync(scenario)()
    assert frame['code'] == 'blocked', frame

    assert Message.objects.filter(conversation=conversation).count() == 1
    assert Notification.objects.filter(recipient=client,
                                       event_type='chat_message').count() == 1, \
        "the blocker was pushed a message sent after the block"


# ----------------------------------------------------------------------- S-092
@override_settings(CHANNEL_LAYERS=IN_MEMORY_LAYER)
def test_s092_the_socket_spends_the_http_allowance(api, pair, conversation,
                                                   socket_keeps_the_test_connection):
    """One allowance, whichever door: a client whose hour is spent over HTTP cannot
    keep writing over a socket."""
    from chat.models import Message
    from training_platform.cache import ratelimit_cache
    from training_platform.middleware import RateLimitMiddleware

    _trainer, client = pair
    allowance = RateLimitMiddleware(lambda r: None).rate_limits['client']['requests']
    # The hour spent, in the very bucket the HTTP middleware counts in.
    ratelimit_cache().set(f"rate_limit:user_{client.pk}", allowance, 3600)
    assert api(client).get(f"{CHAT}/conversations/").status_code == 429

    comm = _socket(conversation, client)

    async def scenario():
        connected, _sub = await comm.connect()
        assert connected
        await comm.send_to(text_data=json.dumps(
            {'type': 'message.send', 'body': 'one more'}))
        frame = json.loads(await comm.receive_from(timeout=5))
        await comm.disconnect()
        return frame

    frame = async_to_sync(scenario)()
    assert frame['type'] == 'error' and frame['code'] == 'rate_limited', frame
    assert frame['retry_after'] > 0
    assert not Message.objects.filter(conversation=conversation).exists()


# ----------------------------------------------------------------------- S-093
@override_settings(CHANNEL_LAYERS=IN_MEMORY_LAYER)
def test_s093_a_socket_refusal_is_in_the_readers_language(
        pair, conversation, socket_keeps_the_test_connection):
    """An Arabic reader got English over the socket for every refusal, although the
    REST route answers in Arabic: the text was rendered before the language was on."""
    from django.utils import translation

    from users.exceptions import CoachingFrozen
    from users.models import CustomUser, TrainerClientRelation

    trainer, client = pair
    CustomUser.objects.filter(pk=client.pk).update(preferred_language='ar')
    client.refresh_from_db()
    _month(trainer, client, TrainerClientRelation.objects.get(trainer=trainer,
                                                             client=client), frozen=True)
    with translation.override('ar'):
        frozen_ar = str(CoachingFrozen.default_detail)
        frame_ar = str(translation.gettext('Send a JSON object.'))
    with translation.override('en'):
        assert str(CoachingFrozen.default_detail) != frozen_ar, \
            "the catalogue has no Arabic for this refusal; the test would prove nothing"

    comm = _socket(conversation, client)

    async def scenario():
        connected, _sub = await comm.connect()
        assert connected
        await comm.send_to(text_data=json.dumps(
            {'type': 'message.send', 'body': 'let me in'}))
        refused = json.loads(await comm.receive_from(timeout=5))
        await comm.send_to(text_data='[1, 2]')
        malformed = json.loads(await comm.receive_from(timeout=5))
        await comm.disconnect()
        return refused, malformed

    refused, malformed = async_to_sync(scenario)()
    assert refused['code'] == 'coaching_frozen', refused
    assert refused['detail'] == frozen_ar, refused
    assert malformed['code'] == 'invalid_frame' and malformed['detail'] == frame_ar


# ----------------------------------------------------------------------- S-094
def test_s094_a_deleted_message_is_not_quoted_in_the_inbox(api, pair, conversation,
                                                           queued_events):
    from notifications.models import Notification

    trainer, client = pair

    def inbox_text():
        body = api(client).get("/api/social/notifications/").json()
        rows = body['results'] if isinstance(body, dict) else body
        return " ".join(f"{r['title']} {r['message']}" for r in rows)

    # The push was written before the delete.
    sent = _send(api, trainer, conversation, "my home address is 12 Palm St")
    queued_events.drain()
    assert "Palm St" in inbox_text()
    assert api(trainer).delete(f"{CHAT}/messages/{sent.json()['id']}/").status_code == 200
    note = Notification.objects.get(recipient=client, event_type='chat_message')
    assert note.metadata['context']['preview'] == ''
    assert "Palm St" not in inbox_text(), "the inbox still quotes a deleted message"

    # And the worker was still behind when the delete landed.
    assert api(client).post(f"{CHAT}/conversations/{conversation.pk}/read/"
                            ).status_code == 200
    late = _send(api, trainer, conversation, "my card pin is 4471")
    assert api(trainer).delete(f"{CHAT}/messages/{late.json()['id']}/").status_code == 200
    queued_events.drain()
    assert Notification.objects.filter(recipient=client,
                                       event_type='chat_message').count() == 2
    assert "4471" not in inbox_text(), "a push written after the delete quoted it"


# ----------------------------------------------------------------------- S-095
def test_s095_one_push_per_unread_burst_however_far_the_worker_lags(
        api, pair, conversation, queued_events):
    from notifications.models import Notification

    trainer, client = pair

    def pushes():
        return Notification.objects.filter(recipient=client,
                                           event_type='chat_message').count()

    for i in range(4):
        assert _send(api, trainer, conversation, f"burst {i}").status_code == 201
    assert len(queued_events) == 1, \
        f"a burst the worker had not drained yet queued {len(queued_events)} pushes"

    # The client opens the thread BEFORE the worker writes that first push...
    assert api(client).post(f"{CHAT}/conversations/{conversation.pk}/read/"
                            ).status_code == 200
    queued_events.drain()
    assert pushes() == 1
    # ...so the next message is a legitimate nudge and must not be swallowed.
    assert _send(api, trainer, conversation, "after you read").status_code == 201
    queued_events.drain()
    assert pushes() == 2, "a read between a send and the worker's write swallowed a push"


# ----------------------------------------------------------------------- S-096
def test_s096_an_ended_paid_coaching_reads_coaching_ended(api, pair, conversation):
    """Ending a coaching deletes the relation; the pair's paid months stay. The chat
    promised `coaching_ended` and a thread that can still be opened."""
    from django.db import transaction

    from users.coaching import end_coaching
    from users.models import TrainerClientRelation

    trainer, client = pair
    relation = TrainerClientRelation.objects.get(trainer=trainer, client=client)
    _month(trainer, client, relation)
    assert _send(api, trainer, conversation, "our last session").status_code == 201
    with transaction.atomic():
        end_coaching(TrainerClientRelation.objects.select_for_update().get(
            pk=relation.pk), reason='unpaid')
    assert not TrainerClientRelation.objects.filter(pk=relation.pk).exists()

    for who in (trainer, client):
        row = _row(api, who, conversation)
        assert (row['can_send'], row['refusal']) == (False, 'coaching_ended'), row
        refused = _send(api, who, conversation, "hello again")
        assert (refused.status_code, refused.json()['code']) == (403, 'coaching_ended')
        assert refused.json()['trainer_id'] == trainer.pk

    reopened = api(client).post(f"{CHAT}/conversations/start/",
                                {'trainer_id': trainer.pk},
                                content_type="application/json")
    assert reopened.status_code == 200, reopened.content
    assert reopened.json()['conversation']['id'] == conversation.pk
    assert reopened.json()['conversation']['refusal'] == 'coaching_ended'


# ----------------------------------------------------------------------- S-065
def test_s065_a_challenge_can_be_reported_and_hidden_by_an_operator(make_user, api):
    from admin_dashboard.admin import ChallengeAdmin
    from social.models import Challenge

    creator = make_user(_name("fx_creator"))
    reader = make_user(_name("fx_reader"))
    now = timezone.now()
    challenge = Challenge.objects.create(
        title="abusive title", description="abusive text", challenge_type="custom",
        creator=creator, start_date=now - timedelta(days=1),
        end_date=now + timedelta(days=7))

    def report(who):
        return api(who).post("/api/v1/reports/",
                             {"target_type": "challenge", "target_id": challenge.pk,
                              "reason": "harassment"},
                             content_type="application/json")

    filed = report(reader)
    assert filed.status_code == 201, filed.content
    own = report(creator)
    assert (own.status_code, own.json()['code']) == (403, 'cannot_report_yourself')

    # The operator's switch exists in the console...
    assert {a.__name__ for a in ChallengeAdmin.actions} >= {'hide_challenges',
                                                           'unhide_challenges'}
    # ...and a hidden challenge is served to nobody, its creator included.
    challenge.is_hidden = True
    challenge.save()
    for who in (reader, creator):
        listed = api(who).get("/api/social/challenges/").json()
        rows = listed['results'] if isinstance(listed, dict) else listed
        assert challenge.pk not in {r['id'] for r in rows}
        assert api(who).get(f"/api/social/challenges/{challenge.pk}/").status_code == 404
    again = make_user(_name("fx_other"))
    gone = report(again)
    assert (gone.status_code, gone.json()['code']) == (404, 'target_not_found')


# ----------------------------------------------------------------------- S-105
def test_s105_a_message_in_the_trainer_chat_can_be_reported(make_user, api, pair,
                                                            conversation):
    trainer, client = pair
    sent = _send(api, trainer, conversation, "something abusive").json()

    def report(who, target_id):
        return api(who).post("/api/v1/reports/",
                             {"target_type": "coach_message", "target_id": target_id,
                              "reason": "harassment"},
                             content_type="application/json")

    outsider = make_user(_name("fx_outsider"))
    assert report(outsider, sent['id']).status_code == 404
    own = report(trainer, sent['id'])
    assert (own.status_code, own.json()['code']) == (403, 'cannot_report_yourself')

    # Taken back a minute later: still the reporter's evidence.
    assert api(trainer).delete(f"{CHAT}/messages/{sent['id']}/").status_code == 200
    filed = report(client, sent['id'])
    assert filed.status_code == 201, filed.content

    from moderation.models import Report
    row = Report.objects.get(pk=filed.json()['id'])
    assert (row.target_type, row.target_id) == ('coach_message', sent['id'])


# ----------------------------------------------------------------------- S-106
def test_s106_the_block_contract_says_what_a_block_does_not_hide():
    """A block does not end or hide an approved coaching; the docstring said it hid the
    "trainer roster", which the server does not do (owner decision pending)."""
    import moderation.services
    from moderation.views import BlockListCreateView

    for text in (BlockListCreateView.__doc__, moderation.services.__doc__):
        assert "trainer roster" not in " ".join(text.split())
    assert "trainer/clients/" in BlockListCreateView.__doc__


# ----------------------------------------------------------------------- S-066
def test_s066_message_client_obeys_blocks_and_the_freeze(api, pair):
    from moderation.models import Block
    from notifications.models import Notification
    from users.models import TrainerClientRelation

    trainer, client = pair

    def message():
        return api(trainer).post("/api/notifications/message-client/",
                                 {"client_id": client.pk, "message": "rest day"},
                                 content_type="application/json")

    def notices():
        return Notification.objects.filter(recipient=client, event_type='custom').count()

    assert message().status_code == 201
    assert notices() == 1

    block = Block.objects.create(blocker=client, blocked=trainer)
    refused = message()
    assert (refused.status_code, refused.json()['code']) == (403, 'user_blocked')
    block.delete()

    _month(trainer, client, TrainerClientRelation.objects.get(trainer=trainer,
                                                             client=client), frozen=True)
    frozen = message()
    assert (frozen.status_code, frozen.json()['code']) == (403, 'coaching_frozen')
    assert frozen.json()['trainer_id'] == trainer.pk
    assert notices() == 1, "a refused message still reached the client's inbox"


# ----------------------------------------------------------------------- S-107
def test_s107_a_hidden_or_deleted_comment_is_not_quoted_in_the_inbox(make_user, api):
    from notifications.models import Notification
    from social.models import Comment, Post

    author = make_user(_name("fx_author"))
    commenter = make_user(_name("fx_commenter"))
    post = Post.objects.create(author=author, content="my run", visibility="public")

    def comment(text):
        made = api(commenter).post("/api/social/comments/",
                                   {"post": post.pk, "content": text},
                                   content_type="application/json")
        assert made.status_code == 201, made.content
        return made.json()['id']

    def preview(comment_id):
        return Notification.objects.get(
            recipient=author, event_type='comment_created',
            related_object_id=str(comment_id)).metadata['context']['preview']

    hidden_id = comment("slur number one")
    deleted_id = comment("slur two")
    assert preview(hidden_id) == "slur number one"

    # The operator hides one (the console saves row by row)...
    hidden = Comment.objects.get(pk=hidden_id)
    hidden.is_hidden = True
    hidden.save()
    assert preview(hidden_id) == ''
    # ...and its author deletes the other.
    assert api(commenter).delete(f"/api/social/comments/{deleted_id}/").status_code == 204
    assert preview(deleted_id) == ''

    body = api(author).get("/api/social/notifications/").json()
    rows = body['results'] if isinstance(body, dict) else body
    text = " ".join(r['message'] for r in rows)
    assert "slur" not in text, text
