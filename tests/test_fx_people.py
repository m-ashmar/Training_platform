"""The `users`, `diet` and `ai_assistant` rows of `SIDE_BUGS.md` fixed on `fx/people`,
one test per defect.

Each was watched failing against the code that was there before its fix:

    S-150  three `validate_profile_picture` methods that never ran
    S-152  every trainer's email address sent to any signed-in client
    S-153  the client-profile cache invalidated in the wrong Redis database
    S-154  a blocked pair's pending request still disclosing the client's data
    S-155  refusals on the request, approve and unassign paths with no `code`
    S-046  an unreachable, untranslated date branch in the progress views
    S-047  the three "only clients" 403s of the diet progress views with no `code`
    S-048  `DailyAdviceView`'s 500 body with no `code`
    S-097  the advice row dropped for a client whose profile is incomplete
    S-050  the AI allowance's `resets_at` with no offset
    S-089  the first AI message of the day not reserved atomically
    S-060  the AI coach reading next week's plan as "the plan"
    S-061  REST feedback stored nowhere for a user without training consent
    S-090  the AI tool lines in English for an Arabic reader
    S-091  AI adherence averaged over the days that happen to have a row
"""
import inspect
import json
import threading
import uuid
from datetime import datetime, time, timedelta
from types import SimpleNamespace

import pytest
from asgiref.sync import async_to_sync
from django.utils import timezone

pytestmark = pytest.mark.django_db

JSON = "application/json"
ARABIC = ("؀", "ۿ")


def _tag(prefix):
    return f"{prefix}_{uuid.uuid4().hex[:8]}"


def _has_arabic(text):
    return any(ARABIC[0] <= ch <= ARABIC[1] for ch in str(text))


def _trainer(make_user, prefix, **kw):
    kw.setdefault("email_verified", True)
    kw.setdefault("trainer_is_available", True)
    return make_user(_tag(prefix), user_type="trainer", **kw)


def _ai_subscriber(make_user, prefix, **kw):
    """A client the AI routes admit: a live plan with the AI coach, and consent."""
    from django.utils import translation

    from moderation.models import AIConsent
    from subscription.models import Subscription, SubscriptionPlan

    person = make_user(_tag(prefix), **kw)
    with translation.override("en"):
        plan = SubscriptionPlan.objects.create(
            name=f"fx ai {uuid.uuid4().hex[:6]}", plan_type="premium", description="ai",
            price=0, currency="SYP", duration_days=30, is_active=True, has_ai_advice=True)
    Subscription.objects.create(user=person, plan=plan, status="active")
    AIConsent.objects.create(user=person, granted_at=timezone.now(),
                             policy_version="2026-09-01")
    return person


def _food():
    from diet.models import FoodCategory, FoodItem

    category, _ = FoodCategory.objects.get_or_create(name="fx-people-cat")
    food, _ = FoodItem.objects.get_or_create(
        api_id="fx-people-oats",
        defaults=dict(name="FX Oats", calories=380.0, protein=13.0, carbs=67.0, fat=7.0,
                      serving_size="100 g", serving_size_grams=100, category=category))
    return food


def _plan(user, start, end, *, calories=2000.0, meal_days=()):
    """An active plan from `start` to `end`, with one meal on each of `meal_days`."""
    from diet.models import DietPlan, Meal, MealComponent

    plan = DietPlan.objects.create(user=user, goal="Maintain", daily_calories=calories,
                                   start_date=start, end_date=end)
    food = _food()
    for day in meal_days:
        meal = Meal.objects.create(diet_plan=plan, date=day, meal_type="Breakfast",
                                   name=f"fx meal {day}", scheduled_time=time(8, 0))
        MealComponent.objects.create(meal=meal, food=food, quantity=100.0)
    return plan


def _tracked(user, plan, day, *, consumed=2000.0, target=2000.0):
    from diet.models import DailyProgress

    return DailyProgress.objects.create(
        user=user, diet_plan=plan, date=day, target_calories=target,
        calories_consumed=consumed, meals_completed=1, total_meals=1)


