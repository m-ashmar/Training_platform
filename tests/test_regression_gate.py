"""The regression gate.

Every assertion here corresponds to a defect this audit actually found and fixed. It
exists because 84 fixes across 11 phases had no automated protection: `manage.py test`
discovered nothing, and CI ran security scanners without ever executing the application.

Kept deliberately fast (one shared database, no per-test migrations) so it can run on
every pull request. The 53 standalone probes in tests/security/ remain the deep suite.
"""
import json

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
    from users.models import CustomUser

    tr = CustomUser.objects.create_user(email="gate_t@x.test", username="gate_t", password="Xx!23456")
    Exercise.objects.create(name="Barbell Squat", created_by=tr, is_global=True)
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
    return {
        n: FoodItem.objects.create(
            name=n, name_en=n, category=cat, api_id=f"gate-{n}", calories=c,
            protein=p, carbs=cb, fat=f, serving_size="100g",
            allergens=[], allergen_source="verified",
        )
        for n, c, p, cb, f in rows
    }


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
    plan = DietPlan.objects.create(user=user, goal="maintain", daily_calories=2000,
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

    user = make_user("gate_dedup")
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
