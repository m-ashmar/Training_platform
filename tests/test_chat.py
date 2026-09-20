"""The trainer-client chat (chat/).

Each test names a rule from the owner's decision or from `chat/permissions.py`:

    the pair rule            only an approved trainer and client may open a thread
    someone else's thread    404, never 403
    idempotent send          a repeated `client_uuid` returns the FIRST message
    unread counts            what the other side wrote since I last opened it
    frozen coaching          READABLE, and the send refused with the coaching's code
    the tombstone            a deleted message keeps its place and loses its text
    the socket               connect, send, and both ends hear `message.new`
    the push                 a `Notification` ROW, not a mock that was called
    the list's query count   fixed, whatever the page holds
"""
import json
import uuid
from datetime import timedelta
from decimal import Decimal

import pytest
from asgiref.sync import async_to_sync
from django.db import connection
from django.test.utils import CaptureQueriesContext, override_settings
from django.utils import timezone

pytestmark = pytest.mark.django_db

CHAT = '/api/v1/chat'


# --------------------------------------------------------------------------- setup
@pytest.fixture
def pair(make_user):
    """An approved trainer and client, with no coaching month bought yet.

    No `CoachingPeriod` at all means `refusals_for` has nothing to refuse — coaching
    sold as one fee, or not yet charged — which is the state every test that is not
    about money wants.
    """
    from users.models import TrainerClientRelation

    trainer = make_user("chat_trainer", user_type="trainer")
    client = make_user("chat_client", user_type="client")
    TrainerClientRelation.objects.create(trainer=trainer, client=client,
                                         status="approved", initiated_by="client")
    return trainer, client


@pytest.fixture
def conversation(pair):
    from chat.services import open_conversation

    return open_conversation(*pair)


def _send(api, user, conversation, body="hello", **extra):
    payload = {'body': body, **extra}
    return api(user).post(f"{CHAT}/conversations/{conversation.pk}/messages/",
                          payload, content_type="application/json")


# ----------------------------------------------------------------------- the pair
def test_only_an_approved_pair_can_open_a_conversation(make_user, api, pair):
    """The unique constraint is the pair rule at the database, and `may_message` is the
    same rule at the door. A trainer with no approved relation gets the answer a
    nonexistent user id gets, so this cannot be walked to find out who is here."""
    trainer, client = pair
    stranger = make_user("chat_stranger_t", user_type="trainer")

    opened = api(trainer).post(f"{CHAT}/conversations/start/", {'client_id': client.pk},
                               content_type="application/json")
    assert opened.status_code == 200, opened.content
    first = opened.json()['conversation']['id']

    # From the other side: the SAME conversation, not a second one.
    again = api(client).post(f"{CHAT}/conversations/start/", {'trainer_id': trainer.pk},
                             content_type="application/json")
    assert again.status_code == 200 and again.json()['conversation']['id'] == first

    from chat.models import Conversation
    assert Conversation.objects.filter(trainer=trainer, client=client).count() == 1

    # Not their client, and a user id that does not exist: one answer for both.
    assert api(stranger).post(f"{CHAT}/conversations/start/", {'client_id': client.pk},
                              content_type="application/json").status_code == 404
    assert api(trainer).post(f"{CHAT}/conversations/start/", {'client_id': 99999999},
                             content_type="application/json").status_code == 404
    # Naming both sides, or neither, is the caller's mistake.
    assert api(trainer).post(f"{CHAT}/conversations/start/",
                             {'client_id': client.pk, 'trainer_id': trainer.pk},
                             content_type="application/json").status_code == 400


def test_the_database_refuses_a_second_conversation_for_one_pair(pair):
    """A constraint, not a convention: two phones tapping at once make one row."""
    from django.db import IntegrityError, transaction

    from chat.models import Conversation

    trainer, client = pair
    Conversation.objects.create(trainer=trainer, client=client)
    with pytest.raises(IntegrityError):
        with transaction.atomic():
            Conversation.objects.create(trainer=trainer, client=client)


