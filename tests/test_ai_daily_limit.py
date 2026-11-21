"""The AI coach's daily allowance: 10 messages per person, ONE value (the owner, 2026-11-21).

The free Workers AI allowance is about 115 coach messages a day for everyone together,
so the per-person limit drops from 50 to 10. It was a literal in `settings_base`
(`AI_ASSISTANT_CONFIG["MAX_MESSAGES_PER_DAY"] = 50`), and each of its two readers, the
socket that refuses (`ai_assistant/consumers.py`) and `usage/today/` that draws the bar
(`subscription/views.py`), carried a fallback `50` of its own that could disagree with
it. Now the environment variable `AI_MAX_MESSAGES_PER_DAY` (default 10) feeds the
config, production refuses to boot below 1, and both readers go through
`ai_assistant.consumers.daily_message_limit`.

On ccd5da0 the default is 50, the variable is ignored, a 0 boots, and every test here
fails.
"""
import json
import os
import re
import subprocess
import sys
import uuid
from datetime import timedelta
from pathlib import Path

import pytest
from asgiref.sync import async_to_sync
from django.test import override_settings

ROOT = Path(__file__).resolve().parent.parent
USAGE = "/api/subscription/v1/usage/today/"
#: The config key, and not the environment variable whose name ends in it.
KEY = re.compile(r"(?<![A-Z_])MAX_MESSAGES_PER_DAY")

IN_MEMORY_CHANNELS = override_settings(
    CHANNEL_LAYERS={"default": {"BACKEND": "channels.layers.InMemoryChannelLayer"}})


def _tag(prefix):
    return f"{prefix}_{uuid.uuid4().hex[:6]}"


def _ai_subscriber(make_user, tag):
    """Someone entitled to the coach and consenting to it."""
    from django.utils import timezone

    from moderation.models import AIConsent
    from subscription.models import Subscription, SubscriptionPlan

    person = make_user(tag)
    plan = SubscriptionPlan.objects.create(
        name=f"ai {uuid.uuid4().hex[:6]}", plan_type="premium", description="ai",
        price=0, currency="SYP", duration_days=30, is_active=True, has_ai_advice=True,
        max_meals_per_day=5)
    Subscription.objects.create(user=person, plan=plan, status="active",
                                end_date=timezone.now() + timedelta(days=30))
    AIConsent.objects.create(user=person, granted_at=timezone.now(),
                             policy_version="2026-09-01")
    return person


def _limit_in_a_new_process(value=None):
    """`MAX_MESSAGES_PER_DAY` as a freshly started process reads it, with
    `AI_MAX_MESSAGES_PER_DAY` unset (None) or set to `value`."""
    env = {k: v for k, v in os.environ.items() if k != "AI_MAX_MESSAGES_PER_DAY"}
    if value is not None:
        env["AI_MAX_MESSAGES_PER_DAY"] = value
    env["DJANGO_SETTINGS_MODULE"] = "training_platform.settings_local"
    # settings_local loads the checkout's .env; this process's environment (inherited,
    # less the one variable) already carries the rest of it, and a line in a
    # developer's .env must not stand in for the default under test.
    code = ("import dotenv; dotenv.load_dotenv = lambda *a, **k: False; "
            "from django.conf import settings; "
            "print('LIMIT', repr(settings.AI_ASSISTANT_CONFIG['MAX_MESSAGES_PER_DAY']))")
    ran = subprocess.run([sys.executable, "-c", code], cwd=str(ROOT), env=env,
                         capture_output=True, text=True, timeout=120)
    assert ran.returncode == 0, ran.stderr[-2000:]
    (line,) = [ln for ln in ran.stdout.splitlines() if ln.startswith("LIMIT ")]
    return line.split(" ", 1)[1]


def _bootable():
    from tests.test_media_storage import GCS_KEY_JSON

    return dict(MEDIA_BACKEND="gcs", GS_BUCKET_NAME="yalla-gym-media",
                GS_CREDENTIALS_JSON=GCS_KEY_JSON)


# ================================================================ the value, and where
def test_the_default_is_ten_and_the_variable_changes_it():
    assert _limit_in_a_new_process() == "10"
    assert _limit_in_a_new_process("7") == "7"
    if "AI_MAX_MESSAGES_PER_DAY" not in os.environ:
        from django.conf import settings

        assert settings.AI_ASSISTANT_CONFIG["MAX_MESSAGES_PER_DAY"] == 10


def test_the_one_reader_answers_the_setting(settings):
    from ai_assistant.consumers import daily_message_limit

    settings.AI_ASSISTANT_CONFIG = {**settings.AI_ASSISTANT_CONFIG,
                                    "MAX_MESSAGES_PER_DAY": 3}
    assert daily_message_limit() == 3


def test_nothing_else_reads_the_allowance_or_carries_a_number_of_its_own():
    """ONE value: the setting is defined in settings_base, checked by the production
    boot, and read in one function; no reader keeps a fallback number (the `50`s that
    could disagree with the setting)."""
    readers = []
    for package in sorted(p for p in ROOT.iterdir()
                          if (p / "__init__.py").is_file() and p.name != "tests"):
        for path in package.rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            if KEY.search(text):
                readers.append(path.relative_to(ROOT).as_posix())
            assert not re.search(r"(?<![A-Z_])MAX_MESSAGES_PER_DAY['\"]\s*,", text), path
    assert sorted(readers) == ["ai_assistant/consumers.py",
                               "training_platform/settings_base.py",
                               "training_platform/settings_production.py"], readers

    consumers = (ROOT / "ai_assistant" / "consumers.py").read_text(encoding="utf-8")
    assert consumers.count('settings.AI_ASSISTANT_CONFIG["MAX_MESSAGES_PER_DAY"]') == 1
    # The socket and the usage screen take it from that function.
    assert "reserve_daily_slot(self.user.id, daily_message_limit())" in consumers
    views = (ROOT / "subscription" / "views.py").read_text(encoding="utf-8")
    assert "limit = daily_message_limit()" in views


