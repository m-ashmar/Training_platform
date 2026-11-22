"""The `users` and `diet` rows of `SIDE_BUGS.md`, one test per defect.

Each names the defect it closes and was watched failing against the behaviour that
was there before the fix:

    S-023  `ClientProgressService.get_daily_progress` and
           `get_enhanced_daily_progress` did `get_or_create` + `SELECT … FOR UPDATE`
           + `save()` — so opening the progress screen WROTE a row and two readers of
           the same day serialised behind the lock
    S-025  `DailyAdviceView`'s docstring promised the AI-advice entitlement on a route
           that serves rule-written text and carries the diet guard
    S-026  every candidate trainer's reasons were rendered in Arabic AND English
           before the top three were sliced
    S-035  `request_id is required` and `action must be … "approve" … "reject"` had no
           `code`, beside twelve refusals in the same file that do
"""
from datetime import time, timedelta

import pytest
from django.utils import timezone

pytestmark = pytest.mark.django_db

JSON = "application/json"


# ------------------------------------------------------------------------ helpers
def _plan_with(tag, **features):
    """A subscription plan carrying exactly the features a test names."""
    from django.utils import translation

    from subscription.models import SubscriptionPlan

    # modeltranslation resolves `name` against the ACTIVE language, so the lookup has
    # to be pinned or a preceding Arabic request collides on the unique column.
    with translation.override("en"):
        plan, _created = SubscriptionPlan.objects.get_or_create(
            name=f"people {tag}",
            defaults=dict(plan_type="premium", description="side bugs", price="5000.00",
                          currency="SYP", duration_days=30, is_active=True, **features))
    return plan


def _subscribe(user, tag, **features):
    from subscription.models import Subscription

    return Subscription.objects.create(
        user=user, plan=_plan_with(tag, **features), status="active",
        end_date=timezone.now() + timedelta(days=30))


def _diet_day(user, *, calories=2000.0, meals=2):
    """A plan covering today with `meals` meals, one of them carrying a real food."""
    from diet.models import DietPlan, FoodCategory, FoodItem, Meal, MealComponent

    today = timezone.localdate()
    plan = DietPlan.objects.create(
        user=user, goal="Maintain", daily_calories=calories,
        # Stored targets, so `macro_targets()` answers from the row and the query count
        # below does not depend on the planner's policy tables.
        target_protein=150.0, target_carbs=220.0, target_fat=65.0,
        start_date=today - timedelta(days=1), end_date=today + timedelta(days=5),
        duration_weeks=1)
    category, _ = FoodCategory.objects.get_or_create(name="people-cat")
    food, _ = FoodItem.objects.get_or_create(
        api_id="people-oats",
        defaults=dict(name="People Oats", calories=380.0, protein=13.0, carbs=67.0,
                      fat=7.0, serving_size="100 g", serving_size_grams=100,
                      category=category))
    first = None
    for i in range(meals):
        meal = Meal.objects.create(
            diet_plan=plan, date=today, meal_type="Breakfast" if i == 0 else "Dinner",
            name=f"people meal {i}", scheduled_time=time(8 + i, 0))
        if first is None:
            first = meal
    MealComponent.objects.create(meal=first, food=food, quantity=100.0)
    return plan, first


def _writes_to_progress(captured):
    """Every captured statement that writes the day's progress row, or locks it."""
    bad = []
    for query in captured:
        sql = query["sql"].lower()
        if "diet_dailyprogress" not in sql:
            continue
        if sql.startswith("insert") or sql.startswith("update") or "for update" in sql:
            bad.append(query["sql"])
    return bad


