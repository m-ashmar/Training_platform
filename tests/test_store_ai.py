"""The AI coach on a store build, and the medical disclaimers.

Spec §9 B2/B4 and §10 ("a support bot that answers 'how do I pay' with instructions
inside a store build" never ships), and PAYMENTS_COMPLIANCE_PLAN step 2 follow-up (a):
the AI coach must decline "how do I pay" on `play`/`ios`, which needs the channel on
the WebSocket handshake.

1. The build reaches the coach: the socket reads `X-App-Channel` / `X-App-Version`
   from its handshake (headers, or `?app_channel=&app_version=` for a client that
   cannot set headers) exactly as `policy.channels` reads them on REST; a socket that
   declares nothing gets policy's strictest answer. The REST history reads the same
   headers.
2. On a build that may not sell the store-billed goods, a payment / price / purchase
   question (Arabic, dialect, Arabizi, English) is answered by a fixed, store-safe
   reply WITHOUT calling the model; the system prompt carries the store rule for
   anything the recogniser misses; the model's own reply is never edited. The same
   question on `apk` / `web` reaches the model. Identical for every user of a build.
3. A bilingual medical disclaimer rides every AI coach reply (socket and REST) and the
   nutrition onboarding and generation responses.
"""
import asyncio
import json
import re
import uuid
from types import SimpleNamespace

import pytest
from asgiref.sync import async_to_sync
from django.test.utils import override_settings
from django.utils import translation

from tests.test_fx_people import _ai_subscriber

pytestmark = pytest.mark.django_db

JSON = "application/json"
ARABIC = re.compile(r"[؀-ۿ]")
IN_MEMORY_CHANNELS = override_settings(
    CHANNEL_LAYERS={"default": {"BACKEND": "channels.layers.InMemoryChannelLayer"}})

#: What a store-safe answer must never carry (spec §3 "Not sold in app", §10).
FORBIDDEN_IN_A_STORE_REPLY = re.compile(
    r"https?://|www\.|\.com|\d|\$|syp|ليرة|ل\.س|website|web site|الموقع|subscribe on|"
    r"shamcash|شام كاش|agent|وكيل|transfer|حوالة|تحويل", re.IGNORECASE)


@pytest.fixture
def socket_keeps_the_test_connection(monkeypatch):
    """Channels closes "old" connections around `database_sync_to_async`; in a test the
    one it finds is the one holding the open transaction (tests/test_side_bugs_round1)."""
    monkeypatch.setattr("channels.db.close_old_connections", lambda: None)


def _token(user):
    from rest_framework_simplejwt.tokens import RefreshToken

    return str(RefreshToken.for_user(user).access_token)


class _Model:
    """The model: records every call and answers one fixed sentence."""

    def __init__(self, answer="Three sets of eight, rest ninety seconds."):
        self.sent, self.answer = [], answer

    async def create(self, **kwargs):
        self.sent.append(kwargs)
        chunk = SimpleNamespace(delta=SimpleNamespace(content=self.answer, tool_calls=None))
        return _Chunks([SimpleNamespace(choices=[chunk])])


class _Chunks:
    def __init__(self, chunks):
        self.chunks = list(chunks)

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self.chunks:
            raise StopAsyncIteration
        return self.chunks.pop(0)


class _Memory:
    """Token counting without tiktoken's download; no history."""

    def count_tokens(self, text):
        return len(text or "") // 4

    def truncate_tool_result(self, result):
        return result

    def get_truncated_history(self, session):
        return []


def _drive(stream):
    """Every event an async generator yields, stepped by hand (tests/test_moderation.py
    `_chat` says why not under an event loop)."""
    events = []
    while True:
        step = stream.asend(None)
        try:
            step.send(None)
        except StopIteration as done:
            events.append(done.value)
            continue
        except StopAsyncIteration:
            return events
        finally:
            step.close()
        raise AssertionError(f"the stream suspended after {events}")


