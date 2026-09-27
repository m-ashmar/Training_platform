"""Launch data: `bootstrap_platform` and `seed_review_accounts`.

The production-readiness audit (2026-09-27) found that the deploy's seed step ran only
`load_food_catalogue`, so a new database opened with no plans, no diet templates, no
achievements and no shared exercises; and that the demo accounts the store reviewers
need (spec section 10, section 9 B8) had no command at all — only the app's dev script,
which hard-codes a shared password.

`bootstrap_platform` must bring an EMPTY database to launch and change nothing on a
second run; `seed_review_accounts` must build the reviewers' accounts through the
platform's own services, from addresses and a password it is GIVEN, refuse without
them, and change nothing on a second run.
"""
from __future__ import annotations

import io
import re
from pathlib import Path

import pytest
from django.core.management import call_command
from django.utils import translation

ROOT = Path(__file__).resolve().parents[1]
ARABIC = re.compile(r"[؀-ۿ]")


# ------------------------------------------------------------------------- helpers
def _bootstrap(*args):
    out = io.StringIO()
    call_command("bootstrap_platform", *args, stdout=out)
    return out.getvalue()


def _empty_the_launch_tables():
    """What an empty production database holds of everything the bootstrap writes:
    nothing. Deleted inside the test's transaction, so it is all back afterwards."""
    from achievements.models import Achievement
    from diet.models import (DietPlanTemplate, FoodItem, MealComponent, MealSwap, Recipe,
                             RecipeIngredient)
    from policy.models import ChannelCapabilities
    from routine.models import Exercise
    from social.models import Achievement as SocialAchievement
    from subscription.models import SubscriptionFeature, SubscriptionPlan

    for model in (ChannelCapabilities, SubscriptionFeature, SubscriptionPlan,
                  DietPlanTemplate, Achievement, SocialAchievement, Exercise,
                  MealSwap, MealComponent, RecipeIngredient, Recipe, FoodItem):
        model.objects.all().delete()


def _rows(model, **filters):
    """Every column of every row but the timestamps, in a stable order."""
    rows = []
    for row in model.objects.filter(**filters).order_by("pk").values():
        rows.append({k: v for k, v in row.items()
                     if not k.endswith(("_at", "updated", "created"))})
    return rows


def _snapshot():
    from achievements.models import Achievement
    from diet.models import DietPlanTemplate, FoodItem, Recipe, RecipeIngredient
    from policy.models import CapabilityChangeLog, ChannelCapabilities
    from routine.models import Exercise
    from social.models import Achievement as SocialAchievement
    from subscription.models import SubscriptionFeature, SubscriptionPlan

    return {
        "capabilities": _rows(ChannelCapabilities),
        "capability_log": CapabilityChangeLog.objects.count(),
        "features": _rows(SubscriptionFeature),
        "plans": _rows(SubscriptionPlan),
        "templates": _rows(DietPlanTemplate),
        "achievements": _rows(Achievement),
        "social_achievements": _rows(SocialAchievement),
        "exercises": _rows(Exercise),
        "foods": _rows(FoodItem),
        "recipes": _rows(Recipe),
        "recipe_lines": RecipeIngredient.objects.count(),
    }


