"""The adversarial review of the merged wave-1 social fixes (B-161, B-162): what it
confirmed, each pinned here.

* R-8 — a `streak_days` window ends PARTWAY through its last day (the app creates a
  challenge at the hour it is made, a close ends it at the hour of the close). The run
  standing at the end instant (7 of 7, completed) was taken to 0 at the first hourly
  recount after the following midnight, because that last day — never over while the
  window ran — was read as a day without a set, and the winner settled on the 0.
* R-11 / R-3 — `exercise` on an `exercise_reps` challenge accepted any exercise the
  CREATOR may read, whoever the challenge is for. A trainer's own private exercise in
  a community challenge (nobody but their clients can log a set of it, so nobody else
  can move or win it), or a client publishing their coach's private exercise to every
  stranger (its id and name served in the list and detail, though the exercise route
  answers that stranger 404). A private exercise is now accepted only in its author's
  `my_clients` challenge — judged on the challenge as it will be, so a PATCH of
  `audience` back to `community` is refused as well (`exercise_not_found`).
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


def _exercise(name, author=None):
    from routine.models import Exercise

    return Exercise.objects.create(name=name, target_muscle="Lats", created_by=author)


# ----------------------------------------------------------------------------- R-8
def test_r8_a_streak_that_ended_partway_through_its_last_day_keeps_its_run(make_user):
    """The window runs from eight days ago to one day ago at this hour, so it ended
    partway through yesterday. Trained every day from its first through the day before
    yesterday, nothing yesterday: at the end instant the run was 7 — the target, so
    completed. The hourly recount, today, kept it at 7; someone whose last set was two
    days before the end still reads 0."""
    from social.challenge_progress import recount, recount_streaks
    from social.models import Challenge, ChallengeParticipation

    trainer = make_user("w1rs8_t", user_type="trainer", trainer_is_verified=True)
    steady = make_user("w1rs8_steady")
    lapsed = make_user("w1rs8_lapsed")
    squat = _exercise("w1rs8 squat")
    now = timezone.now()
    start, end = now - timedelta(days=8), now - timedelta(days=1)
    first, last = timezone.localdate(start), timezone.localdate(end)
    assert last < timezone.localdate(now), "the window's last day is over"
    challenge = Challenge.objects.create(
        creator=trainer, title="w1rs8 streak", description="days in a row",
        challenge_type="habit", metric=Challenge.METRIC_STREAK_DAYS,
        start_date=start, end_date=end, target_value=7, unit="days")

    day = first
    while day < last:                      # the first day .. the day before the last
        _sets(steady, squat, day, 1, reps=5, weight=40.0)
        if day < last - timedelta(days=1):
            _sets(lapsed, squat, day, 1, reps=5, weight=40.0)
        day += timedelta(days=1)
    assert (last - first).days == 7
    # As it stood at the end instant: the run of 7, the target reached.
    kept = ChallengeParticipation.objects.create(
        user=steady, challenge=challenge, current_value=7.0, progress_percentage=100.0,
        status="completed", completed_at=end)
    gone = ChallengeParticipation.objects.create(
        user=lapsed, challenge=challenge, current_value=6.0)
    assert challenge.counting_open(), "still counting: within LATE_HOURS of the end"

    recount_streaks()                      # the hourly phase task's call
    kept.refresh_from_db()
    gone.refresh_from_db()
    assert (kept.current_value, kept.status) == (7.0, "completed")
    assert kept.completed_at == end
    # The day before the last is a day like any other: missing it breaks the run.
    assert gone.current_value == 0.0

    # A direct recount reads the same, and a set on the last day still extends it.
    recount(challenge)
    kept.refresh_from_db()
    assert kept.current_value == 7.0
    _sets(steady, squat, last, 1, reps=5, weight=40.0)
    recount(challenge)
    kept.refresh_from_db()
    assert (kept.current_value, kept.status) == (8.0, "completed")