def _turn(person, message, build, *, answer=None, language="en"):
    """One real `chat_stream` turn for `build`, with only the model, the clock and the
    thread hop replaced. Returns (events, the model's recorded calls)."""
    from unittest import mock

    from ai_assistant.services import chat_service as chat_service_module

    def inline(func):
        async def run_now(*args, **kwargs):
            return func(*args, **kwargs)
        return run_now

    async def no_clock(awaitable, timeout=None):
        return await awaitable

    model = _Model(answer) if answer else _Model()
    service = chat_service_module.ChatService()
    service.build = build
    service.client = SimpleNamespace(chat=SimpleNamespace(completions=model))
    service.memory_service = _Memory()
    with mock.patch.object(chat_service_module, "database_sync_to_async", inline), \
            mock.patch.object(asyncio, "wait_for", no_clock), translation.override(language):
        events = _drive(service.chat_stream(person, message))
    return events, model.sent


def _reply_text(events):
    return "".join(e["content"] for e in events if e["type"] == "token")


# ============================================================ 1. the build reaches it
def test_a_socket_declares_its_build_on_the_handshake_header_or_query():
    from ai_assistant.store_safe import build_of_scope

    headers = {"headers": [(b"x-app-channel", b"ios"), (b"x-app-version", b"1.4.0+27")],
               "query_string": b"token=abc"}
    assert build_of_scope(headers) == ("ios", (1, 4, 0))

    # A client that cannot set a header on a WebSocket (a browser) says it in the query.
    query = {"headers": [], "query_string": b"token=abc&app_channel=play&app_version=2.1"}
    assert build_of_scope(query) == ("play", (2, 1, 0))

    # The header is the declaration when both are sent.
    both = {"headers": [(b"x-app-channel", b"apk")],
            "query_string": b"app_channel=ios"}
    assert build_of_scope(both)[0] == "apk"

    # Exact, lower-case values only, as on REST: a typo is never guessed at.
    for raw in (b"IOS", b"iphone", b"android", b""):
        assert build_of_scope({"headers": [(b"x-app-channel", raw)]})[0] is None
    assert build_of_scope({}) == (None, None)
    assert build_of_scope({"query_string": b"app_channel=ios&app_version=v1"}) == ("ios", None)


def test_the_store_rule_comes_from_policy_not_from_channel_names():
    """play, ios and a build that declares nothing may not buy the store-billed goods,
    so the coach may not talk money there; apk and web may. Read from
    `policy.services.flags_for`, so an operator row moves it and nothing else does."""
    from policy.models import ChannelCapabilities
    from policy.services import flags_for
    from ai_assistant.store_safe import payment_talk_blocked

    assert payment_talk_blocked("play", None) is True
    assert payment_talk_blocked("ios", (1, 0, 0)) is True
    # No channel is what policy answers a REST caller that declares none: STRICTEST.
    assert flags_for(None, None)["can_purchase_platform_sub"] is False
    assert payment_talk_blocked(None, None) is True
    assert payment_talk_blocked("apk", (1, 0, 0)) is False
    assert payment_talk_blocked("web", None) is False

    # An apk build whose row stops selling plans is store-safe too, from that version.
    ChannelCapabilities.objects.filter(channel="apk", min_version="9.0.0").delete()
    ChannelCapabilities(channel="apk", min_version="9.0", can_purchase_platform_sub=False,
                        can_purchase_routine_product=True, can_pay_coaching_in_app=True,
                        can_show_prices=True, can_show_coaching_prices=True,
                        can_show_web_hub_link=True, can_transfer_wallet=True).save()
    assert payment_talk_blocked("apk", (9, 0, 0)) is True
    assert payment_talk_blocked("apk", (8, 9, 0)) is False