# ============================================================== bootstrap_platform
@pytest.mark.django_db
def test_bootstrap_brings_an_empty_database_to_launch_and_a_second_run_writes_nothing():
    from achievements.models import Achievement
    from achievements.registry import ACHIEVEMENTS
    from diet.models import DietPlanTemplate, FoodItem, Recipe
    from policy.defaults import DEFAULT_FLAGS
    from policy.models import FLAG_FIELDS, ChannelCapabilities
    from routine.data.exercise_catalogue import STARTER_EXERCISES
    from routine.models import Exercise
    from social.management.commands.create_achievements import Command as Social
    from social.models import Achievement as SocialAchievement
    from subscription.models import SubscriptionFeature, SubscriptionPlan
    from subscription.quota import FEATURES

    _empty_the_launch_tables()
    first = _bootstrap()

    # The spec's table, one base row per channel.
    for channel, flags in DEFAULT_FLAGS.items():
        row = ChannelCapabilities.objects.get(channel=channel, min_version="0.0.0")
        assert {name: getattr(row, name) for name in FLAG_FIELDS} == flags, channel
    # Every quota the permission layer counts has its row.
    assert set(SubscriptionFeature.objects.values_list("name", flat=True)) == set(FEATURES)
    # Four plans, each named in both languages.
    plans = {p.name_en: p for p in SubscriptionPlan.objects.all()}
    assert set(plans) == {"Free Plan", "Basic Plan", "Premium Plan", "Professional Plan"}
    for plan in plans.values():
        assert ARABIC.search(plan.name_ar) and ARABIC.search(plan.description_ar), plan.name_en
    assert DietPlanTemplate.objects.count() == 6
    assert all(ARABIC.search(t.name_ar) for t in DietPlanTemplate.objects.all())
    assert Achievement.objects.count() == len(ACHIEVEMENTS)
    assert SocialAchievement.objects.count() == len(Social().get_achievements_data())
    # The starter exercises: global, both languages, each marked with its key.
    exercises = Exercise.objects.all()
    assert exercises.count() == len(STARTER_EXERCISES)
    assert {e.catalogue_key for e in exercises} == {e.key for e in STARTER_EXERCISES}
    assert all(e.is_global and e.created_by_id is None and e.is_active for e in exercises)
    by_key = {e.catalogue_key: e for e in exercises}
    for entry in STARTER_EXERCISES:
        row = by_key[entry.key]
        assert (row.name_en, row.name_ar, row.target_muscle, row.equipment,
                row.difficulty_level) == (entry.name_en, entry.name_ar,
                                          entry.target_muscle, entry.equipment,
                                          entry.difficulty), entry.key
    # The food catalogue and the recipe library it seeds.
    assert FoodItem.objects.count() > 100 and Recipe.objects.filter(is_active=True).count() > 50
    assert "created" in first

    snapshot = _snapshot()
    second = _bootstrap()
    assert _snapshot() == snapshot, "the second run changed something"
    assert "nothing was missing" in second, second
    assert set(_created_column(second).values()) == {"0"}, second
    assert all(int(n) > 0 for n in _created_column(first).values()), first


def _created_column(out):
    """{step: created} from the command's table."""
    lines = out.splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith("step"))
    table = {}
    for line in lines[start + 1:]:
        cells = re.split(r"\s{2,}", line.strip())
        if len(cells) >= 3 and cells[1].isdigit():
            table[cells[0]] = cells[1]
    return table


@pytest.mark.django_db
def test_bootstrap_under_an_arabic_locale_writes_the_rows_an_english_run_then_finds():
    """Several of these models have translated names, and a lookup on a translated
    field follows the ACTIVE language: run under Arabic, a naive get_or_create finds
    nothing English and writes a second set. The bootstrap runs in the default
    language whatever the caller's locale."""
    from routine.models import Exercise
    from subscription.models import SubscriptionPlan

    _empty_the_launch_tables()
    with translation.override("ar"):
        _bootstrap("--skip-food-catalogue")
        call_command("setup_subscription_plans", stdout=io.StringIO())
        call_command("setup_diet_templates", stdout=io.StringIO())
    snapshot = _snapshot()
    assert "nothing was missing" in _bootstrap("--skip-food-catalogue")
    assert _snapshot() == snapshot
    assert SubscriptionPlan.objects.filter(name_en="Premium Plan").count() == 1
    assert Exercise.objects.filter(name_en="Deadlift").count() == 1