def test_someone_elses_conversation_is_a_404_not_a_403(make_user, api, conversation):
    """A 403 would confirm the conversation exists and belongs to someone."""
    outsider = make_user("chat_outsider", user_type="client")
    for path, method in ((f"{CHAT}/conversations/{conversation.pk}/messages/", 'get'),
                         (f"{CHAT}/conversations/{conversation.pk}/read/", 'post')):
        resp = getattr(api(outsider), method)(path)
        assert resp.status_code == 404, f"{path} -> {resp.status_code}"
        assert resp.json()['code'] == 'not_found'
    resp = api(outsider).post(f"{CHAT}/conversations/{conversation.pk}/messages/",
                              {'body': 'hi'}, content_type="application/json")
    assert resp.status_code == 404
    # And a conversation that does not exist reads identically.
    assert api(outsider).get(f"{CHAT}/conversations/99999999/messages/").status_code == 404


# ------------------------------------------------------------------------- sending
def test_a_repeated_client_uuid_returns_the_first_message(api, pair, conversation):
    """A retry whose first attempt landed (the answer was lost) must not post twice."""
    trainer, client = pair
    key = str(uuid.uuid4())

    first = _send(api, trainer, conversation, "same message", client_uuid=key)
    assert first.status_code == 201, first.content

    retry = _send(api, trainer, conversation, "different text entirely", client_uuid=key)
    assert retry.status_code == 200, retry.content
    assert retry.json()['id'] == first.json()['id']
    assert retry.json()['body'] == "same message", "the retry overwrote the first message"

    from chat.models import Message
    assert Message.objects.filter(conversation=conversation).count() == 1

    # The same id from the OTHER person in the thread is a conflict, and says nothing.
    conflict = _send(api, client, conversation, "mine now", client_uuid=key)
    assert conflict.status_code == 409
    assert conflict.json()['code'] == 'idempotency_conflict'
    assert 'same message' not in conflict.content.decode()


def test_the_same_client_uuid_in_two_conversations_is_two_messages(make_user, api, pair,
                                                                   conversation):
    """The index is per conversation: one phone's counter cannot collide across threads."""
    from users.models import TrainerClientRelation

    from chat.services import open_conversation

    trainer, _client = pair
    other_client = make_user("chat_client_2", user_type="client")
    TrainerClientRelation.objects.create(trainer=trainer, client=other_client,
                                         status="approved", initiated_by="client")
    second = open_conversation(trainer, other_client)

    key = str(uuid.uuid4())
    assert _send(api, trainer, conversation, "a", client_uuid=key).status_code == 201
    assert _send(api, trainer, second, "b", client_uuid=key).status_code == 201


def test_a_blank_message_is_refused_and_the_text_is_kept_as_typed(api, pair,
                                                                  conversation):
    """Capped, never rewritten: whitespace-only is a 400, and a message that has
    leading spaces keeps them."""
    trainer, _client = pair
    blank = _send(api, trainer, conversation, "   ")
    assert blank.status_code == 400
    assert blank.json()['field_errors']['body'][0]['code'] == 'blank_message'

    assert _send(api, trainer, conversation, "x" * 4001).status_code == 400

    kept = _send(api, trainer, conversation, "  spaced  out  ")
    assert kept.status_code == 201
    assert kept.json()['body'] == "  spaced  out  "


# ------------------------------------------------------------------- unread counts
def test_the_unread_count_is_what_the_other_side_wrote_since_i_looked(api, pair,
                                                                      conversation):
    trainer, client = pair
    for i in range(3):
        assert _send(api, trainer, conversation, f"msg {i}").status_code == 201

    def row(user):
        body = api(user).get(f"{CHAT}/conversations/").json()
        assert body['count'] == 1
        return body['results'][0]

    assert row(client)['unread_count'] == 3
    assert row(trainer)['unread_count'] == 0, "my own messages are not unread to me"
    assert row(client)['last_message']['body'] == "msg 2"
    assert row(client)['other']['id'] == trainer.pk

    read = api(client).post(f"{CHAT}/conversations/{conversation.pk}/read/")
    assert read.status_code == 200 and read.json()['unread_count'] == 0
    assert row(client)['unread_count'] == 0

    # And the trainer can now see it was read.
    from chat.models import Message
    assert not Message.objects.filter(conversation=conversation,
                                      read_at__isnull=True).exists()

    assert _send(api, trainer, conversation, "one more").status_code == 201
    assert row(client)['unread_count'] == 1


