"""Cycle 9 minor findings, fixed in files the five cycle-9 fix branches do not touch.

Kept out of test_regression_gate.py so those branches merge without conflicts.
"""
import types
import uuid
from datetime import timedelta

import pytest
from django.utils import timezone

pytestmark = pytest.mark.django_db


# --- Push delivery -------------------------------------------------------------------

def _sdk_errors():
    from firebase_admin import exceptions, messaging
    return {
        "iam_403": exceptions.PermissionDeniedError("caller lacks messaging permission"),
        "payload_400": exceptions.InvalidArgumentError("invalid data payload"),
        "project_404": exceptions.NotFoundError("requested entity was not found"),
        "unavailable": exceptions.UnavailableError("try again"),
        "unregistered": messaging.UnregisteredError("token gone"),
        "sender_mismatch": messaging.SenderIdMismatchError("wrong sender"),
    }


def test_only_token_specific_fcm_errors_deactivate_a_token():
    """Generic 403/400/404 from the SDK said nothing about the token, but their codes
    were in the dead list: an IAM mistake at launch deactivated every device token of
    every recipient on the first push."""
    from notifications.channels.fcm import FCMChannel

    errors = _sdk_errors()
    for name in ("iam_403", "payload_400", "project_404", "unavailable"):
        assert FCMChannel._token_is_dead(errors[name]) is False, name
    for name in ("unregistered", "sender_mismatch"):
        assert FCMChannel._token_is_dead(errors[name]) is True, name


def _push_setup(make_user, tokens, event_type="custom"):
    from notifications.models import Notification
    from users.models import DeviceToken

    user = make_user(f"push_{uuid.uuid4().hex[:8]}")
    for token in tokens:
        DeviceToken.objects.create(user=user, token=token, is_active=True)
    notification = Notification.objects.create(
        recipient=user, event_type=event_type, related_object_id="x",
        metadata={"context": {"message": "hello"}, "data": {}},
        deduplication_key=uuid.uuid4().hex)
    return user, notification


def _fake_transport(monkeypatch, outcome_for_token):
    """outcome_for_token(token) -> None for success, or the exception to report."""
    from firebase_admin import messaging

    calls = []

    def send_each_for_multicast(message, dry_run=False):
        calls.append(list(message.tokens))
        responses = []
        for token in message.tokens:
            exc = outcome_for_token(token)
            responses.append(types.SimpleNamespace(success=exc is None, exception=exc))
        return types.SimpleNamespace(
            responses=responses,
            success_count=sum(r.success for r in responses),
            failure_count=sum(not r.success for r in responses))

    monkeypatch.setattr(messaging, "send_each_for_multicast", send_each_for_multicast)
    return calls


def test_an_iam_denial_leaves_every_device_token_active(make_user, monkeypatch):
    from notifications.channels.fcm import FCMChannel
    from users.models import DeviceToken

    user, notification = _push_setup(make_user, ["t-a", "t-b"])
    errors = _sdk_errors()
    _fake_transport(monkeypatch, lambda token: errors["iam_403"])

    FCMChannel.send(notification)

    assert DeviceToken.objects.filter(user=user, is_active=True).count() == 2
    notification.refresh_from_db()
    assert notification.status["fcm"]["status"] == "failed"


def test_a_push_with_no_device_left_settles_and_leaves_the_drain(make_user, monkeypatch):
    """The drain re-selected a 'failed' push whose tokens had all been deactivated: send
    returned early without touching the status, so the row came back every hour for
    ever and crowded real outage failures out of the 100-row window."""
    from notifications.channels.fcm import FCMChannel
    from notifications.tasks import drain_dead_letter_queue

    user, notification = _push_setup(make_user, ["t-dead"])
    errors = _sdk_errors()
    calls = _fake_transport(monkeypatch, lambda token: errors["unregistered"])

    FCMChannel.send(notification)
    notification.refresh_from_db()
    assert notification.status["fcm"]["status"] == "undeliverable"

    # A row left 'failed' by an earlier crash, whose device has since gone.
    notification.status["fcm"]["status"] = "failed"
    notification.save(update_fields=["status"])
    drain_dead_letter_queue()
    notification.refresh_from_db()
    assert notification.status["fcm"]["status"] == "undeliverable"

    calls.clear()
    drain_dead_letter_queue()
    assert calls == []


