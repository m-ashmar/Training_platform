"""The safety layer: reporting, blocking, the moderation queue and AI consent.

Store-review blockers B1, B2 and B5 (`PAYMENTS_COMPLIANCE_PLAN.md` step 7). What is
asserted here is the BEHAVIOUR a reviewer checks — a person can report content, block
another person and be sure the AI asked first — not the shape of the three tables.
"""
import uuid

import pytest

pytestmark = pytest.mark.django_db


# --------------------------------------------------------------------- helpers
def _post(author, *, visibility="public", content="hello"):
    from social.models import Post

    return Post.objects.create(author=author, content=content, visibility=visibility)


def _consent(user, *, granted=True, version="2026-09-01"):
    from django.utils import timezone

    from moderation.models import AIConsent

    row, _created = AIConsent.objects.get_or_create(user=user)
    if granted:
        row.granted_at = timezone.now()
        row.revoked_at = None
        row.policy_version = version
    else:
        row.revoked_at = timezone.now()
    row.save()
    return row


def _block(blocker, blocked):
    from moderation.models import Block

    return Block.objects.create(blocker=blocker, blocked=blocked)


def _report(api, reporter, target_type, target_id, reason="harassment", note=""):
    return api(reporter).post(
        "/api/v1/reports/",
        {"target_type": target_type, "target_id": target_id, "reason": reason,
         "note": note},
        content_type="application/json")


# ------------------------------------------------------------------- reporting
def test_reporting_something_you_cannot_see_is_refused(make_user, api):
    """The one answer for "gone" and "not yours", so the route cannot be used to
    discover which private rows exist."""
    from routine.models import Routine

    reporter = make_user(f"rep_{uuid.uuid4().hex[:6]}")
    stranger = make_user(f"str_{uuid.uuid4().hex[:6]}", user_type="trainer")
    private = Routine.objects.create(name="theirs", created_by=stranger)

    refused = _report(api, reporter, "routine", private.pk)
    assert (refused.status_code, refused.json()["code"]) == (404, "target_not_found")

    # A routine that does not exist at all answers identically — that is the point.
    missing = _report(api, reporter, "routine", private.pk + 10_000)
    assert (missing.status_code, missing.json()["code"]) == (404, "target_not_found")

    # And a followers-only post by someone the reporter does not follow.
    hidden = _post(stranger, visibility="followers")
    assert _report(api, reporter, "social_post", hidden.pk).status_code == 404


def test_you_cannot_report_yourself_but_you_can_report_what_the_ai_told_you(
        make_user, api):
    from ai_assistant.models import ChatMessage, ChatSession

    person = make_user(f"self_{uuid.uuid4().hex[:6]}")
    mine = _post(person)

    own_account = _report(api, person, "user", person.pk)
    assert (own_account.status_code, own_account.json()["code"]) == (
        403, "cannot_report_yourself")
    own_post = _report(api, person, "social_post", mine.pk)
    assert (own_post.status_code, own_post.json()["code"]) == (
        403, "cannot_report_yourself")

    # The assistant's reply lives in the person's own session and has no author, so
    # they can report it. A store reviewer asks for exactly this.
    session = ChatSession.objects.create(user=person)
    said_by_them = ChatMessage.objects.create(session=session, role="user", content="hi")
    said_by_ai = ChatMessage.objects.create(session=session, role="assistant",
                                            content="eat rocks")
    assert _report(api, person, "chat_message", said_by_them.pk).status_code == 403
    assert _report(api, person, "chat_message", said_by_ai.pk).status_code == 201


def test_a_second_report_of_the_same_thing_waits_for_the_first_to_be_handled(
        make_user, api):
    from moderation.models import Report

    reporter = make_user(f"dup_{uuid.uuid4().hex[:6]}")
    author = make_user(f"aut_{uuid.uuid4().hex[:6]}")
    post = _post(author)

    first = _report(api, reporter, "social_post", post.pk)
    assert first.status_code == 201

    again = _report(api, reporter, "social_post", post.pk, reason="spam")
    assert (again.status_code, again.json()["code"]) == (409, "already_reported")

    # Somebody else reporting the same post is not a duplicate.
    other = make_user(f"oth_{uuid.uuid4().hex[:6]}")
    assert _report(api, other, "social_post", post.pk).status_code == 201

    # Once it is handled, the same person may report the same post again: a second
    # offence is a new complaint, not a duplicate of the closed one.
    Report.objects.filter(pk=first.json()["id"]).update(status="dismissed")
    assert _report(api, reporter, "social_post", post.pk).status_code == 201

    mine = api(reporter).get("/api/v1/reports/mine/").json()
    assert mine["count"] == 2
    assert {r["target_type"] for r in mine["results"]} == {"social_post"}
    # The operator's identity is never returned to the reporter.
    assert "handled_by" not in mine["results"][0]