def test_a_tombstone_is_not_unread(api, pair, conversation):
    """Nobody should be nudged to go and read a message that was taken back."""
    trainer, client = pair
    sent = _send(api, trainer, conversation, "oops")
    assert api(trainer).delete(f"{CHAT}/messages/{sent.json()['id']}/").status_code == 200
    body = api(client).get(f"{CHAT}/conversations/").json()
    assert body['results'][0]['unread_count'] == 0


# ------------------------------------------------------------------ frozen coaching
@pytest.fixture
def frozen(pair):
    """The pair's coaching, frozen: the renewal found the wallet short.

    One settled month that has ENDED and carries `frozen_at`, which is exactly the row
    `users.coaching.refusals_for` reads as `coaching_frozen`.
    """
    from users.models import CoachingPeriod, TrainerClientRelation

    trainer, client = pair
    relation = TrainerClientRelation.objects.get(trainer=trainer, client=client)
    now = timezone.now()
    CoachingPeriod.objects.create(
        relation=relation, trainer=trainer, client=client, number=1,
        price=Decimal("100.00"), commission_rate=Decimal("0.2000"), status='settled',
        hold_reference=str(uuid.uuid4()),
        starts_at=now - timedelta(days=31), ends_at=now - timedelta(days=1),
        frozen_at=now - timedelta(days=1))
    return trainer, client


def test_a_frozen_coaching_can_still_be_read_and_cannot_be_written_to(api, frozen,
                                                                      conversation):
    """The owner, 2026-09-19: an unpaid relationship is never deleted and cannot be
    used. The thread stays there to read; the composer closes with the reason."""
    trainer, client = frozen

    for who in (trainer, client):
        history = api(who).get(f"{CHAT}/conversations/{conversation.pk}/messages/")
        assert history.status_code == 200, f"{who.username} lost the thread"
        row = api(who).get(f"{CHAT}/conversations/").json()['results'][0]
        assert row['can_send'] is False
        assert row['refusal'] == 'coaching_frozen'

    # Silence is mutual: neither side may write while the coaching is paused.
    for who in (client, trainer):
        refused = _send(api, who, conversation, "are you there?")
        assert refused.status_code == 403, refused.content
        assert refused.json()['code'] == 'coaching_frozen'
        assert refused.json()['trainer_id'] == trainer.pk

    from chat.models import Message
    assert not Message.objects.filter(conversation=conversation).exists()


def test_losing_the_relation_keeps_the_thread_readable(api, pair, conversation):
    """An ended coaching is not a deleted conversation. `no_relation` closes the
    composer; every old message is still there."""
    from users.models import TrainerClientRelation

    trainer, client = pair
    assert _send(api, trainer, conversation, "the last thing I said").status_code == 201
    TrainerClientRelation.objects.filter(trainer=trainer, client=client).delete()

    history = api(client).get(f"{CHAT}/conversations/{conversation.pk}/messages/")
    assert history.status_code == 200
    assert history.json()['results'][0]['body'] == "the last thing I said"

    row = api(client).get(f"{CHAT}/conversations/").json()['results'][0]
    assert row['can_send'] is False and row['refusal'] == 'no_relation'

    refused = _send(api, client, conversation, "hello?")
    assert refused.status_code == 403 and refused.json()['code'] == 'no_relation'


# ---------------------------------------------------------------------- the tombstone
def test_a_deleted_message_keeps_its_place_and_loses_its_text(api, pair, conversation):
    trainer, client = pair
    sent = _send(api, trainer, conversation, "delete me").json()
    kept = _send(api, trainer, conversation, "but not me").json()

    # Only the sender.
    wrong = api(client).delete(f"{CHAT}/messages/{sent['id']}/")
    assert wrong.status_code == 403 and wrong.json()['code'] == 'not_your_message'

    gone = api(trainer).delete(f"{CHAT}/messages/{sent['id']}/")
    assert gone.status_code == 200
    assert gone.json()['deleted'] is True and gone.json()['body'] == ''

    # Not a hard delete: the row is still in the thread, and the cursor does not move.
    from chat.models import Message
    row = Message.objects.get(pk=sent['id'])
    assert row.deleted_at is not None

    page = api(client).get(f"{CHAT}/conversations/{conversation.pk}/messages/").json()
    assert [m['id'] for m in page['results']] == [kept['id'], sent['id']]
    assert page['results'][1] == {**page['results'][1], 'body': '', 'deleted': True}

    # Deleting twice is not an error and changes nothing.
    twice = api(trainer).delete(f"{CHAT}/messages/{sent['id']}/")
    assert twice.status_code == 200
    assert twice.json()['deleted_at'] == gone.json()['deleted_at']


