"""The regression gate.

Every assertion here corresponds to a defect this audit actually found and fixed. It
exists because 84 fixes across 11 phases had no automated protection: `manage.py test`
discovered nothing, and CI ran security scanners without ever executing the application.

Kept deliberately fast (one shared database, no per-test migrations) so it can run on
every pull request. The 53 standalone probes in tests/security/ remain the deep suite.
"""
import json
import random

import pytest
from django.urls import get_resolver
from django.utils import timezone

pytestmark = pytest.mark.django_db


def _routes(with_params=False):
    def walk(patterns, prefix=""):
        out = []
        for p in patterns:
            if hasattr(p, "url_patterns"):
                out += walk(p.url_patterns, prefix + str(p.pattern))
            else:
                out.append(prefix + str(p.pattern))
        return out

    for r in walk(get_resolver().url_patterns):
        has_param = "<" in r or "(?P" in r
        if has_param != with_params:
            continue
        if any(x in r for x in ("admin/", "swagger", "redoc", "format")):
            continue
        yield "/" + r.lstrip("/")


# --------------------------------------------------------------------------- config
def test_django_check_is_clean():
    from django.core.management import call_command
    call_command("check")


def test_no_pending_migrations():
    """A model change without a migration used to merge cleanly and break deploy."""
    from django.core.management import call_command
    call_command("makemigrations", check=True, dry_run=True, verbosity=0)


def test_every_model_has_a_total_order():
    """Single non-unique ordering is not a total order — paging repeated and hid rows."""
    from django.apps import apps

    local = {"users", "wallet", "diet", "routine", "subscription", "social",
             "ai_assistant", "achievements", "analytics", "notifications", "challenges"}
    weak = []
    for m in apps.get_models():
        if m._meta.app_label not in local:
            continue
        ordering = list(m._meta.ordering or [])
        assert ordering, f"{m.__name__} has no Meta.ordering"
        tail = ordering[-1].lstrip("-")
        unique = tail in ("id", "pk") or any(
            f.name == tail and (f.unique or f.primary_key)
            for f in m._meta.get_fields() if getattr(f, "concrete", False)
        )
        if not unique:
            weak.append((m.__name__, ordering))
    assert not weak, f"non-deterministic ordering: {weak}"


# ------------------------------------------------------------------- authorization
def test_anonymous_gets_no_5xx_and_reaches_only_public_endpoints(api):
    """An unauthenticated sweep caught permission classes that 500'd instead of 401ing."""
    anon = api()
    allowed = {"/api/auth/health/", "/api/auth/trainers/public/", "/api/auth/trainers/stats/"}
    errors, reachable = [], []
    for path in _routes():
        resp = anon.get(path)
        if resp.status_code >= 500:
            errors.append((path, resp.status_code))
        elif 200 <= resp.status_code < 300:
            reachable.append(path)
    assert not errors, f"anonymous 5xx: {errors}"
    assert set(reachable) <= allowed, f"unexpectedly public: {set(reachable) - allowed}"


def test_a_user_cannot_write_to_another_users_content(make_user, api):
    """Any authenticated user could once PATCH and DELETE anyone's post/comment."""
    from social.models import Comment, Post

    alice, bob = make_user("gate_alice"), make_user("gate_bob")
    post = Post.objects.create(author=alice, content="alice", post_type="text", visibility="public")
    comment = Comment.objects.create(post=post, author=alice, content="alice")
    cb = api(bob)

    for url in (f"/api/social/posts/{post.id}/", f"/api/social/comments/{comment.id}/"):
        assert cb.patch(url, json.dumps({"content": "HACKED"}),
                        content_type="application/json").status_code == 403, url
        assert cb.delete(url).status_code == 403, url
    post.refresh_from_db()
    assert post.content == "alice"


def test_privilege_escalation_via_profile_update_is_ignored(make_user, api):
    alice = make_user("gate_esc")
    c = api(alice)
    for payload in ({"user_type": "admin"}, {"is_staff": True}, {"is_superuser": True}):
        c.post("/api/auth/user/update/", json.dumps(payload), content_type="application/json")
    alice.refresh_from_db()
    assert alice.user_type == "client"
    assert not alice.is_staff and not alice.is_superuser


# --------------------------------------------------------------------- data safety
def test_deleting_a_user_cannot_erase_the_ledger(make_user):
    """user.delete() used to cascade away the wallet balance and all payment history."""
    import decimal
    from django.db.models import ProtectedError
    from wallet.models import Wallet

    u = make_user("gate_wallet")
    w, _ = Wallet.objects.get_or_create(owner=u)
    w.balance = decimal.Decimal("250.00")
    w.save()
    with pytest.raises(ProtectedError):
        u.delete()
    w.refresh_from_db()
    assert w.balance == decimal.Decimal("250.00")


def test_declared_allergens_are_never_served(db):
    """`peanuts, shellfish, milk` once blocked nothing at all."""
    from diet.services.meal_validator import MealValidator

    class F:
        def __init__(self, n):
            self.name = n
            self.allergens = []
            self.allergen_source = "unknown"
            self.ingredients_text = ""

    offered = ["Peanut butter", "Shellfish platter", "Milk chocolate", "Grilled chicken"]
    kept = [f.name for f, _q in
            MealValidator("peanuts, shellfish, milk").validate([(F(n), "1") for n in offered])]
    assert kept == ["Grilled chicken"], kept


def test_impossible_nutrition_is_rejected(db):
    from django.core.exceptions import ValidationError
    from diet.models import FoodItem

    impossible = FoodItem(name="Cheese, Brick", api_id="gate", calories=1200,
                          protein=23.2, carbs=2.79, fat=29.7,
                          serving_size="Serving", serving_size_grams=100)
    with pytest.raises(ValidationError):
        impossible.clean()

    fine = FoodItem(name="Chicken", api_id="gate2", calories=165, protein=31, carbs=0,
                    fat=3.6, serving_size="100g", serving_size_grams=100)
    fine.clean()


def test_uploads_reject_disguised_payloads(db):
    """A PHP shell renamed .png used to be accepted."""
    from django.core.exceptions import ValidationError
    from django.core.files.uploadedfile import SimpleUploadedFile
    from training_platform.file_security import process_uploaded_image

    shell = SimpleUploadedFile("evil.png", b"<?php system($_GET['c']); ?>", content_type="image/png")
    with pytest.raises(ValidationError):
        process_uploaded_image(shell)


def test_catalogue_names_are_not_blank(db):
    """modeltranslation returned '' for 542 of 554 exercises through the ORM."""
    from routine.models import Exercise

    Exercise.objects.create(name="Barbell Squat")
    assert Exercise.objects.first().name == "Barbell Squat"


# ------------------------------------------------------------------ privacy layer
def test_personal_data_registry_covers_every_model_holding_user_data():
    """A model added later must not silently escape export, erasure and retention."""
    from training_platform import privacy
    assert privacy.audit_coverage() == []


def test_erasure_removes_personal_data_but_preserves_the_ledger(make_user):
    import decimal
    from social.models import Post
    from training_platform import privacy
    from wallet.models import Wallet

    u = make_user("gate_priv", specific_injury="lower back hernia")
    Post.objects.create(author=u, content="mine", post_type="text", visibility="public")
    w, _ = Wallet.objects.get_or_create(owner=u)
    w.balance = decimal.Decimal("250.00")
    w.save()

    privacy.erase_user_data(u)
    u.refresh_from_db()
    w.refresh_from_db()

    assert not Post.objects.filter(content="mine").exists()
    assert u.specific_injury == ""
    assert u.email.startswith("retired+")
    assert not u.is_active
    assert w.balance == decimal.Decimal("250.00"), "erasure must never destroy a balance"


def test_export_contains_the_users_own_data(make_user, api):
    u = make_user("gate_export", specific_injury="knee injury")
    resp = api(u).get("/api/privacy/export/")
    assert resp.status_code == 200
    assert "attachment" in resp.get("Content-Disposition", "")
    assert resp.get("Cache-Control") == "no-store"
    assert "knee injury" in resp.content.decode()


# ------------------------------------------------------------------- diet planner
@pytest.fixture
def diet_catalogue(db):
    """A small but nutritionally complete catalogue."""
    from diet.models import FoodCategory, FoodItem

    cat = FoodCategory.objects.create(name="gate")
    rows = [
        ("Chicken Breast", 165, 31, 0, 3.6), ("Salmon", 208, 20, 0, 13),
        ("Egg White", 52, 11, 0.7, 0.2), ("Greek Yogurt", 59, 10, 3.6, 0.4),
        ("White Rice", 130, 2.7, 28, 0.3), ("Sweet Potato", 86, 1.6, 20, 0.1),
        ("Oats", 389, 17, 66, 7), ("Lentils", 116, 9, 20, 0.4),
        ("Olive Oil", 884, 0, 0, 100), ("Almond Butter", 614, 21, 19, 56),
        ("Avocado", 160, 2, 9, 15), ("Broccoli", 34, 2.8, 7, 0.4),
        ("Spinach", 23, 2.9, 3.6, 0.4), ("Apple", 52, 0.3, 14, 0.2),
        ("Banana", 89, 1.1, 23, 0.3),
    ]
    # The macros above are for the food as eaten, so the row must say so. A cup is a
    # cup of what the row holds: sixty grams is a cup of dry rice and a third of a cup
    # of cooked rice, and naming a cooked staple as though it were dry gave it a
    # ceiling a third of a real serving. The production catalogue carries these
    # qualifiers; the fixture has to as well or it measures a different engine.
    catalogue_names = {
        "White Rice": "White Rice (Cooked)", "Lentils": "Lentils (Cooked)",
        "Sweet Potato": "Sweet Potato (Baked)", "Oats": "Oats (Rolled, Dry)",
    }
    made = {
        n: FoodItem.objects.create(
            name=catalogue_names.get(n, n), name_en=catalogue_names.get(n, n),
            category=cat, api_id=f"gate-{n}", calories=c,
            protein=p, carbs=cb, fat=f, serving_size="100g",
            allergens=[], allergen_source="verified",
        )
        for n, c, p, cb, f in rows
    }
    # Give them servings, as the real catalogue has. Without this the fixture's foods
    # declared no unit, so every test built on it exercised the planner's fallback
    # ladder rather than the path production takes — and a fixture that is not shaped
    # like production measures something that does not ship.
    from django.core.management import call_command

    call_command("seed_food_units", "--apply", verbosity=0)
    for food in made.values():
        food.refresh_from_db()
    return made


def test_food_classification_is_correct(diet_catalogue):
    """Misclassification hid oats' carbohydrate and left plans ~40% over on carbs."""
    from diet.planner.candidates import classify_food

    expected = {
        "Chicken Breast": "protein", "Salmon": "protein", "Greek Yogurt": "protein",
        "Egg White": "protein", "Oats": "carb", "White Rice": "carb", "Lentils": "carb",
        "Olive Oil": "fat", "Almond Butter": "fat", "Avocado": "fat",
        "Broccoli": "vegetable", "Spinach": "vegetable",
        "Apple": "fruit", "Banana": "fruit",
    }
    wrong = {n: classify_food(diet_catalogue[n]) for n, want in expected.items()
             if classify_food(diet_catalogue[n]) != want}
    assert not wrong, f"misclassified: {wrong}"


def test_a_user_with_no_preferences_still_gets_a_full_candidate_pool(make_user, diet_catalogue):
    """This exact case produced a 233 kcal plan (-90% of target), stored silently."""
    from diet.planner import build_pool, load_policy

    pool = build_pool(make_user("gate_nopref"), load_policy("maintain"))
    assert pool.empty_slots == [], f"empty slots: {pool.empty_slots}"


def test_allergens_never_reach_the_candidate_pool(make_user, diet_catalogue):
    from diet.models import UserFoodPreference
    from diet.planner import build_pool, load_policy
    from diet.services.meal_validator import AllergenChecker

    user = make_user("gate_allergy")
    UserFoodPreference.objects.create(user=user, allergies="fish")
    pool = build_pool(user, load_policy("maintain"), allergen_checker=AllergenChecker("fish"))
    names = {f.name for macros in pool.by_slot.values() for lst in macros.values() for f in lst}
    assert "Salmon" not in names
    assert "Chicken Breast" in names