# ============================================================== S-023: a read reads
def test_s023_opening_the_progress_screen_writes_no_row_and_takes_no_lock(
        make_user, api, django_assert_max_num_queries):
    """Three GET routes went through a service that did `get_or_create` plus
    `SELECT … FOR UPDATE` plus `save()`. Opening a screen minted a `DailyProgress` row
    for a day the client had not touched, and two readers of the same day queued
    behind one another for a number neither of them changed.

    `django_assert_max_num_queries` is used for its capture, not its bound: the
    assertion that matters is that NOTHING in the captured SQL writes or locks
    `diet_dailyprogress`. An exact total would also count the auth, subscription and
    middleware queries around the view, which this defect is not about — the service's
    own count is pinned exactly further down.
    """
    from diet.models import DailyProgress

    user = make_user("s023_reader")
    _subscribe(user, "s023", has_diet_access=True)
    plan, _meal = _diet_day(user)
    assert not DailyProgress.objects.filter(user=user).exists()

    for url in ("/api/diet/v1/client/progress/",
                "/api/diet/v1/client/progress/enhanced/"):
        with django_assert_max_num_queries(60) as captured:
            response = api(user).get(url)
        assert response.status_code == 200, (url, response.content[:200])
        assert _writes_to_progress(captured.captured_queries) == [], url
        assert not DailyProgress.objects.filter(user=user).exists(), (
            f"{url} created a progress row on a GET")

    # And it still answers the day's figures, not an empty shape.
    body = api(user).get("/api/diet/v1/client/progress/").json()
    assert body["has_active_plan"] is True
    assert (body["total_meals"], body["meals_completed"]) == (2, 0)
    assert body["target_calories"] == plan.daily_calories
    assert body["calories_consumed"] == 0
    assert body["is_day_completed"] is False


def test_s023_the_read_service_runs_three_selects_and_nothing_else(
        make_user, django_assert_num_queries):
    """The service on its own: one plan lookup and two aggregates. No INSERT, no
    UPDATE, no lock — the numbers are computed on an instance that is never saved."""
    from diet.models import DailyProgress
    from diet.trainer_services import ClientProgressService

    user = make_user("s023_service")
    _diet_day(user)

    with django_assert_num_queries(3):
        figures = ClientProgressService(user).get_daily_progress()
    assert figures["total_meals"] == 2
    assert not DailyProgress.objects.filter(user=user).exists()

    row = DailyProgress.figures_for(user, user.diet_plans.get(), timezone.localdate())
    assert row.pk is None, "figures_for must hand back a detached instance"


def test_s023_what_the_client_eats_is_still_written_and_still_read_back(make_user, api):
    """The create moved to the write paths, so the day has to survive the round trip:
    ticking a component writes the row, and the read — which now writes nothing —
    reports it."""
    from diet.models import DailyProgress, MealComponent

    user = make_user("s023_eater")
    _subscribe(user, "s023", has_diet_access=True)
    _plan, meal = _diet_day(user)

    MealComponent.objects.get(meal=meal).complete()
    stored = DailyProgress.objects.get(user=user, date=timezone.localdate())
    assert stored.calories_consumed > 0

    body = api(user).get("/api/diet/v1/client/progress/").json()
    assert body["calories_consumed"] == stored.calories_consumed
    enhanced = api(user).get("/api/diet/v1/client/progress/enhanced/").json()
    assert enhanced["progress"]["calories_consumed"] == stored.calories_consumed
    assert DailyProgress.objects.filter(user=user).count() == 1