# ============================================================== users (S-150..S-155)
def test_s150_no_profile_serializer_pretends_to_check_an_upload():
    """`profile_picture` is a read-only method field on all three, so a
    `validate_profile_picture` there never ran and read as a second, content-checked
    upload path that does not exist."""
    from rest_framework import serializers

    from users.serializers import (ClientProfileSerializer, CustomUserSerializer,
                                   TrainerProfileSerializer)

    for cls in (TrainerProfileSerializer, ClientProfileSerializer, CustomUserSerializer):
        assert not hasattr(cls, "validate_profile_picture"), cls.__name__
        # The reason it could never run, pinned: the field only reads the URL.
        field = cls().fields["profile_picture"]
        assert isinstance(field, serializers.SerializerMethodField), cls.__name__


def test_s152_no_client_endpoint_sends_a_trainers_email(make_user, api):
    """`client/available-trainers/` sent every trainer's address to any signed-in
    client, and `client/request-status/` sent it for every trainer ever asked,
    declined ones included."""
    from users.models import TrainerClientRelation

    trainer = _trainer(make_user, "s152t", first_name="Samer")
    client = make_user(_tag("s152c"))
    TrainerClientRelation.objects.create(trainer=trainer, client=client,
                                         status="rejected", initiated_by="client")

    listed = api(client).get(f"/api/auth/client/available-trainers/?search={trainer.username}")
    assert listed.status_code == 200, listed.content
    body = listed.json()
    rows = (body.get("results") or body)["available_trainers"]
    assert [row["id"] for row in rows] == [trainer.id]
    assert rows[0]["email"] is None, "the key stays for older builds, the address never"
    assert trainer.email not in listed.content.decode()

    history = api(client).get("/api/auth/client/request-status/")
    assert history.status_code == 200, history.content
    (row,) = history.json()["requests"]
    assert row["status"] == "rejected" and row["trainer_email"] is None
    assert trainer.email not in history.content.decode()


def test_s153_invalidation_reaches_the_cache_the_keys_live_in(make_user):
    """`ClientProfileViewSet` writes these keys through `private_cache()` (DB3) and the
    invalidation deleted them from the default cache (DB0), so nothing was invalidated
    and the trainer read the old profile and client list for five minutes."""
    from django.conf import settings

    from training_platform.cache import private_cache
    from training_platform.i18n import CACHE_VERSION
    from users.views import ClientProfileViewSet

    trainer, client = _trainer(make_user, "s153t"), make_user(_tag("s153c"))
    store = private_cache()
    keys = []
    for lang, _name in settings.LANGUAGES:
        keys += [f"client_profile:{client.id}:{lang}:{CACHE_VERSION}",
                 f"trainer:{trainer.id}:approved_clients:{lang}:{CACHE_VERSION}"]
    for key in keys:
        store.set(key, {"stale": True}, 300)

    ClientProfileViewSet.invalidate_client_cache(client.id, trainer.id)

    assert {key: store.get(key) for key in keys} == {key: None for key in keys}