def test_history_pages_backwards_by_id(api, pair, conversation):
    trainer, _client = pair
    ids = [_send(api, trainer, conversation, f"m{i}").json()['id'] for i in range(5)]

    first = api(trainer).get(
        f"{CHAT}/conversations/{conversation.pk}/messages/?limit=2").json()
    assert [m['id'] for m in first['results']] == ids[-1:-3:-1]
    assert first['next_before'] == ids[-2]

    second = api(trainer).get(
        f"{CHAT}/conversations/{conversation.pk}/messages/"
        f"?limit=2&before={first['next_before']}").json()
    assert [m['id'] for m in second['results']] == [ids[2], ids[1]]

    last = api(trainer).get(
        f"{CHAT}/conversations/{conversation.pk}/messages/"
        f"?limit=2&before={second['next_before']}").json()
    assert [m['id'] for m in last['results']] == [ids[0]]
    assert last['next_before'] is None, "the start of the thread is not another page"

    bad = api(trainer).get(
        f"{CHAT}/conversations/{conversation.pk}/messages/?limit=9999")
    assert bad.status_code == 400 and bad.json()['code'] == 'validation_error'


# ------------------------------------------------------------------------ the push
def test_a_message_writes_the_push_to_the_recipients_inbox(api, pair, conversation):
    """The ROW, not a mock: this is the record the app's notification list reads."""
    from notifications.models import Notification

    trainer, client = pair
    assert _send(api, trainer, conversation, "warm up first").status_code == 201

    note = Notification.objects.filter(recipient=client,
                                       event_type='chat_message').first()
    assert note is not None, "no notification was written for the recipient"
    assert note.actor_id == trainer.pk
    assert note.related_object_id == str(conversation.pk)
    assert note.metadata['data']['type'] == 'chat_message'
    assert note.metadata['data']['conversation_id'] == str(conversation.pk)
    assert note.metadata['context']['preview'] == "warm up first"
    # The sender is never told about their own message.
    assert not Notification.objects.filter(recipient=trainer,
                                           event_type='chat_message').exists()


def test_a_burst_of_messages_is_one_push_until_the_thread_is_opened(api, pair,
                                                                    conversation):
    """The rule, stated once: tell someone only when they have opened the thread since
    the last time we told them."""
    from notifications.models import Notification

    trainer, client = pair

    def pushes():
        return Notification.objects.filter(recipient=client,
                                           event_type='chat_message').count()

    for i in range(5):
        assert _send(api, trainer, conversation, f"burst {i}").status_code == 201
    assert pushes() == 1, "a burst of five messages pushed more than once"

    assert api(client).post(f"{CHAT}/conversations/{conversation.pk}/read/"
                            ).status_code == 200
    assert _send(api, trainer, conversation, "and again").status_code == 201
    assert pushes() == 2, "reading the thread did not re-arm the nudge"

    # And through a STALE conversation object — which is what a WebSocket consumer
    # holds for the whole life of a socket. The read stamp has to come from the row,
    # not from whatever was loaded when the socket opened.
    from chat.services import send_message
    stale = conversation                      # loaded before either read above
    assert api(client).post(f"{CHAT}/conversations/{stale.pk}/read/").status_code == 200
    send_message(stale, trainer, {'body': 'from a long-lived socket'})
    assert pushes() == 3, "a stale in-memory conversation swallowed the next nudge"