# ================================================= S-025: the advice route is not AI
def test_s025_the_advice_route_is_the_diet_feature_and_says_so(make_user, api):
    """The docstring claimed the AI-advice entitlement while the code checked diet
    access and no consent at all. Establishing which was the bug: the only writer of
    `DailyAdvice` is `diet/tasks.py`, whose text is three gettext sentences chosen from
    the client's goal, plan calories and BMI. Nothing calls a model, so the AI-coach
    guards do not belong here and the DOCSTRING was the defect.

    Pinned here: a client with the diet feature, WITHOUT the `ai_advice` entitlement
    and WITHOUT AI consent, reads their advice — no `no_entitlement`, no
    `ai_consent_required` — and the docstring no longer promises an entitlement the
    route does not enforce.
    """
    from diet.models import DailyAdvice
    from diet.views import DailyAdviceView
    from moderation.models import AIConsent
    from moderation.permissions import HasAIConsent
    from subscription.permissions import HasAIAdviceAccess

    user = make_user("s025_reader")
    _subscribe(user, "s025", has_diet_access=True, has_ai_advice=False)
    assert not AIConsent.objects.filter(user=user).exists()
    DailyAdvice.objects.create(user=user, for_date=timezone.localdate(),
                               text="Keep your intake steady today.", context_data={})

    response = api(user).get("/api/diet/v1/advice/latest/")
    assert response.status_code == 200, response.content[:200]
    assert response.json()["advice"]["text"] == "Keep your intake steady today."

    enforced = set(DailyAdviceView.permission_classes)
    assert HasAIAdviceAccess not in enforced and HasAIConsent not in enforced
    assert "AI advice access" not in (DailyAdviceView.__doc__ or ""), (
        "the docstring still promises an entitlement the route does not check")


def test_s025_the_ai_coach_itself_still_carries_both_guards(make_user, api):
    """The other half of the finding: the guard is where the AI actually answers. If
    this ever stops being true, the advice route's docstring is wrong again."""
    from ai_assistant.views import AI_PERMISSIONS
    from moderation.permissions import HasAIConsent
    from subscription.permissions import HasAIAdviceAccess

    assert AI_PERMISSIONS.index(HasAIAdviceAccess) < AI_PERMISSIONS.index(HasAIConsent), \
        "entitlement before consent: a lapsed subscriber hears no_entitlement"

    user = make_user("s025_ai")
    _subscribe(user, "s025", has_diet_access=True, has_ai_advice=True)
    refused = api(user).get("/api/ai/sessions/")
    assert refused.status_code == 403
    assert refused.json()["code"] == "ai_consent_required"


# ===================================== S-026: score first, slice, and only then render
def _coach(make_user, username, **fields):
    from decimal import Decimal

    # Deliberate contract change (Road to 10 S0.3, BACKEND_BUGS B-198, 2026-11-22): a
    # trainer with no first name is not listed or matched, so a listed coach has one.
    base = dict(user_type="trainer", trainer_is_available=True, email_verified=True,
                trainer_hourly_rate=Decimal("12000"), first_name="Coach")
    base.update(fields)
    return make_user(username, **base)


def test_s026_reasons_are_rendered_only_for_the_trainers_on_the_page(
        make_user, api, monkeypatch):
    """Every candidate's reasons were built in BOTH languages — two
    `translation.override` blocks and a catalogue lookup each, up to seven per trainer
    — and then all but the top three were thrown away. With eight equally-suited
    trainers that is thirty-two renders for the nine the client reads.

    The count is asserted as "one render per reason that actually reaches the client",
    which is the invariant, not a magic number: it holds for any candidate count.
    """
    import users.trainer_directory as directory

    client = make_user("s026_client", training_goal="build_muscle",
                       training_place="gym", injury_areas=["shoulder"])
    for i in range(8):
        _coach(make_user, f"s026_t{i}", trainer_goal_focus=["build_muscle"],
               trainer_injury_focus=["shoulder"], trainer_training_places=["gym"],
               trainer_experience_years=11)

    renders = []
    original = directory._in_both_languages

    def counting(render):
        rendered = original(render)
        renders.append(rendered)
        return rendered

    monkeypatch.setattr(directory, "_in_both_languages", counting)

    response = api(client).get("/api/v1/trainers/matches?limit=3")
    assert response.status_code == 200, response.content[:200]
    results = response.json()["results"]
    assert len(results) == 3

    shown = sum(len(row["reasons"]) for row in results)
    assert shown == 9, [r["reasons"] for r in results]
    assert len(renders) == shown, (
        f"{len(renders)} reasons rendered for the {shown} the client reads")


