"""What the adversarial review of B-066..B-086 found (M1..M9, 2026-09-24).

Two reviewers read the merged fixes (4a6da70) — progress + AI, and community — and one
verifier reproduced each finding; every test here failed there for the reason its name
gives. M3 was refuted.
"""
import json
import threading
import uuid
from datetime import time, timedelta

import pytest
from django.utils import timezone

from tests.test_home import _log_sets, _program

JSON = "application/json"


@pytest.fixture(autouse=True)
def _cold_caches():
    from django.core.cache import cache

    from training_platform.cache import private_cache

    private_cache().clear()
    cache.clear()
    yield
    private_cache().clear()
    cache.clear()


def _running_challenge(owner, tag, **kw):
    from social.models import Challenge

    now = timezone.now()
    fields = dict(
        title=f"rv {tag}", description="rv challenge", challenge_type="workout",
        creator=owner, start_date=now - timedelta(days=2), end_date=now + timedelta(days=5),
        status="active", target_value=100_000.0, unit="kg")
    fields.update(kw)
    return Challenge.objects.create(**fields)


# ------------------------------------------------------------------------------ M1
@pytest.mark.django_db
def test_m1_sets_in_an_unfinished_session_still_count_as_a_trained_day(
        make_user, api, django_capture_on_commit_callbacks):
    """All of today's sets logged inside a session that is never finished (the nightly
    sweep abandons it). The day was trained; the streak must not read it as a rest day."""
    from achievements.engine import AchievementEngine
    from routine.models import ExerciseSetLog, UserExerciseProgress, WorkoutSession
    from routine.tasks import abandon_forgotten_sessions

    trainer, client, routine, rows = _program(make_user, "rvm1", sets=3)
    today = timezone.localdate()
    progress, _ = UserExerciseProgress.objects.get_or_create(
        user=client, exercise=rows[0].exercise, date=today, defaults={"target_sets": 3})
    session = WorkoutSession.objects.create(
        user=client, routine=routine, day=1, status="active",
        start_time=timezone.now() - timedelta(hours=13))
    with django_capture_on_commit_callbacks(execute=True):
        for number in (1, 2, 3):
            ExerciseSetLog.objects.create(user_exercise_progress=progress,
                                          workout_session=session, set_number=number,
                                          weight=40, reps=10, date=today)
        abandon_forgotten_sessions()
    session.refresh_from_db()
    assert session.status == "abandoned"
    assert AchievementEngine._calculate_workout_streak(client) == 1, \
        "a day with every set logged is a trained day"
    assert AchievementEngine._get_metric_value(client, "workout_count", {}) >= 1


