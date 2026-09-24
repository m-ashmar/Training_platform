"""BACKEND_BUGS B-082..B-085: what the app session found wiring the AI screens
(2026-09-24), one test per row.
"""
import inspect
import re
import uuid
from datetime import datetime, timedelta

import pytest
from django.utils import timezone

from tests.test_fx_people import _ai_subscriber

pytestmark = pytest.mark.django_db

JSON = "application/json"


def _walk(client, url):
    """Every row of a cursor-paged list, following `next` to the end."""
    rows, seen_pages = [], 0
    while url:
        page = client.get(url)
        assert page.status_code == 200, page.content
        body = page.json()
        rows += body["results"]
        url = body["next"]
        seen_pages += 1
        assert seen_pages < 50, "the cursor never ended"
    return rows


# ------------------------------------------------------------------------ B-082
def test_b082_the_chat_list_is_paged_by_last_activity(make_user, api):
    """`sessions/` shared the messages' CursorPagination, whose `-created_at` replaced
    the view's `order_by('-updated_at')`: a chat started long ago and continued today
    sat on a late page. It is paged by last activity now, with a tiebreak, and the
    messages keep their own order."""
    from ai_assistant.models import ChatMessage, ChatSession

    person = _ai_subscriber(make_user, "b082")
    now = timezone.now()

    def chat(title, created_days_ago, updated_days_ago):
        row = ChatSession.objects.create(user=person, title=title)
        ChatSession.objects.filter(pk=row.pk).update(
            created_at=now - timedelta(days=created_days_ago),
            updated_at=now - timedelta(days=updated_days_ago))
        return ChatSession.objects.get(pk=row.pk)

    continued = chat("started a month ago, continued today", 30, 0)
    recent = chat("started yesterday", 1, 1)
    older = chat("started last week", 7, 7)
    # Two chats last touched in the same instant: one order, each shown once.
    twin_a = chat("twin a", 3, 2)
    twin_b = chat("twin b", 3, 2)
    ChatSession.objects.filter(pk=twin_b.pk).update(updated_at=twin_a.updated_at)

    client = api(person)
    first = client.get("/api/ai/sessions/?page_size=1")
    assert first.status_code == 200, first.content
    assert first.json()["results"][0]["session_id"] == str(continued.session_id), \
        "the chat continued today is not on the first page"

    walked = [row["session_id"] for row in _walk(client, "/api/ai/sessions/?page_size=2")]
    expected = [str(s.session_id) for s in sorted(
        ChatSession.objects.filter(user=person), key=lambda s: (s.updated_at, s.id),
        reverse=True)]
    assert walked == expected, "not ordered by last activity, then id"
    assert expected[:2] == [str(continued.session_id), str(recent.session_id)]
    assert expected[-1] == str(older.session_id)
    assert len(set(walked)) == 5, "a chat was repeated or skipped across pages"

    # The messages still page newest-created first.
    earlier = ChatMessage.objects.create(session=continued, role="user", content="first")
    later = ChatMessage.objects.create(session=continued, role="assistant", content="reply")
    ChatMessage.objects.filter(pk=earlier.pk).update(created_at=now - timedelta(minutes=5))
    messages = client.get(f"/api/ai/sessions/{continued.session_id}/messages/").json()
    assert [m["id"] for m in messages["results"]] == [later.id, earlier.id]


# ------------------------------------------------------------------------ B-083
def test_b083_what_the_assistant_keeps_is_readable_whenever_it_can_be_deleted(make_user,
                                                                               api):
    """Nothing reported what the assistant keeps outside the plan and consent gates,
    while `DELETE data/` works without either: after the plan lapsed or consent was
    withdrawn, a person could delete what they could not see. `GET data/` answers
    with the delete's own gate, and says what the delete then removes."""
    from ai_assistant.models import AITrainingData, ChatMessage, ChatSession

    # No plan and no AI consent: the history routes refuse, the data route answers.
    person = make_user(f"b083_{uuid.uuid4().hex[:8]}")
    client = api(person)
    assert client.get("/api/ai/sessions/").status_code == 403

    empty = client.get("/api/ai/data/")
    assert empty.status_code == 200, empty.content
    assert empty.json()["total_records"] == 0
    assert empty.json()["oldest_session_at"] is None

    now = timezone.now()
    first = ChatSession.objects.create(user=person, title="oldest")
    second = ChatSession.objects.create(user=person, title="newer")
    oldest_at = now - timedelta(days=40)
    ChatSession.objects.filter(pk=first.pk).update(created_at=oldest_at)
    ChatSession.objects.filter(pk=second.pk).update(created_at=now - timedelta(days=2))
    for session in (first, second):
        ChatMessage.objects.create(session=session, role="user", content="q")
        ChatMessage.objects.create(session=session, role="assistant", content="a")
    # Setup, not an assertion: since the owner's decision of 2026-09-24 a training copy
    # has no user or chat FK and is stored under the person's keyed pseudonym.
    from ai_assistant.anonymise import subject_key_for
    AITrainingData.objects.create(subject_key=subject_key_for(person), consented=True,
                                  user_context_snapshot={}, user_message="q",
                                  ai_response="a")

    summary = client.get("/api/ai/data/")
    assert summary.status_code == 200, summary.content
    body = summary.json()
    assert set(body) == {"total_records", "details", "oldest_session_at"}
    assert (body["details"]["sessions"], body["details"]["messages"],
            body["details"]["training_records"]) == (2, 4, 1)
    assert body["total_records"] == sum(body["details"].values())
    assert datetime.fromisoformat(body["oldest_session_at"]) == oldest_at

    # Reading deleted nothing, and what it said would go is what the delete reports.
    assert ChatSession.objects.filter(user=person).count() == 2
    deleted = client.delete("/api/ai/data/").json()
    assert deleted["details"] == body["details"]
    assert deleted["deleted_records"] == body["total_records"]
    assert client.get("/api/ai/data/").json()["total_records"] == 0

    assert api().get("/api/ai/data/").status_code == 401