@pytest.mark.django_db
def test_bootstrap_leaves_what_an_operator_changed_alone():
    from decimal import Decimal

    from achievements.models import Achievement
    from policy.models import ChannelCapabilities
    from routine.models import Exercise
    from subscription.models import SubscriptionPlan

    _empty_the_launch_tables()
    _bootstrap("--skip-food-catalogue")

    premium = SubscriptionPlan.objects.get(name_en="Premium Plan")
    premium.price = Decimal("99000.00")
    premium.name_ar = "بريميوم"
    premium.save()
    deadlift = Exercise.objects.get(catalogue_key="deadlift")
    deadlift.name_en, deadlift.is_active = "Conventional Deadlift", False
    deadlift.save()
    apk = ChannelCapabilities.objects.get(channel="apk", min_version="0.0.0")
    apk.can_show_web_hub_link = False
    apk.save()
    badge = Achievement.objects.order_by("id").first()
    badge.points = 999
    badge.save()

    assert "nothing was missing" in _bootstrap("--skip-food-catalogue")
    premium.refresh_from_db()
    deadlift.refresh_from_db()
    apk.refresh_from_db()
    badge.refresh_from_db()
    assert (premium.price, premium.name_ar) == (Decimal("99000.00"), "بريميوم")
    assert (deadlift.name_en, deadlift.is_active) == ("Conventional Deadlift", False)
    assert Exercise.objects.filter(name_en__in=["Deadlift", "Conventional Deadlift"]).count() == 1
    assert apk.can_show_web_hub_link is False
    assert badge.points == 999


@pytest.mark.django_db
def test_bootstrap_adopts_an_exercise_library_that_is_already_there():
    """A database that already holds a shared library (the development one had 554
    exercises) gets no duplicate of a same-named exercise: the existing row takes the
    key, and nothing else about it changes."""
    from routine.models import Exercise

    _empty_the_launch_tables()
    with translation.override("en"):
        mine = Exercise(name="Deadlift", name_en="Deadlift", name_ar="ديدلفت",
                        target_muscle="Hamstrings", difficulty_level="expert")
        mine.save()
    out = _bootstrap("--skip-food-catalogue")
    mine.refresh_from_db()
    assert mine.catalogue_key == "deadlift"
    assert (mine.name_ar, mine.target_muscle, mine.difficulty_level) == (
        "ديدلفت", "Hamstrings", "expert")
    assert Exercise.objects.filter(name_en="Deadlift").count() == 1
    assert "adopted" in out


@pytest.mark.django_db
def test_a_free_plan_is_created_switched_off_and_the_paid_ones_on():
    """Subscribing to a price-0 plan leaves the subscription pending for ever (the Pay
    button answers `payment_not_required` and nothing activates it), so a new database
    must not offer one until something grants it. `setup_subscription_plans` shares the
    catalogue and the rule."""
    from subscription.models import SubscriptionPlan

    _empty_the_launch_tables()
    call_command("setup_subscription_plans", stdout=io.StringIO())
    active = dict(SubscriptionPlan.objects.values_list("name_en", "is_active"))
    assert active == {"Free Plan": False, "Basic Plan": True, "Premium Plan": True,
                      "Professional Plan": True}


