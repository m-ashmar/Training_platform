"""Cross-cutting fixes: the privacy registry, the response cache, the theme's contrast
guard, the contract, the audit probes. One test per SIDE_BUGS row, named after it.

    S-080  the export told a blocked person who had blocked them
    S-146  an erased sender's name and words stayed in the other side's inbox
    S-147  a reporter's export named the operator who handled each report
    S-145  a cache hit rewrote its own entry, so the TTL never ran out
    S-108  a transparent media veil or glass saved, and text on photos went unreadable
    S-087  the contract sent the coaching price to a route that does not exist
    S-054  fifty audit probes read as a security suite and nothing ever ran them
"""
import importlib.util
import json
import re
import time
import uuid
from pathlib import Path

import pytest
from django.core.exceptions import ValidationError

REPO_ROOT = Path(__file__).resolve().parent.parent
JSON = "application/json"


def _export(api, user):
    response = api(user).get("/api/privacy/export/")
    assert response.status_code == 200, response.content
    return json.loads(response.content)["sections"]


# ------------------------------------------ S-080: a block against you stays secret
@pytest.mark.django_db
def test_s080_a_blocked_person_never_learns_who_blocked_them(make_user, api):
    """`GET blocks/` shows only the blocks a person MADE, and the contract says one made
    against them is never disclosed. The export was registered on both sides and
    exported every column, so the blocked person's download named the blocker — and
    the erasure preview counted the row, which says "you were blocked" on its own."""
    from moderation.models import Block
    from training_platform import privacy

    blocker = make_user(f"s080_a_{uuid.uuid4().hex[:6]}")
    blocked = make_user(f"s080_b_{uuid.uuid4().hex[:6]}")
    block = Block.objects.create(blocker=blocker, blocked=blocked)

    assert _export(api, blocked)["blocks"] == []
    mine = _export(api, blocker)["blocks"]
    assert [(row["id"], row["blocked"]) for row in mine] == [(block.pk, blocked.pk)]
    assert set(mine[0]) == {"id", "blocked", "created_at"}

    assert privacy.erase_user_data(blocked, dry_run=True)["blocks"]["rows"] == 0
    assert privacy.erase_user_data(blocker, dry_run=True)["blocks"]["rows"] == 1

    # And the rows still go with the account, on either side: the erased person does
    # not linger in someone else's block list.
    privacy.erase_user_data(blocked)
    assert not Block.objects.filter(pk=block.pk).exists()


# ------------------------------- S-146: the other side's inbox forgets you as well
@pytest.mark.django_db
def test_s146_erasing_a_sender_takes_their_words_out_of_the_other_inbox(make_user, api):
    """Erasure deletes the conversation "rather than leaving the other side reading half
    a conversation". The `chat_message` notice in the other side's inbox holds the
    sender's name and the first 120 characters of the message, and was registered only
    under its recipient — so it outlived the thread by up to 90 days."""
    from chat.services import open_conversation
    from notifications.models import Notification
    from training_platform import privacy
    from users.models import TrainerClientRelation

    trainer = make_user(f"s146_t_{uuid.uuid4().hex[:6]}", user_type="trainer")
    client = make_user(f"s146_c_{uuid.uuid4().hex[:6]}")
    TrainerClientRelation.objects.create(trainer=trainer, client=client,
                                         status="approved", initiated_by="client")
    conversation = open_conversation(trainer, client)
    words = "my knee has been bad since the accident"
    sent = api(client).post(f"/api/v1/chat/conversations/{conversation.pk}/messages/",
                            {"body": words}, content_type=JSON)
    assert sent.status_code == 201, sent.content
    notices = Notification.objects.filter(recipient=trainer, event_type="chat_message")
    assert notices.filter(actor=client).exists(), "the send wrote no notice to delete"

    # Someone else's notice to the same person is not the erased account's to take.
    unrelated = Notification.objects.create(
        recipient=trainer, event_type="coaching_renewed",
        deduplication_key=f"s146-{uuid.uuid4().hex}", metadata={"context": {}})

    # Not counted in the preview: the subject is shown it, and it is not their inbox.
    own = Notification.objects.filter(recipient=client).count()
    assert privacy.erase_user_data(client, dry_run=True)["notifications"]["rows"] == own

    privacy.erase_user_data(client)

    assert not notices.exists()
    assert Notification.objects.filter(pk=unrelated.pk).exists()
    inbox = api(trainer).get("/api/social/notifications/")
    assert inbox.status_code == 200, inbox.content
    assert words not in inbox.content.decode()


