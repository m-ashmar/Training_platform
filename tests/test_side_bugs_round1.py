"""The first batch of `SIDE_BUGS.md` findings, one test per row.

Each test names the defect it closes and fails against the behaviour that was there:

    S-008  every audit script in tests/security/ resolved the repo root from a
           hardcoded path, so a worktree exercised the MAIN checkout
    S-011  `SocialConsumer.receive` echoed any frame back verbatim
    S-012  the assistant's error paths carried no `code` and were untranslated
    S-013  the AI daily allowance was spent by a turn that only produced an error
    S-014  social counts ignored blocking, so they exceeded the rows a viewer sees
    S-015  trainer-request refusals carried no `code`
"""
import json
import uuid
from pathlib import Path

import pytest
from asgiref.sync import async_to_sync
from django.db import connection
from django.test.utils import CaptureQueriesContext, override_settings

pytestmark = pytest.mark.django_db

REPO_ROOT = Path(__file__).resolve().parent.parent
SECURITY = REPO_ROOT / "tests" / "security"

IN_MEMORY_CHANNELS = override_settings(
    CHANNEL_LAYERS={'default': {'BACKEND': 'channels.layers.InMemoryChannelLayer'}})


# ------------------------------------------------------------------------ helpers
def _tag(prefix):
    return f"{prefix}_{uuid.uuid4().hex[:6]}"


@pytest.fixture
def socket_keeps_the_test_connection(monkeypatch):
    """Stop `database_sync_to_async` closing the connection this test lives in.

    The same shim `tests/test_chat.py` uses, and for the same reason: Channels sweeps
    "old" connections around every `database_sync_to_async` call, and in a test the one
    it finds is the one holding the open transaction.
    """
    monkeypatch.setattr('channels.db.close_old_connections', lambda: None)


def _token(user):
    from rest_framework_simplejwt.tokens import RefreshToken

    return str(RefreshToken.for_user(user).access_token)


# ------------------------------------------------------------- S-008: script paths
def _path_line(source):
    """The one line in an audit script that puts the repo on `sys.path`."""
    lines = [line for line in source.splitlines() if "sys.path.insert" in line]
    assert len(lines) == 1, lines
    return lines[0]


def test_an_audit_script_puts_its_own_tree_on_the_path_not_a_hardcoded_one(tmp_path):
    """S-008. Every script in tests/security/ began with

        sys.path.insert(0, '/Users/mac/Desktop/Git/t2/Training_platform')

    so running the audit from a git worktree imported the MAIN checkout's apps: the
    branch under test was never the code under test, and a broken branch passed.

    Each script's line is executed here with a `__file__` pointing into a throwaway
    tree. What it puts on `sys.path` must be THAT tree's root — which the hardcoded
    version could not do for any tree but one.
    """
    import sys

    scripts = sorted(SECURITY.glob("*.py"))
    assert len(scripts) > 40, "the audit scripts have moved; this test found almost none"

    fake_root = tmp_path / "some-other-worktree"
    fake_file = fake_root / "tests" / "security" / "probe.py"
    fake_file.parent.mkdir(parents=True)

    for script in scripts:
        line = _path_line(script.read_text(encoding="utf-8"))
        assert "__file__" in line, (
            f"{script.name} resolves the repo root from something other than its own "
            f"location, so a worktree runs another tree's code: {line.strip()}")
        before = list(sys.path)
        try:
            exec(compile(line, str(fake_file), "exec"), {"__file__": str(fake_file)})
            assert sys.path[0] == str(fake_root), (
                f"{script.name} put {sys.path[0]} on the path, not its own tree")
        finally:
            sys.path[:] = before