def test_a_transient_failure_on_one_of_two_devices_is_retried_for_that_device(
        make_user, monkeypatch):
    """'sent' was recorded on the first success, so the drain never saw a second device
    that failed with UNAVAILABLE."""
    from notifications.channels.fcm import FCMChannel
    from notifications.tasks import drain_dead_letter_queue

    user, notification = _push_setup(make_user, ["t-ok", "t-flaky"])
    errors = _sdk_errors()
    flaky = {"down": True}
    calls = _fake_transport(
        monkeypatch,
        lambda token: errors["unavailable"] if token == "t-flaky" and flaky["down"] else None)

    FCMChannel.send(notification)
    notification.refresh_from_db()
    assert notification.status["fcm"]["status"] == "partial"

    flaky["down"] = False
    calls.clear()
    drain_dead_letter_queue()
    assert calls == [["t-flaky"]]
    notification.refresh_from_db()
    assert notification.status["fcm"]["status"] == "sent"
    assert sorted(notification.status["fcm"]["sent_tokens"]) == ["t-flaky", "t-ok"]


def test_the_drain_gives_up_after_the_attempt_cap_and_the_retry_window(make_user, monkeypatch):
    from notifications.models import Notification
    from notifications.tasks import PUSH_MAX_ATTEMPTS, PUSH_RETRY_WINDOW, drain_dead_letter_queue

    errors = _sdk_errors()
    calls = _fake_transport(monkeypatch, lambda token: errors["unavailable"])

    _, exhausted = _push_setup(make_user, ["t-1"])
    exhausted.status = {"fcm": {"status": "failed", "attempts": PUSH_MAX_ATTEMPTS}}
    exhausted.save(update_fields=["status"])

    _, stale = _push_setup(make_user, ["t-2"])
    stale.status = {"fcm": {"status": "failed", "attempts": 1}}
    stale.save(update_fields=["status"])
    Notification.objects.filter(pk=stale.pk).update(
        created_at=timezone.now() - PUSH_RETRY_WINDOW - timedelta(minutes=1))

    _, fresh = _push_setup(make_user, ["t-3"])
    fresh.status = {"fcm": {"status": "failed"}}
    fresh.save(update_fields=["status"])

    drain_dead_letter_queue()
    assert calls == [["t-3"]]
    fresh.refresh_from_db()
    assert fresh.status["fcm"]["attempts"] == 1


def test_a_crash_before_the_send_keeps_the_delivery_history(make_user, monkeypatch):
    """The failure record replaced the whole fcm block, dropping sent_tokens (a retry
    pushed again to devices that already had it) and attempts (the cap never moved)."""
    from notifications.channels.fcm import FCMChannel
    from notifications.services import NotificationService

    user, notification = _push_setup(make_user, ["t-a"])
    notification.status = {"fcm": {"status": "partial", "sent_tokens": ["t-old"], "attempts": 2}}
    notification.save(update_fields=["status"])

    def boom(n):
        raise RuntimeError("renderer exploded")

    monkeypatch.setattr(FCMChannel, "send", classmethod(lambda cls, n: boom(n)))
    NotificationService._dispatch_channels(notification)

    notification.refresh_from_db()
    fcm = notification.status["fcm"]
    assert fcm["status"] == "partial"
    assert fcm["sent_tokens"] == ["t-old"]
    assert fcm["attempts"] == 3


# --- Routine notifications -----------------------------------------------------------

def test_reassigning_the_same_routine_notifies_the_client_again(
        make_user, django_capture_on_commit_callbacks):
    """send_async_notification passed no event_id, so the dedup key was the routine id
    under a permanent unique constraint: the second assignment sent nothing."""
    from notifications.models import Notification
    from routine.services import send_notification

    client = make_user(f"rn_{uuid.uuid4().hex[:8]}")
    routine = types.SimpleNamespace(id=424242)
    for _ in range(2):
        with django_capture_on_commit_callbacks(execute=True):
            send_notification(client, "routine_assignment", "Push/Pull", related_object=routine)

    assert Notification.objects.filter(recipient=client, event_type="routine_assignment").count() == 2