@IN_MEMORY_CHANNELS
def test_the_socket_hands_its_build_to_the_coach(
        make_user, monkeypatch, socket_keeps_the_test_connection):
    """End to end through the real consumer and the real `chat_stream`: an `ios`
    handshake (query) gets the fixed reply without the model, an `apk` handshake
    (header) reaches the model with the same words; both are told the disclaimer."""
    from channels.routing import URLRouter
    from channels.testing import WebsocketCommunicator

    import ai_assistant.routing
    from ai_assistant.services import chat_service as chat_service_module

    model = _Model()
    monkeypatch.setattr(chat_service_module.openai, "AsyncOpenAI",
                        lambda **kw: SimpleNamespace(chat=SimpleNamespace(completions=model)))
    monkeypatch.setattr(chat_service_module, "MemoryService", _Memory)

    person = _ai_subscriber(make_user, "wsstore")
    application = URLRouter(ai_assistant.routing.websocket_urlpatterns)
    token = _token(person)

    async def turn(path, headers, text):
        comm = WebsocketCommunicator(application, path, headers=headers)
        connected, _sub = await comm.connect()
        assert connected
        welcome = json.loads(await comm.receive_from(timeout=10))
        await comm.send_to(text_data=json.dumps({"type": "message", "content": text}))
        frames = []
        while True:
            frame = json.loads(await comm.receive_from(timeout=10))
            frames.append(frame)
            if frame["type"] == "rate_limit":
                break
        await comm.disconnect()
        return welcome, frames

    welcome, frames = async_to_sync(turn)(
        f"/ws/ai/chat/?token={token}&app_channel=ios&app_version=1.0.0", [],
        "How do I pay for premium?")
    assert welcome["code"] == "connected" and welcome["medical_disclaimer"]
    assert [f["type"] for f in frames] == ["token", "complete", "rate_limit"], frames
    assert model.sent == [], "a store build's payment question reached the model"
    assert frames[1]["medical_disclaimer"] == welcome["medical_disclaimer"]
    assert not FORBIDDEN_IN_A_STORE_REPLY.search(frames[0]["content"]), frames[0]

    _welcome, frames = async_to_sync(turn)(
        f"/ws/ai/chat/?token={token}",
        [(b"x-app-channel", b"apk"), (b"x-app-version", b"1.0.0")],
        "How do I pay for premium?")
    assert len(model.sent) == 1, "the same question on the direct APK must reach the model"
    assert _reply_text(frames) == model.answer
    assert frames[-2]["type"] == "complete" and frames[-2]["medical_disclaimer"]


# ============================================================ 2. the guard
PAYMENT_QUESTIONS = [
    # English
    "How do I pay?",
    "how can I pay for premium",
    "Where do I pay my subscription?",
    "How much is Premium?",
    "How much does the subscription cost?",
    "What's the price of the Professional plan?",
    "How do I subscribe?",
    "How do I upgrade to premium?",
    "Can I pay with ShamCash?",
    "How do I top up my wallet?",
    "Is it cheaper on the website?",
    "How much does a personal trainer cost?",
    "Where can I buy the program?",
    "Is the app free?",
    "what are the coaching fees",
    "How do I renew my membership?",
    # Arabic, standard and Levantine
    "كيف ادفع؟",
    "كيف أدفع ثمن الاشتراك؟",
    "وين بدفع؟",
    "كيف فيني ادفع للمدرب",
    "كم سعر الاشتراك؟",
    "قديش سعر الباقة؟",
    "أديش الاشتراك بالشهر؟",
    "بقديش البريميوم؟",
    "كيف اشترك؟",
    "بدي اشترك بالبريميوم",
    "كيف بشحن رصيدي؟",
    "هل التطبيق مجاني؟",
    "في طريقة ادفع فيها بشام كاش؟",
    "شو أرخص طريقة للاشتراك؟",
    "كم تكلفة المدرب الشخصي؟",
    "ممكن ادفع كاش؟",
    "كيف رقّي حسابي للبريميوم",
    "هل الاشتراك أرخص على الموقع؟",
    "كيف اشتري البرنامج؟",
    "إشتراكي بيخلص إمتى وكيف جدده؟",
    # diacritics, tatweel and the other hamza forms fold to the same words
    "كَيْفَ أَدْفَعُ؟",
    "كيف ادفــع الاشتــراك",
    # Arabizi
    "kif bedfa3?",
    "adesh el eshterak?",
    "b2adesh el premium",
]