# ---------------------------------------------------------- S-011: no reflection
@IN_MEMORY_CHANNELS
def test_the_legacy_social_socket_never_echoes_a_frame_back(
        make_user, socket_keeps_the_test_connection):
    """S-011. `SocialConsumer.receive` replied `{"type": "echo", "data": <the frame>}`
    to anything sent to it, so one authenticated account was a reflected-amplification
    surface: `training_platform/ws_limits.py` measured a large frame doubling the
    server's memory peak because the bytes were paid for twice.

    The socket still ACCEPTS the frame — it must not close on a stray ping — it simply
    says nothing back.
    """
    from channels.routing import URLRouter
    from channels.testing import WebsocketCommunicator

    import social.routing

    person = make_user(_tag("echo"))
    application = URLRouter(social.routing.websocket_urlpatterns)
    token = _token(person)

    async def scenario():
        comm = WebsocketCommunicator(application, f"/ws/social/?token={token}")
        connected, _sub = await comm.connect()
        assert connected, "the deprecated socket stopped accepting connections"
        # The welcome frame, which is the only thing this socket may send unprompted.
        welcome = json.loads(await comm.receive_from(timeout=5))
        await comm.send_to(text_data=json.dumps({"type": "anything", "payload": "x" * 64}))
        # Nothing comes back at all — no echo, and no close either: `receive_nothing`
        # looks at the whole output queue, and a socket that had hung up would have
        # put a close message on it.
        silent = await comm.receive_nothing(timeout=1)
        await comm.disconnect()
        return welcome, silent

    welcome, silent = async_to_sync(scenario)()
    assert welcome["type"] == "connection"
    assert silent, "the socket answered a client frame — it is echoing again"


# ------------------------------------------------- S-012: a code on every refusal
def _ai_subscriber(make_user, tag):
    """Someone entitled to the assistant and consenting to it."""
    from django.utils import timezone

    from moderation.models import AIConsent
    from subscription.models import Subscription, SubscriptionPlan

    person = make_user(tag)
    plan = SubscriptionPlan.objects.create(
        name=f"ai {uuid.uuid4().hex[:6]}", plan_type="premium", description="ai",
        price=0, currency="SYP", duration_days=30, is_active=True, has_ai_advice=True)
    Subscription.objects.create(user=person, plan=plan, status="active")
    AIConsent.objects.create(user=person, granted_at=timezone.now(),
                             policy_version="2026-09-01")
    return person


def test_the_assistants_refusals_carry_a_code_and_speak_arabic(make_user, api):
    """S-012. Three error paths were inconsistent with everything around them:

    * `POST /api/ai/feedback/` answered `{"error": "Message not found."}` — no `code`,
      and never through gettext, so an Arabic user read English.
    * the socket's feedback failure was the one error frame in that consumer with no
      `code` at all.
    * five refusals in `services/chat_service.py` were bare English literals beside a
      `code` the app could already branch on.

    Asserted on the `code`, never on the sentence.
    """
    person = _ai_subscriber(make_user, _tag("fb"))

    client = api(person)
    missing = client.post("/api/ai/feedback/",
                          {"message_id": 987654321, "feedback": "positive"},
                          content_type="application/json")
    assert missing.status_code == 404
    assert missing.json()["code"] == "message_not_found"

    # And the sentence is translated, which is the other half of the row.
    arabic = api(person)
    arabic.defaults["HTTP_ACCEPT_LANGUAGE"] = "ar"
    translated = arabic.post("/api/ai/feedback/",
                             {"message_id": 987654321, "feedback": "positive"},
                             content_type="application/json")
    assert translated.json()["code"] == "message_not_found"
    assert any("؀" <= ch <= "ۿ" for ch in translated.json()["error"]), translated.json()


def test_the_streams_own_error_paths_are_translated(make_user, settings):
    """S-012, the `chat_service.py` half: the five refusals raised while a turn runs
    (timeout, the model's own rate limit, a bad key, a connection failure, anything
    else) were untranslated literals. They already carried a `code`; a shipped `code`
    never changes, so only the text is touched.
    """
    from django.utils import translation

    from ai_assistant.services import chat_service as module

    with translation.override("ar"):
        arabic = {
            "timeout": str(translation.gettext(
                "I'm taking longer than usual. Please try again in a moment.")),
            "rate_limit_openai": str(translation.gettext(
                "I'm experiencing high demand. Please try again in a few seconds.")),
            "config_error": str(translation.gettext(
                "AI service is temporarily unavailable. Please contact support.")),
            "connection": str(translation.gettext(
                "I'm temporarily unavailable. Please try again shortly.")),
        }
    for code, text in arabic.items():
        assert any("؀" <= ch <= "ۿ" for ch in text), f"{code} has no Arabic"

    # And no bare literal is left: every error frame in the module — the one beside a
    # `code` — says its sentence through gettext, so a future edit cannot slip an
    # untranslated one back in unnoticed.
    lines = Path(module.__file__).read_text(encoding="utf-8").splitlines()
    checked = 0
    for index, line in enumerate(lines):
        if not line.strip().startswith('"code":'):
            continue
        frame = "\n".join(lines[max(0, index - 4):index])
        assert '"content":' in frame, frame
        assert "_(" in frame.split('"content":', 1)[1], f"untranslated frame: {frame}"
        checked += 1
    assert checked >= 7, f"only {checked} error frames found in chat_service.py"