def test_the_optimiser_converges_and_never_returns_a_worse_plan(diet_catalogue):
    """The old chain reached +4.1% then degraded to -6.6% and shipped the worse one."""
    from diet.planner import compute_targets, deviation_of, load_policy
    from diet.planner.optimize import optimize_meal, totals_of

    policy = load_policy("maintain")
    targets = compute_targets(2400, policy, ["Breakfast", "Lunch", "Dinner"], 1)
    lunch = next(m for m in targets.meals if m.name == "Lunch").as_dict()

    start = [(diet_catalogue["Chicken Breast"], 200.0), (diet_catalogue["White Rice"], 300.0),
             (diet_catalogue["Olive Oil"], 5.0), (diet_catalogue["Broccoli"], 150.0)]
    before = deviation_of(totals_of(start), lunch)
    result = optimize_meal(start, lunch, policy)

    assert result.deviation.magnitude <= before.magnitude
    assert result.deviation.within(policy.tolerance), result.deviation.human()


def test_planner_policy_uses_the_canonical_macro_ratios():
    """A duplicated ratio table made the planner and the optimiser disagree."""
    from diet.planner import load_policy
    from diet.utils.nutrition import get_macro_ratios

    for goal in ("lose", "maintain", "gain"):
        policy, canonical = load_policy(goal), get_macro_ratios(goal)
        assert policy.protein_ratio == canonical["protein"]
        assert policy.carb_ratio == canonical["carb"]
        assert policy.fat_ratio == canonical["fat"]


def test_food_weights_learn_from_what_was_actually_eaten(make_user, diet_catalogue):
    """smart_score_weight was declared adaptive and never written."""
    from diet.models import DietPlan, Meal, MealComponent
    from diet.planner.learning import update_weights
    from django.utils import timezone

    user = make_user("gate_learn")
    plan = DietPlan.objects.create(user=user, goal="Maintain", daily_calories=2000,
                                   start_date=timezone.localdate(), end_date=timezone.localdate())
    refused = diet_catalogue["Broccoli"]
    for i in range(3):
        meal = Meal.objects.create(diet_plan=plan, date=timezone.localdate(),
                                   meal_type="Lunch", scheduled_time=f"1{i}:00")
        MealComponent.objects.create(meal=meal, food=refused, quantity=200.0,
                                     actual_quantity_consumed=10.0, is_completed=False)

    changes = update_weights(user)
    assert refused.id in changes, "a consistently refused food must lose rank"
    assert changes[refused.id] < 1.0


def test_recipes_produce_named_dishes_within_tolerance(make_user, diet_catalogue):
    """A meal must be food, not a macro pile.

    Component assembly once produced "Extra Virgin Olive Oil 25 g" as a snack —
    exactly 200 calories and obviously not something anyone eats.
    """
    from django.core.management import call_command

    from diet.planner import compute_targets, find_recipe, load_policy

    call_command("seed_recipes", verbosity=0)
    policy = load_policy("maintain")
    targets = compute_targets(2200, policy, ["Breakfast", "Lunch", "Dinner"], 1)

    matched = 0
    for meal in targets.meals:
        match = find_recipe(meal.name, meal.as_dict(), policy)
        if match and match.deviation.within(policy.tolerance):
            matched += 1
            assert len(match.components) >= 2, f"{match.name} is not a dish"
            assert match.name.lower() not in ("breakfast", "lunch", "dinner", "snack")
    assert matched >= 3, f"only {matched} of {len(targets.meals)} meals matched a dish"


def test_a_recipe_containing_an_allergen_is_never_offered(make_user, diet_catalogue):
    from django.core.management import call_command

    from diet.planner import compute_targets, find_recipe, load_policy
    from diet.services.meal_validator import AllergenChecker

    call_command("seed_recipes", verbosity=0)
    policy = load_policy("maintain")
    dinner = next(m for m in compute_targets(2200, policy, ["Breakfast", "Lunch", "Dinner"], 1).meals
                  if m.name == "Dinner")

    match = find_recipe("Dinner", dinner.as_dict(), policy,
                        allergen_checker=AllergenChecker("fish"))
    if match is not None:
        names = " ".join(f.name.lower() for f, _g in match.components)
        assert "salmon" not in names and "fish" not in names


# ------------------------------------------------------------------------- wiring
def test_completing_a_workout_records_analytics(make_user, django_capture_on_commit_callbacks):
    """Analytics was read in 37 places and written by the server in none, so the
    achievement criteria computed from it could never be met.

    The recorder runs in `transaction.on_commit` so it can never fail a user's save;
    that also means the test must let the callbacks run — inside pytest's rollback
    transaction they otherwise never fire.
    """
    from django.utils import timezone

    from analytics.models import UserActivity
    from routine.models import Routine, WorkoutSession

    user = make_user("gate_analytics")
    trainer = make_user("gate_an_trainer", user_type="trainer")
    routine = Routine.objects.create(name="gate", created_by=trainer)

    with django_capture_on_commit_callbacks(execute=True):
        session = WorkoutSession.objects.create(user=user, routine=routine, status="in_progress")
        session.status = "completed"
        session.end_time = timezone.now()
        session.save()

    assert UserActivity.objects.filter(user=user, activity_type="routine_completed").count() == 1

    with django_capture_on_commit_callbacks(execute=True):
        session.save()
    assert UserActivity.objects.filter(user=user, activity_type="routine_completed").count() == 1, \
        "re-saving must not log the workout twice"


def test_analytics_failure_cannot_break_a_user_write(make_user):
    from unittest import mock

    from django.utils import timezone
    from routine.models import Routine, WorkoutSession

    user = make_user("gate_an_fail")
    trainer = make_user("gate_an_fail_t", user_type="trainer")
    routine = Routine.objects.create(name="gate2", created_by=trainer)

    with mock.patch("analytics.signals.record_activity", side_effect=RuntimeError("analytics down")):
        session = WorkoutSession.objects.create(user=user, routine=routine,
                                                status="completed", end_time=timezone.now())
    session.refresh_from_db()
    assert session.status == "completed"


def test_partial_achievement_progress_is_recorded(make_user):
    """AchievementProgress had a model, serializer, admin and view import, and nothing
    ever wrote a row — so a "3 of 5" screen was plumbed and always empty."""
    import uuid

    from achievements.engine import AchievementEngine
    from achievements.models import Achievement, AchievementProgress
    from analytics.models import UserActivity

    user = make_user("gate_progress")
    category = Achievement._meta.get_field("category").choices[0][0]
    achievement = Achievement.objects.create(
        key="gate_five_workouts", name="Five Workouts", description="d",
        category=category, criteria={"type": "workout_count", "target": 5, "condition": "gte"},
        points=10,
    )
    for _ in range(3):
        UserActivity.objects.create(user=user, activity_type="routine_completed",
                                    session_id=str(uuid.uuid4()), user_agent="gate")

    AchievementEngine.check_and_award(user, event_type="routine_completed")
    progress = AchievementProgress.objects.filter(user=user, achievement=achievement).first()
    assert progress is not None, "partial progress must be recorded"
    assert 0 < progress.progress_percentage < 100


def test_the_dead_letter_queue_can_be_drained():
    """NotificationFailure was written once and read by nothing."""
    from django.core.management import call_command

    call_command("retry_failed_notifications", dry_run=True, verbosity=0)


def test_removed_models_are_gone_and_privacy_coverage_is_still_complete():
    from django.apps import apps

    from training_platform import privacy

    for name in ("FeatureUsage", "PlatformMetric", "ErrorLog"):
        assert not any(m.__name__ == name for m in apps.get_models()), f"{name} still registered"
    assert privacy.audit_coverage() == []


def test_specific_injury_is_encrypted_at_rest(make_user):
    """P12-05 — free-text health data was readable in any database dump.

    Asserts against the raw column, not the ORM: the ORM decrypts, so reading through
    it would pass whether or not the encryption is actually wired up.
    """
    from django.db import connection

    secret = "ACL reconstruction, right knee"
    u = make_user("gate_enc", specific_injury=secret)

    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT specific_injury FROM users_customuser WHERE id = %s", [u.pk]
        )
        raw = cursor.fetchone()[0]

    assert secret not in (raw or ""), "health data is stored in plain text"
    assert raw.startswith("gAAAAA"), "column does not hold a Fernet token"

    u.refresh_from_db()
    assert u.specific_injury == secret, "decryption on read is broken"


def test_password_reset_token_is_never_stored_raw(make_user):
    """P12-01 — a database read used to be enough to take over any account."""
    from users.models import PasswordResetToken

    u = make_user("gate_prt")
    token, raw = PasswordResetToken.issue(u, minutes=15)

    assert raw not in token.token_hash
    assert len(token.token_hash) == 64
    assert PasswordResetToken.verify(raw).pk == token.pk

    # The stored hash must not itself be usable as a token — a database read is the
    # exact threat this fix exists to close.
    assert PasswordResetToken.verify(token.token_hash) is None
    assert PasswordResetToken.verify("not-a-real-token") is None
    assert PasswordResetToken.verify("") is None


def test_static_files_are_served_when_debug_is_off(settings):
    """Nothing served /static/ in production: staticfiles_urlpatterns() is dev-only,
    there is no CDN or NGINX in front on Fly, and WhiteNoise — which a comment claimed
    was doing the job — was in neither MIDDLEWARE nor STORAGES. The admin dashboard at
    /dj-admin/ loaded with no CSS or JS.
    """
    assert any("whitenoise" in m.lower() for m in settings.MIDDLEWARE), \
        "WhiteNoise middleware is missing — nothing will serve /static/ in production"

    # It has to come after SecurityMiddleware, or static responses bypass HTTPS
    # redirects and the security headers.
    names = [m.lower() for m in settings.MIDDLEWARE]
    sec = next(i for i, m in enumerate(names) if "securitymiddleware" in m)
    wn = next(i for i, m in enumerate(names) if "whitenoise" in m)
    assert wn > sec, "WhiteNoise must sit after SecurityMiddleware"

    backend = settings.STORAGES["staticfiles"]["BACKEND"]
    assert "whitenoise" in backend.lower(), f"staticfiles backend is {backend}"


def test_every_scheduled_task_exists_and_is_registered():
    """A beat entry naming a task that isn't registered fails silently at runtime —
    beat logs and moves on, and the job simply never runs. `generate-daily-advice` was
    assigned in celery.py after config_from_object(), which loads lazily, so settings
    overwrote it and the entry vanished from the effective schedule entirely.
    """
    from training_platform.celery import app

    schedule = app.conf.beat_schedule
    assert schedule, "no periodic tasks are scheduled at all"

    app.loader.import_default_modules()  # populate the registry the way the worker does
    registered = set(app.tasks.keys())

    missing = {
        name: entry["task"]
        for name, entry in schedule.items()
        if entry["task"] not in registered
    }
    assert not missing, f"scheduled tasks that are not registered: {missing}"

    # The jobs the platform genuinely depends on, by name, so deleting one is loud.
    for required in (
        "notifications.drain_dead_letter_queue",
        "training_platform.privacy.purge_expired_personal_data",
        "diet.tasks.generate_daily_advice",
    ):
        assert any(e["task"] == required for e in schedule.values()), \
            f"{required} is no longer scheduled"


@pytest.mark.parametrize("path,method,headers,expected_code,expected_status", [
    ("/api/routine/routines/",        "get",  {},                                          "not_authenticated",  401),
    ("/api/routine/routines/",        "get",  {"HTTP_AUTHORIZATION": "Bearer garbage"},    "token_not_valid",    401),
    ("/api/routine/routines/999999/", "get",  "AUTH",                                      "not_found",          404),
    ("/api/auth/health/",             "delete", {},                                        "method_not_allowed", 405),
])
def test_every_error_carries_a_stable_machine_readable_code(
    make_user, api, path, method, headers, expected_code, expected_status
):
    """The API serves English and Arabic. A client branching on translated message text
    works in English and silently stops working in Arabic, so `code` — which is never
    translated — is the only thing the app can switch on. Validation errors, the most
    common failure, previously had no envelope and no code at all.
    """
    from django.test import Client

    if headers == "AUTH":
        client, kwargs = api(make_user("gate_err")), {}
    else:
        client, kwargs = Client(), headers

    resp = getattr(client, method)(path, **kwargs)
    body = resp.json()

    assert resp.status_code == expected_status
    assert body.get("code") == expected_code, f"got {body.get('code')!r}"
    assert "detail" in body and "error" in body


