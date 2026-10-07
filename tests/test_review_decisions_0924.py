"""What the adversarial review of the owner's decisions of 2026-09-24 found (D1..D9).

Two reviewers read the merged build (b7f8b40) — the anonymous AI training copies, and
the suspended-account visibility with the refusal-code sweep — and one verifier
reproduced each finding; every test here failed there for the reason its name gives.
D5 and D10 were refuted.
"""
import json
import uuid
from datetime import timedelta

import pytest
from django.utils import timezone

from tests.test_owner_0924_ai_privacy import _person, _turn

pytestmark = pytest.mark.django_db

JSON = "application/json"


# ------------------------------------------------------------------------------ D1
def test_d1_trainer_name_from_the_prompt_is_scrubbed_from_the_copy(make_user):
    from ai_assistant.services.context_compiler import ContextCompiler

    trainer = make_user(f"d1_tr_{uuid.uuid4().hex[:6]}", user_type="trainer",
                        first_name="خالد", last_name="العلي")
    client = _person(make_user, "d1c", assigned_trainer=trainer)
    prompt = ContextCompiler()._user_context(client)
    assert "خالد العلي" in prompt            # the coach is told the trainer's name

    _reply, copy = _turn(client, user_message="كابتن خالد قال لي ارتاح",
                         ai_response="اسأل مدربك خالد العلي عن ركبتك")
    assert "خالد" not in copy.user_message, copy.user_message
    assert "خالد" not in copy.ai_response, copy.ai_response


# ------------------------------------------------------------------------------ D2
def test_d2_hamza_and_ta_marbuta_spellings_of_the_name_are_scrubbed(make_user):
    client = _person(make_user, "d2c", first_name="أسامة", last_name="حمزة")
    _reply, copy = _turn(client, user_message="انا اسامه حمزه", ai_response="اهلا")
    assert "اسامه" not in copy.user_message, copy.user_message
    assert "حمزه" not in copy.user_message, copy.user_message


# ------------------------------------------------------------------------------ D3
def test_d3_a_truncated_tool_result_loses_the_name_and_the_ids(make_user):
    client = _person(make_user, "d3c", first_name="أحمد", last_name="Saleh")
    full = {"routine": {"id": 991, "name": "برنامج أحمد للتنشيف",
                        "trainer": "Khaled Ali",
                        "exercises": [{"exercise_id": i, "name": f"ex {i}"}
                                      for i in range(60)]}}
    # The shape `MemoryService.truncate_tool_result` returns for a result over 500
    # tokens: the json.dumps text (ASCII-escaped), cut, under `data`.
    truncated = {"data": json.dumps(full, default=str)[:1500], "_truncated": True}
    _reply, copy = _turn(client, tools_called=["get_routine_schedule"],
                         tool_results=[truncated])
    # The truncated text is dropped from the copy altogether (stricter than scrubbing
    # it: cut JSON cannot be walked); whatever is kept must name nobody.
    kept = copy.tool_results[0].get("data", "")
    decoded = kept.encode("ascii", "ignore").decode("unicode_escape")
    assert "أحمد" not in decoded, decoded[:200]
    assert "Khaled" not in kept, kept[:200]
    assert '"id": 991' not in kept, kept[:200]


# ------------------------------------------------------------------------------ D4
@pytest.mark.parametrize("text", [
    "واتسابي 963 933 123 456",
    "رقمي 933 123 456",
    "رقمي 0933 - 123 - 456",
    "رقمي 0933/123/456",
    "t.me/ahmadfit",
])
def test_d4_common_phone_and_link_formats_are_removed(text):
    from ai_assistant.anonymise import scrub_text

    assert scrub_text(text, ["Rania"]) != text


# ------------------------------------------------------------------------------ D6
def test_d6_a_suspended_actor_leaves_other_peoples_notification_list(
        make_user, api, django_capture_on_commit_callbacks):
    from notifications.models import Notification

    a = make_user(f"d6_a_{uuid.uuid4().hex[:6]}")
    b = make_user(f"d6_b_{uuid.uuid4().hex[:6]}")
    Notification.objects.create(
        recipient=b, actor=a, event_type="comment_created",
        related_object_id="1", deduplication_key=uuid.uuid4().hex,
        metadata={"context": {"actor": a.username, "preview": "you look weak lol"},
                  "data": {"type": "comment_created", "post_id": 1, "comment_id": 1}})
    with django_capture_on_commit_callbacks(execute=True):
        a.is_active = False
        a.save()

    resp = api(b).get("/api/social/notifications/")
    assert resp.status_code == 200, resp.content
    body = json.dumps(resp.json(), ensure_ascii=False)
    assert a.username not in body, body[:600]