def test_a_muted_recipient_still_gets_the_inbox_row(api, pair, conversation,
                                                    monkeypatch):
    """Settings govern DELIVERY; the inbox keeps the record (B-014)."""
    from notifications.models import Notification, UserNotificationPreference

    trainer, client = pair
    UserNotificationPreference.objects.create(user=client, event_type='chat_message',
                                              is_enabled=False)
    sent = []
    monkeypatch.setattr('notifications.channels.fcm.FCMChannel.send',
                        lambda notification: sent.append(notification))

    assert _send(api, trainer, conversation, "muted").status_code == 201
    assert Notification.objects.filter(recipient=client,
                                       event_type='chat_message').exists()
    assert sent == [], "a muted event type was still pushed"


# ---------------------------------------------------------------------- the socket
@pytest.fixture
def socket_keeps_the_test_connection(monkeypatch):
    """Stop `database_sync_to_async` closing the connection this test lives in.

    Channels sweeps "old" database connections either side of every
    `database_sync_to_async` call, which is right for a consumer that lives for hours
    and wrong inside a test: the one connection it finds is the one holding the test's
    transaction, and the next query raises "connection already closed". A test shim, not
    a change of behaviour — the consumers still call `database_sync_to_async`, and the
    sweep still runs in production.
    """
    monkeypatch.setattr('channels.db.close_old_connections', lambda: None)


@override_settings(CHANNEL_LAYERS={
    'default': {'BACKEND': 'channels.layers.InMemoryChannelLayer'}})
def test_the_socket_carries_a_message_to_both_ends(pair, conversation,
                                                   socket_keeps_the_test_connection):
    """The happy path: both sides connect, one sends over the socket, both hear it.

    Driven through `async_to_sync` rather than an async test so that
    `database_sync_to_async` runs its queries back on THIS thread — the one holding the
    test's transaction. A thread of its own would open a second connection and see an
    empty database.
    """
    from channels.routing import URLRouter
    from channels.testing import WebsocketCommunicator
    from rest_framework_simplejwt.tokens import RefreshToken

    import chat.routing
    from chat.models import Message

    trainer, client = pair
    application = URLRouter(chat.routing.websocket_urlpatterns)
    # Minted OUT here: issuing a token writes an OutstandingToken row, and a write from
    # inside the running loop is a SynchronousOnlyOperation.
    tokens = {u.pk: str(RefreshToken.for_user(u).access_token) for u in (trainer, client)}

    def socket(user):
        return WebsocketCommunicator(
            application, f"/ws/chat/{conversation.pk}/?token={tokens[user.pk]}")

    async def scenario():
        trainer_socket, client_socket = socket(trainer), socket(client)
        connected, _sub = await trainer_socket.connect()
        assert connected, "the trainer could not open the socket"
        connected, _sub = await client_socket.connect()
        assert connected, "the client could not open the socket"

        await client_socket.send_to(text_data=json.dumps(
            {'type': 'message.send', 'body': 'sent over the socket'}))

        heard = [json.loads(await s.receive_from(timeout=5))
                 for s in (client_socket, trainer_socket)]
        await trainer_socket.disconnect()
        await client_socket.disconnect()
        return heard

    heard = async_to_sync(scenario)()

    for frame in heard:
        assert frame['type'] == 'message.new', frame
        assert frame['message']['body'] == 'sent over the socket'
        assert frame['message']['sender'] == client.pk
    assert Message.objects.filter(conversation=conversation).count() == 1, \
        "the socket wrote the message more than once, or not at all"


@override_settings(CHANNEL_LAYERS={
    'default': {'BACKEND': 'channels.layers.InMemoryChannelLayer'}})
def test_the_socket_refuses_a_conversation_that_is_not_the_callers(
        make_user, conversation, socket_keeps_the_test_connection):
    """The same answer the REST 404 gives: a socket must not confirm the thread exists."""
    from channels.routing import URLRouter
    from channels.testing import WebsocketCommunicator
    from rest_framework_simplejwt.tokens import RefreshToken

    import chat.routing

    outsider = make_user("chat_ws_outsider", user_type="client")
    application = URLRouter(chat.routing.websocket_urlpatterns)
    token = str(RefreshToken.for_user(outsider).access_token)

    async def scenario():
        comm = WebsocketCommunicator(
            application, f"/ws/chat/{conversation.pk}/?token={token}")
        connected, code = await comm.connect()
        await comm.disconnect()
        return connected, code

    connected, code = async_to_sync(scenario)()
    assert connected is False and code == 4004