ORDINARY_QUESTIONS = [
    "How much protein should I eat?",
    "Should I pay attention to my knees on squats?",
    "All that work finally paid off, what next?",
    "Give me a cheap meal plan for bulking",
    "What is the calorie cost of running 5k?",
    "How much weight should my coach add each week?",
    "Is my training plan good for fat loss?",
    "Can I join the push-up challenge?",
    "What are free weights good for?",
    "Is fat free yogurt ok on my diet?",
    "Who is in charge of my plan, you or my trainer?",
    "How do I push through the last reps?",
    "كم سعرة حرارية في الموزة؟",
    "كم سعر حراري أحتاج باليوم؟",
    "كيف ادفع الوزن للأعلى في تمرين البنش؟",
    "شو هي تمارين الدفع والسحب؟",
    "قديش لازم آكل بروتين؟",
    "كيف اشترك بتحدي السكوات؟",
    "بدي خطة غذائية رخيصة",
    "وين موقع الألم بالضبط؟ عندي وجع بالركبة",
    "ما هي مميزات تمرين الديدليفت؟",
    "كم مجموعة لازم اعمل؟",
    "هل الأوزان الحرة أفضل؟",
    "يا غالي كيف تمريني اليوم؟",
    "بدي ادفع حالي أكتر بالتمرين",
]


@pytest.mark.parametrize("question", PAYMENT_QUESTIONS)
def test_a_payment_question_is_recognised(question):
    from ai_assistant.store_safe import is_payment_question

    assert is_payment_question(question) is True, question


@pytest.mark.parametrize("question", ORDINARY_QUESTIONS)
def test_an_ordinary_fitness_question_is_not_mistaken_for_one(question):
    from ai_assistant.store_safe import is_payment_question

    assert is_payment_question(question) is False, question


@pytest.mark.parametrize("build", [("ios", (1, 0, 0)), ("play", (1, 0, 0)), (None, None)])
def test_on_a_store_build_the_coach_answers_a_payment_question_without_the_model(
        make_user, build):
    from ai_assistant.models import ChatMessage

    person = _ai_subscriber(make_user, "storeq")
    events, sent = _turn(person, "كم سعر الاشتراك؟ وكيف ادفع؟", build)

    assert sent == [], "the model was called"
    assert [e["type"] for e in events] == ["token", "complete"], events
    reply = _reply_text(events)
    assert reply and not FORBIDDEN_IN_A_STORE_REPLY.search(reply), reply
    complete = events[-1]
    # Kept like any other turn: the history and 👍/👎 work on it.
    saved = ChatMessage.objects.get(pk=complete["message_id"])
    assert saved.role == "assistant" and saved.content == reply
    assert ChatMessage.objects.filter(session=saved.session, role="user").count() == 1


def test_the_same_question_on_the_direct_apk_and_the_web_reaches_the_model(make_user):
    person = _ai_subscriber(make_user, "apkq")
    for build in (("apk", (1, 0, 0)), ("web", None)):
        events, sent = _turn(person, "How much is premium and how do I pay?", build)
        assert len(sent) == 1, build
        assert _reply_text(events) == _Model().answer
        system = sent[0]["messages"][0]["content"]
        assert "SELLS NOTHING" not in system, "the store rule reached a build that sells"