# ----------------------------------- S-147: who handled a report is never returned
@pytest.mark.django_db
def test_s147_a_reporters_export_never_names_the_operator(make_user, api):
    """`ReportSerializer` leaves `handled_by` out on purpose and the contract says who
    handled a report is never returned. The export had no field list, so it carried
    every column — the operator's id among them."""
    from django.utils import timezone

    from moderation.models import Report
    from moderation.serializers import ReportSerializer

    reporter = make_user(f"s147_r_{uuid.uuid4().hex[:6]}")
    operator = make_user(f"s147_op_{uuid.uuid4().hex[:6]}", user_type="admin")
    Report.objects.create(reporter=reporter, target_type="user",
                          target_id=operator.pk + 1000, reason="spam", note="again",
                          status="dismissed", handled_by=operator,
                          handled_at=timezone.now(), resolution_note="not spam")

    rows = _export(api, reporter)["reports_made"]
    assert len(rows) == 1
    assert "handled_by" not in rows[0]
    assert operator.pk not in rows[0].values()
    # Exactly what the reporter's own list shows them, no more and no less.
    assert set(rows[0]) == set(ReportSerializer.Meta.fields)


# ---------------------------------------- S-145: a hit does not refresh the entry
@pytest.mark.django_db
def test_s145_a_cache_hit_does_not_extend_the_entry(monkeypatch):
    """Django runs `process_response` for the response `process_request` returns, and
    that wrote every hit back with a fresh TTL: an entry read once per TTL never
    expired. Measured on the clock: stored at t, read at t+200, it must be gone at
    t+350 on a 300 s route."""
    from django.http import JsonResponse
    from django.test import RequestFactory

    from training_platform.cache_config import match_route
    from training_platform.middleware import CacheMiddleware

    path = "/api/achievements/"
    ttl = match_route(path)["ttl"]
    clock = [1_900_000_000.0]
    monkeypatch.setattr(time, "time", lambda: clock[0])

    served = []

    def view(request):
        served.append(1)
        return JsonResponse({"served": len(served)})

    middleware = CacheMiddleware(view)
    query = {"fx": uuid.uuid4().hex}         # a key no other test has written
    factory = RequestFactory()

    middleware(factory.get(path, query))
    assert len(served) == 1

    clock[0] += ttl - 100
    hit = middleware(factory.get(path, query))
    assert len(served) == 1, "the second read should have been a hit"
    varies = {part.strip().lower() for part in hit["Vary"].split(",")}
    assert {"accept-language", "authorization", "x-app-channel",
            "x-app-version"} <= varies, "a hit must still say what it varies by"

    clock[0] += 150                          # ttl + 50 after the only write
    middleware(factory.get(path, query))
    assert len(served) == 2, "the hit extended the entry past its TTL"


# ------------------------------- S-108: a see-through backdrop is measured as drawn
@pytest.mark.django_db
@pytest.mark.parametrize("tokens, backdrop", [
    ({"media-veil": {"dark": "rgba(0,0,0,0)"}}, "media-veil"),
    ({"media-veil": {"light": "#00000000"}}, "media-veil"),
    ({"media-veil": {"dark": "rgba(11,12,14,0.3)"}}, "media-veil"),   # thin, not gone
    ({"glass": {"light": "rgba(255,255,255,0)"}}, "glass"),
    ({"glass": {"dark": "#10121600"}}, "glass"),
])
def test_s108_a_veil_or_glass_that_leaves_text_unreadable_is_refused(tokens, backdrop):
    """The guard skipped every translucent value and no pair named one, while the
    grammar takes any alpha down to 0 on these tokens. So a transparent veil saved and
    shipped, and text on photos had nothing under it."""
    from policy.models import ThemeOverride

    with pytest.raises(ValidationError) as caught:
        ThemeOverride.objects.create(name="s108", channels=["apk"], is_enabled=True,
                                     tokens=tokens)
    message = " ".join(str(m) for m in caught.value.message_dict["tokens"])
    assert f"ink on {backdrop}" in message and "4.5" in message


@pytest.mark.django_db
def test_s108_the_shipped_design_and_a_readable_backdrop_still_save():
    """The guard refuses bad edits, not the design the app ships."""
    from policy.models import ThemeOverride
    from policy.theme_tokens import contrast_failures

    assert contrast_failures({}) == []
    row = ThemeOverride.objects.create(
        name="s108 ok", channels=["apk"], is_enabled=True,
        tokens={"media-veil": {"dark": "rgba(11,12,14,0.85)"},
                "glass": {"light": "rgba(255,255,255,0.6)"}})
    assert row.pk