def test_a_completed_session_is_still_announced_once(make_user, django_capture_on_commit_callbacks):
    from notifications.models import Notification
    from routine.services import send_notification

    client = make_user(f"rs_{uuid.uuid4().hex[:8]}")
    session = types.SimpleNamespace(id=515151)
    for _ in range(2):
        with django_capture_on_commit_callbacks(execute=True):
            send_notification(client, "session_completed", "well done", related_object=session)

    assert Notification.objects.filter(recipient=client, event_type="session_completed").count() == 1


def test_a_rolled_back_assignment_sends_nothing(make_user, django_capture_on_commit_callbacks):
    from django.db import transaction

    from notifications.models import Notification
    from routine.services import send_notification

    client = make_user(f"rb_{uuid.uuid4().hex[:8]}")
    with django_capture_on_commit_callbacks(execute=True) as callbacks:
        try:
            with transaction.atomic():
                send_notification(client, "routine_assignment", "never", related_object=None)
                raise RuntimeError("assignment failed")
        except RuntimeError:
            pass
    assert callbacks == []
    assert not Notification.objects.filter(recipient=client).exists()


def test_a_database_error_in_the_routine_task_is_not_reported_as_success(make_user, monkeypatch):
    from django.db import OperationalError

    from notifications.services import NotificationService
    from routine.tasks import send_async_notification

    client = make_user(f"re_{uuid.uuid4().hex[:8]}")

    def fail(*args, **kwargs):
        raise OperationalError("connection reset")

    monkeypatch.setattr(NotificationService, "create_and_send", fail)
    # Eager mode surfaces autoretry as a raised Retry; the old catch-all returned None.
    from celery.exceptions import Retry
    with pytest.raises((Retry, OperationalError)):
        send_async_notification.apply(args=(client.id, "routine_assignment", "x"), throw=True)


def test_event_processing_that_overruns_is_not_retried(monkeypatch):
    from celery.exceptions import Ignore, Retry, SoftTimeLimitExceeded

    from notifications.domain.dispatcher import EventDispatcher
    from notifications.tasks import process_event_task

    def overrun(event):
        raise SoftTimeLimitExceeded()

    monkeypatch.setattr(EventDispatcher, "dispatch", staticmethod(overrun))
    event_path = "notifications.domain.events.PostLikedEvent"
    with pytest.raises(Ignore):
        process_event_task.run(event_path, {})


# --- AI feedback ---------------------------------------------------------------------

def _assistant_turn(user):
    from ai_assistant.models import ChatMessage, ChatSession

    session = ChatSession.objects.create(user=user)
    return ChatMessage.objects.create(session=session, role="assistant", content="Eat more protein.")


def _save_feedback(user, message_id, feedback):
    """The consumer's DB helper, run synchronously: going through `async_to_sync` makes
    `database_sync_to_async` close the connection holding the test's transaction."""
    from ai_assistant.consumers import AIChatConsumer

    consumer = AIChatConsumer()
    consumer.user = user
    return AIChatConsumer.__dict__["_save_feedback"].func(consumer, message_id, feedback)


def test_feedback_is_saved_for_a_user_without_training_consent(make_user):
    """Feedback lived only on AITrainingData, which is written only for users who opted
    in (off by default), so every default user was told "Could not save feedback."."""
    user = make_user(f"fb_{uuid.uuid4().hex[:8]}", ai_training_consent=False)
    reply = _assistant_turn(user)

    assert _save_feedback(user, reply.id, "positive") is True
    reply.refresh_from_db()
    assert reply.feedback == "positive"