def test_the_store_rule_is_in_the_system_prompt_of_a_store_build(make_user):
    """(b): the recogniser answers the questions it knows without the model; anything it
    misses still meets the rule in the model's instructions."""
    person = _ai_subscriber(make_user, "storerule")
    _events, sent = _turn(person, "how is my squat going?", ("ios", (1, 0, 0)))
    assert len(sent) == 1
    system = sent[0]["messages"][0]["content"]
    assert "SELLS NOTHING" in system
    for promise in ("NEVER give prices", "NEVER explain how to pay", "NEVER include links"):
        assert promise in system, promise


def test_the_models_reply_is_never_edited(make_user):
    """(c): nothing post-filters the model. A normal answer that happens to use money
    words ('pay attention', 'the price of') is streamed and saved verbatim on a store
    build."""
    from ai_assistant.models import ChatMessage

    person = _ai_subscriber(make_user, "verbatim")
    answer = ("Pay attention to your depth: the price of ego lifting is a sore back. "
              "Free weights, 3 sets of 8.")
    events, sent = _turn(person, "how do I squat deeper?", ("ios", (1, 0, 0)), answer=answer)
    assert len(sent) == 1
    assert _reply_text(events) == answer
    assert ChatMessage.objects.get(pk=events[-1]["message_id"]).content == answer


def test_every_user_of_a_build_gets_the_same_answer(make_user):
    """Rule zero: the answer depends on the build and the words, never on who asks."""
    first = _ai_subscriber(make_user, "rz1", preferred_language="ar")
    second = _ai_subscriber(make_user, "rz2", preferred_language="en", is_staff=True)
    for language in ("en", "ar"):
        one, _ = _turn(first, "How do I pay?", ("ios", (1, 0, 0)), language=language)
        two, _ = _turn(second, "How do I pay?", ("ios", (1, 0, 0)), language=language)
        assert _reply_text(one) == _reply_text(two)
        assert one[-1]["medical_disclaimer"] == two[-1]["medical_disclaimer"]
    arabic, _ = _turn(first, "كيف ادفع؟", ("ios", (1, 0, 0)), language="ar")
    assert ARABIC.search(_reply_text(arabic)), "the fixed reply is in the reader's language"


def test_on_a_store_build_the_history_never_shows_payment_instructions(make_user, api):
    """The REST history learns the build too. A reply given on the direct APK to a
    payment question is shown on a store build as the store-safe reply; every other
    reply, and every reply on apk, is shown as it was written."""
    from ai_assistant.models import ChatMessage, ChatSession

    person = _ai_subscriber(make_user, "hist")
    session = ChatSession.objects.create(user=person)
    ChatMessage.objects.create(session=session, role="user", content="How much is premium?")
    paid = ChatMessage.objects.create(
        session=session, role="assistant",
        content="Premium is 50,000 SYP a month; subscribe on our website with ShamCash.")
    ChatMessage.objects.create(session=session, role="user", content="how do I squat deeper?")
    normal = ChatMessage.objects.create(
        session=session, role="assistant",
        content="Pay attention to depth; the price of ego lifting is injury.")
    url = f"/api/ai/sessions/{session.session_id}/messages/"

    def shown(**headers):
        answer = api(person).get(url, **headers)
        assert answer.status_code == 200, answer.content
        return {m["id"]: m for m in answer.json()["results"]}

    store = shown(HTTP_X_APP_CHANNEL="ios")
    assert not FORBIDDEN_IN_A_STORE_REPLY.search(store[paid.id]["content"])
    assert store[normal.id]["content"] == normal.content, "a normal reply was edited"
    assert shown(HTTP_X_APP_CHANNEL="")[paid.id]["content"] == store[paid.id]["content"]
    # Paged one message at a time, the reply's question sits on the NEXT page and is
    # still found.
    pages, next_url = [], url + "?page_size=1"
    while next_url:
        page = api(person).get(next_url, HTTP_X_APP_CHANNEL="play").json()
        pages += page["results"]
        next_url = page["next"]
    by_id = {m["id"]: m for m in pages}
    assert by_id[paid.id]["content"] == store[paid.id]["content"]

    apk = shown(HTTP_X_APP_CHANNEL="apk")
    assert apk[paid.id]["content"] == paid.content
    assert apk[normal.id]["content"] == normal.content