def test_validation_errors_expose_per_field_codes(api, make_user):
    """A 400 used to arrive as a bare {field: [message]} with nothing to branch on and
    no top-level message the app could show its user."""
    from django.test import Client

    resp = Client().post(
        "/api/auth/login/", {"email": "not-an-email"}, content_type="application/json"
    )
    body = resp.json()

    assert resp.status_code == 400
    assert body["code"] == "validation_error"
    assert body["detail"], "no human-readable message for the client to display"
    assert body["field_errors"]["password"][0]["code"] == "required"
    assert body["field_errors"]["email"][0]["code"] == "invalid"


def test_no_list_endpoint_returns_a_bare_array(api, make_user):
    """Every list must use the {count, next, previous, results} envelope. The wallet
    ledger returned a bare array hard-sliced at 200 rows, so a user with more history
    than that could never reach it and nothing said the list was truncated.
    """
    client = api(make_user("gate_shape"))
    for path in ("/api/wallet/transactions/", "/api/routine/routines/"):
        resp = client.get(path)
        assert resp.status_code == 200, f"{path} -> {resp.status_code}"
        body = resp.json()
        assert isinstance(body, dict), f"{path} returned a bare {type(body).__name__}"
        assert {"count", "next", "previous", "results"} <= set(body), \
            f"{path} is missing the pagination envelope: {sorted(body)}"


def test_push_data_type_always_equals_the_event_type():
    """The mobile app routes a tap on a notification by `data.type`. Four social
    notifications sent a short alias (`like`, `comment`, `follow`, `achievement`)
    instead of the event_type, so the app needed a second vocabulary that no endpoint
    published — `GET /api/notifications/event-types/` lists the registry keys only.
    """
    import pathlib
    import re

    from notifications.channels.fcm import EVENT_CLASS_REGISTRY

    valid = set(EVENT_CLASS_REGISTRY)
    offenders = {}
    for f in pathlib.Path("notifications/listeners").glob("*.py"):
        for t in re.findall(r"'data':\s*\{'type':\s*'([a-z_]+)'", f.read_text()):
            if t not in valid:
                offenders.setdefault(f.name, []).append(t)

    assert not offenders, f"push data.type values that are not event types: {offenders}"


def test_registration_through_otp_verification_actually_works(db):
    """The whole signup path, end to end.

    `otp_code` was `varchar(6)` — the width of the plaintext code — long after the
    column started holding a 64-character hash, so every insert failed with
    "value too long for type character varying(6)" and no one could register at all.
    Nothing in the suite exercised this path, so it passed a full audit unnoticed.
    SQLite ignores CharField width, which is why it only breaks on real Postgres.
    """
    from django.contrib.auth import get_user_model
    from users.models import OTPVerification
    from users.utils import create_otp, verify_otp

    User = get_user_model()
    user = User.objects.create_user(
        username="gate_signup", email="gate_signup@example.com", password="Xx!23456aA"
    )

    create_otp(user)  # used to raise DataError

    row = OTPVerification.objects.filter(user=user).order_by("-id").first()
    assert row is not None, "no OTP row was written"
    assert len(row.otp_code) == 64, f"stored value is {len(row.otp_code)} chars, not a hash"
    assert not row.otp_code.isdigit(), "the plaintext code is in the database"


def test_page_size_query_param_is_honoured_and_capped():
    """?page_size was silently ignored on every endpoint using the default paginator —
    the client asked for a different page size and got 25 rows with no error."""
    from training_platform.pagination import StandardPagination

    assert StandardPagination.page_size_query_param == "page_size"
    assert StandardPagination.max_page_size == 100, "an uncapped page_size serialises whole tables"

    from django.conf import settings

    assert settings.REST_FRAMEWORK["DEFAULT_PAGINATION_CLASS"].endswith("StandardPagination")


def test_login_returns_a_credential_that_actually_authenticates(db):
    """`/api/auth/login/` answered with dj-rest-auth's {"key": "<DRF Token>"} while
    DEFAULT_AUTHENTICATION_CLASSES contained JWTAuthentication only. The key was
    accepted by nothing — every following request was 401 — so the primary entry
    point into the app handed out a dead credential.
    """
    from django.contrib.auth import get_user_model
    from django.test import Client

    User = get_user_model()
    user = User.objects.create_user(
        username="gate_login", email="gate_login@example.com", password="Str0ng!Pass1"
    )
    user.is_active = True
    user.save()

    c = Client()
    resp = c.post(
        "/api/auth/login/",
        {"email": "gate_login@example.com", "password": "Str0ng!Pass1"},
        content_type="application/json",
    )
    assert resp.status_code == 200, resp.content
    body = resp.json()
    assert {"access", "refresh"} <= set(body), f"login returned {sorted(body)}"

    # The credential must work, and so must the legacy `key` field.
    for field in ("access", "key"):
        probe = c.get("/api/routine/routines/", HTTP_AUTHORIZATION=f"Bearer {body[field]}")
        assert probe.status_code == 200, f"login's `{field}` does not authenticate ({probe.status_code})"

    # Refresh must rotate — the app has to store the new one.
    rotated = c.post(
        "/api/auth/token/refresh/", {"refresh": body["refresh"]}, content_type="application/json"
    )
    assert rotated.status_code == 200
    assert rotated.json().get("refresh") != body["refresh"], "ROTATE_REFRESH_TOKENS is not in effect"


# ------------------------------------------------- previously unemitted notifications

def test_session_reminder_nudges_only_the_right_people(make_user):
    """`session_reminder` was registered and templated but nothing ever emitted it —
    the platform had no re-engagement loop at all. It must reach a client who has
    drifted, and leave alone anyone who trained today or who left months ago.
    """
    from datetime import timedelta
    from zoneinfo import ZoneInfo

    from django.utils import timezone

    from notifications.models import Notification
    from notifications.tasks import send_workout_reminders
    from routine.models import Routine, WorkoutSession

    trainer = make_user("gate_rem_t", user_type="trainer")
    # An expired window, so this exercises the drift branch specifically. The scheduled
    # branch is covered by its own assertions below.
    routine = Routine.objects.create(
        name="Gate Reminder", created_by=trainer,
        start_date=timezone.localdate() - timedelta(days=60),
        end_date=timezone.localdate() - timedelta(days=30),
    )

    # The task sends only to users whose LOCAL hour matches their workout_reminder_hour,
    # so every fixture here is pinned to the hour this test runs in.
    tz = ZoneInfo("Asia/Damascus")
    local_hour = timezone.now().astimezone(tz).hour

    def client_last_trained(name, days_ago):
        u = make_user(name, user_type="client")
        u.preferred_timezone = "Asia/Damascus"
        u.workout_reminder_hour = local_hour
        u.save()
        routine.assigned_to.add(u)
        s = WorkoutSession.objects.create(user=u, routine=routine, status="completed")
        # start_time is auto_now_add, so it is ignored on create(); .update() writes it.
        WorkoutSession.objects.filter(pk=s.pk).update(
            start_time=timezone.now() - timedelta(days=days_ago)
        )
        return u

    drifting = client_last_trained("gate_rem_drift", 3)
    today = client_last_trained("gate_rem_today", 0)
    gone = client_last_trained("gate_rem_gone", 40)

    send_workout_reminders()

    def nudged(u):
        return Notification.objects.filter(recipient=u, event_type="session_reminder").exists()

    from routine.models import RoutineProgress
    assert nudged(drifting), "a client who has drifted 3 days got no reminder"
    assert not nudged(today), "a client who trained today was nudged anyway"
    assert not nudged(gone), "a client gone 40 days is being chased indefinitely"

    # Running twice in one day must not send twice.
    before = Notification.objects.filter(event_type="session_reminder").count()
    send_workout_reminders()
    assert Notification.objects.filter(event_type="session_reminder").count() == before


def test_progress_milestones_fire_once_each(make_user):
    """`progress_milestone` had a template and no producer — users crossed every
    threshold in silence. Each milestone must fire exactly once, ever."""
    from django.utils import timezone

    from notifications import milestones
    from notifications.models import Notification
    from routine.models import Routine, WorkoutSession

    trainer = make_user("gate_ms_t", user_type="trainer")
    routine = Routine.objects.create(name="Gate Milestone", created_by=trainer)
    user = make_user("gate_ms_c", user_type="client")

    WorkoutSession.objects.create(user=user, routine=routine, status="completed")
    assert milestones.award(user) >= 1

    reached = set(
        Notification.objects.filter(recipient=user, event_type="progress_milestone")
        .values_list("related_object_id", flat=True)
    )
    assert "sessions-1" in reached, f"first workout produced {reached}"

    # Re-running must be a no-op — the dedup key is what guarantees this.
    assert milestones.award(user) == 0, "a milestone was awarded twice"

    for _ in range(9):
        WorkoutSession.objects.create(user=user, routine=routine, status="completed")
    milestones.award(user)
    reached = set(
        Notification.objects.filter(recipient=user, event_type="progress_milestone")
        .values_list("related_object_id", flat=True)
    )
    assert "sessions-10" in reached, f"10-session threshold missed: {reached}"


def test_trainer_can_message_only_their_own_approved_clients(make_user, api):
    """`custom` existed as a template with no endpoint — a trainer could assign
    routines but could not say a word to the person following them. This is the only
    place one user writes text onto another user's lock screen, so the boundary is the
    whole point.
    """
    from notifications.models import Notification
    from users.models import TrainerClientRelation

    trainer = make_user("gate_msg_t", user_type="trainer")
    other_trainer = make_user("gate_msg_t2", user_type="trainer")
    client = make_user("gate_msg_c", user_type="client")
    TrainerClientRelation.objects.create(trainer=trainer, client=client, status="approved")

    url = "/api/notifications/message-client/"
    body = {"client_id": client.pk, "message": "Rest day tomorrow, focus on mobility."}

    resp = api(trainer).post(url, body, content_type="application/json")
    assert resp.status_code == 201, resp.content
    note = Notification.objects.filter(recipient=client, event_type="custom").first()
    assert note is not None and note.actor_id == trainer.pk

    # An unrelated trainer gets the same answer as a nonexistent client, so this
    # cannot be used to enumerate user ids.
    assert api(other_trainer).post(url, body, content_type="application/json").status_code == 404
    # A client cannot message anyone.
    assert api(client).post(url, body, content_type="application/json").status_code == 403
    # An empty message is not a notification.
    assert api(trainer).post(
        url, {"client_id": client.pk, "message": "   "}, content_type="application/json"
    ).status_code == 400


def test_every_registered_event_type_is_actually_emitted():
    """Three event types sat in the registry with templates and no producer. Anything
    listed by GET /api/notifications/preferences/event_types/ is something a user can
    toggle — offering a switch for a notification that can never arrive is a lie in the
    settings screen.
    """
    import pathlib
    import re

    from notifications.channels.fcm import EVENT_CLASS_REGISTRY

    blob = ""
    for f in pathlib.Path(".").rglob("*.py"):
        s = str(f)
        if any(x in s for x in ("_excluded", ".venv", "migrations", "tests/", "test_")):
            continue
        try:
            blob += f.read_text() + "\n"
        except OSError:
            continue

    emitted = set()
    for pattern in (
        r"notif_type\s*=\s*['\"]([a-z_]+)['\"]",
        r"event_type\s*=\s*['\"]([a-z_]+)['\"]",
        r"'data':\s*\{'type':\s*'([a-z_]+)'",
    ):
        emitted |= set(re.findall(pattern, blob))

    orphans = sorted(set(EVENT_CLASS_REGISTRY) - emitted)
    assert not orphans, f"event types offered to users but never emitted: {orphans}"


def test_a_duplicate_notification_does_not_roll_back_the_callers_work(make_user):
    """Deduplication is a normal outcome, not an error, and must cost the caller nothing.

    `Notification.objects.create()` ran without its own savepoint, so the IntegrityError
    from a duplicate poisoned the entire surrounding transaction. Any view under
    @transaction.atomic that sends a notification would then fail with "You can't
    execute queries until the end of the 'atomic' block" — meaning a completed workout
    or an assigned routine would roll back because the user had already been told
    about it.
    """
    from django.db import transaction

    from notifications.services import NotificationService
    from routine.models import Routine

    user = make_user("gate_dedup", user_type="trainer")
    payload = {"context": {"message": "hi"}, "data": {"type": "custom"}}

    with transaction.atomic():
        first = NotificationService.create_and_send(
            recipient=user, event_type="custom", related_object_id="dup-1", metadata=payload
        )
        assert first is not None

        # Same dedup key — the DB constraint fires.
        NotificationService.create_and_send(
            recipient=user, event_type="custom", related_object_id="dup-1", metadata=payload
        )

        # The caller's transaction must still be usable afterwards.
        Routine.objects.create(name="survives the duplicate", created_by=user)

    assert Routine.objects.filter(name="survives the duplicate").exists(), \
        "the caller's work was rolled back by a suppressed duplicate"


