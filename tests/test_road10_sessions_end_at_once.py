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
