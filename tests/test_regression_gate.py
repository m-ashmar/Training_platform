"""The regression gate.

Every assertion here corresponds to a defect this audit actually found and fixed. It
exists because 84 fixes across 11 phases had no automated protection: `manage.py test`
discovered nothing, and CI ran security scanners without ever executing the application.

Kept deliberately fast (one shared database, no per-test migrations) so it can run on
every pull request. The 53 standalone probes in tests/security/ remain the deep suite.
"""
import collections
import json
import pathlib
from datetime import date
import random
import re

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

    # `chat` joined the list when the app was added: both of its tables are paged
    # (the conversation list, the thread) and neither sorts on a unique column alone.
    local = {"users", "wallet", "diet", "routine", "subscription", "social",
             "ai_assistant", "achievements", "analytics", "notifications", "challenges",
             "chat"}
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
    allowed = {"/api/auth/health/", "/api/auth/trainers/public/", "/api/auth/trainers/stats/",
               # What a build may show and sell. Public on purpose: the app asks at launch,
               # before anyone has signed in, and the answer never depends on who asks.
               "/api/v1/config/capabilities", "/api/v1/config/capabilities/",
               # The remote theme, for the same reason and on the same terms: the app
               # asks for its skin at launch, before sign-in, and the answer is computed
               # from the channel and version headers alone. Both spellings, as
               # capabilities does.
               "/api/v1/config/theme", "/api/v1/config/theme/",
               # Design v2: the intro story plays and the trainer roster is browsed before
               # sign-in (the visitor shell). Neither runs authentication, and neither
               # answer depends on who asks. The media route answers both spellings, as
               # capabilities does: its contract names it without the slash (B-035).
               "/api/v1/content/media", "/api/v1/content/media/", "/api/v1/trainers/"}
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
    _p, _ = UserFoodPreference.objects.get_or_create(user=user)
    _p.allergies = "fish"; _p.save(update_fields=["allergies"])
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
    assert match is not None, "a fish-free dinner exists in the library; none was offered"
    names = " ".join(f.name.lower() for f, _g in match.components)
    assert "salmon" not in names and "fish" not in names and "tilapia" not in names


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
        "ai_assistant.tasks.purge_expired_training_data",
    ):
        assert any(e["task"] == required for e in schedule.values()), \
            f"{required} is no longer scheduled"

    # And the OTHER direction, which is how the retention purge above went missing. It
    # was registered with Celery, written to run on a timer, documented as the only code
    # that reads `retain_until` correctly — and in no beat entry, so it never ran once.
    # Checking only beat -> registry cannot see that.
    #
    # A task whose name is a periodic verb has to be scheduled or listed here with a
    # reason. Anything else is triggered by an event and does not belong on a timer.
    triggered_on_purpose = {
        # Enqueued by a request, not by the clock.
        "diet.tasks.generate_rule_based_plan",
        "notifications.tasks.process_event_task",
        "notifications.tasks.send_push_notification",
        "notifications.tasks.retry_failed_notification",
        "social.tasks.fan_out_post",
    }
    periodic_verbs = ("purge_", "expire_", "drain_", "refresh_", "sweep_", "cleanup_",
                      "close_idle")
    scheduled = {e["task"] for e in schedule.values()}
    unscheduled = sorted(
        name for name in registered
        if not name.startswith("celery.")
        and any(verb in name.rsplit(".", 1)[-1] for verb in periodic_verbs)
        and name not in scheduled
        and name not in triggered_on_purpose
    )
    assert not unscheduled, (
        "these tasks are written to run on a timer and are on no timer: "
        f"{unscheduled}")


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
    for f in _repo_python_files():
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
    for f in _repo_python_files():
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


@pytest.fixture
def configured_gateway():
    """A ShamCash that can actually give a customer somewhere to send money.

    The registry is built from the environment at import, and the test environment sets
    none of it — so a gateway with no API token AND no account id is, correctly, refused
    for payment initiation. That is the deploy failure `can_initiate_payments` exists to
    stop: all three ShamCash variables blank used to answer 200 and tell every customer
    to transfer 5,000 SYP to account "". A test about PRICING needs a usable gateway to
    reach the pricing code, so it configures one rather than asserting against a broken
    deployment.
    """
    from subscription.settings import gateway_config

    config = gateway_config.GATEWAY_REGISTRY["shamcash"]["config"][gateway_config.GATEWAY_MODE]
    before = config.get("merchant_id")
    config["merchant_id"] = "GATE-ACCOUNT-1"
    try:
        yield
    finally:
        config["merchant_id"] = before


def test_the_client_cannot_choose_what_it_pays(make_user, api, paid_plan, configured_gateway):
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
    for path in [p for p in _repo_python_files() if p.name == "views.py"]:
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


_PO_STRING = r'"(?:[^"\\]|\\.)*"'  # a quoted .po line; `"[^"]*"` stopped at an escaped \"


def test_every_string_in_the_arabic_catalogue_is_translated():
    """A msgid holding an escaped quote never matched `"[^"]*"`, so three empty
    translations passed this check and reached Arabic users in English."""
    import re
    from pathlib import Path

    catalogue = Path("locale/ar/LC_MESSAGES/django.po").read_text(encoding="utf-8")
    entries = re.findall(rf'msgid ((?:{_PO_STRING}\s*)+)msgstr ((?:{_PO_STRING}\s*)+)', catalogue)
    missing = [msgid for msgid, msgstr in entries
               if msgstr.strip() == '""' and "".join(re.findall(r'"((?:[^"\\]|\\.)*)"', msgid))]
    assert not missing, f"{len(missing)} untranslated entries, e.g. {missing[:3]}"


def test_no_entry_in_the_arabic_catalogue_is_fuzzy():
    """makemessages marks a changed msgid `fuzzy` and fills in the Arabic of the closest
    old one; msgfmt leaves fuzzy entries out of the .mo, so Arabic users got English. 113
    sat in the catalogue, many carrying another message's Arabic ("Calories cannot be
    negative" read "Price cannot be negative"). Translate the new msgid, then drop the
    flag and its `#| msgid` line."""
    import re
    from pathlib import Path

    catalogue = Path("locale/ar/LC_MESSAGES/django.po").read_text(encoding="utf-8")
    fuzzy = re.findall(rf'^#,.*\bfuzzy\b.*\n(?:#.*\n)*msgid ((?:{_PO_STRING}\s*)+)', catalogue, re.M)
    assert not fuzzy, f"{len(fuzzy)} fuzzy entries, e.g. {fuzzy[:3]}"


#: The names a msgid can be written under, and which argument carries it. `_` is the
#: project's spelling of `gettext`/`gettext_lazy`; the context and plural forms put the
#: literal somewhere other than the first argument.
_GETTEXT_KEYWORDS = {
    "_": (0,), "gettext": (0,), "gettext_lazy": (0,), "gettext_noop": (0,),
    "pgettext": (1,), "pgettext_lazy": (1,),
    "ngettext": (0, 1), "ngettext_lazy": (0, 1),
    "npgettext": (1, 2), "npgettext_lazy": (1, 2),
}

_PO_ESCAPES = {"n": "\n", "t": "\t", "r": "\r", '"': '"', "\\": "\\",
               "a": "\a", "b": "\b", "f": "\f", "v": "\v"}


def _po_unescape(raw):
    out, i = [], 0
    while i < len(raw):
        if raw[i] == "\\" and i + 1 < len(raw):
            out.append(_PO_ESCAPES.get(raw[i + 1], raw[i + 1]))
            i += 2
        else:
            out.append(raw[i])
            i += 1
    return "".join(out)


def _read_po(text):
    """Every entry in a .po file, read LINE BY LINE.

    Not a regex over blank-line-separated blocks. An entry ends at a blank line or at
    the next entry's comment lines, and one entry in this catalogue had lost its blank
    line — which made a block reader glue it to its neighbour and then report a msgid
    that IS translated as missing. Each entry is
    `{msgid, msgid_plural, msgstrs, flags}`.
    """
    import re

    string = re.compile(r'"((?:[^"\\]|\\.)*)"')
    entries, entry, field = [], None, None

    def blank():
        return {"msgid": "", "msgid_plural": "", "msgstrs": [], "flags": set()}

    def flush():
        nonlocal entry
        if entry and (entry["msgid"] or entry["msgid_plural"]):
            entries.append(entry)
        entry = None

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            flush()
            field = None
            continue
        if line.startswith("#"):
            flush()
            entry = entry or blank()
            if line.startswith("#,"):
                entry["flags"].update(f.strip() for f in line[2:].split(","))
            field = None
            continue
        entry = entry or blank()
        chunk = "".join(_po_unescape(m) for m in string.findall(line))
        if line.startswith("msgid_plural"):
            entry["msgid_plural"], field = chunk, "msgid_plural"
        elif line.startswith("msgid"):
            if entry["msgid"] or entry["msgstrs"]:
                flush()
                entry = blank()
            entry["msgid"], field = chunk, "msgid"
        elif line.startswith("msgctxt"):
            field = "msgctxt"
        elif line.startswith("msgstr"):
            entry["msgstrs"].append(chunk)
            field = "msgstr"
        elif line.startswith('"') and field == "msgid":
            entry["msgid"] += chunk
        elif line.startswith('"') and field == "msgid_plural":
            entry["msgid_plural"] += chunk
        elif line.startswith('"') and field == "msgstr":
            entry["msgstrs"][-1] += chunk
    flush()
    return entries


def _first_party_msgids():
    """`{msgid: "app/file.py:42"}` for every literal a first-party module translates.

    Parsed with `ast`, which resolves escapes and implicit concatenation exactly as the
    interpreter does, so the string compared against the catalogue is the one gettext
    will be handed at runtime. A `_(variable)` cannot be seen here — `makemessages`
    cannot see one either, which is why the project writes literals.

    First-party means every Python package in this checkout, not `INSTALLED_APPS`:
    `home/` is a package of views included by the URLconf and is not an app, and a
    future one would be missed the same way. `.venv` is skipped because it holds no
    top-level `__init__.py` — and naming it would not help in a worktree, where it is
    a symlink INSIDE the repo.
    """
    import ast
    from pathlib import Path

    from django.conf import settings

    #: `tests` is this suite; the other three are not part of the codebase (CLAUDE.md).
    not_ours = {"tests", "_excluded", "mobile", "flutter", "yalla_gym"}

    root = Path(settings.BASE_DIR).resolve()
    roots = {child for child in root.iterdir()
             if child.is_dir() and child.name not in not_ours
             and (child / "__init__.py").exists()}

    def literal(node, index):
        args = node.args
        if len(args) > index and isinstance(args[index], ast.Constant):
            value = args[index].value
            return value if isinstance(value, str) and value else None
        return None

    found = {}
    for app_root in sorted(roots):
        for source in sorted(app_root.rglob("*.py")):
            relative = source.relative_to(root)
            if {"migrations", "tests"} & set(relative.parts):
                continue
            for node in ast.walk(ast.parse(source.read_text(encoding="utf-8"))):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                name = (func.id if isinstance(func, ast.Name)
                        else func.attr if isinstance(func, ast.Attribute) else None)
                for index in _GETTEXT_KEYWORDS.get(name, ()):
                    value = literal(node, index)
                    if value:
                        found.setdefault(value, f"{relative}:{node.lineno}")
    return found


def test_every_first_party_msgid_reaches_the_arabic_catalogue():
    """The two checks above only look at entries that ARE in the catalogue, so a msgid
    that never reached it at all was invisible to the gate. `policy` — the app that
    refuses a purchase on a store build — had none of its six strings translated, so
    `app_channel_required` and `not_available_on_channel` answered an Arabic-speaking
    user in English; the sweep that followed found 152 of them across ten first-party
    apps (SIDE_BUGS S-019). Arabic is this platform's primary language: a literal handed
    to gettext in this repo and missing here is a defect, not a to-do.

    The compiled `.mo` is checked too, not just the `.po`, because that is the file the
    server reads — a translation written and never compiled reaches the user in English
    exactly as if it had never been written. After adding entries:

        msgfmt --check-format -o locale/ar/LC_MESSAGES/django.mo \\
                                 locale/ar/LC_MESSAGES/django.po
    """
    import gettext as gettext_module
    from pathlib import Path

    source_msgids = _first_party_msgids()

    translated = set()
    for entry in _read_po(Path("locale/ar/LC_MESSAGES/django.po").read_text(encoding="utf-8")):
        if not entry["msgstrs"] or not all(s.strip() for s in entry["msgstrs"]):
            continue
        translated.update(m for m in (entry["msgid"], entry["msgid_plural"]) if m)

    absent = sorted((msgid, where) for msgid, where in source_msgids.items()
                    if msgid not in translated)
    assert not absent, (
        f"{len(absent)} first-party msgids have no Arabic in "
        f"locale/ar/LC_MESSAGES/django.po, e.g. {absent[:3]}")

    with open("locale/ar/LC_MESSAGES/django.mo", "rb") as handle:
        compiled = gettext_module.GNUTranslations(handle)._catalog
    uncompiled = sorted((msgid, where) for msgid, where in source_msgids.items()
                        if msgid not in compiled)
    assert not uncompiled, (
        f"{len(uncompiled)} msgids are translated in django.po but missing from the "
        f"compiled django.mo — run msgfmt. e.g. {uncompiled[:3]}")


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
    # 5 -> 6, deliberately, when blocking arrived: `NotificationViewSet.get_queryset`
    # now calls `moderation.services.blocked_ids`, which is ONE query for the whole
    # page — a blocked person's like, comment or follow must not keep arriving as a
    # notification. The claim this test makes is unchanged: the cost does not grow
    # with the number of rows, and 20 rows still must not cost 20 queries.
    assert len(ctx.captured_queries) <= 6, \
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
    templates = [q for q in REPO_ROOT.rglob("*.html")
                 if not ({".venv", "_excluded", "node_modules", "site-packages"}
                         & set(q.parts))]
    # From the REPO, not the working directory, and it must actually find some.
    assert len(templates) > 5, f"only {len(templates)} templates found under {REPO_ROOT}"
    for path in templates:
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
    assert diet_quality.days_repeating_a_dish == 0, \
        f"a dish was served twice in one day on " \
        f"{diet_quality.days_repeating_a_dish} of {diet_quality.days_measured} days"

    import json
    from pathlib import Path as _Path

    recorded = json.loads(
        (_Path(__file__).parent / "diet_quality_baseline.json").read_text())["measured"]
    recorded_slots = recorded.get("distinct_dishes_by_slot", {})
    # Every slot the baseline records must still BE served. Iterating only the slots
    # the measurement produced meant a slot that stopped being served entirely was
    # simply not iterated, and satisfied the test by disappearing.
    assert set(recorded_slots) <= set(diet_quality.distinct_dishes_by_slot), (
        f"slots vanished from the plan: "
        f"{sorted(set(recorded_slots) - set(diet_quality.distinct_dishes_by_slot))}")

    for slot, distinct in diet_quality.distinct_dishes_by_slot.items():
        # A slot with two dishes cannot serve either under 60% of the time without
        # alternating perfectly, so the rule would read the library's breadth as the
        # engine's behaviour. Residual carry made a second snack dish reachable and
        # concentration fell from 100% to 64% — an improvement this rule then failed.
        if distinct < 3:
            # Skipping means a change that DROPS a slot to two dishes turns its own
            # concentration check off — degrading the measured thing satisfies the
            # test. The slot still has to justify itself against the baseline.
            assert distinct >= recorded_slots.get(slot, 0), (
                f"{slot} fell to {distinct} distinct dishes, from "
                f"{recorded_slots.get(slot, 0)}")
            continue
        served = diet_quality.meals_by_slot.get(slot, 0)
        repeats = diet_quality.max_repeats_by_slot.get(slot, 0)
        # Against the RECORDED figure plus one, not 0.6 x served. Snack's worst is 5 of
        # about 42, so the proportional bound sat at 25 — the same dish could have been
        # served five times as often and this still passed. A ratchet nothing can trip
        # is a comment.
        allowed = recorded["max_repeats_by_slot"].get(slot, 0) + 1
        assert repeats <= allowed, (
            f"{slot} served one of its {distinct} dishes {repeats} times in {served}; "
            f"the baseline records {allowed - 1}")


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