def test_the_starter_catalogue_is_a_usable_mix():
    """About sixty exercises, compound and isolation, for the gym and for home, every
    one named in both languages and using only the keys the model stores."""
    from routine.data.exercise_catalogue import STARTER_EXERCISES
    from routine.models import EQUIPMENT_CHOICES, Exercise

    muscles = {key for key, _label in Exercise._meta.get_field("target_muscle").choices}
    levels = {key for key, _label in Exercise._meta.get_field("difficulty_level").choices}
    equipment = {code for code, _label in EQUIPMENT_CHOICES}

    assert 55 <= len(STARTER_EXERCISES) <= 70
    assert len({e.key for e in STARTER_EXERCISES}) == len(STARTER_EXERCISES)
    assert len({e.name_en.lower() for e in STARTER_EXERCISES}) == len(STARTER_EXERCISES)
    for e in STARTER_EXERCISES:
        assert re.fullmatch(r"[a-z0-9]+(-[a-z0-9]+)*", e.key) and len(e.key) <= 64, e.key
        assert e.target_muscle in muscles and e.equipment in equipment, e.key
        assert e.difficulty in levels and e.kind in ("compound", "isolation"), e.key
        assert ARABIC.search(e.name_ar) and e.name_en.isascii(), e.key
    assert {e.kind for e in STARTER_EXERCISES} == {"compound", "isolation"}
    assert {e.home for e in STARTER_EXERCISES} == {True, False}
    assert {e.equipment for e in STARTER_EXERCISES} == equipment
    home = [e for e in STARTER_EXERCISES if e.home]
    assert all(e.equipment in ("bodyweight", "dumbbell") for e in home)
    # Every region of the body a program is built from.
    regions = {"Middle Chest", "Lats", "Front Deltoid", "Biceps", "Triceps",
               "Front Quads", "Hamstrings", "Glutes", "Calves", "Abdominals"}
    assert regions <= {e.target_muscle for e in STARTER_EXERCISES}


def test_the_bootstrap_seeds_the_quota_features_migration_0008_seeds():
    import importlib

    from subscription.quota import FEATURES
    from users.management.commands.bootstrap_platform import QUOTA_FEATURE_DESCRIPTIONS

    migration = importlib.import_module(
        "subscription.migrations.0008_quota_periods_and_features")
    assert migration.FEATURES == QUOTA_FEATURE_DESCRIPTIONS
    assert set(QUOTA_FEATURE_DESCRIPTIONS) == set(FEATURES)


# ============================================================ seed_review_accounts
ROLES = ("client", "trainer", "agent", "client2", "client3", "prospect")


@pytest.fixture
def storage_roots(settings, tmp_path):
    """The trainer's verification documents land in a temporary directory, apart from
    the public media root, as in production."""
    settings.MEDIA_ROOT = str(tmp_path / "media")
    settings.PRIVATE_MEDIA_ROOT = str(tmp_path / "private")
    return tmp_path


@pytest.fixture
def review_env(monkeypatch):
    """The two variables, with a password made up at run time: none is written here."""
    import secrets

    password = f"Rv-{secrets.token_urlsafe(18)}!7"
    monkeypatch.setenv("REVIEW_ACCOUNTS_EMAIL", "store-review@yalla-review.test")
    monkeypatch.setenv("REVIEW_ACCOUNTS_PASSWORD", password)
    return password


def _seed(*args):
    out = io.StringIO()
    call_command("seed_review_accounts", *args, stdout=out)
    return out.getvalue()


def _review_tables():
    """Row counts of everything the seed writes."""
    from achievements.models import Achievement
    from diet.models import DietPlan
    from routine.models import (DayOutcome, ExerciseSetLog, Routine, RoutineTemplate,
                                UserExerciseProgress, WorkoutSession)
    from social.models import Challenge, ChallengeParticipation
    from subscription.models import Payment, Subscription
    from trainer_verification.models import (VerificationApplication,
                                             VerificationDecision, VerificationDocument)
    from users.models import CoachingPeriod, CustomUser, TrainerClientRelation
    from wallet.models import AgentProfile, AgentReceivableEntry, Transaction

    models = (CustomUser, Achievement, DietPlan, Routine, RoutineTemplate, WorkoutSession,
              ExerciseSetLog, UserExerciseProgress, DayOutcome, Challenge,
              ChallengeParticipation, Payment, Subscription, VerificationApplication,
              VerificationDecision, VerificationDocument, CoachingPeriod,
              TrainerClientRelation, AgentProfile, AgentReceivableEntry, Transaction)
    return {model.__name__: model.objects.count() for model in models}