def test_duplicate_detection_is_not_written_against_sqlite(db):
    """The registration handler matched "UNIQUE constraint failed: users_customuser.email"
    — SQLite's wording. Postgres says `duplicate key value violates unique constraint
    "users_customuser_email_..."`, so the branch never fired in production and a
    duplicate that slipped past field validation (two simultaneous signups) surfaced as
    a 500 instead of the 400 the handler exists to produce.
    """
    import uuid

    from django.contrib.auth import get_user_model
    from django.db import IntegrityError, transaction

    User = get_user_model()
    email = f"dupe{uuid.uuid4().hex[:8]}@example.com"
    User.objects.create_user(username=f"seed{uuid.uuid4().hex[:6]}", email=email)

    with pytest.raises(IntegrityError) as exc:
        with transaction.atomic():
            User.objects.create_user(username=f"other{uuid.uuid4().hex[:6]}", email=email)

    # Whatever the backend, the field name must be recoverable from the message —
    # that is what the handler now branches on.
    assert "email" in str(exc.value).lower()


def test_a_loop_that_continues_after_a_failed_write_uses_a_savepoint(db):
    """Two routine endpoints record a failed write into an `errors` list and keep
    looping. Correct under autocommit, which is how they run today — but the first
    IntegrityError aborts the whole transaction under any enclosing atomic(), and every
    later iteration then dies with TransactionManagementError instead of being
    recorded. A savepoint makes them correct either way.
    """
    import uuid

    from django.contrib.auth import get_user_model
    from django.db import IntegrityError, transaction

    User = get_user_model()
    seed = f"sp{uuid.uuid4().hex[:8]}@example.com"
    User.objects.create_user(username=f"sp{uuid.uuid4().hex[:6]}", email=seed)

    succeeded = 0
    with transaction.atomic():          # the enclosing transaction that used to break it
        for i in range(3):
            try:
                with transaction.atomic():   # the savepoint the two loops now use
                    User.objects.create_user(
                        username=f"n{uuid.uuid4().hex[:6]}",
                        email=seed if i == 0 else f"ok{uuid.uuid4().hex[:8]}@example.com",
                    )
                succeeded += 1
            except IntegrityError:
                pass

    assert succeeded == 2, "iterations after the failed write did not survive"


def test_no_unguarded_db_write_recovers_inside_a_loop():
    """Guards the pattern itself, not just the three places it was found.

    A `try: <write> except IntegrityError: <record and continue>` with no savepoint is
    invisible under autocommit and breaks the moment anything wraps the caller in a
    transaction — which is exactly how NotificationService started rolling back
    completed workouts.
    """
    import ast
    import pathlib

    DB_ERRORS = {"IntegrityError", "DatabaseError", "DataError", "InternalError"}
    WRITES = (".create(", ".save(", ".get_or_create(", ".update_or_create(", ".bulk_create(")

    offenders = []
    for f in pathlib.Path(".").rglob("*.py"):
        s = str(f)
        if any(x in s for x in ("_excluded", ".venv", "migrations", "tests/", "test_")):
            continue
        try:
            src = f.read_text()
            tree = ast.parse(src)
        except (OSError, SyntaxError):
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Try):
                continue
            caught = {
                n.id
                for h in node.handlers if h.type
                for n in ast.walk(h.type) if isinstance(n, ast.Name)
            }
            if not (caught & DB_ERRORS):
                continue
            body = "\n".join(ast.get_source_segment(src, st) or "" for st in node.body)
            if not any(w in body for w in WRITES):
                continue
            if "transaction.atomic(" in body or "atomic()" in body:
                continue
            # Re-raising ends the request, so the poisoned transaction never matters.
            handlers = "\n".join(
                ast.get_source_segment(src, st) or "" for h in node.handlers for st in h.body
            )
            if "raise" in handlers:
                continue
            offenders.append(f"{s}:{node.lineno}")

    assert not offenders, (
        "DB writes that swallow an integrity error and continue, with no savepoint: "
        f"{offenders}"
    )


def test_reminders_fire_in_each_users_own_evening(make_user):
    """`preferred_timezone` was declared with the help text "for localized dates and
    times" and read by NOTHING. A single daily sweep at a fixed UTC hour reaches a user
    in Damascus and a user in Berlin at different points in their day; the reminder now
    runs hourly and sends only to users whose own local hour has come.
    """
    from datetime import timedelta
    from zoneinfo import ZoneInfo

    from django.utils import timezone

    from notifications.models import Notification
    from notifications.tasks import send_workout_reminders
    from routine.models import Routine, RoutineProgress

    trainer = make_user("gate_tz_t", user_type="trainer")
    routine = Routine.objects.create(
        name="Gate TZ", created_by=trainer, days=3,
        start_date=timezone.localdate() - timedelta(days=1),
    )

    now = timezone.now()

    def client(name, tzname, hour):
        u = make_user(name, user_type="client")
        u.preferred_timezone = tzname
        u.workout_reminder_hour = hour
        u.save()
        routine.assigned_to.add(u)
        RoutineProgress.objects.get_or_create(
            user=u, routine=routine, day=1, date=routine.start_date,
            defaults={"status": "not_started"},
        )
        return u

    dam_hour = now.astimezone(ZoneInfo("Asia/Damascus")).hour
    ber_hour = now.astimezone(ZoneInfo("Europe/Berlin")).hour

    due = client("gate_tz_due", "Asia/Damascus", dam_hour)
    later = client("gate_tz_later", "Asia/Damascus", (dam_hour + 5) % 24)
    berlin = client("gate_tz_berlin", "Europe/Berlin", ber_hour)
    broken = client("gate_tz_broken", "Not/AZone", now.astimezone(ZoneInfo("UTC")).hour)

    send_workout_reminders()

    def got(u):
        return Notification.objects.filter(recipient=u, event_type="session_reminder").exists()

    assert got(due), "the user whose local hour it is got nothing"
    assert not got(later), "a user 5 hours from their reminder time was messaged anyway"
    assert got(berlin), "preferred_timezone is being ignored for non-default zones"
    assert got(broken), "an unparseable timezone silences the user instead of falling back"

    note = Notification.objects.filter(recipient=due, event_type="session_reminder").first()
    assert note.metadata["data"]["reason"] == "scheduled"
    assert routine.name in note.metadata["context"]["message"], \
        "a scheduled reminder should name the routine, not just count days"


def test_no_account_is_left_on_the_frozen_timezone_default(db):
    """`preferred_timezone` used `default=getattr(settings, 'TIME_ZONE', ...)`, which
    Django evaluates once at import — so the column default froze to 'UTC' and stopped
    tracking the setting, leaving 376 of 378 accounts on the wrong clock. That is the
    field the reminder resolves each user's local hour through, so those users would
    have been reminded three hours early.

    Asserts the model default is now dynamic; the existing rows were corrected by
    migration users/0029.
    """
    from zoneinfo import ZoneInfo

    from django.conf import settings
    from django.contrib.auth import get_user_model

    User = get_user_model()
    field = User._meta.get_field("preferred_timezone")

    assert callable(field.default), "the default is frozen at import time again"
    assert field.default() == settings.TIME_ZONE

    fresh = User.objects.create_user(username="gate_tzdefault", email="gate_tzd@example.com")
    assert fresh.preferred_timezone == settings.TIME_ZONE

    # Whatever is stored must be resolvable, or the reminder silently falls back to UTC.
    ZoneInfo(fresh.preferred_timezone)


# --------------------------------------------------------------- money is server-side
@pytest.fixture
def paid_plan(db):
    """A plan with a real price, in the currency the platform actually charges in."""
    from subscription.models import SubscriptionPlan

    return SubscriptionPlan.objects.create(
        name="gate_paid_plan", plan_type="premium", description="gate",
        price="5000.00", currency="SYP", duration_days=30, is_active=True,
        has_diet_access=True, has_routine_access=True, has_challenges_access=True,
        has_ai_advice=True, has_priority_support=True,
    )


def test_a_plan_carries_its_own_currency(paid_plan):
    """A price without a currency is not a price.

    Plans were priced in SYP while three separate call sites stamped their payments
    'USD', so the ledger was denominated in a currency the platform never charged in
    and every renewal failed the gateway's currency check.
    """
    from subscription.models import Payment, SubscriptionPlan

    assert SubscriptionPlan._meta.get_field("currency"), "plans must declare a currency"
    assert not SubscriptionPlan.objects.exclude(
        currency__in=[c for c, _ in Payment._meta.get_field("currency").choices]
    ).exists(), "a plan is priced in a currency payments cannot express"


def test_the_client_cannot_choose_what_it_pays(make_user, api, paid_plan):
    """`amount` used to be read straight from the request body.

    Every later check compares the gateway's report against `payment.amount`, so the
    whole verification chain agreed with a number the payer had picked: a 5000.00 plan
    activated in full for the 100 SYP gateway floor.
    """
    from django.utils import timezone
    from datetime import timedelta

    from subscription.models import Payment, Subscription

    user = make_user("gate_payer")
    sub = Subscription.objects.create(
        user=user, plan=paid_plan, status="pending",
        end_date=timezone.now() + timedelta(days=30),
    )
    client = api(user)

    # An amount that disagrees with the plan is refused outright, never charged.
    resp = client.post(
        "/api/subscription/v1/gateways/",
        data=json.dumps({"gateway": "shamcash", "subscription_id": str(sub.id),
                         "amount": "100.00", "currency": "SYP"}),
        content_type="application/json",
    )
    assert resp.status_code == 409, f"underpricing was accepted: {resp.status_code}"

    # Omitting it entirely is the correct call, and the server sets the price itself.
    resp = client.post(
        "/api/subscription/v1/gateways/",
        data=json.dumps({"gateway": "shamcash", "subscription_id": str(sub.id)}),
        content_type="application/json",
    )
    assert resp.status_code == 200, resp.content
    payment = Payment.objects.get(id=resp.json()["payment_id"])
    assert payment.amount == paid_plan.price
    assert payment.currency == paid_plan.currency


def test_every_payment_row_inherits_the_plans_currency(make_user, api, paid_plan):
    """No call site may invent a currency of its own."""
    from django.utils import timezone
    from datetime import timedelta

    from subscription.models import Payment, Subscription
    from subscription.services.payment_service import PaymentService

    user = make_user("gate_currency")
    sub = Subscription.objects.create(
        user=user, plan=paid_plan, status="active",
        end_date=timezone.now() + timedelta(days=30),
    )
    PaymentService.start_renewal(sub)
    assert set(Payment.objects.filter(subscription=sub).values_list("currency", flat=True)) == {
        paid_plan.currency
    }


def test_a_webhook_signature_header_is_found_whatever_its_case(settings):
    """HTTP header names are case-insensitive; a plain dict is not.

    The view passed `dict(request.headers)`, Django title-cases every segment, and the
    gateway looked for 'X-ShamCash-Signature' with a capital C. No spelling a client
    could send would ever match, so 100% of webhooks were rejected as unsigned and no
    payment could be confirmed by webhook at all.
    """
    from subscription.gateways.shamcash import ShamCashGateway

    gateway = ShamCashGateway({"webhook_secret": "x" * 20})
    for spelling in ("X-ShamCash-Signature", "x-shamcash-signature",
                     "X-SHAMCASH-SIGNATURE", "X-Shamcash-Signature"):
        assert gateway.header({spelling: "sig"}, "X-ShamCash-Signature") == "sig", spelling


# ------------------------------------------------------------------ error contract
def test_an_object_level_error_reaches_the_client_as_a_sentence(db):
    """A wrong password is the most common failure the API has.

    `non_field_errors` was excluded from field_errors, which skipped the branch that
    builds a human `detail`, so the app was handed the repr of a Python dict to show
    its user: "{'non_field_errors': [ErrorDetail(string='Unable to log in...')]}".
    """
    from django.test import Client

    resp = Client().post(
        "/api/auth/login/",
        {"email": "nobody@gate.test", "password": "wrong-password"},
        content_type="application/json",
    )
    body = resp.json()
    assert resp.status_code == 400
    assert "ErrorDetail" not in body["detail"], body["detail"]
    assert "non_field_errors" not in body["detail"], body["detail"]
    assert body["code"] == "validation_error"
    assert body["field_errors"]["non_field_errors"][0]["message"]


