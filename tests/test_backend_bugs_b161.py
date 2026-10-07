"""BACKEND_BUGS B-161: the CreateChallenge artboard's «تكرارات تمرين» (reps of ONE
exercise, with an exercise field) and «أيام متتالية» (a streak) had no server metric.

The owner's decision (2026-09-28): two new metrics, additive —

* `exercise_reps` — the reps of ONE exercise: a nullable `exercise` on the challenge,
  required for this metric (`exercise_required`); review threshold 500 reps a day;
* `streak_days` — the CURRENT run of consecutive local days with a credible set; any
  day without one breaks it (today, not over yet, does not break it until it ends).

«عدد التمارين» is the existing `workouts` metric (workouts finished).
"""
from datetime import timedelta

import pytest
from django.utils import timezone

from tests.test_owner_decisions_0924_challenges import _sets

pytestmark = pytest.mark.django_db

JSON = "application/json"


@pytest.fixture(autouse=True)
def a_cold_cache():
    from django.core.cache import cache

    cache.clear()
    yield
    cache.clear()


def _global_exercise(name):
    from routine.models import Exercise

    return Exercise.objects.create(name=name, target_muscle="Lats")


def _create(api, creator, **body):
    now = timezone.now()
    payload = {"title": "b161 challenge", "description": "reps of one exercise",
               "challenge_type": "strength",
               "start_date": (now - timedelta(days=6)).isoformat(),
               "end_date": (now + timedelta(days=3)).isoformat(),
               "target_value": 100, "unit": "reps"}
    payload.update(body)
    return api(creator).post("/api/social/challenges/", payload, content_type=JSON)


def _join(api, person, challenge_id):
    joined = api(person).post(f"/api/social/challenges/{challenge_id}/join/", {},
                              content_type=JSON)
    assert joined.status_code == 201, joined.content


def _value(challenge_id, person):
    from social.models import ChallengeParticipation

    row = ChallengeParticipation.objects.get(challenge_id=challenge_id, user=person)
    return row.current_value, row.review_state


def test_b161_exercise_reps_and_streak_are_counted(make_user, api):
    trainer = make_user("b161_t", user_type="trainer", trainer_is_verified=True)
    client = make_user("b161_c")
    pull_up = _global_exercise("b161 pull-up")
    squat = _global_exercise("b161 squat")
    today = timezone.localdate()

    made = _create(api, trainer, metric="exercise_reps", exercise=pull_up.pk)
    assert made.status_code == 201, made.content
    body = made.json()
    assert (body["metric"], body["exercise"], body["exercise_name"]) == \
        ("exercise_reps", pull_up.pk, "b161 pull-up")
    assert body["auto_counted"] is True and body["ranked"] is True

    # 3 x 10 pull-ups and 2 x 10 squats inside the window: only the pull-ups count.
    _sets(client, pull_up, today - timedelta(days=1), 3, reps=10, weight=None)
    _sets(client, squat, today - timedelta(days=1), 2, reps=10, weight=60.0)
    _join(api, client, body["id"])
    assert _value(body["id"], client) == (30.0, "clear")
    detail = api(client).get(f"/api/social/challenges/{body['id']}/").json()
    assert detail["user_progress"]["current_value"] == 30.0
    assert detail["exercise_name"] == "b161 pull-up"

    # A streak: sets on three consecutive days, today included.
    streak = _create(api, trainer, metric="streak_days", title="b161 streak")
    assert streak.status_code == 201, streak.content
    assert streak.json()["exercise"] is None
    _sets(client, squat, today - timedelta(days=2), 1, reps=5, weight=40.0)
    _sets(client, squat, today, 1, reps=5, weight=40.0)
    _join(api, client, streak.json()["id"])
    assert _value(streak.json()["id"], client)[0] == 3.0


def test_b161_exercise_reps_needs_an_exercise_it_may_use(make_user, api):
    trainer = make_user("b161r_t", user_type="trainer", trainer_is_verified=True)
    other = make_user("b161r_o", user_type="trainer", trainer_is_verified=True)

    missing = _create(api, trainer, metric="exercise_reps")
    assert missing.status_code == 400, missing.content
    assert missing.json()["field_errors"]["exercise"][0]["code"] == "exercise_required"

    # Another trainer's private exercise is answered like one that does not exist.
    from routine.models import Exercise

    private = Exercise.objects.create(name="b161 private", created_by=other,
                                      target_muscle="Lats")
    for exercise_id in (private.pk, 987654321):
        refused = _create(api, trainer, metric="exercise_reps", exercise=exercise_id)
        assert refused.status_code == 400, refused.content
        assert refused.json()["field_errors"]["exercise"][0]["code"] == \
            "exercise_not_found"

    # A metric that is not `exercise_reps` stores no exercise.
    sets = _create(api, trainer, metric="sets",
                   exercise=_global_exercise("b161 ignored").pk)
    assert sets.status_code == 201, sets.content
    assert sets.json()["exercise"] is None

    # Switching a running challenge to `exercise_reps` without naming one: refused.
    switched = api(trainer).patch(f"/api/social/challenges/{sets.json()['id']}/",
                                  {"metric": "exercise_reps"}, content_type=JSON)
    assert switched.status_code == 400, switched.content
    assert switched.json()["field_errors"]["exercise"][0]["code"] == "exercise_required"