def test_feedback_given_before_the_training_row_is_written_reaches_it(make_user):
    from ai_assistant.models import AITrainingData
    from ai_assistant.services.data_collector import DataCollector

    user = make_user(f"fbc_{uuid.uuid4().hex[:8]}", ai_training_consent=True)
    reply = _assistant_turn(user)
    assert _save_feedback(user, reply.id, "negative") is True

    DataCollector().log_interaction(
        user=user, session=reply.session, user_message="q", ai_response=reply.content,
        tools_called=[], tool_results=[], message_id=reply.id)
    assert AITrainingData.objects.get(message=reply).user_feedback == "negative"


def test_feedback_on_someone_elses_reply_is_refused(make_user):
    owner = make_user(f"fbo_{uuid.uuid4().hex[:8]}")
    stranger = make_user(f"fbs_{uuid.uuid4().hex[:8]}")
    reply = _assistant_turn(owner)
    assert _save_feedback(stranger, reply.id, "negative") is False
    reply.refresh_from_db()
    assert reply.feedback is None


# --- Catalogue wipe ------------------------------------------------------------------

def test_a_wipe_counts_every_table_that_references_a_food():
    """The guard was a hand-written list and missed the five macro-choice M2Ms, so a
    reseed erased every client's chosen proteins, carbs, fats, vegetables and fruits."""
    from diet.management.commands.load_food_catalogue import _client_data_referencing_foods
    from diet.models import FoodItem

    labels = set(_client_data_referencing_foods())
    for choice in ("protein", "carb", "fat", "vegetable", "fruit"):
        assert f"chosen {choice}(s)" in labels
    referencing = {
        rel.related_model for rel in FoodItem._meta.get_fields(include_hidden=True)
        if rel.is_relation and rel.auto_created and not rel.concrete and not rel.many_to_many
        and rel.related_model.__name__ != "RecipeIngredient"}
    assert len(labels) == len(referencing)


# --- Social --------------------------------------------------------------------------

def test_a_comment_cannot_be_moved_into_another_post(make_user, api):
    """`post` was writable on update and only perform_create checked visibility, so the
    author of any comment could PATCH it into someone's private post."""
    import json as _json

    from social.models import Comment, Post

    alice = make_user(f"ca_{uuid.uuid4().hex[:8]}")
    bob = make_user(f"cb_{uuid.uuid4().hex[:8]}")
    public = Post.objects.create(author=alice, content="hello", visibility="public")
    private = Post.objects.create(author=alice, content="diary", visibility="private")
    comment = Comment.objects.create(post=public, author=bob, content="hi")

    response = api(bob).patch(
        f"/api/social/comments/{comment.id}/", _json.dumps({"post": private.id}),
        content_type="application/json")
    assert response.status_code == 400, response.content
    comment.refresh_from_db()
    assert comment.post_id == public.id

    edited = api(bob).patch(
        f"/api/social/comments/{comment.id}/", _json.dumps({"content": "edited"}),
        content_type="application/json")
    assert edited.status_code == 200, edited.content


def test_a_reply_must_be_in_its_parents_thread(make_user, api):
    import json as _json

    from social.models import Comment, Post

    alice = make_user(f"ra_{uuid.uuid4().hex[:8]}")
    stranger = make_user(f"rs_{uuid.uuid4().hex[:8]}")
    public = Post.objects.create(author=alice, content="open", visibility="public")
    hidden = Post.objects.create(author=alice, content="secret", visibility="private")
    hidden_comment = Comment.objects.create(post=hidden, author=alice, content="note")

    response = api(stranger).post(
        "/api/social/comments/",
        _json.dumps({"post": public.id, "content": "reply", "parent_comment": hidden_comment.id}),
        content_type="application/json")
    assert response.status_code == 400, response.content