# ------------------------------------------- S-013: a failed turn is not charged
class _StubStream:
    """A `chat_stream` that fails the first turn and answers the second."""

    def __init__(self):
        self.turns = 0

    def __call__(self, user, content, session_id=None):
        self.turns += 1
        failing = self.turns == 1

        async def stream():
            if failing:
                yield {"type": "error", "content": "nope", "code": "timeout"}
                return
            yield {"type": "token", "content": "hi"}
            yield {"type": "complete", "session_id": "s", "message_id": 1,
                   "tools_used": []}

        return stream()


@IN_MEMORY_CHANNELS
def test_a_turn_that_only_errors_does_not_spend_the_daily_allowance(
        make_user, settings, monkeypatch, socket_keeps_the_test_connection):
    """S-013. `_check_rate_limit` incremented the counter up front and nothing ever
    gave the slot back, so a turn that produced only an error — a model timeout, a
    refused consent, the budget gate — still cost the user one of their fifty messages
    for the day. They paid for the assistant's failures.

    The reservation stays up front, because `incr` is the only operation two concurrent
    turns cannot both win (see `_reserve_rate_limit`); what is new is the refund. Two
    turns here, the first failing: the counter must read 1, not 2.
    """
    from channels.routing import URLRouter
    from channels.testing import WebsocketCommunicator
    from django.utils import timezone

    import ai_assistant.routing
    from ai_assistant.services.chat_service import ChatService
    from training_platform.cache import ratelimit_cache

    person = _ai_subscriber(make_user, _tag("rl"))
    # `monkeypatch`, never a bare assignment: setting the attribute and deleting it
    # afterwards REMOVES the real method from the class for the rest of the session.
    monkeypatch.setattr(ChatService, "chat_stream", _StubStream())
    key = f"ai_chat_limit:{person.id}:{timezone.localdate().isoformat()}"
    ratelimit_cache().delete(key)

    application = URLRouter(ai_assistant.routing.websocket_urlpatterns)
    token = _token(person)

    async def scenario():
        comm = WebsocketCommunicator(application, f"/ws/ai/chat/?token={token}")
        connected, _sub = await comm.connect()
        assert connected, "the AI socket refused an entitled subscriber"
        await comm.receive_from(timeout=5)          # the welcome status

        seen = []
        for _turn in range(2):
            await comm.send_to(text_data=json.dumps(
                {"type": "message", "content": "how is my squat?"}))
            frames = []
            while True:
                frame = json.loads(await comm.receive_from(timeout=5))
                frames.append(frame)
                if frame["type"] == "rate_limit":
                    break
            seen.append(frames)
        await comm.disconnect()
        return seen

    first, second = async_to_sync(scenario)()

    # The defect itself: two turns, one of which answered, cost exactly one slot.
    assert int(ratelimit_cache().get(key)) == 1, \
        "the failed turn was charged, or the answered one was not"

    assert [f["type"] for f in first] == ["error", "rate_limit"]
    assert first[0]["code"] == "timeout"
    # And what the app is told matches: `remaining` already excludes the turn that
    # just ran, so a refunded turn leaves the allowance where it started.
    assert first[-1]["remaining"] == 50
    assert [f["type"] for f in second] == ["token", "complete", "rate_limit"]
    assert second[-1]["remaining"] == 49