# -------------------------------------------------------------------- blocking
def test_blocking_hides_both_ways_in_social(make_user, api):
    """One row, two directions: neither sees the other's posts, comments, profile or
    notifications, and neither appears in the other's follower lists."""
    from social.models import Comment, UserFollow

    a = make_user(f"blk_a_{uuid.uuid4().hex[:6]}")
    b = make_user(f"blk_b_{uuid.uuid4().hex[:6]}")
    post_a, post_b = _post(a, content="from a"), _post(b, content="from b")
    UserFollow.objects.create(follower=b, following=a)
    Comment.objects.create(author=b, post=post_a, content="on a's post")

    def ids(user, path):
        body = api(user).get(path).json()
        rows = body["results"] if isinstance(body, dict) and "results" in body else body
        return {row["id"] for row in rows}

    assert post_b.pk in ids(a, "/api/social/posts/")
    assert post_a.pk in ids(b, "/api/social/posts/")

    blocked = api(a).post("/api/v1/blocks/", {"user_id": b.pk},
                          content_type="application/json")
    assert (blocked.status_code, blocked.json()["blocked"]) == (201, True)

    # A blocked B, so neither sees the other — the block is owned by A and felt by both.
    assert post_b.pk not in ids(a, "/api/social/posts/")
    assert post_a.pk not in ids(b, "/api/social/posts/")
    assert api(a).get("/api/social/comments/").json()["results"] == []
    assert api(a).get("/api/social/follows/followers/").json()["count"] == 0
    assert api(b).get("/api/social/follows/following/").json()["count"] == 0
    assert api(a).get(f"/api/social/users/public-profile/{b.pk}/").status_code == 404
    assert api(b).get(f"/api/social/users/public-profile/{a.pk}/").status_code == 404
    by_name = api(b).get("/api/social/users/public-profile/by_username/",
                         {"username": a.username})
    assert by_name.status_code == 404

    # Only the blocker sees the block, and only the blocker can lift it.
    assert api(a).get("/api/v1/blocks/").json()["count"] == 1
    assert api(b).get("/api/v1/blocks/").json()["count"] == 0
    assert api(a).delete(f"/api/v1/blocks/{b.pk}/").status_code == 204
    assert post_b.pk in ids(a, "/api/social/posts/")


def test_blocking_yourself_is_refused_and_unblocking_twice_is_not_an_error(
        make_user, api):
    person = make_user(f"self_blk_{uuid.uuid4().hex[:6]}")
    other = make_user(f"oth_blk_{uuid.uuid4().hex[:6]}")

    refused = api(person).post("/api/v1/blocks/", {"user_id": person.pk},
                               content_type="application/json")
    assert (refused.status_code, refused.json()["code"]) == (400, "cannot_block_yourself")
    assert api(person).delete(f"/api/v1/blocks/{other.pk}/").status_code == 204
    # Idempotent: the app retries, and a second block is the same state.
    for _ in range(2):
        assert api(person).post("/api/v1/blocks/", {"user_id": other.pk},
                                content_type="application/json").status_code == 201
    assert api(person).get("/api/v1/blocks/").json()["count"] == 1