def test_a_challenge_runs_between_its_dates_without_anyone_activating_it(make_user):
    """`status` defaulted to 'upcoming', nothing ever moved it, and `is_active` required
    'active', so an API-created challenge could never be joined."""
    from social.models import Challenge
    from social.serializers import ChallengeSerializer

    creator = make_user(f"ch_{uuid.uuid4().hex[:8]}", user_type="trainer")
    now = timezone.now()

    def make(start, end, status="upcoming"):
        return Challenge.objects.create(
            title="Steps", description="walk", challenge_type="habit", creator=creator,
            start_date=start, end_date=end, status=status)

    running = make(now - timedelta(hours=1), now + timedelta(days=7))
    future = make(now + timedelta(days=1), now + timedelta(days=8))
    over = make(now - timedelta(days=8), now - timedelta(days=1), status="active")
    cancelled = make(now - timedelta(hours=1), now + timedelta(days=7), status="cancelled")

    assert running.is_active and running.lifecycle_status == "active"
    assert not future.is_active and future.lifecycle_status == "upcoming"
    assert not over.is_active and over.lifecycle_status == "completed"
    assert not cancelled.is_active and cancelled.lifecycle_status == "cancelled"
    assert ChallengeSerializer(running).data["status"] == "active"

    ids = set(Challenge.objects.filter(Challenge.running_q()).values_list("id", flat=True))
    assert running.id in ids and not ids & {future.id, over.id, cancelled.id}


class _FakeRedis:
    def __init__(self):
        self.zsets, self.sets = {}, {}

    def pipeline(self):
        return self

    def execute(self):
        return []

    def zadd(self, key, mapping):
        self.zsets.setdefault(key, {}).update(mapping)

    def zremrangebyrank(self, key, start, stop):
        return 0

    def expire(self, key, seconds):
        return True

    def sadd(self, key, *members):
        self.sets.setdefault(key, set()).update(members)

    def smembers(self, key):
        return {m.encode() for m in self.sets.get(key, set())}

    def zrem(self, key, member):
        self.zsets.get(key, {}).pop(member, None)

    def delete(self, key):
        self.sets.pop(key, None)


def test_deleting_a_post_evicts_it_from_every_feed_it_reached(make_user, monkeypatch):
    """Eviction used the author's CURRENT followers, but the fan-out also wrote the
    author's own feed and anyone who has unfollowed since still held the id."""
    from social import feed_cache, tasks

    fake = _FakeRedis()
    monkeypatch.setattr(feed_cache, "get_redis_client", lambda: fake)
    monkeypatch.setattr(tasks, "get_redis_client", lambda: fake)

    author_id, ex_follower_id, follower_id, post_id = 11, 22, 33, 9001
    tasks.fan_out_batch([author_id, ex_follower_id, follower_id], post_id, 1.0)
    feed_cache.remove_post_from_feeds(post_id, follower_ids=[follower_id])

    for uid in (author_id, ex_follower_id, follower_id):
        assert str(post_id) not in fake.zsets[f"feed:v1:{uid}"], uid
    assert feed_cache.feed_carriers_key(post_id) not in fake.sets


# --- Subscription --------------------------------------------------------------------

def test_a_client_without_a_subscription_is_told_to_subscribe(make_user, api):
    """The paywall answered DRF's generic permission_denied, so the app could not tell
    'renew your subscription' from an authorization error."""
    client = make_user(f"ns_{uuid.uuid4().hex[:8]}")
    response = api(client).get("/api/diet/v1/my/diet-plans/")
    assert response.status_code == 403
    assert response.json()["code"] == "no_entitlement", response.content


def test_a_plan_without_the_feature_says_so(make_user, api):
    from subscription.models import Subscription, SubscriptionPlan

    plan = SubscriptionPlan.objects.create(
        name=f"no diet {uuid.uuid4().hex[:6]}", plan_type="basic", description="x",
        price="1000.00", currency="SYP", duration_days=30, is_active=True,
        has_diet_access=False)
    client = make_user(f"nd_{uuid.uuid4().hex[:8]}")
    Subscription.objects.create(user=client, plan=plan, status="active")

    response = api(client).get("/api/diet/v1/my/diet-plans/")
    assert response.status_code == 403
    assert response.json()["code"] == "feature_not_in_plan", response.content


