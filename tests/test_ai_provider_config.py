"""The AI coach's provider is configuration, and "no provider" is a state it survives.

The coach was tied to one vendor in code: `ChatService` built `AsyncOpenAI(api_key=
settings.OPENAI_API_KEY)` and nothing else could be said about where the model lives.
Two things followed, and both are held here.

1. **With no key the coach's socket died instead of answering.** The pinned client
   library refuses to be constructed without credentials, and the service is built in
   the socket consumer's `__init__` — so on a deploy with the AI coach off, or on any
   machine whose `.env` carries no key, every connection to `ws/ai/chat/` failed on an
   unhandled `OpenAIError`. (It also meant seventeen tests of the coach passed only on
   a machine that had somebody's key.) Now: no client, and every turn answers the
   `config_error` frame the app already knows, leaving nothing behind.
2. **The provider could not be changed without editing code.** Now the key, the
   address, the model's name, a provider-specific request body and the model's price
   are settings (`settings_base.py`, "AI Integration"), with the old `OPENAI_API_KEY`
   still honoured.

Plus `manage.py ai_coach_eval`, which puts a fixed set of Arabic questions to whatever
is configured, so one model can be compared with another.
"""
import asyncio
import io
import json
import logging
from types import SimpleNamespace
from unittest import mock

import pytest
from asgiref.sync import async_to_sync
from django.core.exceptions import ImproperlyConfigured
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test.utils import override_settings
from django.utils import translation

from tests.test_fx_people import _ai_subscriber
from tests.test_store_ai import _Chunks, _drive, _Memory, _Model, _token

pytestmark = pytest.mark.django_db

IN_MEMORY_CHANNELS = override_settings(
    CHANNEL_LAYERS={"default": {"BACKEND": "channels.layers.InMemoryChannelLayer"}})


@pytest.fixture
def no_key(settings, monkeypatch):
    """A machine with no AI provider at all: not in settings, and not in the two
    environment variables the client library reads on its own."""
    settings.AI_API_KEY = ""
    settings.OPENAI_API_KEY = ""
    settings.AI_BASE_URL = ""
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_ADMIN_KEY", raising=False)


@pytest.fixture
def socket_keeps_the_test_connection(monkeypatch):
    """As in tests/test_store_ai.py: Channels closes "old" connections around
    `database_sync_to_async`, and in a test that is the one holding the transaction."""
    monkeypatch.setattr("channels.db.close_old_connections", lambda: None)


def _inline(func):
    async def run_now(*args, **kwargs):
        return func(*args, **kwargs)
    return run_now


async def _no_clock(awaitable, timeout=None):
    return await awaitable


def _stream(service, person, message, language="en"):
    """Every event of one real `chat_stream` turn, with only the thread hop and the
    clock replaced (tests/test_moderation.py `_chat` says why not under an event loop)."""
    from ai_assistant.services import chat_service as chat_service_module

    with mock.patch.object(chat_service_module, "database_sync_to_async", _inline), \
            mock.patch.object(asyncio, "wait_for", _no_clock), translation.override(language):
        return _drive(service.chat_stream(person, message))


class _ToolThenAnswer:
    """A model that asks for one tool on its first pass and answers on the next."""

    def __init__(self, tool="get_user_profile", answer="وزنك ثابت هذا الأسبوع."):
        self.sent, self.tool, self.answer = [], tool, answer

    async def create(self, **kwargs):
        self.sent.append(kwargs)
        if len(self.sent) == 1:
            call = SimpleNamespace(index=0, id="call_1", function=SimpleNamespace(
                name=self.tool, arguments="{}"))
            delta = SimpleNamespace(content=None, tool_calls=[call])
        else:
            delta = SimpleNamespace(content=self.answer, tool_calls=None)
        return _Chunks([SimpleNamespace(choices=[SimpleNamespace(delta=delta)])])


def _service_with(model, build=("apk", (1, 0, 0))):
    from ai_assistant.services.chat_service import ChatService

    service = ChatService(build=build)
    service.client = SimpleNamespace(chat=SimpleNamespace(completions=model))
    service.memory_service = _Memory()
    return service


# ============================================================ 1. no provider: off, not dead
def test_with_no_key_the_service_is_built_and_has_no_client(no_key):
    from ai_assistant.services.chat_service import ChatService

    assert ChatService().client is None


def test_with_no_key_a_turn_answers_config_error_and_leaves_nothing_behind(make_user, no_key):
    from ai_assistant.models import ChatMessage, ChatSession, UsageCost
    from ai_assistant.services.chat_service import ChatService

    person = _ai_subscriber(make_user, "nokey")
    events = _stream(ChatService(), person, "how is my squat?")

    assert [(e["type"], e["code"]) for e in events] == [("error", "config_error")], events
    assert events[0]["content"]
    assert not ChatSession.objects.filter(user=person).exists()
    assert not ChatMessage.objects.filter(session__user=person).exists()
    assert not UsageCost.objects.filter(user=person).exists()


