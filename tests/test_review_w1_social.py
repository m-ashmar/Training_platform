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


# ------------------------------------------------------------------- R-11 and R-3
def _create(api, creator, **body):
    now = timezone.now()
    payload = {"title": "w1rs challenge", "description": "reps of one exercise",
               "challenge_type": "strength",
               "start_date": (now - timedelta(days=1)).isoformat(),
               "end_date": (now + timedelta(days=10)).isoformat(),
               "target_value": 100, "unit": "reps", "metric": "exercise_reps"}
    payload.update(body)
    return api(creator).post("/api/social/challenges/", payload, content_type=JSON)


def _code(response):
    assert response.status_code == 400, response.content
    return response.json()["field_errors"]["exercise"][0]["code"]


def _names_listed(api, person):
    body = api(person).get("/api/social/challenges/").json()
    rows = body["results"] if isinstance(body, dict) else body
    return {row["exercise_name"] for row in rows}


def test_r11_a_trainers_own_exercise_counts_only_in_their_clients_only_challenge(
        make_user, api):
    """Nobody but the trainer's clients can log a set of the trainer's own exercise, so
    a community challenge on it held every other participant at 0. It is refused there
    as an exercise that does not exist, on create, on a PATCH naming it, and on a PATCH
    moving the clients-only challenge that has it out to the community."""
    from social.models import Challenge

    trainer = make_user("w1rs11_t", user_type="trainer", trainer_is_verified=True)
    stranger = make_user("w1rs11_s")
    secret = _exercise("w1rs11 secret move", author=trainer)
    shared = _exercise("w1rs11 catalogue move")

    assert _code(_create(api, trainer, exercise=secret.pk)) == "exercise_not_found"
    assert _code(_create(api, trainer, exercise=secret.pk, audience="community")) == \
        "exercise_not_found"
    assert not Challenge.objects.filter(exercise=secret).exists()
    assert "w1rs11 secret move" not in _names_listed(api, stranger)

    # For the trainer's clients only: the people who can log it.
    mine = _create(api, trainer, exercise=secret.pk, audience="my_clients")
    assert mine.status_code == 201, mine.content
    assert (mine.json()["exercise"], mine.json()["audience"]) == (secret.pk, "my_clients")
    url = f"/api/social/challenges/{mine.json()['id']}/"

    # Moving it out to the community, with the exercise it has: refused, nothing written.
    out = api(trainer).patch(url, {"audience": "community"}, content_type=JSON)
    assert _code(out) == "exercise_not_found"
    assert Challenge.objects.get(pk=mine.json()["id"]).audience == "my_clients"
    assert "w1rs11 secret move" not in _names_listed(api, stranger)

    # With a catalogue exercise in the same edit it may go; then the private one may
    # not come back while it is a community challenge.
    moved = api(trainer).patch(url, {"audience": "community", "exercise": shared.pk},
                               content_type=JSON)
    assert moved.status_code == 200, moved.content
    assert (moved.json()["audience"], moved.json()["exercise"]) == ("community", shared.pk)
    back = api(trainer).patch(url, {"exercise": secret.pk}, content_type=JSON)
    assert _code(back) == "exercise_not_found"
    assert Challenge.objects.get(pk=mine.json()["id"]).exercise_id == shared.pk

    # A community challenge on a catalogue exercise is as it was.
    plain = _create(api, trainer, exercise=shared.pk)
    assert plain.status_code == 201, plain.content


def test_r3_a_client_cannot_publish_their_coachs_private_exercise(make_user, api):
    """A client may read their coach's private exercises, and could name one in a
    community challenge: every stranger was then served its id and name, while the
    exercise route answers that stranger 404. A client cannot open a clients-only
    challenge, so a private exercise is never theirs to name."""
    from social.models import Challenge
    from users.models import TrainerClientRelation

    coach = make_user("w1rs3_coach", user_type="trainer", trainer_is_verified=True)
    client = make_user("w1rs3_client")
    stranger = make_user("w1rs3_stranger")
    TrainerClientRelation.objects.create(
        trainer=coach, client=client, status="approved", initiated_by="client",
        responded_at=timezone.now() - timedelta(days=5))
    secret = _exercise("w1rs3 secret tempo squat", author=coach)
    assert api(stranger).get(f"/api/routine/exercises/{secret.pk}/").status_code == 404

    refused = _create(api, client, exercise=secret.pk)
    assert _code(refused) == "exercise_not_found"
    assert not Challenge.objects.filter(exercise=secret).exists()
    assert "w1rs3 secret tempo squat" not in _names_listed(api, stranger)

    # The catalogue is the client's to use, as before.
    shared = _exercise("w1rs3 catalogue squat")
    made = _create(api, client, exercise=shared.pk)
    assert made.status_code == 201, made.content
    detail = api(stranger).get(f"/api/social/challenges/{made.json()['id']}/").json()
    assert detail["exercise_name"] == "w1rs3 catalogue squat"