def test_subscribing_returns_the_subscription_it_created(make_user, api):
    import json as _json

    from subscription.models import SubscriptionPlan

    plan = SubscriptionPlan.objects.create(
        name=f"sub {uuid.uuid4().hex[:6]}", plan_type="premium", description="x",
        price="5000.00", currency="SYP", duration_days=30, is_active=True)
    client = make_user(f"sb_{uuid.uuid4().hex[:8]}")
    response = api(client).post(
        "/api/subscription/v1/subscriptions/", _json.dumps({"plan_id": plan.id}),
        content_type="application/json")
    assert response.status_code == 201, response.content
    body = response.json()
    assert body["id"] == str(client.subscription.id)
    assert body["status"] == client.subscription.status


# --- Diet learning (cycle 10 dive) ---------------------------------------------------

def _two_foods():
    from diet.models import FoodCategory, FoodItem

    cat = FoodCategory.objects.create(name=f"learn-{uuid.uuid4().hex[:6]}")
    return [FoodItem.objects.create(
        name=name, name_en=name, category=cat, api_id=f"learn-{uuid.uuid4().hex[:8]}",
        calories=kcal, protein=p, carbs=c, fat=f, serving_size="100g",
        allergens=[], allergen_source="verified")
        for name, kcal, p, c, f in (("Broccoli", 34, 2.8, 7, 0.4),
                                    ("Lentils (Cooked)", 116, 9, 20, 0.4))]


def test_learning_counts_only_meals_the_client_was_offered(make_user):
    """Every MealComponent counted as "served": tomorrow's dinner, and every meal of a
    plan replaced on day two. For a client who uses the completion screen those read as
    served-and-not-finished, so the foods of their CURRENT plan were pushed down nightly
    and each regeneration added a month of phantom refusals."""
    from diet.models import DietPlan, Meal, MealComponent
    from diet.planner.learning import observations_for, update_weights
    from training_platform.user_time import today_for

    user = make_user(f"learn_{uuid.uuid4().hex[:8]}")
    today = today_for(user)
    eaten, planned_only = _two_foods()

    old = DietPlan.objects.create(user=user, goal="Maintain", daily_calories=2000,
                                  start_date=today - timedelta(days=10),
                                  end_date=today + timedelta(days=19), is_active=False)
    new = DietPlan.objects.create(user=user, goal="Maintain", daily_calories=2000,
                                  start_date=today - timedelta(days=3),
                                  end_date=today + timedelta(days=26))
    DietPlan.objects.filter(pk=old.pk).update(created_at=timezone.now() - timedelta(days=10))

    meals = {}

    def component(plan, day, food, **kw):
        meal = meals.get((plan.pk, day))
        if meal is None:
            meal = meals[(plan.pk, day)] = Meal.objects.create(
                diet_plan=plan, date=day, meal_type="Lunch", scheduled_time="12:00")
        return MealComponent.objects.create(meal=meal, food=food, quantity=100.0, **kw)

    for offset in range(-10, 20):          # the replaced plan, its whole month
        component(old, today + timedelta(days=offset), eaten,
                  is_completed=offset == -10)
        component(old, today + timedelta(days=offset), planned_only)
    for offset in range(-3, 27):           # the current plan, mostly still ahead
        component(new, today + timedelta(days=offset), eaten)
        if offset >= 0:
            component(new, today + timedelta(days=offset), planned_only)

    stats = observations_for(user)
    # Offered: old plan days -10..-4 (7) and new plan days -3..-1 (3).
    assert stats[eaten.id]["served"] == 10
    assert stats[planned_only.id]["served"] == 7   # only the old plan's offered week

    before = stats[planned_only.id]
    changes = update_weights(user)
    # 7 offered, none finished: that is real evidence. The 30 future and superseded
    # servings of `planned_only` must not have deepened it.
    assert before["served"] == 7
    assert changes.get(eaten.id, 1.0) >= changes.get(planned_only.id, 1.0)


def test_diet_review_actions_are_logged_with_who_did_them(make_user, client):
    from django.contrib.admin.models import LogEntry
    from django.contrib.contenttypes.models import ContentType

    from diet.models import Recipe

    admin_user = make_user(f"dadm_{uuid.uuid4().hex[:8]}", user_type="admin")
    recipe = Recipe.objects.create(name=f"Review me {uuid.uuid4().hex[:6]}", reviewed=False)
    client.force_login(admin_user)
    response = client.post("/admin/diet/recipe/", {
        "action": "mark_reviewed", "_selected_action": [recipe.pk]})
    assert response.status_code in (200, 302), response.status_code

    recipe.refresh_from_db()
    assert recipe.reviewed is True and recipe.reviewed_at is not None
    assert LogEntry.objects.filter(
        user=admin_user, object_id=str(recipe.pk),
        content_type=ContentType.objects.get_for_model(Recipe)).exists()


