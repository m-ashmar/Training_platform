"""The owner, 2026-11-24: a password change signs every device out AT ONCE.

Before, `users/utils.py end_every_session` blacklisted the refresh tokens and every
other device kept its ACCESS token for up to an hour (`ACCESS_TOKEN_LIFETIME`), on
every route, a cached one and an open socket included. Now each token carries a digest
of the password hash it was made under (simplejwt's `CHECK_REVOKE_TOKEN`), checked by
`training_platform/jwt_sessions.py`:

  the password change, the forgot-password reset, the console's reset: another device's
      access token answers 401 `password_changed` on its next request
  a route the response cache serves: refused too, not served from the cache
  an open socket: closed at the change (4001), not at its token's expiry
  the console's deactivation: the same at once, sockets included
  the deploy: a token from before it (no digest) still works, and its refresh gains one

Each test here fails on the code before (the deploy-day ones because no token carried a
digest; they hold the line that the deploy signs nobody out).
"""
import asyncio
import json
import uuid

import pytest
from asgiref.sync import async_to_sync, sync_to_async
from django.test import override_settings

pytestmark = pytest.mark.django_db

JSON = "application/json"
OLD, NEW = "Xx!23456", "Brand-new-pass-91"
CHANGE = "/api/auth/password/change/"
ME = "/api/auth/user/details/"
CACHED = "/api/routine/exercises/"
REFRESH = "/api/auth/token/refresh/"


def _name(tag):
    return f"se_{tag}_{uuid.uuid4().hex[:6]}"


def _sign_in(api, user):
    """Another device: a real sign-in, its own pair."""
    resp = api().post("/api/auth/token/", {"email": user.email, "password": OLD},
                      content_type=JSON)
    assert resp.status_code == 200, resp.content
    return resp.json()


def _bearer(client, access):
    client.defaults["HTTP_AUTHORIZATION"] = f"Bearer {access}"
    return client


def _with(api, access):
    return _bearer(api(), access)


@pytest.fixture
def person(make_user):
    return make_user(_name("p"), email_verified=True)


# ------------------------------------------------------------------ the password change
def test_the_other_devices_access_token_is_refused_right_after_the_change(
        api, person, django_capture_on_commit_callbacks):
    other = _sign_in(api, person)
    assert _with(api, other["access"]).get(ME).status_code == 200
    here = api(person)

    with django_capture_on_commit_callbacks(execute=True):
        changed = here.post(CHANGE, {"current_password": OLD, "new_password": NEW},
                            content_type=JSON)
    assert changed.status_code == 200, changed.content
    fresh = changed.json()

    refused = _with(api, other["access"]).get(ME)
    assert refused.status_code == 401, refused.content
    assert refused.json()["code"] == "password_changed"
    # This device's own old access token too: the app stores the fresh pair.
    assert here.get(ME).status_code == 401
    # The fresh pair works, and so does its refresh.
    assert _with(api, fresh["access"]).get(ME).status_code == 200
    renewed = api().post(REFRESH, {"refresh": fresh["refresh"]}, content_type=JSON)
    assert renewed.status_code == 200, renewed.content
    assert _with(api, renewed.json()["access"]).get(ME).status_code == 200
    # The other device's refresh token is dead as before.
    assert api().post(REFRESH, {"refresh": other["refresh"]},
                      content_type=JSON).status_code == 401


def test_a_cached_route_refuses_the_old_token_at_once(
        api, person, django_capture_on_commit_callbacks):
    other = _with(api, _sign_in(api, person)["access"])
    assert other.get(CACHED).status_code == 200            # warm the entry and the memo
    assert other.get(CACHED).status_code == 200            # served from the cache

    with django_capture_on_commit_callbacks(execute=True):
        assert api(person).post(CHANGE, {"current_password": OLD, "new_password": NEW},
                                content_type=JSON).status_code == 200

    assert other.get(CACHED).status_code == 401, "the old token was served the cached body"


def test_the_forgot_password_reset_refuses_every_old_access_token(
        api, person, django_capture_on_commit_callbacks):
    from users.models import PasswordResetToken

    other = _sign_in(api, person)
    _token, raw = PasswordResetToken.issue(person, minutes=15)
    with django_capture_on_commit_callbacks(execute=True):
        done = api().post("/api/auth/forgot-password/confirm/",
                          {"reset_token": raw, "new_password": NEW}, content_type=JSON)
    assert done.status_code == 200, done.content

    refused = _with(api, other["access"]).get(ME)
    assert (refused.status_code, refused.json()["code"]) == (401, "password_changed")
    assert api().post(REFRESH, {"refresh": other["refresh"]},
                      content_type=JSON).status_code == 401