def test_s154_a_block_withdraws_what_a_pending_request_disclosed(make_user, api):
    """A block does not touch pending rows, so a client who asked and then blocked the
    trainer stayed listed on `trainer/pending-requests/` with their email, age, gender,
    goals, activity level and note — shown to the person the block is against."""
    from moderation.models import Block
    from users.models import TrainerClientRelation

    personal = ("client_email", "client_profile_picture", "client_age", "client_gender",
                "client_goals", "client_activity_level", "note", "requested_at")
    trainer = _trainer(make_user, "s154t")
    blocker = make_user(_tag("s154a"), age=31, gender="Female", client_goals=["lose"])
    blocked_by_trainer = make_user(_tag("s154b"), age=29, gender="Male")
    unblocked = make_user(_tag("s154c"), age=40, gender="Male")
    for person in (blocker, blocked_by_trainer, unblocked):
        TrainerClientRelation.objects.create(
            trainer=trainer, client=person, status="pending", initiated_by="client",
            client_note="knee surgery last year", requested_at=timezone.now())
    Block.objects.create(blocker=blocker, blocked=trainer)
    Block.objects.create(blocker=trainer, blocked=blocked_by_trainer)

    listed = api(trainer).get("/api/auth/trainer/pending-requests/")
    assert listed.status_code == 200, listed.content
    rows = {row["client_id"]: row for row in listed.json()["pending_requests"]}

    for person in (blocker, blocked_by_trainer):
        row = rows[person.id]            # still listed, so it can be declined
        leaked = [key for key in personal if key in row]
        assert not leaked, (person.username, leaked)
        assert person.email not in listed.content.decode()
    # Whichever side blocked, the two rows look the same.
    assert set(rows[blocker.id]) == set(rows[blocked_by_trainer.id])
    # A client who asked and blocked nobody is shown as before.
    assert rows[unblocked.id]["client_email"] == unblocked.email
    assert rows[unblocked.id]["note"] == "knee surgery last year"


def test_s155_every_refusal_on_the_request_paths_carries_its_code(make_user, api,
                                                                    monkeypatch):
    """`already assigned to another trainer` (twice), the approval's hold failure, both
    unassign refund failures and a non-numeric id answered with no `code` of their own —
    and `{"request_id": "abc"}` was a ValueError the broad handler turned into a 500."""
    from users import trainer_relations
    from users.models import TrainerClientRelation
    from users.trainer_relations import HoldFailed

    trainer = _trainer(make_user, "s155t")
    other = _trainer(make_user, "s155o")

    # A request id or trainer id that is not a number names nothing.
    abc = api(trainer).post("/api/auth/trainer/respond-to-request/",
                            {"request_id": "abc", "action": "approve"}, content_type=JSON)
    assert (abc.status_code, abc.json()["code"]) == (404, "request_not_found")
    asker = make_user(_tag("s155q"))
    for bad in ("abc", [1], 999999999):
        missing = api(asker).post("/api/auth/client/request-trainer/",
                                  {"trainer_id": bad}, content_type=JSON)
        assert (missing.status_code, missing.json()["code"]) == (404, "not_found"), bad

    # The client already has another trainer: both checks, one code.
    taken = make_user(_tag("s155a"))
    taken.assigned_trainer = other
    taken.save(update_fields=["assigned_trainer"])
    relation = TrainerClientRelation.objects.create(
        trainer=trainer, client=taken, status="pending", initiated_by="client")
    refused = api(trainer).post("/api/auth/trainer/respond-to-request/",
                                {"request_id": relation.id, "action": "approve"},
                                content_type=JSON)
    assert (refused.status_code, refused.json()["code"]) == (400, "client_has_other_trainer")

    held_elsewhere = make_user(_tag("s155b"))
    TrainerClientRelation.objects.create(trainer=other, client=held_elsewhere,
                                         status="approved", initiated_by="client")
    relation = TrainerClientRelation.objects.create(
        trainer=trainer, client=held_elsewhere, status="pending", initiated_by="client")
    refused = api(trainer).post("/api/auth/trainer/respond-to-request/",
                                {"request_id": relation.id, "action": "approve"},
                                content_type=JSON)
    assert (refused.status_code, refused.json()["code"]) == (400, "client_has_other_trainer")

    # The month cannot be held: `hold_failed`, or the rate's own code when it has one.
    short = make_user(_tag("s155c"))
    relation = TrainerClientRelation.objects.create(
        trainer=trainer, client=short, status="pending", initiated_by="client")

    def no_hold(relation, actor):
        raise HoldFailed("Insufficient client wallet balance for the trainer charge")

    monkeypatch.setattr(trainer_relations, "take_hold", no_hold)
    refused = api(trainer).post("/api/auth/trainer/respond-to-request/",
                                {"request_id": relation.id, "action": "approve"},
                                content_type=JSON)
    assert (refused.status_code, refused.json()["code"]) == (402, "hold_failed")

    def unpriced(relation, actor):
        failure = HoldFailed("no rate")
        failure.code = "exchange_rate_missing"
        raise failure

    monkeypatch.setattr(trainer_relations, "take_hold", unpriced)
    refused = api(trainer).post("/api/auth/trainer/respond-to-request/",
                                {"request_id": relation.id, "action": "approve"},
                                content_type=JSON)
    assert (refused.status_code, refused.json()["code"]) == (402, "exchange_rate_missing")
    relation.refresh_from_db()
    assert relation.status == "pending", "a refused approval changed the row"

    # The held month cannot be returned: `refund_failed` on both unassign routes, the
    # code the decline path already answers for the same condition.
    def no_refund(relation, actor):
        raise HoldFailed("escrow cannot cover this hold")

    monkeypatch.setattr(trainer_relations, "release_hold", no_refund)
    mine = make_user(_tag("s155d"))
    TrainerClientRelation.objects.create(trainer=trainer, client=mine,
                                         status="approved", initiated_by="client")
    mine.assigned_trainer = trainer
    mine.save(update_fields=["assigned_trainer"])
    kept = api(trainer).post("/api/auth/trainer/unassign-client/", {"client_id": mine.id},
                             content_type=JSON)
    assert (kept.status_code, kept.json()["code"]) == (409, "refund_failed")
    kept = api(mine).post("/api/auth/client/unassign-trainer/", {}, content_type=JSON)
    assert (kept.status_code, kept.json()["code"]) == (409, "refund_failed")
    assert TrainerClientRelation.objects.filter(
        trainer=trainer, client=mine, status="approved").exists(), "nothing may change"