def test_daily_advice_names_the_plans_target(make_user):
    """The advice quoted the profile formula while the plan screen beside it showed the
    plan's own target — a trainer's plan, or one built at an earlier weight."""
    from diet.models import DailyAdvice, DietPlan
    from diet.tasks import generate_daily_advice

    client = make_user(f"adv_{uuid.uuid4().hex[:8]}", preferred_language="en",
                       weight=80, height=180, age=30, gender="male")
    today = timezone.localdate()
    DietPlan.objects.create(user=client, goal="Maintain", daily_calories=1737,
                            start_date=today, end_date=today + timedelta(days=6))
    assert round(float(client.calculate_daily_calories())) != 1737

    generate_daily_advice(user_id=client.id)
    advice = DailyAdvice.objects.get(user=client)
    assert "1737" in advice.text, advice.text


def test_a_database_outage_during_the_advice_sweep_reaches_the_retry(make_user, monkeypatch):
    from django.db import OperationalError

    from diet import tasks as diet_tasks

    client = make_user(f"advo_{uuid.uuid4().hex[:8]}")

    def outage(user):
        raise OperationalError("server closed the connection")

    monkeypatch.setattr(diet_tasks, "_generate_personalized_advice", outage)
    with pytest.raises(OperationalError):
        diet_tasks.generate_daily_advice.run(user_id=client.id)


@pytest.mark.parametrize("value", ["NaN", "nan", "Infinity", "-inf", float("nan"), True])
def test_diet_validators_refuse_what_is_not_a_real_number(value):
    """`float("nan")` passes `< MIN` and `> MAX` alike, so "nan" was a valid daily calorie
    target: a trainer's plan was stored with NaN and every screen that rendered it
    answered 500 for good, because JSON has no NaN."""
    from django.core.exceptions import ValidationError

    from diet.validators import DietInputValidator as V

    for check in (lambda: V.validate_daily_calories(value),
                  lambda: V.validate_quantity(value),
                  lambda: V.validate_macro_targets(value, 100, 60),
                  lambda: V.validate_food_item({"name": "x", "calories": value, "protein": 1,
                                                "carbs": 1, "fat": 1,
                                                "serving_size_grams": 100})):
        with pytest.raises(ValidationError):
            check()


def test_the_database_refuses_a_nan_diet_number(make_user):
    """Postgres orders NaN above every number, so `calories >= 0` and `quantity > 0`
    both held for NaN; only an upper bound refuses it."""
    from django.db import IntegrityError, transaction

    from diet.models import DietPlan, FoodCategory, FoodItem

    user = make_user(f"nan_{uuid.uuid4().hex[:8]}")
    today = timezone.localdate()
    plan = DietPlan.objects.create(user=user, goal="Maintain", daily_calories=2000,
                                   start_date=today, end_date=today)
    cat = FoodCategory.objects.create(name=f"nan-{uuid.uuid4().hex[:6]}")
    food = FoodItem.objects.create(
        name="Nan Test", name_en="Nan Test", category=cat, api_id=f"nan-{uuid.uuid4().hex}",
        calories=100, protein=5, carbs=10, fat=2, serving_size="100g",
        allergens=[], allergen_source="verified")

    for queryset, field in ((DietPlan.objects.filter(pk=plan.pk), "daily_calories"),
                            (DietPlan.objects.filter(pk=plan.pk), "target_protein"),
                            (FoodItem.objects.filter(pk=food.pk), "calories"),
                            (FoodItem.objects.filter(pk=food.pk), "fat")):
        for bad in (float("nan"), float("inf")):
            with pytest.raises(IntegrityError), transaction.atomic():
                queryset.update(**{field: bad})