def test_with_no_key_consent_is_still_asked_for_first(make_user, no_key):
    """The gates that cost nothing keep their order: a person who has not allowed the
    coach is told that, not that the service is unavailable."""
    from ai_assistant.services.chat_service import ChatService

    events = _stream(ChatService(), make_user("nokeynoconsent"), "how is my squat?")
    assert [e["code"] for e in events] == ["ai_consent_required"]


@IN_MEMORY_CHANNELS
def test_with_no_key_the_socket_connects_and_answers_instead_of_dying(
        make_user, no_key, monkeypatch, socket_keeps_the_test_connection):
    """The regression itself, through the real consumer and the real client library:
    the socket used to fail while being constructed. Now it welcomes the person, refuses
    the turn with `config_error`, and does not charge the daily allowance for it."""
    from channels.routing import URLRouter
    from channels.testing import WebsocketCommunicator

    import ai_assistant.routing
    from ai_assistant.services import chat_service as chat_service_module

    monkeypatch.setattr(chat_service_module, "MemoryService", _Memory)
    person = _ai_subscriber(make_user, "wsnokey")
    application = URLRouter(ai_assistant.routing.websocket_urlpatterns)
    token = _token(person)

    async def turn():
        comm = WebsocketCommunicator(
            application, f"/ws/ai/chat/?token={token}",
            headers=[(b"x-app-channel", b"apk"), (b"x-app-version", b"1.0.0")])
        connected, _sub = await comm.connect()
        assert connected
        welcome = json.loads(await comm.receive_from(timeout=10))
        await comm.send_to(text_data=json.dumps({"type": "message", "content": "hello"}))
        frames = []
        while True:
            frame = json.loads(await comm.receive_from(timeout=10))
            frames.append(frame)
            if frame["type"] == "rate_limit":
                break
        await comm.disconnect()
        return welcome, frames

    welcome, frames = async_to_sync(turn)()
    assert welcome["code"] == "connected"
    assert [f["type"] for f in frames] == ["error", "rate_limit"], frames
    assert frames[0]["code"] == "config_error"
    from django.conf import settings as live
    assert frames[1]["remaining"] == live.AI_ASSISTANT_CONFIG["MAX_MESSAGES_PER_DAY"], (
        "a turn the platform could not answer was charged to the person")