def test_seed_refuses_without_its_environment_and_writes_nothing(db, monkeypatch):
    from django.contrib.auth.models import Group
    from django.core.management.base import CommandError

    from users.models import CustomUser

    users = CustomUser.objects.count()
    monkeypatch.delenv("REVIEW_ACCOUNTS_EMAIL", raising=False)
    monkeypatch.delenv("REVIEW_ACCOUNTS_PASSWORD", raising=False)
    with pytest.raises(CommandError) as refused:
        _seed()
    assert "REVIEW_ACCOUNTS_EMAIL" in str(refused.value)
    assert "REVIEW_ACCOUNTS_PASSWORD" in str(refused.value)

    monkeypatch.setenv("REVIEW_ACCOUNTS_EMAIL", "store-review@yalla-review.test")
    with pytest.raises(CommandError) as refused:
        _seed()
    assert "REVIEW_ACCOUNTS_PASSWORD" in str(refused.value)
    assert "REVIEW_ACCOUNTS_EMAIL" not in str(refused.value)

    # A weak password is refused by the platform's own validators.
    monkeypatch.setenv("REVIEW_ACCOUNTS_PASSWORD", "password")
    with pytest.raises(CommandError, match="not strong enough"):
        _seed()
    # An address that is not one.
    monkeypatch.setenv("REVIEW_ACCOUNTS_EMAIL", "not-an-address")
    with pytest.raises(CommandError, match="REVIEW_ACCOUNTS_EMAIL"):
        _seed()

    assert CustomUser.objects.count() == users
    assert not Group.objects.filter(name="Store review accounts").exists()


def test_seed_refuses_on_a_database_that_was_not_bootstrapped(db, review_env):
    from django.core.management.base import CommandError

    from users.models import CustomUser

    _empty_the_launch_tables()
    users = CustomUser.objects.count()
    with pytest.raises(CommandError, match="bootstrap_platform"):
        _seed()
    assert CustomUser.objects.count() == users


def test_seed_never_takes_over_an_account_that_is_not_a_review_account(
        seeded_catalogue, review_env, make_user):
    from django.core.management.base import CommandError

    _bootstrap("--skip-food-catalogue")
    person = make_user("someone_real")
    person.email = "store-review+trainer@yalla-review.test"
    person.save(update_fields=["email"])
    with pytest.raises(CommandError, match="not a review account"):
        _seed()
    person.refresh_from_db()
    assert person.check_password("Xx!23456") and person.user_type == "client"
    assert not person.groups.exists()


