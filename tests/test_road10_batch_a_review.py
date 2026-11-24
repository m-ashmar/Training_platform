"""Road to 10 batch A, the adversarial review (2026-11-23): one test per confirmed finding,
each failing on the code before its fix.

  S2.8  a new password typed with spaces around it could never sign in again
  S2.8  guesses sent at once all passed the limit of five (the race itself is in
        tests/test_zz_road10_s28_guessing.py; here, that a right password costs no slot)
  S2.8  "every other session ends": a chat socket that only listened outlived its token
        (and so a password change and a logout), because only frames IN were re-checked
  S2.1  a request Home no longer shows (trainer suspended, or a block between them) kept
        refusing every next request with `request_pending_elsewhere`
"""
import asyncio
import json
import uuid
from datetime import timedelta
from decimal import Decimal

import pytest
from asgiref.sync import async_to_sync
from django.test.utils import override_settings

pytestmark = pytest.mark.django_db

JSON = "application/json"
CHANGE = "/api/auth/password/change/"
OLD, NEW = "Xx!23456", "Brand-new-pass-91"


def _name(tag):
    return f"r10a_{tag}_{uuid.uuid4().hex[:6]}"


# ------------------------------------------------------------------ S2.8, spaces
def test_s28_a_password_typed_with_spaces_signs_in_as_typed(make_user, api):
    """Every sign-in route trims the password it is given (DRF's default); the change
    route stored the spaces. The person was then locked out of signing in with a
    password: the trimmed string was not the stored one, and the untrimmed one was
    trimmed on the way in."""
    person = make_user(_name("spaces"), email_verified=True)
    typed = f"  {NEW} "
    changed = api(person).post(CHANGE, {"current_password": f" {OLD}  ",
                                        "new_password": typed}, content_type=JSON)
    assert changed.status_code == 200, changed.content

    for route in ("/api/auth/token/", "/api/auth/login/"):
        signed_in = api().post(route, {"email": person.email, "password": typed},
                               content_type=JSON)
        assert signed_in.status_code == 200, (route, signed_in.content)
    person.refresh_from_db()
    assert person.check_password(NEW)


# ------------------------------------------------------------------ S2.8, the limit
def test_s28_a_right_current_password_does_not_use_up_the_limit(make_user, api):
    """Each attempt now takes its slot before the check; one whose current password was
    right gives it back, so only wrong guesses count."""
    from training_platform.cache import ratelimit_cache

    ratelimit_cache().clear()
    person = make_user(_name("limit"), email_verified=True)
    try:
        for _ in range(4):
            assert api(person).post(CHANGE, {"current_password": "guess",
                                             "new_password": NEW},
                                    content_type=JSON).json()["code"] == \
                "current_password_wrong"
        for _ in range(3):
            same = api(person).post(CHANGE, {"current_password": OLD, "new_password": OLD},
                                    content_type=JSON)
            assert same.json()["code"] == "password_unchanged"
        fifth = api(person).post(CHANGE, {"current_password": "guess", "new_password": NEW},
                                 content_type=JSON)
        assert fifth.json()["code"] == "current_password_wrong"
        sixth = api(person).post(CHANGE, {"current_password": OLD, "new_password": NEW},
                                 content_type=JSON)
        assert (sixth.status_code, sixth.json()["code"]) == (429, "throttled")
    finally:
        ratelimit_cache().clear()


# ------------------------------------------------------------------ S2.8, the socket
@pytest.fixture
def socket_keeps_the_test_connection(monkeypatch):
    """Channels' connection sweep would close the test's own connection (tests/test_chat.py)."""
    monkeypatch.setattr('channels.db.close_old_connections', lambda: None)


@override_settings(CHANNEL_LAYERS={
    'default': {'BACKEND': 'channels.layers.InMemoryChannelLayer'}})
def test_s28_a_listening_chat_socket_closes_once_its_token_expires(
        make_user, socket_keeps_the_test_connection):
    from channels.layers import get_channel_layer
    from channels.routing import URLRouter
    from channels.testing import WebsocketCommunicator
    from rest_framework_simplejwt.tokens import AccessToken

    import chat.routing
    from chat.events import group_name
    from chat.services import open_conversation
    from users.models import TrainerClientRelation

    trainer = make_user(_name("ws_t"), user_type="trainer")
    client = make_user(_name("ws_c"))
    TrainerClientRelation.objects.create(trainer=trainer, client=client, status="approved",
                                         initiated_by="client")
    conversation = open_conversation(trainer, client)
    token = AccessToken.for_user(client)
    token.set_exp(lifetime=timedelta(seconds=3))
    comm = WebsocketCommunicator(URLRouter(chat.routing.websocket_urlpatterns),
                                 f"/ws/chat/{conversation.pk}/?token={token}")
    event = {'type': 'message.new', 'message': {'id': 1, 'body': 'for the client only'}}

    async def scenario():
        connected, _sub = await comm.connect()
        assert connected
        layer = get_channel_layer()
        await layer.group_send(group_name(conversation.pk), event)
        live = json.loads(await comm.receive_from(timeout=3))
        await asyncio.sleep(3.5)          # the token has expired; the socket never sent
        await layer.group_send(group_name(conversation.pk), event)
        after = json.loads(await comm.receive_from(timeout=3))
        closed = await comm.receive_output(timeout=3)
        await comm.disconnect()
        return live, after, closed

    live, after, closed = async_to_sync(scenario)()
    assert live["type"] == "message.new"
    assert (after["type"], after["code"]) == ("error", "auth"), after
    assert closed == {"type": "websocket.close", "code": 4001}, closed