# ================================================================ diet (S-046..S-097)
def test_s046_no_unreachable_date_branch_is_left_in_the_date_views(make_user, api):
    """`if target_date_str and target_date is None` sat after the `bad_date` return
    that already covers it: dead, untranslated and code-less, and it read like a live
    guard. Its identical twin in `DietPlanNutritionView` went with it."""
    from django.utils import translation

    from diet.views import DietPlanNutritionView, EnhancedClientProgressView
    from subscription.models import Subscription, SubscriptionPlan

    for view in (EnhancedClientProgressView, DietPlanNutritionView):
        source = inspect.getsource(view)
        assert "if target_date_str and target_date is None" not in source, view.__name__
        assert "Invalid date format. Use YYYY-MM-DD." not in source, view.__name__

    # The live guard above it still answers.
    client = make_user(_tag("s046"))
    with translation.override("en"):
        plan = SubscriptionPlan.objects.create(
            name=f"fx diet {uuid.uuid4().hex[:6]}", plan_type="premium", description="d",
            price=0, currency="SYP", duration_days=30, is_active=True,
            has_diet_access=True)
    Subscription.objects.create(user=client, plan=plan, status="active")
    bad = api(client).get("/api/diet/v1/client/progress/enhanced/?date=2026-13-45")
    assert (bad.status_code, bad.json()["code"]) == (400, "bad_date")


def test_s047_a_trainer_on_the_progress_routes_is_told_clients_only(make_user, api):
    """The three "Only clients can access progress tracking" 403s named no code, so the
    envelope filled in the generic `permission_denied`: the same refusal the coaching
    routes answer as `clients_only`."""
    trainer = _trainer(make_user, "s047", trainer_is_verified=True)   # past the paywall
    for path in ("/api/diet/v1/client/progress/", "/api/diet/v1/client/progress/enhanced/",
                 "/api/diet/v1/client/progress/weekly/"):
        refused = api(trainer).get(path)
        assert (refused.status_code, refused.json()["code"]) == (403, "clients_only"), path