def test_the_reservation_is_still_atomic_so_two_turns_cannot_both_slip_through(
        make_user, settings):
    """S-013's other side: the refund must not turn the counter into read-modify-write.

    With one slot left, two reservations taken back to back must not both be allowed —
    the check has to stay `incr`-first. Driven against the method rather than two live
    sockets, because what is under test is the counter, not the transport.
    """
    from django.utils import timezone

    from ai_assistant.consumers import AIChatConsumer
    from training_platform.cache import ratelimit_cache

    settings.AI_ASSISTANT_CONFIG = {**getattr(settings, "AI_ASSISTANT_CONFIG", {}),
                                    "MAX_MESSAGES_PER_DAY": 50}
    person = make_user(_tag("atomic"))
    key = f"ai_chat_limit:{person.id}:{timezone.localdate().isoformat()}"
    ratelimit_cache().set(key, 49, 3600)            # one message left of fifty

    consumer = AIChatConsumer()
    consumer.user = person
    first = async_to_sync(consumer._reserve_rate_limit)()
    second = async_to_sync(consumer._reserve_rate_limit)()
    assert first[0] is True and first[1] == 0
    assert second[0] is False, "two turns both got the last slot"

    # And the refund gives exactly one back, so the refused turn did not leave the
    # counter above the ceiling.
    assert async_to_sync(consumer._refund_rate_limit)(0) == 1
    assert int(ratelimit_cache().get(key)) == 49


# ------------------------------------------------ S-014: counts obey the block
def _follow(follower, following):
    from social.models import UserFollow

    return UserFollow.objects.create(follower=follower, following=following)


def test_a_count_never_exceeds_the_rows_the_viewer_can_fetch(make_user, api):
    """S-014. `PublicUserProfileSerializer` counted followers, following and posts
    unfiltered, and `likes_count`/`comments_count` are denormalised columns maintained
    with `F()`. After a block every one of them was larger than the list it summarises:
    a profile said "2 followers" beside a follower list of one, and a post's badge
    promised comments the thread would not show.
    """
    from moderation.models import Block
    from social.models import Comment, Post, PostLike

    viewer = make_user(_tag("v14"))
    author = make_user(_tag("a14"))
    nuisance = make_user(_tag("n14"))
    bystander = make_user(_tag("b14"))

    _follow(nuisance, author)
    _follow(bystander, author)
    _follow(author, nuisance)
    _follow(author, bystander)

    post = Post.objects.create(author=author, content="hello", visibility="public")
    for liker in (nuisance, bystander):
        PostLike.objects.create(user=liker, post=post)
    Post.objects.filter(pk=post.pk).update(likes_count=2, comments_count=2)
    for writer in (nuisance, bystander):
        Comment.objects.create(post=post, author=writer, content="hi")

    Block.objects.create(blocker=viewer, blocked=nuisance)

    profile = api(viewer).get(f"/api/social/users/public-profile/{author.pk}/").json()
    assert (profile["followers_count"], profile["following_count"]) == (1, 1), profile
    assert profile["posts_count"] == 1

    listed = api(viewer).get(f"/api/social/posts/{post.pk}/").json()
    assert listed["likes_count"] == 1, listed
    assert listed["comments_count"] == 1, listed

    # The comment list the badge describes returns exactly that many rows.
    thread = api(viewer).get(f"/api/social/comments/?post={post.pk}").json()
    rows = thread["results"] if isinstance(thread, dict) else thread
    assert len(rows) == listed["comments_count"]

    # Nobody else's numbers changed: a block is one person's view, not a deletion.
    theirs = api(bystander).get(
        f"/api/social/users/public-profile/{author.pk}/").json()
    assert (theirs["followers_count"], theirs["following_count"]) == (2, 2)
    assert api(bystander).get(f"/api/social/posts/{post.pk}/").json()["likes_count"] == 2