def test_a_portion_is_never_stuck_at_its_own_minimum(diet_quality):
    """A floor in a greedy filler is an attractor: the algorithm satisfies it minimally
    and stops, so the floor becomes the permanent answer.

    The metric this used to assert — `min_distinct_portions_per_food >= 2` — could not
    tell that defect from its opposite. It is a minimum over every food served four or
    more times, so ONE food pins it, and it says nothing about WHERE the food is pinned.
    Three foods currently take a single amount every time: six figs, a large Arabic
    coffee, half a lemon — and each of those is the TOP of that food's ladder, on days the
    plan runs slightly under target. Maximising a line that costs four calories to close
    an energy deficit is the optimiser working, not an attractor, and failing the gate for
    it would have pushed someone to weaken the optimiser to satisfy a number.

    So the assertion says what the docstring says: nothing is stuck at its minimum.
    """
    assert not diet_quality.floor_pinned_foods, (
        "a food is served at the smallest amount it has, every single time: "
        + "; ".join(diet_quality.floor_pinned_foods))
    # And ceiling-pinning stays the exception. It is legitimate, but a plan where most
    # lines are maxed out is a plan that cannot reach its target any other way.
    served_often = max(1, diet_quality.portions_measured // 8)
    assert len(diet_quality.ceiling_pinned_foods) <= served_often, (
        f"{len(diet_quality.ceiling_pinned_foods)} foods are always served at their "
        f"maximum: {diet_quality.ceiling_pinned_foods[:6]}")


def test_calorie_drift_stays_bounded(diet_quality):
    """Round-up steps, floors firing after the target is met, and residual filling all
    push one way. Noise would fall both ways, so this is a bias with a cause.

    The bound is deliberately loose. Drift is noisy today for two reasons that P4
    removes: the planner seeds its RNG from the user id, and portions are continuous
    grams so a single rounding decision moves the total. Observed range across runs and
    catalogues is +6.2% to +10.9%. What this guards is a regression to something much
    worse, not the exact figure.

    Tightens in P4 to <= 2% and drift_all_one_sided False, at which point it becomes a
    real assertion rather than a tripwire."""
    assert diet_quality.drift_worst_abs <= 5.0, \
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
    assert breakfast >= 0.60, f"breakfast dish rate fell to {breakfast:.0%}"
    # 0.72 after P7.5 moved protein to grams per kilogram and the chicken-heavy dinners
    # stopped fitting. 0.70 after P9.3 began judging the two paths by one rule: when a
    # plate built from the client's own foods and a library dish both fit, and only the
    # plate contains what they chose, the plate wins. That is the product, and it moved
    # chosen-ingredient share from 0.10 to 0.23 and unchanged-after-choosing from 27 to
    # 17 in the same measurement. P9.4 lets a dish absorb a chosen food; this rises then.
    assert diet_quality.dish_rate_overall >= 0.72, \
        f"overall dish rate fell to {diet_quality.dish_rate_overall:.0%}"


def test_the_recorded_baseline_still_describes_this_engine(diet_quality):
    """Guards the harness itself. If the catalogue changes underneath it the recorded
    baseline stops meaning anything, and every comparison above is against a number
    that no longer applies."""
    import json
    from pathlib import Path

    from diet.models import FoodItem, Recipe

    baseline = json.loads((Path(__file__).parent / "diet_quality_baseline.json").read_text())
    # EQUAL, not "at least". `>=` caught a catalogue that shrank and waved through one
    # that grew — and growing it raises dish rate, distinct dishes, chosen-ingredient
    # share and preference satisfaction with no engine change at all, so content work
    # would launder as engine improvement while this guard stayed green.
    assert FoodItem.objects.filter(needs_review=False).count() == \
        baseline["catalogue"]["foods"], (
            "the catalogue is not the one the baseline was recorded against; "
            "re-record it deliberately")
    assert Recipe.objects.filter(is_active=True).count() == \
        baseline["catalogue"]["recipes"], (
            "the recipe library is not the one the baseline was recorded against; "
            "re-record it deliberately")


def test_the_two_seed_commands_agree_on_food_names(seeded_catalogue, db):
    """A fresh production database cannot build most of its own recipes.

    `diet/data/catalogue.py` names the foods and `seed_recipes` writes recipes that name
    their ingredients, and the two used to disagree: the catalogue has "Chicken Breast
    (Grilled)", "Salmon Fillet", "Greek Yogurt (Non-Fat)", "Sweet Potato (Baked)",
    "Extra Virgin Olive Oil" and "Lentils (Cooked)", while the recipes ask for "Chicken
    Breast", "Salmon", "Greek Yogurt", "Sweet Potato", "Olive Oil" and "Lentils".

    (The `add_healthy_foods` command this was originally written against is gone. It wrote
    a hundred unvetted foods with no units, no meal slots and no allergen composition,
    duplicating 69 names the curated catalogue already held, and created its own `Dairy`
    and `Legumes` categories flagged `is_protein=True` — which `auto_assign_category`
    resolves with `filter(is_protein=True).first()`, so every imported protein was filed
    as dairy.)

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
            if not FoodItem.objects.filter(name_en__iexact=food_name).exists()
            and not FoodItem.objects.filter(name_en__icontains=food_name).exists()
        ]
        if missing:
            unresolvable[name] = missing

    seeded = Recipe.objects.filter(is_active=True).count()
    assert seeded == len(RECIPES), f"only {seeded} of {len(RECIPES)} recipes survived seeding"
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
    # 0.85 on the 136-food catalogue: more foods, more ladders, and a few constructed

    # minima now need three moves. Phase B's exhaustive portioning closes those.

    assert escaped >= stalled * 0.85, (f"paired moves escaped only {escaped} of {stalled} two-move local minima")


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
    # The denominator first. `optimal_share` is judged over feasible, non-skipped meals,
    # and both exclusions are things a REGRESSION produces: a meal too large to enumerate
    # is dropped, and a meal that misses tolerance everywhere is filed infeasible. An
    # engine that put more foods on every plate, or missed tolerance more often, would
    # remove its own hardest cases from the denominator and the share would RISE.
    measured = len(report.feasible) + len(report.infeasible) + report.skipped_too_large
    assert len(report.feasible) >= 0.8 * measured, (
        f"only {len(report.feasible)} of {measured} meals were judged: "
        f"{len(report.infeasible)} infeasible, {report.skipped_too_large} too large to "
        f"enumerate — the share below is measured over what is left")
    assert report.optimal_share >= 0.95, (
        f"only {report.optimal_share:.0%} of feasible meals are at the proven optimum "
        f"({len(report.off_optimum)} of {len(report.feasible)} off it); "
        f"worst objective gap {report.worst_objective_gap:.3f}")
    # The SHARE is the gate, and it is tightened here from 0.90 to 0.95 because the
    # engine now reaches 0.96. The worst single gap is not a useful bound on its own:
    # an augmented dish has five lines and the pair search was proven on four, so one
    # such meal sits further out than the rest and the number moves with whichever
    # foods the client's choices put on that plate. What must not happen is that
    # becoming common — so the rule is "at most one meal beyond 0.25", which is
    # strictly stronger than the old single-number bound it replaces.
    far = [m for m in report.feasible if m.objective_gap > 0.25]
    assert len(far) <= 1, (
        f"{len(far)} meals sit more than 0.25 from their proven optimum: "
        + "; ".join(f"{m.dish} {m.objective_gap:.3f}" for m in far[:4]))
    assert report.worst_objective_gap <= 0.35, (
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

# ---------------------------------------------------------------------------
# Hard constraints: what a client may not eat, on every path
# ---------------------------------------------------------------------------

def test_every_ordinary_phrasing_of_a_nut_allergy_blocks_peanuts(seeded_catalogue):
    """Peanut is a legume and carries its own tag, so a vocabulary that reads "nuts" as
    tree nuts alone leaves the most common and most dangerous nut allergy unprotected.

    The synonym table mapped one term to one tag, so whoever wrote it had to choose, and
    a client who typed "nut allergy" was served peanut butter. An ambiguous everyday
    word must widen to everything it could mean.
    """
    from diet.allergens import parse_user_allergies

    for phrase in ("nuts", "nut", "nut allergy", "allergic to nuts", "nut free",
                   "no nuts", "mixed nuts", "nut intolerance"):
        tags = parse_user_allergies(phrase)
        assert "peanut" in tags, f"{phrase!r} does not protect against peanuts: {tags}"
        assert "tree_nut" in tags, f"{phrase!r} does not protect against tree nuts: {tags}"

    # Specific phrasings stay specific.
    assert parse_user_allergies("peanuts") == {"peanut"}
    assert parse_user_allergies("tree nuts") == {"tree_nut"}
    # "Seafood" and "shellfish" are wider in speech than in the vocabulary.
    assert parse_user_allergies("seafood") == {"fish", "shellfish", "mollusc"}
    assert "mollusc" in parse_user_allergies("shellfish")


def test_a_nut_butter_is_not_dairy(seeded_catalogue):
    """"Butter" is a milk marker and peanut butter contains no milk.

    Over-blocking is the safe direction and it is still wrong: a client avoiding dairy
    lost both nut butters in the catalogue, which are the only spoonable fats it has.
    """
    from diet.allergens import infer_allergens

    assert "milk" not in infer_allergens("Peanut Butter (Natural)")
    assert "milk" not in infer_allergens("Almond Butter")
    assert "milk" not in infer_allergens("Almond Milk")
    assert "peanut" in infer_allergens("Peanut Butter (Natural)")
    assert "tree_nut" in infer_allergens("Almond Butter")
    # A real dairy food is still caught.
    assert "milk" in infer_allergens("Butter (Salted)")
    assert "milk" in infer_allergens("Greek Yogurt (Non-Fat)")


def test_a_disliked_food_never_reaches_the_plate(seeded_catalogue):
    """Dislikes were enforced in the pool and nowhere else.

    Three quarters of meals come from the recipe library, and `find_recipe` checked
    allergies and not dislikes, so a dish built on a rejected food was served — and
    persistence then refused the finished plan outright rather than producing one
    without it, so the client got an error instead of dinner.
    """
    from diet.models import FoodItem, UserFoodPreference
    from diet.services.diet_persistence import DietPersistenceService
    from diet.services.rule_based_planner import RuleBasedPlanner

    from tests.diet_quality import PROFILES, _make_client

    names = ["Chicken Breast (Grilled)", "White Rice (Cooked)"]
    user = _make_client("dislike-gate", *PROFILES[2])
    pref, _ = UserFoodPreference.objects.get_or_create(user=user)
    pref.disliked_foods.set(FoodItem.objects.filter(name__in=names))

    out = RuleBasedPlanner(user, seed_salt="dislike-gate").generate(
        daily_kcal=2200, duration_days=5, snack_count=1)
    served = {i.name for m in out.plan for i in m.ingredients}
    assert not (served & set(names)), f"disliked food served: {sorted(served & set(names))}"
    assert len(out.plan) == 20, "the client must still get a full plan, not a refusal"
    DietPersistenceService(user).save_plan(out, meal_count=3, snack_count=1)


def test_an_allergen_never_reaches_the_plate(seeded_catalogue):
    from diet.models import UserFoodPreference
    from diet.services.rule_based_planner import RuleBasedPlanner

    from tests.diet_quality import PROFILES, _make_client

    banned = {
        "nut allergy": ("almond", "walnut", "pecan", "cashew", "pistachio",
                        "macadamia", "peanut"),
        "seafood": ("salmon", "tuna", "cod", "shrimp", "tilapia", "halibut",
                    "sea bass", "mackerel", "sardine", "trout"),
        "dairy": ("yogurt", "cheese", "milk", "labneh", "cottage"),
    }
    for index, (phrase, words) in enumerate(banned.items()):
        user = _make_client(f"allergy-gate-{index}", *PROFILES[2])
        _p, _ = UserFoodPreference.objects.get_or_create(user=user)
        _p.allergies = phrase; _p.save(update_fields=["allergies"])
        out = RuleBasedPlanner(user, seed_salt=f"allergy{index}").generate(
            daily_kcal=2200, duration_days=5, snack_count=1)
        served = [i.name for m in out.plan for i in m.ingredients]
        hits = sorted({n for n in served if any(w in n.lower() for w in words)})
        assert not hits, f"declared {phrase!r} and was served {hits}"
        assert len(out.plan) == 20


# ---------------------------------------------------------------------------
# One objective, one arithmetic, one measurement
# ---------------------------------------------------------------------------

def test_the_engine_has_exactly_one_notion_of_better(seeded_catalogue):
    """Two measures of "which meal is better" always drift apart.

    This engine has made the mistake twice: a duplicated macro-ratio table, where the
    planner aimed at one target while the optimiser judged another, and then a second
    scoring function in the portion solver weighting calories at 2.0 against the
    engine's 0.5. Six meals in every three hundred came out provably off the optimum for
    that reason alone.
    """
    import diet.planner.portion as portion

    assert not hasattr(portion, "deviation"), (
        "portion.py has its own scoring function again; the objective lives in report.py")

    # And one arithmetic. `calories` and `calories_per_gram` are separate stored columns
    # reconciled only when one is zero, so two implementations could add the same meal
    # up and disagree.
    from diet.models import FoodItem
    from diet.planner.optimize import totals_of
    from diet.planner.report import totals_of as canonical

    assert totals_of is canonical
    food = FoodItem.objects.filter(needs_review=False).first()
    by_portion = portion.totals([portion.Portion(food, 100.0)])
    by_components = canonical([(food, 100.0)])
    assert by_portion == by_components


def test_the_quality_harness_reports_the_same_numbers_twice(seeded_catalogue):
    """A measurement that moves on its own cannot attribute a change to anything.

    The harness created a fresh client per run and the planner seeds each day's
    generator from the client's row id, so every number was one draw from a
    distribution. Calorie drift read 1.2% and 9.2% on consecutive runs of identical
    code, and the recorded baseline was whichever draw happened that day.
    """
    from tests.diet_quality import PROFILES, measure

    first = measure(PROFILES, days=3)
    second = measure(PROFILES, days=3)
    assert first.as_dict() == second.as_dict(), "the quality harness is not deterministic"


# ---------------------------------------------------------------------------
# A plan says what it delivers
# ---------------------------------------------------------------------------

def test_a_plan_that_cannot_reach_its_target_says_so(seeded_catalogue):
    """A day is capped by what its meals can hold, and that cap is a fact about food.

    Three meals and a snack top out near 3,700 kcal because every portion is bounded by
    an amount a person would serve. A 5,000 kcal request returned a 3,900 kcal plan,
    correct in every other respect, labelled 5,000 and silent about the shortfall.
    """
    from diet.services.rule_based_planner import RuleBasedPlanner

    from tests.diet_quality import PROFILES, _make_client

    user = _make_client("delivery-gate", *PROFILES[4])
    planner = RuleBasedPlanner(user, seed_salt="delivery")

    reachable = planner.generate(daily_kcal=2200, duration_days=1, snack_count=1)
    assert reachable.plan_metadata["delivery"]["met"] is True

    impossible = RuleBasedPlanner(user, seed_salt="delivery").generate(
        daily_kcal=5000, duration_days=1, snack_count=1)
    delivery = impossible.plan_metadata["delivery"]
    assert delivery["met"] is False
    assert delivery["deviation"] < -0.1
    assert "reason" in delivery


def test_a_slot_with_no_suitable_shape_falls_back_instead_of_inventing_one(seeded_catalogue):
    """`or list(templates)` inverted the meal-type restriction it had just applied.

    When nothing suited the slot the code used every shape instead of none, so a library
    with no snack recipes would serve a lunch at snack. The caller already has a
    fallback; assembly is a better answer than a confidently wrong one.
    """
    from diet.planner.templates import derive_templates, plan_meal

    lunch_only = [t for t in derive_templates() if t.meals and "Snack" not in t.meals]
    assert lunch_only, "fixture needs at least one slot-restricted shape"
    portions, _score, template = plan_meal(
        "Snack", None, {"calories": 200, "protein": 15, "carb": 25, "fat": 4.4},
        templates=lunch_only, edges={})
    assert portions == [] and template is None



# ---------------------------------------------------------------------------
# P0 — food identity survives persistence
# ---------------------------------------------------------------------------

def test_the_food_the_planner_chose_is_the_food_that_is_saved(seeded_catalogue):
    """The planner constraint-filters and ranks a FoodItem, then used to hand persistence
    a name string. Persistence re-resolved it: exact name, then a fuzzy scan over the whole
    table, then a brand-new row. With 23 duplicate names in the database, the row on the
    plate was not provably the row the planner cleared. Now the DTO carries the id and the
    grams, and persistence uses nothing else.
    """
    from diet.models import FoodItem, MealComponent
    from diet.services.diet_persistence import DietPersistenceService
    from diet.services.rule_based_planner import RuleBasedPlanner

    from tests.diet_quality import PROFILES, _make_client

    user = _make_client("identity-gate", *PROFILES[2])
    out = RuleBasedPlanner(user, seed_salt="identity").generate(
        daily_kcal=2200, duration_days=2, snack_count=1)

    planned = []
    for meal in out.plan:
        for ing in meal.ingredients:
            assert ing.food_id is not None, f"{ing.name} left the planner without an id"
            assert ing.grams is not None, f"{ing.name} left the planner without grams"
            planned.append((ing.food_id, round(float(ing.grams), 1)))
    assert all(m.recipe_id or m.shape for m in out.plan), "every meal carries provenance"

    before = FoodItem.objects.count()
    plan = DietPersistenceService(user).save_plan(out, meal_count=3, snack_count=1)
    assert FoodItem.objects.count() == before, "persistence must never invent a food row"

    saved = sorted((c.food_id, round(float(c.quantity), 1))
                   for c in MealComponent.objects.filter(meal__diet_plan=plan))
    # converge_plan may re-portion on the ladder, so compare identity exactly and amount
    # only up to being on the same food's ladder.
    assert sorted(f for f, _ in saved) == sorted(f for f, _ in planned)
    from diet.planner.portion import portions_for
    by_id = {f.id: f for f in FoodItem.objects.filter(id__in={f for f, _ in saved})}
    for food_id, grams in saved:
        rungs = [p.grams for p in portions_for(by_id[food_id])]
        assert any(abs(r - grams) < 0.6 for r in rungs), \
            f"{by_id[food_id].name} saved at {grams} g, off its ladder {rungs}"


def test_two_rows_for_one_food_in_one_meal_cannot_exist(seeded_catalogue):
    from django.db import IntegrityError, transaction

    from diet.models import DietPlan, FoodItem, Meal, MealComponent

    from tests.diet_quality import PROFILES, _make_client

    user = _make_client("dupe-gate", *PROFILES[2])
    plan = DietPlan.objects.create(user=user, goal="Maintain", daily_calories=2000.0,
                                   start_date="2026-09-05", end_date="2026-09-05")
    meal = Meal.objects.create(diet_plan=plan, meal_type="Lunch", date="2026-09-05")
    food = FoodItem.objects.filter(needs_review=False).first()
    MealComponent.objects.create(meal=meal, food=food, quantity=100.0)
    with pytest.raises(IntegrityError), transaction.atomic():
        MealComponent.objects.create(meal=meal, food=food, quantity=50.0)


# ---------------------------------------------------------------------------
# P1 — the core feature: liked foods, by meal and macro, and the cuisine a client chose
# ---------------------------------------------------------------------------


def _grant_diet_access(user):
    """A live subscription whose plan carries diet access, the way production gates it."""
    from datetime import timedelta

    from django.utils import timezone

    from django.utils import translation

    from subscription.models import Subscription, SubscriptionPlan

    # Under modeltranslation `name` resolves to the ACTIVE language's column, and the
    # test client leaves the language a request activated; after an Arabic request the
    # lookup missed the English row and the create collided on the unique name.
    with translation.override("en"):
        plan, _ = SubscriptionPlan.objects.get_or_create(
            name="gate_diet_plan",
            defaults=dict(plan_type="premium", description="gate", price="5000.00",
                          currency="SYP", duration_days=30, is_active=True,
                          has_diet_access=True, has_routine_access=True,
                          has_challenges_access=True, has_ai_advice=True,
                          has_priority_support=True))
    return Subscription.objects.create(
        user=user, plan=plan, status="active",
        end_date=timezone.now() + timedelta(days=30))


def test_one_food_can_be_chosen_for_several_meals(seeded_catalogue):
    """405 categorisation rows in the old database, 405 distinct (user, food) pairs, zero
    in more than one slot. The write endpoint was keyed on (user, food), so the second
    slot overwrote the first and "chicken is my lunch protein AND my dinner protein" had
    never once existed.
    """
    from diet.models import FoodItem, UserFoodCategoryPreference, UserFoodPreference

    from tests.diet_quality import PROFILES, _make_client

    user = _make_client("slots-gate", *PROFILES[2])
    _grant_diet_access(user)
    chicken = FoodItem.objects.get(name="Chicken Breast (Grilled)")
    from django.test import Client
    from rest_framework_simplejwt.tokens import RefreshToken
    c = Client()
    c.defaults["HTTP_AUTHORIZATION"] = f"Bearer {RefreshToken.for_user(user).access_token}"

    for meal in ("Lunch", "Dinner"):
        r = c.post("/api/diet/v1/preferences/food-category/", {"food_id": chicken.id, "meal": meal,
                                                   "macro": "protein"},
                   content_type="application/json")
        assert r.status_code in (200, 201), r.content

    rows = UserFoodCategoryPreference.objects.filter(user=user, food=chicken)
    assert sorted(rows.values_list("meal", flat=True)) == ["Dinner", "Lunch"]
    # Categorising implies liking; the client no longer has to do both.
    assert UserFoodPreference.objects.get(user=user).liked_foods.filter(pk=chicken.pk).exists()


def test_a_client_can_set_every_preference_list_and_the_cuisine_ratio(seeded_catalogue):
    from diet.models import FoodItem, UserFoodPreference

    from tests.diet_quality import PROFILES, _make_client

    user = _make_client("lists-gate", *PROFILES[2])
    _grant_diet_access(user)
    from django.test import Client
    from rest_framework_simplejwt.tokens import RefreshToken
    c = Client()
    c.defaults["HTTP_AUTHORIZATION"] = f"Bearer {RefreshToken.for_user(user).access_token}"
    salmon = FoodItem.objects.get(name="Salmon Fillet")
    rice = FoodItem.objects.get(name="White Rice (Cooked)")
    r = c.post("/api/diet/v1/preferences/", {"protein_choices": [salmon.id],
                                          "carb_choices": [rice.id], "local_ratio": 0.8},
               content_type="application/json")
    assert r.status_code == 200, r.content
    pref = UserFoodPreference.objects.get(user=user)
    assert list(pref.protein_choices.values_list("id", flat=True)) == [salmon.id]
    assert pref.local_ratio == 0.8
    body = c.get("/api/diet/v1/preferences/").json()
    assert "vegetable_choices" in body and "fruit_choices" in body
    assert body["local_ratio"] == 0.8
    assert c.post("/api/diet/v1/preferences/", {"local_ratio": 1.7},
                  content_type="application/json").status_code == 400


def test_the_cuisine_ratio_is_honoured_on_every_path(seeded_catalogue):
    """0.0 is Western only, 1.0 is Levantine only. Universal foods are always eligible.
    At the ends the other cuisine is excluded outright; between, it is weighted.
    """
    from diet.models import FoodItem, UserFoodPreference
    from diet.services.rule_based_planner import RuleBasedPlanner

    from tests.diet_quality import PROFILES, _make_client

    western = set(FoodItem.objects.filter(cuisine="western").values_list("name", flat=True))
    levantine = set(FoodItem.objects.filter(cuisine="levantine").values_list("name", flat=True))
    assert western and levantine

    served = {}
    for ratio in (0.0, 1.0):
        user = _make_client(f"cuisine-gate-{int(ratio)}", *PROFILES[2])
        _p, _ = UserFoodPreference.objects.get_or_create(user=user)
        _p.local_ratio = ratio; _p.save(update_fields=["local_ratio"])
        out = RuleBasedPlanner(user, seed_salt="cuisine").generate(
            daily_kcal=2200, duration_days=5, snack_count=1)
        served[ratio] = {i.name for m in out.plan for i in m.ingredients}
        assert len(out.plan) == 20

    assert not (served[0.0] & levantine), f"Western-only got Levantine: {served[0.0] & levantine}"
    assert not (served[1.0] & western), f"Levantine-only got Western: {served[1.0] & western}"


# ---------------------------------------------------------------------------
# P4 — the target a plan was built to is the target it is saved and converged to
# ---------------------------------------------------------------------------

def test_a_plan_is_saved_and_converged_to_the_target_it_was_built_for(seeded_catalogue):
    """Persistence recomputed daily_calories from the profile, so a plan generated at
    1,400 kcal was stored as the client's 2,695 kcal TDEE and converge_plan then pushed
    every meal toward the wrong number: +63% over the request. Found the moment the
    quality harness started measuring persisted rows instead of the in-memory plan.
    """
    from diet.planner.report import totals_of

    from tests.diet_quality import PROFILES, _components, _generate, _make_client, _meals_of

    user = _make_client("target-gate", *PROFILES[2])
    assert user.calculate_daily_calories() > 2000, "fixture must have a TDEE far from 1400"
    plan = _generate(user, 1400, 1, "target-gate")
    assert plan is not None
    assert abs(plan.daily_calories - 1400) < 1.0, f"stored {plan.daily_calories}, built for 1400"
    produced = sum(totals_of(_components(m))["calories"] for m in _meals_of(plan))
    assert abs(produced / 1400 - 1) <= 0.10, f"persisted day is {produced:.0f} kcal for a 1400 request"
    # And the persisted macro targets describe the same plan.
    assert plan.target_protein and plan.target_carbs and plan.target_fat
    kcal_from_targets = plan.target_protein * 4 + plan.target_carbs * 4 + plan.target_fat * 9
    assert abs(kcal_from_targets / 1400 - 1) <= 0.02


def test_plan_time_and_converge_time_targets_are_the_same_numbers(seeded_catalogue):
    """Calories agreeing is not enough. After protein moved to grams per kilogram, the
    planner built toward one macro split and converge_plan re-optimised toward another,
    and the calorie-only check above passed while 114 of 144 meals drifted off their
    optimum. Every per-meal gram must match, from the one function both read.
    """
    from diet.planner.policy import load_policy
    from diet.planner.targets import compute_targets
    from diet.services.rule_based_planner import PlannerContext, RuleBasedPlanner

    from tests.diet_quality import PROFILES, _make_client

    user = _make_client("bind-gate", *PROFILES[2])
    planner = RuleBasedPlanner(user, seed_salt="bind")
    ctx = PlannerContext(goal=planner._resolve_goal(), allowed_map={})
    day = planner._prepare_day(ctx=ctx, current_date=None, daily_kcal=2200, snack_count=1,
                               meals=["Breakfast", "Lunch", "Dinner"], used_in_window={},
                               no_repeat_days=3)
    converge_side = {t.name: t for t in compute_targets(
        2200, load_policy(ctx.goal), ["Breakfast", "Lunch", "Dinner"], 1,
        weight_kg=user.weight).meals}
    for name, mine in day.meal_targets.items():
        theirs = converge_side[name]
        assert abs(mine.kcal_target - theirs.calories) < 0.5, name
        for macro, key in (("protein", "protein"), ("carb", "carb"), ("fat", "fat")):
            assert abs(mine.macro_targets[macro] - getattr(theirs, key)) < 0.5, \
                f"{name} {macro}: planner {mine.macro_targets[macro]:.1f} vs converge {getattr(theirs, key):.1f}"



# ---------------------------------------------------------------------------
# P4.3 — the goal metrics are ratchets, not observations
# ---------------------------------------------------------------------------

def test_the_goal_metrics_never_move_the_wrong_way(diet_quality):
    """chosen_ingredient_share and twin_identical_meals were recorded in the baseline and
    asserted by nothing. Locking the gate without them would have ratified "choosing your
    food changes nothing" as the accepted state, and every later change to selection
    would have been graded by a gate that could not see the product goal.

    Ratchets against the recorded baseline: each may only move in the direction of
    improvement. To move the floor, re-record the baseline deliberately.
    """
    import json
    from pathlib import Path

    recorded = json.loads(
        (Path(__file__).parent / "diet_quality_baseline.json").read_text())["measured"]
    # The baseline is recorded to four places, so "not lower" means not lower than the
    # recorded figure less half a unit of that precision. A 1e-9 slack failed the gate
    # on the very measurement the baseline was recorded from.
    assert diet_quality.chosen_ingredient_share >= recorded["chosen_ingredient_share"] - 5e-5, (
        f"chosen-ingredient share fell: {diet_quality.chosen_ingredient_share:.3f} "
        f"< recorded {recorded['chosen_ingredient_share']:.3f}")
    # The meals the engine BUILDS must all change when the client chooses. A library
    # dish that is the best fit for a slot both before and after is not a
    # personalisation failure, so the overall twin count is a bound and the built-meal
    # count is the invariant.
    assert diet_quality.twin_identical_built == 0, (
        f"{diet_quality.twin_identical_built} engine-built meals ignored the client's choices")
    assert diet_quality.twin_identical_meals <= recorded["twin_identical_meals"], (
        f"more meals unchanged after choosing: {diet_quality.twin_identical_meals} "
        f"> recorded {recorded['twin_identical_meals']}")
    assert diet_quality.distinct_dishes >= recorded["distinct_dishes"]
    assert diet_quality.absurd_portion_rate == 0.0
    assert diet_quality.days_repeating_a_dish == 0
    # A crash inside generation is recorded here by the harness; it is never a metric.
    assert diet_quality.catalogue_gaps == [], diet_quality.catalogue_gaps
    # D1: recall. Of the foods the chooser picked for a slot, the share that reached
    # that slot's plate at least once in the week. Chosen share is the precision side.
    assert diet_quality.preference_satisfaction >= recorded["preference_satisfaction"] - 5e-5, (
        f"preference satisfaction fell: {diet_quality.preference_satisfaction:.3f} "
        f"< recorded {recorded['preference_satisfaction']:.3f}")
    # C: no client is served one dish more often in a week than the recorded worst.
    assert diet_quality.max_repeats_per_client_week <= recorded["max_repeats_per_client_week"], (
        f"a dish was served {diet_quality.max_repeats_per_client_week} times in one client's week")


# ---------------------------------------------------------------------------
# P9.2 — one scorer for both paths, and the six rows it had to pass first
# ---------------------------------------------------------------------------

def test_the_recipe_scorer_passes_its_own_gate(seeded_catalogue):
    """The plan required these distributions measured BEFORE the unified scorer was
    wired. Two drafts failed: a plain calorie-share mean diluted a chosen food's bonus to
    its share of the dish (P(chosen wins) 0.13), and a plain presence max counted a chosen
    garnish like a chosen anchor (olive oil +100). Structure by calorie share, preference
    by presence weighted by role, pairing bounded, is what passed. This keeps it passing.
    """
    import statistics as st

    from diet.models import FoodItem, Recipe, UserFoodCategoryPreference
    from diet.planner.candidates import build_pool, classify_food
    from diet.planner.policy import load_policy
    from diet.planner.recipes import _recipe_lines, find_recipe, recipe_pool_score, softmax_weights
    from diet.planner.targets import compute_targets
    from diet.planner.templates import SOFTMAX_T, W_PAIRING, _affinity, pairing_edges

    from tests.diet_quality import PROFILES, _make_client

    pol = load_policy("maintain")
    edges = pairing_edges()
    recipes = list(Recipe.objects.filter(is_active=True).prefetch_related("ingredients__food__category"))
    plain = build_pool(_make_client("scorer-plain", *PROFILES[2]), pol)

    # T2: no penalty for a complete dish.
    by_n = {}
    for r in recipes:
        by_n.setdefault(len(_recipe_lines(r)), []).append(
            recipe_pool_score(_recipe_lines(r), plain, (r.meal_types or ["Lunch"])[0], edges))
    means = [st.mean(v) for _n, v in sorted(by_n.items())]
    assert all(b >= a - 5 for a, b in zip(means, means[1:])), f"score falls with ingredient count: {means}"

    # T3: a chosen staple moves the dish far more than a chosen garnish.
    dish = next(r for r in recipes
                if any("Chicken" in l.food.name for l in r.ingredients.all())
                and any("Olive Oil" in l.food.name for l in r.ingredients.all()))
    lines = _recipe_lines(dish)
    base = recipe_pool_score(lines, plain, "Lunch", edges)
    def lift(name):
        u = _make_client(f"scorer-{name.split()[0].lower()}", *PROFILES[2])
        f = next(l[0] for l in lines if name in l[0].name)
        UserFoodCategoryPreference.objects.get_or_create(user=u, food=f, meal="Lunch", macro=classify_food(f))
        return recipe_pool_score(lines, build_pool(u, pol), "Lunch", edges) - base
    chicken, oil = lift("Chicken"), lift("Olive Oil")
    assert chicken >= 90, f"choosing the anchor lifted the dish only +{chicken:.0f}"
    assert oil <= 0.4 * chicken, f"choosing the garnish lifted the dish +{oil:.0f} vs anchor +{chicken:.0f}"

    # T4: pairing never outweighs a choice.
    pairing_max = max(W_PAIRING * _affinity(f.id, [i for i in [l[0].id for l in _recipe_lines(rr)] if i != f.id], edges)
                      for rr in recipes for f, _g, _s in _recipe_lines(rr))
    assert pairing_max <= 20, f"pairing term reaches {pairing_max:.1f} against a 100-point choice"

    # T5, the informative case: chicken chosen for lunch, rice NOT. Among the fitting
    # lunch dishes, the ones with chicken must win at this temperature; the ones
    # without (salmon, lentils) must not.
    ch = _make_client("scorer-chooser", *PROFILES[2])
    chicken_row = FoodItem.objects.get(name="Chicken Breast (Grilled)")
    UserFoodCategoryPreference.objects.get_or_create(user=ch, food=chicken_row, meal="Lunch", macro="protein")
    pool_c = build_pool(ch, pol)
    lunch = next(t for t in compute_targets(2695, pol, ["Breakfast", "Lunch", "Dinner"], 1,
                                            weight_kg=ch.weight).meals if t.name == "Lunch")
    within = [(rr, recipe_pool_score(_recipe_lines(rr), pool_c, "Lunch", edges))
              for rr in recipes if (not rr.meal_types or "Lunch" in rr.meal_types)
              and (m := find_recipe("Lunch", lunch.as_dict(), pol, recipes=[rr]))
              and m.deviation.within(pol.tolerance)]
    has = [any(l[0].id == chicken_row.id for l in _recipe_lines(rr)) for rr, _ in within]
    assert any(has) and not all(has), "fixture must contain fitting dishes with AND without chicken"
    w = softmax_weights([s for _r, s in within], SOFTMAX_T)
    p_chosen = sum(wi for wi, h in zip(w, has) if h) / sum(w)
    # 0.80, calibrated on the 76-recipe library: eighteen lunch dishes now fit where five
    # did, and the non-chosen mass grows with their number even as each one stays far
    # below a chosen dish. The argmax must still be a chosen dish.
    assert has[max(range(len(w)), key=w.__getitem__)], "the most likely dish does not contain the choice"
    assert 0.80 <= p_chosen <= 0.999, f"P(chosen dish wins) = {p_chosen:.2f}"


# ---------------------------------------------------------------------------
# P11 — the signal learning needs is collected
# ---------------------------------------------------------------------------

def test_a_client_can_swap_a_food_and_learning_hears_it(seeded_catalogue):
    """learning.py read fields nothing wrote; zero consumption rows existed. A swap is
    the strongest preference signal a client will ever give — they rejected one food and
    named another — and it is free. The endpoint offers similarity neighbours, swaps at
    the same energy on the food's own ladder, records a MealSwap, and learning reads it
    as a refusal of one and a like of the other.
    """
    from django.test import Client
    from rest_framework_simplejwt.tokens import RefreshToken

    from diet.models import FoodItem, MealComponent, MealSwap
    from diet.planner.learning import observations_for
    from diet.planner.portion import portions_for
    from diet.services.diet_persistence import DietPersistenceService
    from diet.services.rule_based_planner import RuleBasedPlanner

    from tests.diet_quality import PROFILES, _make_client

    user = _make_client("swap-gate", *PROFILES[2])
    _grant_diet_access(user)
    out = RuleBasedPlanner(user, seed_salt="swap").generate(daily_kcal=2200, duration_days=1, snack_count=1)
    plan = DietPersistenceService(user).save_plan(out, meal_count=3, snack_count=1)
    component = MealComponent.objects.filter(meal__diet_plan=plan).select_related("food").first()
    assert component is not None

    c = Client()
    c.defaults["HTTP_AUTHORIZATION"] = f"Bearer {RefreshToken.for_user(user).access_token}"
    url = "/api/diet/v1/client/meals/interact/"

    offers = c.post(url, {"action": "swap_food", "component_id": component.id},
                    content_type="application/json")
    assert offers.status_code == 200, offers.content
    offered = offers.json()["offers"]
    assert offered and all(o["similarity"] >= 0.5 for o in offered)
    chosen_id = offered[0]["food_id"]
    assert chosen_id != component.food_id

    rejected_id = component.food_id
    done = c.post(url, {"action": "swap_food", "component_id": component.id, "chosen_food_id": chosen_id},
                  content_type="application/json")
    assert done.status_code == 200, done.content
    component.refresh_from_db()
    assert component.food_id == chosen_id
    rungs = [p.grams for p in portions_for(component.food)]
    assert any(abs(r - float(component.quantity)) < 0.6 for r in rungs), "swapped portion is off its ladder"

    swap = MealSwap.objects.get(user=user, meal=component.meal)
    assert (swap.rejected_food_id, swap.chosen_food_id) == (rejected_id, chosen_id)
    # D2: swap distance, stored at write time. High means the food was fine and the
    # client wanted a variant; low means the shape was wrong for them.
    assert swap.similarity is not None and 0.0 <= swap.similarity <= 1.0
    assert abs(swap.similarity - offered[0]["similarity"]) < 0.02

    stats = observations_for(user)
    assert stats[rejected_id]["refused"] >= 1 and stats[chosen_id]["liked"] >= 1

    # A food already on the plate cannot be swapped in twice.
    other = MealComponent.objects.filter(meal=component.meal).exclude(pk=component.pk).first()
    if other is not None:
        dup = c.post(url, {"action": "swap_food", "component_id": other.id, "chosen_food_id": chosen_id},
                     content_type="application/json")
        assert dup.status_code == 409

    # Per-food rating outranks the meal's.
    rated = c.post(url, {"action": "rate_component", "component_id": component.id, "is_liked": False},
                   content_type="application/json")
    assert rated.status_code == 200, rated.content
    component.refresh_from_db()
    assert component.is_liked is False
    assert observations_for(user)[chosen_id]["disliked"] >= 1


# ---------------------------------------------------------------------------
# Phase A — the recipe path is alive, and the gate can tell when it is not
# ---------------------------------------------------------------------------

#: The repository, found from THIS file. Three source scans used `pathlib.Path(".")`,
#: which is the working directory: run pytest from anywhere but the repo root and the
#: glob matched nothing, `offenders` was empty and the test passed green having read
#: zero files.
REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent


def _repo_python_files():
    """Every source file in the project, excluding what is not ours to police."""
    skip = {"__pycache__", ".venv", "_excluded", "node_modules", ".git", "migrations"}
    found = []
    for path in REPO_ROOT.rglob("*.py"):
        if skip & set(path.parts):
            continue
        found.append(path)
    assert len(found) > 100, f"only {len(found)} source files found under {REPO_ROOT}"
    return found


def _chooser(name):
    """A client who filled in the meal-and-macro picker, as the harness does."""
    from diet.models import UserFoodCategoryPreference
    from tests.diet_quality import PROFILES, SLOT_CHOICES, _make_client, _resolve_food
    user = _make_client(name, *PROFILES[2])
    chosen = collections.defaultdict(set)
    for meal, macro, names in SLOT_CHOICES:
        for food_name in names:
            food = _resolve_food(food_name)
            if food is not None:
                UserFoodCategoryPreference.objects.get_or_create(
                    user=user, food=food, meal=meal, macro=macro)
                chosen[meal].add(food.id)
    return user, chosen


def test_the_gate_runs_the_planner_strict(seeded_catalogue, monkeypatch):
    """A crash inside a path must FAIL the gate, not become "no recipe fits".

    `for index in range(steps)` in `find_recipe` overwrote the similarity index with an
    int; every substitution after the first recipe raised; the planner's fail-open
    logged it and served templates; 54 tests stayed green and the baseline moved. The
    session fixture sets `DIET_PLANNER_STRICT`, and this test proves it is in force.
    """
    import diet.services.rule_based_planner as planner_module
    from diet.services.rule_based_planner import RuleBasedPlanner

    user, _chosen = _chooser("strict-gate")

    def boom(*_a, **_k):
        raise RuntimeError("injected")
    monkeypatch.setattr(planner_module, "find_recipe", boom, raising=False)
    import diet.planner.recipes as recipes_module
    monkeypatch.setattr(recipes_module, "find_recipe", boom)
    with pytest.raises(RuntimeError, match="injected"):
        RuleBasedPlanner(user, seed_salt="strict").generate(
            daily_kcal=user.calculate_daily_calories(), duration_days=1, snack_count=1)


def test_a_chooser_is_served_dishes_with_their_foods_in_them(seeded_catalogue):
    """With 76 recipes and a client who chose foods for every slot, most meals are
    named dishes, and most of those carry one of the client's choices in place of the
    line it most resembles. Zero of either is the dead-path signature."""
    from diet.models import Meal
    from tests.diet_quality import _generate

    user, chosen = _chooser("alive-gate")
    plan = _generate(user, user.calculate_daily_calories(), 7, "alive")
    assert plan is not None, "generation refused"
    meals = list(Meal.objects.filter(diet_plan=plan).prefetch_related("components"))
    # A plan with no meals satisfies every ratio below — `0 >= 0.0` is True — so the
    # population is asserted before anything is measured over it. `plan is not None`
    # above does not promise the plan holds anything.
    assert len(meals) >= 7 * 3, f"a seven-day plan produced {len(meals)} meals"
    dishes = [m for m in meals if m.recipe_id]
    assert len(dishes) >= 0.5 * len(meals), f"{len(dishes)} of {len(meals)} meals are dishes"
    with_choice = [m for m in dishes
                   if {c.food_id for c in m.components.all()} & chosen.get(m.meal_type, set())]
    assert len(with_choice) >= 0.5 * len(dishes), (
        f"only {len(with_choice)} of {len(dishes)} dishes contain a chosen food")

    # A substitution names what it replaced. "(with X)" read as an addition and was
    # wrong whenever X replaced the food in the dish's own name.
    swapped = [m.name for m in dishes if " instead of " in m.name]
    assert swapped, "no substitution reached a persisted name"
    assert not any("(with " in m.name for m in dishes), "an unattributed substitution"


# ---------------------------------------------------------------------------
# Phase E — what the client actually receives
# ---------------------------------------------------------------------------

def _api_client_for(user):
    from django.test import Client
    from rest_framework_simplejwt.tokens import RefreshToken
    _grant_diet_access(user)
    c = Client()
    c.defaults["HTTP_AUTHORIZATION"] = f"Bearer {RefreshToken.for_user(user).access_token}"
    return c


_ARABIC = re.compile(r"[؀-ۿ]")


def test_every_meal_payload_carries_what_the_engine_decided(seeded_catalogue):
    """E1. Five endpoints serialised a meal by hand and none carried its name, the
    reason it was chosen, the dish it is, or the target it was built to. The client
    read "Levantine · 25 minutes" where the engine had said "Mujadara with Salad".
    Every meal-bearing payload now comes from one builder, in both languages.
    """
    user, _chosen = _chooser("payload-gate")
    c = _api_client_for(user)

    r = c.post("/api/diet/v1/plans/generate-rule/", {"duration_days": 1, "snack_count": 1},
               content_type="application/json")
    assert r.status_code == 201, r.content
    body = r.json()
    meals = body["meals"]
    assert len(meals) == 4
    for m in meals:
        for key in ("name", "name_ar", "display_name", "reason", "reason_ar", "recipe_id",
                    "is_dish", "targets", "components", "nutrition"):
            assert key in m, f"{key} missing from generate payload"
        assert m["name"] and m["reason"], m
        assert m["targets"]["calories"] > 0
        for comp in m["components"]:
            for key in ("food_id", "name", "name_ar", "serving", "serving_ar", "quantity_grams"):
                assert key in comp, f"{key} missing from component"
    assert any(m["is_dish"] for m in meals), "a chooser's day has no named dish"
    # Every meal, dish or built, has an Arabic name with no English left in it.
    for m in meals:
        assert _ARABIC.search(m["name_ar"]), m["name_ar"]
        assert not re.search(r"[A-Za-z]{3,}", m["name_ar"].replace("(", " ")), f"English in an Arabic name: {m['name_ar']}"
    assert all(_ARABIC.search(m["reason_ar"]) for m in meals), "a reason has no Arabic"
    plan_id, meal_id = body["diet_plan_id"], meals[0]["id"]

    r = c.get(f"/api/diet/v1/nutrition/plan/{plan_id}/")
    assert r.status_code == 200, r.content
    by_id = {m["id"]: m for m in r.json()["meals"]}
    assert by_id[meal_id]["name"] == meals[0]["name"]
    assert by_id[meal_id]["targets"] == meals[0]["targets"], "two views, two targets"

    r = c.get(f"/api/diet/v1/meals/{meal_id}/components/")
    assert r.status_code == 200, r.content
    j = r.json()
    assert j["meal"]["name"] == meals[0]["name"] and j["meal"]["reason"]
    assert j["nutrition"]["targets"]["calories"] == meals[0]["targets"]["calories"]
    comp = j["components"][0]
    assert comp["food_id"] == comp["food"]["id"] and comp["serving_ar"] and comp["name_ar"]

    r = c.get(f"/api/diet/v1/plan/{plan_id}/meals-with-ingredients/")
    assert r.status_code == 200, r.content
    m0 = next(m for m in r.json()["meals"] if m["id"] == meal_id)
    assert m0["name"] == meals[0]["name"]
    assert m0["target_nutrition"] == m0["targets"] == meals[0]["targets"]
    assert m0["components"][0]["food_name"] == m0["components"][0]["name"]

    r = c.get(f"/api/diet/v1/client/meals/{meal_id}/")
    assert r.status_code == 200, r.content
    assert r.json()["name"] == meals[0]["name"] and r.json()["reason"]

    # An Arabic client reads Arabic where the engine had a translation.
    r = c.get(f"/api/diet/v1/meals/{meal_id}/components/", HTTP_ACCEPT_LANGUAGE="ar")
    assert r.status_code == 200, r.content
    j = r.json()
    assert j["meal"]["display_name"] == j["meal"]["name_ar"]
    assert j["components"][0]["display_name"] == j["components"][0]["name_ar"]


def test_an_unservable_slot_is_a_422_not_a_500(seeded_catalogue, monkeypatch):
    """E2. `NoServableMealError` fell into the view's broad handler and reached the
    client as "Failed to generate rule-based plan", status 500. It is the client's to
    act on: choose more foods, loosen a constraint."""
    from diet.services.rule_based_planner import RuleBasedPlanner

    user, _chosen = _chooser("unservable-gate")
    c = _api_client_for(user)
    monkeypatch.setattr(RuleBasedPlanner, "_plan_meal_from_recipe", lambda *a, **k: None)
    monkeypatch.setattr(RuleBasedPlanner, "_plan_meal_from_template", lambda *a, **k: None)
    r = c.post("/api/diet/v1/plans/generate-rule/", {"duration_days": 1}, content_type="application/json")
    assert r.status_code == 422, r.content
    assert r.json()["code"] == "no_servable_meal"


def test_the_task_and_the_endpoint_generate_plans_of_the_same_shape(seeded_catalogue):
    """E3. The Celery fallback read a stale `daily_calorie_target` (default 2000) and
    skipped the feasibility check; the synchronous view did neither. One function now
    serves the inline endpoint and the task, so pressing either button yields the plan
    the other would have. The GPT path that the fallback existed for is gone: there is
    one engine, and both endpoints run it.
    """
    from diet.models import DietPlan, Meal
    from diet.services.plan_generation import generate_and_save
    from diet.tasks import generate_rule_based_plan
    from tests.diet_quality import _make_client

    user, _chosen = _chooser("task-gate")
    _output, direct = generate_and_save(user, duration_days=2, seed_salt="parity")
    result = generate_rule_based_plan(user.id, duration_days=2)
    assert "plan_id" in result, result
    via_task = DietPlan.objects.get(pk=result["plan_id"])
    assert via_task.daily_calories == direct.daily_calories
    assert Meal.objects.filter(diet_plan=via_task).count() == Meal.objects.filter(diet_plan=direct).count() == 8
    assert via_task.generation_strategy == direct.generation_strategy == "ENGINE"

    # A profile the meal structure cannot carry is refused with a reason, not retried.
    heavy = _make_client("task-heavy", ["Muscle Gain"], 250, 300, 20, "Male", "VeryActive")
    result = generate_rule_based_plan(heavy.id, duration_days=1)
    assert result.get("code") == "infeasible_calories", result


def test_when_neither_candidate_fits_the_closer_one_is_served(seeded_catalogue, monkeypatch):
    """The judge ranked preference before deviation even when both candidates missed
    tolerance, so a plate of chosen foods 20% under its target beat a dish 11% under."""
    from diet.services.rule_based_planner import RuleBasedPlanner

    user, _chosen = _chooser("judge-gate")
    planner = RuleBasedPlanner(user, seed_salt="judge")
    served = []
    recipe = ("dish", False, 10.0, False, 0.11, lambda: served.append("dish"))
    template = ("plate", False, 500.0, False, 0.20, lambda: served.append("plate"))
    monkeypatch.setattr(RuleBasedPlanner, "_plan_meal_from_recipe", lambda *a, **k: recipe)
    monkeypatch.setattr(RuleBasedPlanner, "_plan_meal_from_template", lambda *a, **k: template)
    assert planner._plan_meal("Lunch", None, None) == "dish" and served == ["dish"]

    # Inside tolerance, preference decides, as before.
    served.clear()
    both_in = ("plate", True, 500.0, False, 0.05, lambda: served.append("plate"))
    dish_in = ("dish", True, 10.0, False, 0.01, lambda: served.append("dish"))
    monkeypatch.setattr(RuleBasedPlanner, "_plan_meal_from_recipe", lambda *a, **k: dish_in)
    monkeypatch.setattr(RuleBasedPlanner, "_plan_meal_from_template", lambda *a, **k: both_in)
    assert planner._plan_meal("Lunch", None, None) == "plate"


def test_a_week_is_generated_and_saved_within_the_query_budget(seeded_catalogue):
    """E4/E5. A 7-day plan ran 924 queries to generate (a chosen-foods lookup per
    recipe per meal) and 479 to persist (a food lookup per ingredient, an existence
    check per component). Both were fixed at the source; this holds the budget so a
    loop cannot creep back in. Wall-clock is bounded loosely — it is a CPU-bound path
    on a shared CI box — and the query counts are the real assertion.
    """
    import time

    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    from diet.services.diet_persistence import DietPersistenceService
    from diet.services.rule_based_planner import RuleBasedPlanner

    user, _chosen = _chooser("budget-gate")
    t0 = time.perf_counter()
    with CaptureQueriesContext(connection) as generating:
        out = RuleBasedPlanner(user, seed_salt="budget").generate(
            daily_kcal=user.calculate_daily_calories(), meal_count=3, snack_count=1, duration_days=7)
    with CaptureQueriesContext(connection) as persisting:
        plan = DietPersistenceService(user).save_plan(out, meal_count=3, snack_count=1)
    elapsed = time.perf_counter() - t0
    assert plan is not None and len(out.plan) == 28
    assert len(generating) <= 120, f"generating a week took {len(generating)} queries"
    assert len(persisting) <= 300, f"persisting a week took {len(persisting)} queries"
    assert elapsed < 20, f"a week took {elapsed:.1f}s"


def test_the_async_endpoint_can_run_the_deterministic_engine_for_a_month(seeded_catalogue):
    """E3. The synchronous endpoint caps at seven days and pointed at "the Celery task,
    which already exists for this planner" — it did not; the only task ran the LLM
    path with a stale-calorie fallback. `engine: "rule"` on /plans/generate/ enqueues
    the deterministic engine for up to thirty days."""
    from diet.models import DietPlan, Meal

    user, _chosen = _chooser("async-gate")
    c = _api_client_for(user)
    r = c.post("/api/diet/v1/plans/generate/", {"engine": "rule", "duration_days": 10, "snack_count": 1},
               content_type="application/json")
    assert r.status_code == 200, r.content
    assert r.json()["engine"] == "rule" and r.json()["task_id"]
    # The task's own answer, not "the newest plan for this user": under
    # CELERY_TASK_ALWAYS_EAGER the result carries the id it created, and reading it
    # back by ordering assumed nothing else had written a plan in between.
    from celery.result import AsyncResult

    outcome = AsyncResult(r.json()["task_id"]).result
    assert isinstance(outcome, dict) and "plan_id" in outcome, outcome
    plan = DietPlan.objects.get(pk=outcome["plan_id"])
    assert plan.user_id == user.id
    assert Meal.objects.filter(diet_plan=plan).count() == 40
    assert plan.generation_strategy == "ENGINE"


# ---------------------------------------------------------------------------
# Phase C/D — variety over the week, and the owner's four
# ---------------------------------------------------------------------------

def test_learning_never_changes_a_meals_shape(seeded_catalogue):
    """D3 invariant. Whatever the learned weights say, lunch and dinner keep a
    vegetable, no slot empties, and the plan still lands inside tolerance as often.
    Learning may reorder what fills a shape; it may not change the shape."""
    from diet.models import FoodItem, UserFoodWeight
    from diet.planner.candidates import classify_food
    from diet.planner.learning import MAX_WEIGHT, MIN_WEIGHT
    from diet.services.rule_based_planner import RuleBasedPlanner

    def plan_for(user):
        out = RuleBasedPlanner(user, seed_salt="shape").generate(
            daily_kcal=user.calculate_daily_calories(), meal_count=3, snack_count=1, duration_days=7)
        return out.plan

    def shape_of(meals):
        # On the plates the engine BUILDS. A library dish is what its author wrote
        # (Mujadara has no salad); learning cannot change that and is not asked to.
        built = [m for m in meals if m.recipe_id is None and m.meal_type in ("Lunch", "Dinner")]
        veg_at_mains = sum(1 for m in built
                           if any(classify_food(FoodItem.objects.get(pk=i.food_id)) == "vegetable"
                                  for i in m.ingredients if i.food_id))
        empty = sum(1 for m in meals if not m.ingredients)
        return (veg_at_mains / len(built) if built else 1.0), empty

    user, _chosen = _chooser("shape-gate")
    before_veg, before_empty = shape_of(plan_for(user))

    # The most hostile weights learning is allowed to produce: every vegetable at the
    # floor, one starch at the ceiling.
    foods = list(FoodItem.objects.filter(needs_review=False))
    rows = [UserFoodWeight(user=user, food=f, weight=MIN_WEIGHT, observations=9)
            for f in foods if classify_food(f) == "vegetable"]
    starch = next(f for f in foods if classify_food(f) == "carb")
    rows.append(UserFoodWeight(user=user, food=starch, weight=MAX_WEIGHT, observations=9))
    UserFoodWeight.objects.bulk_create(rows)

    after_veg, after_empty = shape_of(plan_for(user))
    assert after_empty == before_empty == 0
    assert after_veg >= before_veg - 1e-9, f"vegetables at built mains fell {before_veg:.2f} -> {after_veg:.2f}"
    assert after_veg == 1.0


def test_recorded_swaps_can_be_replayed_offline(seeded_catalogue):
    """D3 replay. Given what a client swapped away, would the next plan avoid it? A
    report, not a target: the client's signal may be biased, so learning stays hand-set
    until this says yes on real data."""
    from diet.models import Meal, MealComponent, MealSwap
    from diet.planner.learning import replay_swaps
    from diet.services.plan_generation import generate_and_save

    user, _chosen = _chooser("replay-gate")
    assert replay_swaps(user)["swaps"] == 0
    _out, plan = generate_and_save(user, duration_days=1, seed_salt="replay-seed")
    lunch = Meal.objects.get(diet_plan=plan, meal_type="Lunch")
    comp = MealComponent.objects.filter(meal=lunch).select_related("food").first()
    other = MealComponent.objects.exclude(food=comp.food).select_related("food").first()
    MealSwap.objects.create(user=user, meal=lunch, rejected_food=comp.food, chosen_food=other.food, similarity=0.9)
    report = replay_swaps(user, days=7)
    assert report["swaps"] == 1 and report["avoided"] in (0, 1)
    assert 0.0 <= report["avoided_share"] <= 1.0
    assert all(" at Lunch" in line for line in report["still_served"])


def test_the_funnel_records_every_step_a_client_takes(seeded_catalogue):
    """D4. Generated -> viewed -> accepted -> completed -> rated -> swapped. Viewed and
    accepted come from the app through one action; the rest are recorded by the
    endpoints that do them. A report reads the table; the planner never does."""
    from diet.models import Meal, MealComponent, MealEvent

    user, _chosen = _chooser("funnel-gate")
    c = _api_client_for(user)
    r = c.post("/api/diet/v1/plans/generate-rule/", {"duration_days": 1}, content_type="application/json")
    assert r.status_code == 201, r.content
    meal_id = r.json()["meals"][0]["id"]
    url = "/api/diet/v1/client/meals/interact/"

    for kind in ("viewed", "accepted"):
        r = c.post(url, {"action": "event", "meal_id": meal_id, "kind": kind}, content_type="application/json")
        assert r.status_code == 200, r.content
    r = c.post(url, {"action": "event", "meal_id": meal_id, "kind": "generated"}, content_type="application/json")
    assert r.status_code == 400
    r = c.post(url, {"action": "event", "meal_id": meal_id, "kind": "viewed"}, content_type="application/json")
    assert r.status_code == 200

    comp = MealComponent.objects.filter(meal_id=meal_id).first()
    r = c.post(url, {"action": "rate_component", "component_id": comp.id, "is_liked": True}, content_type="application/json")
    assert r.status_code == 200, r.content
    r = c.post(f"/api/diet/v1/client/meals/{meal_id}/complete/", {"action": "complete_meal"}, content_type="application/json")
    assert r.status_code == 200, r.content

    kinds = collections.Counter(MealEvent.objects.filter(user=user, meal_id=meal_id).values_list("kind", flat=True))
    assert kinds == {"viewed": 2, "accepted": 1, "rated": 1, "completed": 1}, kinds

    # Another client's meal does not exist as far as they are concerned. 404, not 403,
    # on BOTH endpoints: 403 means "this meal exists and is not yours", which is an
    # existence oracle over the whole meal table, and the two endpoints used to disagree
    # about the same object.
    other, _ = _chooser("funnel-other")
    o = _api_client_for(other)
    r = o.post(url, {"action": "event", "meal_id": meal_id, "kind": "viewed"}, content_type="application/json")
    assert r.status_code == 404
    r = o.get(f"/api/diet/v1/client/meals/{meal_id}/")
    assert r.status_code == 404
    r = o.post(f"/api/diet/v1/client/meals/{meal_id}/complete/", {}, content_type="application/json")
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# Review findings, pinned
# ---------------------------------------------------------------------------

def test_a_plan_is_readable_only_by_its_client_or_its_author(seeded_catalogue):
    """meals-with-ingredients let every trainer read every engine plan (created_by is
    None) and let any other role read all of them. Default-deny now."""
    from tests.diet_quality import PROFILES, _make_client

    owner, _chosen = _chooser("plan-owner")
    c = _api_client_for(owner)
    r = c.post("/api/diet/v1/plans/generate-rule/", {"duration_days": 1}, content_type="application/json")
    assert r.status_code == 201, r.content
    plan_id = r.json()["diet_plan_id"]

    # A trainer who is not this client's trainer is told they may not — they can already
    # see the client on their own screens, so nothing is disclosed. Everyone else gets a
    # 404: 403 means "this exists and is not yours", and walking the id space with it
    # counts the platform's plans. The three client meal endpoints already answered 404.
    trainer = _make_client("plan-trainer", *PROFILES[0]); trainer.user_type = "trainer"; trainer.save()
    assert _api_client_for(trainer).get(f"/api/diet/v1/plan/{plan_id}/meals-with-ingredients/").status_code == 403
    agent = _make_client("plan-agent", *PROFILES[0]); agent.user_type = "agent"; agent.save()
    assert _api_client_for(agent).get(f"/api/diet/v1/plan/{plan_id}/meals-with-ingredients/").status_code == 404
    other, _ = _chooser("plan-other")
    other_api = _api_client_for(other)   # once: it gives the user a subscription
    assert other_api.get(f"/api/diet/v1/plan/{plan_id}/meals-with-ingredients/").status_code == 404
    # And the same answer from the sibling read over the same object.
    assert other_api.get(f"/api/diet/v1/nutrition/plan/{plan_id}/").status_code == 404
    assert c.get(f"/api/diet/v1/plan/{plan_id}/meals-with-ingredients/").status_code == 200


def test_wrong_shaped_input_is_a_400_never_a_500(seeded_catalogue):
    """Ids as strings, flags as strings, dates in the wrong format, and a tiny profile
    all used to reach the broad handlers as 500s."""
    from tests.diet_quality import PROFILES, _make_client

    user, _chosen = _chooser("input-gate")
    c = _api_client_for(user)
    # A single id, as a string or a bare int, is accepted as a list of one; anything
    # that is not an id is a 400 naming the field, never a 500.
    for body, expected in (({"liked_foods": "12"}, 200), ({"liked_foods": 5}, 200),
                           ({"disliked_foods": ["a"]}, 400), ({"protein_choices": "x,y"}, 400),
                           ({"liked_foods": {"id": 1}}, 400)):
        r = c.post("/api/diet/v1/preferences/", body, content_type="application/json")
        assert r.status_code == expected, (body, r.status_code, r.content[:120])
    r = c.post("/api/diet/v1/preferences/food-category/", {"food_id": "abc", "meal": "Lunch", "macro": "protein"},
               content_type="application/json")
    assert r.status_code == 400, r.content
    # A parseable date in another notation is normalised to ISO; garbage is a 400.
    r = c.post("/api/diet/v1/plans/generate-rule/", {"duration_days": 1, "start_date": "next tuesday"},
               content_type="application/json")
    assert r.status_code == 400 and "start_date" in r.json()["error"], r.content
    r = c.post("/api/diet/v1/plans/generate/", {"meal_count": "x"}, content_type="application/json")
    assert r.status_code == 400, r.content
    r = c.post("/api/diet/v1/plans/generate-rule/", {"duration_days": 1}, content_type="application/json")
    assert r.status_code == 201, r.content
    plan_id, meal_id = r.json()["diet_plan_id"], r.json()["meals"][0]["id"]
    comp_id = r.json()["meals"][0]["components"][0]["id"]
    for url in (f"/api/diet/v1/nutrition/plan/{plan_id}/?date=2026/09/08",
                "/api/diet/v1/client/progress/enhanced/?date=yesterday"):
        assert c.get(url).status_code == 400, url
    url = "/api/diet/v1/client/meals/interact/"
    r = c.post(url, {"action": "rate_component", "component_id": comp_id, "is_liked": "false"},
               content_type="application/json")
    assert r.status_code == 200 and r.json()["component"]["is_liked"] is False, r.content
    r = c.post(url, {"action": "rate_component", "component_id": comp_id, "is_liked": "maybe"},
               content_type="application/json")
    assert r.status_code == 400, r.content
    assert c.get("/api/diet/v1/meals/999999/components/").status_code == 404

    # The smallest profile the bounds allow is clamped to 1,200 kcal and served; the
    # validator's 1,000 kcal floor is unreachable through the API, and a profile the
    # engine cannot serve is a 4xx with a reason, never a 500.
    small = _make_client("input-small", ["Weight Loss"], 100, 30, 100, "Female", "Sedentary")
    r = _api_client_for(small).post("/api/diet/v1/plans/generate-rule/", {"duration_days": 1},
                                    content_type="application/json")
    assert r.status_code in (201, 400, 422), r.content
    if r.status_code == 201:
        assert r.json()["plan_nutrition"]["targets"]["calories"] >= 1200


def test_the_async_outcome_is_readable_and_private(seeded_catalogue):
    """The async endpoints returned a task id that nothing could read, and the quota
    was spent before the task ran. `plans/tasks/<id>/` reads the outcome; the quota
    is recorded by the task when a plan exists."""
    from diet.models import DietPlan

    user, _chosen = _chooser("task-status")
    c = _api_client_for(user)
    r = c.post("/api/diet/v1/plans/generate/", {"engine": "rule", "duration_days": 2}, content_type="application/json")
    assert r.status_code == 200, r.content
    task_id = r.json()["task_id"]
    r = c.get(f"/api/diet/v1/plans/tasks/{task_id}/")
    assert r.status_code == 200, r.content
    body = r.json()
    assert body["state"] == "SUCCESS" and body["plan_id"] == DietPlan.objects.filter(user=user).latest("id").id
    assert "user_id" not in body
    other, _ = _chooser("task-status-other")
    assert _api_client_for(other).get(f"/api/diet/v1/plans/tasks/{task_id}/").status_code == 404


def test_targets_and_events_reach_every_endpoint(seeded_catalogue):
    """The built-to target is persisted on the meal and served by every endpoint (the
    components view kept it under nutrition.targets and the details/progress views had
    none); rating a whole meal records the funnel step; a swap answers with the shared
    component shape."""
    from diet.models import Meal, MealEvent

    user, _chosen = _chooser("targets-gate")
    c = _api_client_for(user)
    r = c.post("/api/diet/v1/plans/generate-rule/", {"duration_days": 1}, content_type="application/json")
    assert r.status_code == 201, r.content
    meal = r.json()["meals"][0]
    meal_id = meal["id"]
    stored = Meal.objects.get(pk=meal_id)
    assert stored.target and abs(stored.target["calories"] - meal["targets"]["calories"]) < 0.6
    for url, path in ((f"/api/diet/v1/meals/{meal_id}/components/", ("meal", "targets")),
                      (f"/api/diet/v1/client/meals/{meal_id}/", ("targets",)),
                      ("/api/diet/v1/client/progress/enhanced/", None)):
        body = c.get(url).json()
        if path is None:
            found = next(m for m in body["meals"] if m["id"] == meal_id)["targets"]
        else:
            found = body
            for key in path:
                found = found[key]
        assert found == meal["targets"], (url, found, meal["targets"])

    url = "/api/diet/v1/client/meals/interact/"
    r = c.post(url, {"action": "rate_meal", "meal_id": meal_id, "is_liked": "false"}, content_type="application/json")
    assert r.status_code == 200 and r.json()["meal"]["is_liked"] is False, r.content
    assert MealEvent.objects.filter(user=user, meal_id=meal_id, kind=MealEvent.RATED).count() == 1
    comp = meal["components"][0]
    offers = c.post(url, {"action": "swap_food", "component_id": comp["id"]}, content_type="application/json").json()["offers"]
    r = c.post(url, {"action": "swap_food", "component_id": comp["id"], "chosen_food_id": offers[0]["food_id"]},
               content_type="application/json")
    assert r.status_code == 200, r.content
    assert {"food_id", "name_ar", "serving", "serving_ar", "nutrition"} <= set(r.json()["component"])


def test_constraints_fail_closed_and_reasons_are_honest(seeded_catalogue, monkeypatch):
    """A failure reading a client's allergies used to fall back to no constraints at
    DEBUG level; it now raises. And "because you chose X" used to be decided on the
    total score, which a universal staple reaches with no choice made."""
    from diet.planner.constraints import ClientConstraints
    from diet.services.rule_based_planner import RuleBasedPlanner
    from tests.diet_quality import PROFILES, _make_client

    user = _make_client("closed-gate", *PROFILES[2])
    def boom(*_a, **_k):
        raise RuntimeError("allergy read failed")
    monkeypatch.setattr(ClientConstraints, "for_user", classmethod(lambda cls, u: boom()))
    with pytest.raises(RuntimeError, match="allergy read failed"):
        RuleBasedPlanner(user, seed_salt="closed").generate(
            daily_kcal=user.calculate_daily_calories(), duration_days=1, snack_count=1)
    monkeypatch.undo()

    plain = _make_client("plain-reason", *PROFILES[3])
    out = RuleBasedPlanner(plain, seed_salt="plain").generate(
        daily_kcal=plain.calculate_daily_calories(), duration_days=3, snack_count=1)
    assert out.plan and not any("because you chose" in (m.reason or "") for m in out.plan), \
        [m.reason for m in out.plan if "because you chose" in (m.reason or "")]


# ---------------------------------------------------------------------------
# Gate blind spots, closed
# ---------------------------------------------------------------------------

def test_every_day_of_a_week_lands_near_its_energy_target(seeded_catalogue):
    """Drift was measured on one-day plans only; a week's later days carry residuals
    and are the ones a client actually eats."""
    from diet.models import Meal
    from tests.diet_quality import _generate

    user, _chosen = _chooser("week-days")
    kcal = user.calculate_daily_calories()
    plan = _generate(user, kcal, 7, "week-days")
    assert plan is not None
    by_day = collections.defaultdict(float)
    for m in Meal.objects.filter(diet_plan=plan).prefetch_related("components__food"):
        by_day[m.date] += m.calculate_nutrition()["calories"]
    assert len(by_day) == 7
    worst = max(abs(v - kcal) / kcal for v in by_day.values())
    assert worst <= 0.10, f"a day landed {worst:.0%} from its target: {dict(by_day)}"


def test_in_production_a_path_failure_is_logged_and_served_around(seeded_catalogue, monkeypatch, settings, caplog):
    """The non-strict contract: with DIET_PLANNER_STRICT off, a crash inside the recipe
    path is an ERROR log and a plan built from the other path, never silence."""
    import logging

    import diet.planner.recipes as recipes_module
    from diet.services.rule_based_planner import RuleBasedPlanner

    settings.DIET_PLANNER_STRICT = False
    user, _chosen = _chooser("fail-open")

    def boom(*_a, **_k):
        raise RuntimeError("injected recipe failure")
    import diet.services.rule_based_planner as planner_module
    from unittest import mock

    monkeypatch.setattr(recipes_module, "find_recipe", boom)
    # The diet loggers do not propagate to the root handler caplog installs, so the
    # ERROR call is observed on the planner's own logger.
    with mock.patch.object(planner_module.logger, "error") as error:
        out = RuleBasedPlanner(user, seed_salt="open").generate(
            daily_kcal=user.calculate_daily_calories(), duration_days=1, snack_count=1)
    assert len(out.plan) == 4 and all(m.recipe_id is None for m in out.plan)
    assert error.call_count >= 4 and any("recipe" in str(c.args[0]).lower() for c in error.call_args_list)


def test_a_meal_with_no_stored_name_is_named_from_its_plate(seeded_catalogue):
    """Trainer-written meals and rows older than the name column have name=''. The
    client must never read an empty title, in either language."""
    from diet.models import FoodItem, Meal, MealComponent

    user, _chosen = _chooser("noname")
    c = _api_client_for(user)
    r = c.post("/api/diet/v1/plans/generate-rule/", {"duration_days": 1}, content_type="application/json")
    assert r.status_code == 201, r.content
    plan_id = r.json()["diet_plan_id"]
    first = Meal.objects.get(pk=r.json()["meals"][0]["id"])
    bare = Meal.objects.create(diet_plan_id=plan_id, date=first.date, meal_type="Dinner", name="", name_ar="")
    foods = list(FoodItem.objects.filter(name__in=["Chicken Breast (Grilled)", "White Rice (Cooked)", "Broccoli"]))
    assert len(foods) == 3
    for f in foods:
        MealComponent.objects.create(meal=bare, food=f, quantity=100.0, meal_time="Dinner")
    body = c.get(f"/api/diet/v1/meals/{bare.id}/components/").json()
    assert body["meal"]["name"] and " and " in body["meal"]["name"]
    assert _ARABIC.search(body["meal"]["name_ar"])
    body = c.get(f"/api/diet/v1/meals/{bare.id}/components/", HTTP_ACCEPT_LANGUAGE="ar").json()
    assert body["meal"]["display_name"] == body["meal"]["name_ar"]


def test_a_week_without_snacks_through_the_api(seeded_catalogue):
    """snack_count=0 and duration_days=7 on the synchronous endpoint: 21 meals, no
    Snack slot, and each day's targets sum to the day."""
    from diet.models import Meal

    user, _chosen = _chooser("nosnack")
    c = _api_client_for(user)
    r = c.post("/api/diet/v1/plans/generate-rule/", {"duration_days": 7, "snack_count": 0},
               content_type="application/json")
    assert r.status_code == 201, r.content
    plan_id = r.json()["diet_plan_id"]
    meals = list(Meal.objects.filter(diet_plan=plan_id))
    assert len(meals) == 21 and not any(m.meal_type == "Snack" for m in meals)
    body = c.get(f"/api/diet/v1/plan/{plan_id}/meals-with-ingredients/").json()
    daily = body["plan"]["daily_calories"]
    by_day = collections.defaultdict(float)
    for m in body["meals"]:
        by_day[m["date"]] += m["targets"]["calories"]
    assert len(by_day) == 7
    assert all(abs(v - daily) <= 0.16 * daily for v in by_day.values()), dict(by_day)


def test_a_chosen_food_that_is_an_allergen_is_never_served_nor_mentioned(seeded_catalogue):
    """Choosing shrimp for dinner and declaring a shellfish allergy: the allergy wins on
    every path, including substitution and the reason sentence."""
    from diet.models import FoodItem, Meal, UserFoodCategoryPreference, UserFoodPreference
    from tests.diet_quality import PROFILES, _generate, _make_client

    user = _make_client("allergy-chooser", *PROFILES[2])
    shrimp = FoodItem.objects.get(name="Shrimp")
    UserFoodCategoryPreference.objects.get_or_create(user=user, food=shrimp, meal="Dinner", macro="protein")
    pref, _ = UserFoodPreference.objects.get_or_create(user=user)
    pref.allergies = "shellfish"; pref.save()
    plan = _generate(user, user.calculate_daily_calories(), 7, "allergy")
    assert plan is not None
    for m in Meal.objects.filter(diet_plan=plan).prefetch_related("components__food"):
        assert all(c.food_id != shrimp.id for c in m.components.all()), m.name
        assert "shrimp" not in (m.reason or "").lower() and "shrimp" not in (m.name or "").lower()


def test_the_residual_carry_is_bounded_and_never_negative():
    """A 500 kcal miss moves the next slot by at most 15% of its base, in the direction
    of the miss, and can never drive a target below zero."""
    from types import SimpleNamespace

    from diet.services.rule_based_planner import RuleBasedPlanner

    planner = RuleBasedPlanner.__new__(RuleBasedPlanner)
    def target(kcal, p, c, f):
        return SimpleNamespace(kcal_target=kcal, macro_targets={"protein": p, "carb": c, "fat": f})
    served = target(700, 40, 90, 20)
    meal = SimpleNamespace(total_nutrition={"calories": 200, "protein": 10, "carbs": 30, "fat": 5})
    nxt = target(800, 50, 100, 25)
    planner._carry_residual(meal, served, nxt)
    assert nxt.kcal_target == 800 + 0.15 * 800
    assert nxt.macro_targets["protein"] == 50 + 0.15 * 50
    over = SimpleNamespace(total_nutrition={"calories": 1400, "protein": 90, "carbs": 200, "fat": 60})
    nxt = target(800, 50, 100, 25)
    planner._carry_residual(over, served, nxt)
    assert nxt.kcal_target == 800 - 0.15 * 800 and nxt.macro_targets["fat"] == 25 - 0.15 * 25
    tiny = target(20, 1, 2, 1)
    planner._carry_residual(over, served, tiny)
    assert tiny.kcal_target >= 0 and all(v >= 0 for v in tiny.macro_targets.values())


def test_a_task_status_is_only_readable_by_whoever_started_it(seeded_catalogue):
    """Celery reports PENDING for ANY id it has never seen, so an unclaimed id used to
    look like a real pending task — and the pending and failed branches answered before
    the ownership check, which asked the task RESULT who owned it. A pending or failed
    task has no result. The owner is recorded when the task is enqueued.
    """
    from diet.models import DietPlan

    user, _chosen = _chooser("task-owner")
    other, _o = _chooser("task-stranger")
    c, o = _api_client_for(user), _api_client_for(other)

    # A guessed id is not a task to report on.
    r = c.get("/api/diet/v1/plans/tasks/not-a-real-id/")
    assert r.status_code == 404, r.content

    r = c.post("/api/diet/v1/plans/generate/", {"engine": "rule", "duration_days": 1},
               content_type="application/json")
    assert r.status_code == 200, r.content
    task_id = r.json()["task_id"]

    mine = c.get(f"/api/diet/v1/plans/tasks/{task_id}/")
    assert mine.status_code == 200, mine.content
    assert mine.json()["task_id"] == task_id

    theirs = o.get(f"/api/diet/v1/plans/tasks/{task_id}/")
    assert theirs.status_code == 404, theirs.content
    assert "state" not in theirs.json()


def test_a_fitting_dish_at_the_weekly_cap_beats_a_plate_that_misses(seeded_catalogue, monkeypatch):
    """The cap demotes a dish only below a FITTING plate; it must never hand the client
    an off-target plate to avoid one more serving.

    "At the cap" is expressed as WEEK_MAX_SERVINGS, not as the literal 2 it happened to
    be when this was written: the owner sets that number (3 since 2026-09-18), and a
    hardcoded count stops describing this scenario the moment it changes."""
    from diet.services.rule_based_planner import WEEK_MAX_SERVINGS, RuleBasedPlanner

    user, _chosen = _chooser("cap-judge")
    planner = RuleBasedPlanner(user, seed_salt="cap")
    capped_dish = ("dish", True, 10.0, WEEK_MAX_SERVINGS, 0.01, lambda: None)
    missing_plate = ("plate", False, 500.0, False, 0.20, lambda: None)
    monkeypatch.setattr(RuleBasedPlanner, "_plan_meal_from_recipe", lambda *a, **k: capped_dish)
    monkeypatch.setattr(RuleBasedPlanner, "_plan_meal_from_template", lambda *a, **k: missing_plate)
    assert planner._plan_meal("Lunch", None, None) == "dish"
    fitting_plate = ("plate", True, 5.0, False, 0.05, lambda: None)
    monkeypatch.setattr(RuleBasedPlanner, "_plan_meal_from_template", lambda *a, **k: fitting_plate)
    assert planner._plan_meal("Lunch", None, None) == "plate"


def test_a_superseded_plan_is_not_history(seeded_catalogue):
    """Regenerating today replaces today's plan. Its dishes must not count toward the
    weekly cap, or two taps put every dish at the cap for tomorrow."""
    import datetime as dt

    from diet.models import DietPlan
    from diet.services.plan_generation import generate_and_save
    from diet.services.rule_based_planner import RuleBasedPlanner

    user, _chosen = _chooser("supersede")
    day = "2026-02-02"
    generate_and_save(user, duration_days=1, start_date=day, seed_salt="s1")
    generate_and_save(user, duration_days=1, start_date=day, seed_salt="s2")
    assert DietPlan.objects.filter(user=user, is_active=True).count() == 1
    counts = RuleBasedPlanner(user)._get_recent_recipe_counts(7, dt.date(2026, 2, 3))
    assert counts and max(counts.values()) <= 1, counts


def test_the_daily_generation_quota_is_enforced(seeded_catalogue, django_capture_on_commit_callbacks):
    """The quota is spent on commit, so every test that generated a plan inside a
    rolled-back transaction never exercised it. With the callbacks executed, the
    plan's daily limit refuses the request after it is reached."""
    from subscription.models import Subscription

    user, _chosen = _chooser("quota-gate")
    c = _api_client_for(user)
    plan = Subscription.objects.get(user=user).plan
    plan.max_meals_per_day = 1
    plan.save(update_fields=["max_meals_per_day"])
    with django_capture_on_commit_callbacks(execute=True):
        r = c.post("/api/diet/v1/plans/generate-rule/", {"duration_days": 1}, content_type="application/json")
        assert r.status_code == 201, r.content
    r = c.post("/api/diet/v1/plans/generate-rule/", {"duration_days": 1}, content_type="application/json")
    # The QUOTA's answer, by its code. `in (403, 429)` accepted the rate limiter too, so
    # a completely broken quota passed whenever the limiter happened to fire first.
    assert r.status_code == 429, (r.status_code, r.content[:200])
    assert r.json().get("code") == "quota_exhausted", r.content[:200]


def test_a_client_can_remove_a_food_they_chose(seeded_catalogue):
    """`deleted, _ = rows.delete()` rebound gettext to the per-model dict, so every
    translated string after it raised: removing a chosen food was a 500. Same shadowing
    class as the loop variable that killed the recipe path."""
    from diet.models import FoodItem, UserFoodCategoryPreference

    user, _chosen = _chooser("remove-choice")
    c = _api_client_for(user)
    food = FoodItem.objects.get(name="Chicken Breast (Grilled)")
    r = c.post("/api/diet/v1/preferences/food-category/",
               {"food_id": food.id, "meal": "Lunch", "macro": "protein"}, content_type="application/json")
    assert r.status_code in (200, 201), r.content
    r = c.delete(f"/api/diet/v1/preferences/food-category/{food.id}/",
                 {"meal": "Lunch", "macro": "protein"}, content_type="application/json")
    assert r.status_code == 200, r.content
    assert r.json()["deleted"] >= 1
    assert not UserFoodCategoryPreference.objects.filter(
        user=user, food=food, meal="Lunch", macro="protein").exists()
    # Removing it again says so, in words, rather than crashing.
    r = c.delete(f"/api/diet/v1/preferences/food-category/{food.id}/",
                 {"meal": "Lunch", "macro": "protein"}, content_type="application/json")
    assert r.status_code == 404, r.content


def test_an_imported_food_is_never_planned_until_it_is_vetted(seeded_catalogue, make_user):
    """Any authenticated client can create a FoodItem through the import endpoint, with
    macros of their choosing and no meal slots. Such a row must stay out of every pool
    and every plate until a human accepts it, or the catalogue rebuild is undone one
    import at a time."""
    from diet.models import FoodItem, Meal
    from diet.planner.candidates import build_pool
    from diet.planner.policy import load_policy
    from tests.diet_quality import _generate

    user, _chosen = _chooser("import-gate")
    # Staff: the catalogue is shared, so only staff may add to it. A client trying is
    # refused, which is the other half of this rule.
    from django.test import Client
    from rest_framework_simplejwt.tokens import RefreshToken

    staff = make_user("import-staff", user_type="admin")
    c = Client()
    c.defaults["HTTP_AUTHORIZATION"] = f"Bearer {RefreshToken.for_user(staff).access_token}"
    refused = _api_client_for(user).post(
        "/api/diet/v1/food/import/",
        {"food_data": {"api_id": "edamam_client_try", "name": "Client Cake",
                       "calories": 5, "protein": 0, "carbs": 1, "fat": 0}},
        content_type="application/json")
    assert refused.status_code == 403, refused.content

    r = c.post("/api/diet/v1/food/import/",
               {"food_data": {"api_id": "edamam_food_test1", "name": "Free Cake",
                              "calories": 5, "protein": 0, "carbs": 1, "fat": 0}},
               content_type="application/json")
    assert r.status_code in (200, 201), r.content
    imported = FoodItem.objects.get(api_id="edamam_food_test1")
    assert imported.needs_review is True

    pool = build_pool(user, load_policy("maintain"))
    in_pool = {f.id for macros in pool.by_slot.values() for lst in macros.values() for f in lst}
    assert imported.id not in in_pool
    plan = _generate(user, user.calculate_daily_calories(), 2, "import")
    assert plan is not None
    served = {c.food_id for m in Meal.objects.filter(diet_plan=plan) for c in m.components.all()}
    assert imported.id not in served


def test_food_search_offers_only_what_can_be_chosen(seeded_catalogue):
    """The catalogue is the source of truth since the rebuild. External rows carry ids
    no other endpoint accepts, so they are off by default and marked unselectable."""
    user, _chosen = _chooser("search-gate")
    c = _api_client_for(user)
    for url in ("/api/diet/v1/food/search/?q=chicken", "/api/diet/v1/food/search/?query=chicken"):
        r = c.get(url)
        assert r.status_code == 200, (url, r.content[:120])
        body = r.json()
        assert body["results"], url
        assert body["edamam_count"] == 0
        assert all(f["selectable"] and isinstance(f["id"], int) for f in body["results"])
    assert c.get("/api/diet/v1/food/search/").status_code == 400


def test_a_meal_title_reads_like_something_a_person_would_write(seeded_catalogue):
    """The catalogue's qualifiers belong in a food list, not in a meal title: clients
    were reading "Labneh with Honey (Pita Bread (White) instead of Pita Bread (Whole
    Wheat))". And a substitution must never replace the ingredient the dish is named
    after — a lentil soup with chickpeas instead of lentils is a different dish."""
    from diet.models import Meal
    from tests.diet_quality import _generate

    user, _chosen = _chooser("title-gate")
    plan = _generate(user, user.calculate_daily_calories(), 7, "titles")
    assert plan is not None
    names = [(m.name, m.name_ar, m.recipe.name if m.recipe else "") for m in
             Meal.objects.filter(diet_plan=plan).select_related("recipe")]
    assert names
    for name, name_ar, dish in names:
        assert name.count("(") <= 1, f"nested parentheses in {name!r}"
        assert name and name_ar
        if " instead of " in name:
            replaced = name.split(" instead of ")[1].split(")")[0].strip().lower()
            assert replaced not in dish.lower(), f"{dish!r} lost its namesake: {name!r}"
            new = name.split("(")[1].split(" instead of ")[0].strip().lower()
            assert new != replaced, f"a substitution nobody can see: {name!r}"


def test_the_diet_app_calls_no_external_model(seeded_catalogue):
    """One engine. The GPT path — a prompt builder, a response parser, a sync endpoint,
    a Celery task and a training-data collector — was deleted: the deterministic planner
    replaced it, and an external model is no longer part of the diet app. This keeps it
    out, because a reintroduced import is how a deleted dependency comes back.
    """
    import pathlib

    root = pathlib.Path(__file__).resolve().parent.parent / "diet"
    offenders = []
    for path in root.rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        for needle in ("import openai", "api.openai.com", "OPENAI_API_KEY", "DietGenerator"):
            if needle in text:
                offenders.append(f"{path.relative_to(root.parent)}: {needle}")
    assert not offenders, offenders

    # And the route is gone, rather than left returning 500 from a missing import.
    from django.urls import NoReverseMatch, reverse
    with pytest.raises(NoReverseMatch):
        reverse("diet:v1_generate_plan_sync")


def test_the_admin_can_review_a_dishes_gram_amounts(seeded_catalogue, make_user):
    """The 105 dishes were written to a macro target, not to a plate, and reviewing
    them needs the grams AND what they add up to. This is the screen that job happens
    on, so it has to load and it has to show the numbers."""
    from django.test import Client

    from diet.models import Recipe

    admin = make_user("recipe-admin", user_type="admin")
    admin.set_password("Xx!23456"); admin.save()
    c = Client()
    assert c.login(email=admin.email, password="Xx!23456")

    dish = Recipe.objects.filter(is_active=True, ingredients__isnull=False).distinct().first()
    assert dish is not None
    nutrition = dish.nutrition()
    assert nutrition["calories"] > 0 and nutrition["protein"] >= 0

    r = c.get("/dj-admin/diet/recipe/")
    assert r.status_code == 200, r.status_code
    assert str(round(nutrition["calories"])).encode() in r.content

    r = c.get(f"/dj-admin/diet/recipe/{dish.id}/change/")
    assert r.status_code == 200, r.status_code
    body = r.content.decode()
    assert "reviewed" in body
    first = dish.ingredients.select_related("food").first()
    assert first.food.name in body and str(round(first.grams)) in body

    # The review flag is what the owner's pass is tracked with.
    assert Recipe.objects.filter(reviewed=False).exists()
    Recipe.objects.filter(pk=dish.pk).update(reviewed=True)
    assert Recipe.objects.get(pk=dish.pk).reviewed is True


def test_the_admin_can_vet_a_food_and_the_planner_obeys(seeded_catalogue, make_user):
    """`needs_review` is the gate that stops the import endpoint re-creating the noise
    the catalogue rebuild removed. Approving a food in admin must actually let the
    planner serve it, and every field the planner reads must be editable there."""
    from django.test import Client

    from diet.models import FoodItem
    from diet.planner.candidates import build_pool
    from diet.planner.policy import load_policy

    admin = make_user("food-admin", user_type="admin")
    admin.set_password("Xx!23456"); admin.save()
    c = Client()
    assert c.login(email=admin.email, password="Xx!23456")

    food = FoodItem.objects.create(
        api_id="hand-curated-1", name="Hand Added Cheese", calories=250, protein=18,
        carbs=2, fat=19, serving_size="100g", serving_size_grams=100,
        needs_review=True, meal_slots=["Breakfast", "Snack"], cuisine="levantine")
    user, _chosen = _chooser("vetting-client")

    def in_pool():
        pool = build_pool(user, load_policy("maintain"))
        return food.id in {f.id for macros in pool.by_slot.values()
                           for lst in macros.values() for f in lst}

    assert not in_pool(), "an unvetted food reached the planner"

    r = c.get(f"/dj-admin/diet/fooditem/{food.id}/change/")
    assert r.status_code == 200
    body = r.content.decode()
    for field in ("needs_review", "meal_slots", "household_unit", "unit_grams", "cuisine", "role"):
        assert f'name="{field}"' in body, f"{field} is not editable in admin"

    r = c.post("/dj-admin/diet/fooditem/",
               {"action": "approve_foods", "_selected_action": [str(food.id)],
                "index": "0", "select_across": "0"}, follow=True)
    assert r.status_code == 200
    food.refresh_from_db()
    assert food.needs_review is False
    assert in_pool(), "an approved food still does not reach the planner"


# ---------------------------------------------------------------------------
# The trainer path
# ---------------------------------------------------------------------------

def _trainer_and_client(make_user, tag):
    from django.test import Client
    from rest_framework_simplejwt.tokens import RefreshToken

    trainer = make_user(f"tr-{tag}", user_type="trainer")
    client_user = make_user(f"cl-{tag}", user_type="client")
    for u in (trainer, client_user):
        _grant_diet_access(u)

    def api(user):
        c = Client()
        c.defaults["HTTP_AUTHORIZATION"] = f"Bearer {RefreshToken.for_user(user).access_token}"
        return c
    return trainer, client_user, api(trainer), api(client_user)


def test_a_trainer_cannot_claim_a_client_who_never_agreed(seeded_catalogue, make_user):
    """A trainer called assign-client on any unassigned client, which created a pending
    row, then approved that row at respond-to-request. Two calls, no consent, and the
    trainer then owned that person's height, weight, goals, training history and diet
    plan. A request is approved by the side that did not make it."""
    from users.models import TrainerClientRelation

    trainer, client_user, t_api, c_api = _trainer_and_client(make_user, "claim")

    r = t_api.post("/api/auth/trainer/assign-client/", {"client_id": client_user.id},
                   content_type="application/json")
    assert r.status_code in (200, 201), r.content
    relation = TrainerClientRelation.objects.get(trainer=trainer, client=client_user)
    assert relation.status == "pending" and relation.initiated_by == "trainer"

    # The trainer cannot approve their own invitation.
    r = t_api.post("/api/auth/trainer/respond-to-request/",
                   {"request_id": relation.id, "action": "approve"}, content_type="application/json")
    assert r.status_code == 403, r.content
    client_user.refresh_from_db()
    assert client_user.assigned_trainer_id is None, "a trainer claimed a client who never agreed"

    # The client can see it and accept it, and only then is the trainer assigned.
    r = c_api.get("/api/auth/client/trainer-invites/")
    assert r.status_code == 200, r.content
    invites = r.json()["invitations"]
    assert [i["request_id"] for i in invites] == [relation.id]

    r = c_api.post("/api/auth/client/respond-to-invite/",
                   {"request_id": relation.id, "action": "approve"}, content_type="application/json")
    assert r.status_code == 200, r.content
    client_user.refresh_from_db()
    assert client_user.assigned_trainer_id == trainer.id


def test_a_client_request_is_approved_by_the_trainer_and_not_by_the_client(seeded_catalogue, make_user):
    """The mirror image: the client asks, the trainer accepts. A client must not be
    able to accept their own request either."""
    from users.models import TrainerClientRelation

    trainer, client_user, t_api, c_api = _trainer_and_client(make_user, "mirror")
    r = c_api.post("/api/auth/client/request-trainer/", {"trainer_id": trainer.id},
                   content_type="application/json")
    assert r.status_code in (200, 201), r.content
    relation = TrainerClientRelation.objects.get(trainer=trainer, client=client_user)
    assert relation.initiated_by == "client"

    r = c_api.post("/api/auth/client/respond-to-invite/",
                   {"request_id": relation.id, "action": "approve"}, content_type="application/json")
    assert r.status_code == 403, r.content
    client_user.refresh_from_db()
    assert client_user.assigned_trainer_id is None

    r = t_api.post("/api/auth/trainer/respond-to-request/",
                   {"request_id": relation.id, "action": "approve"}, content_type="application/json")
    assert r.status_code == 200, r.content
    client_user.refresh_from_db()
    assert client_user.assigned_trainer_id == trainer.id


def test_a_trainer_cannot_unassign_someone_elses_client(seeded_catalogue, make_user):
    """Unassign matched a relation of ANY status, so a second trainer could create a
    pending row and then strip the real trainer's client: the approved row survived
    while assigned_trainer went null, and the two disagreed."""
    from users.models import TrainerClientRelation

    owner, client_user, o_api, c_api = _trainer_and_client(make_user, "own")
    intruder = make_user("tr-intruder", user_type="trainer")
    _grant_diet_access(intruder)
    from django.test import Client
    from rest_framework_simplejwt.tokens import RefreshToken
    i_api = Client()
    i_api.defaults["HTTP_AUTHORIZATION"] = f"Bearer {RefreshToken.for_user(intruder).access_token}"

    c_api.post("/api/auth/client/request-trainer/", {"trainer_id": owner.id}, content_type="application/json")
    relation = TrainerClientRelation.objects.get(trainer=owner, client=client_user)
    o_api.post("/api/auth/trainer/respond-to-request/",
               {"request_id": relation.id, "action": "approve"}, content_type="application/json")
    client_user.refresh_from_db()
    assert client_user.assigned_trainer_id == owner.id

    i_api.post("/api/auth/trainer/assign-client/", {"client_id": client_user.id},
               content_type="application/json")
    r = i_api.post("/api/auth/trainer/unassign-client/", {"client_id": client_user.id},
                   content_type="application/json")
    assert r.status_code in (400, 403, 404), r.content
    client_user.refresh_from_db()
    assert client_user.assigned_trainer_id == owner.id, "an intruder stripped another trainer's client"
    assert TrainerClientRelation.objects.get(trainer=owner, client=client_user).status == "approved"


def _a_template():
    """Any plan template. The trainer endpoint requires one."""
    from diet.models import DietPlanTemplate
    t = DietPlanTemplate.objects.first()
    if t is None:
        t = DietPlanTemplate.objects.create(name="gate template", meals_per_day=3,
                                            snacks_per_day=1)
    return t


def _assigned_pair(make_user, tag):
    """A trainer and a client who actually agreed, through the real endpoints."""
    from users.models import TrainerClientRelation

    trainer, client_user, t_api, c_api = _trainer_and_client(make_user, tag)
    c_api.post("/api/auth/client/request-trainer/", {"trainer_id": trainer.id},
               content_type="application/json")
    relation = TrainerClientRelation.objects.get(trainer=trainer, client=client_user)
    t_api.post("/api/auth/trainer/respond-to-request/",
               {"request_id": relation.id, "action": "approve"}, content_type="application/json")
    client_user.refresh_from_db()
    assert client_user.assigned_trainer_id == trainer.id
    for field, value in (("height", 175), ("weight", 78), ("age", 28),
                         ("gender", "Male"), ("activity_level", "Moderate")):
        setattr(client_user, field, value)
    client_user.client_goals = ["Maintain"]
    client_user.save()
    return trainer, client_user, t_api, c_api


def test_a_trainer_can_build_a_plan_for_a_client_who_already_has_one(seeded_catalogue, make_user):
    """`DietPlan.clean()` refuses a second active plan over the same window, so trainer
    plan creation failed for every client the engine had ever served — and failed as an
    opaque 400. Creating a plan retires the one it replaces."""
    from diet.models import DietPlan, Meal
    from diet.services.plan_generation import generate_and_save

    trainer, client_user, t_api, _c = _assigned_pair(make_user, "build")
    _out, generated = generate_and_save(client_user, duration_days=2, seed_salt="trainer")
    assert generated.is_active

    r = t_api.post("/api/diet/v1/trainer/diet-plans/",
                   {"client_id": client_user.id, "goal": "Maintain", "duration_weeks": 2,
                    "template_id": _a_template().id, "start_date": str(date.today())},
                   content_type="application/json")
    assert r.status_code in (200, 201), r.content
    plan = DietPlan.objects.filter(user=client_user, created_by=trainer).first()
    assert plan is not None and plan.is_active
    generated.refresh_from_db()
    assert not generated.is_active, "two active plans over one window"
    # The same macro targets a generated plan carries, not nulls.
    assert plan.target_protein and plan.target_carbs and plan.target_fat
    assert plan.macro_targets()["protein"] == plan.target_protein

    # And a meal the trainer adds reads like every other meal.
    from diet.models import FoodItem
    chicken = FoodItem.objects.get(name="Chicken Breast (Grilled)")
    rice = FoodItem.objects.get(name="White Rice (Cooked)")
    r = t_api.post("/api/diet/v1/trainer/meals/", {
        "diet_plan_id": plan.id, "meal_type": "Lunch",
        "target_date": str(plan.start_date),
        "food_items": [{"food_id": chicken.id, "quantity": 150},
                       {"food_id": rice.id, "quantity": 180}]},
        content_type="application/json")
    assert r.status_code in (200, 201), r.content
    meal = Meal.objects.get(diet_plan=plan, meal_type="Lunch")
    assert meal.name and meal.name_ar and meal.reason and meal.reason_ar
    assert meal.components.count() == 2


def test_a_trainer_meal_refuses_what_it_cannot_serve(seeded_catalogue, make_user):
    """None of this was checked: a meal type off the list, a negative quantity, the
    same food twice, a date outside the plan, a food that does not exist. Each was a
    persisted bad row or an opaque 400."""
    from diet.models import DietPlan, FoodItem, Meal

    trainer, client_user, t_api, _c = _assigned_pair(make_user, "refuse")
    r = t_api.post("/api/diet/v1/trainer/diet-plans/",
                   {"client_id": client_user.id, "goal": "Maintain", "duration_weeks": 1,
                    "template_id": _a_template().id, "start_date": str(date.today())},
                   content_type="application/json")
    assert r.status_code in (200, 201), r.content
    plan = DietPlan.objects.get(user=client_user, created_by=trainer)
    food = FoodItem.objects.filter(needs_review=False).first()
    good = [{"food_id": food.id, "quantity": 100}]

    def post(**over):
        body = {"diet_plan_id": plan.id, "meal_type": "Lunch",
                "target_date": str(plan.start_date), "food_items": good}
        body.update(over)
        return t_api.post("/api/diet/v1/trainer/meals/", body, content_type="application/json")

    for label, over in (
        ("meal type", {"meal_type": "Brunch"}),
        ("negative quantity", {"food_items": [{"food_id": food.id, "quantity": -500}]}),
        ("huge quantity", {"food_items": [{"food_id": food.id, "quantity": 99999}]}),
        ("duplicate food", {"food_items": good + good}),
        ("unknown food", {"food_items": [{"food_id": 99999999, "quantity": 100}]}),
        ("empty meal", {"food_items": []}),
        ("date outside plan", {"target_date": "2099-01-01"}),
        ("bad quantity type", {"food_items": [{"food_id": food.id, "quantity": "lots"}]}),
    ):
        r = post(**over)
        assert r.status_code == 400, f"{label} was accepted: {r.status_code} {r.content[:120]}"
        # A reason the trainer can act on, either the view's own required-field
        # message or the service's code. Not "Request could not be completed."
        body = r.json()
        assert body.get("code") or "required" in body.get("error", ""), \
            f"{label} gave no reason: {r.content[:120]}"
    assert not Meal.objects.filter(diet_plan=plan).exists(), "a refused meal was persisted"

    # duration_weeks is bounded like every other numeric input: it was passed
    # straight to timedelta(weeks=...), where a string was a TypeError and a huge
    # number an OverflowError, both arriving as an opaque 400.
    r = t_api.post("/api/diet/v1/trainer/diet-plans/",
                   {"client_id": client_user.id, "goal": "Maintain", "duration_weeks": 9999,
                    "template_id": _a_template().id, "start_date": str(date.today())},
                   content_type="application/json")
    assert r.status_code in (200, 201), r.content
    clamped = DietPlan.objects.filter(user=client_user, created_by=trainer).order_by("-id").first()
    assert clamped.duration_weeks == 52, clamped.duration_weeks
    r = t_api.post("/api/diet/v1/trainer/diet-plans/",
                   {"client_id": client_user.id, "goal": "Maintain", "duration_weeks": "many",
                    "template_id": _a_template().id, "start_date": str(date.today())},
                   content_type="application/json")
    assert r.status_code == 400, r.content


def test_a_dismissed_trainer_cannot_touch_the_plan_they_wrote(seeded_catalogue, make_user):
    """Authorship alone was the test, so authority granted once was never revoked: a
    trainer the client had dismissed could still rewrite or delete every meal of that
    client's live plan."""
    from diet.models import DietPlan, FoodItem, Meal

    trainer, client_user, t_api, c_api = _assigned_pair(make_user, "revoke")
    r = t_api.post("/api/diet/v1/trainer/diet-plans/",
                   {"client_id": client_user.id, "goal": "Maintain", "duration_weeks": 1,
                    "template_id": _a_template().id, "start_date": str(date.today())},
                   content_type="application/json")
    assert r.status_code in (200, 201), r.content
    plan = DietPlan.objects.get(user=client_user, created_by=trainer)
    food = FoodItem.objects.filter(needs_review=False).first()
    r = t_api.post("/api/diet/v1/trainer/meals/", {
        "diet_plan_id": plan.id, "meal_type": "Lunch", "target_date": str(plan.start_date),
        "food_items": [{"food_id": food.id, "quantity": 120}]}, content_type="application/json")
    assert r.status_code in (200, 201), r.content
    meal = Meal.objects.get(diet_plan=plan)

    r = c_api.post("/api/auth/client/unassign-trainer/", {}, content_type="application/json")
    assert r.status_code in (200, 204), r.content
    client_user.refresh_from_db()
    assert client_user.assigned_trainer_id is None

    r = t_api.put(f"/api/diet/v1/trainer/meals/{meal.id}/",
                  {"food_items": [{"food_id": food.id, "quantity": 400}]},
                  content_type="application/json")
    assert r.status_code == 403, r.content
    r = t_api.delete(f"/api/diet/v1/trainer/meals/{meal.id}/")
    assert r.status_code == 403, r.content
    assert Meal.objects.filter(pk=meal.pk).exists()
    meal.refresh_from_db()
    assert meal.components.first().quantity == 120


def test_every_import_path_produces_an_unvetted_food(seeded_catalogue):
    """Two ways in: the client endpoint and the admin's Edamam screen. Both create a
    FoodItem with no meal slots, no household unit and no cuisine, so both must leave
    it unvetted or the curated catalogue is undone one import at a time."""
    import inspect

    from diet import admin as diet_admin
    from diet import views as diet_views

    for source, label in ((inspect.getsource(diet_admin.ImprovedFoodItemAdmin.import_food_from_edamam),
                           "admin Edamam import"),
                          (inspect.getsource(diet_views.FoodImportView), "client import endpoint")):
        assert "FoodItem.objects.create(" in source, label
        create_at = source.index("FoodItem.objects.create(")
        window = source[create_at:create_at + 900]
        assert "needs_review=True" in window, f"{label} creates a plannable food"


def test_a_relation_from_before_the_consent_flow_cannot_be_approved(seeded_catalogue, make_user):
    """The code fix did not reach the data the old code had written. Migration 0033
    labelled every existing row 'client', which is exactly what the old
    trainer/assign-client produced — so the takeover survived in any pending row that
    predated the deploy. Those rows are 'unknown' and neither side may approve them.
    """
    from users.models import TrainerClientRelation

    trainer, client_user, t_api, c_api = _trainer_and_client(make_user, "legacy")
    relation = TrainerClientRelation.objects.create(
        trainer=trainer, client=client_user, status="pending", initiated_by="unknown")

    r = t_api.post("/api/auth/trainer/respond-to-request/",
                   {"request_id": relation.id, "action": "approve"}, content_type="application/json")
    assert r.status_code == 409, r.content
    r = c_api.post("/api/auth/client/respond-to-invite/",
                   {"request_id": relation.id, "action": "approve"}, content_type="application/json")
    assert r.status_code == 409, r.content
    client_user.refresh_from_db()
    assert client_user.assigned_trainer_id is None

    # And a row written without saying who asked defaults to un-approvable, rather
    # than to the value the trainer is allowed to approve.
    blank = TrainerClientRelation.objects.create(
        trainer=trainer, client=make_user("cl-blank", user_type="client"), status="pending")
    assert blank.initiated_by == "unknown"


def test_the_escrow_hold_comes_back_when_the_relationship_ends(seeded_catalogue, make_user):
    """Approving a trainer moves the client's money into the platform escrow wallet,
    and nothing gave it back: a client who accepted a trainer and later dismissed them
    lost it permanently. The hold now has a lifecycle tied to the relation, and
    releasing it is idempotent."""
    from decimal import Decimal

    from wallet.models import Transaction, Wallet
    from users.models import TrainerClientRelation

    trainer, client_user, t_api, c_api = _trainer_and_client(make_user, "escrow")
    trainer.trainer_hourly_rate = Decimal("100.00")
    trainer.save()
    wallet, _created = Wallet.objects.get_or_create(
        owner=client_user, defaults={"owner_type": "client"})
    wallet.balance = Decimal("500.00")
    wallet.save()

    c_api.post("/api/auth/client/request-trainer/", {"trainer_id": trainer.id},
               content_type="application/json")
    relation = TrainerClientRelation.objects.get(trainer=trainer, client=client_user)
    r = t_api.post("/api/auth/trainer/respond-to-request/",
                   {"request_id": relation.id, "action": "approve"}, content_type="application/json")
    assert r.status_code == 200, r.content
    wallet.refresh_from_db()
    assert wallet.balance == Decimal("400.00"), "the hold was not taken"
    assert Transaction.objects.filter(metadata__purpose="trainer_request_hold").exists()

    r = c_api.post("/api/auth/client/unassign-trainer/", {}, content_type="application/json")
    assert r.status_code in (200, 204), r.content
    wallet.refresh_from_db()
    assert wallet.balance == Decimal("500.00"), "the client's money stayed in escrow"

    # Releasing twice does not mint money: the hold has an identity and it is spent.
    from users.trainer_relations import release_hold
    stale = TrainerClientRelation(trainer=trainer, client=client_user)
    release_hold(stale, client_user)
    wallet.refresh_from_db()
    assert wallet.balance == Decimal("500.00")


def test_the_price_the_client_agreed_to_is_the_price_they_pay(seeded_catalogue, make_user):
    """The charge was re-read from the trainer's own `trainer_hourly_rate` at approval
    time, and that field is trainer-writable: advertise 10, wait for the request, raise
    the rate, approve, and the client's wallet is swept for a price they never saw."""
    from decimal import Decimal

    from wallet.models import Wallet
    from users.models import TrainerClientRelation

    trainer, client_user, t_api, c_api = _trainer_and_client(make_user, "price")
    trainer.trainer_hourly_rate = Decimal("10.00")
    trainer.save()
    wallet, _created = Wallet.objects.get_or_create(
        owner=client_user, defaults={"owner_type": "client"})
    wallet.balance = Decimal("500.00")
    wallet.save()

    c_api.post("/api/auth/client/request-trainer/", {"trainer_id": trainer.id},
               content_type="application/json")
    relation = TrainerClientRelation.objects.get(trainer=trainer, client=client_user)
    assert relation.agreed_rate == Decimal("10.00")

    trainer.trainer_hourly_rate = Decimal("450.00")
    trainer.save()
    r = t_api.post("/api/auth/trainer/respond-to-request/",
                   {"request_id": relation.id, "action": "approve"}, content_type="application/json")
    assert r.status_code == 200, r.content
    wallet.refresh_from_db()
    assert wallet.balance == Decimal("490.00"), "the client paid a price they never agreed to"


def test_a_relationship_is_not_granted_when_the_charge_cannot_be_taken(seeded_catalogue, make_user):
    """The hold failure was logged and the relation granted anyway: the trainer got
    full access to the client's data, the client was charged nothing, and the routine
    app would then pay that trainer out of other clients' money."""
    from decimal import Decimal

    from wallet.models import Wallet
    from users.models import TrainerClientRelation

    trainer, client_user, t_api, c_api = _trainer_and_client(make_user, "nofunds")
    trainer.trainer_hourly_rate = Decimal("100.00")
    trainer.save()
    wallet, _created = Wallet.objects.get_or_create(
        owner=client_user, defaults={"owner_type": "client"})
    wallet.balance = Decimal("500.00")
    wallet.save()

    c_api.post("/api/auth/client/request-trainer/", {"trainer_id": trainer.id},
               content_type="application/json")
    relation = TrainerClientRelation.objects.get(trainer=trainer, client=client_user)
    # The balance is checked when the request is made and again when it is answered.
    # Between those two moments the client can spend it.
    wallet.balance = Decimal("5.00")
    wallet.save()
    r = t_api.post("/api/auth/trainer/respond-to-request/",
                   {"request_id": relation.id, "action": "approve"}, content_type="application/json")
    assert r.status_code == 402, r.content
    relation.refresh_from_db()
    client_user.refresh_from_db()
    assert relation.status == "pending" and not relation.hold_reference
    assert client_user.assigned_trainer_id is None
    wallet.refresh_from_db()
    assert wallet.balance == Decimal("5.00")


def test_a_trainer_is_paid_the_held_amount_once(seeded_catalogue, make_user):
    """Assigning a routine moved the trainer's CURRENT rate out of the single shared
    escrow wallet every time, with no link to any hold: fifty assignments were fifty
    payouts, drawn from money other clients had deposited."""
    from decimal import Decimal

    from wallet.models import Transaction, Wallet
    from routine.models import Routine
    from users.models import TrainerClientRelation

    trainer, client_user, t_api, c_api = _trainer_and_client(make_user, "settle")
    trainer.trainer_hourly_rate = Decimal("100.00")
    trainer.save()
    wallet, _created = Wallet.objects.get_or_create(
        owner=client_user, defaults={"owner_type": "client"})
    wallet.balance = Decimal("500.00")
    wallet.save()
    c_api.post("/api/auth/client/request-trainer/", {"trainer_id": trainer.id},
               content_type="application/json")
    relation = TrainerClientRelation.objects.get(trainer=trainer, client=client_user)
    t_api.post("/api/auth/trainer/respond-to-request/",
               {"request_id": relation.id, "action": "approve"}, content_type="application/json")

    from users.trainer_relations import settle_hold
    relation.refresh_from_db()
    assert relation.hold_reference
    settle_hold(relation, trainer)
    trainer_wallet = Wallet.objects.get(owner=trainer)
    assert trainer_wallet.balance == Decimal("100.00")

    # Every repeat moves nothing: the hold is spent and the relation says so.
    for _ in range(5):
        relation.refresh_from_db()
        settle_hold(relation, trainer)
    trainer_wallet.refresh_from_db()
    assert trainer_wallet.balance == Decimal("100.00"), "the trainer was paid more than was held"
    assert Transaction.objects.filter(
        metadata__purpose="trainer_assignment_settlement").count() == 1


def test_editing_a_completed_meal_does_not_invent_calories(seeded_catalogue, make_user):
    """`Meal.is_completed` is a cache of its components, and nothing invalidated it
    when a trainer replaced them. The client completed 100 g of eggs, the trainer
    swapped it for 2000 g of rice, and DailyProgress — which sums the components of
    completed meals — credited the client with food they never ate."""
    from diet.models import DailyProgress, DietPlan, FoodItem, Meal

    trainer, client_user, t_api, c_api = _assigned_pair(make_user, "recompute")
    r = t_api.post("/api/diet/v1/trainer/diet-plans/",
                   {"client_id": client_user.id, "goal": "Maintain", "duration_weeks": 1,
                    "template_id": _a_template().id, "start_date": str(date.today())},
                   content_type="application/json")
    assert r.status_code in (200, 201), r.content
    plan = DietPlan.objects.get(user=client_user, created_by=trainer)
    small = FoodItem.objects.get(name="Cucumber")
    big = FoodItem.objects.get(name="White Rice (Cooked)")
    r = t_api.post("/api/diet/v1/trainer/meals/", {
        "diet_plan_id": plan.id, "meal_type": "Lunch", "target_date": str(date.today()),
        "food_items": [{"food_id": small.id, "quantity": 100}]}, content_type="application/json")
    assert r.status_code in (200, 201), r.content
    meal = Meal.objects.get(diet_plan=plan, meal_type="Lunch")

    r = c_api.post(f"/api/diet/v1/client/meals/{meal.id}/complete/",
                   {"action": "complete_meal"}, content_type="application/json")
    assert r.status_code == 200, r.content
    meal.refresh_from_db()
    assert meal.is_completed

    r = t_api.put(f"/api/diet/v1/trainer/meals/{meal.id}/",
                  {"food_items": [{"food_id": big.id, "quantity": 1000}]},
                  content_type="application/json")
    assert r.status_code in (200, 201), r.content
    meal.refresh_from_db()
    assert not meal.is_completed, "the client is credited with food they never ate"
    progress = DailyProgress.objects.filter(user=client_user, diet_plan=plan, date=date.today()).first()
    if progress is not None:
        assert progress.calories_consumed == 0, progress.calories_consumed

    # The same slot cannot be written twice by a retried request.
    r = t_api.post("/api/diet/v1/trainer/meals/", {
        "diet_plan_id": plan.id, "meal_type": "Lunch", "target_date": str(date.today()),
        "food_items": [{"food_id": small.id, "quantity": 50}]}, content_type="application/json")
    assert r.status_code == 409, r.content
    assert Meal.objects.filter(diet_plan=plan, meal_type="Lunch", date=date.today()).count() == 1


def test_a_dismissed_trainer_cannot_read_the_plan_either(seeded_catalogue, make_user):
    """The write path was fixed and the read path was not: two views tested authorship
    inline, so a dismissed trainer kept reading the client's plan, every meal and every
    gram."""
    from diet.models import DietPlan, Meal

    trainer, client_user, t_api, c_api = _assigned_pair(make_user, "readrevoke")
    r = t_api.post("/api/diet/v1/trainer/diet-plans/",
                   {"client_id": client_user.id, "goal": "Maintain", "duration_weeks": 1,
                    "template_id": _a_template().id, "start_date": str(date.today())},
                   content_type="application/json")
    assert r.status_code in (200, 201), r.content
    plan = DietPlan.objects.get(user=client_user, created_by=trainer)
    from diet.models import FoodItem
    food = FoodItem.objects.filter(needs_review=False).first()
    t_api.post("/api/diet/v1/trainer/meals/", {
        "diet_plan_id": plan.id, "meal_type": "Lunch", "target_date": str(date.today()),
        "food_items": [{"food_id": food.id, "quantity": 100}]}, content_type="application/json")
    meal = Meal.objects.get(diet_plan=plan)

    assert t_api.get(f"/api/diet/v1/nutrition/plan/{plan.id}/").status_code == 200
    assert t_api.get(f"/api/diet/v1/meals/{meal.id}/components/").status_code == 200

    r = c_api.post("/api/auth/client/unassign-trainer/", {}, content_type="application/json")
    assert r.status_code in (200, 204), r.content

    assert t_api.get(f"/api/diet/v1/nutrition/plan/{plan.id}/").status_code == 403
    assert t_api.get(f"/api/diet/v1/meals/{meal.id}/components/").status_code == 403


def test_a_trainer_cannot_touch_another_trainers_routine(seeded_catalogue, make_user):
    """`IsTrainerOrAdmin` defined only `has_permission`, so DRF's object permission
    defaulted to True: any trainer could modify any routine that reached their
    queryset, and one does as soon as a client moves between trainers. And the owning
    trainer could never edit their own routine's exercises, because the ownership test
    required a `created_by` field that RoutineExercise does not have."""
    from django.test import Client
    from rest_framework_simplejwt.tokens import RefreshToken

    from routine.models import Exercise, Routine, RoutineExercise

    owner, client_user, o_api, c_api = _assigned_pair(make_user, "routine")
    intruder = make_user("tr-routine-intruder", user_type="trainer")
    i_api = Client()
    i_api.defaults["HTTP_AUTHORIZATION"] = f"Bearer {RefreshToken.for_user(intruder).access_token}"

    exercise = Exercise.objects.create(name="Gate Squat", created_by=owner)
    routine = Routine.objects.create(name="Owner Routine", days=3, created_by=owner)
    routine.assigned_to.set([client_user])
    line = RoutineExercise.objects.create(routine=routine, exercise=exercise,
                                          sets=3, reps=10, day=1, order=1)

    # The owner can edit the composition of their own routine.
    r = o_api.patch(f"/api/routine/routine-exercises/{line.id}/", {"sets": 4},
                    content_type="application/json")
    assert r.status_code == 200, r.content
    line.refresh_from_db()
    assert line.sets == 4

    # A stranger cannot, and cannot touch the routine either.
    r = i_api.patch(f"/api/routine/routines/{routine.id}/", {"name": "Stolen"},
                    content_type="application/json")
    assert r.status_code in (403, 404), r.content
    r = i_api.delete(f"/api/routine/routines/{routine.id}/")
    assert r.status_code in (403, 404), r.content
    routine.refresh_from_db()
    assert routine.name == "Owner Routine"

    # An assigned routine cannot be deleted at all: it holds recorded progress.
    r = o_api.delete(f"/api/routine/routines/{routine.id}/")
    assert r.status_code in (204, 409), r.content
    if r.status_code == 409:
        assert Routine.objects.filter(pk=routine.pk).exists()


def test_deleting_an_exercise_cannot_erase_a_clients_history(seeded_catalogue, make_user):
    """The ProtectedError guard was written for Routine and attached to Exercise,
    where nothing is protected: `UserExerciseProgress` and every `ExerciseSetLog`
    cascade, so one delete silently rewrote a client's past volume and streaks."""
    from routine.models import Exercise, UserExerciseProgress

    owner, client_user, o_api, _c = _assigned_pair(make_user, "exdelete")
    exercise = Exercise.objects.create(name="Gate Press", created_by=owner)
    UserExerciseProgress.objects.create(user=client_user, exercise=exercise)

    r = o_api.delete(f"/api/routine/exercises/{exercise.id}/")
    assert r.status_code == 409, r.content
    assert Exercise.objects.filter(pk=exercise.pk).exists()
    assert UserExerciseProgress.objects.filter(exercise=exercise).exists()

    # One nobody has trained with can still be removed.
    spare = Exercise.objects.create(name="Gate Spare", created_by=owner)
    assert o_api.delete(f"/api/routine/exercises/{spare.id}/").status_code == 204


def test_a_new_trainer_plan_says_what_it_replaced(seeded_catalogue, make_user):
    """A client follows one plan at a time, and the model refuses two active plans
    over one window — which is why trainer plan creation used to fail outright for
    any client the engine had ever served. Creating one retires what overlaps it, and
    reports which, so the trainer can explain where the old plan went."""
    from diet.models import DietPlan
    from diet.services.plan_generation import generate_and_save

    trainer, client_user, t_api, _c = _assigned_pair(make_user, "supersede")
    _out, generated = generate_and_save(client_user, duration_days=3, seed_salt="sup")
    assert generated.is_active

    r = t_api.post("/api/diet/v1/trainer/diet-plans/",
                   {"client_id": client_user.id, "goal": "Maintain", "duration_weeks": 1,
                    "template_id": _a_template().id, "start_date": str(date.today())},
                   content_type="application/json")
    assert r.status_code in (200, 201), r.content
    assert generated.id in r.json()["superseded_plan_ids"]
    generated.refresh_from_db()
    assert not generated.is_active
    assert DietPlan.objects.filter(user=client_user, is_active=True).count() == 1


def test_a_trainer_cannot_lock_a_client_out_of_choosing(seeded_catalogue, make_user):
    """`trainer/assign-client/` needs no consent to create a pending row, and the
    client's own request endpoint then refused because "you already have a pending
    request". Any trainer could freeze any client out of choosing anybody, for free,
    by inviting them and never withdrawing it."""
    from users.models import TrainerClientRelation

    wanted, client_user, w_api, c_api = _trainer_and_client(make_user, "lockout")
    squatter = make_user("tr-squatter", user_type="trainer")
    _grant_diet_access(squatter)
    from django.test import Client
    from rest_framework_simplejwt.tokens import RefreshToken
    s_api = Client()
    s_api.defaults["HTTP_AUTHORIZATION"] = f"Bearer {RefreshToken.for_user(squatter).access_token}"

    r = s_api.post("/api/auth/trainer/assign-client/", {"client_id": client_user.id},
                   content_type="application/json")
    assert r.status_code in (200, 201), r.content

    # The client can still choose the trainer they actually want.
    r = c_api.post("/api/auth/client/request-trainer/", {"trainer_id": wanted.id},
                   content_type="application/json")
    assert r.status_code in (200, 201), r.content
    mine = TrainerClientRelation.objects.get(trainer=wanted, client=client_user)
    r = w_api.post("/api/auth/trainer/respond-to-request/",
                   {"request_id": mine.id, "action": "approve"}, content_type="application/json")
    assert r.status_code == 200, r.content
    client_user.refresh_from_db()
    assert client_user.assigned_trainer_id == wanted.id

    # And the squatter's invitation cannot now be accepted over the top.
    theirs = TrainerClientRelation.objects.get(trainer=squatter, client=client_user)
    r = c_api.post("/api/auth/client/respond-to-invite/",
                   {"request_id": theirs.id, "action": "approve"}, content_type="application/json")
    assert r.status_code == 400, r.content
    client_user.refresh_from_db()
    assert client_user.assigned_trainer_id == wanted.id


def test_the_collection_routes_answer_405_not_a_crash(seeded_catalogue, make_user):
    """`trainer/meals/` routes PUT and DELETE to handlers that require a meal id, so
    Django called them without one: a TypeError caught by the broad handler and
    reported as a 400."""
    trainer, _client_user, t_api, _c = _assigned_pair(make_user, "verbs")
    for method in ("put", "delete"):
        r = getattr(t_api, method)("/api/diet/v1/trainer/meals/", {}, content_type="application/json")
        assert r.status_code == 405, (method, r.status_code, r.content[:100])


def test_leaving_a_trainer_leaves_no_contradictory_history(seeded_catalogue, make_user):
    """Unassign deleted only the approved row, so a stale pending or rejected row for
    the same pair survived: the client's own request-status screen then contradicted
    itself, and the leftover could re-trigger the block on choosing anyone else."""
    from users.models import TrainerClientRelation

    trainer, client_user, t_api, c_api = _trainer_and_client(make_user, "history")
    c_api.post("/api/auth/client/request-trainer/", {"trainer_id": trainer.id},
               content_type="application/json")
    relation = TrainerClientRelation.objects.get(trainer=trainer, client=client_user)
    t_api.post("/api/auth/trainer/respond-to-request/",
               {"request_id": relation.id, "action": "approve"}, content_type="application/json")
    # A leftover from an earlier cycle.
    TrainerClientRelation.objects.create(
        trainer=make_user("tr-old", user_type="trainer"), client=client_user,
        status="rejected", initiated_by="client")

    r = c_api.post("/api/auth/client/unassign-trainer/", {}, content_type="application/json")
    assert r.status_code in (200, 204), r.content
    assert not TrainerClientRelation.objects.filter(trainer=trainer, client=client_user).exists()
    client_user.refresh_from_db()
    assert client_user.assigned_trainer_id is None

    # And they can immediately choose again.
    r = c_api.post("/api/auth/client/request-trainer/", {"trainer_id": trainer.id},
                   content_type="application/json")
    assert r.status_code in (200, 201), r.content


def test_one_predicate_decides_what_a_trainer_can_see(seeded_catalogue, make_user):
    """Six routine querysets filtered on the `assigned_trainer` pointer while the
    permission layer required an APPROVED relation — two definitions of the same
    thing, which drift. This asserts the code has one, because fresh data flows
    through the same code and cannot fix a divergence between two code paths."""
    import inspect
    import re

    from routine import models as routine_models
    from routine import views as routine_views

    # Both modules. The regex below used to name three exact spellings in views.py
    # only, and four more sites — `client.assigned_trainer_id == request.user.id` in
    # RoutineTemplateViewSet.generate, `created_by=user.assigned_trainer` in the client
    # exercise queryset, and `self.user.assigned_trainer == user` in two
    # `can_be_accessed_by` methods on routine/models.py — were a second definition of
    # "may this trainer act on this client" that the guard could not see. Any read of
    # the pointer that is not plainly display now fails this.
    offenders = []
    for module in (routine_views, routine_models):
        # Strip comments before searching, so the explanations above the fixes do not
        # count as usages.
        code = "\n".join(line.split("#")[0]
                         for line in inspect.getsource(module).splitlines())
        offenders += [
            f"{module.__name__}: {m}"
            for m in re.findall(
                r"assigned_trainer(?:_id)?\s*(?:=|==)\s*"
                r"(?:user|request\.user|self\.request\.user)"
                r"|assigned_to__assigned_trainer"
                r"|user__assigned_trainer="
                r"|created_by=\w+\.assigned_trainer"
                r"|\.assigned_trainer\s*==\s*user", code)
        ]
    assert not offenders, f"trainer scope still read from the pointer: {offenders}"
    assert "accessible_user_ids" in inspect.getsource(routine_views)


def test_a_trainer_sees_only_clients_who_approved_them(seeded_catalogue, make_user):
    """The pointer and the approved relation agree today, so this pins the behaviour
    rather than the implementation: a client whose relation is not approved is not
    visible in any trainer-scoped routine list."""
    from routine.models import Routine
    from users.models import TrainerClientRelation

    trainer, client_user, t_api, c_api = _assigned_pair(make_user, "scope")
    routine = Routine.objects.create(name="Scoped", days=1, created_by=trainer)
    routine.assigned_to.set([client_user])
    # Reachable ONLY through the client, not through authorship.
    other_trainer = make_user("tr-foreign-author", user_type="trainer")
    foreign = Routine.objects.create(name="Foreign", days=1, created_by=other_trainer)
    foreign.assigned_to.set([client_user])
    assert any(x["id"] == foreign.id
               for x in (lambda b: b.get("results", b) if isinstance(b, dict) else b)(
                   t_api.get("/api/routine/routines/").json())), \
        "the fixture does not reproduce client-reachable visibility"

    r = t_api.get("/api/routine/routines/")
    body = r.json()
    rows = body.get("results", body) if isinstance(body, dict) else body
    assert any(x["id"] == routine.id for x in rows), r.content[:200]

    # The client leaves. The routine is no longer theirs to see through the client.
    c_api.post("/api/auth/client/unassign-trainer/", {}, content_type="application/json")
    TrainerClientRelation.objects.filter(trainer=trainer, client=client_user).delete()
    # A routine written by ANOTHER trainer and reachable only through this client must
    # disappear. The previous assertion here was `... or routine.created_by_id ==
    # trainer.id`, and the routine was created by that trainer two lines above, so the
    # right-hand side was always true and the test proved nothing.
    r = t_api.get("/api/routine/routines/")
    body = r.json()
    rows = body.get("results", body) if isinstance(body, dict) else body
    assert all(x["id"] != foreign.id for x in rows), \
        "a dismissed trainer still sees a routine reachable only through that client"
    # Their own authorship still stands.
    assert any(x["id"] == routine.id for x in rows)


def test_every_engine_meal_says_when_it_is_eaten(seeded_catalogue, make_user):
    """The engine decided WHAT a breakfast is and left WHEN unset. The app had nothing
    to build a schedule from, and `Meal`'s uniqueness key includes `scheduled_time`,
    which Postgres treats as distinct when null — so the key never fired and a
    regenerated day could hold seven lunches."""
    from datetime import time as time_cls

    from diet.models import Meal
    from diet.services.plan_generation import generate_and_save

    user, _chosen = _chooser("mealtimes")
    _out, plan = generate_and_save(user, duration_days=2, snack_count=1, seed_salt="times")
    meals = list(Meal.objects.filter(diet_plan=plan))
    assert meals
    assert all(m.scheduled_time is not None for m in meals), "a meal with no time"

    by_day = collections.defaultdict(list)
    for m in meals:
        by_day[m.date].append(m)
    for day, day_meals in by_day.items():
        keys = [(m.meal_type, m.scheduled_time) for m in day_meals]
        assert len(keys) == len(set(keys)), f"{day} has two meals the database cannot tell apart"
        breakfast = next((m for m in day_meals if m.meal_type == "Breakfast"), None)
        dinner = next((m for m in day_meals if m.meal_type == "Dinner"), None)
        if breakfast and dinner:
            assert breakfast.scheduled_time < dinner.scheduled_time

    # Two snacks in one day get different times, so they are distinguishable.
    from diet.services.meal_plan_factory import scheduled_time_for
    assert scheduled_time_for("Snack", 0) != scheduled_time_for("Snack", 1)
    assert scheduled_time_for("Breakfast", 0) == time_cls(8, 0)


def test_the_dashboards_do_not_count_in_python(seeded_catalogue, make_user):
    """Two analytics paths walked every row they could see and tallied them in the
    worker: completion loaded roughly 200,000 RoutineProgress rows to return at most
    500 summaries, and streaks grouped by the routine's ordinal day instead of the
    calendar date, so a client training daily scored a streak of one.

    Measured by COUNTING QUERIES against two row counts. Reading the method's source for
    the strings `for rp in qs` and `.annotate(` passes any rewrite that keeps the
    spelling and fails any correct one that changes it; what matters is that the work
    stays in the database, and the only way to see that is to add rows and watch the
    cost.
    """
    from datetime import timedelta

    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    from routine.models import Routine, RoutineProgress

    admin = make_user("dash-admin", user_type="admin")
    trainer = make_user("dash-trainer", user_type="trainer")
    client_user = make_user("dash-client")
    routine = Routine.objects.create(name="Dash", created_by=trainer)
    today = timezone.localdate()
    # Built once: `_api_client_for` gives the user a subscription, and a second call for
    # the same user collides on `subscription.user` being unique.
    c = _api_client_for(admin)

    def cost(rows):
        # `(user, routine, day, date)` is unique, so each row gets its own day AND its
        # own date.
        RoutineProgress.objects.all().delete()
        RoutineProgress.objects.bulk_create([
            RoutineProgress(user=client_user, routine=routine, day=i + 1,
                            date=today - timedelta(days=i),
                            status="completed" if i % 2 else "pending")
            for i in range(rows)])
        with CaptureQueriesContext(connection) as captured:
            r = c.get("/api/routine/analytics/completion/")
        assert r.status_code == 200, r.content[:200]
        return len(captured)

    small, large = cost(20), cost(400)
    # Twenty times the rows must not cost more queries. A Python tally shows up here as
    # growth; an aggregate does not.
    assert large <= small, (
        f"{small} queries for 20 rows, {large} for 400 — the count is being done in "
        f"Python, not in the query")


def test_completing_a_meal_is_visible_to_everything_that_listens(seeded_catalogue):
    """`complete_meal` marked components with a bulk `update()`, which fires no
    `post_save` — and completion is the only thing the analytics and achievement
    listeners watch on MealComponent. The app's main "I ate this" button therefore
    recorded nothing downstream."""
    from diet.models import Meal, MealComponent
    from diet.services.plan_generation import generate_and_save

    user, _chosen = _chooser("complete-signal")
    _out, plan = generate_and_save(user, duration_days=1, seed_salt="signal")
    meal = Meal.objects.filter(diet_plan=plan).first()
    c = _api_client_for(user)

    seen = []
    from django.db.models.signals import post_save

    def listener(sender, instance, **kwargs):
        if instance.is_completed:
            seen.append(instance.pk)

    post_save.connect(listener, sender=MealComponent, dispatch_uid="gate-complete")
    try:
        r = c.post(f"/api/diet/v1/client/meals/{meal.id}/complete/",
                   {"action": "complete_meal"}, content_type="application/json")
        assert r.status_code == 200, r.content
    finally:
        post_save.disconnect(sender=MealComponent, dispatch_uid="gate-complete")

    assert len(seen) == meal.components.count() > 0, "completion was invisible to listeners"
    meal.refresh_from_db()
    assert meal.is_completed


def test_swapping_a_food_after_eating_does_not_keep_the_old_calories(seeded_catalogue):
    """A swap rewrote the component's food and left it marked completed, and
    DailyProgress sums the components of completed meals — so the client was credited
    with food they never ate."""
    from diet.models import DailyProgress, Meal, MealComponent
    from diet.services.plan_generation import generate_and_save

    user, _chosen = _chooser("swap-invalidate")
    _out, plan = generate_and_save(user, duration_days=1, seed_salt="swap")
    meal = Meal.objects.filter(diet_plan=plan).first()
    c = _api_client_for(user)
    assert c.post(f"/api/diet/v1/client/meals/{meal.id}/complete/",
                  {"action": "complete_meal"}, content_type="application/json").status_code == 200

    component = MealComponent.objects.filter(meal=meal).first()
    url = "/api/diet/v1/client/meals/interact/"
    offers = c.post(url, {"action": "swap_food", "component_id": component.id},
                    content_type="application/json")
    assert offers.status_code == 200, offers.content
    offered = offers.json()["offers"]
    if not offered:
        pytest.skip("no similar food to swap to in this catalogue")
    r = c.post(url, {"action": "swap_food", "component_id": component.id,
                     "chosen_food_id": offered[0]["food_id"]}, content_type="application/json")
    assert r.status_code == 200, r.content

    component.refresh_from_db()
    meal.refresh_from_db()
    assert not component.is_completed, "the client is credited with food they never ate"
    assert not meal.is_completed


def test_the_progress_endpoints_refuse_a_date_they_cannot_read(seeded_catalogue):
    """Two of the four date-taking endpoints silently fell back to today, so asking
    about an impossible date returned today's numbers with a 200."""
    user, _chosen = _chooser("bad-date")
    c = _api_client_for(user)
    for url in ("/api/diet/v1/client/progress/?date=31-02-2026",
                "/api/diet/v1/client/progress/weekly/?start_date=notadate"):
        r = c.get(url)
        assert r.status_code == 400, (url, r.status_code, r.content[:120])
        assert r.json().get("code") == "bad_date"


def test_the_daily_quota_follows_the_users_day(seeded_catalogue, make_user):
    """Two properties, and they pull against each other.

    The window has to turn over at the CLIENT's midnight: `timezone.now()` is UTC and
    TIME_ZONE is Asia/Damascus, so a daily allowance used to roll at 03:00 local and a
    client generating at 01:00 was still spending yesterday's.

    And the row's identity must not be something the client can WRITE.
    `SubscriptionUsage` is unique on (subscription, feature, period_start), and
    `period_start` was the instant of local midnight — a different instant for every UTC
    offset — so one `POST /api/auth/user/update/ {"preferred_timezone": ...}` between
    generations minted a fresh row with `usage_count = 0`. A 1/day plan yielded 11 extra
    allowances from 12 timezones.

    Keying on the local DATE satisfies both: the date turns over at local midnight, and
    every offset that shares today shares the row. This asserts the BEHAVIOUR either
    side of midnight rather than how the stamp happens to render.
    """
    import datetime as _dt

    from subscription import quota

    class _Sub:
        def __init__(self, user):
            self.user = user

    traveller = make_user("gate_quota_tz", user_type="client")
    traveller.preferred_timezone = "Asia/Damascus"
    traveller.save(update_fields=["preferred_timezone"])
    subscription = _Sub(traveller)

    damascus = _dt.timezone(_dt.timedelta(hours=3))
    before_midnight = _dt.datetime(2026, 5, 10, 23, 30, tzinfo=damascus)
    after_midnight = _dt.datetime(2026, 5, 11, 0, 30, tzinfo=damascus)
    later_that_day = _dt.datetime(2026, 5, 11, 22, 0, tzinfo=damascus)

    start_before, _e1 = quota._period(subscription, "day", before_midnight)
    start_after, end_after = quota._period(subscription, "day", after_midnight)
    start_later, _e3 = quota._period(subscription, "day", later_that_day)

    assert start_before != start_after, \
        "the allowance did not turn over at the client's midnight"
    assert start_after == start_later, \
        "the allowance turned over again during the client's own day"
    assert (end_after - start_after).days == 1

    # And moving timezone inside one local day must not mint a second row.
    traveller.preferred_timezone = "Europe/Berlin"
    traveller.save(update_fields=["preferred_timezone"])
    berlin_same_day = _dt.datetime(2026, 5, 11, 12, 0, tzinfo=damascus)
    start_berlin, _e4 = quota._period(subscription, "day", berlin_same_day)
    assert start_berlin == start_after, \
        "changing timezone minted a fresh allowance for the same day"


def test_an_achievement_counts_meals_not_mouthfuls(seeded_catalogue, django_capture_on_commit_callbacks):
    """Analytics writes one `meal_completed` row per COMPONENT, so a four-food meal
    counted as four: "log your first meal" fired on the first food and "log 100 meals"
    at about twenty-five."""
    from django.test import Client

    from achievements.engine import AchievementEngine
    from diet.models import Meal
    from diet.services.plan_generation import generate_and_save

    user, _chosen = _chooser("achieve-meals")
    _out, plan = generate_and_save(user, duration_days=1, seed_salt="ach")
    meal = Meal.objects.filter(diet_plan=plan).first()
    assert meal.components.count() >= 2, "fixture needs a multi-food meal"

    c = _api_client_for(user)
    with django_capture_on_commit_callbacks(execute=True):
        r = c.post(f"/api/diet/v1/client/meals/{meal.id}/complete/",
                   {"action": "complete_meal"}, content_type="application/json")
    assert r.status_code == 200, r.content

    counted = AchievementEngine._get_metric_value(user, "meal_count", {})
    assert counted == 1, f"one meal counted as {counted}"


def test_the_ai_calls_the_closest_day_the_best_one(seeded_catalogue):
    """Ranking by `calories_pct` reported a 200% binge as the client's best adherence
    day and a fasting day as their worst — the opposite of the adherence score computed
    beside it, which this tool hands to the model as fact.

    Asserted by RUNNING the tool on three days with known outcomes. It used to read the
    module's source for two strings, which fails a correct engine on a rename and passes
    a reintroduced bug written as `sorted(daily_data, key=...)[-1]`.
    """
    from datetime import timedelta

    from ai_assistant.tools.diet_tools import get_diet_adherence
    from diet.models import DailyProgress
    from diet.services.plan_generation import generate_and_save

    user, _chosen = _chooser("adherence-days")
    _out, plan = generate_and_save(user, duration_days=1, seed_salt="adh")
    today = timezone.localdate()
    # On target, far over, far under.
    for offset, consumed in ((1, 2000.0), (2, 4000.0), (3, 300.0)):
        DailyProgress.objects.update_or_create(
            user=user, diet_plan=plan, date=today - timedelta(days=offset),
            defaults={"target_calories": 2000.0, "calories_consumed": consumed,
                      "meals_completed": 1, "total_meals": 3})

    out = get_diet_adherence(user, days=7)
    assert out["days_tracked"] == 3, out
    assert out["best_day"]["calories_consumed"] == 2000.0, out["best_day"]
    # The worst is whichever is FURTHEST from target, and 4000 (+100%) is further than
    # 300 (-85%).
    assert out["worst_day"]["calories_consumed"] == 4000.0, out["worst_day"]


def test_no_tool_reports_a_score_for_a_client_who_never_tracked(seeded_catalogue):
    """`round(total/count, 1) if count else 0` told the assistant a client who has never
    tracked a day scored ZERO on adherence, beside `days_tracked: 0` that it does not
    read. No data is not a score."""
    from ai_assistant.tools.diet_tools import get_diet_adherence
    from diet.services.plan_generation import generate_and_save

    user, _chosen = _chooser("adherence-empty")
    generate_and_save(user, duration_days=1, seed_salt="adh0")
    out = get_diet_adherence(user, days=7)
    assert out["days_tracked"] == 0, out
    assert out["average_adherence_pct"] is None, out
    assert out.get("message"), "the model is told nothing about why the number is absent"


def test_no_tool_prints_health_data_to_stdout(seeded_catalogue):
    """Username plus BMI, BMR, goal and calorie target were printed to the log stream,
    unredacted and outside every retention window the privacy registry enforces on the
    same data everywhere else."""
    import pathlib

    root = REPO_ROOT / "ai_assistant" / "tools"
    files = list(root.glob("*.py"))
    assert files, f"no tool modules found under {root}"
    # ANY output call carrying a health field, not just `print(`. The scan looked for
    # lines starting with `print(`, so `logger.info(f"...{bmi}...")` leaked exactly the
    # same data past exactly the same test.
    health = ("bmi", "bmr", "daily_calories", "calorie_target", "weight", "body_fat")
    emitters = ("print(", "logger.", "logging.", "sys.stdout", "sys.stderr")
    offenders = []
    for path in files:
        for n, line in enumerate(path.read_text().splitlines(), 1):
            body = line.strip()
            if not any(body.startswith(e) or f" {e}" in body for e in emitters):
                continue
            if any(f"{{{h}" in body.lower() or f"%({h})" in body.lower()
                   or f"' + str({h}" in body.lower() for h in health):
                offenders.append(f"{path.name}:{n} {body[:80]}")
    assert not offenders, offenders


def test_every_diet_list_endpoint_answers_in_one_shape(seeded_catalogue):
    """The contract promises {count, next, previous, results} everywhere. Five diet
    lists answered with `total_count` or a nested `pagination` object instead, so a
    client reading `count` and `next` — as it does for every other list — got null."""
    user, _chosen = _chooser("list-shape")
    c = _api_client_for(user)
    for url in ("/api/diet/v1/food/list/?page=1",
                "/api/diet/v1/food/categories/",
                "/api/diet/v1/food/search/?q=chicken",
                "/api/diet/v1/my/diet-plans/",
                "/api/diet/v1/client/progress/weekly/",
                "/api/diet/v1/preferences/food-category/"):
        r = c.get(url)
        assert r.status_code == 200, (url, r.content[:120])
        body = r.json()
        for key in ("count", "next", "previous", "results"):
            assert key in body, f"{url} is missing {key}"
        assert isinstance(body["results"], list)
        assert isinstance(body["count"], int)


def test_the_catalogue_speaks_arabic_too(seeded_catalogue):
    """An Arabic client browsing foods saw English names, then Arabic ones for the same
    foods inside a meal."""
    user, _chosen = _chooser("catalogue-ar")
    c = _api_client_for(user)
    for url in ("/api/diet/v1/food/list/?page=1", "/api/diet/v1/food/search/?q=rice"):
        rows = c.get(url).json()["results"]
        assert rows, url
        assert all("name_ar" in row for row in rows), url
        assert any(_ARABIC.search(row["name_ar"] or "") for row in rows), url






def test_generating_again_replaces_the_plan_it_overlaps(seeded_catalogue):
    """Generating again is not an error, and it must not leave two plans behind.

    This asserted a 409 with `code: "plan_already_exists"`, which came from a check in
    `DietPlan.clean()` — a CROSS-ROW rule in a row validator, so it also re-fired on
    every later `save(update_fields=[...])` and could refuse a plan that was already
    committed and live. The rule is enforced by `save_plan`'s lock now. What the client
    sees is what the code was always written to do: the new plan replaces the old one.
    """
    from diet.services.plan_generation import generate_and_save

    from diet.models import DietPlan

    user, _chosen = _chooser("conflict-answer")
    _out, first = generate_and_save(user, duration_days=2, seed_salt="one")
    assert first.is_active

    # Sequentially there is no conflict: a new plan SUPERSEDES the one it overlaps,
    # which is what "generate again" should do.
    _out, second = generate_and_save(user, duration_days=2, seed_salt="two")
    first.refresh_from_db()
    assert second.is_active and not first.is_active
    assert DietPlan.objects.filter(user=user, is_active=True).count() == 1

    # A third time, through the endpoint, so the wire answer is asserted too and not
    # just the service. This used to read the view's SOURCE for three strings, which
    # passes whether or not the endpoint behaves.
    c = _api_client_for(user)
    r = c.post("/api/diet/v1/plans/generate-rule/", {"duration_days": 2},
               content_type="application/json")
    assert r.status_code == 201, r.content[:200]
    assert DietPlan.objects.filter(user=user, is_active=True).count() == 1
    assert DietPlan.objects.filter(user=user).count() == 3


# ============================================================================
# The safety and locale properties, asserted end to end rather than by reading
# the code that is supposed to provide them.
# ============================================================================

def test_a_declared_allergy_survives_the_whole_pipeline(seeded_catalogue):
    """Not one ingredient a client cannot eat, on any path, and the plan says so.

    Every stage has its own reason to be trusted — the pool filters, the recipe path
    re-checks, persistence validates — and each of those was true at some point while an
    allergen still reached the plate. The only assertion worth having is over the rows
    that were actually written.
    """
    from diet.models import MealComponent, UserFoodPreference
    from diet.services.meal_validator import AllergenChecker, SAFE
    from diet.services.plan_generation import generate_and_save

    user, _chosen = _chooser("allergy-pipeline")
    UserFoodPreference.objects.update_or_create(
        user=user, defaults={"allergies": "nuts, dairy"})

    _out, plan = generate_and_save(user, duration_days=3, seed_salt="allergy")
    checker = AllergenChecker("nuts, dairy")
    served = {c.food for c in
              MealComponent.objects.filter(meal__diet_plan=plan).select_related("food")}
    assert served, "nothing was served, so nothing was proven"
    # Not "no violation" — no ingredient the checker cannot positively clear. An
    # UNVERIFIED row is one whose composition nobody has written down, and the catalogue
    # declares one for every food it holds.
    unclear = sorted(f.name_en for f in served
                     if checker.check_food(f).verdict != SAFE)
    assert not unclear, unclear
    assert plan.allergen_report.get("is_safe") is True, plan.allergen_report


def test_a_trainer_cannot_serve_a_food_the_client_declared(seeded_catalogue, make_user):
    """The generator has checked allergies since the allergen work landed. The trainer's
    own write never did, and `allergen_report` is written once at generation — so a
    client who declared peanuts could be given peanut butter by their trainer and their
    plan still said `is_safe: true`."""
    from django.core.exceptions import ValidationError

    from diet.models import FoodItem, UserFoodPreference
    from diet.trainer_services import TrainerDietPlanService

    trainer, client_user, _t_api, _c_api = _trainer_and_client(make_user, "tr-allergy")
    UserFoodPreference.objects.update_or_create(
        user=client_user, defaults={"allergies": "peanuts"})

    peanut = FoodItem.objects.filter(name_en__icontains="Peanut").first()
    assert peanut is not None, "the catalogue has no peanut food to test with"
    with pytest.raises(ValidationError) as refused:
        TrainerDietPlanService(trainer)._validated_items(
            [{"food_id": peanut.id, "quantity": 30}], "Lunch", client=client_user)
    assert refused.value.code == "declared_allergen", refused.value.code


def test_every_diet_error_carries_a_machine_readable_code(seeded_catalogue):
    """API_CONTRACT.md: "Branch on `code`, never on `detail`" — and 128 of this app's
    error returns carried neither `code` nor `detail`, so an Arabic client received a
    translated sentence and no way to tell a bad date from a server fault."""
    user, _chosen = _chooser("error-codes")
    c = _api_client_for(user)
    probes = [
        ("get", "/api/diet/v1/food/list/?page=notanumber", None),
        ("post", "/api/diet/v1/plans/generate-rule/", []),
        ("get", "/api/diet/v1/client/meals/99999999/", None),
        ("get", "/api/diet/v1/nutrition/plan/99999999/", None),
        ("post", "/api/diet/v1/client/meals/interact/", {"action": "nope"}),
        ("post", "/api/diet/v1/trainer/meals/", {"meal_type": "Lunch"}),
    ]
    for method, url, body in probes:
        r = (c.get(url) if method == "get"
             else c.post(url, body, content_type="application/json"))
        assert r.status_code >= 400, (url, r.status_code)
        data = r.json()
        assert data.get("code"), (url, data)
        assert data.get("error") and data.get("detail"), (url, data)


def test_an_arabic_request_still_carries_the_english_name(seeded_catalogue):
    """`FoodItem.name` is a modeltranslation virtual field, so `{"name": food.name,
    "name_ar": ...}` emitted the SAME Arabic string twice under an Arabic request and
    dropped the English name entirely — while the contract promises both languages on
    every row so the app can switch without a refetch."""
    from django.utils import translation

    from diet.services.plan_generation import generate_and_save

    user, _chosen = _chooser("both-languages")
    _out, plan = generate_and_save(user, duration_days=1, seed_salt="lang")
    c = _api_client_for(user)
    try:
        r = c.get(f"/api/diet/v1/plan/{plan.id}/meals-with-ingredients/",
                  HTTP_ACCEPT_LANGUAGE="ar")
        assert r.status_code == 200, r.content[:200]
        rows = [comp for meal in r.json()["meals"] for comp in meal.get("components", [])]
        assert rows, r.json()
        assert not [x for x in rows if x["name"] == x["name_ar"]], rows[:3]
    finally:
        translation.deactivate()


def test_the_plan_is_the_same_whichever_language_asked_for_it(seeded_catalogue):
    """`Meta.ordering = ['name', 'id']` is rewritten by modeltranslation to the ACTIVE
    language, so the recipe population `random.choices` samples from was ordered
    differently under Arabic and the same client with the same seed on the same day got
    a different dish from the Arabic app than from the English one."""
    from django.utils import translation

    from diet.services.rule_based_planner import RuleBasedPlanner

    user, _chosen = _chooser("locale-stable")
    kcal = user.calculate_daily_calories()

    def plan_under(language):
        with translation.override(language):
            out = RuleBasedPlanner(user, seed_salt="locale").generate(
                daily_kcal=kcal, duration_days=3, snack_count=1, start_date="2026-03-02")
        return [tuple(sorted((i.food_id, i.grams) for i in meal.ingredients))
                for meal in out.plan]

    english = plan_under("en")
    assert english, "nothing was planned"
    assert english == plan_under("ar"), "the plan changed with the display language"


def test_a_refund_takes_the_money_back_and_not_only_the_access(
        make_user, django_capture_on_commit_callbacks):
    """`refund_payment` withdrew the ACCESS a payment bought and wrote nothing to the
    wallet ledger — while `wallet.signals` credits the client on completion and pays the
    trainer out of that credit. So a gateway refund or a chargeback cancelled the
    subscription, returned the money to the card, and left the minted top-up and the
    trainer's payout in place: every refund was a net loss of the amount to the platform.

    The test that existed built a client with NO trainer relation, so the payout leg never
    ran and the assertion never reached the ledger.
    """
    from decimal import Decimal as D

    from subscription.models import Payment, Subscription, SubscriptionPlan
    from subscription.services.payment_service import PaymentService
    from users.models import TrainerClientRelation
    from wallet.models import Transaction, Wallet

    client_user = make_user("ref-client")
    trainer = make_user("ref-trainer", user_type="trainer")
    TrainerClientRelation.objects.create(trainer=trainer, client=client_user, status="approved")
    client_user.assigned_trainer = trainer; client_user.save()

    plan = SubscriptionPlan.objects.create(name="ref plan", price=D("10.00"), currency="USD",
                                           duration_days=30, description="x")
    from django.utils import timezone
    from datetime import timedelta
    sub = Subscription.objects.create(user=client_user, plan=plan, status="pending",
                                      start_date=timezone.now(),
                                      end_date=timezone.now() + timedelta(days=30))
    pay = Payment.objects.create(subscription=sub, amount=D("10.00"), currency="USD",
                                 status="pending", payment_method="manual")
    # The payout fires on commit — `PaymentWebhookView` wraps the whole update in a
    # transaction that can still roll back, and the receiver moves money — so the test
    # has to let the callbacks run.
    with django_capture_on_commit_callbacks(execute=True):
        PaymentService.complete_payment(pay.id, {"amount": "10.00", "currency": "USD",
                                                 "status": "completed",
                                                 "transaction_id": "probe-1"})
    # Since plan step 4 the payout goes to the PLATFORM, not the trainer: a plan payment
    # stays 100% with the platform (the owner, 2026-09-19). What this test is about is
    # unchanged — a refund must take the money back, not only the access.
    from wallet.utils import get_platform_revenue_wallet
    pw = get_platform_revenue_wallet(); cw = Wallet.objects.get(owner=client_user)
    tw = Wallet.objects.get(owner=trainer)
    pw.refresh_from_db(); cw.refresh_from_db(); tw.refresh_from_db()
    assert pw.balance == D("10.00")
    assert tw.balance == D("0.00"), "a plan payment is not the trainer's"

    with django_capture_on_commit_callbacks(execute=True):
        PaymentService.refund_payment(pay.id, reason="chargeback")
    pw.refresh_from_db(); cw.refresh_from_db()
    assert pw.balance == D("0.00"), "the platform kept a refunded payout"
    assert cw.balance == D("0.00"), "the minted credit was never taken back"
    assert Transaction.objects.filter(tx_type="reversal").count() == 2


@pytest.mark.django_db
def test_a_plan_generated_in_arabic_keeps_its_english_columns(seeded_catalogue):
    """`Meal.name` and `Meal.reason` are the ENGLISH columns, with `name_ar`/`reason_ar`
    beside them, and both were composed from modeltranslation virtual fields — so a plan
    generated from the Arabic app stored Arabic in them. A client who later views in
    English reads Arabic for the life of that plan, because these sentences are built at
    generation time out of the foods on the plate and cannot be translated back.

    Four places had to be right for this to hold: the reason composer, the dish title,
    and both constructions of `AIIngredient`, whose `name` is what
    `MealPlanFactory._compose_name` writes into `Meal.name` for a meal with no dish.
    """
    import re

    from django.utils import translation

    from diet.models import Meal
    from diet.services.plan_generation import generate_and_save

    ARABIC = re.compile(r"[؀-ۿ]")

    user, _ = _chooser("lang-persist")
    with translation.override("ar"):
        _out, plan = generate_and_save(user, duration_days=2, seed_salt="langp")
    translation.deactivate()
    bad = []
    for meal in Meal.objects.filter(diet_plan=plan):
        for field in ("name", "reason"):
            value = getattr(meal, field) or ""
            if ARABIC.search(value):
                bad.append((meal.id, field, value[:60]))
    assert not bad, bad


def test_a_main_meal_is_built_around_a_protein(seeded_catalogue):
    """Lunch and dinner have to be meals, not just numbers that add up.

    `PlannerPolicy` declares `protein_floor_main_g = 40.0` and nothing has ever read it,
    so the only thing asking a main meal for protein is its own target — and on a small
    calorie budget that target is small. The failure this guards against is a dinner of
    barley, onion and tahini: 3.6 g of protein against a 30.7 g target, arithmetically
    defensible and not dinner.

    Asserted over what the engine SERVES, across the whole profile range, because a
    fixed gram floor would fight the target for a client whose dinner is legitimately a
    23 g meal.
    """
    import uuid

    from diet.services.rule_based_planner import RuleBasedPlanner
    from tests.diet_quality import HARNESS_START_DATE, PROFILES, _make_client

    short = []
    mains = 0
    for index, profile in enumerate(PROFILES):
        user = _make_client(f"pfloor{uuid.uuid4().hex[:6]}", *profile)
        out = RuleBasedPlanner(user, seed_salt=f"floor{index}").generate(
            daily_kcal=user.calculate_daily_calories(), duration_days=7, snack_count=1,
            start_date=HARNESS_START_DATE)
        for meal in out.plan:
            if (meal.meal_type or "") not in ("Lunch", "Dinner"):
                continue
            mains += 1
            served = float((meal.total_nutrition or {}).get("protein", 0) or 0)
            target = float((meal.target or {}).get("protein", 0) or 0)
            if target > 0 and served < 0.6 * target:
                short.append((meal.meal_type, round(served, 1), round(target, 1),
                              (meal.meal_name or "")[:40]))

    assert mains >= 6 * 7 * 2 * 0.8, f"only {mains} main meals were planned"
    assert not short, (
        f"{len(short)} of {mains} main meals came in under 60% of their own protein "
        f"target: {short[:5]}")


def test_the_day_counts_what_the_client_says_they_ate(seeded_catalogue):
    """Two defects in one aggregate, both of them about the client's own record.

    It filtered on `meal__is_completed`, so a client ticking ingredients off as they ate
    saw 0 kcal all the way through the meal and then the whole meal at once — including
    the parts they never marked. And it summed the PLANNED grams, so
    `actual_quantity_consumed` — the one field a client fills in to say "I only had a
    third of it" — was collected, echoed back over the API, and changed no number they
    could ever see.
    """
    from diet.models import DailyProgress, Meal, MealComponent
    from diet.services.plan_generation import generate_and_save

    user, _chosen = _chooser("day-counts")
    _out, plan = generate_and_save(user, duration_days=1, seed_salt="counts")
    meal = Meal.objects.filter(diet_plan=plan).first()
    components = list(MealComponent.objects.filter(meal=meal).select_related("food"))
    assert len(components) >= 3, "need a few components to tick off separately"
    c = _api_client_for(user)

    def eaten():
        row = DailyProgress.objects.filter(
            user=user, diet_plan=plan, date=meal.date).first()
        return round(row.calories_consumed, 1) if row else 0.0

    assert eaten() == 0.0

    r = c.post(f"/api/diet/v1/client/meals/{meal.id}/complete/",
               {"action": "complete_component", "component_id": components[0].id},
               content_type="application/json")
    assert r.status_code == 200, r.content[:200]
    after_one = eaten()
    assert after_one > 0.0, "one ingredient eaten counted as nothing"

    # A third of the next one is a third of its calories, not all of them.
    whole = components[1].calculate_nutrition()["calories"]
    r = c.post(f"/api/diet/v1/client/meals/{meal.id}/complete/",
               {"action": "complete_component", "component_id": components[1].id,
                "actual_quantity": float(components[1].quantity) / 3.0},
               content_type="application/json")
    assert r.status_code == 200, r.content[:200]
    added = eaten() - after_one
    assert added < whole * 0.5, (
        f"a third of a {whole:.0f} kcal portion added {added:.0f} kcal")
    assert added > 0.0


def test_swapping_one_food_does_not_erase_the_rest_of_the_day(seeded_catalogue):
    """A swap clears the component's completion and the meal's, and the day's totals used
    to be counted per MEAL — so a client who ate breakfast and then swapped the almonds
    for pistachios watched their whole day reset to zero, and the two things they had
    actually eaten stopped counting."""
    from diet.models import DailyProgress, Meal, MealComponent
    from diet.services.plan_generation import generate_and_save

    user, _chosen = _chooser("swap-keeps-day")
    _out, plan = generate_and_save(user, duration_days=1, seed_salt="swapkeep")
    meal = Meal.objects.filter(diet_plan=plan).first()
    c = _api_client_for(user)

    def eaten():
        row = DailyProgress.objects.filter(
            user=user, diet_plan=plan, date=meal.date).first()
        return round(row.calories_consumed, 1) if row else 0.0

    r = c.post(f"/api/diet/v1/client/meals/{meal.id}/complete/",
               {"action": "complete_meal"}, content_type="application/json")
    assert r.status_code == 200, r.content[:200]
    before = eaten()
    assert before > 0.0

    component = MealComponent.objects.filter(meal=meal).first()
    offers = c.post("/api/diet/v1/client/meals/interact/",
                    {"action": "swap_food", "component_id": component.id},
                    content_type="application/json").json().get("offers", [])
    assert offers, "no alternative was offered"
    r = c.post("/api/diet/v1/client/meals/interact/",
               {"action": "swap_food", "component_id": component.id,
                "chosen_food_id": offers[0]["food_id"]},
               content_type="application/json")
    assert r.status_code == 200, r.content[:200]

    after = eaten()
    assert after > 0.0, "the whole day was erased by one swap"
    assert after < before, "the rejected food is still being counted"


def test_the_policy_override_is_wired(settings):
    """`PlannerPolicy` exists so "a dietitian could not change one without a deploy" is
    not true — and its one override path read `settings.DIET_PLANNER_POLICY`, which was
    declared in no settings module, so every constant in it was in fact unreachable to a
    deployment. Two other dead reads in the same function (`DIET_CONFIG`,
    `AI_CHEF_CONFIG["MACRO_TOLERANCE"]`) had already been found; a comment in the middle
    of the paragraph describing them asserted this third one was tested. It was not.

    A dict field MERGES. `{"tolerance": {"fat": 0.25}}` means widen fat, and replacing the
    dict wholesale left it with one key — so `tolerance.get("carb", 0.10)` fell back and
    silently tightened carbohydrate from 0.20 and protein from 0.15.
    """
    from diet.planner.policy import load_policy

    assert hasattr(settings, "DIET_PLANNER_POLICY"), "the override path is not declared"
    default = load_policy("maintain")

    settings.DIET_PLANNER_POLICY = {"*": {"tolerance": {"fat": 0.25}}}
    widened = load_policy("maintain")
    assert widened.tolerance["fat"] == 0.25
    for macro in ("calories", "protein", "carb"):
        assert widened.tolerance[macro] == default.tolerance[macro], (
            f"widening fat moved {macro}")

    settings.DIET_PLANNER_POLICY = {"lose": {"snack_kcal": 150.0}}
    assert load_policy("lose").snack_kcal == 150.0
    assert load_policy("maintain").snack_kcal == default.snack_kcal, \
        "a per-goal override leaked into another goal"

    settings.DIET_PLANNER_POLICY = {}
    assert load_policy("maintain").tolerance == default.tolerance


def test_every_shipped_achievement_can_be_earned():
    """A badge nobody can reach is a promise the platform breaks.

    Two shipped: `marathon_master` needed `PerformanceMetric(metric_type='distance_run')`,
    which `analytics.recorder.record_metric` has never once been called with, and
    `perfectionist` needed a metric whose body was `return 0` under a comment saying it
    was a placeholder. Both returned a value that could not grow, so a user could chase
    them forever.

    `IMPLEMENTED_CRITERIA` is the list of metrics the engine can actually compute; this
    holds it and the registry in step in both directions, so neither can drift.
    """
    from achievements.engine import IMPLEMENTED_CRITERIA, AchievementEngine
    from achievements.registry import ACHIEVEMENTS

    shipped = {a.criteria_type for a in ACHIEVEMENTS}
    assert shipped, "no achievements are defined"
    unreachable = sorted(shipped - IMPLEMENTED_CRITERIA)
    assert not unreachable, (
        f"these achievements use a metric the engine cannot compute: {unreachable}")

    # And the other way: a metric declared implemented must be handled by the dispatcher,
    # or the declaration is the thing that is stale.
    import inspect

    source = inspect.getsource(AchievementEngine._get_metric_value)
    unhandled = sorted(c for c in IMPLEMENTED_CRITERIA if f"'{c}'" not in source)
    assert not unhandled, (
        f"IMPLEMENTED_CRITERIA names metrics the dispatcher does not handle: {unhandled}")


def test_an_admin_cannot_poll_a_task_that_was_never_issued(seeded_catalogue, make_user):
    """`task_belongs_to` returned True for an admin before looking the id up at all, so
    polling any string produced a fabricated `200 PENDING` and support tooling waited
    forever on a task that had never existed."""
    admin = make_user("task-admin", user_type="admin")
    c = _api_client_for(admin)
    r = c.get("/api/diet/v1/plans/tasks/never-issued-at-all/")
    assert r.status_code == 404, (r.status_code, r.content[:200])
    assert r.json().get("code") == "not_found", r.content[:200]


def test_a_search_reports_how_many_matches_there_are(seeded_catalogue):
    """`count` was the length of a silently truncated ten-row slice, with `next: null`, so
    a catalogue holding two hundred matches told the client there were ten and gave it no
    way to ask for the rest. And the lookup went through `name`, a translated virtual
    field, so an English query from an Arabic session matched nothing."""
    user, _chosen = _chooser("search-total")
    c = _api_client_for(user)

    r = c.get("/api/diet/v1/food/search/?q=e&page_size=5")
    assert r.status_code == 200, r.content[:200]
    body = r.json()
    assert len(body["results"]) <= 5
    assert body["count"] > len(body["results"]), (
        "count is the size of the page, not the number of matches")
    assert body["next"], "there is more and no way to ask for it"

    # Either language finds the food.
    assert c.get("/api/diet/v1/food/search/?q=chicken").json()["count"] > 0
    assert c.get("/api/diet/v1/food/search/?q=دجاج").json()["count"] > 0


def test_a_plan_is_refused_rather_than_served_hollowed_out(seeded_catalogue):
    """The allergen check at save time re-reads the client's allergies from the database
    ON PURPOSE, so it can disagree with the constraints the pool was built from — and a
    thirty-day generation takes about forty seconds, which is time enough for someone to
    add "egg, dairy" in the app.

    It used to drop the offending ingredients and persist the remains, and nothing
    recomputed the meal's name, its stored target, the plan's daily calories or the
    delivery report. Measured before this: a client on 2,633 kcal got a day of 1,409, a
    breakfast named "Kishk Breakfast + Salmon Fillet" holding olive oil and an onion, and
    a snack named "Egg, Pita Bread and Peach" that was a peach — while `Meal.target` still
    said 852 kcal and the plan reported `met: true`.
    """
    from diet.exceptions import PersistenceError
    from diet.models import DietPlan, UserFoodPreference
    from diet.services.diet_persistence import DietPersistenceService
    from diet.services.rule_based_planner import RuleBasedPlanner

    user, _chosen = _chooser("hollowed-out")
    plan_output = RuleBasedPlanner(user, seed_salt="hollow").generate(
        daily_kcal=user.calculate_daily_calories(), duration_days=2, snack_count=1,
        start_date="2026-03-02")
    assert plan_output.plan, "nothing was planned"

    # The client edits their allergies between the plan being built and being saved.
    UserFoodPreference.objects.update_or_create(
        user=user, defaults={"allergies": "egg, dairy, gluten, fish, nuts"})

    before = DietPlan.objects.filter(user=user).count()
    with pytest.raises(PersistenceError) as refused:
        DietPersistenceService(user).save_plan(plan_output, meal_count=3, snack_count=1)
    assert str(refused.value).startswith("preferences_changed_during_generation"), \
        str(refused.value)
    assert DietPlan.objects.filter(user=user).count() == before, \
        "a plan that could not be served was left behind"


# --------------------------------------------------------------------------------
# API_CONTRACT.md is the document the Flutter client is built from, and nothing
# checked it against the URLconf. It drifted three ways at once: it documented
# `GET`/`PATCH /api/auth/profile/` as the home of `preferred_timezone` and
# `workout_reminder_hour` (that route does not exist and both fields are written with
# POST elsewhere), and it omitted `client/respond-to-invite/` — the only way a client
# can accept a trainer's invitation, leaving the documented `trainer/assign-client/`
# as an invitation nothing could answer — along with `client/trainer-invites/` and
# `notifications/message-client/`.
# --------------------------------------------------------------------------------

def _contract_documented_paths():
    from pathlib import Path as _Path
    text = (_Path(__file__).resolve().parent.parent / "API_CONTRACT.md").read_text()
    out = {}
    for m in re.finditer(r"^\|\s*`([A-Z, ]+)`\s*\|\s*`(/[^`]+)`\s*\|", text, re.M):
        out[m.group(2)] = set(x.strip() for x in m.group(1).split(","))
    return out


def _resolved_api_paths():
    from django.urls import get_resolver
    from django.urls.resolvers import URLPattern, URLResolver

    out = {}

    def walk(patterns, prefix=""):
        for p in patterns:
            if isinstance(p, URLResolver):
                walk(p.url_patterns, prefix + str(p.pattern))
            elif isinstance(p, URLPattern):
                out["/" + prefix + str(p.pattern)] = True

    walk(get_resolver().url_patterns)
    return out


def _contract_normalise(path):
    path = re.sub(r"\(\?P<[^>]+>[^)]*\)", "{id}", path)
    path = re.sub(r"<[^>]+>", "{id}", path)
    path = re.sub(r"\{[a-zA-Z_]+\}", "{id}", path)
    path = path.replace("^", "").replace("$", "").replace("\\.", ".")
    # DRF's optional `.json` format suffix on every router route: one URL, not two.
    path = re.sub(r"\.\{id\}/?\??$", "", path)
    return path if path.endswith("/") else path + "/"


def test_every_api_route_is_in_the_contract_and_every_documented_route_exists():
    documented = {_contract_normalise(p) for p in _contract_documented_paths()}
    # `?format=json` variants are the same endpoint written twice, so they are dropped
    # on the RAW pattern — after normalisation `<drf_format_suffix:format>` looks exactly
    # like a real `<int:pk>` and a filter there would swallow every detail route.
    resolved = {_contract_normalise(p) for p in _resolved_api_paths()
                if "format" not in p}

    live = {p for p in resolved if p.startswith("/api/")}
    docs = {p for p in documented if p.startswith("/api/")}

    undocumented = sorted(live - docs)
    phantom = sorted(docs - live)
    assert not undocumented, (
        "routes exist that API_CONTRACT.md does not list: " + ", ".join(undocumented))
    assert not phantom, (
        "API_CONTRACT.md lists routes that do not exist: " + ", ".join(phantom))


def test_the_contract_does_not_point_the_client_at_a_route_that_is_not_there():
    """The prose, not the table. `/api/auth/profile/` was named in the reminder section
    as the home of both scheduling fields and has never existed."""
    from pathlib import Path as _Path
    text = (_Path(__file__).resolve().parent.parent / "API_CONTRACT.md").read_text()
    assert "/api/auth/profile/" not in text


# --------------------------------------------------------------------------------
# Client-fault input answered 500. A sweep of every argument-free POST route found 18
# endpoints that crashed on the body `[]` — a JSON array reaches `request.data` as a
# `list`, and `list` has no `.get` — plus a NUL byte in a query string or a field, a
# non-numeric id reaching `.get(id=...)`, and a JSON number where `.lower()` was
# called. Each was a free error-budget burn on a trivially reachable path, and
# `respond-to-invite` is the route a client uses to accept a trainer.
# --------------------------------------------------------------------------------

_MALFORMED_BODY_ROUTES = [
    "/api/analytics/sessions/end_session/",
    "/api/auth/client/request-trainer/",
    "/api/auth/client/respond-to-invite/",
    "/api/auth/device-token/",
    "/api/auth/resend-otp/",
    "/api/auth/token/logout/",
    "/api/auth/trainer/assign-client/",
    "/api/auth/trainer/respond-to-request/",
    "/api/auth/trainer/unassign-client/",
    "/api/auth/verify-otp/",
    "/api/routine/exercisesetlogs/bulk-create/",
    "/api/routine/set-logs/bulk-create/",
    "/api/routine/user-exercise-progress/bulk-complete/",
    "/api/social/follows/follow_user/",
    "/api/social/follows/unfollow_user/",
    "/api/subscription/v1/access/check/",
    "/api/subscription/v1/admin/management/",
    "/api/subscription/v1/gateways/",
]


def test_a_json_array_body_is_a_400_not_a_500(make_user, api):
    admin = make_user("gate_shape_admin", user_type="admin")
    trainer = make_user("gate_shape_trainer", user_type="trainer")
    crashed = []
    for url in _MALFORMED_BODY_ROUTES:
        for actor in (admin, trainer):
            resp = api(actor).post(url, data="[]", content_type="application/json")
            if resp.status_code >= 500:
                crashed.append((url, resp.status_code))
                break
    assert not crashed, f"a JSON array body still crashes: {crashed}"


def test_a_nul_byte_is_a_400_not_a_500(make_user, api):
    """Postgres refuses a NUL inside a text value and psycopg raises a plain ValueError
    at execute time, deep inside a view — usually inside a broad `except Exception`
    that answers 500. The parser rejects it at the boundary instead."""
    import json as _json

    client = make_user("gate_nul_client", user_type="client")
    admin = make_user("gate_nul_admin", user_type="admin")

    for url, body in (
        ("/api/auth/device-token/", {"token": "a\u0000b"}),
        ("/api/analytics/sessions/end_session/", {"session_id": "a\u0000b"}),
    ):
        resp = api(client).post(url, data=_json.dumps(body),
                                content_type="application/json")
        assert resp.status_code == 400, (url, resp.status_code, resp.content[:200])

    for url in ("/api/social/users/public-profile/by_username/?username=%00",
                "/api/auth/trainers/public/?search=%00"):
        resp = api(admin).get(url)
        assert resp.status_code == 400, (url, resp.status_code, resp.content[:200])


def test_a_non_numeric_id_is_a_400_not_a_500(make_user, api):
    import json as _json

    admin = make_user("gate_badid_admin", user_type="admin")
    client = make_user("gate_badid_client", user_type="client")

    for url in ("/api/social/follows/follow_user/", "/api/social/follows/unfollow_user/"):
        resp = api(admin).post(url, data=_json.dumps({"user_id": "abc"}),
                               content_type="application/json")
        assert resp.status_code == 400, (url, resp.status_code, resp.content[:200])

    # A JSON boolean where a string is expected: `(x or '').lower()` was an
    # AttributeError and a 500 on the client's own accept-a-trainer path.
    resp = api(client).post("/api/auth/client/respond-to-invite/",
                            data=_json.dumps({"request_id": 1, "action": True}),
                            content_type="application/json")
    assert resp.status_code == 400, (resp.status_code, resp.content[:200])

    # `?agent_id=abc` reached an integer column and came back 500.
    resp = api(admin).get("/api/wallet/admin/audit/export/?agent_id=abc")
    assert resp.status_code == 400, (resp.status_code, resp.content[:200])


def test_an_unsupported_period_is_rejected_not_echoed(make_user, api):
    """`?period=zzz` answered 200 with a response key literally named `zzz_volume`:
    the caller chose the shape of our response, and a client asking for a period we do
    not implement was told nothing."""
    admin = make_user("gate_period_admin", user_type="admin")
    resp = api(admin).get("/api/routine/analytics/summary/?period=zzz")
    assert resp.status_code == 400, (resp.status_code, resp.content[:200])
    assert api(admin).get("/api/routine/analytics/summary/?period=week").status_code == 200


def test_an_unresolvable_timezone_is_rejected(make_user, api):
    """Any string was accepted, and the reminder sweep catches ZoneInfoNotFoundError and
    falls back to UTC — so a typo left the user getting reminders in the wrong part of
    their day with nothing anywhere to say why."""
    import json as _json

    user = make_user("gate_tz_user", user_type="client")
    bad = api(user).post("/api/auth/user/update/",
                         data=_json.dumps({"preferred_timezone": "Not/AZone"}),
                         content_type="application/json")
    assert bad.status_code == 400, (bad.status_code, bad.content[:200])
    good = api(user).post("/api/auth/user/update/",
                          data=_json.dumps({"preferred_timezone": "Asia/Damascus"}),
                          content_type="application/json")
    assert good.status_code == 200, good.content[:200]


# --------------------------------------------------------------------------------
# Erasure, both sides of a two-sided row.
# --------------------------------------------------------------------------------

def test_a_clients_erasure_ends_the_trainer_bond(make_user, api):
    """`trainer_relations` was registered on `trainer` alone, so a CLIENT's erasure left
    the relation standing: the trainer kept them on the client list, kept reading their
    profile, and kept creating routines attached to them — writing NEW personal data
    about someone who had asked to be forgotten — and the erased account could not stop
    it, because erasure had deactivated it."""
    from routine.permissions import can_access_user_data
    from users.models import TrainerClientRelation

    trainer = make_user("gate_erase_t", user_type="trainer")
    client = make_user("gate_erase_c", user_type="client")
    TrainerClientRelation.objects.create(trainer=trainer, client=client, status="approved")
    client.assigned_trainer = trainer
    client.save(update_fields=["assigned_trainer"])

    resp = api(client).delete("/api/privacy/erase/?confirm=ERASE")
    assert resp.status_code == 200, resp.content[:300]

    client.refresh_from_db()
    assert client.assigned_trainer_id is None
    assert not TrainerClientRelation.objects.filter(client=client).exists()
    assert not can_access_user_data(trainer, client.pk)
    assert api(trainer).get(
        f"/api/auth/trainer/client-profile/{client.pk}/").status_code == 403


def test_erasure_covers_both_directions_of_a_follow():
    """Registered on `follower` alone, an erasure dropped who you follow and left every
    row saying who follows you."""
    import training_platform.privacy.sources  # noqa: F401  (populates the registry)
    from training_platform.privacy.registry import registry

    by_label = {s.label: s for s in registry.values()}
    assert set(by_label["follows"].user_field) == {"follower", "following"}
    assert set(by_label["trainer_relations"].user_field) == {"trainer", "client"}


# --------------------------------------------------------------------------------
# Query counts that grew with the row count.
# --------------------------------------------------------------------------------

def test_the_trainers_roster_costs_the_same_at_any_size(make_user, api):
    """`client-progress/recent/` serialised each client on its own and each serializer
    asked its own questions: ~9 queries for one client, ~109 for 21, ~259 for 51, up to
    ~509 at the [:100] cap — on a deployment whose connection budget is 15 per machine
    against a 100-connection Postgres."""
    from django.db import connection as _conn
    from django.test.utils import CaptureQueriesContext
    from users.models import TrainerClientRelation

    def roster(n, tag):
        trainer = make_user(f"gate_qg_t_{tag}", user_type="trainer")
        for i in range(n):
            c = make_user(f"gate_qg_c_{tag}_{i}", user_type="client")
            TrainerClientRelation.objects.create(
                trainer=trainer, client=c, status="approved")
        return trainer

    counts = {}
    for n, tag in ((1, "a"), (25, "b")):
        client = api(roster(n, tag))
        client.get("/api/routine/trainer/client-progress/recent/")  # warm
        with CaptureQueriesContext(_conn) as ctx:
            resp = client.get("/api/routine/trainer/client-progress/recent/")
        assert resp.status_code == 200, resp.content[:200]
        assert len(resp.json()["recent_activity"]) == n
        counts[n] = len(ctx)

    assert counts[25] <= counts[1] + 2, counts


def test_the_ledger_does_not_cost_two_queries_per_row(make_user, api):
    """The serializer reads `source_wallet.owner.username`, one hop past the end of
    `select_related("source_wallet", "destination_wallet")` — 50 queries for a 20-row
    page of the financial history screen."""
    from decimal import Decimal

    from django.db import connection as _conn
    from django.test.utils import CaptureQueriesContext

    from wallet.models import Transaction, Wallet

    user = make_user("gate_ledger_u", user_type="client")
    other = make_user("gate_ledger_t", user_type="trainer")
    source, _ = Wallet.objects.get_or_create(owner=user, defaults={"owner_type": "client"})
    dest, _ = Wallet.objects.get_or_create(owner=other, defaults={"owner_type": "trainer"})
    client = api(user)
    client.get("/api/wallet/transactions/")  # warm

    with CaptureQueriesContext(_conn) as empty:
        client.get("/api/wallet/transactions/")

    for i in range(20):
        Transaction.objects.create(
            source_wallet=source, destination_wallet=dest, amount=Decimal("1.00"),
            tx_type="transfer", actor=user, reference_id=f"gate-ledger-{i}")

    with CaptureQueriesContext(_conn) as full:
        resp = client.get("/api/wallet/transactions/")
    assert resp.status_code == 200
    assert len(full) <= len(empty) + 2, (len(empty), len(full))


def test_the_trainer_can_open_a_clients_progress(make_user, api):
    """A `Prefetch` queryset that was sliced and then nested-prefetched. Django applies
    the nested prefetch by filtering the outer queryset, and "Cannot filter a query once
    a slice has been taken" fired on every request, for every trainer, whether or not
    the client had ever trained: the endpoint had never worked for anyone."""
    from users.models import TrainerClientRelation

    trainer = make_user("gate_cp_t", user_type="trainer")
    client = make_user("gate_cp_c", user_type="client")
    TrainerClientRelation.objects.create(trainer=trainer, client=client, status="approved")

    resp = api(trainer).get(f"/api/routine/trainer/client-progress/{client.pk}/")
    assert resp.status_code == 200, resp.content[:300]
    assert resp.json()["client_info"]["id"] == client.pk


# --------------------------------------------------------------------------------
# Boot configuration: the deploy the checklist documents.
# --------------------------------------------------------------------------------

def test_production_secrets_come_from_the_environment_the_deploy_actually_sets():
    """`get_secret` demanded AWS Secrets Manager in production and ignored the
    environment. `DEPLOY_CHECKLIST.md` documents `fly secrets set ...`, `fly.toml`
    provisions no AWS credentials and the Dockerfile sets none, so the documented deploy
    could not boot: settings raised before `migrate` ran, and `celery.py` calls
    `django.setup()` at import so the worker died the same way."""
    import os

    from django.core.exceptions import ImproperlyConfigured

    from training_platform.settings_secrets import get_secret

    name = "GATE_SECRET_PROBE"
    os.environ.pop("AWS_SECRET_NAME", None)
    os.environ[name] = "a-value"
    try:
        assert get_secret(name) == "a-value"
        del os.environ[name]
        # Still fail-closed: a missing name crashes rather than defaulting.
        with pytest.raises(ImproperlyConfigured):
            get_secret(name)
    finally:
        os.environ.pop(name, None)


def test_the_channel_layer_is_built_from_the_same_redis_as_every_cache():
    """It was assembled from REDIS_HOST/REDIS_PORT, which nothing in fly.toml, the
    Dockerfile or the checklist sets, so production resolved to redis://127.0.0.1:6379/5
    — a Redis that does not exist inside the app container. Boot succeeded and the AI
    assistant and social consumers simply could not reach their group layer."""
    from pathlib import Path as _Path

    base = (_Path(__file__).resolve().parent.parent
            / "training_platform" / "settings_base.py").read_text()
    assert 'get_env("REDIS_URL")' in base, "the channel layer ignores REDIS_URL again"

    prod = (_Path(__file__).resolve().parent.parent
            / "training_platform" / "settings_production.py").read_text()
    assert "CHANNEL_LAYERS" in prod and "WebSockets would be dead" in prod, \
        "nothing refuses to boot when the channel layer points at localhost"


def test_the_admin_login_is_rate_limited_and_locks_out():
    """`/admin/` was on the rate-limit skip list, matched with `startswith`, so the
    exemption covered django.contrib.admin's own login form — which counts nothing and
    locks nothing. That left the one login page in the product with no ceiling at all,
    guarding accounts that can reverse transactions and read the audit log."""
    from training_platform.middleware import RateLimitMiddleware

    middleware = RateLimitMiddleware(lambda r: None)

    class _Req:
        def __init__(self, path):
            self.path = path

    assert not middleware._should_skip(_Req("/admin/login/"))
    assert middleware._should_skip(_Req("/static/admin/css/base.css"))


def test_the_admin_login_locks_out_after_five_failures(client):
    from training_platform.cache import ratelimit_cache

    ratelimit_cache().clear()
    body = {"username": "gate_admin_probe", "password": "wrong-password"}
    seen = [client.post("/admin/login/", body).status_code for _ in range(7)]
    assert 429 in seen, seen


# --------------------------------------------------------------------------------
# A refund that cannot fully unwind must SAY so, not crash.
# --------------------------------------------------------------------------------

def test_an_unreversible_refund_leg_is_recorded_rather_than_raising(make_user):
    """`unwind_payment` logged the one case a human has to chase — a client who already
    spent the credit — through a `logger` that was never imported in that module. The
    NameError rolled the whole refund back: the payment stayed completed, the
    subscription stayed active, and the gateway retried into the same crash forever."""
    import wallet.signals as wallet_signals

    assert getattr(wallet_signals, "logger", None) is not None, \
        "wallet/signals.py has no logger; the refund failure branch raises NameError"


# --------------------------------------------------------------------------------
# Cycle 8 criticals. Each of these was reproduced before the fix.
# --------------------------------------------------------------------------------

def test_a_suspended_account_cannot_re_enable_itself(make_user, api):
    """`is_active` meant both "email verified" and "account enabled", and the
    registration OTP flow owned the write — so suspending an account put it into
    exactly the state `resend-otp` exists to service. The suspended user tapped Resend,
    entered the code that arrived in their own inbox, and `verify-otp` set
    `is_active = True` and handed them a token."""
    from users.models import OTPVerification

    user = make_user("gate_ban_probe", user_type="client")
    assert user.email_verified is True or user.is_active  # created enabled

    user.email_verified = True
    user.is_active = False          # a moderator suspends the account
    user.save(update_fields=["email_verified", "is_active"])

    before = OTPVerification.objects.filter(user=user).count()
    resp = api().post("/api/auth/resend-otp/", {"email": user.email},
                      content_type="application/json")
    assert resp.status_code == 200, resp.content[:200]
    assert OTPVerification.objects.filter(user=user).count() == before, \
        "a suspended account was issued a fresh registration OTP"

    user.refresh_from_db()
    assert user.is_active is False


def test_verifying_an_email_twice_does_not_re_enable_a_suspended_account(make_user):
    """The second half of the same defect: even holding a valid code, verification
    proves the address and must not lift a suspension."""
    from users.utils import create_otp
    from users.models import CustomUser

    user = make_user("gate_ban_probe2", user_type="client")
    user.email_verified = False
    user.is_active = False
    user.save(update_fields=["email_verified", "is_active"])
    create_otp(user)                       # a legitimate unverified signup

    user.email_verified = True             # now verified...
    user.is_active = False                 # ...and suspended
    user.save(update_fields=["email_verified", "is_active"])

    fresh = CustomUser.objects.get(pk=user.pk)
    assert fresh.email_verified is True and fresh.is_active is False


def test_a_dead_fcm_token_is_deactivated(make_user):
    """The code matched `registration-token-not-registered` — a Node.js SDK string the
    Python SDK never produces. `firebase_admin` reports `NOT_FOUND`, so the comparison
    could never be true, `_mark_tokens_inactive` was unreachable, and this is the only
    automatic deactivation path in the product: every uninstall left a permanently
    active row that was re-attempted on every push, for ever."""
    from notifications.channels.fcm import FCMChannel

    class _Dead(Exception):
        code = "NOT_FOUND"

    class _Transient(Exception):
        code = "UNAVAILABLE"

    assert FCMChannel._token_is_dead(_Dead()) is True
    assert FCMChannel._token_is_dead(_Transient()) is False
    assert FCMChannel._token_is_dead(None) is False

    try:
        from firebase_admin import messaging
    except Exception:
        return
    unregistered = getattr(messaging, "UnregisteredError", None)
    if unregistered is not None:
        assert FCMChannel._token_is_dead(unregistered("gone")) is True


def test_a_failing_listener_reaches_the_retry_and_the_dead_letter_queue():
    """Three layers swallowed the same failure: the listener logged and returned, the
    dispatcher logged and continued, and `create_and_send` returned None. So
    `process_event_task` always saw a clean return and the retry-with-backoff and the
    dead-letter write below it were unreachable for the exact failure they were written
    for. A Postgres blip deleted the notification and the task reported SUCCESS."""
    import inspect

    from notifications.domain.dispatcher import EventDispatcher, ListenerFailed
    from notifications import services as notification_services

    class _Boom(Exception):
        pass

    class _Event:
        pass

    EventDispatcher._listeners.setdefault(_Event, [])
    calls = []

    def ok(event):
        calls.append("ok")

    def bad(event):
        raise _Boom("database went away")

    EventDispatcher._listeners[_Event] = [ok, bad]
    try:
        with pytest.raises(ListenerFailed):
            EventDispatcher.dispatch(_Event())
    finally:
        EventDispatcher._listeners.pop(_Event, None)
    assert calls == ["ok"], "a failing listener must not stop the others"

    # And the service no longer answers None for a write it could not do.
    source = inspect.getsource(notification_services.NotificationService.create_and_send)
    assert "return None" not in source.split("except Exception")[-1], \
        "create_and_send still swallows its own write failure"


def test_a_repeating_event_notifies_again():
    """The dedup key was `recipient:event_type:related_object_id` behind a PERMANENT
    unique constraint, and every listener passes a stable entity id — a routine id, an
    actor id, a post id. A trainer re-assigning the same routine in June produced no
    push, no unread badge, and the January text still at its January position."""
    from notifications.services import NotificationService

    january = NotificationService._generate_dedup_key(7, "routine_assignment", "42",
                                                      event_id="uuid-jan")
    june = NotificationService._generate_dedup_key(7, "routine_assignment", "42",
                                                   event_id="uuid-jun")
    assert january != june, "two emissions about one routine still collide"

    # With no event_id the entity key is still right: session_reminder passes the local
    # DATE and progress_milestone passes `streak-7`, both once-only by design.
    same_day = NotificationService._generate_dedup_key(7, "session_reminder", "2026-01-05")
    again = NotificationService._generate_dedup_key(7, "session_reminder", "2026-01-05")
    assert same_day == again


def test_the_dedup_lock_is_taken_after_the_write_not_before():
    """`is_duplicate` called `cache.add`, which takes the lock AS its test — so the
    check marked the notification delivered before anything was written, and the retry
    that followed a failed write was suppressed by the lock the failed attempt set.
    The backoff is 5s/10s/20s, all inside the 60s window."""
    from training_platform.cache import ratelimit_cache  # noqa: F401  (cache configured)
    from notifications.utils.deduplication import DeduplicationService

    from django.core.cache import cache

    cache.delete(DeduplicationService.generate_key(1, "probe", "x"))
    assert DeduplicationService.is_duplicate(1, "probe", "x") is False
    # Checking twice must NOT suppress: nothing has been delivered yet.
    assert DeduplicationService.is_duplicate(1, "probe", "x") is False
    DeduplicationService.mark_sent(1, "probe", "x")
    assert DeduplicationService.is_duplicate(1, "probe", "x") is True
    cache.delete(DeduplicationService.generate_key(1, "probe", "x"))


def test_a_websocket_frame_is_bounded():
    """Django's MIDDLEWARE does not run for WebSockets, so the project's only body-size
    cap never saw a frame, and Daphne leaves autobahn's payload limits at 0 = unlimited.
    Measured on real Daphne: one 128 MB frame took the 512 MB web machine from 36 MB to
    710 MB, and SocialConsumer echoes the frame back, doubling the peak."""
    from training_platform.ws_limits import MAX_WS_MESSAGE_BYTES, apply_websocket_limits

    assert apply_websocket_limits() is True
    from daphne.ws_protocol import WebSocketProtocol

    assert WebSocketProtocol.maxMessagePayloadSize == MAX_WS_MESSAGE_BYTES
    assert WebSocketProtocol.maxFramePayloadSize == MAX_WS_MESSAGE_BYTES
    assert 0 < MAX_WS_MESSAGE_BYTES <= 1024 * 1024


def test_a_native_client_may_connect_without_an_origin_but_a_foreign_page_may_not():
    """Channels' own AllowedHostsOriginValidator refuses a handshake with NO Origin
    unless ALLOWED_HOSTS contains `*`, and the Flutter client is not a browser and sends
    none — wrapping the router in it would take the mobile app offline. A handshake that
    DOES carry an Origin must still be checked: browsers attach cookies to a cross-site
    WebSocket handshake and there is no CORS preflight to stop them."""
    from training_platform.ws_limits import SameSiteOriginValidator

    # A native client: no Origin header at all, and none of the other headers is one.
    assert SameSiteOriginValidator.origin_of({"headers": []}) is None
    assert SameSiteOriginValidator.origin_of(
        {"headers": [(b"user-agent", b"Dart/3.3"), (b"sec-websocket-key", b"x")]}) is None
    assert SameSiteOriginValidator.should_validate({"headers": []}) is False

    # An empty Origin is not an Origin.
    assert SameSiteOriginValidator.should_validate(
        {"headers": [(b"origin", b"   ")]}) is False

    # A browser: checked, whoever it is.
    for origin in (b"https://evil.example", b"https://app.example.com", b"null"):
        scope = {"headers": [(b"origin", origin)]}
        assert SameSiteOriginValidator.origin_of(scope) == origin.strip()
        assert SameSiteOriginValidator.should_validate(scope) is True, origin

    # And the wrapper really does hand those to Channels' validator.
    import inspect

    source = inspect.getsource(SameSiteOriginValidator.__call__)
    assert "AllowedHostsOriginValidator" in source


def test_migrations_run_once_per_deploy_not_once_per_machine():
    """`migrate` was part of the web process command, and `auto_start_machines` with
    `min_machines_running = 0` means several machines cold-start at the same moment by
    design. Django takes no cross-process migration lock: reproduced with two concurrent
    `migrate` runs, one exited 0 and the other died on "constraint already exists" —
    and `&&` meant daphne never started on that machine."""
    from pathlib import Path as _Path

    fly = (_Path(__file__).resolve().parent.parent / "fly.toml").read_text()
    web_line = next(line for line in fly.splitlines() if line.strip().startswith("web "))
    assert "migrate" not in web_line, \
        "migrate is back in the web process command, once per machine"
    assert "release_command" in fly and "migrate" in fly


def test_the_two_non_atomic_migrations_can_be_run_twice():
    """`atomic = False` is required for both (Postgres refuses ALTER TABLE in the same
    transaction as the DML above it) and it removes the all-or-nothing guarantee: Django
    records the migration only after the LAST operation, so an interruption left
    committed DDL with no record of it and every retry died on "relation already
    exists" — permanently, because `migrate` could never advance."""
    import inspect
    from importlib import import_module

    for name, marker in (
        ("diet.migrations.0064_category_name_unique", "add_unique_indexes"),
        ("diet.migrations.0055_preferences_one_to_one_and_cuisine",
         "one_preference_per_user"),
    ):
        module = import_module(name)
        source = inspect.getsource(module)
        assert marker in source, f"{name} lost its re-runnable DDL step"
        assert "indisunique" in source, f"{name} no longer checks before creating"
        assert "SeparateDatabaseAndState" in source, \
            f"{name} would double-apply its state operations"


def test_a_free_text_allergy_matches_singular_and_plural(seeded_catalogue):
    """The free-text branch matched with a raw `term in text.lower()`, so English
    morphology decided who was protected: a client who typed "strawberry" was SERVED
    Strawberries and one who typed "tomatoes" was served Tomato — each with the plan
    reporting `is_safe: true`. Substring matching is wrong the other way too: "egg"
    is inside "eggplant"."""
    from diet.allergens import mentions_term

    for term, text in (("strawberry", "Strawberries"), ("strawberries", "Strawberries"),
                       ("banana", "Banana"), ("bananas", "Banana"),
                       ("tomato", "Tomato"), ("tomatoes", "Tomato"),
                       ("grape", "Grapes"), ("grapes", "Grape")):
        assert mentions_term(term, text), f"{term!r} did not match {text!r}"

    for term, text in (("egg", "Eggplant"), ("corn", "Cornish Hen"),
                       ("pea", "Peach"), ("nut", "Nutmeg")):
        assert not mentions_term(term, text), f"{term!r} wrongly matched {text!r}"


def test_a_list_of_allergies_is_recorded_not_stringified(make_user, api, seeded_catalogue):
    """`preferences.allergies = request.data['allergies']` stored the literal text
    "['peanut', 'sesame']" for a client posting a list, which `parse_user_allergies`
    turned into `free:'peanut'` WITH the quote characters — matching no ingredient. Every
    canonical tag was lost, the checker graded on `allergen_source` alone, and the plan
    came back `is_safe: true` while offering the client tahini by name."""
    import json as _json

    from diet.models import UserFoodPreference
    from diet.services.meal_validator import AllergenChecker

    user = make_user("gate_allergy_list", user_type="client")
    _grant_diet_access(user)

    resp = api(user).post("/api/diet/v1/preferences/",
                          data=_json.dumps({"allergies": ["peanut", "sesame"]}),
                          content_type="application/json")
    assert resp.status_code == 200, resp.content[:300]

    stored = UserFoodPreference.objects.get(user=user).allergies
    assert "[" not in stored and "'" not in stored, stored
    checker = AllergenChecker(stored)
    assert "peanuts" in checker.canonical or "peanut" in checker.canonical, checker.user_tags
    assert not any(t.startswith("free:'") for t in checker.user_tags), checker.user_tags


# --------------------------------------------------------------------------------
# Cycle 8: money, social and request-handling.
# --------------------------------------------------------------------------------

def _c8_payment(make_user, tag, currency, amount="5000.00"):
    from decimal import Decimal as _D
    from datetime import timedelta as _td

    from django.utils import timezone as _tz, translation as _tr

    from subscription.models import Payment, Subscription, SubscriptionPlan
    from users.models import TrainerClientRelation

    client = make_user(f"c8pay_{tag}_c", user_type="client")
    trainer = make_user(f"c8pay_{tag}_t", user_type="trainer")
    TrainerClientRelation.objects.create(trainer=trainer, client=client, status="approved")
    with _tr.override("en"):
        plan = SubscriptionPlan.objects.create(
            name=f"c8pay {tag}", price=_D(amount), currency=currency,
            duration_days=30, description="x")
    sub = Subscription.objects.create(user=client, plan=plan, status="pending",
                                      start_date=_tz.now(),
                                      end_date=_tz.now() + _td(days=30))
    pay = Payment.objects.create(subscription=sub, amount=_D(amount), currency=currency,
                                 status="pending", payment_method="manual")
    return client, trainer, pay


def test_a_payment_pays_out_in_the_currency_it_was_charged_in(
        make_user, django_capture_on_commit_callbacks):
    """`Wallet.currency` was a hardcoded `default="USD"` and nothing ever set it from the
    plan, while `move_funds_atomic` only compared currencies when there were TWO wallets
    — so the mint leg of a payment, which passes `source=None`, was never checked at all.
    A 5,000 SYP subscription credited a wallet labelled USD at 1:1 and paid the trainer
    5,000 "USD" for something worth about forty cents. The ledger reconciled perfectly
    the whole way, which is why it survived every internal check: the arithmetic was
    right and the unit was wrong."""
    from decimal import Decimal as _D

    from subscription.services.payment_service import PaymentService
    from wallet.models import Wallet

    from wallet.models import Transaction

    # Read on the MINT leg and the client's own wallet. That leg is what the bug was
    # about — `source=None`, so `move_funds_atomic` compared no currencies at all — and
    # it is per client, where a shared destination wallet (the platform's, since plan
    # step 4) can only ever hold one currency. What the destination does with a second
    # currency is the next test's subject.
    for tag, currency in (("syp", "SYP"), ("usd", "USD")):
        client_user, _trainer, pay = _c8_payment(make_user, tag, currency)
        with django_capture_on_commit_callbacks(execute=True):
            PaymentService.complete_payment(pay.id, {
                "amount": "5000.00", "currency": currency, "status": "completed",
                "transaction_id": f"gate-{tag}"})
        wallet = Wallet.objects.get(owner=client_user)
        assert wallet.currency == currency, (currency, wallet.currency)
        mint = Transaction.objects.get(tx_type="topup",
                                       metadata__original_payment_id=str(pay.id))
        assert mint.currency == currency and mint.amount == _D("5000.00")


def test_a_funded_wallet_refuses_a_payment_in_another_currency(
        make_user, django_capture_on_commit_callbacks):
    """There is no FX rate in this system and there is not meant to be. A wallet that
    already holds money has a currency that is a fact, so a payment in a different one is
    refused rather than moved at 1:1."""
    from decimal import Decimal as _D

    from subscription.models import Payment
    from subscription.services.payment_service import PaymentService
    from wallet.models import Wallet

    from wallet.utils import get_platform_revenue_wallet

    _client, trainer, pay = _c8_payment(make_user, "mix", "SYP")
    with django_capture_on_commit_callbacks(execute=True):
        PaymentService.complete_payment(pay.id, {
            "amount": "5000.00", "currency": "SYP", "status": "completed",
            "transaction_id": "gate-mix-1"})
    # The platform's wallet since plan step 4; the rule under test is the wallet's, not
    # the owner's — a funded wallet's currency is a fact and there is no FX here.
    wallet = get_platform_revenue_wallet()
    wallet.refresh_from_db()
    assert wallet.currency == "SYP" and wallet.balance == _D("5000.00")

    foreign = Payment.objects.create(subscription=pay.subscription, amount=_D("5000.00"),
                                     currency="USD", status="pending",
                                     payment_method="manual")
    foreign.subscription.plan.currency = "USD"
    foreign.subscription.plan.save(update_fields=["currency"])
    with django_capture_on_commit_callbacks(execute=True):
        PaymentService.complete_payment(foreign.id, {
            "amount": "5000.00", "currency": "USD", "status": "completed",
            "transaction_id": "gate-mix-2"})

    wallet.refresh_from_db()
    assert wallet.balance == _D("5000.00") and wallet.currency == "SYP", \
        "a foreign-currency payment moved money into this wallet"


def test_one_renewal_at_a_time_holds_under_concurrency(make_user):
    """`start_renewal` was a `.filter(...).first()` followed by a `.create(...)` with no
    lock, so two real threads both found nothing and both opened a renewal: a
    double-tapped Renew charged twice and granted sixty days, and both charges cleared
    verification because both were legitimate."""
    import inspect

    from subscription.services import payment_service

    source = inspect.getsource(payment_service.PaymentService.start_renewal)
    assert "select_for_update" in source, "start_renewal takes no lock again"
    assert source.index("select_for_update") < source.index("status__in"), \
        "the lock is taken after the check it is meant to protect"


def test_a_refunded_payment_does_not_pay_out(make_user, django_capture_on_commit_callbacks):
    """The payout is deferred to `transaction.on_commit`, so between the COMMIT and the
    receiver running there is a real window in which a refund can land and commit. The
    receiver judged the STALE in-memory payment the lambda captured, so it paid out on an
    already-refunded payment — and `unwind_payment` had found no legs to reverse, because
    they did not exist yet."""
    from decimal import Decimal as _D

    from subscription.services.payment_service import PaymentService
    from wallet.models import Wallet
    from wallet.signals import handle_payment_completed

    _client, trainer, pay = _c8_payment(make_user, "raced", "SYP")
    PaymentService.complete_payment(pay.id, {
        "amount": "5000.00", "currency": "SYP", "status": "completed",
        "transaction_id": "gate-raced"})
    PaymentService.refund_payment(pay.id, reason="chargeback")

    # The deferred payout now runs, holding the object as it was BEFORE the refund.
    pay.status = "completed"
    handle_payment_completed(sender=None, payment=pay, subscription=pay.subscription)

    wallet, _ = Wallet.objects.get_or_create(
        owner=trainer, defaults={"owner_type": "trainer"})
    assert wallet.balance == _D("0.00"), \
        f"a refunded payment still paid out: trainer holds {wallet.balance}"


def test_an_unconfigured_gateway_does_not_take_payments():
    """`PaymentGatewayService(gateway_name)` was constructed directly, skipping the only
    place the enabled check lives — so a deploy missing SHAMCASH_ACCOUNT_ID answered 200
    and told every customer to transfer 5,000 SYP to account "" ."""
    import inspect

    from subscription import views as subscription_views

    for view in (subscription_views.PaymentGatewayView,
                 subscription_views.SubscriptionViewSet):
        source = inspect.getsource(view)
        assert "PaymentGatewayManager.get_service_for_initiation" in source, view.__name__
        code = "\n".join(line.split("#")[0] for line in source.splitlines())
        assert "PaymentGatewayService(gateway_name)" not in code, \
            f"{view.__name__} bypasses the enabled check again"

    # The WEBHOOK deliberately does not check: it is about money that already moved,
    # and refusing a delivery because new payments are switched off would strand it.
    webhook = inspect.getsource(subscription_views.PaymentWebhookView)
    assert "PaymentGatewayService(gateway_name)" in webhook


def test_liking_and_joining_someone_elses_content_is_allowed(make_user, api):
    """`IsOwnerOrReadOnly` treated every non-safe method as an edit, but liking a post,
    liking a comment, joining a challenge and logging progress are POSTs to a detail
    route that create a row BESIDE the object. `get_object()` refused everyone but the
    author, so the like button, Join and Log progress returned 403 for every user on
    every piece of content they did not write."""
    from datetime import timedelta as _td

    from django.utils import timezone as _tz

    from social.models import Challenge, Post, PostLike

    alice = make_user("c8soc_alice", user_type="client")
    bob = make_user("c8soc_bob", user_type="client")

    post = Post.objects.create(author=alice, content="hello", visibility="public")
    resp = api(bob).post(f"/api/social/posts/{post.id}/like/", {},
                         content_type="application/json")
    assert resp.status_code == 200, resp.content[:200]
    assert PostLike.objects.filter(post=post, user=bob).exists()

    challenge = Challenge.objects.create(
        creator=alice, title="C", description="d", status="active",
        start_date=_tz.now() - _td(days=1), end_date=_tz.now() + _td(days=10),
        target_value=100)
    joined = api(bob).post(f"/api/social/challenges/{challenge.id}/join/", {},
                           content_type="application/json")
    assert joined.status_code == 201, joined.content[:200]


def test_hidden_content_is_hidden_and_strangers_cannot_comment_into_it(make_user, api):
    """`Post.can_be_viewed_by` encodes the `is_hidden` rule and has never had a caller;
    the querysets that actually run encode visibility WITHOUT it, so moderation did
    nothing. And the comment read path was scoped by visibility while the WRITE path was
    not, so a stranger could comment into a private post — 201, counter bumped, push
    notification to the author quoting their text — which was also a post-existence
    oracle over the whole id space."""
    import json as _json

    from social.models import Post

    alice = make_user("c8soc_a2", user_type="client")
    mallory = make_user("c8soc_m2", user_type="client")

    hidden = Post.objects.create(author=alice, content="hidden", visibility="public",
                                 is_hidden=True)
    private = Post.objects.create(author=alice, content="diary", visibility="private")

    assert api(mallory).get(f"/api/social/posts/{hidden.id}/").status_code == 404
    body = api(mallory).get("/api/social/posts/").json()
    rows = body.get("results", body)
    assert not any(row["id"] == hidden.id for row in rows), "a hidden post is in the feed"

    for target in (private, hidden):
        resp = api(mallory).post("/api/social/comments/",
                                 data=_json.dumps({"post": target.id, "content": "x"}),
                                 content_type="application/json")
        assert resp.status_code == 404, (target.id, resp.status_code, resp.content[:200])

    # The author still sees their own hidden post: hiding is not a silent deletion.
    assert api(alice).get(f"/api/social/posts/{hidden.id}/").status_code == 200


def test_a_follower_list_does_not_carry_email_addresses(make_user, api):
    """`UserMinimalSerializer` deliberately dropped `email`; these two hand-rolled dicts
    were never updated, so following someone and reading your own list harvested their
    address."""
    from social.models import UserFollow

    alice = make_user("c8soc_a3", user_type="client")
    bob = make_user("c8soc_b3", user_type="client")
    UserFollow.objects.create(follower=bob, following=alice)

    for url, key in (("/api/social/follows/following/", "following"),
                     ("/api/social/follows/followers/", "followers")):
        actor = bob if key == "following" else alice
        rows = api(actor).get(url).json()[key]
        assert rows, url
        assert "email" not in rows[0], (url, sorted(rows[0]))


def test_challenge_progress_is_a_real_amount(make_user, api):
    """`float(current_value)` with no range check accepted -500 (progress percentage
    -500, because `min(100.0, ...)` had no lower bound) and 1e12 (instant completion) —
    and the completion branch only ever SET `completed`, so correcting 100 back to 0 left
    a participant "completed" with zero progress and still eligible for the badge."""
    import json as _json
    from datetime import timedelta as _td

    from django.utils import timezone as _tz

    from social.models import Challenge, ChallengeParticipation

    owner = make_user("c8soc_a4", user_type="client")
    player = make_user("c8soc_b4", user_type="client")
    challenge = Challenge.objects.create(
        creator=owner, title="C4", description="d", status="active",
        start_date=_tz.now() - _td(days=1), end_date=_tz.now() + _td(days=10),
        target_value=100)
    api(player).post(f"/api/social/challenges/{challenge.id}/join/", {},
                     content_type="application/json")

    def log(value):
        return api(player).post(
            f"/api/social/challenges/{challenge.id}/update_progress/",
            data=_json.dumps({"current_value": value}),
            content_type="application/json")

    assert log(-500).status_code == 400
    assert log(10 ** 12).status_code == 400
    assert log(100).status_code == 200
    row = ChallengeParticipation.objects.get(challenge=challenge, user=player)
    assert row.status == "completed"

    assert log(0).status_code == 200
    row.refresh_from_db()
    assert row.status != "completed", "completion never reverted when the number did"
    assert row.progress_percentage >= 0


def test_a_challenge_that_has_ended_cannot_be_joined(make_user, api):
    """`join` checked capacity only while `update_progress` checked `is_active`, so a
    client could join a challenge that ended ten days ago and then be unable to log
    anything against it."""
    from datetime import timedelta as _td

    from django.utils import timezone as _tz

    from social.models import Challenge

    owner = make_user("c8soc_a5", user_type="client")
    player = make_user("c8soc_b5", user_type="client")
    over = Challenge.objects.create(
        creator=owner, title="C5", description="d", status="active",
        start_date=_tz.now() - _td(days=30), end_date=_tz.now() - _td(days=10),
        target_value=100)
    resp = api(player).post(f"/api/social/challenges/{over.id}/join/", {},
                            content_type="application/json")
    assert resp.status_code == 400, resp.content[:200]


def test_a_chunked_body_cannot_skip_the_size_cap():
    """The cap read CONTENT_LENGTH and treated a missing one as 0, which is never over
    the limit — so `Transfer-Encoding: chunked` skipped the only body-size control in the
    stack. Django spools the whole body to FILE_UPLOAD_TEMP_DIR before any middleware
    runs, and in production that is the same 1 GB volume as all user media: a 400 MB
    chunked POST was measured buffering past 368 MB against a 15 MB cap."""
    from django.test import RequestFactory

    from training_platform.middleware import RequestSizeLimitMiddleware

    middleware = RequestSizeLimitMiddleware(lambda request: "reached the view")

    chunked = RequestFactory().post("/api/auth/user/profile-picture/")
    chunked.META.pop("CONTENT_LENGTH", None)
    chunked.META["HTTP_TRANSFER_ENCODING"] = "chunked"
    assert getattr(middleware(chunked), "status_code", None) == 411

    # An ordinary empty POST still passes: no length and no chunked encoding means no
    # body at all, which is not something to refuse.
    empty = RequestFactory().post("/api/social/posts/1/like/")
    empty.META.pop("CONTENT_LENGTH", None)
    assert middleware(empty) == "reached the view"


def test_quarantined_uploads_are_bounded():
    """Every rejected upload was copied at full size into the media volume and kept for
    ever — no TTL, no cap, no purge anywhere — so thirty malformed 2 MB "images" put
    58 MB on the volume in two seconds and roughly 500 rejected requests fill the 1 GB
    disk that also serves all user media."""
    from training_platform.file_security import FileSecurityValidator

    assert FileSecurityValidator.QUARANTINE_MAX_BYTES > 0
    assert FileSecurityValidator.QUARANTINE_MAX_AGE_SECONDS > 0
    assert 0 < FileSecurityValidator.QUARANTINE_SAMPLE_BYTES <= 1024 * 1024
    assert hasattr(FileSecurityValidator, "_evict_quarantine")

    import inspect

    source = inspect.getsource(FileSecurityValidator._quarantine_file)
    assert "sanitize_filename" in source, "the quarantine path is built from a raw name"


def test_both_admin_logins_are_throttled():
    """`/dj-admin/` is a full AdminSite authenticating the SAME staff and superuser
    accounts, and its session is valid on both sites — but the lockout matched `/admin/`
    only, so the throttle added for exactly these accounts was defeated by attacking its
    sibling path."""
    from training_platform.middleware import AdminLoginThrottleMiddleware

    middleware = AdminLoginThrottleMiddleware(lambda request: None)

    class _Req:
        def __init__(self, path):
            self.method = "POST"
            self.path = path

    for path in ("/admin/login/", "/dj-admin/login/"):
        assert middleware._is_admin_login(_Req(path)), path
    assert not middleware._is_admin_login(_Req("/api/auth/login/"))


def test_the_retention_purge_does_not_load_every_expiring_row():
    """`qs.delete()` looks cheap and is not: `analytics.PerformanceMetric.related_activity`
    is SET_NULL on `UserActivity`, so Django cannot fast-delete and materialises the whole
    result set. 200,000 expiring rows cost 181 MB of resident memory against a worker that
    gets 512 MB and runs two children — and ACKS_LATE plus REJECT_ON_WORKER_LOST then
    redeliver the task to the sibling, which dies the same way."""
    import inspect

    # `importlib`, not `import ... as`: `training_platform/privacy/__init__.py` binds the
    # NAME `registry` to the dict inside the module, so attribute lookup on the package
    # returns the dict and shadows the module of the same name.
    import importlib

    registry_module = importlib.import_module("training_platform.privacy.registry")

    assert registry_module.PURGE_BATCH > 0
    source = inspect.getsource(registry_module.purge_expired)
    assert "_delete_in_batches" in source
    code = "\n".join(line.split("#")[0] for line in source.splitlines())
    assert "pk__in=list(" not in code, "the deadline branch materialises ids again"


def test_a_job_too_big_for_its_slot_is_not_retried_three_more_times():
    """`autoretry_for=(Exception,)` also catches `SoftTimeLimitExceeded`, which Celery
    raises INSIDE the task when it overruns. That is not transient: every retry restarts
    the same work and overruns at the same point, so a job too big for its 300 s slot
    burned four consecutive slots on a worker running two children."""
    from celery.exceptions import Ignore

    from training_platform.task_retries import no_retry_on_timeout

    from celery.exceptions import SoftTimeLimitExceeded

    @no_retry_on_timeout
    def overruns():
        raise SoftTimeLimitExceeded()

    with pytest.raises(Ignore):
        overruns()

    @no_retry_on_timeout
    def blips():
        raise OSError("connection reset")

    with pytest.raises(OSError):
        blips()   # a real transient fault still retries


def test_a_gateway_with_no_credentials_at_all_cannot_take_payments():
    """All three ShamCash variables blank used to answer 200 and hand the customer
    transfer instructions naming account `""` — while `GET /v1/gateways/` correctly hid
    the gateway, so the failure was invisible until customers reported the money gone.
    Either credential makes it usable; neither makes it unusable."""
    from subscription.settings import gateway_config

    config = gateway_config.GATEWAY_REGISTRY["shamcash"]["config"][
        gateway_config.GATEWAY_MODE]
    before = dict(config)
    try:
        config["api_key"] = ""
        config["merchant_id"] = ""
        assert gateway_config.can_initiate_payments("shamcash") is False

        # A transfer/reconcile deployment has no API token and is perfectly usable.
        config["merchant_id"] = "ACCOUNT-1"
        assert gateway_config.can_initiate_payments("shamcash") is True

        # And a hosted deployment has the token.
        config["merchant_id"] = ""
        config["api_key"] = "token"
        assert gateway_config.can_initiate_payments("shamcash") is True
    finally:
        config.update(before)

    assert gateway_config.can_initiate_payments("no_such_gateway") is False


def test_a_set_log_belongs_to_one_person(make_user, api):
    """`user_exercise_progress` was validated and `workout_session` beside it was a bare
    primary key over every session in the table. Session ids are sequential, and every
    reader aggregates a session's set logs without re-checking whose progress each log
    belongs to — so a stranger pushed a client's recorded volume from 500 kg to
    100,500 kg and put an exercise they never performed into their activity list."""
    import json as _json

    from django.utils import timezone as _tz

    from routine.models import (Exercise, Routine, RoutineExercise,
                                UserExerciseProgress, WorkoutSession)
    from users.models import TrainerClientRelation

    trainer = make_user("c8set_t", user_type="trainer")
    owner = make_user("c8set_owner", user_type="client")
    stranger = make_user("c8set_stranger", user_type="client")
    TrainerClientRelation.objects.create(trainer=trainer, client=owner, status="approved")

    routine = Routine.objects.create(name="c8set", days=1, created_by=trainer,
                                     start_date=_tz.localdate(), is_active=True)
    exercise = Exercise.objects.create(name="c8set ex", created_by=trainer)
    RoutineExercise.objects.create(routine=routine, exercise=exercise, day=1,
                                   sets=3, reps=10)
    routine.assigned_to.add(owner)

    session = WorkoutSession.objects.create(user=owner, routine=routine, day=1,
                                            status="active")
    theirs = UserExerciseProgress.objects.create(
        user=stranger, exercise=exercise, date=_tz.localdate(), target_sets=3)

    resp = api(stranger).post("/api/routine/set-logs/", data=_json.dumps({
        "user_exercise_progress": theirs.id, "workout_session": session.id,
        "set_number": 1, "weight": 1000, "reps": 100,
        "date": _tz.localdate().isoformat()}), content_type="application/json")
    assert resp.status_code == 400, resp.content[:300]


def test_logging_sets_moves_the_day_it_was_logged_for(
        make_user, api, django_capture_on_commit_callbacks):
    """Two defects in one path. A race fix replaced a model save with a queryset
    `.update()`, which emits no `post_save`, so the receiver that writes
    `RoutineProgress` never ran: a client logged every set and the day stayed
    "not started", the streak stayed 0 and the trainer's screen read 0%. And
    `UserExerciseProgress` records neither routine nor day, so the propagation walked
    every routine containing the exercise and marked every day of each — one full-body
    workout marked the whole week done."""
    import json as _json

    from django.utils import timezone as _tz

    from routine.models import (Exercise, Routine, RoutineExercise, RoutineProgress,
                                UserExerciseProgress, WorkoutSession)
    from users.models import TrainerClientRelation

    trainer = make_user("c8prog_t", user_type="trainer")
    client_user = make_user("c8prog_c", user_type="client")
    TrainerClientRelation.objects.create(trainer=trainer, client=client_user,
                                         status="approved")

    exercise = Exercise.objects.create(name="c8prog ex", created_by=trainer)
    trained = Routine.objects.create(name="c8prog trained", days=3, created_by=trainer,
                                     start_date=_tz.localdate(), is_active=True)
    for day in (1, 2, 3):
        RoutineExercise.objects.create(routine=trained, exercise=exercise, day=day,
                                       sets=3, reps=10)
    trained.assigned_to.add(client_user)

    # A second routine holding the same exercise, which the client never opens.
    untouched = Routine.objects.create(name="c8prog other", days=1, created_by=trainer,
                                       start_date=_tz.localdate(), is_active=True)
    RoutineExercise.objects.create(routine=untouched, exercise=exercise, day=1,
                                   sets=3, reps=10)
    untouched.assigned_to.add(client_user)

    session = WorkoutSession.objects.create(user=client_user, routine=trained, day=1,
                                            status="active")
    progress = UserExerciseProgress.objects.create(
        user=client_user, exercise=exercise, date=_tz.localdate(), target_sets=3)

    for number in (1, 2, 3):
        with django_capture_on_commit_callbacks(execute=True):
            resp = api(client_user).post("/api/routine/set-logs/", data=_json.dumps({
                "user_exercise_progress": progress.id, "workout_session": session.id,
                "set_number": number, "weight": 50, "reps": 10,
                "date": _tz.localdate().isoformat()}),
                content_type="application/json")
        assert resp.status_code in (200, 201), resp.content[:200]

    progress.refresh_from_db()
    assert progress.completed_sets == 3, "the set logs never reached the derived totals"

    verdicts = {
        (row["routine__name"], row["day"]): row["status"]
        for row in RoutineProgress.objects.filter(user=client_user)
        .values("routine__name", "day", "status")
    }
    assert verdicts[("c8prog trained", 1)] == "completed", verdicts
    assert verdicts[("c8prog trained", 2)] != "completed", verdicts
    assert verdicts[("c8prog trained", 3)] != "completed", verdicts
    assert verdicts[("c8prog other", 1)] != "completed", verdicts


def test_a_client_cannot_author_their_own_compliance(make_user, api):
    """`status`, `exercises_completed` and `total_exercises` were writable and
    `perform_create` checked only that the routine was assigned to the caller — never
    that the numbers corresponded to work done. A PATCH put 4950% on the trainer's
    screen, and two HTTP calls with no set logs behind them manufactured a streak."""
    import json as _json

    from django.utils import timezone as _tz

    from routine.models import (Exercise, Routine, RoutineExercise, RoutineProgress)
    from users.models import TrainerClientRelation

    trainer = make_user("c8comp_t", user_type="trainer")
    client_user = make_user("c8comp_c", user_type="client")
    TrainerClientRelation.objects.create(trainer=trainer, client=client_user,
                                         status="approved")
    routine = Routine.objects.create(name="c8comp", days=1, created_by=trainer,
                                     start_date=_tz.localdate(), is_active=True)
    RoutineExercise.objects.create(
        routine=routine, exercise=Exercise.objects.create(name="c8comp ex",
                                                          created_by=trainer),
        day=1, sets=3, reps=10)
    routine.assigned_to.add(client_user)

    row = RoutineProgress.objects.filter(user=client_user, routine=routine).first()
    assert row is not None
    api(client_user).patch(
        f"/api/routine/routine-progress/{row.id}/",
        data=_json.dumps({"status": "completed", "exercises_completed": 99,
                          "total_exercises": 2}),
        content_type="application/json")

    row.refresh_from_db()
    assert row.exercises_completed == 0, row.exercises_completed
    assert row.status != "completed", row.status

    body = api(client_user).get(f"/api/routine/routine-progress/{row.id}/").json()
    assert 0 <= body["completion_percentage"] <= 100, body["completion_percentage"]


def test_a_workout_cannot_end_before_it_started_or_last_a_year(make_user, api):
    """`start_time` is `auto_now_add` so only one side of the interval was trusted, and
    nothing compared them: an end three days BEFORE the start reported -259,199 seconds
    and one 400 days after gave 34,560,000. The activity chart sums these."""
    import json as _json
    from datetime import timedelta as _td

    from django.utils import timezone as _tz

    from routine.models import Routine, WorkoutSession
    from users.models import TrainerClientRelation

    trainer = make_user("c8dur_t", user_type="trainer")
    client_user = make_user("c8dur_c", user_type="client")
    TrainerClientRelation.objects.create(trainer=trainer, client=client_user,
                                         status="approved")
    routine = Routine.objects.create(name="c8dur", days=1, created_by=trainer,
                                     start_date=_tz.localdate(), is_active=True)
    routine.assigned_to.add(client_user)
    session = WorkoutSession.objects.create(user=client_user, routine=routine,
                                            status="active")

    def finish(when):
        return api(client_user).patch(
            f"/api/routine/workout-sessions/{session.id}/",
            data=_json.dumps({"end_time": when.isoformat()}),
            content_type="application/json").status_code

    assert finish(_tz.now() - _td(days=3)) == 400
    assert finish(_tz.now() + _td(days=400)) == 400
    assert finish(_tz.now() + _td(minutes=40)) == 200


def test_a_progress_status_must_be_one_we_declared(make_user, api):
    """`choices` is a form-layer rule and `update_or_create` never calls `full_clean`,
    so 'banana' was stored and returned with a 200 — a row that is neither complete nor
    incomplete, invisible to every `filter(status=...)` in analytics."""
    import json as _json

    from django.utils import timezone as _tz

    from routine.models import Exercise, Routine, RoutineExercise
    from users.models import TrainerClientRelation

    trainer = make_user("c8st_t", user_type="trainer")
    client_user = make_user("c8st_c", user_type="client")
    TrainerClientRelation.objects.create(trainer=trainer, client=client_user,
                                         status="approved")
    routine = Routine.objects.create(name="c8st", days=1, created_by=trainer,
                                     start_date=_tz.localdate(), is_active=True)
    RoutineExercise.objects.create(
        routine=routine, exercise=Exercise.objects.create(name="c8st ex",
                                                          created_by=trainer),
        day=1, sets=3, reps=10)
    routine.assigned_to.add(client_user)

    def set_status(value):
        # `confirm_unlogged`: since the owner's decision of 2026-09-19 (BACKEND_BUGS
        # B-011) completing a day with unlogged sets is allowed only after the person
        # was warned; this test is about which statuses are accepted, not about that.
        return api(client_user).post(
            f"/api/routine/routines/{routine.id}/update_progress/",
            data=_json.dumps({"day": 1, "status": value, "confirm_unlogged": True}),
            content_type="application/json").status_code

    assert set_status("banana") == 400
    assert set_status("completed") == 200


def test_an_unverified_trainer_does_not_get_the_paid_product(make_user, api):
    """`user_type='trainer'` is self-service at registration — only `admin` and `agent`
    are blocked — and both subscription permissions short-circuited on `is_trainer`
    alone, a role check standing in for an entitlement check. So anyone who picked
    "trainer" at signup got the whole paid product free, permanently, with no admin ever
    approving them, while the people who told the truth about who they are paid for it.
    `trainer_is_verified` already existed and gated nothing anywhere."""
    trainer = make_user("gate_unverified_trainer", user_type="trainer")
    assert trainer.trainer_is_verified is False

    assert api(trainer).get("/api/diet/v1/food/list/").status_code == 403

    trainer.trainer_is_verified = True
    trainer.save(update_fields=["trainer_is_verified"])
    assert api(trainer).get("/api/diet/v1/food/list/").status_code == 200


def test_an_unanswered_invitation_does_not_disclose_the_client(make_user, api):
    """Trainer-side invitations are unlimited and can name any client id, and this view
    rendered the full record for every pending row. Register as a trainer, loop
    `client_id` 1..N, read the list once: the name, email address, age, gender and
    fitness goals of the entire client base. The consent boundary was enforced at
    APPROVAL and not at disclosure."""
    from users.models import TrainerClientRelation

    trainer = make_user("gate_inv_t", user_type="trainer")
    invited = make_user("gate_inv_invited", user_type="client", age=32, gender="Female")
    asker = make_user("gate_inv_asker", user_type="client", age=28, gender="Male")

    TrainerClientRelation.objects.create(trainer=trainer, client=invited,
                                         status="pending", initiated_by="trainer")
    TrainerClientRelation.objects.create(trainer=trainer, client=asker,
                                         status="pending", initiated_by="client")

    rows = {row["client_id"]: row for row in
            api(trainer).get("/api/auth/trainer/pending-requests/")
            .json()["pending_requests"]}

    uninvited = rows[invited.pk]
    for leaked in ("client_email", "client_age", "client_gender", "client_goals"):
        assert leaked not in uninvited, (leaked, sorted(uninvited))

    # Someone who ASKED has chosen to be seen.
    assert "client_email" in rows[asker.pk]


def test_a_trainer_cannot_hold_unlimited_invitations(make_user, api):
    """`unique_pending_trainer_request_per_client` is scoped to client-initiated rows,
    so trainer-initiated invitations were unbounded — which is what made the
    pending-requests list reachable as a directory."""
    import json as _json

    from users.models import TrainerClientRelation

    trainer = make_user("gate_invcap_t", user_type="trainer")
    for index in range(25):
        client_user = make_user(f"gate_invcap_c{index}", user_type="client")
        TrainerClientRelation.objects.create(
            trainer=trainer, client=client_user, status="pending",
            initiated_by="trainer")

    one_more = make_user("gate_invcap_extra", user_type="client")
    resp = api(trainer).post("/api/auth/trainer/assign-client/",
                             data=_json.dumps({"client_id": one_more.pk}),
                             content_type="application/json")
    assert resp.status_code == 429, resp.content[:200]


def test_a_deactivated_account_is_not_served_from_cache(make_user, api):
    """`CacheMiddleware.process_request` returns a JsonResponse from the middleware
    chain, which runs BEFORE DRF authentication, and `AccessToken(...)` verifies a
    signature and an expiry and nothing else. A banned account kept reading its
    exercises, templates and achievements for the full TTL while every uncached route
    correctly answered 401."""
    from training_platform.cache import private_cache

    user = make_user("gate_cache_ban", user_type="client")
    client = api(user)

    cacheable = "/api/routine/exercises/"
    assert client.get(cacheable).status_code == 200      # warm it

    user.is_active = False
    user.save(update_fields=["is_active"])
    private_cache().delete(f"acct_usable:{user.pk}")     # the 60s memo, not the entry

    assert client.get(cacheable).status_code == 401, \
        "a deactivated account was served the cached body"


def test_the_weekly_screen_sees_a_plan_that_covers_only_midweek(
        make_user, api, seeded_catalogue):
    """`_get_active_diet_plan(start) or _get_active_diet_plan(end)` requires a plan to
    cover the Monday or the Sunday of the week, so a plan covering only mid-week
    satisfied neither probe and the whole week came back blank — which is every 1-day and
    7-day plan that does not start on a Monday, and `duration_days` defaults to 1. The
    client's own progress screen showed today's meals while this one showed seven empty
    days."""
    import datetime as _dt

    from django.utils import timezone as _tz

    from diet.services.plan_generation import generate_and_save

    user = make_user("gate_week_client", user_type="client", age=30, gender="male",
                     height=175, weight=75, activity_level="Moderate")
    _grant_diet_access(user)

    today = _tz.localdate()
    midweek = today - _dt.timedelta(days=today.weekday()) + _dt.timedelta(days=3)
    _out, plan = generate_and_save(user, duration_days=1, seed_salt="gate-week",
                                   start_date=midweek)
    meal = plan.meals.first()
    for component in meal.components.all():
        component.complete()

    body = api(user).get("/api/diet/v1/client/progress/weekly/").json()
    rows = body["results"] if isinstance(body, dict) else body
    with_a_plan = [row for row in rows if row["has_active_plan"]]
    assert with_a_plan, "a mid-week plan renders as seven empty days"
    assert any(row["meals_completed"] > 0 for row in with_a_plan), with_a_plan


def test_the_plan_screen_does_not_report_planned_food_as_eaten(
        make_user, api, seeded_catalogue):
    """`calculate_daily_nutrition` sums every meal of the day with no reference to
    `is_completed`, and the view emitted that as `calories_consumed` — so one response
    told a client who had eaten a quarter of their food that they were at 97.3% of
    target, while `/client/progress/` said 30%. On a cut that is the difference between
    eating again and not."""
    from diet.services.plan_generation import generate_and_save

    user = make_user("gate_nut_client", user_type="client", age=30, gender="male",
                     height=175, weight=75, activity_level="Moderate")
    _grant_diet_access(user)
    _out, plan = generate_and_save(user, duration_days=1, seed_salt="gate-nut")

    meal = plan.meals.first()
    for component in meal.components.all():
        component.complete()

    when = plan.start_date.isoformat()
    summary = api(user).get(
        f"/api/diet/v1/nutrition/plan/{plan.id}/?date={when}").json()["summary"]
    progress = api(user).get(f"/api/diet/v1/client/progress/?date={when}").json()

    assert summary["completed_meals"] < summary["total_meals"], summary
    assert summary["calories_consumed"] < summary["calories_planned"], summary
    # The two screens agree, which is the whole point.
    assert abs(summary["calories_consumed"] - progress["calories_consumed"]) < 0.5, \
        (summary["calories_consumed"], progress["calories_consumed"])


def test_a_meal_on_a_superseded_plan_cannot_be_completed(make_user, api, seeded_catalogue):
    """`MealCompletionView` was scoped by owner only, never by `is_active`, while
    `ClientProgressService` resolves progress by `is_active=True`. So completing a meal
    on a retired plan answered 200 with a completion percentage, wrote a `DailyProgress`
    row keyed to the retired plan, and no read endpoint ever consulted it: the client
    tapped "I ate this", was told it worked, and the number never moved."""
    from diet.services.plan_generation import generate_and_save

    user = make_user("gate_sup_client", user_type="client", age=30, gender="male",
                     height=175, weight=75, activity_level="Moderate")
    _grant_diet_access(user)
    _out, first = generate_and_save(user, duration_days=1, seed_salt="gate-sup-1")
    stale_meal = first.meals.first()

    generate_and_save(user, duration_days=1, seed_salt="gate-sup-2",
                      start_date=first.start_date)
    first.refresh_from_db()
    assert first.is_active is False

    resp = api(user).post(f"/api/diet/v1/client/meals/{stale_meal.id}/complete/", {},
                          content_type="application/json")
    assert resp.status_code == 409, resp.content[:200]
    assert resp.json().get("code") == "plan_superseded"


def test_a_client_who_trains_every_day_reads_as_complete(make_user, api):
    """Two keying conventions collide in `RoutineProgress`. The assignment receiver
    scaffolds one row per day all stamped with `routine.start_date`; the completion path
    writes at the ACTUAL training date; and `(user, routine, day, date)` is unique — so
    every scaffold except the one whose date happened to coincide is stranded as
    `not_started` for ever. Counting ROWS gave a client who trained perfectly on all 7
    days of a 7-day routine 7 of 13: 53.85%, permanently, with no amount of training able
    to reach 100. The trainer's roster showed the same figure."""
    import datetime as _dt

    from django.utils import timezone as _tz

    from routine.models import Exercise, Routine, RoutineExercise, RoutineProgress
    from users.models import TrainerClientRelation

    trainer = make_user("gate_perfect_t", user_type="trainer")
    client_user = make_user("gate_perfect_c", user_type="client")
    TrainerClientRelation.objects.create(trainer=trainer, client=client_user,
                                         status="approved")

    today = _tz.localdate()
    routine = Routine.objects.create(name="gate perfect", days=7, created_by=trainer,
                                     start_date=today - _dt.timedelta(days=7),
                                     is_active=True)
    for day in range(1, 8):
        RoutineExercise.objects.create(
            routine=routine,
            exercise=Exercise.objects.create(name=f"gate perfect ex{day}",
                                             created_by=trainer),
            day=day, sets=3, reps=10)
    routine.assigned_to.add(client_user)

    for offset, day in enumerate(range(1, 8)):
        RoutineProgress.objects.update_or_create(
            user=client_user, routine=routine, day=day,
            date=today - _dt.timedelta(days=6 - offset),
            defaults={"status": "completed", "exercises_completed": 1,
                      "total_exercises": 1})

    # The stranded scaffolds are still there; the figure must not care.
    assert RoutineProgress.objects.filter(user=client_user, routine=routine).count() > 7

    results = api(trainer).get(
        f"/api/routine/analytics/completion/?user_id={client_user.pk}").json()["results"]
    assert results, results
    row = next(r for r in results if r["routine_id"] == routine.pk)
    assert row["completed"] == 7, row
    assert row["completion_rate"] == 100.0, row


def test_every_daily_job_survives_a_worker_restart():
    """A plain-interval beat entry's clock starts at worker startup — a fresh
    `ScheduleEntry` sets `last_run_at = now`, so the first fire is a full interval later
    — and `celery -A ... worker --beat` keeps its schedule file on the container
    filesystem, which `[mounts]` scopes to the web process only. On a free-tier machine a
    deploy, an OOM or a replacement resets that clock, so a DAILY job never ran if the
    worker restarted more often than daily. Two of these are the GDPR retention purges,
    whose whole point is a legal deadline, and the beat log looked healthy throughout."""
    from celery.schedules import crontab

    from django.conf import settings

    interval_entries = {
        name: entry["schedule"]
        for name, entry in settings.CELERY_BEAT_SCHEDULE.items()
        if not isinstance(entry["schedule"], crontab)
    }
    # A short interval is fine: restarting costs at most that interval, not a whole day.
    for name, schedule in interval_entries.items():
        seconds = getattr(schedule, "total_seconds", lambda: float(schedule))()
        assert seconds <= 900, (name, seconds)


def test_expired_jwt_rows_are_flushed_on_a_schedule():
    """`ROTATE_REFRESH_TOKENS` + `BLACKLIST_AFTER_ROTATION` write two permanent rows per
    refresh, and simplejwt's `flushexpiredtokens` appeared in no schedule, no Dockerfile
    and no checklist. The row holds the whole RS256 token in a text column at ~869 bytes:
    one client rotating hourly is 25 rows a day, so 10,000 daily users is 250,000 rows
    and 217 MB of heap per day, 91 million rows a year, on a 1 GB volume."""
    from django.conf import settings

    from training_platform.celery import app

    app.loader.import_default_modules()

    entry = settings.CELERY_BEAT_SCHEDULE.get("flush-expired-jwt-tokens")
    assert entry is not None, "nothing flushes the JWT blacklist"
    assert entry["task"] in app.tasks, entry["task"]


def test_every_beat_entry_resolves_to_a_registered_task():
    """A schedule naming a task nobody registered is a job that silently never runs."""
    from django.conf import settings

    from training_platform.celery import app

    app.loader.import_default_modules()
    unresolvable = [entry["task"] for entry in settings.CELERY_BEAT_SCHEDULE.values()
                    if entry["task"] not in app.tasks]
    assert not unresolvable, unresolvable


def test_a_large_adult_is_offered_a_structure_that_works(seeded_catalogue, make_user):
    """`MAX_SNACKS` was 1, so the largest structure the planner would accept topped out
    at 3,325 kcal on `maintain` — while `CustomUser.MAX_DAILY_CALORIES` is 6,000 and
    `calculate_daily_calories` legitimately returns more for a large or very active
    adult. Four of ten plausible adult profiles were refused outright, and the suggestion
    handed back was 3 meals and 1 snack: the default, and the structure that had just
    been refused. There was no input that produced a plan for them."""
    from diet.services.plan_generation import (
        InfeasibleCaloriesError, MAX_MAIN_MEALS, MAX_SNACKS, generate_and_save)
    from diet.services.rule_based_planner import feasible_kcal_ceiling

    assert MAX_SNACKS > 1, "the structure cap is back to one snack"
    assert feasible_kcal_ceiling(MAX_MAIN_MEALS, MAX_SNACKS) > \
        feasible_kcal_ceiling(MAX_MAIN_MEALS, 1)

    # A suggestion must be a structure that would actually succeed...
    refused = InfeasibleCaloriesError(3400.0, feasible_kcal_ceiling(3, 1), 3, 1)
    suggestion = refused.suggestion
    assert feasible_kcal_ceiling(suggestion["meal_count"],
                                 suggestion["snack_count"]) >= 3400.0, suggestion

    # ...or an honest admission that none does, with the ceiling and the shortfall.
    impossible = InfeasibleCaloriesError(9000.0, feasible_kcal_ceiling(3, 1), 3, 1)
    hopeless = impossible.suggestion
    assert hopeless["short_by"] > 0 and hopeless["max_daily_calories"] > 0, hopeless

    # And a real large adult gets a plan.
    user = make_user("gate_big_adult", user_type="client", gender="male", height=185,
                     weight=90, age=25, activity_level="VeryActive")
    planned = False
    for snacks in range(1, MAX_SNACKS + 1):
        try:
            generate_and_save(user, duration_days=1, snack_count=snacks,
                              seed_salt=f"gate-big-{snacks}")
            planned = True
            break
        except InfeasibleCaloriesError:
            continue
    assert planned, "no structure produced a plan for a 90 kg very active adult"


def test_an_over_cap_dish_is_not_offered_at_all(seeded_catalogue):
    """`find_recipe` filtered the fitting dishes to those under `WEEK_MAX_SERVINGS` and
    then wrote `if fresh: within = fresh` — so when EVERY fitting dish was already at the
    cap, which is exactly the situation the cap exists for, the filter was discarded and
    an over-cap dish was returned anyway.

    The cap is still ADVISORY overall, deliberately: `_plan_meal` ranks "is inside
    tolerance" above the serving count, so an over-cap dish can still win when the
    template plate does not fit — see
    `test_a_fitting_dish_at_the_weekly_cap_beats_a_plate_that_misses`, and the open
    decision recorded in tests/diet_quality_baseline.json. What this pins is that
    `find_recipe` itself no longer hands one back."""
    import inspect

    from diet.planner import recipes

    source = inspect.getsource(recipes.find_recipe)
    code = "\n".join(line.split("#")[0] for line in source.splitlines())
    assert "if fresh:" not in code, "find_recipe discards the weekly cap again"
    assert "within = fresh" in code


def test_the_deploy_gate_notices_a_catalogue_the_planner_cannot_use(seeded_catalogue):
    """Every check in `check_catalogue` was a COUNT over `needs_review=False`, and none
    asked the only question that matters: can the planner still build from this. The
    admin's own bulk action "Send back: stop the planner serving these" on the Proteins
    category left 90 servable foods, a full recipe library, ten green ticks and exit 0 —
    while the planner logged "Candidate pool has empty slots" for every meal and
    delivered a protein-free day 43% under target, with a 200 OK. This command is what
    the deploy gates on."""
    import contextlib
    import io

    from django.core.management import call_command
    from django.core.management.base import CommandError

    from diet.models import FoodItem
    from diet.planner.candidates import classify_food

    call_command("check_catalogue", verbosity=0)      # the healthy catalogue passes

    proteins = [food.id for food in FoodItem.objects.filter(needs_review=False)
                if classify_food(food) == "protein"]
    assert proteins, "the seeded catalogue has no protein to remove"
    FoodItem.objects.filter(id__in=proteins).update(needs_review=True)

    printed = io.StringIO()
    try:
        with contextlib.redirect_stdout(printed):
            call_command("check_catalogue", verbosity=1)
    except (CommandError, SystemExit):
        pass
    else:
        raise AssertionError("a catalogue with no servable protein passed the gate")
    finally:
        FoodItem.objects.filter(id__in=proteins).update(needs_review=False)

    assert "no servable food classifies as" in printed.getvalue(), printed.getvalue()


def test_a_wipe_counts_everything_it_would_destroy(seeded_catalogue, make_user):
    """The guard counted `MealComponent` alone — the one FK that had been changed to
    PROTECT — so on any database whose meal history had been trimmed, `--wipe` needed no
    `--force`, printed nothing about it, and cascade-deleted the planner's LEARNED
    per-client weights, the category preferences and both sides of the liked/disliked
    M2M: the entire signal the personalisation is built on."""
    from django.core.management import call_command
    from django.core.management.base import CommandError

    from diet.models import FoodItem, UserFoodWeight

    user = make_user("gate_wipe_owner", user_type="client")
    food = FoodItem.objects.filter(needs_review=False).first()
    UserFoodWeight.objects.create(user=user, food=food, weight=1.5)

    try:
        with pytest.raises(CommandError) as refusal:
            call_command("load_food_catalogue", "--apply", "--wipe", verbosity=0)
        assert "learned food weight" in str(refusal.value), str(refusal.value)
    finally:
        UserFoodWeight.objects.filter(user=user).delete()


def test_a_social_write_does_not_scan_the_whole_badge_catalogue(make_user, api):
    """`check_and_award` looped every matching definition, ran `.exists()` per definition
    to see if it was already earned, computed the metric in `_evaluate_criteria` and then
    computed the SAME metric again in `_record_progress`, which wrote with
    `update_or_create` — a SELECT plus a write each. And `post_liked` was not in
    `ACTIVITY_TO_CATEGORIES`, so a like scanned the catalogue unfiltered. Measured at 50
    definitions: a like cost 352 queries and 279 ms, a follow 817 and 704 ms. Growing the
    badge catalogue degraded every post, like and follow linearly."""
    import json as _json

    from django.db import connection as _conn
    from django.test.utils import CaptureQueriesContext

    from achievements.models import Achievement
    from social.models import Post

    for index in range(40):
        Achievement.objects.get_or_create(
            key=f"gate_scan_{index}",
            defaults=dict(name=f"Gate scan {index}", description="probe",
                          category="social",
                          criteria={"type": "posts_created", "target": 500 + index},
                          points=10, is_active=True))
    definitions = Achievement.objects.filter(is_active=True).count()
    assert definitions >= 40, definitions

    author = make_user("gate_scan_author", user_type="client")
    liker = make_user("gate_scan_liker", user_type="client")
    post = Post.objects.create(author=author, content="x", visibility="public")

    like_url = f"/api/social/posts/{post.id}/like/"
    api(liker).post(like_url, {}, content_type="application/json")   # warm
    api(liker).post(like_url, {}, content_type="application/json")   # unlike
    with CaptureQueriesContext(_conn) as liking:
        assert api(liker).post(like_url, {},
                               content_type="application/json").status_code == 200

    with CaptureQueriesContext(_conn) as posting:
        assert api(author).post(
            "/api/social/posts/",
            data=_json.dumps({"content": "hello", "visibility": "public"}),
            content_type="application/json").status_code == 201

    # Well under one query per definition, rather than the ~7 it used to cost.
    assert len(liking) < definitions, (len(liking), definitions)
    assert len(posting) < definitions, (len(posting), definitions)


def test_one_hole_in_the_feed_does_not_end_it(make_user, api):
    """`has_more` was `len(ordered_posts) == limit` — the count AFTER the SQL visibility
    filter — and nothing ever ZREMs a post id, so deleted, erased and now-invisible posts
    sit in every follower's sorted set for the key's 24-hour life. One hole ended the
    feed: the author deleted a single post, the page came back four rows instead of five,
    `has_more` went False, and everything behind it was unreachable."""
    import inspect

    from social import views as social_views

    source = inspect.getsource(social_views.PostViewSet.feed)
    assert "len(post_ids) == limit" in source, \
        "has_more counts survivors again, so one hole ends the feed"

    # And a deleted post is evicted rather than left as a hole.
    assert hasattr(social_views.PostViewSet, "perform_destroy")
    from social.feed_cache import remove_post_from_feeds  # noqa: F401


def test_refunding_one_payment_does_not_destroy_the_periods_still_paid_for(
        make_user, django_capture_on_commit_callbacks):
    """The `else` branch set `end_date = now` unconditionally, with no reference to what
    else had been paid — so refunding a customer's FIRST month destroyed the two months
    they had paid for and kept: 15,000 SYP over three payments, one 5,000 refunded, the
    subscription cancelled outright, the trainer holding 10,000 and the client nothing."""
    from decimal import Decimal as _D

    from django.utils import timezone as _tz, translation as _tr

    from subscription.models import Payment, Subscription, SubscriptionPlan
    from subscription.services.payment_service import PaymentService

    user = make_user("gate_refund_scope", user_type="client")
    with _tr.override("en"):
        plan = SubscriptionPlan.objects.create(
            name="gate refund scope", price=_D("5000.00"), currency="SYP",
            duration_days=30, description="x")
    subscription = Subscription.objects.create(
        user=user, plan=plan, status="pending",
        start_date=_tz.now(), end_date=_tz.now())

    payments = []
    for index in range(3):
        payment = Payment.objects.create(
            subscription=subscription, amount=_D("5000.00"), currency="SYP",
            status="pending", payment_method="manual",
            metadata={"kind": "renewal"} if index else {})
        with django_capture_on_commit_callbacks(execute=True):
            PaymentService.complete_payment(payment.id, {
                "amount": "5000.00", "currency": "SYP", "status": "completed",
                "transaction_id": f"gate-refund-{index}"})
        payments.append(payment)

    subscription.refresh_from_db()
    paid_until = subscription.end_date

    with django_capture_on_commit_callbacks(execute=True):
        PaymentService.refund_payment(payments[0].id, reason="chargeback")

    subscription.refresh_from_db()
    assert subscription.status != "cancelled", subscription.status
    assert subscription.end_date > _tz.now(), subscription.end_date
    # Exactly one period came off, not all of them.
    assert (paid_until - subscription.end_date).days == plan.duration_days, (
        paid_until, subscription.end_date)


def test_an_abandoned_checkout_is_cancelled_not_refunded():
    """`cancelled`/`canceled`/`voided` were in REFUND_STATUSES, and `refund_payment` is
    only legal from `completed` — so the most common gateway event of all, a customer
    opening the payment page and walking away, produced a 400 and an endless redelivery
    loop against a row that could never leave `pending`. `PAYMENT_STATUS_TRANSITIONS`
    has declared `pending -> cancelled` all along and no service method implemented it."""
    from subscription import views as subscription_views
    from subscription.services.payment_service import PaymentService

    assert hasattr(PaymentService, "cancel_payment")
    for word in ("cancelled", "canceled", "voided"):
        assert word in subscription_views.CANCELLATION_STATUSES, word
        assert word not in subscription_views.REFUND_STATUSES, word
    # A real refund is still a refund.
    assert "refunded" in subscription_views.REFUND_STATUSES
    assert "chargeback" in subscription_views.REFUND_STATUSES


def test_a_push_that_failed_to_send_is_recorded_and_retried(make_user, monkeypatch):
    """`_dispatch_channels` caught the transport error and logged it, and
    `notification.status` is written INSIDE `FCMChannel.send` after the sends — so a
    failure before that point left `status = {}`. No query anywhere read
    `status['fcm']['status'] == 'failed'` and the admin offered no resend, so an FCM
    outage of any length cost every push issued during it, permanently and invisibly,
    because the in-app row survives.

    Exercised rather than grepped: the source check matched a literal that a correct
    record (keeping the delivery history) no longer spells the same way."""
    import uuid

    from notifications.channels.fcm import FCMChannel
    from notifications.models import Notification
    from notifications.services import NotificationService
    from notifications.tasks import drain_dead_letter_queue

    user = make_user(f"gate_push_{uuid.uuid4().hex[:8]}")
    notification = Notification.objects.create(
        recipient=user, event_type="custom", related_object_id="x",
        metadata={"context": {"message": "hi"}, "data": {}},
        deduplication_key=uuid.uuid4().hex)

    calls = []

    def outage(cls, n):
        calls.append(n.pk)
        raise ConnectionError("FCM unreachable")

    monkeypatch.setattr(FCMChannel, "send", classmethod(outage))
    NotificationService._dispatch_channels(notification)
    notification.refresh_from_db()
    assert notification.status["fcm"]["status"] == "failed", "an FCM failure is not recorded"

    drain_dead_letter_queue()
    assert calls.count(notification.pk) == 2, "nothing re-sends a push that failed"


def test_no_view_orders_on_a_column_that_is_not_a_total_order():
    """Every model declares a `-created_at, -id` tiebreaker in `Meta.ordering` and the
    sweep that added them never reached the view layer — a view-level `.order_by()`
    OVERRIDES Meta. A single non-unique sort column is not a total order, so LIMIT/OFFSET
    paging repeats and hides rows whenever two records share the value, which a fan-out
    batch guarantees. Reproduced with 50 notifications sharing a timestamp: `b-1` was on
    page 1 and page 2, and `b-6` was on neither."""
    import inspect
    import re

    from achievements import views as achievements_views
    from analytics import views as analytics_views
    from notifications import tasks as notification_tasks
    from social import views as social_views
    from subscription import views as subscription_views
    from users import utils as users_utils
    from users import views as users_views
    from wallet import views as wallet_views

    offenders = []
    for module in (social_views, users_views, users_utils, wallet_views,
                   analytics_views, achievements_views, subscription_views,
                   notification_tasks):
        code = "\n".join(line.split("#")[0]
                         for line in inspect.getsource(module).splitlines())
        for match in re.findall(r"order_by\(\s*['\"]-?created_at['\"]\s*\)", code):
            offenders.append(f"{module.__name__}: {match}")

    assert not offenders, offenders


def test_a_trainer_cannot_write_to_a_plan_the_client_no_longer_follows(
        seeded_catalogue, make_user):
    """`diet_persistence` identified this exact failure — "every meal they added to it
    afterwards landed on a plan nobody follows" — and fixed only the LOGGING on the
    superseding side. The client taps Generate, silently retiring the trainer's plan; the
    trainer then builds next week's meals, gets 200 on every one, and the client sees an
    empty day."""
    import datetime as _dt

    from django.core.exceptions import ValidationError as _DjangoValidationError
    from django.utils import timezone as _tz

    from diet.models import DietPlanTemplate, FoodItem
    from diet.services.plan_generation import generate_and_save
    from diet.trainer_services import TrainerDietPlanService
    from users.models import TrainerClientRelation

    trainer = make_user("gate_retired_t", user_type="trainer")
    client_user = make_user("gate_retired_c", user_type="client", age=30, gender="male",
                            height=175, weight=75, activity_level="Moderate")
    _grant_diet_access(client_user)
    TrainerClientRelation.objects.create(trainer=trainer, client=client_user,
                                         status="approved")

    template = DietPlanTemplate.objects.create(
        name="gate retired", description="d", meals_per_day=3, snacks_per_day=1,
        days_variation=7)
    service = TrainerDietPlanService(trainer)
    plan = service.create_diet_plan(client_user, template, _tz.localdate(),
                                    duration_weeks=4)
    food = FoodItem.objects.filter(needs_review=False).first()

    # The client generates their own, which supersedes the trainer's.
    generate_and_save(client_user, duration_days=1, seed_salt="gate-retired")
    plan.refresh_from_db()
    assert plan.is_active is False

    with pytest.raises(_DjangoValidationError) as refusal:
        service.add_meal_to_plan(plan, "Dinner", _tz.localdate() + _dt.timedelta(days=2),
                                 [{"food_id": food.id, "quantity": 100}])
    assert getattr(refusal.value, "code", None) == "plan_superseded"


def test_editing_a_past_meal_does_not_erase_what_the_client_ate(
        seeded_catalogue, make_user):
    """The component rows ARE the completion record — `is_completed`, `completed_at`,
    `actual_quantity_consumed` — and `update_meal` deletes and re-creates them. A trainer
    tidying up last month's plan silently zeroed days the client actually lived, and the
    coach and analytics layers read the zeros as non-compliance. `add_meal_to_plan`
    checks the plan window; `update_meal` checked nothing."""
    import datetime as _dt

    from django.core.exceptions import ValidationError as _DjangoValidationError
    from django.utils import timezone as _tz

    from diet.models import DietPlanTemplate, FoodItem
    from diet.trainer_services import TrainerDietPlanService
    from users.models import TrainerClientRelation

    trainer = make_user("gate_past_t", user_type="trainer")
    client_user = make_user("gate_past_c", user_type="client", age=30, gender="male",
                            height=175, weight=75, activity_level="Moderate")
    _grant_diet_access(client_user)
    TrainerClientRelation.objects.create(trainer=trainer, client=client_user,
                                         status="approved")

    template = DietPlanTemplate.objects.create(
        name="gate past", description="d", meals_per_day=3, snacks_per_day=1,
        days_variation=7)
    service = TrainerDietPlanService(trainer)
    plan = service.create_diet_plan(
        client_user, template, _tz.localdate() - _dt.timedelta(days=7),
        duration_weeks=4)
    food = FoodItem.objects.filter(needs_review=False).first()

    eaten = service.add_meal_to_plan(
        plan, "Lunch", _tz.localdate() - _dt.timedelta(days=3),
        [{"food_id": food.id, "quantity": 100}])
    for component in eaten.components.all():
        component.complete()

    with pytest.raises(_DjangoValidationError) as refusal:
        service.update_meal(eaten, food_items=[{"food_id": food.id, "quantity": 200}])
    assert getattr(refusal.value, "code", None) == "meal_already_happened"

    eaten.refresh_from_db()
    assert eaten.components.filter(is_completed=True).exists(), \
        "the client's record of what they ate was destroyed"

    # A FUTURE meal is still freely editable.
    upcoming = service.add_meal_to_plan(
        plan, "Dinner", _tz.localdate() + _dt.timedelta(days=2),
        [{"food_id": food.id, "quantity": 100}])
    service.update_meal(upcoming, food_items=[{"food_id": food.id, "quantity": 150}])


def test_moving_a_meal_onto_an_occupied_slot_says_so(seeded_catalogue, make_user):
    """`add_meal_to_plan` has an explicit `duplicate_meal` pre-check and `update_meal`
    had none, so `one_meal_per_slot_per_day` fired at the database and the view's broad
    handler answered `{"code": "server_error"}` with a 500. The data was safe; the answer
    was wrong, and it spent the error budget."""
    import datetime as _dt

    from django.core.exceptions import ValidationError as _DjangoValidationError
    from django.utils import timezone as _tz

    from diet.models import DietPlanTemplate, FoodItem
    from diet.trainer_services import TrainerDietPlanService
    from users.models import TrainerClientRelation

    trainer = make_user("gate_slot_t", user_type="trainer")
    client_user = make_user("gate_slot_c", user_type="client", age=30, gender="male",
                            height=175, weight=75, activity_level="Moderate")
    _grant_diet_access(client_user)
    TrainerClientRelation.objects.create(trainer=trainer, client=client_user,
                                         status="approved")

    template = DietPlanTemplate.objects.create(
        name="gate slot", description="d", meals_per_day=3, snacks_per_day=1,
        days_variation=7)
    service = TrainerDietPlanService(trainer)
    plan = service.create_diet_plan(client_user, template, _tz.localdate(),
                                    duration_weeks=4)
    food = FoodItem.objects.filter(needs_review=False).first()
    when = _tz.localdate() + _dt.timedelta(days=2)

    first = service.add_meal_to_plan(plan, "Snack", when,
                                     [{"food_id": food.id, "quantity": 50}],
                                     scheduled_time=_dt.time(10, 0))
    service.add_meal_to_plan(plan, "Snack", when,
                             [{"food_id": food.id, "quantity": 50}],
                             scheduled_time=_dt.time(16, 0))

    with pytest.raises(_DjangoValidationError) as refusal:
        service.update_meal(first, scheduled_time=_dt.time(16, 0))
    assert getattr(refusal.value, "code", None) == "duplicate_meal"


def test_the_ledger_has_indexes_for_the_way_it_is_read():
    """`wallet_transaction` had `(source_wallet_id)`, `(destination_wallet_id)` and
    `(created_at)` separately, and the ledger screen filters `source = w OR
    destination = w` sorted by `(-created_at, -id)` — a shape none of them can serve. The
    planner walked `created_at` backwards over the WHOLE table: measured on 500,000 rows,
    a wallet with NO transactions took 207 ms and read every row to return zero, and a
    new user's first visit to their wallet history did exactly that.

    The agent top-up cap had the same problem from the other direction: it filtered
    `created_at__date=`, a function of the column that no btree on it can serve, twice
    per top-up — one agent with 10,000 rows scanned 10,000 to find 500, and the scan
    never resets."""
    import inspect

    from wallet.models import Transaction
    from wallet import views as wallet_views

    names = {index.name for index in Transaction._meta.indexes}
    assert "tx_source_recent_idx" in names, names
    assert "tx_destination_recent_idx" in names, names
    assert "tx_actor_type_time_idx" in names, names

    # And no second btree on a column that is already unique.
    fields = [tuple(index.fields) for index in Transaction._meta.indexes]
    assert ("reference_id",) not in fields, fields

    # The cap filters a RANGE, not a function of the column.
    source = inspect.getsource(wallet_views)
    code = "\n".join(line.split("#")[0] for line in source.splitlines())
    assert "created_at__date" not in code, "the top-up cap is non-sargable again"


# --------------------------------------------------------------------------------
# Cycle 9: analytics and the admin.
# --------------------------------------------------------------------------------

def test_a_client_cannot_plant_a_dashboard_on_someone_else(make_user, api):
    """`AnalyticsDashboardViewSet` was the one viewset in the module with no
    `perform_create`, and the serializer left `user` writable — so any client could POST
    a dashboard row owned by another account, with `data` of their choosing, and it sorted
    FIRST on the victim's progress screen."""
    import json as _json

    from analytics.models import AnalyticsDashboard

    victim = make_user("gate_dash_victim", user_type="client")
    attacker = make_user("gate_dash_attacker", user_type="client")
    resp = api(attacker).post("/api/analytics/dashboard/", data=_json.dumps({
        "dashboard_type": "user", "user": victim.pk, "data": {"note": "PLANTED"},
        "period_start": "2030-01-01T00:00:00Z", "period_end": "2030-02-01T00:00:00Z"}),
        content_type="application/json")
    assert resp.status_code == 201, resp.content[:200]
    assert not AnalyticsDashboard.objects.filter(user=victim).exists()
    assert AnalyticsDashboard.objects.filter(user=attacker).exists()


def test_a_non_finite_number_cannot_take_the_metrics_api_down(make_user, api):
    """DRF's FloatField accepts "NaN", "Infinity" and "1e400", Postgres stores them, and
    the row commits before the response is rendered — so every later read of that row was
    a 500. Three POSTs from one client took the metrics endpoints down for that client and
    for every staff account, whose queryset is unscoped."""
    import json as _json

    from analytics.models import PerformanceMetric, UserGoal

    user = make_user("gate_nan_client", user_type="client")
    admin = make_user("gate_nan_admin", user_type="admin")
    for value in ("NaN", "Infinity", "1e400"):
        resp = api(user).post("/api/analytics/metrics/", data=_json.dumps(
            {"metric_type": "body_fat", "value": value}), content_type="application/json")
        assert resp.status_code == 400, (value, resp.status_code)
    assert not PerformanceMetric.objects.filter(user=user).exists()
    assert api(user).get("/api/analytics/metrics/").status_code == 200
    assert api(admin).get("/api/analytics/metrics/").status_code == 200

    goal = UserGoal.objects.create(user=user, goal_type="custom", title="g", target_value=10)
    for value in ("abc", "nan", "1e400"):
        resp = api(user).post(f"/api/analytics/goals/{goal.id}/update_progress/",
                              data=_json.dumps({"new_value": value}),
                              content_type="application/json")
        assert resp.status_code == 400, (value, resp.status_code)
    assert api(user).get("/api/analytics/goals/").status_code == 200


def test_achievements_cannot_be_farmed_through_the_activity_log(make_user, api):
    """`UserActivity` was a plain ModelViewSet, and the achievement engine counts
    `routine_completed` rows while `post_save` on the model fires the award check. Three
    POSTs earned "Three Workouts" with no routine, no session and no set log anywhere. The
    server records those facts; the app may log telemetry, and a log is append-only."""
    import json as _json

    from achievements.models import Achievement, UserAchievement
    from analytics.models import UserActivity

    Achievement.objects.get_or_create(key="gate_farm_three", defaults=dict(
        name="Gate Three Workouts", description="x", category="workout",
        criteria={"type": "workout_count", "target": 3, "condition": "gte"},
        points=10, is_active=True))
    farmer = make_user("gate_farmer", user_type="client")
    for _ in range(3):
        resp = api(farmer).post("/api/analytics/activities/", data=_json.dumps(
            {"activity_type": "routine_completed"}), content_type="application/json")
        assert resp.status_code == 400, resp.content[:200]
    assert not UserActivity.objects.filter(user=farmer).exists()
    assert not UserAchievement.objects.filter(user=farmer).exists()

    logged = api(farmer).post("/api/analytics/activities/", data=_json.dumps(
        {"activity_type": "page_view"}), content_type="application/json")
    assert logged.status_code == 201
    row = logged.json()["id"]
    assert api(farmer).patch(f"/api/analytics/activities/{row}/", data=_json.dumps(
        {"activity_type": "logout"}), content_type="application/json").status_code == 405
    assert api(farmer).delete(f"/api/analytics/activities/{row}/").status_code == 405

    oversized = api(farmer).post("/api/analytics/activities/", data=_json.dumps(
        {"activity_type": "page_view", "metadata": {"x": "a" * 200000}}),
        content_type="application/json")
    assert oversized.status_code == 400


def test_analytics_numbers_are_bounded_and_server_derived(make_user, api):
    """A 3,000 kg weigh-in, a billion page views and a thirty-day session were all 201s,
    and a goal could be declared completed, reopened, given a negative target, or shown as
    100% at "3 kg of 10" by PATCHing `current_value` past `update_progress`."""
    import json as _json

    from analytics.models import UserGoal

    user = make_user("gate_bounds_client", user_type="client")

    def post(url, body):
        return api(user).post(url, data=_json.dumps(body), content_type="application/json")

    assert post("/api/analytics/metrics/", {"metric_type": "weight", "value": 3000}).status_code == 400
    assert post("/api/analytics/metrics/", {"metric_type": "weight", "value": 80}).status_code == 201

    assert post("/api/analytics/sessions/", {"session_id": "gate-s1",
                                            "page_views": 10 ** 9}).status_code == 400
    session = post("/api/analytics/sessions/", {"session_id": "gate-s2",
                                               "duration_seconds": 2592000})
    assert session.status_code == 201 and session.json()["duration_seconds"] is None

    assert post("/api/analytics/goals/", {"goal_type": "custom", "title": "g",
                                         "target_value": -10}).status_code == 400
    goal = UserGoal.objects.create(user=user, goal_type="custom", title="g", target_value=10)
    assert api(user).patch(f"/api/analytics/goals/{goal.id}/", data=_json.dumps(
        {"status": "completed"}), content_type="application/json").status_code == 400
    api(user).patch(f"/api/analytics/goals/{goal.id}/", data=_json.dumps(
        {"current_value": 3}), content_type="application/json")
    goal.refresh_from_db()
    assert goal.current_value == 0 and goal.progress_percentage == 0

    post(f"/api/analytics/goals/{goal.id}/update_progress/", {"new_value": -5})
    goal.refresh_from_db()
    assert goal.progress_percentage == 0 and goal.status == "active"


def test_a_staff_accounts_own_metrics_are_its_own(make_user, api):
    """`get_queryset()` is the whole table for a staff caller and `current_metrics` never
    re-scoped, so DISTINCT ON returned the newest weight of ANY user as the admin's own."""
    from analytics.models import PerformanceMetric

    admin = make_user("gate_scope_admin", user_type="admin")
    other = make_user("gate_scope_other", user_type="client")
    PerformanceMetric.objects.create(user=admin, metric_type="weight", value=70.0)
    PerformanceMetric.objects.create(user=other, metric_type="weight", value=123.4)
    rows = api(admin).get("/api/analytics/metrics/current_metrics/").json()
    assert [r["latest_value"] for r in rows if r["metric_type"] == "weight"] == [70.0]


def test_money_is_not_editable_in_the_admin(make_user, client):
    """Every concrete field of a ModelAdmin is editable by default, and `WalletAdmin`
    declared only list columns — so POSTing `balance=999999` was a 302 and a committed row
    with zero Transaction rows and zero audit entries: money minted outside
    `move_funds_atomic` and invisible to any reconciliation."""
    from decimal import Decimal as _D

    from wallet.models import Wallet

    superuser = make_user("gate_money_su", user_type="admin")
    superuser.is_superuser = True
    superuser.save()
    client.force_login(superuser)

    owner = make_user("gate_money_owner", user_type="client")
    wallet, _ = Wallet.objects.get_or_create(owner=owner, defaults={"owner_type": "client"})
    client.post(f"/admin/wallet/wallet/{wallet.pk}/change/",
                {"balance": "999999", "owner_type": "client", "currency": "SYP"})
    wallet.refresh_from_db()
    assert wallet.balance == _D("0"), wallet.balance

    from wallet.admin import AgentAPIKeyAdmin

    assert "secret_ciphertext" in (AgentAPIKeyAdmin.exclude or ())
    assert "hashed_key" in AgentAPIKeyAdmin.readonly_fields


def test_the_wallet_audit_log_refuses_delete_at_the_database(make_user):
    """`WalletAuditLog.delete()` raised, which protected one path. `QuerySet.delete()`
    never calls it, so the admin's own `delete_selected` removed rows — and
    `verify_chain()` walks only surviving rows, so deleting the NEWEST entries left the
    chain reporting VALID. The database is the one layer every path goes through."""
    from django.db import DatabaseError, connection, transaction as _txn

    from wallet.models import WalletAuditLog

    if connection.vendor != "postgresql":
        pytest.skip("the append-only trigger is PostgreSQL-only")

    WalletAuditLog.objects.create(event_type="gate_probe", payload={})
    before = WalletAuditLog.objects.count()
    with pytest.raises(DatabaseError):
        with _txn.atomic():
            WalletAuditLog.objects.all().delete()
    assert WalletAuditLog.objects.count() == before


def test_the_client_inline_cannot_delete_a_person(make_user, client):
    """The inline's object is the client's USER ACCOUNT. With the default
    `can_delete = True`, the DELETE box on a trainer's "Clients" row read as "remove this
    client" and deleted the person, with their history cascading away."""
    from users.admin import ClientInline

    assert ClientInline.can_delete is False
    assert ClientInline.has_add_permission(ClientInline, None) is False

    superuser = make_user("gate_inline_su", user_type="admin")
    superuser.is_superuser = True
    superuser.save()
    client.force_login(superuser)
    trainer = make_user("gate_inline_trainer", user_type="trainer")
    kid = make_user("gate_inline_kid", user_type="client")
    kid.assigned_trainer = trainer
    kid.save()
    page = client.get(f"/admin/users/customuser/{trainer.pk}/change/")
    assert page.status_code == 200
    assert b"clients-0-DELETE" not in page.content


def test_a_staff_account_with_no_permissions_is_not_shown_the_platform(make_user, client):
    """Replacing `AdminSite.index` also replaced the one place the admin filters through
    `has_module_permission`; `admin_view` only checks `is_staff`. A staff account refused
    on every changelist was shown the newest users with their email addresses and
    everyone's recent activity on the landing page."""
    import re as _re

    make_user("gate_index_bystander", user_type="client")
    staff = make_user("gate_index_staff", user_type="client")
    staff.is_staff = True
    staff.save()
    client.force_login(staff)

    page = client.get("/dj-admin/")
    assert page.status_code == 200
    emails = set(_re.findall(rb"[\w.+-]+@gate\.test", page.content))
    assert emails <= {b"gate_index_staff@gate.test"}, emails
    assert b"Training Platform Dashboard" not in page.content


def test_bulk_user_actions_go_through_the_model_and_are_logged(make_user, client):
    """`queryset.update()` skipped `CustomUser.save()` and wrote no admin log: an
    admin-type account activated in bulk never got `is_staff`, and a bulk privilege change
    left no record of who made it. The correct version lived in a class no site
    registered."""
    from django.contrib.admin.models import LogEntry

    from users.models import CustomUser

    superuser = make_user("gate_bulk_su", user_type="admin")
    superuser.is_superuser = True
    superuser.save()
    client.force_login(superuser)

    target = make_user("gate_bulk_target", user_type="admin")
    CustomUser.objects.filter(pk=target.pk).update(is_active=False, is_staff=False)
    client.post("/admin/users/customuser/", {"action": "activate_users",
                                            "_selected_action": [target.pk]})
    target.refresh_from_db()
    assert target.is_active is True and target.is_staff is True
    assert LogEntry.objects.filter(object_id=str(target.pk)).exists()


def test_environment_settings_extend_the_throttle_rates_rather_than_replace_them():
    """settings_local and settings_production each assigned a NEW dict to
    `DEFAULT_THROTTLE_RATES`, silently dropping every scope settings_base declared — so a
    view throttled on a base scope raised in every real environment."""
    from rest_framework.settings import api_settings

    assert "analytics_write" in api_settings.DEFAULT_THROTTLE_RATES
    assert "charging" in api_settings.DEFAULT_THROTTLE_RATES


def test_a_missed_reminder_is_recovered_briefly_and_never_sent_twice(make_user, monkeypatch):
    """Two failures that pull against each other, pinned at fixed clock times so the
    result does not depend on when the gate runs.

    Exactly-on-the-hour lost every user a cut-off sweep did not reach, for the whole day.
    The first fix — "at or past their hour on the same local day" — compared hours on a
    0-23 line, so a 02:00 reminder fired at 21:00, nineteen hours late; the gate only
    caught it because it happened to run in the evening. The window is now a few hours
    after the due hour, measured mod 24, and the dedup date is the date it was DUE.
    """
    import datetime as _dt

    from django.utils import timezone as _tz

    from notifications.models import Notification
    from notifications.tasks import REMINDER_CATCH_UP_HOURS, send_workout_reminders
    from routine.models import Routine, RoutineProgress

    trainer = make_user("gate_catchup_t", user_type="trainer")
    routine = Routine.objects.create(name="Gate catch-up", created_by=trainer, days=3,
                                     start_date=_dt.date(2026, 3, 1))

    def client_due_at(name, hour):
        user = make_user(name, user_type="client")
        user.preferred_timezone = "UTC"
        user.workout_reminder_hour = hour
        user.save()
        routine.assigned_to.add(user)
        RoutineProgress.objects.get_or_create(
            user=user, routine=routine, day=1, date=routine.start_date,
            defaults={"status": "not_started"})
        return user

    def run_at(when):
        monkeypatch.setattr(_tz, "now", lambda: when)
        send_workout_reminders()

    def reminders(user):
        return Notification.objects.filter(recipient=user, event_type="session_reminder").count()

    late_night = client_due_at("gate_catchup_2300", 23)

    # The 23:00 sweep reaches them.
    run_at(_dt.datetime(2026, 3, 10, 23, 10, tzinfo=_dt.timezone.utc))
    assert reminders(late_night) == 1

    # A second user due at 23:00 whom that sweep never reached (it was cut off).
    missed = client_due_at("gate_catchup_missed", 23)
    # And someone whose reminder was hours ago.
    long_gone = client_due_at("gate_catchup_2000", 20)

    # Two hours later, across midnight: the missed user is recovered, the first user is
    # NOT reminded again (same due date, same dedup key), and the 20:00 user is too late.
    assert REMINDER_CATCH_UP_HOURS >= 2
    run_at(_dt.datetime(2026, 3, 11, 1, 10, tzinfo=_dt.timezone.utc))
    assert reminders(missed) == 1, "a user a cut-off sweep missed was never recovered"
    assert reminders(late_night) == 1, "a recovered reminder was sent a second time"
    assert reminders(long_gone) == 0, "a reminder five hours late was sent anyway"

    # And a user whose reminder is 02:00 is not messaged at 21:00 the evening before.
    early = client_due_at("gate_catchup_0200", 2)
    run_at(_dt.datetime(2026, 3, 11, 21, 10, tzinfo=_dt.timezone.utc))
    assert reminders(early) == 0, "a 02:00 reminder fired nineteen hours late"


def test_every_declared_admin_action_exists(db):
    """Fourteen actions were declared by NAME with no method behind them. `get_action`
    returns None for a name it cannot resolve, `_get_base_actions` drops it silently, and
    the admin checks do not run on a custom AdminSite — so there was no bulk post
    moderation and no bulk subscription cancel, with no error anywhere."""
    from django.contrib import admin as dj_admin

    from admin_dashboard.admin import admin_site

    missing = []
    for site in (dj_admin.site, admin_site):
        for model, model_admin in site._registry.items():
            for declared in (getattr(model_admin, "actions", None) or []):
                if model_admin.get_action(declared) is None:
                    missing.append((site.name, model.__name__, declared))
    assert not missing, missing


def test_admin_bulk_actions_do_not_bypass_the_model(db):
    """`queryset.update()` in an admin action skips `save()`, every signal and every
    validator, and writes nothing to the admin log."""
    import pathlib

    root = pathlib.Path(__file__).resolve().parent.parent
    offenders = []
    for path in root.glob("*/admin.py"):
        if "_excluded" in path.parts:
            continue
        for number, line in enumerate(path.read_text().splitlines(), 1):
            code = line.split("#")[0]
            # Every app's admin, not a list of four: the list left diet/admin.py's review
            # actions writing unlogged, which is how the rule stayed half-applied.
            if "queryset.update(" in code:
                offenders.append(f"{path.parts[-2]}/admin.py:{number}")
    assert not offenders, offenders


def test_moderation_actions_hide_a_post_and_record_who_did_it(make_user, client):
    from django.contrib.admin.models import LogEntry

    from social.models import Post

    superuser = make_user("gate_mod_su", user_type="admin")
    superuser.is_superuser = True
    superuser.save()
    client.force_login(superuser)
    author = make_user("gate_mod_author", user_type="client")
    post = Post.objects.create(author=author, content="spam", visibility="public")

    client.post("/dj-admin/social/post/", {"action": "hide_posts",
                                          "_selected_action": [post.pk]})
    post.refresh_from_db()
    assert post.is_hidden is True
    assert LogEntry.objects.filter(object_id=str(post.pk)).exists()


def test_rendering_a_routine_as_text_costs_no_queries(make_user):
    """`Routine.__str__` ran two queries — five assignee usernames and a COUNT — every
    time a routine was printed: 90 rows of routine progress in the admin cost 180 queries,
    and client usernames went into every admin log entry about a routine."""
    from django.db import connection as _conn
    from django.test.utils import CaptureQueriesContext

    from routine.models import Routine

    trainer = make_user("gate_str_trainer", user_type="trainer")
    routine = Routine.objects.create(name="Gate text", created_by=trainer, days=1)
    routine.assigned_to.add(make_user("gate_str_client", user_type="client"))
    routine = Routine.objects.get(pk=routine.pk)

    with CaptureQueriesContext(_conn) as queries:
        text = str(routine)
    assert len(queries) == 0, [q["sql"] for q in queries]
    assert text == "Gate text"
    assert "gate_str_client" not in text


def test_admin_changelists_do_not_query_per_row(make_user, client):
    from django.db import connection as _conn
    from django.test.utils import CaptureQueriesContext

    from routine.models import Routine

    superuser = make_user("gate_cl_su", user_type="admin")
    superuser.is_superuser = True
    superuser.save()
    client.force_login(superuser)
    trainer = make_user("gate_cl_trainer", user_type="trainer")

    def measure(rows):
        for index in range(rows):
            routine = Routine.objects.create(name=f"gate cl {rows}-{index}",
                                             created_by=trainer, days=1)
            routine.assigned_to.add(make_user(f"gate_cl_c{rows}_{index}", user_type="client"))
        client.get("/dj-admin/routine/routine/")
        with CaptureQueriesContext(_conn) as queries:
            assert client.get("/dj-admin/routine/routine/").status_code == 200
        return len(queries)

    few = measure(2)
    many = measure(20)
    assert many <= few + 2, (few, many)


def test_changing_a_foods_serving_size_does_not_rescale_its_macros(make_user, client,
                                                                   seeded_catalogue):
    """`FoodItem.save()` normalised macros whenever `serving_size_grams` was not 100 — on
    every save, including updates — and the admin showed that field under "Nutrition Per
    Serving". An owner typing 40 for "a serving is 40 g" saved, saw the box read 100 again,
    and Akkawi Cheese had gone from 264 to 660 kcal per 100 g in every plan built after.
    Normalising is an import convenience; importers create."""
    import json as _json

    from django.core.exceptions import ValidationError as _Invalid

    from diet.models import FoodItem

    food = FoodItem.objects.filter(needs_review=False, serving_size_grams=100).first()
    before = (food.calories, food.protein, food.carbs, food.fat)

    # The model refuses the ambiguous update outright.
    food.serving_size_grams = 40
    with pytest.raises(_Invalid):
        food.save()
    food.refresh_from_db()
    assert (food.calories, food.protein, food.carbs, food.fat) == before

    # The admin no longer offers it as an input at all.
    from diet.admin import ImprovedFoodItemAdmin
    assert "serving_size_grams" in ImprovedFoodItemAdmin.readonly_fields

    superuser = make_user("gate_serving_su", user_type="admin")
    superuser.is_superuser = True
    superuser.save()
    client.force_login(superuser)
    page = client.get(f"/admin/diet/fooditem/{food.pk}/change/")
    assert 'name="serving_size_grams"' not in page.content.decode()

    # And an IMPORT that arrives per serving is still normalised to per 100 g.
    imported = FoodItem.objects.create(name="gate per-serving import", calories=100,
                                       protein=10, carbs=10, fat=2, serving_size_grams=50,
                                       needs_review=True)
    assert imported.serving_size_grams == 100 and imported.calories == 200


def test_an_unsupported_accept_language_defers_to_the_users_choice(make_user, api):
    """`get_language_from_request` never returns None — it falls through to
    LANGUAGE_CODE — so ANY Accept-Language header short-circuited the stored preference,
    and a user who chose Arabic got English for `*` or `fr-FR`. An explicit supported
    language still wins, as the contract documents."""
    user = make_user("gate_lang_user", user_type="client")
    user.preferred_language = "ar"
    user.save(update_fields=["preferred_language"])
    client_api = api(user)

    def language_for(header):
        extra = {"HTTP_ACCEPT_LANGUAGE": header} if header is not None else {}
        return client_api.get("/api/achievements/", **extra).headers.get("Content-Language")

    assert language_for(None) == "ar"
    assert language_for("*") == "ar"
    assert language_for("fr-FR") == "ar"
    assert language_for("ar") == "ar"
    assert language_for("en") == "en"