def test_s048_the_advice_views_own_500_names_its_code(make_user, monkeypatch):
    """`DailyAdviceView`'s broad handler returned `{"error": ...}` alone while every
    sibling in the file names `server_error`, and its docstring — the contract the app
    sessions read — said a 500 there carries no `code`.

    Driven through the view itself, without the middleware that pads a missing code, so
    what is asserted is the view's own answer."""
    from rest_framework.test import APIRequestFactory, force_authenticate

    from diet import views as diet_views

    class _Broken:
        class objects:            # noqa: N801 — stands in for a manager
            @staticmethod
            def filter(*args, **kwargs):
                raise RuntimeError("database fell over")

    trainer = _trainer(make_user, "s048", trainer_is_verified=True)
    monkeypatch.setattr(diet_views, "DailyAdvice", _Broken)
    request = APIRequestFactory().get("/api/diet/v1/advice/latest/")
    force_authenticate(request, user=trainer)
    response = diet_views.DailyAdviceView.as_view()(request)

    assert response.status_code == 500
    assert response.data.get("code") == "server_error", response.data
    assert "server_error" in diet_views.DailyAdviceView.__doc__
    assert "500 with no `code`" not in diet_views.DailyAdviceView.__doc__


def test_s097_advice_is_written_for_an_incomplete_profile_and_names_the_plans_figure(
        make_user):
    """The row's `context_data` called the profile formula, which raises for a profile
    without height, weight, age or gender: a client on a trainer's plan with none of
    those never got advice at all. With a complete profile it stored a figure from an
    activity level the client never chose, beside the plan's target in the text."""
    from diet.models import DailyAdvice
    from diet.tasks import generate_daily_advice

    today = timezone.localdate()

    # A trainer's plan, and no height, weight, age or gender.
    bare = make_user(_tag("s097a"), preferred_language="en")
    _plan(bare, today - timedelta(days=1), today + timedelta(days=5), calories=1800)
    generate_daily_advice(user_id=bare.id)
    advice = DailyAdvice.objects.get(user=bare)
    assert advice.context_data["user_metrics"]["daily_calories"] == 1800
    assert "1800" in advice.text, advice.text

    # A complete profile: the stored figure is the one in the sentence, the plan's.
    full = make_user(_tag("s097b"), preferred_language="en", weight=80, height=180,
                     age=30, gender="Male")
    _plan(full, today, today + timedelta(days=6), calories=1737)
    assert round(float(full.calculate_daily_calories())) != 1737
    generate_daily_advice(user_id=full.id)
    advice = DailyAdvice.objects.get(user=full)
    assert advice.context_data["user_metrics"]["daily_calories"] == 1737
    assert "1737" in advice.text, advice.text

    # No plan for today and an activity level never answered: no figure is invented
    # (the owner, 2026-09-20: nothing is built from an unanswered level).
    guess = make_user(_tag("s097c"), preferred_language="en", weight=80, height=180,
                      age=30, gender="Male")
    assert guess.activity_level_unanswered
    _plan(guess, today + timedelta(days=3), today + timedelta(days=9))   # next week's
    generate_daily_advice(user_id=guess.id)
    advice = DailyAdvice.objects.get(user=guess)
    assert advice.context_data["user_metrics"]["daily_calories"] is None
    assert "None" not in advice.text and not any(ch.isdigit() for ch in advice.text), \
        advice.text