def test_control_flow_exceptions_are_never_flattened_into_a_500():
    """Views wrap their work in `except Exception -> 500`, so anything carrying its own
    status has to be re-raised first. The hand-written tuple that did this named five
    classes, missed four more, and in two files named a symbol nobody had imported —
    so evaluating the clause raised NameError and destroyed the original exception.
    """
    from rest_framework import exceptions

    from training_platform.api_exceptions import PASSTHROUGH_EXCEPTIONS

    for cls in (exceptions.NotFound, exceptions.PermissionDenied,
                exceptions.NotAuthenticated, exceptions.ValidationError,
                exceptions.MethodNotAllowed, exceptions.UnsupportedMediaType,
                exceptions.NotAcceptable, exceptions.Throttled):
        assert issubclass(cls, PASSTHROUGH_EXCEPTIONS), cls.__name__


def test_no_view_hand_writes_the_passthrough_tuple():
    """Guards the pattern, not just the two files it was broken in."""
    import pathlib

    offenders = []
    for path in pathlib.Path(".").glob("*/views.py"):
        if "except (Http404, NotFound" in path.read_text():
            offenders.append(str(path))
    assert not offenders, f"hand-written exception tuples returned: {offenders}"


# ---------------------------------------------------------------------- websockets
def test_a_websocket_refuses_a_refresh_token(make_user):
    """Both consumers used `UntypedToken`, whose purpose is to NOT check the token type.

    So a refresh token opened a socket, and since UntypedToken has no BlacklistMixin it
    never consulted the blacklist: logging out closed nothing, and the 7-day refresh
    token the logout endpoint had just revoked still opened the AI socket, which spends
    model tokens per message.
    """
    from rest_framework_simplejwt.tokens import RefreshToken

    from training_platform.ws_auth import authenticate_scope

    user = make_user("gate_ws")
    refresh = RefreshToken.for_user(user)

    def scope(token):
        return {"headers": [], "query_string": f"token={token}".encode()}

    assert authenticate_scope(scope(str(refresh.access_token))) == user
    assert authenticate_scope(scope(str(refresh))) is None, "a refresh token opened a socket"

    refresh.blacklist()
    assert authenticate_scope(scope(str(refresh))) is None


def test_the_ai_socket_and_the_http_route_agree_on_entitlement(make_user, paid_plan):
    """They disagreed. The HTTP guard goes through Subscription.is_active, which checks
    end_date; the socket used a bare filter on `status`, and nothing sweeps lapsed rows
    out of 'active' — 64 of 72 rows in the database were already past their end_date.
    """
    from datetime import timedelta

    from django.utils import timezone

    from subscription.models import Subscription

    user = make_user("gate_lapsed")
    sub = Subscription.objects.create(
        user=user, plan=paid_plan, status="active",
        end_date=timezone.now() + timedelta(days=1),
    )
    Subscription.objects.filter(pk=sub.pk).update(
        end_date=timezone.now() - timedelta(days=365))
    sub.refresh_from_db()

    assert sub.status == "active"
    assert sub.is_active is False, "is_active must consider end_date"
    assert sub.has_ai_advice is True, "the flag is still set; only the window has closed"


# ------------------------------------------------------------------------ retention
def test_every_privacy_source_matches_its_model():
    """A declaration nobody verifies rots exactly like a scheduled task nobody registers.

    `sessions` declared retention_field="session_start"; the column is `started_at`.
    purge_expired() caught the FieldError, logged it and moved on, so retention reported
    success while the rows holding IP address and user agent were never purged at all.
    """
    from training_platform import privacy

    assert privacy.validate_sources() == []


def test_lapsed_subscriptions_have_a_scheduled_sweep():
    """`expire_subscriptions()` carried the docstring "This should be run as a scheduled
    task" and never was: not a Celery task, no beat entry, no management command, and
    every other function in that module was uncalled too.
    """
    from training_platform.celery import app

    app.loader.import_default_modules()
    assert "subscription.expire_lapsed_subscriptions" in app.tasks
    assert any(e["task"] == "subscription.expire_lapsed_subscriptions"
               for e in app.conf.beat_schedule.values())


def test_expiring_subscriptions_twice_changes_nothing_the_second_time(make_user, paid_plan):
    """Celery delivers at least once, so every task runs twice sooner or later."""
    from datetime import timedelta

    from django.utils import timezone

    from subscription.models import Subscription
    from subscription.tasks import expire_lapsed_subscriptions

    user = make_user("gate_expiry")
    sub = Subscription.objects.create(
        user=user, plan=paid_plan, status="active",
        end_date=timezone.now() + timedelta(days=1),
    )
    Subscription.objects.filter(pk=sub.pk).update(
        end_date=timezone.now() - timedelta(days=2))

    assert expire_lapsed_subscriptions() >= 1
    sub.refresh_from_db()
    assert sub.status == "expired"
    assert expire_lapsed_subscriptions() == 0


# ------------------------------------------------- rules that must not rot with time
def test_reassigning_a_client_does_not_freeze_their_old_routines(make_user):
    """Routine.clean() compared each assigned client's *current* trainer to the
    routine's creator. Moving a client to another trainer therefore invalidated every
    routine the first trainer had ever written for them: 100 rows in the development
    database could no longer be saved at all. Who may be assigned is settled where the
    assignment is made, not re-litigated on every write of a years-old row."""
    from routine.models import Routine

    first = make_user("rot_tr_a", user_type="trainer")
    second = make_user("rot_tr_b", user_type="trainer")
    client = make_user("rot_client", assigned_trainer=first)

    routine = Routine.objects.create(name="rot legacy", created_by=first, days=3)
    routine.assigned_to.add(client)

    client.assigned_trainer = second
    client.save()

    routine.name = "renamed after the move"
    routine.save()
    routine.refresh_from_db()
    assert routine.name == "renamed after the move"


def test_retiring_an_exercise_does_not_freeze_routines_that_use_it(make_user):
    """RoutineExercise.clean() asked whether the exercise was *still* accessible to
    the routine's creator, so deactivating one made every row referencing it
    unsaveable — 878 of 2201 rows. Accessibility is checked when the exercise is
    added, by the serializer, where the actor is known."""
    from routine.models import Exercise, Routine, RoutineExercise

    trainer = make_user("rot_ex_tr", user_type="trainer")
    exercise = Exercise.objects.create(name="rot squat", created_by=trainer)
    routine = Routine.objects.create(name="rot routine", created_by=trainer, days=3)
    link = RoutineExercise.objects.create(
        routine=routine, exercise=exercise, day=1, order=1, sets=3, reps=10)

    exercise.is_active = False
    exercise.save()

    link.reps = 12
    link.save()
    link.refresh_from_db()
    assert link.reps == 12


def test_shortening_a_routine_does_not_freeze_the_days_already_written(make_user):
    from routine.models import Exercise, Routine, RoutineExercise

    trainer = make_user("rot_days_tr", user_type="trainer")
    exercise = Exercise.objects.create(name="rot press", created_by=trainer)
    routine = Routine.objects.create(name="rot days", created_by=trainer, days=5)
    link = RoutineExercise.objects.create(
        routine=routine, exercise=exercise, day=4, order=1, sets=3, reps=10)

    routine.days = 2
    routine.save()

    link.order = 2
    link.save()
    assert RoutineExercise.objects.get(pk=link.pk).order == 2


# --------------------------------------------------------- exercise visibility
def test_an_exercise_is_global_exactly_when_it_has_no_owner(make_user):
    """`is_global` and `created_by IS NULL` were both used as the definition of
    "global", in different code paths, and disagreed about the same row."""
    from django.db import IntegrityError, transaction
    from routine.models import Exercise

    trainer = make_user("vis_tr", user_type="trainer")

    catalogue = Exercise.objects.create(name="vis catalogue")
    assert catalogue.is_global is True

    owned = Exercise.objects.create(name="vis owned", created_by=trainer, is_global=True)
    assert owned.is_global is False, "an owned exercise is not global, whatever was passed"

    with pytest.raises(IntegrityError), transaction.atomic():
        Exercise.objects.filter(pk=owned.pk).update(is_global=True)


# ------------------------------------------------------------------ the client's goal
def test_a_generated_plan_uses_the_clients_goal_not_maintenance(make_user):
    """`fitness_goal` was read in five places across the diet app and was a field on
    no model, so every read returned its 'Maintain' fallback. Generated plans were
    labelled Maintain and — because calculate_daily_calories() defaulted the same way —
    given a maintenance calorie target, whatever the client had asked for."""
    losing = make_user("goal_lose", client_goals=["Weight Loss"], height=180,
                       weight=90, age=30, gender="Male", activity_level="Moderate")
    gaining = make_user("goal_gain", client_goals=["Muscle Gain"], height=180,
                        weight=70, age=30, gender="Male", activity_level="Moderate")
    unsure = make_user("goal_both", client_goals=["Weight Loss", "Muscle Gain"],
                       height=180, weight=80, age=30, gender="Male",
                       activity_level="Moderate")

    assert losing.resolve_fitness_goal() == "Lose"
    assert gaining.resolve_fitness_goal() == "Gain"
    assert unsure.resolve_fitness_goal() == "Maintain", "do not guess a deficit"

    assert losing.calculate_daily_calories() < losing.calculate_daily_calories("Maintain")
    assert gaining.calculate_daily_calories() > gaining.calculate_daily_calories("Maintain")


def test_a_goal_survives_the_round_trip_through_the_planner():
    """The planner package works in lower case and the column stores title case, so a
    goal that made the trip came back rejected by its own model."""
    from diet.models import DietPlan

    assert DietPlan.normalise_goal("maintain") == "Maintain"
    assert DietPlan.normalise_goal("LOSE") == "Lose"
    assert DietPlan.normalise_goal("Gain") == "Gain"
    assert DietPlan.normalise_goal("nonsense") == "Maintain"
    assert DietPlan.normalise_goal(None) == "Maintain"


# ---------------------------------------------------------- untrustworthy nutrition
def test_a_food_with_impossible_nutrition_is_flagged_not_frozen(db):
    """Brick cheese stated 1200 kcal against 371 from its own macros. Rejecting the
    row on save meant the weight-learning loop crashed on it and nobody could correct
    it either; the flag keeps it writable and keeps the planner away from it."""
    from diet.models import FoodItem
    from diet.planner.candidates import build_pool
    from diet.planner.policy import load_policy

    bad = FoodItem.objects.create(
        name="gate impossible", name_en="gate impossible", api_id="gate-impossible",
        calories=1200, protein=23.2, carbs=2.79, fat=29.7, serving_size="100g",
        allergens=[], allergen_source="verified", needs_review=True)

    bad.smart_score_weight = 0.5
    bad.save(update_fields=["smart_score_weight"])
    assert FoodItem.objects.get(pk=bad.pk).smart_score_weight == 0.5

    pool = build_pool(None, load_policy("maintain"))
    ranked = {getattr(entry, "id", None)
              for macros in pool.by_slot.values()
              for foods in macros.values()
              for entry in foods}
    assert bad.id not in ranked, "a flagged food must not be portioned from"


def test_nutrition_that_contradicts_itself_is_still_refused(db):
    from django.core.exceptions import ValidationError
    from diet.models import FoodItem

    with pytest.raises(ValidationError):
        FoodItem.objects.create(
            name="gate contradiction", name_en="gate contradiction",
            api_id="gate-contradiction", calories=1200, protein=23.2, carbs=2.79,
            fat=29.7, serving_size="100g", allergens=[], allergen_source="verified")