def test_seed_builds_what_the_reviewers_need_and_a_second_run_changes_nothing(
        seeded_catalogue, review_env, storage_roots, settings, monkeypatch,
        django_capture_on_commit_callbacks):
    from datetime import timedelta
    from decimal import Decimal

    from django.utils import timezone

    from diet.models import DietPlan
    from routine.models import DayOutcome, ExerciseSetLog, Routine, WorkoutSession
    from social.models import ChallengeParticipation
    from subscription.models import Payment, Subscription
    from trainer_verification.models import VerificationDecision
    from users.coaching import running_month
    from users.models import CoachingPeriod, CustomUser, TrainerClientRelation
    from wallet import agents
    from wallet.models import AgentProfile, Transaction, wallet_for
    from wallet.utils import get_platform_revenue_wallet

    settings.COACHING_COMMISSION_RATE = Decimal("0.20")     # the owner's 20%
    _bootstrap("--skip-food-catalogue")
    with django_capture_on_commit_callbacks(execute=True):
        first = _seed()

    people = {u.username.removeprefix("review_"): u
              for u in CustomUser.objects.filter(groups__name="Store review accounts")}
    assert set(people) == set(ROLES), first
    for role, user in people.items():
        assert user.email == f"store-review+{role}@yalla-review.test"
        assert user.check_password(review_env) and user.email_verified and user.is_active
        assert user.age and user.age >= 14
    client, trainer, agent = people["client"], people["trainer"], people["agent"]
    assert (client.user_type, trainer.user_type, agent.user_type) == ("client", "trainer", "agent")
    for name in review_env, "Yalla!Gym2026":
        assert name not in first, "the output never shows a password"

    # Premium, granted by the operator: active, and no payment behind it.
    subscription = Subscription.objects.get(user=client)
    assert subscription.is_active and subscription.plan.name_en == "Premium Plan"
    assert subscription.source == "operator_grant"
    assert subscription.end_date - timezone.now() > timedelta(days=80)
    assert not Payment.objects.filter(subscription=subscription).exists()

    # A verified trainer, by an operator's decision, with three clients and a request.
    trainer.refresh_from_db()
    assert trainer.trainer_is_verified and not trainer.trainer_is_available
    assert VerificationDecision.objects.filter(
        application__trainer=trainer, outcome="verified").count() == 1
    relations = TrainerClientRelation.objects.filter(trainer=trainer)
    assert {r.client.username for r in relations.filter(status="approved")} == {
        "review_client", "review_client2", "review_client3"}
    assert [r.client.username for r in relations.filter(status="pending")] == ["review_prospect"]

    # The coaching month: held from the client's wallet, paid out on the first program,
    # the platform's 20% taken.
    relation = relations.get(client=client)
    month = running_month(relation)
    assert month is not None and month.price == Decimal("20000.00")
    assert month.commission == Decimal("4000.00") and month.trainer_share == Decimal("16000.00")
    assert CoachingPeriod.objects.filter(trainer=trainer).count() == 3
    assert wallet_for(trainer, "trainer").balance == Decimal("48000.00")
    assert get_platform_revenue_wallet("SYP").balance >= Decimal("12000.00")

    # Wallet history: the agent's top-up and the coaching month.
    wallet = wallet_for(client, "client")
    assert wallet.balance == Decimal("30000.00")
    kinds = set(Transaction.objects.filter(source_wallet=wallet).values_list(
        "money_type", flat=True)) | set(Transaction.objects.filter(
            destination_wallet=wallet).values_list("money_type", flat=True))
    assert kinds == {"WALLET_MOVE", "COACHING_1TO1"}
    topup = Transaction.objects.get(destination_wallet=wallet, tx_type="topup")
    assert topup.actor_id == agent.id and topup.metadata["agent_id"] == agent.id

    # The agent, with the platform's default caps, holding the cash it took.
    profile = AgentProfile.objects.get(user=agent)
    assert profile.daily_limit == settings.AGENT_DEFAULT_DAILY_LIMIT > 0
    assert profile.is_listed is False
    assert agents.receivable_of(profile, "SYP") == Decimal("125000.00")

    # A routine that is the client's own copy, trained for two weeks.
    (routine,) = Routine.objects.filter(assigned_to=client)
    assert routine.source_template.created_by == trainer and routine.created_by == trainer
    assert routine.routine_exercises.count() == 20
    sessions = WorkoutSession.objects.filter(user=client, routine=routine, status="completed")
    today = timezone.localdate()
    assert 5 <= sessions.count() <= 8
    assert all(0 < (today - timezone.localtime(s.start_time).date()).days <= 14
               for s in sessions)
    assert ExerciseSetLog.objects.filter(workout_session__in=sessions).count() > 100
    assert DayOutcome.objects.filter(user=client, via="skipped").count() == 1
    assert DayOutcome.objects.filter(user=client, via="session", complete=True).exists()
    for role in ("client2", "client3"):
        assert Routine.objects.filter(assigned_to=people[role]).count() == 1

    # A meal plan covering this week.
    plan = DietPlan.objects.get(user=client, is_active=True, is_draft=False)
    assert plan.start_date <= today <= plan.end_date
    assert 6 <= (plan.end_date - plan.start_date).days <= 7

    # A running challenge the client is in, counting the sets they logged.
    standing = ChallengeParticipation.objects.get(user=client)
    assert standing.challenge.is_active and standing.challenge.creator == trainer
    assert standing.current_value > 0 and standing.rank is not None

    counts = _review_tables()
    with django_capture_on_commit_callbacks(execute=True):
        second = _seed()
    assert _review_tables() == counts, "the second run wrote something"
    assert "nothing to change" in second, second

    # Later runs keep the time-bound parts current, and a new password rotates it.
    Subscription.objects.filter(pk=subscription.pk).update(
        end_date=timezone.now() + timedelta(days=5))
    monkeypatch.setenv("REVIEW_ACCOUNTS_PASSWORD", review_env + "-rotated")
    with django_capture_on_commit_callbacks(execute=True):
        third = _seed()
    subscription.refresh_from_db()
    assert subscription.end_date - timezone.now() > timedelta(days=80), third
    client.refresh_from_db()
    assert client.check_password(review_env + "-rotated")
    assert _review_tables() == counts