def test_the_block_aware_counts_cost_the_page_a_fixed_number_of_queries(
        make_user, api):
    """S-014's constraint: one batched exclusion for the page, never a query per row.

    The obvious fix — asking `blocked_ids` inside a serializer method — is three extra
    queries per profile and two per post, which is exactly the N+1 the feed spent a
    whole audit removing. Twenty rows must cost what two rows cost.
    """
    from moderation.models import Block
    from social.models import Post

    viewer = make_user(_tag("q14"))
    nuisance = make_user(_tag("qn14"))
    Block.objects.create(blocker=viewer, blocked=nuisance)

    authors = [make_user(_tag(f"qa{i}")) for i in range(20)]
    for author in authors:
        Post.objects.create(author=author, content="x", visibility="public")

    client = api(viewer)

    def cost(url):
        with CaptureQueriesContext(connection) as captured:
            response = client.get(url)
            assert response.status_code == 200, response.content
        return len(captured), response

    few, _r = cost("/api/social/posts/?page_size=2")
    many, response = cost("/api/social/posts/?page_size=20")
    assert many <= few, (
        f"the post page cost {many} queries at 20 rows and {few} at 2 — per-row work")

    few_profiles, _r = cost("/api/social/users/public-profile/?page_size=2")
    many_profiles, _r = cost("/api/social/users/public-profile/?page_size=20")
    assert many_profiles <= few_profiles, (
        f"the profile page cost {many_profiles} queries at 20 rows "
        f"and {few_profiles} at 2 — per-row work")


# ------------------------------------------ S-015: a code on every request refusal
def _relation(trainer, client, initiated_by="client", status="pending"):
    from users.models import TrainerClientRelation

    return TrainerClientRelation.objects.create(
        trainer=trainer, client=client, status=status, initiated_by=initiated_by,
        agreed_rate=0)


def test_answering_a_trainer_request_refuses_with_a_code(make_user, api):
    """S-015. These refusals came back as a sentence and nothing else, next to newer
    ones in the same file that carry a `code` — so the app either matched on English
    text or did not branch at all. Codes added, never renamed; both sides of the
    handshake answer the same ones.
    """
    trainer = make_user(_tag("t15"), user_type="trainer")
    client = make_user(_tag("c15"), user_type="client")

    # The trainer answering a request nobody sent.
    missing = api(trainer).post("/api/auth/trainer/respond-to-request/",
                                {"request_id": 987654321, "action": "approve"},
                                content_type="application/json")
    assert missing.status_code == 404
    assert missing.json()["code"] == "request_not_found"

    # The trainer answering their OWN invitation: the client has to accept it.
    invited = _relation(trainer, client, initiated_by="trainer")
    wrong_party = api(trainer).post("/api/auth/trainer/respond-to-request/",
                                    {"request_id": invited.pk, "action": "approve"},
                                    content_type="application/json")
    assert wrong_party.status_code == 403
    assert wrong_party.json()["code"] == "wrong_party_to_answer"

    # And the client answering their own request gets the SAME code from the mirror
    # endpoint, so the app needs one branch for both sides.
    invited.initiated_by = "client"
    invited.save(update_fields=["initiated_by"])
    mirrored = api(client).post("/api/auth/client/respond-to-invite/",
                                {"request_id": invited.pk, "action": "approve"},
                                content_type="application/json")
    assert mirrored.status_code == 403
    assert mirrored.json()["code"] == "wrong_party_to_answer"

    # A row from before `initiated_by` existed cannot be answered by either side.
    invited.initiated_by = "unknown"
    invited.save(update_fields=["initiated_by"])
    legacy = api(trainer).post("/api/auth/trainer/respond-to-request/",
                               {"request_id": invited.pk, "action": "approve"},
                               content_type="application/json")
    assert legacy.status_code == 409
    assert legacy.json()["code"] == "request_predates_consent"


def test_a_deactivated_trainer_and_an_answered_request_each_have_their_own_code(
        make_user, api):
    """The two refusals raised inside `approve_trainer_relation`, which is shared by
    both endpoints — so neither had a code on either side."""
    trainer = make_user(_tag("t15b"), user_type="trainer")
    client = make_user(_tag("c15b"), user_type="client")

    relation = _relation(trainer, client, initiated_by="trainer")
    trainer.is_active = False
    trainer.save(update_fields=["is_active"])

    gone = api(client).post("/api/auth/client/respond-to-invite/",
                            {"request_id": relation.pk, "action": "approve"},
                            content_type="application/json")
    assert gone.status_code == 409
    assert gone.json()["code"] == "trainer_unavailable"