# ------------------------------------------------------------------ idempotency
def test_an_idempotency_key_belongs_to_the_caller_who_chose_it(make_user, api):
    """`IdempotencyKey.key` was unique across the whole table and nothing compared the
    row's owner to the caller. Clients pick their own keys, so two of them choosing the
    same string was enough: the second was handed the first's stored response —
    reference id and both balances — while their own transfer never happened, with a
    200 to say it had."""
    import decimal
    from wallet.models import Wallet

    alice, bob = make_user("idem_alice"), make_user("idem_bob")
    trainer = make_user("idem_tr", user_type="trainer")
    for u in (alice, bob):
        w, _ = Wallet.objects.get_or_create(owner=u, defaults={"owner_type": "client"})
        Wallet.objects.filter(pk=w.pk).update(balance=decimal.Decimal("500.00"))
    Wallet.objects.get_or_create(owner=trainer, defaults={"owner_type": "trainer"})

    url = "/api/wallet/client/transfer/"
    body = {"trainer_id": trainer.id, "amount": "25.00", "idempotency_key": "shared"}

    first = api(alice).post(url, data=body, content_type="application/json")
    assert first.status_code == 200

    second = api(bob).post(url, data=body, content_type="application/json")
    assert second.status_code == 200
    assert second.json()["reference_id"] != first.json()["reference_id"], \
        "bob received alice's receipt"
    assert Wallet.objects.get(owner=bob).balance == decimal.Decimal("475.00")


def test_replaying_a_key_with_different_content_is_refused(make_user, api):
    """`request_hash` was written by four endpoints and compared by none, so the same
    key carrying a different amount replayed the earlier receipt and moved no money."""
    import decimal
    from wallet.models import Wallet

    user = make_user("idem_replay")
    trainer = make_user("idem_replay_tr", user_type="trainer")
    w, _ = Wallet.objects.get_or_create(owner=user, defaults={"owner_type": "client"})
    Wallet.objects.filter(pk=w.pk).update(balance=decimal.Decimal("500.00"))
    Wallet.objects.get_or_create(owner=trainer, defaults={"owner_type": "trainer"})

    url = "/api/wallet/client/transfer/"
    key = "replay-key"
    ok = api(user).post(url, content_type="application/json", data={
        "trainer_id": trainer.id, "amount": "25.00", "idempotency_key": key})
    assert ok.status_code == 200

    same = api(user).post(url, content_type="application/json", data={
        "trainer_id": trainer.id, "amount": "25.00", "idempotency_key": key})
    assert same.status_code == 200, "an identical replay is still a replay"
    assert same.json()["reference_id"] == ok.json()["reference_id"]

    different = api(user).post(url, content_type="application/json", data={
        "trainer_id": trainer.id, "amount": "400.00", "idempotency_key": key})
    assert different.status_code == 422
    assert Wallet.objects.get(owner=user).balance == decimal.Decimal("475.00")


# ------------------------------------------------------------------------ quota
def _paid_user(make_user, name, meals_per_day):
    import decimal
    from datetime import timedelta
    from django.utils import timezone
    from subscription.models import Subscription, SubscriptionPlan

    user = make_user(name)
    plan = SubscriptionPlan.objects.create(
        name=f"plan {name}", price=decimal.Decimal("10.00"), currency="SYP",
        duration_days=30, description="gate", max_meals_per_day=meals_per_day)
    Subscription.objects.create(user=user, plan=plan, status="active",
                                start_date=timezone.now(),
                                end_date=timezone.now() + timedelta(days=30))
    return user


def test_a_metered_feature_counts_and_stops_at_the_limit(make_user):
    """`track_feature_usage` held the only increment and had no callers, so
    `usage_count` never left 0 and every limit passed."""
    from subscription import quota
    from subscription.models import SubscriptionFeature, SubscriptionUsage

    user = _paid_user(make_user, "quota_counts", 3)
    for _ in range(3):
        assert quota.has_headroom(user, "daily_meals")
        quota.consume(user, "daily_meals")
    assert not quota.has_headroom(user, "daily_meals")

    feature = SubscriptionFeature.objects.get(name="daily_meals")
    rows = SubscriptionUsage.objects.filter(subscription=user.subscription, feature=feature)
    assert rows.count() == 1
    assert rows.first().usage_count == 3


def test_a_quota_check_does_not_lock_the_subscriber_out(make_user):
    """The lookup was `(subscription, feature)` while the unique key was
    `(subscription, feature, period_start)` and `period_start` was `auto_now_add`, so
    the constraint could never fire. Concurrent requests each inserted a row; from the
    second on, `get_or_create` raised `MultipleObjectsReturned`, a bare `except:` turned
    that into a denial, and the paying subscriber was refused for good."""
    from subscription import quota
    from subscription.models import SubscriptionFeature, SubscriptionUsage

    user = _paid_user(make_user, "quota_lockout", 5)
    feature = SubscriptionFeature.objects.get(name="daily_meals")

    # Even given rows that pre-date the repair, the check must answer, not blow up.
    for _ in range(3):
        assert quota.has_headroom(user, "daily_meals") is True
    assert SubscriptionUsage.objects.filter(
        subscription=user.subscription, feature=feature).count() == 1


# ---------------------------------------------------------------------- refunds
def _completed_payment(make_user, name):
    import decimal
    from datetime import timedelta
    from django.utils import timezone
    from subscription.models import Payment, Subscription, SubscriptionPlan
    from subscription.services.payment_service import PaymentService

    user = make_user(name)
    plan = SubscriptionPlan.objects.create(
        name=f"refund {name}", price=decimal.Decimal("10.00"), currency="SYP",
        duration_days=30, description="gate")
    sub = Subscription.objects.create(user=user, plan=plan, status="pending",
                                      start_date=timezone.now(),
                                      end_date=timezone.now() + timedelta(days=30))
    payment = Payment.objects.create(
        subscription=sub, amount=decimal.Decimal("10.00"), currency="SYP",
        status="pending", payment_method="shamcash", description="gate")
    PaymentService.complete_payment(payment.id, {
        "amount": "10.00", "currency": "SYP", "status": "completed",
        "event_id": f"gate-{payment.id}"})
    sub.refresh_from_db()
    assert sub.status == "active"
    return sub, payment


def test_a_refund_takes_back_the_access_it_paid_for(make_user):
    """`'completed' -> 'refunded'` was a declared transition nothing could reach: the
    webhook had no branch for it and the admin action wrote the column with
    `queryset.update()`. Either way the money went back and the subscription stayed
    active."""
    from subscription.services.payment_service import PaymentService

    sub, payment = _completed_payment(make_user, "refund_revokes")
    PaymentService.refund_payment(payment.id, "gate")

    payment.refresh_from_db(); sub.refresh_from_db()
    assert payment.status == "refunded"
    assert sub.status == "cancelled"
    assert sub.is_active is False


def test_a_subscription_holds_one_pending_renewal(make_user):
    """Nothing stopped a retried task or a double tap from opening several renewals,
    and every one of them could be completed and charged."""
    from subscription.services.payment_service import PaymentService

    sub, _payment = _completed_payment(make_user, "renewal_guard")
    payments = {PaymentService.start_renewal(sub).id for _ in range(5)}
    assert len(payments) == 1


# ------------------------------------------------------- one bad row, one response
def test_one_unusable_profile_does_not_fail_the_whole_client_list(make_user, api):
    """`get_tdee` called a function that raises on an incomplete profile and on an
    activity level outside its table, once per row of a list, so one client stored as
    'moderate' returned 500 for all 260."""
    from users.models import TrainerClientRelation

    trainer = make_user("tdee_tr", user_type="trainer")
    good = make_user("tdee_ok", height=180, weight=80, age=30, gender="Male",
                     activity_level="Moderate")
    thin = make_user("tdee_partial", height=180, weight=80, age=30)  # no gender
    for client in (good, thin):
        TrainerClientRelation.objects.create(trainer=trainer, client=client, status="approved")

    response = api(trainer).get("/api/auth/trainer/client-profile/")
    assert response.status_code == 200
    assert response.json()["client_count"] == 2


def test_the_database_refuses_an_activity_level_the_calculation_cannot_weight(make_user):
    from django.db import IntegrityError, transaction
    from users.models import CustomUser

    user = make_user("act_level")
    with pytest.raises(IntegrityError), transaction.atomic():
        CustomUser.objects.filter(pk=user.pk).update(activity_level="moderate")


# ---------------------------------------------------------------------- language
def test_a_404_answers_in_the_callers_language(make_user, api):
    """401, 403 and 400 came back in Arabic and 404 did not: Django builds the message
    by interpolating a model name, so it never reaches the catalogue."""
    client = api(make_user("lang_404"))
    client.defaults["HTTP_ACCEPT_LANGUAGE"] = "ar"
    response = client.get("/api/routine/routines/99999999/")
    assert response.status_code == 404
    detail = response.json()["detail"]
    assert detail != "No Routine matches the given query."
    assert any("؀" <= ch <= "ۿ" for ch in detail), detail


def test_every_string_in_the_arabic_catalogue_is_translated():
    import re
    from pathlib import Path

    catalogue = Path("locale/ar/LC_MESSAGES/django.po").read_text(encoding="utf-8")
    entries = re.findall(r'msgid ((?:"[^"]*"\s*)+)msgstr ((?:"[^"]*"\s*)+)', catalogue)
    missing = [msgid for msgid, msgstr in entries
               if msgstr.strip() == '""' and "".join(re.findall(r'"([^"]*)"', msgid))]
    assert not missing, f"{len(missing)} untranslated entries, e.g. {missing[:3]}"


# ------------------------------------------------------------------------- volume
def test_the_achievement_list_does_not_query_per_achievement(make_user, api):
    """Three serializer method fields each queried per row — an EXISTS, a SELECT and a
    metric COUNT — so the endpoint cost 70 queries for 20 achievements and 150 for 40."""
    from django.db import connection
    from django.test.utils import CaptureQueriesContext
    from achievements.models import Achievement

    user = make_user("n1_ach")
    for i in range(5):
        Achievement.objects.create(
            name=f"gate ach {i}", description="d", category="workout", key=f"gate-ach-{i}",
            criteria={"type": "workout_count", "target": i + 1}, points=5)

    def count_queries():
        with CaptureQueriesContext(connection) as ctx:
            assert api(user).get("/api/achievements/").status_code == 200
        return len(ctx.captured_queries)

    small = count_queries()
    for i in range(5, 25):
        Achievement.objects.create(
            name=f"gate ach {i}", description="d", category="workout", key=f"gate-ach-{i}",
            criteria={"type": "workout_count", "target": i + 1}, points=5)
    large = count_queries()
    assert large <= small + 2, f"{small} queries for 5 achievements, {large} for 25"


def test_the_notification_list_does_not_query_per_notification(make_user, api):
    """`recipient` is serialised on every row and was not joined, so a page of 20 cost
    21 queries — one identical user select per row, for the user the queryset already
    filters on."""
    import uuid
    from django.db import connection
    from django.test.utils import CaptureQueriesContext
    from notifications.models import Notification

    user = make_user("n1_notif")
    for i in range(30):
        Notification.objects.create(
            recipient=user, event_type="custom",
            metadata={"context": {"message": f"m{i}"}},
            related_object_id=uuid.uuid4().hex, deduplication_key=uuid.uuid4().hex)

    with CaptureQueriesContext(connection) as ctx:
        response = api(user).get("/api/social/notifications/")
    assert response.status_code == 200
    assert len(response.json()["results"]) == 20
    assert len(ctx.captured_queries) <= 5, \
        f"{len(ctx.captured_queries)} queries for a page of 20"


def test_the_query_log_survives_a_request(make_user, api):
    """`DatabaseQueryCountMiddleware` called `reset_queries()` on every request, which
    clears the log `assertNumQueries` reads — so any query-count assertion around a
    test-client call saw zero and passed, and neither N+1 above could be caught."""
    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    user = make_user("query_log")
    with CaptureQueriesContext(connection) as ctx:
        api(user).get("/api/routine/routines/")
    assert len(ctx.captured_queries) > 0, "the middleware is eating the query log again"


def test_the_notification_list_rejects_the_wrong_pagination_contract(make_user, api):
    """It is cursor-paginated, so `?page=2` was ignored and returned page one again —
    a client written against the platform's documented shape paged forever."""
    response = api(make_user("pagination")).get("/api/social/notifications/?page=2")
    assert response.status_code == 400
    assert "page" in response.json().get("field_errors", response.json())