def test_a_blocked_person_cannot_send_or_accept_a_trainer_request(make_user, api):
    from users.models import TrainerClientRelation

    client = make_user(f"cli_{uuid.uuid4().hex[:6]}")
    trainer = make_user(f"trn_{uuid.uuid4().hex[:6]}", user_type="trainer",
                        trainer_is_available=True, email_verified=True,
                        trainer_hourly_rate=0)
    _block(client, trainer)

    asked = api(client).post("/api/auth/client/request-trainer/",
                             {"trainer_id": trainer.pk}, content_type="application/json")
    assert (asked.status_code, asked.json()["code"]) == (403, "user_blocked")

    invited = api(trainer).post("/api/auth/trainer/assign-client/",
                                {"client_id": client.pk},
                                content_type="application/json")
    assert (invited.status_code, invited.json()["code"]) == (403, "user_blocked")

    # Not listed, and not matched. Asserted on the whole body rather than on one key:
    # these two endpoints do not share a response shape, and the claim is that the
    # trainer does not appear in either at all.
    import json as _json

    listed = _json.dumps(api(client).get("/api/auth/client/available-trainers/").json())
    matched = _json.dumps(api(client).get("/api/v1/trainers/matches/").json())
    assert trainer.username not in listed
    assert trainer.username not in matched

    # A request sent BEFORE the block cannot be accepted after it, or the block would
    # be one pending row away from meaningless.
    from django.utils import timezone

    relation = TrainerClientRelation.objects.create(
        trainer=trainer, client=client, status="pending", initiated_by="client",
        agreed_rate=0, requested_at=timezone.now())
    answered = api(trainer).post("/api/auth/trainer/respond-to-request/",
                                 {"request_id": relation.pk, "action": "approve"},
                                 content_type="application/json")
    assert (answered.status_code, answered.json()["code"]) == (403, "user_blocked")


def test_blocked_ids_is_symmetric_and_one_query(make_user, django_assert_num_queries):
    from moderation.services import blocked_ids, is_blocked

    a = make_user(f"svc_a_{uuid.uuid4().hex[:6]}")
    b = make_user(f"svc_b_{uuid.uuid4().hex[:6]}")
    c = make_user(f"svc_c_{uuid.uuid4().hex[:6]}")
    _block(a, b)   # a blocked b
    _block(c, a)   # c blocked a

    with django_assert_num_queries(1):
        assert blocked_ids(a) == {b.pk, c.pk}
    assert blocked_ids(b) == {a.pk}
    assert is_blocked(a, b) and is_blocked(b, a) and is_blocked(a, c)
    assert not is_blocked(b, c)


# ------------------------------------------------------------------ AI consent
def _chat(user, message="how is my squat?"):
    """The first event the real `ChatService.chat_stream` yields for this person.

    Run WITHOUT an event loop, on purpose, and it has to be. Two traps, neither of
    them about the guard:

    * `channels.db.database_sync_to_async` hands the ORM call to another thread, on
      its own connection, which cannot see the row this test just wrote inside an
      open transaction. Every consent assertion would then pass for the wrong reason.
      It is replaced by an inline shim so the same functions run here.
      `tests/test_c9_minor.py` unwraps the same decorator with `.func`.
    * Running the coroutine under `asyncio.run` does not fix it: inside a fresh event
      loop Django hands out a NEW connection too, and the tables read empty. Measured
      — `AIConsent.objects.values()` returned `[]` under `asyncio.run` for a row the
      same thread had just written and could read either side of it.

    So the coroutine is stepped by hand. Nothing before the first yield suspends once
    the shim is in place, so one `.send(None)` runs the sanitiser, the consent gate
    and the budget gate and stops at the first event — which is the refusal when
    consent is missing, and the "getting ready" status when it is not. What is under
    test — `chat_stream` itself — is unmodified.
    """
    from unittest import mock

    from ai_assistant.services import chat_service as chat_service_module

    def inline(func):
        async def run_now(*args, **kwargs):
            return func(*args, **kwargs)
        return run_now

    with mock.patch.object(chat_service_module, "database_sync_to_async", inline):
        stream = chat_service_module.ChatService().chat_stream(user, message)
        step = stream.asend(None)
        try:
            step.send(None)
        except StopIteration as first:
            return first.value
        except StopAsyncIteration:  # pragma: no cover — the stream always says something
            raise AssertionError("chat_stream yielded nothing at all")
        finally:
            step.close()
            stream.aclose().close()
        raise AssertionError("chat_stream suspended before saying anything")


def test_the_ai_refuses_without_consent_and_stops_refusing_with_it(make_user, settings):
    """The guard runs before the session row is written and before any completion, so
    a refused turn costs nothing and leaves nothing behind."""
    from ai_assistant.models import ChatSession

    person = make_user(f"ai_{uuid.uuid4().hex[:6]}")

    refused = _chat(person)
    assert refused["code"] == "ai_consent_required"
    assert refused["type"] == "error"
    assert not ChatSession.objects.filter(user=person).exists()

    _consent(person)
    # Past the consent gate now: the next gate it meets is a different one (there is
    # no OpenAI key in the test settings), which is all this needs to prove.
    assert _chat(person).get("code") != "ai_consent_required"