# ============================================================ 3. medical disclaimer
def test_every_coach_reply_carries_the_disclaimer_in_the_readers_language(make_user, api):
    from ai_assistant.models import ChatMessage, ChatSession
    from training_platform.disclaimers import ai_coach_disclaimer

    with translation.override("en"):
        english = ai_coach_disclaimer()
    with translation.override("ar"):
        arabic = ai_coach_disclaimer()
    assert "doctor" in english.lower()
    assert ARABIC.search(arabic) and "طبيب" in arabic

    person = _ai_subscriber(make_user, "disc")
    events, _ = _turn(person, "how is my squat?", ("apk", (1, 0, 0)), language="ar")
    assert events[-1]["type"] == "complete" and events[-1]["medical_disclaimer"] == arabic
    events, _ = _turn(person, "how is my squat?", ("ios", (1, 0, 0)), language="en")
    assert events[-1]["medical_disclaimer"] == english

    session = ChatSession.objects.create(user=person)
    question = ChatMessage.objects.create(session=session, role="user", content="hi")
    reply = ChatMessage.objects.create(session=session, role="assistant", content="hello")
    url = f"/api/ai/sessions/{session.session_id}/messages/"
    rows = {m["id"]: m for m in api(person).get(url).json()["results"]}
    assert rows[reply.id]["medical_disclaimer"] == english
    assert rows[question.id]["medical_disclaimer"] is None
    rows = {m["id"]: m for m in api(person).get(url, HTTP_ACCEPT_LANGUAGE="ar")
            .json()["results"]}
    assert rows[reply.id]["medical_disclaimer"] == arabic


def test_nutrition_onboarding_carries_the_disclaimer(make_user, api):
    from tests.test_regression_gate import _grant_diet_access
    from training_platform.disclaimers import nutrition_disclaimer

    with translation.override("en"):
        english = nutrition_disclaimer()
    with translation.override("ar"):
        arabic = nutrition_disclaimer()
    assert "doctor" in english.lower() and "طبيب" in arabic

    person = make_user(f"nutdisc_{uuid.uuid4().hex[:6]}")
    _grant_diet_access(person)
    client = api(person)
    read = client.get("/api/diet/v1/preferences/")
    assert read.status_code == 200, read.content
    assert read.json()["medical_disclaimer"] == english
    saved = client.post("/api/diet/v1/preferences/", {"allergies": ["peanut"]},
                        content_type=JSON, HTTP_ACCEPT_LANGUAGE="ar")
    assert saved.status_code == 200, saved.content
    assert saved.json()["medical_disclaimer"] == arabic


def test_diet_generation_carries_the_disclaimer(seeded_catalogue):
    from tests.test_regression_gate import _api_client_for, _chooser
    from training_platform.disclaimers import nutrition_disclaimer

    with translation.override("en"):
        english = nutrition_disclaimer()

    user, _chosen = _chooser(f"gendisc-{uuid.uuid4().hex[:6]}")
    built = _api_client_for(user).post("/api/diet/v1/plans/generate-rule/",
                                       {"duration_days": 1}, content_type=JSON)
    assert built.status_code == 201, built.content
    assert built.json()["medical_disclaimer"] == english

    user, _chosen = _chooser(f"gendisc-async-{uuid.uuid4().hex[:6]}")
    client = _api_client_for(user)
    started = client.post("/api/diet/v1/plans/generate/", {"duration_days": 1},
                          content_type=JSON)
    assert started.status_code == 200, started.content
    assert started.json()["medical_disclaimer"] == english
    outcome = client.get(f"/api/diet/v1/plans/tasks/{started.json()['task_id']}/").json()
    assert outcome["state"] == "SUCCESS" and outcome.get("plan_id"), outcome
    assert outcome["medical_disclaimer"] == english