# --------------------------------------------------- templates that are not templates
def test_no_template_tag_spans_a_line_break():
    """Django's `tag_re` is compiled without `re.DOTALL`, so a `{% ... %}` that does not
    close on the same line is never recognised as a tag — it is emitted as literal text.
    Four `{% trans %}` tags in the account emails were wrapped across lines, so every
    registration and every password reset went out with template syntax visible in the
    HTML body, and those four sentences never reached the translation catalogue."""
    from pathlib import Path

    offenders = []
    for path in Path(".").rglob("*.html"):
        if any(part in {".venv", "_excluded", "node_modules", "site-packages"}
               for part in path.parts):
            continue
        for number, line in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
            opens = line.count("{%") + line.count("{{")
            closes = line.count("%}") + line.count("}}")
            if opens > closes:
                offenders.append(f"{path}:{number}")
    assert not offenders, f"template tags left open at line end: {offenders}"


def test_the_account_emails_contain_no_template_syntax(db):
    """The HTML part is what the user actually reads; the plain-text part was fine, so
    nothing about this was visible from the API."""
    from django.core import mail
    from django.test import override_settings
    from users.utils import send_otp_email
    from users.models import CustomUser

    user = CustomUser.objects.create_user(
        email="tmpl@gate.test", username="tmpl_gate", password="Xx!23456")
    with override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend"):
        mail.outbox = []
        send_otp_email(user, "123456")
        assert mail.outbox, "no email was sent"
        for message in mail.outbox:
            assert "{%" not in message.body and "{{" not in message.body
            for content, _mimetype in getattr(message, "alternatives", []):
                assert "{%" not in content, content[content.find("{%"):][:120]
                assert "{{" not in content


# ------------------------------------------------------------------------ OTP flow
def test_the_otp_locks_out_after_five_wrong_codes(db):
    """A six-digit code with no attempt limit is a 10^6 space one client can walk."""
    from users.utils import verify_otp, create_otp, _clear_otp_attempts
    from users.models import CustomUser

    user = CustomUser.objects.create_user(
        email="otpgate@gate.test", username="otp_gate", password="Xx!23456")
    _clear_otp_attempts(user.email, "registration")
    create_otp(user)

    for _ in range(5):
        ok, _row, message = verify_otp(user.email, "000000")
        assert ok is False
        assert "Too many" not in str(message)

    ok, _row, message = verify_otp(user.email, "000000")
    assert ok is False
    assert "Too many" in str(message), message
    _clear_otp_attempts(user.email, "registration")


# ----------------------------------------------- a derived column and a SET_NULL
def test_deleting_a_trainer_hands_their_exercises_to_the_platform(make_user):
    """`Exercise.is_global` is derived from `created_by` and a check constraint holds
    the pair together. `created_by` is `on_delete=SET_NULL`, which Django implements as
    a bare UPDATE — no `save()`, so the derivation never runs and the row lands ownerless
    with `is_global` still false, which the constraint rejects. Deleting a trainer then
    failed with an IntegrityError naming a constraint the caller never heard of.

    Nothing reached it in practice only because `Wallet.owner` is PROTECT and every user
    gets a wallet, so the delete was refused earlier. Two problems masking each other."""
    from django.db import transaction
    from routine.models import Exercise
    from wallet.models import Wallet

    trainer = make_user("adopt_tr", user_type="trainer")
    exercise = Exercise.objects.create(name="adopt me", created_by=trainer)
    assert exercise.is_global is False

    # Clear the PROTECT that would otherwise stop the delete before the constraint.
    Wallet.objects.filter(owner=trainer).delete()
    with transaction.atomic():
        trainer.delete()

    exercise.refresh_from_db()
    assert exercise.created_by_id is None
    assert exercise.is_global is True, "an exercise whose author is gone belongs to the platform"


def test_a_users_wallet_keeps_them_from_being_deleted(make_user):
    """Deliberate: erasure anonymises and preserves the ledger, and `Wallet.owner` is
    PROTECT so a deletion cannot take a balance with it. Asserted so that a future
    change to `on_delete` is a decision rather than an accident."""
    from django.db.models import ProtectedError
    from wallet.models import Wallet

    user = make_user("protected_by_wallet")
    Wallet.objects.get_or_create(owner=user, defaults={"owner_type": "client"})
    with pytest.raises(ProtectedError):
        user.delete()


# =========================================================================
# Diet engine quality — phase 0.3 of the rebuild.
#
# These assert TODAY's numbers as bounds in the direction of improvement, so a
# regression fails and a fix does not. Each one names the phase that tightens it
# and the value it tightens to. The measurement lives in tests/diet_quality.py and
# the recorded baseline in tests/diet_quality_baseline.json.
#
# The failure mode being guarded is silent: a worse plan is still a plan and
# raises nothing.
# =========================================================================

def test_choosing_your_own_food_changes_what_you_are_served(diet_quality):
    """`find_recipe` took no user argument, so a client who filled in their food
    preferences received exactly the plan of one who ignored the form.

    Measured on the meals the engine BUILDS. A recipe is a fixed combination someone
    wrote down, and when none of the dishes that fit a slot contains what the client
    asked for, the honest answer is that the library does not cover their tastes — no
    amount of ranking puts bulgur in a chicken and rice bowl. Counting both together
    made a library gap read as a personalisation failure, and hid the reverse as well:
    the first version of this measurement had the client choose the library's own
    staples, so the number could not have moved whatever the engine did.
    """
    assert diet_quality.chooser_pool_ranked_first, \
        "a chosen food does not outrank the foods the client did not choose"
    assert diet_quality.twin_built_meals > 0, "nothing was built; nothing to measure"
    assert diet_quality.twin_identical_built == 0, \
        f"choosing your food changed nothing in " \
        f"{diet_quality.twin_identical_built} of " \
        f"{diet_quality.twin_built_meals} meals the engine assembled"


def test_the_library_is_barely_used(diet_quality):
    """find_recipe returned the best-fitting recipe deterministically, so the same
    target always yielded the same dish and most of the library was never served.

    Repeats are counted per slot, because a single global figure treats a slot with one
    usable recipe and a slot with four as the same situation and cannot tell whether
    the planner repeated a dish or had nothing else to serve. Lunch and dinner have
    four dishes each inside tolerance and spread across them; the snack slot has one,
    and no amount of engine work changes that. P6's content is what lifts it.

    Serving the same dish twice in one day is different: nothing about the library
    forces it, and it happened on 9 of 42 days because recency was snapshotted before
    the day began and the set written during the day was only read the next morning.
    A dish is now banned for the rest of the day it is served and penalised for three
    days after, so this is zero and stays zero.
    """
    assert diet_quality.distinct_dishes >= 7, \
        f"dish variety regressed to {diet_quality.distinct_dishes}"
    assert diet_quality.days_repeating_a_dish == 0, \
        f"a dish was served twice in one day on " \
        f"{diet_quality.days_repeating_a_dish} of {diet_quality.days_measured} days"

    for slot, distinct in diet_quality.distinct_dishes_by_slot.items():
        if distinct < 2:
            continue  # nothing else fits this slot; that is the library, not the engine
        served = diet_quality.meals_by_slot.get(slot, 0)
        repeats = diet_quality.max_repeats_by_slot.get(slot, 0)
        assert repeats <= 0.6 * served, \
            f"{slot} served one of its {distinct} dishes {repeats} times in {served}"


def test_no_portion_is_larger_than_the_food_allows(diet_quality):
    """A portion used to be an unbounded gram figure: 350 g of egg white is eleven of
    them, and 370 g of squash is a plate nobody finishes.

    The bound is each food's own ladder, read from the engine rather than from a table
    maintained beside it — when those were two definitions they disagreed and 53
    portions above a food's declared maximum passed a green gate at an 8% allowance.

    Zero, not a percentage. An amount off the ladder is now unrepresentable rather than
    discouraged: every stage that adjusts a portion, including the one that runs after
    the plan is saved, chooses from the rungs instead of multiplying grams by a factor.
    A percentage allowance here would only hide the next stage that forgets.
    """
    assert diet_quality.absurd_portion_rate == 0.0, \
        f"{diet_quality.absurd_portion_rate:.0%} of portions exceed the food's own " \
        f"ceiling: {diet_quality.absurd_examples[:3]}"


def test_the_last_corrector_cannot_undo_the_portioning(make_user, diet_catalogue):
    """`converge_plan` runs on the SAVED rows, after everything else has finished.

    The quality harness measures what `generate` returns, which is not what a client
    receives: this runs later, during persistence, and rewrites the quantities. While
    it adjusted portions by multiplying grams by a factor bounded only by a per-macro
    gram cap, it could take a meal that had been portioned correctly and put 300 g of
    rice back on the plate — and no measurement would have seen it, because every
    number in the baseline is taken before this point.

    Asserting the invariant directly is cheaper than measuring through persistence and
    catches the case the measurement structurally cannot.
    """
    from datetime import date, timedelta

    from diet.models import DietPlan, Meal, MealComponent
    from diet.planner.converge import converge_plan
    from diet.planner.portion import portions_for

    user = make_user("convergegate")
    plan = DietPlan.objects.create(
        user=user, goal="Maintain", daily_calories=2200.0,
        start_date=date.today(), end_date=date.today() + timedelta(days=1))
    meal = Meal.objects.create(diet_plan=plan, meal_type="Lunch", date=date.today())
    # Deliberately off the ladder and well over: what the old corrector used to write.
    starting = {"Chicken Breast": 400.0, "White Rice": 500.0,
                "Olive Oil": 55.0, "Broccoli": 380.0}
    for food_key, grams in starting.items():
        MealComponent.objects.create(meal=meal, food=diet_catalogue[food_key],
                                     quantity=grams, meal_time="Lunch")

    converge_plan(plan)

    for component in meal.components.select_related("food"):
        rungs = [p.grams for p in portions_for(component.food)]
        assert any(abs(component.quantity - g) < 0.5 for g in rungs), (
            f"{component.food.name} left at {component.quantity} g, which is not one of "
            f"{rungs}")


def test_the_catalogue_can_answer_what_a_serving_is(seeded_catalogue, db):
    """A food with no serving unit falls back to a gram ladder, which is a weaker bound
    than its own — so coverage is a property worth failing on, not a statistic.

    The rules match on patterns rather than an exhaustive list precisely so a food
    imported tomorrow is covered, but a whole family can still slip through: halibut,
    sea bass and venison matched nothing, and neither did garlic, which is not a food a
    meal is built on at all.
    """
    from diet.models import FoodItem
    from diet.planner.portion import unit_levels

    selectable = list(FoodItem.objects.filter(needs_review=False)
                      .exclude(role=FoodItem.ROLE_CONDIMENT))
    assert selectable, "nothing to measure"
    without = [f.name for f in selectable if not unit_levels(f)]
    share = len(without) / len(selectable)
    assert share <= 0.05, (
        f"{share:.0%} of selectable foods declare no serving: {sorted(without)[:8]}")


def test_a_portion_sticks_at_one_or_two_sizes(diet_quality):
    """A floor in a greedy filler is an attractor: the algorithm satisfies it minimally
    and stops, so the floor becomes the permanent answer.

    Tightens in P4 to >= 4 once floors become min_units inside the search space."""
    assert diet_quality.min_distinct_portions_per_food >= 1
    assert diet_quality.min_distinct_portions_per_food <= 3, \
        "portion diversity improved; tighten this bound"


def test_calorie_drift_only_ever_goes_up(diet_quality):
    """Round-up steps, floors firing after the target is met, and residual filling all
    push one way. Noise would fall both ways, so this is a bias with a cause.

    The bound is deliberately loose. Drift is noisy today for two reasons that P4
    removes: the planner seeds its RNG from the user id, and portions are continuous
    grams so a single rounding decision moves the total. Observed range across runs and
    catalogues is +6.2% to +10.9%. What this guards is a regression to something much
    worse, not the exact figure.

    Tightens in P4 to <= 2% and drift_all_one_sided False, at which point it becomes a
    real assertion rather than a tripwire."""
    assert diet_quality.drift_worst_abs <= 14.0, \
        f"drift worsened to {diet_quality.drift_worst_abs:+.1f}%"


def test_condiments_top_the_candidate_lists(diet_quality):
    """Ranking is grams of macro per kcal, which is maximised by foods that are almost
    pure macro and nothing else, so sauces and jellies outrank staples.

    Closed in P1: `FoodItem.role` now excludes condiments from the pool entirely, which
    is the source fix. Tuning the density weight would only have moved them down a
    place."""
    assert not diet_quality.condiment_slots, \
        f"condiments are back in {len(diet_quality.condiment_slots)} slots: " \
        f"{diet_quality.condiment_slots}"