def test_b161_a_day_over_500_reps_is_flagged_for_review(make_user, api):
    trainer = make_user("b161f_t", user_type="trainer", trainer_is_verified=True)
    cheat = make_user("b161f_c")
    push_up = _global_exercise("b161 push-up")
    made = _create(api, trainer, metric="exercise_reps", exercise=push_up.pk,
                   target_value=5000)
    assert made.status_code == 201, made.content

    # 17 sets of 30 on one day: 510 reps, above the owner's 500.
    _sets(cheat, push_up, timezone.localdate() - timedelta(days=1), 17, reps=30,
          weight=None)
    _join(api, cheat, made.json()["id"])
    assert _value(made.json()["id"], cheat) == (510.0, "flagged")


def test_b161_a_missed_day_breaks_the_streak_and_the_hourly_run_notices(make_user, api):
    from social.models import ChallengeParticipation
    from social.tasks import advance_challenge_phases

    trainer = make_user("b161s_t", user_type="trainer", trainer_is_verified=True)
    gap = make_user("b161s_gap")
    lapsed = make_user("b161s_lapsed")
    squat = _global_exercise("b161 squat 2")
    today = timezone.localdate()
    made = _create(api, trainer, metric="streak_days", title="b161 streak 2")
    assert made.status_code == 201, made.content
    challenge_id = made.json()["id"]

    # -4, -3, (no -2), -1, and today not trained yet: the run is yesterday alone —
    # today is not over, so it breaks nothing.
    for offset in (4, 3, 1):
        _sets(gap, squat, today - timedelta(days=offset), 1, reps=5, weight=40.0)
    _join(api, gap, challenge_id)
    assert _value(challenge_id, gap)[0] == 1.0

    # Trained -4, -3, -2 and nothing since: yesterday is over, so the run is 0. The
    # number stored while the run was alive is recounted by the hourly phase task,
    # since no set of theirs will arrive to move it.
    for offset in (4, 3, 2):
        _sets(lapsed, squat, today - timedelta(days=offset), 1, reps=5, weight=40.0)
    ChallengeParticipation.objects.create(user=lapsed, challenge_id=challenge_id,
                                          current_value=3.0)
    advance_challenge_phases()
    assert _value(challenge_id, lapsed)[0] == 0.0
    assert _value(challenge_id, gap)[0] == 1.0


def test_b161_the_exercise_name_is_read_with_the_row_and_its_loss_locks_nothing(
        make_user, api):
    """`exercise_name` costs no query per row of the list, and an exercise deleted
    later (SET_NULL) leaves the challenge editable and its standing as it stood."""
    from cachalot.api import cachalot_disabled
    from django.core.cache import cache
    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    trainer = make_user("b161n_t", user_type="trainer", trainer_is_verified=True)
    client = make_user("b161n_c")
    first = _global_exercise("b161 dip")
    made = _create(api, trainer, metric="exercise_reps", exercise=first.pk)
    assert made.status_code == 201, made.content
    api(client).get("/api/social/challenges/")  # warm the per-process caches

    cache.clear()
    with cachalot_disabled(), CaptureQueriesContext(connection) as one_row:
        listed = api(client).get("/api/social/challenges/")
    assert listed.status_code == 200, listed.content
    for n in range(3):
        more = _create(api, trainer, metric="exercise_reps", title=f"b161 more {n}",
                       exercise=_global_exercise(f"b161 row {n}").pk)
        assert more.status_code == 201, more.content
    cache.clear()
    with cachalot_disabled(), CaptureQueriesContext(connection) as four_rows:
        listed = api(client).get("/api/social/challenges/")
    rows = listed.json()["results"]
    assert {r["exercise_name"] for r in rows} == {
        "b161 dip", "b161 row 0", "b161 row 1", "b161 row 2"}
    assert len(four_rows) == len(one_row), "exercise_name costs a query per row"

    # The exercise goes; the challenge stays, its title still editable, nothing counted.
    _sets(client, first, timezone.localdate(), 2, reps=10, weight=None)
    _join(api, client, made.json()["id"])
    assert _value(made.json()["id"], client)[0] == 20.0
    first.delete()
    renamed = api(trainer).patch(f"/api/social/challenges/{made.json()['id']}/",
                                 {"title": "b161 renamed"}, content_type=JSON)
    assert renamed.status_code == 200, renamed.content
    assert (renamed.json()["exercise"], renamed.json()["exercise_name"]) == (None, None)
    assert _value(made.json()["id"], client)[0] == 20.0