# ======================================================== ai_assistant (S-050..S-091)
def test_s050_the_daily_reset_is_an_instant_with_its_offset(make_user, settings):
    """`resets_at` was a naive ISO string ("2026-09-24T00:00:00"): midnight in a zone
    the app had no way to know."""
    from ai_assistant.consumers import AIChatConsumer
    from training_platform.cache import ratelimit_cache

    settings.AI_ASSISTANT_CONFIG = {**getattr(settings, "AI_ASSISTANT_CONFIG", {}),
                                    "MAX_MESSAGES_PER_DAY": 50}
    person = make_user(_tag("s050"))
    ratelimit_cache().clear()
    consumer = AIChatConsumer()
    consumer.user = person

    allowed, remaining, resets_at = async_to_sync(consumer._reserve_rate_limit)()
    assert (allowed, remaining) == (True, 49)

    resets = datetime.fromisoformat(resets_at)
    assert resets.tzinfo is not None, resets_at
    zone = timezone.get_current_timezone()
    midnight = datetime.combine(timezone.localdate() + timedelta(days=1), time.min,
                                tzinfo=zone)
    assert resets == midnight and resets.utcoffset() == midnight.utcoffset()
    assert resets > timezone.now()


class _Interleaving:
    """The rate-limit cache, with a second socket's whole reservation run right after
    this socket's FIRST cache call — the moment two sockets racing on one account
    interleave."""

    def __init__(self, real, other_socket):
        self.real, self.other_socket, self.fired = real, other_socket, False

    def __getattr__(self, name):
        attr = getattr(self.real, name)
        if name not in ("add", "incr", "set", "decr", "get"):
            return attr

        def call(*args, **kwargs):
            try:
                return attr(*args, **kwargs)
            finally:
                if not self.fired:
                    self.fired = True
                    self.other_socket()
        return call


def test_s089_the_first_message_of_the_day_is_reserved_atomically(make_user, settings,
                                                                    monkeypatch):
    """`incr` raises on a missing key, and each socket that saw it raise then `set` the
    counter to 1: two sockets sending the first message of the day together were both
    allowed on an allowance of one, and the counter ended at 1."""
    from ai_assistant.consumers import AIChatConsumer
    from training_platform import cache as cache_module

    settings.AI_ASSISTANT_CONFIG = {**getattr(settings, "AI_ASSISTANT_CONFIG", {}),
                                    "MAX_MESSAGES_PER_DAY": 1}
    person = make_user(_tag("s089"))
    real = cache_module.ratelimit_cache()
    real.clear()                                     # no key: the first message today

    first_socket, second_socket = AIChatConsumer(), AIChatConsumer()
    first_socket.user = second_socket.user = person
    answers = []

    def the_other_socket():
        # Its own thread: the first socket's event loop is running in this one.
        worker = threading.Thread(target=lambda: answers.append(
            async_to_sync(second_socket._reserve_rate_limit)()))
        worker.start()
        worker.join(10)

    racing = _Interleaving(real, the_other_socket)
    monkeypatch.setattr(cache_module, "ratelimit_cache", lambda: racing)
    answers.append(async_to_sync(first_socket._reserve_rate_limit)())

    assert racing.fired and len(answers) == 2, answers
    allowed = [answer[0] for answer in answers]
    assert allowed.count(True) == 1, f"an allowance of one let {allowed.count(True)} through"
    key = f"ai_chat_limit:{person.id}:{timezone.localdate().isoformat()}"
    assert int(real.get(key)) == 1


def test_s060_the_coach_reads_the_plan_that_covers_today(make_user):
    """"The plan" was the newest active one. A trainer scheduling next week leaves two
    active plans (only overlapping ones are superseded), and the coach then read next
    week's: no meals today, and nothing tracked."""
    from ai_assistant.analyzers.diet_analyzer import DietAnalyzer
    from ai_assistant.tools.diet_tools import (get_active_diet_plan, get_diet_adherence,
                                               get_meal_details)
    from ai_assistant.tools.progress_tools import get_overall_progress

    client = make_user(_tag("s060"))
    today = timezone.localdate()
    current = _plan(client, today - timedelta(days=2), today + timedelta(days=4),
                    meal_days=[today])
    upcoming = _plan(client, today + timedelta(days=5), today + timedelta(days=11),
                     meal_days=[today + timedelta(days=5)])     # created later: newer
    _tracked(client, current, today - timedelta(days=1))

    assert get_active_diet_plan(client)["active_plan"]["id"] == current.id
    todays = get_meal_details(client)
    assert len(todays["meals"]) == 1, todays
    # A day in next week's plan is read from that plan: the date asked decides.
    later = get_meal_details(client, date_str=(today + timedelta(days=5)).isoformat())
    assert len(later["meals"]) == 1, later
    assert upcoming.pk != current.pk

    assert get_diet_adherence(client, days=7)["days_tracked"] == 1
    assert get_overall_progress(client, days=7)["diet"]["days_tracked"] == 1
    assert DietAnalyzer().analyze(client)["week2_adherence_pct"] == 100.0