def test_s026_the_top_threes_reasons_are_exactly_what_they_were(make_user, api):
    """Deferring the render must not change one character of the output."""
    client = make_user("s026_same_c", training_goal="build_muscle",
                       training_place="gym", injury_areas=["shoulder"])
    _coach(make_user, "s026_same_t", trainer_goal_focus=["build_muscle"],
           trainer_injury_focus=["shoulder"], trainer_training_places=["gym"])

    (row,) = [r for r in api(client).get("/api/v1/trainers/matches?limit=3").json()["results"]
              if r["trainer"]["id"] != client.pk]
    assert row["reasons"] == [
        {"code": "goal_match",
         "text": {"ar": "متخصص في هدفك: بناء العضل",
                  "en": "Specialises in your goal: building muscle"}},
        {"code": "injury_experience",
         "text": {"ar": "خبرة مع إصابات الكتف", "en": "Experience with shoulder injuries"}},
        {"code": "training_place",
         "text": {"ar": "برامج للتمرين في النادي",
                  "en": "Programs for training at the gym"}},
    ]


def test_s026_scoring_a_candidate_renders_nothing(make_user):
    """The unit behind it: `_score` hands back callables, so ranking a trainer who is
    never shown costs no catalogue lookup at all."""
    import users.trainer_directory as directory

    client = make_user("s026_unit_c", training_goal="build_muscle",
                       training_place="gym", injury_areas=["shoulder"])
    trainer = _coach(make_user, "s026_unit_t", trainer_goal_focus=["build_muscle"],
                     trainer_injury_focus=["shoulder"], trainer_training_places=["gym"],
                     trainer_experience_years=11)
    proof = directory.proof_for([trainer])[trainer.pk]

    score, reasons = directory._score(client, trainer, proof, set())
    assert score > 0
    assert [code for code, _render in reasons] == [
        "goal_match", "injury_experience", "training_place"]
    assert all(callable(render) for _code, render in reasons), (
        "_score rendered the text instead of deferring it")
    assert directory._rendered(reasons)[0]["text"]["en"] == \
        "Specialises in your goal: building muscle"


# ==================================================== S-035: every refusal has a code
def test_s035_the_trainer_side_body_refusals_carry_a_code(make_user, api):
    """Twelve refusals in this file gained a `code` last week; these two were left, so
    the app had to read English message text to tell them apart."""
    trainer = make_user("s035_trainer", user_type="trainer")

    missing = api(trainer).post("/api/auth/trainer/respond-to-request/", {},
                                content_type=JSON)
    assert missing.status_code == 400, missing.content[:200]
    assert missing.json()["code"] == "request_id_required"

    bad = api(trainer).post("/api/auth/trainer/respond-to-request/",
                            {"request_id": 1, "action": "maybe"}, content_type=JSON)
    assert bad.status_code == 400, bad.content[:200]
    assert bad.json()["code"] == "invalid_action"


def test_s035_the_client_side_answers_the_same_code_for_the_same_mistake(make_user, api):
    """Both sides of the handshake refuse with one code, so the app needs one branch —
    the rule the rest of this pair of docstrings already states."""
    client = make_user("s035_client")

    bad = api(client).post("/api/auth/client/respond-to-invite/",
                           {"request_id": 1, "action": "maybe"}, content_type=JSON)
    assert bad.status_code == 400, bad.content[:200]
    assert bad.json()["code"] == "invalid_action"


def test_s035_the_new_codes_are_in_the_contract():
    """A code the app may branch on and the contract does not list is a code the app
    session never learns about."""
    from pathlib import Path

    contract = (Path(__file__).resolve().parent.parent / "API_CONTRACT.md").read_text(
        encoding="utf-8")
    for code in ("request_id_required", "invalid_action"):
        assert f"`{code}`" in contract, code