# ------------------------------------------------------------------------------ M2
@pytest.mark.django_db(transaction=True)
def test_m2_two_foods_ticked_together_log_the_meal_once(make_user, monkeypatch):
    """Both last foods of a lunch committed; the two post-commit callbacks run at the
    same time. Only one `meal_completed` row may be written for the meal."""
    from django.db import connection

    import analytics.signals as signals
    from analytics.models import UserActivity
    from diet.models import DietPlan, FoodItem, Meal, MealComponent

    client = make_user("rvm2")
    today = timezone.localdate()
    plan = DietPlan.objects.create(user=client, goal="Maintain", daily_calories=2200,
                                   target_protein=150, target_carbs=220, target_fat=70,
                                   start_date=today, end_date=today, duration_weeks=1)
    meal = Meal.objects.create(diet_plan=plan, date=today, meal_type="Lunch",
                               name="Lunch", scheduled_time=time(13, 0))
    food = FoodItem.objects.create(name="RVM2 food", name_en="RVM2 food", calories=100,
                                   protein=10, carbs=10, fat=1, serving_size="100g",
                                   serving_size_grams=100)
    other = FoodItem.objects.create(name="RVM2 other", name_en="RVM2 other", calories=100,
                                    protein=10, carbs=10, fat=1, serving_size="100g",
                                    serving_size_grams=100)
    a = MealComponent.objects.create(meal=meal, food=food, quantity=100)
    b = MealComponent.objects.create(meal=meal, food=other, quantity=100)
    # Both committed as eaten (A's and B's UPDATEs have landed).
    MealComponent.objects.filter(pk__in=[a.pk, b.pk]).update(
        is_completed=True, completed_at=timezone.now())

    barrier = threading.Barrier(2, timeout=5)
    real = signals.record_activity

    def paused(*args, **kwargs):
        try:
            barrier.wait()          # both callbacks have passed their check
        except threading.BrokenBarrierError:
            pass                    # serialised by a lock: the other one is waiting
        return real(*args, **kwargs)

    monkeypatch.setattr(signals, "record_activity", paused)

    def tick(pk):
        try:
            instance = MealComponent.objects.select_related("meal__diet_plan").get(pk=pk)
            signals.on_meal_completed(sender=MealComponent, instance=instance)
        finally:
            connection.close()

    threads = [threading.Thread(target=tick, args=(pk,)) for pk in (a.pk, b.pk)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(20)
    assert UserActivity.objects.filter(user=client,
                                       activity_type="meal_completed").count() == 1


# ------------------------------------------------------------------------------ M4
@pytest.mark.django_db(transaction=True)
def test_m4_one_offline_sync_recounts_the_challenge_once(make_user, api, monkeypatch):
    """A phone syncing a batch of sets: the challenge is recounted and re-ranked once per
    request, not once per set."""
    import social.standing as standing

    trainer, client, routine, rows = _program(make_user, "rvm4", sets=10)
    challenge = _running_challenge(trainer, "rvm4", metric="sets", target_value=500.0)
    others = [make_user(f"rvm4_o{i}") for i in range(3)]
    for person in [client] + others:
        assert api(person).post(f"/api/social/challenges/{challenge.pk}/join/", {},
                                content_type=JSON).status_code == 201

    calls = []
    real = standing.write_places

    def counting(ch):
        calls.append(ch.pk)
        return real(ch)

    monkeypatch.setattr(standing, "write_places", counting)
    batch = {"sets": [{"client_uuid": str(uuid.uuid4()), "exercise_id": rows[0].exercise.pk,
                       "set_number": n, "reps": 10, "weight": 40} for n in range(1, 9)]}
    resp = api(client).post("/api/routine/set-logs/sync/", json.dumps(batch),
                            content_type=JSON)
    assert resp.status_code == 200, resp.content
    assert resp.json()["created"] == 8
    assert len(calls) <= 1, f"re-ranked {len(calls)} times for one sync of 8 sets"


# ------------------------------------------------------------------------------ M5
@pytest.mark.django_db
def test_m5_a_rival_passing_me_moves_my_rank_on_the_challenge_list(
        make_user, api, django_capture_on_commit_callbacks):
    """B leads; B's list is cached; A logs sets and passes B. B's `user_progress.rank`
    must agree with B's own `me` row on the leaderboard (B-086: read it from any)."""
    trainer, a, routine, rows = _program(make_user, "rvm5", sets=10)
    b = make_user("rvm5_b")
    routine.assigned_to.add(b)
    challenge = _running_challenge(trainer, "rvm5", metric="sets", target_value=500.0)
    for person in (b, a):
        assert api(person).post(f"/api/social/challenges/{challenge.pk}/join/", {},
                                content_type=JSON).status_code == 201
    with django_capture_on_commit_callbacks(execute=True):
        _log_sets(b, api, rows[0], 1)

    def listed_rank():
        body = api(b).get("/api/social/challenges/").json()
        rows_ = body["results"] if isinstance(body, dict) else body
        return [r for r in rows_ if r["id"] == challenge.pk][0]["user_progress"]["rank"]

    assert listed_rank() == 1
    with django_capture_on_commit_callbacks(execute=True):
        _log_sets(a, api, rows[0], 3)
    board_me = api(b).get(
        f"/api/social/challenges/{challenge.pk}/leaderboard/").json()["me"]["rank"]
    assert board_me == 2
    assert listed_rank() == board_me


# ------------------------------------------------------------------------------ M6
@pytest.mark.django_db
def test_m6_raising_the_target_reopens_a_participant_below_it(
        make_user, api, django_capture_on_commit_callbacks):
    from social.models import ChallengeParticipation

    trainer, client, routine, rows = _program(make_user, "rvm6", sets=10)
    challenge = _running_challenge(trainer, "rvm6", metric="sets", target_value=3.0)
    assert api(client).post(f"/api/social/challenges/{challenge.pk}/join/", {},
                            content_type=JSON).status_code == 201
    with django_capture_on_commit_callbacks(execute=True):
        _log_sets(client, api, rows[0], 3)
    row = ChallengeParticipation.objects.get(user=client, challenge=challenge)
    assert (row.current_value, row.status) == (3.0, "completed")

    with django_capture_on_commit_callbacks(execute=True):
        resp = api(trainer).patch(f"/api/social/challenges/{challenge.pk}/",
                                  {"target_value": 10.0}, content_type=JSON)
    assert resp.status_code == 200, resp.content
    row.refresh_from_db()
    assert row.status == "active", "3 of 10 is not a completed challenge"
    assert row.progress_percentage == pytest.approx(30.0)


# ------------------------------------------------------------------------------ M7
@pytest.mark.django_db
def test_m7_a_secret_badge_is_not_shown_to_others_through_a_post(make_user, api):
    from achievements.models import Achievement, UserAchievement

    author, stranger = make_user("rvm7_a"), make_user("rvm7_s")
    secret = Achievement.objects.create(
        key="rvm7_secret", name="Secret RV", description="hidden",
        category="workout", criteria={"type": "post_count", "target": 10_000},
        points=50, is_secret=True)
    UserAchievement.objects.create(user=author, achievement=secret)
    # The stranger cannot see it in the catalogue.
    catalogue = api(stranger).get("/api/achievements/").json()
    print("CATALOGUE", str(catalogue)[:300])
    catalogue = catalogue.get("results", catalogue.get("achievements", [])) \
        if isinstance(catalogue, dict) else catalogue
    assert secret.pk not in [row["id"] for row in catalogue]

    made = api(author).post("/api/social/posts/",
                            {"content": "look", "share_achievement": secret.pk},
                            content_type=JSON)
    if made.status_code == 400:
        return  # refused: the rule holds
    assert made.status_code == 201, made.content
    listed = api(stranger).get("/api/social/posts/").json()["results"]
    share = [p["share"] for p in listed if p["id"] == made.json()["id"]][0]
    assert not (share and share.get("achievement", {}).get("id") == secret.pk), \
        "a secret badge reached someone who has not earned it"


# ------------------------------------------------------------------------------ M8
@pytest.mark.django_db
def test_m8_a_patch_that_sets_a_share_sets_the_post_type(make_user, api):
    """Docstring: Write (create or PATCH) ... with no post_type it becomes workout."""
    from routine.models import WorkoutSession

    trainer, client, routine, rows = _program(make_user, "rvm8")
    began = timezone.now() - timedelta(minutes=40)
    session = WorkoutSession.objects.create(user=client, routine=routine, day=1,
                                            status="completed", start_time=began,
                                            end_time=began + timedelta(minutes=40))
    made = api(client).post("/api/social/posts/", {"content": "plain"},
                            content_type=JSON)
    assert made.status_code == 201, made.content
    assert made.json()["post_type"] == "text"
    patched = api(client).patch(f"/api/social/posts/{made.json()['id']}/",
                                {"share_workout_session": session.pk}, content_type=JSON)
    assert patched.status_code == 200, patched.content
    assert patched.json()["share"]["kind"] == "workout"
    assert patched.json()["post_type"] == "workout"


# ------------------------------------------------------------------------------ M9
@pytest.mark.django_db
def test_m9_a_unicode_digit_is_an_invalid_user_id_not_a_500(make_user, api):
    me = make_user("rvm9")
    client = api(me)
    client.raise_request_exception = False
    for route in ("follow_user", "unfollow_user"):
        for raw in ("²", "①"):
            resp = client.post(f"/api/social/follows/{route}/", {"user_id": raw},
                               content_type=JSON)
            assert resp.status_code == 400, (route, raw, resp.status_code)
            assert resp.json()["code"] == "invalid_user_id"