def test_s061_rest_feedback_is_stored_for_a_user_without_training_consent(make_user, api):
    """`POST /api/ai/feedback/` updated only the training rows, which exist only for
    the few who opted in, and answered "Feedback saved" to everyone else with nothing
    saved; the messages list never said what had been given either."""
    from ai_assistant.models import ChatMessage, ChatSession

    person = _ai_subscriber(make_user, "s061", ai_training_consent=False)
    session = ChatSession.objects.create(user=person)
    ChatMessage.objects.create(session=session, role="user", content="protein?")
    reply = ChatMessage.objects.create(session=session, role="assistant",
                                       content="Eat more protein.")

    saved = api(person).post("/api/ai/feedback/",
                             {"message_id": reply.id, "feedback": "positive"},
                             content_type=JSON)
    assert saved.status_code == 200, saved.content
    reply.refresh_from_db()
    assert reply.feedback == "positive", "answered 'Feedback saved' and stored nothing"
    assert saved.json()["feedback"] == "positive"

    listed = api(person).get(f"/api/ai/sessions/{session.session_id}/messages/")
    assert listed.status_code == 200, listed.content
    by_id = {row["id"]: row for row in listed.json()["results"]}
    assert by_id[reply.id]["feedback"] == "positive"

    # Sending again replaces it, over REST as over the socket.
    api(person).post("/api/ai/feedback/", {"message_id": reply.id, "feedback": "negative"},
                     content_type=JSON)
    reply.refresh_from_db()
    assert reply.feedback == "negative"


def _drive(stream):
    """Every event an async generator yields, stepped by hand (see `_chat` in
    `tests/test_moderation.py` for why not under an event loop). Fails if anything
    really suspends."""
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


class _FakeCompletions:
    """The model: first a call to one tool, then a sentence."""

    def __init__(self, tool):
        self.tool, self.calls = tool, 0

    async def create(self, **kwargs):
        self.calls += 1
        if self.calls == 1:
            call = SimpleNamespace(index=0, id="call_1", function=SimpleNamespace(
                name=self.tool, arguments="{}"))
            chunks = [SimpleNamespace(delta=SimpleNamespace(content=None,
                                                            tool_calls=[call]))]
        else:
            chunks = [SimpleNamespace(delta=SimpleNamespace(content="Looks good.",
                                                            tool_calls=None))]
        return _Chunks([SimpleNamespace(choices=[chunk]) for chunk in chunks])


class _Chunks:
    def __init__(self, chunks):
        self.chunks = list(chunks)

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self.chunks:
            raise StopAsyncIteration
        return self.chunks.pop(0)


