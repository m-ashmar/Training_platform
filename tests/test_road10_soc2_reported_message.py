"""The fix run's SOC-2, S-188 (2026-11-24): a reported chat message is QUOTED on the
report's page in the operator console. A message has no console page (and must not get
one: that would put every private conversation on the console), so a `coach_message` or
`chat_message` report showed only "Coach message #N" and nothing to judge. The page now
shows who wrote it, when, and the text, escaped; a message its sender took back still
shows, so said. The first three tests fail on the code before; the fourth guards the
link the other kinds keep.
"""
import uuid

import pytest

pytestmark = pytest.mark.django_db

JSON = "application/json"


def _name(tag):
    return f"s188_{tag}_{uuid.uuid4().hex[:6]}"


def _report(api, who, target_type, target_id):
    from moderation.models import Report

    filed = api(who).post("/api/v1/reports/", {"target_type": target_type,
                                              "target_id": target_id,
                                              "reason": "harassment"},
                          content_type=JSON)
    assert filed.status_code == 201, filed.content
    return Report.objects.get(reporter=who, target_type=target_type, target_id=target_id)


def _page(client, make_user, report):
    client.force_login(make_user(_name("op"), user_type="admin"))
    page = client.get(f"/dj-admin/moderation/report/{report.pk}/change/")
    assert page.status_code == 200
    return page.content.decode()


def _thread(make_user):
    from chat.services import open_conversation
    from users.models import TrainerClientRelation

    trainer = make_user(_name("t"), user_type="trainer", first_name="Rami")
    client = make_user(_name("c"))
    TrainerClientRelation.objects.create(trainer=trainer, client=client, status="approved",
                                         initiated_by="client")
    return trainer, client, open_conversation(trainer, client)


def test_a_reported_coach_message_is_quoted_on_its_report(make_user, api, client):
    from chat.models import Message

    trainer, athlete, conversation = _thread(make_user)
    said = Message.objects.create(conversation=conversation, sender=trainer, kind="text",
                                  body="You are useless <b>and</b> lazy")
    page = _page(client, make_user, _report(api, athlete, "coach_message", said.pk))
    assert "You are useless &lt;b&gt;and&lt;/b&gt; lazy" in page       # escaped
    assert "Rami" in page and f"#{trainer.pk}" in page


def test_a_reported_ai_coach_answer_is_quoted(make_user, api, client):
    from ai_assistant.models import ChatMessage, ChatSession

    person = make_user(_name("ai"))
    session = ChatSession.objects.create(user=person)
    answer = ChatMessage.objects.create(session=session, role="assistant",
                                        content="Skip meals for a week to cut fast.")
    page = _page(client, make_user, _report(api, person, "chat_message", answer.pk))
    assert "Skip meals for a week to cut fast." in page and "the AI coach" in page


def test_a_message_taken_back_is_still_quoted_and_says_so(make_user, api, client):
    from django.utils import timezone

    from chat.models import Message

    trainer, athlete, conversation = _thread(make_user)
    said = Message.objects.create(conversation=conversation, sender=trainer, kind="text",
                                  body="deleted a minute later")
    report = _report(api, athlete, "coach_message", said.pk)
    Message.objects.filter(pk=said.pk).update(deleted_at=timezone.now())
    page = _page(client, make_user, report)
    assert "deleted a minute later" in page and "took it back" in page


def test_other_targets_keep_their_link_and_quote_nothing(make_user, api, client):
    from social.models import Post

    author, reader = make_user(_name("au")), make_user(_name("rd"))
    post = Post.objects.create(author=author, content="a post")
    page = _page(client, make_user, _report(api, reader, "social_post", post.pk))
    assert f"/dj-admin/social/post/{post.pk}/change/" in page