@override_settings(CHANNEL_LAYERS={
    'default': {'BACKEND': 'channels.layers.InMemoryChannelLayer'}})
def test_the_socket_refuses_a_send_the_rest_route_would_refuse(
        frozen, conversation, socket_keeps_the_test_connection):
    """One implementation of the rules: a frozen coaching closes both doors, and the
    socket answers with the same `code` the 403 carries."""
    from channels.routing import URLRouter
    from channels.testing import WebsocketCommunicator
    from rest_framework_simplejwt.tokens import RefreshToken

    import chat.routing
    from chat.models import Message

    _trainer, client = frozen
    application = URLRouter(chat.routing.websocket_urlpatterns)
    token = str(RefreshToken.for_user(client).access_token)

    async def scenario():
        comm = WebsocketCommunicator(
            application, f"/ws/chat/{conversation.pk}/?token={token}")
        connected, _sub = await comm.connect()
        assert connected, "a frozen coaching closed the thread instead of the composer"
        await comm.send_to(text_data=json.dumps(
            {'type': 'message.send', 'body': 'let me in'}))
        frame = json.loads(await comm.receive_from(timeout=5))
        await comm.disconnect()
        return frame

    frame = async_to_sync(scenario)()
    assert frame['type'] == 'error' and frame['code'] == 'coaching_frozen', frame
    assert not Message.objects.filter(conversation=conversation).exists()


@override_settings(CHANNEL_LAYERS={
    'default': {'BACKEND': 'channels.layers.InMemoryChannelLayer'}})
def test_the_socket_refuses_a_refresh_token(pair, conversation,
                                            socket_keeps_the_test_connection):
    """`training_platform.ws_auth`, not a second implementation: the token that logging
    out revokes must not open this socket either."""
    from channels.routing import URLRouter
    from channels.testing import WebsocketCommunicator
    from rest_framework_simplejwt.tokens import RefreshToken

    import chat.routing

    _trainer, client = pair
    application = URLRouter(chat.routing.websocket_urlpatterns)
    refresh = str(RefreshToken.for_user(client))

    async def scenario():
        comm = WebsocketCommunicator(
            application, f"/ws/chat/{conversation.pk}/?token={refresh}")
        connected, code = await comm.connect()
        await comm.disconnect()
        return connected, code

    connected, code = async_to_sync(scenario)()
    assert connected is False and code == 4001


# ------------------------------------------------------------------- query budget
def test_the_conversation_list_costs_the_same_at_any_size(make_user, api, pair):
    """No N+1: the last message of every row and every refusal are read in bulk."""
    from users.models import TrainerClientRelation

    from chat.services import open_conversation, send_message

    trainer, first_client = pair

    def add_conversations(n, start):
        for i in range(n):
            other = make_user(f"chat_bulk_{start + i}", user_type="client")
            TrainerClientRelation.objects.create(trainer=trainer, client=other,
                                                 status="approved",
                                                 initiated_by="client")
            conversation = open_conversation(trainer, other)
            send_message(conversation, trainer, {'body': f"hello {start + i}"})

    # The chat's own tables only. Authentication and the language middleware read the
    # user row and are cached after the first request, so counting EVERY query measures
    # the cache warming up rather than this endpoint's budget.
    tables = ('chat_conversation', 'chat_message', 'users_trainerclientrelation',
              'users_coachingperiod')

    def count_queries():
        with CaptureQueriesContext(connection) as ctx:
            resp = api(trainer).get(f"{CHAT}/conversations/")
            assert resp.status_code == 200, resp.content
        return [q['sql'] for q in ctx.captured_queries
                if any(t in q['sql'] for t in tables)]

    open_conversation(trainer, first_client)
    add_conversations(2, 0)
    small = count_queries()
    add_conversations(6, 2)
    large = count_queries()

    assert len(large) == len(small), (
        f"the conversation list costs {len(small)} queries for 3 rows and "
        f"{len(large)} for 9:\n" + "\n".join(large))