# ------------------------------------------------------------------------------ D7
def test_d7_a_target_raised_while_suspended_is_applied_on_reinstatement(
        make_user, django_capture_on_commit_callbacks):
    from social.challenge_progress import reapply_target
    from social.models import Challenge, ChallengeParticipation
    from social.standing import write_places

    owner = make_user(f"d7_o_{uuid.uuid4().hex[:6]}", user_type="trainer")
    p = make_user(f"d7_p_{uuid.uuid4().hex[:6]}")
    q = make_user(f"d7_q_{uuid.uuid4().hex[:6]}")
    now = timezone.now()
    ch = Challenge.objects.create(
        title="d7", description="d7", challenge_type="workout", creator=owner,
        start_date=now - timedelta(days=2), end_date=now + timedelta(days=5),
        status="active", target_value=20.0, unit="kg")
    rp = ChallengeParticipation.objects.create(user=p, challenge=ch, current_value=22.0,
                                               status="completed",
                                               progress_percentage=100.0,
                                               completed_at=now)
    ChallengeParticipation.objects.create(user=q, challenge=ch, current_value=10.0,
                                          status="joined")
    write_places(ch)

    with django_capture_on_commit_callbacks(execute=True):
        p.is_active = False
        p.save()
    ch.target_value = 30.0
    ch.save()
    reapply_target(ch)                      # what perform_update runs on a target edit
    with django_capture_on_commit_callbacks(execute=True):
        p.is_active = True
        p.save()

    rp.refresh_from_db()
    assert rp.status != "completed", (rp.status, rp.current_value, rp.rank)


# ------------------------------------------------------------------------------ D8
def test_d8_switching_a_goal_to_weekly_does_not_keep_a_typed_target(make_user, api):
    from analytics.models import UserGoal

    nothing = make_user(f"d8_{uuid.uuid4().hex[:6]}")
    goal = UserGoal.objects.create(user=nothing, goal_type="custom", title="mine",
                                   target_value=70)
    resp = api(nothing).patch(f"/api/analytics/goals/{goal.pk}/", data=json.dumps(
        {"goal_type": "workout_frequency"}), content_type=JSON)
    # Correct: refused like a create (no calculated target), not stored as weekly 70.
    assert resp.status_code == 400, (resp.status_code, resp.content)


def test_d8_echoing_the_served_saved_target_on_patch(make_user, api):
    from analytics.models import UserGoal

    nothing = make_user(f"d8e_{uuid.uuid4().hex[:6]}")
    goal = UserGoal.objects.create(user=nothing, goal_type="workout_frequency",
                                   title="old", target_value=4)
    served = api(nothing).get(f"/api/analytics/goals/{goal.pk}/").json()
    assert served["target_value"] == 4.0
    resp = api(nothing).patch(f"/api/analytics/goals/{goal.pk}/", data=json.dumps(
        {"title": "x", "target_value": served["target_value"]}), content_type=JSON)
    assert resp.status_code == 200, (resp.status_code, resp.content)


# ------------------------------------------------------------------------------ D9
def test_d9_public_templates_do_not_name_a_suspended_trainer(
        make_user, api, django_capture_on_commit_callbacks):
    from routine.models import RoutineTemplate

    tag = uuid.uuid4().hex[:6]
    trainer = make_user(f"d9_tr_{tag}", user_type="trainer")
    viewer = make_user(f"d9_c_{tag}")
    # A code: the model takes only the closed goal list (wave-1 review R-5, 2026-10-07).
    RoutineTemplate.objects.create(name=f"d9 plan {tag}", goal="strength", days=3,
                                   created_by=trainer, is_public=True)
    with django_capture_on_commit_callbacks(execute=True):
        trainer.is_active = False
        trainer.save()
    resp = api(viewer).get("/api/routine/templates/?page_size=100")
    assert resp.status_code == 200, resp.content
    body = json.dumps(resp.json(), ensure_ascii=False)
    assert trainer.username not in body, body[:600]