def test_seed_takes_an_address_pattern_with_the_role_in_it(
        seeded_catalogue, review_env, storage_roots, monkeypatch):
    from users.models import CustomUser

    _bootstrap("--skip-food-catalogue")
    monkeypatch.delenv("REVIEW_ACCOUNTS_EMAIL")
    _seed("--email", "yalla-{role}@review-mail.test")
    emails = set(CustomUser.objects.filter(groups__name="Store review accounts")
                 .values_list("email", flat=True))
    assert emails == {f"yalla-{role}@review-mail.test" for role in ROLES}
    listed = _seed("--list")
    assert all(f"yalla-{role}@review-mail.test" in listed for role in ROLES)


# ======================================================================== the scans
def _first_party_sources():
    skip = {"tests", "_excluded", "mobile", "flutter", "yalla_gym", ".venv", "migrations",
            ".claude", "node_modules", ".git", "__pycache__"}
    for path in ROOT.rglob("*.py"):
        rel = path.relative_to(ROOT)
        if skip & set(rel.parts) or "site-packages" in str(rel):
            continue
        yield rel, path.read_text(encoding="utf-8", errors="ignore")


def test_no_password_is_written_in_the_source():
    """The review accounts' password comes from the environment. The app's dev seed
    hard-coded one shared password for every account; nothing server-side may carry a
    password as a literal — not the dev one, and not as a `password=` keyword, a
    `set_password` argument or a `create_user` positional."""
    import ast

    offenders = []
    for rel, text in _first_party_sources():
        if "Yalla!Gym2026" in text:
            offenders.append(f"{rel}: the dev seed's password")
        for node in ast.walk(ast.parse(text, filename=str(rel))):
            if not isinstance(node, ast.Call):
                continue
            name = getattr(node.func, "attr", None) or getattr(node.func, "id", None)
            literal = [k.value for k in node.keywords if k.arg == "password"]
            if name == "set_password" and node.args:
                literal.append(node.args[0])
            if name in ("create_user", "create_superuser", "create_trainer") \
                    and len(node.args) >= 3:
                literal.append(node.args[2])
            if any(isinstance(v, ast.Constant) and isinstance(v.value, str) and v.value
                   for v in literal):
                offenders.append(f"{rel}:{node.lineno}")
    assert offenders == []

    from users.management.commands import seed_review_accounts as seed

    # And the one password the seed uses is read from the environment, by that name.
    source = (ROOT / "users/management/commands/seed_review_accounts.py").read_text()
    assert "os.environ.get(PASSWORD_ENV" in source
    assert seed.PASSWORD_ENV == "REVIEW_ACCOUNTS_PASSWORD"


def test_nothing_but_the_seed_reads_the_review_group():
    """Rule zero: behaviour depends on the build and on nothing else, so a review
    account must be served what any account is. The group marks the accounts so they
    can be found and removed; no other source file may name it."""
    from users.management.commands.seed_review_accounts import REVIEW_GROUP

    allowed = "users/management/commands/seed_review_accounts.py"
    readers = sorted(str(rel) for rel, text in _first_party_sources()
                     if (REVIEW_GROUP in text or "REVIEW_GROUP" in text)
                     and str(rel) != allowed)
    assert readers == []