# ------------------------------------------------------------------ S2.1
def _home(api, person):
    from training_platform.cache import private_cache

    private_cache().clear()
    body = api(person).get("/api/v1/home", HTTP_X_APP_CHANNEL="apk").json()
    return {s["type"]: s["data"] for s in body["sections"]}


def _coach(make_user, tag):
    return make_user(_name(tag), user_type="trainer", first_name="Rami",
                     email_verified=True, trainer_is_available=True,
                     trainer_hourly_rate=Decimal("0"), trainer_goal_focus=["build_muscle"])


@pytest.mark.parametrize("how", ["client_blocked", "trainer_blocked", "suspended"])
def test_s21_a_request_nobody_can_answer_gives_way_to_the_next(make_user, api, how):
    from moderation.models import Block
    from users.models import CustomUser, TrainerClientRelation

    first, second = _coach(make_user, "first"), _coach(make_user, "second")
    client = make_user(_name("c21"), training_goal="build_muscle")
    assert api(client).post("/api/auth/client/request-trainer/", {"trainer_id": first.pk},
                            content_type=JSON).status_code == 200
    if how == "client_blocked":
        Block.objects.create(blocker=client, blocked=first)
    elif how == "trainer_blocked":
        Block.objects.create(blocker=first, blocked=client)
    else:
        CustomUser.objects.filter(pk=first.pk).update(is_active=False)
    # Home stops showing it (S2.1, unchanged)...
    assert "step" not in _home(api, client)["find_trainer_hero"]

    # ...and it no longer holds the client back.
    sent = api(client).post("/api/auth/client/request-trainer/", {"trainer_id": second.pk},
                            content_type=JSON)
    assert sent.status_code == 200, sent.content
    assert not TrainerClientRelation.objects.filter(trainer=first, client=client).exists()
    hero = _home(api, client)["find_trainer_hero"]
    assert (hero["step"], hero["trainer"]["id"]) == ("waiting", second.pk)


def test_s21_a_request_still_waiting_elsewhere_still_refuses(make_user, api):
    first, second = _coach(make_user, "live1"), _coach(make_user, "live2")
    client = make_user(_name("c21live"), training_goal="build_muscle")
    assert api(client).post("/api/auth/client/request-trainer/", {"trainer_id": first.pk},
                            content_type=JSON).status_code == 200
    again = api(client).post("/api/auth/client/request-trainer/", {"trainer_id": second.pk},
                             content_type=JSON)
    assert (again.status_code, again.json()["code"]) == (400, "request_pending_elsewhere")


# ------------------------------------------------------------------ S2.8, no password
def test_s28_an_account_with_no_password_is_told_so_and_never_throttled(make_user, api):
    """An operator's reset (or an account made without a password) leaves nothing to
    check: every attempt answered `current_password_wrong` and the sixth was a 429
    (Road to 10 round 4). Now the plain code, every time, counted against nothing."""
    from training_platform.cache import ratelimit_cache
    from users.password_views import _wrong_count

    ratelimit_cache().clear()
    person = make_user(_name("nopass"), email_verified=True)
    person.set_unusable_password()
    person.save(update_fields=["password"])
    try:
        for _ in range(7):
            resp = api(person).post(CHANGE, {"current_password": "anything",
                                             "new_password": NEW}, content_type=JSON)
            assert (resp.status_code, resp.json()["code"]) == (400, "password_not_set")
        assert _wrong_count(person.pk) == 0
        person.refresh_from_db()
        assert not person.has_usable_password()
    finally:
        ratelimit_cache().clear()


# ------------------------------------------------------------------ the console's reset
def test_s28_the_operators_password_reset_ends_every_session(make_user, api):
    """The console's reset made the password unusable and left every refresh token
    alive: each signed-in device stayed signed in for up to seven days (Road to 10
    round 4). Now the same revocation the password change makes."""
    from django.contrib.admin.sites import site as stock_site
    from django.contrib.messages.storage.fallback import FallbackStorage
    from django.contrib.sessions.middleware import SessionMiddleware
    from django.test import RequestFactory

    from admin_dashboard.admin import admin_site
    from users.models import CustomUser

    person = make_user(_name("reset"), email_verified=True)
    phones = [api().post("/api/auth/token/", {"email": person.email, "password": OLD},
                         content_type=JSON).json()["refresh"] for _ in range(2)]
    operator = make_user(_name("resetop"), user_type="admin")

    for site in (admin_site, stock_site):
        request = RequestFactory().post("/", {"_reset_password": "1"})
        SessionMiddleware(lambda r: None).process_request(request)
        request.user = operator
        request._messages = FallbackStorage(request)
        site._registry[CustomUser].response_change(request, person)
        person.refresh_from_db()
        assert not person.has_usable_password()

    for refresh in phones:
        dead = api().post("/api/auth/token/refresh/", {"refresh": refresh},
                          content_type=JSON)
        assert dead.status_code == 401, dead.content