def test_revoking_consent_takes_effect_immediately(make_user, api):
    person = make_user(f"rev_{uuid.uuid4().hex[:6]}")

    granted = api(person).post("/api/v1/ai-consent/",
                               {"granted": True, "policy_version": "2026-09-01"},
                               content_type="application/json")
    assert (granted.status_code, granted.json()["granted"]) == (200, True)
    assert granted.json()["policy_version"] == "2026-09-01"
    assert _chat(person).get("code") != "ai_consent_required"

    revoked = api(person).post("/api/v1/ai-consent/", {"granted": False},
                               content_type="application/json")
    assert (revoked.status_code, revoked.json()["granted"]) == (200, False)
    # Nothing caches the answer: the very next message is refused.
    assert _chat(person)["code"] == "ai_consent_required"

    # And granting again clears the revocation rather than leaving both stamps.
    api(person).post("/api/v1/ai-consent/",
                     {"granted": True, "policy_version": "2026-09-01"},
                     content_type="application/json")
    state = api(person).get("/api/v1/ai-consent/").json()
    assert (state["granted"], state["revoked_at"]) == (True, None)


def test_never_asked_reads_as_not_granted_and_granting_needs_a_policy_version(
        make_user, api):
    person = make_user(f"ask_{uuid.uuid4().hex[:6]}")

    never = api(person).get("/api/v1/ai-consent/").json()
    assert never == {"granted": False, "policy_version": "", "granted_at": None,
                     "revoked_at": None}

    refused = api(person).post("/api/v1/ai-consent/", {"granted": True},
                               content_type="application/json")
    assert refused.status_code == 400
    assert "policy_version" in refused.json()["field_errors"]


def test_the_rest_side_of_the_assistant_refuses_without_consent(make_user, api):
    """One guard, two faces: `HasAIConsent` on the REST routes, the same code from
    `chat_stream` on the socket. Entitlement is still checked first."""
    from tests.test_c9_payments import _plan
    from subscription.models import Subscription

    person = make_user(f"rest_{uuid.uuid4().hex[:6]}")
    Subscription.objects.create(user=person, plan=_plan(f"ai {uuid.uuid4().hex[:6]}",
                                                        has_ai_advice=True),
                                status="active")

    refused = api(person).get("/api/ai/sessions/")
    assert (refused.status_code, refused.json()["code"]) == (403, "ai_consent_required")

    # Deleting what the assistant holds must keep working without consent — that is
    # the commonest reason somebody asks for the deletion.
    assert api(person).delete("/api/ai/data/").status_code == 200

    _consent(person)
    assert api(person).get("/api/ai/sessions/").status_code == 200


# --------------------------------------------------------------- the console
def _console(user, password="Xx!23456"):
    from django.test import Client

    client = Client()
    client.force_login(user)
    return client


def test_the_console_actions_write_who_closed_it_and_when(make_user):
    from django.contrib.admin.models import LogEntry

    from moderation.models import Report

    operator = make_user(f"ops_{uuid.uuid4().hex[:6]}", user_type="admin")
    reporter = make_user(f"rpt_{uuid.uuid4().hex[:6]}")
    author = make_user(f"ath_{uuid.uuid4().hex[:6]}")
    post = _post(author)
    opened = Report.objects.create(reporter=reporter, target_type="social_post",
                                   target_id=post.pk, reason="spam")

    console = _console(operator)
    queue = console.get("/dj-admin/moderation/report/")
    assert queue.status_code == 200
    # The badge: the queue says how many are waiting.
    assert b"1 waiting" in queue.content
    # Filters by status and by reason, and a link that opens the target.
    assert console.get("/dj-admin/moderation/report/?status__exact=open").status_code == 200
    assert console.get("/dj-admin/moderation/report/?reason__exact=spam").status_code == 200
    assert f'/dj-admin/social/post/{post.pk}/change/'.encode() in queue.content

    acted = console.post("/dj-admin/moderation/report/",
                         {"action": "mark_action_taken", "_selected_action": [opened.pk],
                          "index": "0", "select_across": "0"}, follow=True)
    assert acted.status_code == 200

    opened.refresh_from_db()
    assert opened.status == "actioned"
    assert opened.handled_by_id == operator.pk
    assert opened.handled_at is not None
    assert opened.resolution_note
    # Through save(), so the admin log has it: a bulk close with no record of who
    # closed it is the thing `handled_by` exists to prevent.
    assert LogEntry.objects.filter(object_id=str(opened.pk)).exists()

    from moderation.services import open_report_count
    assert open_report_count() == 0