def test_s090_the_tool_lines_are_in_the_readers_language(make_user):
    """`tool_used.display` and `complete.tools_used[].display` were English literals, so
    an Arabic reader saw "Reviewed your profile" beside an Arabic answer. Asserted on
    the frames a real turn yields, with only the model and the clock replaced."""
    import asyncio
    from unittest import mock

    from django.utils import translation

    from ai_assistant.services import chat_service as chat_service_module
    from ai_assistant.tool_registry import _TOOL_FUNCTIONS, ToolRegistry

    # Every tool has a line, and each one is Arabic for an Arabic reader.
    with translation.override("ar"):
        for name in _TOOL_FUNCTIONS:
            line = ToolRegistry.get_display_name(name)
            assert isinstance(line, str) and _has_arabic(line), (name, line)
    with translation.override("en"):
        assert ToolRegistry.get_display_name("get_user_profile").endswith(
            "Reviewed your profile")
    assert ToolRegistry.get_display_name("no_such_tool") == "no_such_tool"

    person = _ai_subscriber(make_user, "s090", preferred_language="ar")

    def inline(func):
        async def run_now(*args, **kwargs):
            return func(*args, **kwargs)
        return run_now

    async def no_clock(awaitable, timeout=None):
        return await awaitable

    service = chat_service_module.ChatService()
    service.client = SimpleNamespace(chat=SimpleNamespace(
        completions=_FakeCompletions("get_user_profile")))
    service.memory_service = SimpleNamespace(
        count_tokens=lambda text: len(text or "") // 4,
        truncate_tool_result=lambda result: result,
        get_truncated_history=lambda session: [])

    with mock.patch.object(chat_service_module, "database_sync_to_async", inline), \
            mock.patch.object(asyncio, "wait_for", no_clock), translation.override("ar"):
        events = _drive(service.chat_stream(person, "how am I doing?"))

    used = [event for event in events if event["type"] == "tool_used"]
    (complete,) = [event for event in events if event["type"] == "complete"]
    assert used and used[0]["name"] == "get_user_profile", events
    for line in [used[0]["display"]] + [tool["display"] for tool in complete["tools_used"]]:
        assert isinstance(line, str) and _has_arabic(line), line
    json.dumps(events)          # what the socket sends: plain strings, no lazy proxies


def test_s091_adherence_counts_the_days_nothing_was_tracked(make_user):
    """Since S-023 a `DailyProgress` row exists only for a day something was ticked, so
    the average covered the logged days alone: 2 perfect days out of 6 read as 100%.
    And "nothing tracked" read as 0 in two of the three places."""
    from ai_assistant.analyzers.diet_analyzer import DietAnalyzer
    from ai_assistant.tools.diet_tools import get_diet_adherence
    from ai_assistant.tools.progress_tools import get_overall_progress

    today = timezone.localdate()
    meal_days = [today - timedelta(days=offset) for offset in range(6, -1, -1)]

    client = make_user(_tag("s091"))
    plan = _plan(client, today - timedelta(days=6), today + timedelta(days=3),
                 meal_days=meal_days)
    for offset in (2, 1):
        _tracked(client, plan, today - timedelta(days=offset))    # on target: 100

    # Six finished plan days (today is not over and nothing is ticked yet), two of
    # them perfect, four with nothing: 200 / 6.
    expected = round(200 / 6, 1)
    adherence = get_diet_adherence(client, days=7)
    assert adherence["average_adherence_pct"] == expected, adherence
    assert (adherence["days_tracked"], adherence["plan_days"]) == (2, 6)
    assert adherence.get("message")

    diet = get_overall_progress(client, days=7)["diet"]
    assert diet["caloric_adherence_pct"] == expected, diet
    assert diet["plan_days"] == 6

    analysis = DietAnalyzer().analyze(client)
    assert analysis["week2_adherence_pct"] == expected, analysis
    # The week before the plan began: nothing to compare, and not a score of 0.
    assert analysis["week1_adherence_pct"] is None
    assert analysis["adherence_trend"] is None

    # A client with a plan who has tracked nothing: no score anywhere.
    idle = make_user(_tag("s091i"))
    _plan(idle, today - timedelta(days=6), today + timedelta(days=3), meal_days=meal_days)
    diet = get_overall_progress(idle, days=7)["diet"]
    assert diet["caloric_adherence_pct"] is None and diet["meal_completion_pct"] is None
    assert get_diet_adherence(idle, days=7)["average_adherence_pct"] is None
    assert DietAnalyzer().analyze(idle)["week2_adherence_pct"] is None