def test_the_consoles_reset_refuses_every_old_access_token(make_user, api, person):
    from django.contrib.messages.storage.fallback import FallbackStorage
    from django.contrib.sessions.middleware import SessionMiddleware
    from django.test import RequestFactory

    from admin_dashboard.admin import admin_site
    from users.models import CustomUser

    other = _sign_in(api, person)
    request = RequestFactory().post("/", {"_reset_password": "1"})
    SessionMiddleware(lambda r: None).process_request(request)
    request.user = make_user(_name("op"), user_type="admin")
    request._messages = FallbackStorage(request)
    admin_site._registry[CustomUser].response_change(request, person)

    refused = _with(api, other["access"]).get(ME)
    assert (refused.status_code, refused.json()["code"]) == (401, "password_changed")


# ------------------------------------------------------------------ the deploy
def _without_digest(token):
    """A token as every token was before the deploy: no `hash_password`."""
    del token.payload["hash_password"]
    return token


def test_a_token_from_before_the_deploy_still_works(api, person):
    from rest_framework_simplejwt.tokens import AccessToken

    assert _with(api, str(_without_digest(AccessToken.for_user(person)))).get(
        ME).status_code == 200


def test_refreshing_a_token_from_before_the_deploy_gives_it_the_digest(api, person):
    from rest_framework_simplejwt.tokens import AccessToken, RefreshToken

    legacy = _without_digest(RefreshToken.for_user(person))
    renewed = api().post(REFRESH, {"refresh": str(legacy)}, content_type=JSON)
    assert renewed.status_code == 200, renewed.content
    body = renewed.json()
    assert AccessToken(body["access"]).get("hash_password")
    assert RefreshToken(body["refresh"]).get("hash_password")
    assert _with(api, body["access"]).get(ME).status_code == 200


# ------------------------------------------------------------------ the sockets
@pytest.fixture
def socket_keeps_the_test_connection(monkeypatch):
    """Channels' connection sweep would close the test's own connection (tests/test_chat.py)."""
    monkeypatch.setattr('channels.db.close_old_connections', lambda: None)


def _conversation(make_user):
    from chat.services import open_conversation
    from users.models import TrainerClientRelation

    trainer = make_user(_name("t"), user_type="trainer")
    client = make_user(_name("c"), email_verified=True)
    TrainerClientRelation.objects.create(trainer=trainer, client=client, status="approved",
                                         initiated_by="client")
    return client, trainer, open_conversation(trainer, client)


def _listening_socket_after(make_user, end_the_session):
    """A chat socket that only listens; `end_the_session(client, trainer)` runs while it
    is open. Answers what the socket received after, and whether the trainer's socket,
    the other side of the thread, stayed open."""
    from channels.routing import URLRouter
    from channels.testing import WebsocketCommunicator
    from rest_framework_simplejwt.tokens import AccessToken

    import chat.routing

    client, trainer, conversation = _conversation(make_user)
    router = URLRouter(chat.routing.websocket_urlpatterns)
    mine = WebsocketCommunicator(router, f"/ws/chat/{conversation.pk}/?token="
                                         f"{AccessToken.for_user(client)}")
    theirs = WebsocketCommunicator(router, f"/ws/chat/{conversation.pk}/?token="
                                           f"{AccessToken.for_user(trainer)}")

    async def scenario():
        assert (await mine.connect())[0] and (await theirs.connect())[0]
        await sync_to_async(end_the_session)(client, trainer)
        said = json.loads(await mine.receive_from(timeout=3))
        closed = await mine.receive_output(timeout=3)
        await asyncio.sleep(0.2)
        other_open = await theirs.receive_nothing(timeout=0.5)
        await theirs.disconnect()
        await mine.disconnect()
        return said, closed, other_open

    return async_to_sync(scenario)()


@override_settings(CHANNEL_LAYERS={
    'default': {'BACKEND': 'channels.layers.InMemoryChannelLayer'}})
def test_a_listening_chat_socket_closes_at_the_password_change(
        make_user, api, socket_keeps_the_test_connection,
        django_capture_on_commit_callbacks):
    from users.password_views import PasswordChangeView  # noqa: F401  (the route below)

    def change(client, _trainer):
        with django_capture_on_commit_callbacks(execute=True):
            resp = api(client).post(CHANGE, {"current_password": OLD,
                                             "new_password": NEW}, content_type=JSON)
        assert resp.status_code == 200, resp.content

    said, closed, other_open = _listening_socket_after(make_user, change)
    assert (said["type"], said["code"]) == ("error", "auth"), said
    assert closed == {"type": "websocket.close", "code": 4001}, closed
    assert other_open, "the other person's socket was closed too"