# ------------------------------------------------------------------------ B-084
def test_b084_help_us_improve_is_set_by_the_person_and_nothing_else(make_user, api):
    """`CustomUser.ai_training_consent` had no writer, so the switch the stores review
    could not reach the server. The person reads and sets it; it is off until they do,
    only a JSON boolean sets it, when is recorded, and switching it off stops the
    collector at once — even for a socket that loaded the person while it was on — and
    removes what was kept on the strength of it."""
    from ai_assistant.models import AITrainingData, ChatMessage, ChatSession
    from ai_assistant.services.data_collector import DataCollector
    from users.models import CustomUser

    person = make_user(f"b084_{uuid.uuid4().hex[:8]}")
    client = api(person)
    url = "/api/ai/training-consent/"

    never = client.get(url)
    assert never.status_code == 200, never.content
    assert never.json() == {"granted": False, "set_at": None}

    # Only the person's explicit `true` opts them in.
    for body in ({}, {"granted": "true"}, {"granted": 1}, {"granted": None}, []):
        refused = client.post(url, body, content_type=JSON)
        assert (refused.status_code, refused.json()["code"]) == (400, "validation_error"), \
            (body, refused.content)
    person.refresh_from_db()
    assert (person.ai_training_consent, person.ai_training_consent_set_at) == (False, None)

    granted = client.post(url, {"granted": True}, content_type=JSON)
    assert granted.status_code == 200, granted.content
    assert granted.json()["granted"] is True and granted.json()["set_at"]
    assert client.get(url).json() == granted.json()
    person.refresh_from_db()
    assert person.ai_training_consent is True
    assert person.ai_training_consent_set_at is not None

    # On: a turn is kept. `socket_user` is the instance a socket loaded at connect.
    socket_user = CustomUser.objects.get(pk=person.pk)
    session = ChatSession.objects.create(user=person)
    reply = ChatMessage.objects.create(session=session, role="assistant", content="a")
    turn = dict(session=session, user_message="q", ai_response="a", tools_called=[],
                tool_results=[], message_id=reply.id)
    DataCollector().log_interaction(user=socket_user, **turn)
    # The owner's decision of 2026-09-24: a training copy has no user FK, so "this
    # person's copies" is `copies_of` (the keyed pseudonym). Same count, same meaning.
    assert AITrainingData.copies_of(person).count() == 1

    # Off: what was kept goes, and the next turn keeps nothing, although the socket's
    # instance still says True.
    revoked = client.post(url, {"granted": False}, content_type=JSON)
    assert revoked.status_code == 200, revoked.content
    assert revoked.json()["granted"] is False and revoked.json()["set_at"]
    assert AITrainingData.copies_of(person).count() == 0
    assert socket_user.ai_training_consent is True
    DataCollector().log_interaction(user=socket_user, **turn)
    assert AITrainingData.copies_of(person).count() == 0, \
        "the collector trusted the socket's stale copy of the consent"

    # It is not the AI consent, and it needs neither that nor a plan.
    from moderation.models import AIConsent
    assert not AIConsent.objects.filter(user=person).exists()
    assert api().get(url).status_code == 401


# ------------------------------------------------------------------------ B-085
def test_b085_the_ai_data_docstring_is_the_contract(make_user, api):
    """`GDPRDataDeleteView`'s docstring — the mobile contract — named no header, no
    body and no code. It names them now, and every key the responses carry."""
    from ai_assistant.views import (AITrainingConsentView, ChatSessionListView,
                                    GDPRDataDeleteView)

    def documents(doc, *, codes=(), keys=(), words=()):
        missing = [f"`{c}`" for c in codes if f"`{c}`" not in doc]
        missing += [k for k in keys if not re.search(rf"\b{re.escape(k)}\b", doc)]
        missing += [w for w in words if w not in doc]
        return missing

    person = _ai_subscriber(make_user, "b085")
    client = api(person)
    deleted = client.delete("/api/ai/data/").json()
    summary = client.get("/api/ai/data/").json()
    assert set(deleted) == {"message", "deleted_records", "details"}
    assert not documents(
        inspect.getdoc(GDPRDataDeleteView),
        codes=("not_authenticated", "method_not_allowed"),
        keys=(*deleted, *deleted["details"], *summary),
        words=("Authorization", "DELETE", "GET")), "GDPRDataDeleteView"

    assert not documents(
        inspect.getdoc(ChatSessionListView),
        codes=("no_entitlement", "feature_not_in_plan", "ai_consent_required",
               "not_authenticated"),
        keys=("updated_at", "page_size", "next"), words=("Authorization",)), \
        "ChatSessionListView"

    consent = client.get("/api/ai/training-consent/").json()
    assert not documents(
        inspect.getdoc(AITrainingConsentView),
        codes=("validation_error", "not_authenticated", "method_not_allowed"),
        keys=tuple(consent), words=("Authorization", "ai-consent/")), \
        "AITrainingConsentView"