def test_s108_every_see_through_token_is_either_measured_or_carries_no_text():
    """A new translucent token must be classified, or it slips past the guard the way
    these three did."""
    from policy.theme_tokens import (
        BACKDROP_TEXT_PAIRS,
        TRANSLUCENT_COLORS,
        TRANSLUCENT_WITHOUT_TEXT,
        worst_case_ratio,
    )

    measured = {backdrop for _fg, _theme, backdrop in BACKDROP_TEXT_PAIRS}
    assert not measured & TRANSLUCENT_WITHOUT_TEXT
    assert measured | TRANSLUCENT_WITHOUT_TEXT == set(TRANSLUCENT_COLORS)
    # Text as bright as some photo seen through the backdrop has no contrast at all.
    assert worst_case_ratio("#808080", "rgba(0,0,0,0)") == 1.0
    assert round(worst_case_ratio("#FFFFFF", "#000000"), 2) == 21.0


# -------------------------------- S-087: the contract names the route that answers
@pytest.mark.django_db
def test_s087_the_coaching_price_is_documented_on_the_route_that_serves_it():
    """`coaching_currency` was documented on `GET /api/v1/coaching/status/`, which does
    not exist; the app would have asked a 404 for the price."""
    from django.urls import Resolver404, resolve

    from users.coaching_views import CurrentCoachingView

    text = (REPO_ROOT / "API_CONTRACT.md").read_text(encoding="utf-8")
    line = next(l for l in text.splitlines()
                if "A coaching price is in the client's currency too" in l)
    documented = re.search(r"`GET (/api/[^`]+)`", line).group(1)
    assert resolve(documented).func.view_class is CurrentCoachingView

    for path in sorted(set(re.findall(r"/api/v1/coaching/[a-z_\-/]*", text))):
        try:
            resolve(path)
        except Resolver404:
            pytest.fail(f"API_CONTRACT.md names {path}, which does not exist")


# ------------------------------------- S-054: the probes can be run, and say so
def _runner():
    path = REPO_ROOT / "tests" / "security" / "run_probes.py"
    spec = importlib.util.spec_from_file_location("fx_cross_run_probes", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("output", [
    "  [FAIL] unsafe foods never reach the optimiser",
    "  *** FAIL *** upload of an SVG was accepted",
    "  [403] *** REGRESSION ***  client reads OWN streaks",
    "  *** STILL DELETED — FIX FAILED ***",
    "routes=210 requests=420 5xx/exceptions=3",
    "5xx / exceptions: 1",
    "RESULT: PASS=11 FAIL=2",
    "chat: *** FAILS OPEN — LLM invoked with no working limiter ***",
])
def test_s054_a_probe_that_prints_a_failure_fails(output):
    """Every probe exits 0 whatever it prints, so the exit status alone said nothing.
    The runner reads each failure mark a probe here actually prints."""
    status, reason = _runner().verdict(0, f"setting up\n{output}\ndone\n")
    assert status == "failed" and reason


def test_s054_the_runner_tells_a_pass_from_a_crash_from_an_observation():
    runner = _runner()
    assert runner.verdict(0, "  [PASS] nosniff header present\n")[0] == "passed"
    assert runner.verdict(0, "routes=210 5xx/exceptions=0\n")[0] == "passed"
    assert runner.verdict(1, "Traceback (most recent call last):\nKeyError: 'x'\n") == (
        "failed", "exit 1: KeyError: 'x'")
    assert runner.verdict(None, "")[0] == "failed"
    # Nothing to pass or fail: reported as run, never as passed.
    assert runner.verdict(0, "settings.TIME_ZONE = Asia/Damascus\n")[0] == "ran"


def test_s054_every_probe_is_found_and_runs_on_a_database_of_its_own():
    """A probe builds and drops `test_<DB_NAME>` — the database a pytest run on the
    same `DB_NAME` is using. The runner moves each probe to its own."""
    runner = _runner()
    probes = runner.discover()
    on_disk = sorted((REPO_ROOT / "tests" / "security").glob("*.py"))
    assert len(probes) == len(on_disk) - 1 > 40
    assert "run_probes" not in {p.stem for p in probes}
    assert runner.probe_env({"DB_NAME": "tp_mine"})["DB_NAME"] == "tp_mine_probe"


def test_s054_the_readme_no_longer_promises_a_suite_nobody_runs():
    text = (REPO_ROOT / "tests" / "security" / "README.md").read_text(encoding="utf-8")
    assert "Phase 14 converts them" not in text
    assert "run_probes.py" in text