@override_settings(CHANNEL_LAYERS={
    'default': {'BACKEND': 'channels.layers.InMemoryChannelLayer'}})
def test_a_listening_chat_socket_closes_when_the_console_deactivates_the_account(
        make_user, socket_keeps_the_test_connection, django_capture_on_commit_callbacks):
    from django.contrib.messages.storage.fallback import FallbackStorage
    from django.contrib.sessions.middleware import SessionMiddleware
    from django.test import RequestFactory

    from users.admin import deactivate_users
    from users.models import CustomUser

    def deactivate(client, _trainer):
        request = RequestFactory().post("/")
        SessionMiddleware(lambda r: None).process_request(request)
        request.user = make_user(_name("op"), user_type="admin")
        request._messages = FallbackStorage(request)
        with django_capture_on_commit_callbacks(execute=True):
            deactivate_users(None, request, CustomUser.objects.filter(pk=client.pk))

    said, closed, other_open = _listening_socket_after(make_user, deactivate)
    assert (said["type"], said["code"]) == ("error", "auth"), said
    assert closed == {"type": "websocket.close", "code": 4001}, closed
    assert other_open


@override_settings(CHANNEL_LAYERS={
    'default': {'BACKEND': 'channels.layers.InMemoryChannelLayer'}})
def test_a_socket_opened_with_the_fresh_pair_stays_open(
        make_user, api, socket_keeps_the_test_connection,
        django_capture_on_commit_callbacks):
    """The change's own `session.ended` reaches every socket of the account; one whose
    token was made under the NEW password re-checks it and stays."""
    from channels.routing import URLRouter
    from channels.testing import WebsocketCommunicator

    import chat.routing
    from training_platform.ws_auth import end_open_sockets

    client, _trainer, conversation = _conversation(make_user)
    with django_capture_on_commit_callbacks(execute=False):
        fresh = api(client).post(CHANGE, {"current_password": OLD, "new_password": NEW},
                                 content_type=JSON).json()
    comm = WebsocketCommunicator(URLRouter(chat.routing.websocket_urlpatterns),
                                 f"/ws/chat/{conversation.pk}/?token={fresh['access']}")

    async def scenario():
        assert (await comm.connect())[0]
        await sync_to_async(end_open_sockets)(client.pk)
        stayed = await comm.receive_nothing(timeout=0.5)
        await comm.disconnect()
        return stayed

    assert async_to_sync(scenario)()


# ------------------------------------------- the integrator's review of 7307f94
def test_a_refresh_token_from_before_the_deploy_is_refused_after_the_change(
        api, person, django_capture_on_commit_callbacks):
    """A refresh token with no digest is accepted, and given one, by the refresh
    (`SessionTokenRefreshSerializer`) — so after a password change the only thing that
    refuses one is the blacklist `end_every_session` writes. Pinned here."""
    from rest_framework_simplejwt.tokens import RefreshToken

    legacy = str(_without_digest(RefreshToken.for_user(person)))
    with django_capture_on_commit_callbacks(execute=True):
        assert api(person).post(CHANGE, {"current_password": OLD, "new_password": NEW},
                                content_type=JSON).status_code == 200

    refused = api().post(REFRESH, {"refresh": legacy}, content_type=JSON)
    assert refused.status_code == 401, refused.content
    assert "access" not in refused.json() and "refresh" not in refused.json()


def test_a_memo_written_after_the_change_is_not_believed(api, person, monkeypatch):
    """The cache memo's race: a cached request reads the row BEFORE the change
    commits and writes its memo AFTER the commit dropped it. That memo holds the old
    digest; it carries the old session generation, so the next request reads the row
    again and refuses the dead token instead of serving it for a minute."""
    from training_platform.cache import private_cache
    from training_platform.middleware import account_memo_key
    from users.utils import _after_the_session_ended

    other = _with(api, _sign_in(api, person)["access"])
    assert other.get(CACHED).status_code == 200            # the entry is cached
    cache = private_cache()
    cache.delete(account_memo_key(person.pk))               # the memo's minute is over

    changed, real_set = [], cache.set

    def set_after_the_change(key, value, *args, **kwargs):
        if key == account_memo_key(person.pk) and not changed:
            # The change commits between this request's read of the row and its write.
            changed.append(value)
            person.set_password(NEW)
            person.save(update_fields=["password"])
            _after_the_session_ended(person.pk)
        return real_set(key, value, *args, **kwargs)

    monkeypatch.setattr(cache, "set", set_after_the_change)
    other.get(CACHED)                    # began before the change: it may be answered
    monkeypatch.undo()
    assert changed, "the interleaving did not happen"

    assert other.get(CACHED).status_code == 401, "the late memo served the dead token"