# ============================================================ 2. the provider is settings
@pytest.fixture
def built_with(monkeypatch):
    """Record what the client library is constructed with."""
    from ai_assistant.services import chat_service as chat_service_module

    calls = []

    def construct(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(chat=SimpleNamespace(completions=_Model()))

    monkeypatch.setattr(chat_service_module.openai, "AsyncOpenAI", construct)
    return calls


def test_the_address_and_the_key_come_from_settings(settings, built_with):
    from ai_assistant.services.chat_service import ChatService

    settings.AI_API_KEY = "key-of-the-new-provider"
    settings.OPENAI_API_KEY = "key-of-the-old-one"
    settings.AI_BASE_URL = "https://ai.example.test/v1"
    ChatService()
    assert built_with == [{"api_key": "key-of-the-new-provider",
                           "base_url": "https://ai.example.test/v1"}]


def test_without_an_address_the_librarys_default_is_left_alone(settings, built_with):
    """`base_url=None` or `""` would be this code's opinion of the default; the field
    is simply not passed."""
    from ai_assistant.services.chat_service import ChatService

    settings.AI_API_KEY = "k"
    settings.AI_BASE_URL = ""
    ChatService()
    assert built_with == [{"api_key": "k"}]


def test_an_environment_that_only_knows_the_old_key_name_keeps_working(settings, built_with):
    from ai_assistant.services.chat_service import ChatService

    settings.AI_API_KEY = ""
    settings.OPENAI_API_KEY = "key-of-the-old-one"
    settings.AI_BASE_URL = ""
    ChatService()
    assert built_with == [{"api_key": "key-of-the-old-one"}]


def test_the_tools_are_left_out_of_the_last_pass_not_sent_as_null(make_user, settings):
    """The last pass forces an answer by offering no tools. `"tools": null` and an
    absent field mean the same to OpenAI; a compatible server may reject the first."""
    settings.AI_ASSISTANT_CONFIG = {**settings.AI_ASSISTANT_CONFIG,
                                    "MAX_TOOL_CALLS_PER_TURN": 1}
    person = _ai_subscriber(make_user, "lastpass")
    model = _ToolThenAnswer()
    events = _stream(_service_with(model), person, "how much do I weigh?")

    assert [e["type"] for e in events if e["type"] in ("tool_used", "token", "complete")] \
        == ["tool_used", "token", "complete"], events
    assert len(model.sent) == 2
    assert model.sent[0]["tools"], "the first pass must offer the tools"
    assert "tools" not in model.sent[1], model.sent[1].keys()


def test_the_extra_body_rides_every_request_and_is_absent_when_unset(make_user, settings):
    person = _ai_subscriber(make_user, "extrabody")

    plain = _Model()
    _stream(_service_with(plain), person, "how is my squat?")
    assert "extra_body" not in plain.sent[0]

    switch = {"thinking": {"type": "disabled"}}
    settings.AI_ASSISTANT_CONFIG = {**settings.AI_ASSISTANT_CONFIG, "EXTRA_BODY": switch,
                                    "MAX_TOOL_CALLS_PER_TURN": 1}
    model = _ToolThenAnswer()
    _stream(_service_with(model), person, "how much do I weigh?")
    assert [call["extra_body"] for call in model.sent] == [switch, switch]


# ------------------------------------------------------------------ the two env parsers
def test_a_malformed_extra_body_stops_the_boot(monkeypatch):
    from training_platform import settings_base

    monkeypatch.delenv("AI_EXTRA_BODY", raising=False)
    assert settings_base._ai_extra_body() == {}
    monkeypatch.setenv("AI_EXTRA_BODY", '{"thinking": {"type": "disabled"}}')
    assert settings_base._ai_extra_body() == {"thinking": {"type": "disabled"}}
    for bad in ('{"thinking": ', '["a list"]', '"a string"'):
        monkeypatch.setenv("AI_EXTRA_BODY", bad)
        with pytest.raises(ImproperlyConfigured):
            settings_base._ai_extra_body()


def test_half_a_price_stops_the_boot(monkeypatch):
    from training_platform import settings_base

    monkeypatch.delenv("AI_PRICE_INPUT_PER_1M_USD", raising=False)
    monkeypatch.delenv("AI_PRICE_OUTPUT_PER_1M_USD", raising=False)
    assert settings_base._ai_price_per_1m_usd() is None

    monkeypatch.setenv("AI_PRICE_INPUT_PER_1M_USD", "0.10")
    with pytest.raises(ImproperlyConfigured):
        settings_base._ai_price_per_1m_usd()

    monkeypatch.setenv("AI_PRICE_OUTPUT_PER_1M_USD", "0.30")
    assert settings_base._ai_price_per_1m_usd() == {"input": "0.10", "output": "0.30"}

    for bad in ("cheap", "-1"):
        monkeypatch.setenv("AI_PRICE_OUTPUT_PER_1M_USD", bad)
        with pytest.raises(ImproperlyConfigured):
            settings_base._ai_price_per_1m_usd()


# ------------------------------------------------------------------ the price of a model
def test_a_configured_price_is_what_the_cost_ceiling_counts(settings):
    from decimal import Decimal

    from ai_assistant.services.cost_tracker import CostTracker

    settings.AI_ASSISTANT_CONFIG = {
        **settings.AI_ASSISTANT_CONFIG, "MODEL": "some-open-model",
        "PRICE_PER_1M_USD": {"input": "0.10", "output": "0.30"}}
    cost = CostTracker().estimate_cost(input_tokens=1_000_000, output_tokens=1_000_000)
    assert cost == Decimal("0.40")


def test_a_model_with_no_price_is_counted_at_the_default_and_the_log_says_so(settings, caplog):
    from decimal import Decimal

    from ai_assistant.services import cost_tracker

    settings.AI_ASSISTANT_CONFIG = {**settings.AI_ASSISTANT_CONFIG,
                                    "MODEL": "a-model-nobody-priced",
                                    "PRICE_PER_1M_USD": None}
    cost_tracker._UNPRICED_WARNED.discard("a-model-nobody-priced")
    with caplog.at_level(logging.WARNING, logger=cost_tracker.logger.name):
        first = cost_tracker.CostTracker()
        cost_tracker.CostTracker()

    assert first.estimate_cost(1_000_000, 1_000_000) == Decimal("0.75")
    said = [r for r in caplog.records if "a-model-nobody-priced" in r.getMessage()]
    assert len(said) == 1, "once per process, not once per socket"

    # A model the table knows is priced from the table, silently, as before.
    settings.AI_ASSISTANT_CONFIG = {**settings.AI_ASSISTANT_CONFIG, "MODEL": "gpt-4o"}
    assert cost_tracker.CostTracker().estimate_cost(1_000_000, 0) == Decimal("2.50")


# ============================================================ 3. the evaluation command
def test_the_evaluation_lists_its_questions_and_calls_nothing(no_key):
    from ai_assistant.management.commands.ai_coach_eval import QUESTIONS

    out = io.StringIO()
    call_command("ai_coach_eval", "--list", stdout=out)
    listed = out.getvalue()
    keys = [q.key for q in QUESTIONS]
    assert len(set(keys)) == len(keys) >= 15
    for question in QUESTIONS:
        assert question.key in listed and question.text in listed
        assert question.look_for, question.key


def test_every_tool_an_evaluation_question_expects_is_a_real_tool():
    """A renamed tool would otherwise turn "looked at the data" into a permanent NO."""
    from ai_assistant.management.commands.ai_coach_eval import QUESTIONS
    from ai_assistant.tool_registry import _TOOL_FUNCTIONS

    expected = {name for question in QUESTIONS for name in question.tools}
    assert expected and expected <= set(_TOOL_FUNCTIONS), expected - set(_TOOL_FUNCTIONS)


def test_the_evaluation_refuses_without_an_account_or_a_provider(make_user, no_key):
    with pytest.raises(CommandError, match="--user is required"):
        call_command("ai_coach_eval", stdout=io.StringIO())
    with pytest.raises(CommandError, match="No account"):
        call_command("ai_coach_eval", "--user", "nobody@nowhere.test", stdout=io.StringIO())
    person = make_user("evalnoprovider")
    with pytest.raises(CommandError, match="No AI provider is configured"):
        call_command("ai_coach_eval", "--user", person.email, stdout=io.StringIO())
    with pytest.raises(CommandError, match="Unknown question key"):
        call_command("ai_coach_eval", "--user", person.email, "--only", "no-such-question",
                     stdout=io.StringIO())


def _finish(coroutine):
    """Run a coroutine that never really suspends, without an event loop."""
    try:
        coroutine.send(None)
    except StopIteration as done:
        return done.value
    raise AssertionError("the coroutine suspended")


def test_the_evaluation_reports_the_answer_the_wait_and_the_tools(make_user):
    from ai_assistant.management.commands.ai_coach_eval import QUESTIONS, Command
    from ai_assistant.services import chat_service as chat_service_module

    person = _ai_subscriber(make_user, "evalrun", preferred_language="ar")
    by_key = {q.key: q for q in QUESTIONS}
    command = Command(stdout=io.StringIO(), stderr=io.StringIO())

    with mock.patch.object(chat_service_module, "database_sync_to_async", _inline), \
            mock.patch.object(asyncio, "wait_for", _no_clock), translation.override("ar"):
        looked = _finish(command._ask(
            _service_with(_ToolThenAnswer(tool="get_body_stats")), person, by_key["weight"]))
        guessed = _finish(command._ask(
            _service_with(_Model("Your weight is fine.")), person, by_key["weight"]))
        named = _finish(command._ask(
            _service_with(_Model("أنا ChatGPT من OpenAI")), person, by_key["who"]))

    assert looked.error == "" and looked.text == "وزنك ثابت هذا الأسبوع."
    assert looked.tools == ["get_body_stats"] and looked.looked is True
    assert looked.passes == 2 and looked.first_word is not None
    assert looked.first_word <= looked.total
    assert looked.arabic_share == 1.0 and looked.vendors_named == []

    assert guessed.looked is False, "answering a data question without the data is a NO"
    assert guessed.arabic_share == 0.0
    assert named.looked is None, "a question that needs no tool expects none"
    assert {"chatgpt", "openai"} <= set(named.vendors_named)

    report = command._report(_service_with(_Model()), person, [looked, guessed, named])
    assert "وزنك ثابت هذا الأسبوع." in report
    assert "Looked at the data when the question needed it: 1 of 2" in report
    assert "Answers that name an AI company or model: 1 (who)" in report
    assert "looked at the data: NO" in report


def test_the_evaluation_report_never_prints_the_key(make_user, settings):
    from ai_assistant.management.commands.ai_coach_eval import Answer, QUESTIONS, Command

    settings.AI_API_KEY = "sk-this-must-never-be-printed"
    settings.AI_BASE_URL = "https://user:secret@ai.example.test/v1?token=also-secret"
    service = _service_with(_Model())
    service.api_key, service.base_url = settings.AI_API_KEY, settings.AI_BASE_URL
    person = make_user("evalkey")
    report = Command(stdout=io.StringIO(), stderr=io.StringIO())._report(
        service, person, [Answer(QUESTIONS[0], error="config_error")])

    assert "sk-this-must-never-be-printed" not in report
    assert "also-secret" not in report and "user:secret" not in report
    assert "ai.example.test" in report
    assert "`config_error`" in report