def test_the_console_dismiss_action_closes_without_claiming_action_was_taken(make_user):
    from moderation.models import Report

    operator = make_user(f"ops2_{uuid.uuid4().hex[:6]}", user_type="admin")
    reporter = make_user(f"rpt2_{uuid.uuid4().hex[:6]}")
    grudge = Report.objects.create(reporter=reporter, target_type="user",
                                   target_id=reporter.pk + 1, reason="other",
                                   resolution_note="")

    _console(operator).post("/dj-admin/moderation/report/",
                            {"action": "dismiss_reports", "_selected_action": [grudge.pk],
                             "index": "0", "select_across": "0"}, follow=True)
    grudge.refresh_from_db()
    assert (grudge.status, grudge.handled_by_id) == ("dismissed", operator.pk)


def test_a_non_admin_cannot_reach_the_queue(make_user):
    from moderation.models import Report

    client = make_user(f"nope_{uuid.uuid4().hex[:6]}")
    # A staff account that is NOT an operator: `admin_view` lets it in, the permissions
    # on ReportAdmin do not. Complaints quote what people wrote about each other.
    staff = make_user(f"staff_{uuid.uuid4().hex[:6]}")
    staff.is_staff = True
    staff.save()
    reporter = make_user(f"rpt3_{uuid.uuid4().hex[:6]}")
    report = Report.objects.create(reporter=reporter, target_type="user",
                                   target_id=reporter.pk + 1, reason="spam")

    assert _console(client).get("/dj-admin/moderation/report/").status_code in (302, 403)
    assert _console(staff).get("/dj-admin/moderation/report/").status_code == 403

    # And the action is refused, not merely hidden: the row must not move.
    _console(staff).post("/dj-admin/moderation/report/",
                         {"action": "dismiss_reports", "_selected_action": [report.pk],
                          "index": "0", "select_across": "0"})
    report.refresh_from_db()
    assert report.status == "open"


# ------------------------------------------------------- the privacy registry
def test_erasing_a_reporter_keeps_the_complaint_and_drops_their_words(make_user):
    """All three models are in the personal-data registry (`privacy/sources.py`).

    `tests/security/privacy_rights.py` cannot prove this on a branch — it pins
    `sys.path` to the main checkout — so the export and the erasure are exercised
    here, where the code under test is the code that runs.
    """
    from training_platform import privacy

    from moderation.models import AIConsent, Block, Report

    reporter = make_user(f"gdpr_{uuid.uuid4().hex[:6]}")
    other = make_user(f"gdpr2_{uuid.uuid4().hex[:6]}")
    report = Report.objects.create(reporter=reporter, target_type="user",
                                   target_id=other.pk, reason="harassment",
                                   note="he said something vile to me")
    _block(reporter, other)
    _consent(reporter)

    exported = privacy.export_user_data(reporter)["sections"]
    assert len(exported["reports_made"]) == 1
    assert len(exported["blocks"]) == 1 and len(exported["ai_consent"]) == 1

    privacy.erase_user_data(reporter)

    # The complaint and its outcome survive — that is what the platform shows a store
    # when it is asked whether reports are acted on — but the reporter's words go.
    report.refresh_from_db()
    assert (report.note, report.reason, report.status) == ("", "harassment", "open")
    # The private list and the permission die with the account.
    assert not Block.objects.filter(blocker=reporter).exists()
    assert not AIConsent.objects.filter(user=reporter).exists()


def test_every_route_here_needs_a_signed_in_caller(client):
    """Anonymous gets 401, never a 500 and never an answer."""
    for method, path in (("get", "/api/v1/reports/mine/"), ("get", "/api/v1/blocks/"),
                         ("get", "/api/v1/ai-consent/")):
        assert getattr(client, method)(path).status_code == 401, path
    assert client.post("/api/v1/reports/", {}, content_type="application/json"
                       ).status_code == 401