@override_settings(CHANNEL_LAYERS={
    'default': {'BACKEND': 'channels.layers.InMemoryChannelLayer'}})
def test_a_socket_that_joins_after_the_change_committed_is_refused(
        make_user, api, socket_keeps_the_test_connection,
        django_capture_on_commit_callbacks, monkeypatch):
    """The handshake's race: the socket authenticates in `connect()`, the change
    commits and tells the session group, and only then does the socket join it — so
    it never hears `session.ended`. The join asks the credential once more, after it is
    in the group, and the socket is refused (4001)."""
    from channels.routing import URLRouter
    from channels.testing import WebsocketCommunicator
    from rest_framework_simplejwt.tokens import AccessToken

    import chat.routing
    from chat.consumers import ChatConsumer

    client, _trainer, conversation = _conversation(make_user)
    token = AccessToken.for_user(client)

    def change():
        with django_capture_on_commit_callbacks(execute=True):
            resp = api(client).post(CHANGE, {"current_password": OLD,
                                             "new_password": NEW}, content_type=JSON)
        assert resp.status_code == 200, resp.content

    real_join = ChatConsumer.join_session_group

    async def change_then_join(self):
        await sync_to_async(change)()     # after connect's authentication, before the join
        return await real_join(self)

    monkeypatch.setattr(ChatConsumer, "join_session_group", change_then_join)
    comm = WebsocketCommunicator(URLRouter(chat.routing.websocket_urlpatterns),
                                 f"/ws/chat/{conversation.pk}/?token={token}")

    async def scenario():
        # The change runs inside the handshake, three password hashes included.
        connected, code = await comm.connect(timeout=10)
        if connected:
            await comm.disconnect()
        return connected, code

    assert async_to_sync(scenario)() == (False, 4001)


@override_settings(CHANNEL_LAYERS={
    'default': {'BACKEND': 'channels.layers.InMemoryChannelLayer'}})
def test_the_other_persons_socket_in_the_chat_stays_open_at_the_change(
        make_user, api, socket_keeps_the_test_connection,
        django_capture_on_commit_callbacks):
    """One person's password change closes THEIR sockets only: the other person in the
    same chat keeps theirs, and the thread keeps reaching it."""
    from channels.layers import get_channel_layer
    from channels.routing import URLRouter
    from channels.testing import WebsocketCommunicator
    from rest_framework_simplejwt.tokens import AccessToken

    import chat.routing
    from chat.events import group_name

    client, trainer, conversation = _conversation(make_user)
    router = URLRouter(chat.routing.websocket_urlpatterns)
    mine = WebsocketCommunicator(router, f"/ws/chat/{conversation.pk}/?token="
                                         f"{AccessToken.for_user(client)}")
    theirs = WebsocketCommunicator(router, f"/ws/chat/{conversation.pk}/?token="
                                           f"{AccessToken.for_user(trainer)}")

    def change():
        with django_capture_on_commit_callbacks(execute=True):
            resp = api(client).post(CHANGE, {"current_password": OLD,
                                             "new_password": NEW}, content_type=JSON)
        assert resp.status_code == 200, resp.content

    event = {'type': 'message.new', 'message': {'id': 1, 'body': 'still here'}}

    async def scenario():
        assert (await mine.connect())[0] and (await theirs.connect())[0]
        await sync_to_async(change)()
        closed_frame = json.loads(await mine.receive_from(timeout=3))
        closed = await mine.receive_output(timeout=3)
        await get_channel_layer().group_send(group_name(conversation.pk), event)
        reached = json.loads(await theirs.receive_from(timeout=3))
        await theirs.disconnect()
        await mine.disconnect()
        return closed_frame, closed, reached

    closed_frame, closed, reached = async_to_sync(scenario)()
    assert (closed_frame["type"], closed_frame["code"]) == ("error", "auth")
    assert closed == {"type": "websocket.close", "code": 4001}
    assert (reached["type"], reached["message"]["body"]) == ("message.new", "still here")