def test_breakfast_is_the_weakest_slot(diet_quality):
    """Half of breakfasts are an unstructured pile because the library holds five
    breakfast recipes and none is Levantine.

    Tightens in P3 and P6 to >= 0.85."""
    breakfast = diet_quality.dish_rate_by_slot.get("Breakfast", 0.0)
    assert breakfast >= 0.45, f"breakfast dish rate fell to {breakfast:.0%}"
    assert diet_quality.dish_rate_overall >= 0.45, \
        f"overall dish rate fell to {diet_quality.dish_rate_overall:.0%}"


def test_the_recorded_baseline_still_describes_this_engine(diet_quality):
    """Guards the harness itself. If the catalogue changes underneath it the recorded
    baseline stops meaning anything, and every comparison above is against a number
    that no longer applies."""
    import json
    from pathlib import Path

    from diet.models import FoodItem, Recipe

    baseline = json.loads(Path("tests/diet_quality_baseline.json").read_text())
    assert FoodItem.objects.filter(needs_review=False).count() >= \
        baseline["catalogue"]["foods"], "catalogue shrank; re-record the baseline"
    assert Recipe.objects.filter(is_active=True).count() >= \
        baseline["catalogue"]["recipes"], "recipes were removed; re-record the baseline"


def test_the_two_seed_commands_agree_on_food_names(seeded_catalogue, db):
    """A fresh production database cannot build most of its own recipes.

    `add_healthy_foods` writes 100 foods and `seed_recipes` writes recipes that name
    their ingredients, and the two disagree: the catalogue has "Chicken Breast
    (Grilled)", "Salmon Fillet", "Greek Yogurt (Non-Fat)", "Sweet Potato (Baked)",
    "Extra Virgin Olive Oil" and "Lentils (Cooked)", while the recipes ask for "Chicken
    Breast", "Salmon", "Greek Yogurt", "Sweet Potato", "Olive Oil" and "Lentils". Oats
    are absent from a hundred-item healthy-food catalogue entirely.

    The effect is not subtle. On the development database, whose 340 Edamam rows happen
    to carry the plain names, the engine serves a named dish 71% of the time. On the
    catalogue a fresh install actually gets, it manages 42%, breakfast drops to 17%, and
    a client's chosen ingredients reach the plate 3% of the time instead of 32%.

    That development database is going to be dropped before launch. This is the number
    that ships."""
    from diet.models import FoodItem, Recipe

    from diet.management.commands.seed_recipes import RECIPES

    # `seed_recipes` skips any recipe whose foods it cannot find, so asking whether the
    # recipes that exist resolve is a question that answers itself. Ask instead what the
    # seed intended to create, and compare that against the catalogue.
    unresolvable = {}
    for name, _meals, _cuisine, _minutes, lines in RECIPES:
        missing = [
            food_name for food_name, _grams, _scalable in lines
            if not FoodItem.objects.filter(name__iexact=food_name).exists()
            and not FoodItem.objects.filter(name__icontains=food_name).exists()
        ]
        if missing:
            unresolvable[name] = missing

    seeded = Recipe.objects.filter(is_active=True).count()
    assert seeded >= 12, f"only {seeded} of {len(RECIPES)} recipes survived seeding"
    assert not unresolvable, (
        f"{len(unresolvable)} of {len(RECIPES)} recipes cannot be built from the seeded "
        f"catalogue: {unresolvable}"
    )


def test_the_catalogue_knows_what_a_serving_is(seeded_catalogue, db):
    """P1.4. A portion was an unbounded gram figure because nothing said what a serving
    was, which is how 350 g of egg white — eleven of them — reached a plate.

    Coverage rather than completeness: a food with no unit still portions in grams, so
    this guards against the seeding rules silently stopping working, not against a gap."""
    from diet.models import FoodItem

    total = FoodItem.objects.filter(needs_review=False).count()
    with_units = FoodItem.objects.filter(
        needs_review=False, unit_grams__isnull=False, max_units__isnull=False).count()
    assert total, "no catalogue to measure"
    assert with_units / total >= 0.75, \
        f"only {with_units} of {total} foods carry a serving unit"

    # The rules must not have drifted back into matching substrings.
    squash = FoodItem.objects.filter(name__icontains="Butternut").first()
    if squash is not None:
        assert squash.household_unit != "tbsp", \
            "Butternut Squash matched the butter rule again"


def test_no_test_row_can_be_served(seeded_catalogue, db):
    """P1.1. Eighteen rows named "Manual Food N" carried 20 g of protein each and ranked
    second through fifth in every protein slot the planner offered."""
    from diet.models import FoodItem

    assert not FoodItem.objects.filter(name__regex=r"^Manual Food \d+$").exists()
    assert not FoodItem.objects.filter(name__iexact="test").exists()


def test_a_meal_has_a_shape(diet_quality, db):
    """P3. Templates are read off the recipe library rather than written by hand, and
    the library turns out to be strikingly consistent: six of sixteen recipes are
    protein + carb + vegetable + fat, two more are protein + carb + fruit.

    The shape is what a pairwise affinity graph cannot express. Pairwise says salmon
    goes with rice; it does not say a meal takes exactly one starch, so it would serve
    salmon with rice and oats and potato and each edge would be individually fine."""
    from diet.planner.templates import derive_templates, pairing_edges

    templates = derive_templates()
    assert len(templates) >= 8, f"only {len(templates)} shapes derived"
    assert all(t.slots for t in templates)
    assert any(t.seen >= 3 for t in templates), "no shape recurs; the library has no pattern"

    edges = pairing_edges()
    assert edges, "no pairings derived from the recipe library"


def test_a_portion_is_a_number_of_somethings(seeded_catalogue, db):
    """P4.1. Every servable amount of a food is a multiple of a unit a person
    recognises, inside the range that food declares. 350 g of egg white — eleven of
    them — stops being discouraged and becomes unrepresentable."""
    from diet.models import FoodItem
    from diet.planner.portion import nearest_portion, portions_for

    egg = FoodItem.objects.filter(name__iexact="Egg White").first()
    assert egg is not None and egg.unit_grams, "the catalogue lost its egg white unit"

    options = portions_for(egg)
    assert options, "no servable portions offered"
    assert max(p.grams for p in options) <= egg.unit_grams * egg.max_units + 0.01

    # Asking for an absurd amount returns the largest sane one, not the absurd one.
    assert nearest_portion(egg, 350).grams <= 200
    assert "egg white" in nearest_portion(egg, 100).described

# ---------------------------------------------------------------------------
# The optimiser, measured against ground truth rather than against itself
# ---------------------------------------------------------------------------

def _mid_rung(food):
    from diet.planner.portion import portions_for
    rungs = portions_for(food)
    return rungs[len(rungs) // 2].grams


def _singles_only(components, targets, tolerance, passes=8):
    """What the optimiser used to be: one portion moved at a time, until it stalls."""
    from diet.planner.optimize import _single_pass, totals_of
    from diet.planner.portion import portions_for
    from diet.planner.report import deviation_of

    best = list(components)
    best_dev = deviation_of(totals_of(best), targets)
    indices = list(range(len(best)))
    ladders = {i: portions_for(best[i][0]) for i in indices}
    for _ in range(passes):
        best, best_dev, improved = _single_pass(
            best, best_dev, targets, tolerance, indices, ladders)
        if not improved:
            break
    return best, best_dev


def _exhaustive_best(components, targets, tolerance):
    import itertools

    from diet.planner.optimize import totals_of
    from diet.planner.portion import portions_for
    from diet.planner.report import deviation_of

    ladders = [portions_for(f) for f, _g in components]
    best = None
    for combo in itertools.product(*ladders):
        cand = [(components[i][0], p.grams) for i, p in enumerate(combo)]
        dev = deviation_of(totals_of(cand), targets)
        if not dev.within(tolerance):
            continue
        if best is None or dev.magnitude < best.magnitude:
            best = dev
    return best


def test_the_optimiser_escapes_two_move_local_minima(seeded_catalogue):
    """A meal over on one macro and under on another needs both portions moved at once.

    Either move alone makes the objective worse, so a search that only ever changes one
    food declares itself finished and serves a portioning it could have beaten. That is
    the shape of every local minimum the adversarial pass found, and it is what paired
    moves exist to escape. The test asserts the old search still stalls, so it keeps its
    teeth as the catalogue changes.
    """
    from diet.models import FoodItem
    from diet.planner.optimize import refine, totals_of
    from diet.planner.policy import load_policy

    policy = load_policy("maintain")
    rng = random.Random(4242)
    foods = list(FoodItem.objects.filter(needs_review=False).exclude(role="condiment"))
    stalled = escaped = compared = 0

    for _ in range(180):
        picked = rng.sample(foods, 3)
        start = [(f, _mid_rung(f)) for f in picked]
        totals = totals_of(start)
        target = {
            "calories": totals["calories"] * rng.uniform(0.75, 1.3),
            "protein": totals["protein"] * rng.uniform(0.7, 1.5),
            "carb": totals["carb"] * rng.uniform(0.7, 1.5),
            "fat": totals["fat"] * rng.uniform(0.7, 1.5),
        }
        if min(target.values()) <= 0:
            continue
        best = _exhaustive_best(start, target, policy.tolerance)
        if best is None:
            continue
        compared += 1
        _s, singles = _singles_only(start, target, policy.tolerance)
        _p, paired = refine(start, target, policy.tolerance)
        if singles.magnitude > best.magnitude + 1e-6:
            stalled += 1
            if paired.magnitude <= best.magnitude + 1e-6:
                escaped += 1

    assert compared >= 30, f"only {compared} feasible cases; the test has no teeth"
    assert stalled >= 5, (
        f"single-move search stalled on only {stalled} of {compared} cases, so this "
        "test is no longer exercising the failure it exists to catch")
    # Measured at 40 of 43 when this was written. The residue needs three portions
    # moved together, which is a wider neighbourhood than this search offers and a
    # deliberate stopping point rather than an oversight: pairs close 93% of the class
    # for about a third more planning time, and triples would multiply that again for
    # the last few percent. The bound is here so a REGRESSION is visible; raising it is
    # a decision about cost, not a bug fix.
    assert escaped >= stalled * 0.9, (
        f"paired moves escaped only {escaped} of {stalled} two-move local minima")


def test_the_optimiser_reaches_the_proven_optimum(seeded_catalogue):
    """Every portioning the engine serves, against every portioning it could have had.

    A green gate says no assertion tripped. It cannot say the optimiser settled for a
    meal it had the information to beat. The benchmark enumerates the whole space for
    each meal, which is small — a handful of foods at a handful of servable amounts —
    and reports the gap. Before paired moves and unconditional refinement, 69% of
    feasible meals sat at the optimum.
    """
    from tests.optimiser_benchmark import run

    report = run(days=2)
    assert report.feasible, "benchmark produced no feasible meals"
    assert report.optimal_share >= 0.90, (
        f"only {report.optimal_share:.0%} of feasible meals are at the proven optimum "
        f"({len(report.off_optimum)} of {len(report.feasible)} off it); "
        f"worst objective gap {report.worst_objective_gap:.3f}")
    assert report.worst_objective_gap <= 0.20, (
        f"worst objective gap {report.worst_objective_gap:.3f}")


def test_paired_moves_cannot_leave_the_ladder_or_change_the_meal(seeded_catalogue):
    """Widening the search must not widen what it is allowed to do.

    Paired moves choose amounts and only amounts. Every amount comes from the food's own
    declared ladder and the set of foods is never touched, so ceilings, minimums,
    allergens, dislikes and the meal's shape are all outside what this search can reach.
    """
    from diet.models import FoodItem
    from diet.planner.optimize import refine
    from diet.planner.policy import load_policy
    from diet.planner.portion import portions_for

    policy = load_policy("maintain")
    rng = random.Random(99)
    foods = list(FoodItem.objects.filter(needs_review=False).exclude(role="condiment"))
    for _ in range(60):
        picked = rng.sample(foods, rng.randint(2, 4))
        start = [(f, _mid_rung(f)) for f in picked]
        target = {"calories": 700, "protein": 45, "carb": 80, "fat": 20}
        tuned, _dev = refine(start, target, policy.tolerance)
        assert [f.id for f, _g in tuned] == [f.id for f, _g in start], \
            "refine changed which foods are in the meal"
        for food, grams in tuned:
            assert any(abs(p.grams - grams) < 1e-6 for p in portions_for(food)), \
                f"refine produced {grams} g of {food.name}, off its own ladder"