# ================================================================ production boot
@pytest.mark.parametrize("value, needle", [
    ("0", "AI_MAX_MESSAGES_PER_DAY is 0"),
    ("-3", "AI_MAX_MESSAGES_PER_DAY is -3"),
    ("ten", "AI_MAX_MESSAGES_PER_DAY' must be an integer"),
])
def test_production_refuses_an_allowance_below_one(value, needle):
    """0 would refuse every message while `usage/today/` reads a `limit` of 0 as
    unlimited. The shared production environment is otherwise bootable."""
    from tests.test_media_storage import _production_boot

    refused = _production_boot(AI_MAX_MESSAGES_PER_DAY=value, **_bootable())
    assert refused.returncode != 0 and "BOOTED" not in refused.stdout, refused.stdout
    assert needle in refused.stderr, refused.stderr[-2000:]


def test_production_boots_with_the_variable_unset_or_at_one():
    """The live server's deploy/.env has no such line: it boots on the default."""
    from tests.test_media_storage import _production_boot

    for fine in ({}, {"AI_MAX_MESSAGES_PER_DAY": "1"}):
        booted = _production_boot(**fine, **_bootable())
        assert booted.returncode == 0 and "BOOTED" in booted.stdout, booted.stderr[-2000:]


# ================================================================ what a person sees
@pytest.mark.django_db
def test_usage_today_shows_a_limit_of_ten(make_user, api):
    """The bar is drawn out of the same allowance and the same counter the socket
    refuses with."""
    from ai_assistant.consumers import daily_message_limit, reserve_daily_slot

    person = _ai_subscriber(make_user, _tag("lim_usage"))
    for _ in range(3):
        assert reserve_daily_slot(person.id, daily_message_limit())[0] is True
    resp = api(person).get(USAGE)
    assert resp.status_code == 200, resp.content
    (row,) = [q for q in resp.json()["quotas"] if q["feature"] == "ai_messages"]
    assert (row["used"], row["limit"]) == (3, 10), row


class _Answering:
    """A `chat_stream` that answers every turn, so every turn is charged."""

    def __init__(self):
        self.turns = 0

    def __call__(self, user, content, session_id=None):
        self.turns += 1
        turn = self.turns

        async def stream():
            yield {"type": "token", "content": "hi"}
            yield {"type": "complete", "session_id": "s", "message_id": turn,
                   "tools_used": []}

        return stream()


@IN_MEMORY_CHANNELS
@pytest.mark.django_db
def test_the_eleventh_message_of_a_day_is_refused_with_rate_limit(
        make_user, monkeypatch):
    """Ten messages are answered, counting down to 0; the eleventh is the existing
    `error` frame `code: "rate_limit"` (`remaining: 0`), reaches no model, and costs
    nothing."""
    from channels.routing import URLRouter
    from channels.testing import WebsocketCommunicator
    from rest_framework_simplejwt.tokens import RefreshToken

    import ai_assistant.routing
    from ai_assistant.services.chat_service import ChatService
    from django.utils import timezone
    from training_platform.cache import ratelimit_cache

    # The same shim `tests/test_chat.py` uses: Channels sweeps "old" connections around
    # every `database_sync_to_async` call, and here that is the test's own.
    monkeypatch.setattr("channels.db.close_old_connections", lambda: None)
    stream = _Answering()
    # `monkeypatch`, never a bare assignment (it would REMOVE the method afterwards).
    monkeypatch.setattr(ChatService, "chat_stream", stream)

    person = _ai_subscriber(make_user, _tag("lim_ws"))
    application = URLRouter(ai_assistant.routing.websocket_urlpatterns)
    token = str(RefreshToken.for_user(person).access_token)

    async def scenario():
        comm = WebsocketCommunicator(application, f"/ws/ai/chat/?token={token}")
        connected, _sub = await comm.connect()
        assert connected, "the AI socket refused an entitled subscriber"
        await comm.receive_from(timeout=5)          # the welcome status
        remaining, refusal = [], None
        for turn in range(11):
            await comm.send_to(text_data=json.dumps(
                {"type": "message", "content": f"how is my squat, set {turn}?"}))
            while True:
                frame = json.loads(await comm.receive_from(timeout=5))
                if frame["type"] == "rate_limit":
                    remaining.append(frame["remaining"])
                    break
                if frame["type"] == "error":
                    refusal = frame
                    break
        await comm.disconnect()
        return remaining, refusal

    remaining, refusal = async_to_sync(scenario)()
    assert remaining == [9, 8, 7, 6, 5, 4, 3, 2, 1, 0], remaining
    assert refusal is not None and refusal["code"] == "rate_limit", refusal
    assert refusal["remaining"] == 0 and refusal["resets_at"], refusal
    assert stream.turns == 10, "the refused message reached the model"
    key = f"ai_chat_limit:{person.id}:{timezone.localdate().isoformat()}"
    assert int(ratelimit_cache().get(key)) == 10
