"""Owner's decision F20 (2026-09-24): challenges must not be scammable.

Every test here TRIES TO CHEAT and proves it does not pay: impossible sets, set
floods, late syncs, empty or one-minute workouts, two-set "training days", a flagged
or disqualified leader, a client crowning themselves in a challenge of their own, a
winner checked before the review window closed. And a legitimate week still counts
exactly.

`social/challenge_progress.py` (what counts), `social/standing.py` (who holds a
place), `social/review.py` (the operator's decisions), `social/tasks.py
settle_winner` (the winner after the review window).
"""
from datetime import datetime, time, timedelta

import pytest
from django.utils import timezone

from tests.test_fx_social import _badge
from tests.test_home import _data, _home

pytestmark = pytest.mark.django_db

# Every trainer below is VERIFIED (`trainer_is_verified`): since the review of F20 only
# staff or a verified trainer makes a ranked challenge, because anyone can sign up as a
# trainer. An unverified trainer is tested on its own.

JSON = "application/json"


# ------------------------------------------------------------------ helpers
def _challenge(creator, tag, metric="sets", **kw):
    from social.models import Challenge

    now = timezone.now()
    fields = dict(title=f"f20 {tag}", description="anti-cheat", challenge_type="workout",
                  creator=creator, start_date=now - timedelta(days=8),
                  end_date=now + timedelta(days=1), status="active", target_value=10.0,
                  unit="sets", metric=metric)
    fields.update(kw)
    return Challenge.objects.create(**fields)


def _exercise(trainer, tag):
    from routine.models import Exercise

    return Exercise.objects.create(name=f"f20 ex {tag}", created_by=trainer,
                                   target_muscle="Triceps")


def _sets(user, exercise, day, count, *, reps=10, weight=50.0, received=None,
          session=None, first=1):
    """`count` sets on `day`, straight into the tables (the count is under test, not
    the set route). Received the evening of `day` (as someone logging in the gym), or
    at `received`."""
    from routine.models import ExerciseSetLog, UserExerciseProgress

    progress, _new = UserExerciseProgress.objects.get_or_create(
        user=user, exercise=exercise, date=day, defaults={"target_sets": 3})
    rows = ExerciseSetLog.objects.bulk_create([
        ExerciseSetLog(user_exercise_progress=progress, set_number=number, reps=reps,
                       weight=weight, date=day, workout_session=session)
        for number in range(first, first + count)])
    if received is None:
        received = min(timezone.now(),
                       timezone.make_aware(datetime.combine(day, time(20))))
    ExerciseSetLog.objects.filter(pk__in=[r.pk for r in rows]).update(created_at=received)
    return rows


def _session(user, routine, start, minutes, status="completed"):
    from routine.models import WorkoutSession

    return WorkoutSession.objects.create(user=user, routine=routine, day=1, status=status,
                                         start_time=start,
                                         end_time=start + timedelta(minutes=minutes))


def _routine(trainer, tag):
    from routine.models import Routine

    return Routine.objects.create(name=f"f20 {tag}", created_by=trainer, days=1)


def _enter(challenge, user, value=0.0, **kw):
    from social.models import ChallengeParticipation

    return ChallengeParticipation.objects.create(user=user, challenge=challenge,
                                                 current_value=value, **kw)


def _count(challenge, user):
    """Recount the challenge and read the person's number and review."""
    from social.challenge_progress import recount
    from social.models import ChallengeParticipation

    recount(challenge)
    row = ChallengeParticipation.objects.get(challenge=challenge, user=user)
    return row.current_value, row.review_state


def _today(offset=0):
    return timezone.localdate() - timedelta(days=offset)


def _end_review_window(challenge, extra=timedelta(minutes=5)):
    from social.models import Challenge

    Challenge.objects.filter(pk=challenge.pk).update(
        end_date=timezone.now() - timedelta(hours=Challenge.REVIEW_HOURS) - extra)


def _holds(person, badge):
    from achievements.models import UserAchievement

    return UserAchievement.objects.filter(user=person, achievement=badge).exists()


# ------------------------------------------------------------ what counts
def test_an_impossible_set_counts_nothing(make_user):
    trainer = make_user("f20_imp_t", user_type="trainer", trainer_is_verified=True)
    cheat = make_user("f20_imp_c")
    ex = _exercise(trainer, "imp")
    volume = _challenge(trainer, "imp_v", metric="volume_kg", target_value=100_000)
    sets = _challenge(trainer, "imp_s", metric="sets")
    for challenge in (volume, sets):
        _enter(challenge, cheat)

    _sets(cheat, ex, _today(1), 1, reps=1000, weight=1000.0)
    assert _count(volume, cheat) == (0.0, "clear"), "1000 kg x 1000 reps counted"
    assert _count(sets, cheat)[0] == 0.0

    # Just over each limit is still nothing; a bodyweight set is a set with no volume.
    _sets(cheat, ex, _today(1), 1, reps=31, weight=10.0, first=2)
    _sets(cheat, ex, _today(1), 1, reps=10, weight=400.5, first=3)
    _sets(cheat, ex, _today(1), 1, reps=0, weight=10.0, first=4)
    _sets(cheat, ex, _today(1), 1, reps=10, weight=None, first=5)
    _sets(cheat, ex, _today(1), 1, reps=30, weight=400.0, first=6)
    assert _count(volume, cheat)[0] == 12_000.0
    assert _count(sets, cheat)[0] == 2.0


def test_two_hundred_sets_in_a_day_count_sixty_and_flag_the_day(make_user):
    from social.challenge_progress import MAX_SETS_PER_DAY, MAX_VOLUME_KG_PER_DAY

    trainer = make_user("f20_flood_t", user_type="trainer", trainer_is_verified=True)
    cheat = make_user("f20_flood_c")
    ex = _exercise(trainer, "flood")
    sets = _challenge(trainer, "flood_s", metric="sets", target_value=500)
    volume = _challenge(trainer, "flood_v", metric="volume_kg", target_value=10**7)
    for challenge in (sets, volume):
        _enter(challenge, cheat)

    _sets(cheat, ex, _today(2), 200, reps=10, weight=20.0)
    assert _count(sets, cheat) == (float(MAX_SETS_PER_DAY), "flagged")
    # Volume: of the 60 sets that count, 60 x 200 kg.
    assert _count(volume, cheat) == (12_000.0, "flagged")

    # The volume cap: 60 sets of 30 x 400 kg is 720 t; a day counts 30 t at most.
    heavy = make_user("f20_flood_h")
    _enter(volume, heavy)
    _sets(heavy, ex, _today(3), 60, reps=30, weight=400.0)
    assert _count(volume, heavy) == (float(MAX_VOLUME_KG_PER_DAY), "flagged")


def test_a_set_synced_five_days_late_does_not_count(make_user):
    from routine.models import ExerciseSetLog
    from social.challenge_progress import LATE_HOURS

    trainer = make_user("f20_late_t", user_type="trainer", trainer_is_verified=True)
    person = make_user("f20_late_c")
    ex = _exercise(trainer, "late")
    challenge = _challenge(trainer, "late")
    _enter(challenge, person)

    day = _today(5)
    _sets(person, ex, day, 4, received=timezone.now())
    assert _count(challenge, person)[0] == 0.0, "a set received 5 days late counted"
    # The journal keeps it: only the challenge refuses to count it.
    assert ExerciseSetLog.objects.filter(user_exercise_progress__user=person).count() == 4

    # Received within `LATE_HOURS` of the day's end: it counts.
    day_end = timezone.make_aware(datetime.combine(day + timedelta(days=1), time.min))
    _sets(person, ex, _today(6), 3,
          received=day_end - timedelta(days=1) + timedelta(hours=LATE_HOURS - 1))
    assert _count(challenge, person)[0] == 3.0
    # A row older than the receipt column (null) is on time.
    rows = _sets(person, ex, _today(7), 2)
    ExerciseSetLog.objects.filter(pk__in=[r.pk for r in rows]).update(created_at=None)
    assert _count(challenge, person)[0] == 5.0


def test_empty_short_and_excess_workouts_do_not_count(make_user):
    trainer = make_user("f20_wo_t", user_type="trainer", trainer_is_verified=True)
    person = make_user("f20_wo_c")
    ex = _exercise(trainer, "wo")
    routine = _routine(trainer, "wo")
    challenge = _challenge(trainer, "wo", metric="workouts")
    _enter(challenge, person)
    base = timezone.make_aware(datetime.combine(_today(3), time(9)))

    _session(person, routine, base, 60)                       # completed, empty
    assert _count(challenge, person)[0] == 0.0, "an empty workout counted"

    for index in range(2):                                    # two 1-minute workouts
        short = _session(person, routine, base + timedelta(hours=2 + index), 1)
        _sets(person, ex, _today(3), 3, session=short, first=10 * index + 1)
    assert _count(challenge, person)[0] == 0.0, "a one-minute workout counted"

    open_one = _session(person, routine, base + timedelta(hours=5), 40, status="active")
    _sets(person, ex, _today(3), 3, session=open_one, first=31)
    assert _count(challenge, person)[0] == 0.0, "an unfinished workout counted"

    # Three real workouts on one day: two count, and the day is flagged.
    for index in range(3):
        real = _session(person, routine, base + timedelta(hours=6 + index), 40)
        _sets(person, ex, _today(3), 3, session=real, first=41 + 10 * index)
    assert _count(challenge, person) == (2.0, "flagged")


def test_a_day_with_two_sets_is_not_a_training_day(make_user):
    trainer = make_user("f20_days_t", user_type="trainer", trainer_is_verified=True)
    person = make_user("f20_days_c")
    ex = _exercise(trainer, "days")
    challenge = _challenge(trainer, "days", metric="training_days")
    _enter(challenge, person)

    for offset in (1, 2, 3):
        _sets(person, ex, _today(offset), 2)
    assert _count(challenge, person)[0] == 0.0
    _sets(person, ex, _today(1), 1, first=3)
    assert _count(challenge, person)[0] == 1.0


def test_a_legit_week_still_counts_exactly(make_user):
    trainer = make_user("f20_week_t", user_type="trainer", trainer_is_verified=True)
    person = make_user("f20_week_c")
    ex = _exercise(trainer, "week")
    routine = _routine(trainer, "week")
    challenges = {metric: _challenge(trainer, f"week_{metric}", metric=metric,
                                     target_value=1_000_000)
                  for metric in ("sets", "volume_kg", "training_days", "workouts")}
    for challenge in challenges.values():
        _enter(challenge, person)

    for offset in range(1, 6):
        day = _today(offset)
        session = _session(person, routine,
                           timezone.make_aware(datetime.combine(day, time(18))), 45)
        _sets(person, ex, day, 4, reps=10, weight=50.0, session=session)

    assert {metric: _count(challenge, person) for metric, challenge in challenges.items()} \
        == {"sets": (20.0, "clear"), "volume_kg": (10_000.0, "clear"),
            "training_days": (5.0, "clear"), "workouts": (5.0, "clear")}


# ------------------------------------------------------------ review and winner
def test_a_flagged_leader_keeps_the_place_but_wins_nothing_until_cleared(make_user, api):
    from social.models import Challenge, ChallengeParticipation
    from social.review import apply_review
    from social.tasks import advance_challenge_phases

    badge = _badge("f20_flag_win", points=50, criteria={"type": "challenge_wins",
                                                        "target": 1})
    trainer = make_user("f20_flag_t", user_type="trainer", trainer_is_verified=True)
    cheat = make_user("f20_flag_c")
    honest = make_user("f20_flag_h")
    ex = _exercise(trainer, "flag")
    challenge = _challenge(trainer, "flag", target_value=10)
    _enter(challenge, cheat)
    _enter(challenge, honest)
    _sets(cheat, ex, _today(1), 50)                          # one day over 45
    for offset in (1, 2, 3, 4):
        _sets(honest, ex, _today(offset), 3)
    assert _count(challenge, cheat) == (50.0, "flagged")
    assert _count(challenge, honest) == (12.0, "clear")

    board = api(cheat).get(f"/api/social/challenges/{challenge.pk}/leaderboard/").json()
    assert [row["user"]["id"] for row in board["leaderboard"]] == [cheat.pk, honest.pk]
    assert board["me"]["rank"] == 1
    assert (board["me"]["review_state"], board["me"]["review_reason"]) == \
        ("flagged", "day_over_threshold")
    assert "review_state" not in board["leaderboard"][0], "someone else's review served"
    other = api(honest).get(f"/api/social/challenges/{challenge.pk}/").json()
    assert other["user_progress"]["review_state"] == "clear"

    _end_review_window(challenge)
    advance_challenge_phases()
    assert not _holds(cheat, badge) and not _holds(honest, badge)
    assert Challenge.objects.get(pk=challenge.pk).winner_checked_at is None
    assert api(honest).get(f"/api/social/challenges/{challenge.pk}/").json()[
        "winner_final"] is False

    # An operator looks and clears it: the winner is settled at once.
    apply_review(ChallengeParticipation.objects.filter(challenge=challenge, user=cheat),
                 "clear")
    assert _holds(cheat, badge)
    row = ChallengeParticipation.objects.get(challenge=challenge, user=cheat)
    assert (row.review_state, row.review_reason, row.rank) == ("clear", "operator", 1)
    assert api(honest).get(f"/api/social/challenges/{challenge.pk}/").json()[
        "winner_final"] is True


def test_a_disqualified_person_vanishes_from_every_place_and_cannot_rejoin(make_user, api):
    from social.models import ChallengeParticipation
    from social.review import apply_review

    trainer = make_user("f20_dq_t", user_type="trainer", trainer_is_verified=True)
    cheat = make_user("f20_dq_c")
    second = make_user("f20_dq_2")
    third = make_user("f20_dq_3")
    ex = _exercise(trainer, "dq")
    challenge = _challenge(trainer, "dq", target_value=100, participants_count=3)
    base = timezone.now() - timedelta(days=1)
    for person, count in ((cheat, 40), (second, 9), (third, 6)):
        _enter(challenge, person)
        _sets(person, ex, _today(1), count)
    ChallengeParticipation.objects.filter(challenge=challenge).update(joined_at=base)
    _count(challenge, cheat)

    apply_review(ChallengeParticipation.objects.filter(challenge=challenge, user=cheat),
                 "disqualified")

    board = api(second).get(f"/api/social/challenges/{challenge.pk}/leaderboard/").json()
    assert [(r["rank"], r["user"]["id"]) for r in board["leaderboard"]] == \
        [(1, second.pk), (2, third.pk)]
    mine = api(cheat).get(f"/api/social/challenges/{challenge.pk}/leaderboard/").json()
    assert mine["me"]["rank"] is None
    assert (mine["me"]["review_state"], mine["me"]["review_reason"]) == \
        ("disqualified", "operator")
    detail = api(cheat).get(f"/api/social/challenges/{challenge.pk}/").json()
    assert detail["user_progress"]["rank"] is None
    assert detail["user_progress"]["review_state"] == "disqualified"
    assert api(second).get(f"/api/social/challenges/{challenge.pk}/").json()[
        "user_progress"]["rank"] == 1
    card = _data(_home(api, third), "challenge_rank")
    assert (card["rank"], card["participants"]) == (2, 2)
    assert dict(ChallengeParticipation.objects.filter(challenge=challenge)
                .values_list("user_id", "rank")) == {cheat.pk: None, second.pk: 1,
                                                     third.pk: 2}

    for route in ("join", "leave"):
        resp = api(cheat).post(f"/api/social/challenges/{challenge.pk}/{route}/", {},
                               content_type=JSON)
        assert resp.status_code == 403, (route, resp.content)
        assert resp.json()["code"] == "challenge_disqualified"
    assert ChallengeParticipation.objects.filter(challenge=challenge, user=cheat).exists()

    # Still counted, so clearing restores a true number.
    _sets(cheat, ex, _today(2), 5)
    assert _count(challenge, cheat) == (45.0, "disqualified")


def test_a_manual_challenge_of_a_client_is_not_ranked_and_awards_nothing(make_user, api):
    from social.models import Challenge, ChallengeParticipation
    from social.tasks import advance_challenge_phases

    badge = _badge("f20_manual_win", points=50, criteria={"type": "challenge_wins",
                                                          "target": 1})
    client = make_user("f20_manual_c")
    now = timezone.now()
    made = api(client).post("/api/social/challenges/", {
        "title": "my own", "description": "self-crowned", "challenge_type": "custom",
        "start_date": (now - timedelta(days=1)).isoformat(),
        "end_date": (now + timedelta(days=1)).isoformat(), "target_value": 5,
        "unit": "reps"}, content_type=JSON)
    assert made.status_code == 201, made.content
    body = made.json()
    assert (body["ranked"], body["counting"], body["review_hours"], body["winner_final"]) \
        == (False, None, Challenge.REVIEW_HOURS, False)
    challenge = Challenge.objects.get(pk=body["id"])

    assert api(client).post(f"/api/social/challenges/{challenge.pk}/join/", {},
                            content_type=JSON).status_code == 201
    logged = api(client).post(f"/api/social/challenges/{challenge.pk}/update_progress/",
                              {"current_value": 5}, content_type=JSON)
    assert logged.status_code == 200, logged.content
    assert ChallengeParticipation.objects.get(challenge=challenge).rank == 1

    _end_review_window(challenge)
    advance_challenge_phases()
    assert Challenge.objects.get(pk=challenge.pk).winner_checked_at is not None
    assert not _holds(client, badge), "a self-reported number won a prize"

    # Auto-counted but created by a client: not ranked either. Staff or a trainer: ranked.
    assert _challenge(client, "client_auto").ranked is False
    trainer = make_user("f20_manual_t", user_type="trainer", trainer_is_verified=True)
    assert _challenge(trainer, "trainer_manual", metric="manual").ranked is False
    staff = make_user("f20_manual_s", is_staff=True)
    assert _challenge(staff, "staff_auto").ranked is True
    counted = api(client).get(f"/api/social/challenges/"
                              f"{_challenge(trainer, 'trainer_auto').pk}/").json()
    assert counted["ranked"] is True
    assert counted["counting"] == {
        "max_reps_per_set": 30, "max_weight_kg_per_set": 400, "min_sets_per_day": 3,
        "max_sets_per_day": 60, "max_volume_kg_per_day": 30000,
        "min_sets_per_workout": 3, "min_workout_minutes": 10,
        "max_workouts_per_day": 2, "late_hours": 72}


def test_the_winner_is_checked_once_and_only_after_the_review_window(make_user):
    from social.models import Challenge
    from social.tasks import advance_challenge_phases, settle_winners

    badge = _badge("f20_once_win", points=50, criteria={"type": "challenge_wins",
                                                        "target": 1})
    trainer = make_user("f20_once_t", user_type="trainer", trainer_is_verified=True)
    winner = make_user("f20_once_w")
    ex = _exercise(trainer, "once")
    challenge = _challenge(trainer, "once", target_value=6)
    _enter(challenge, winner)
    _sets(winner, ex, _today(1), 6)
    assert _count(challenge, winner) == (6.0, "clear")

    for hours_ago in (0, 47):
        Challenge.objects.filter(pk=challenge.pk).update(
            end_date=timezone.now() - timedelta(hours=hours_ago, minutes=5))
        advance_challenge_phases()
        assert not _holds(winner, badge), f"awarded {hours_ago} h after the end"
        assert Challenge.objects.get(pk=challenge.pk).winner_checked_at is None

    _end_review_window(challenge)
    advance_challenge_phases()
    assert _holds(winner, badge)
    settled_at = Challenge.objects.get(pk=challenge.pk).winner_checked_at
    assert settled_at is not None

    assert settle_winners() == 0
    advance_challenge_phases()
    assert Challenge.objects.get(pk=challenge.pk).winner_checked_at == settled_at


def test_the_console_filters_by_review_and_disqualifies_or_clears(make_user):
    from django.test import Client

    from social.models import ChallengeParticipation

    operator = make_user("f20_ops", user_type="admin")
    trainer = make_user("f20_ops_t", user_type="trainer", trainer_is_verified=True)
    cheat = make_user("f20_ops_c")
    honest = make_user("f20_ops_h")
    ex = _exercise(trainer, "ops")
    challenge = _challenge(trainer, "ops", target_value=100)
    for person, count in ((cheat, 50), (honest, 9)):
        _enter(challenge, person)
        _sets(person, ex, _today(1), count)
    assert _count(challenge, cheat) == (50.0, "flagged")
    console = Client()
    console.force_login(operator)
    # The console lists participations under the Challenges group (the proxy of
    # `challenges/admin.py`).
    changelist = "/dj-admin/challenges/challengeparticipationproxy/"

    listed = console.get(changelist, {"review_state__exact": "flagged"})
    assert listed.status_code == 200
    assert "f20_ops_c" in listed.content.decode() and \
        "f20_ops_h" not in listed.content.decode()

    cheat_row = ChallengeParticipation.objects.get(challenge=challenge, user=cheat)
    done = console.post(changelist, {"action": "disqualify_from_challenge",
                                     "_selected_action": [cheat_row.pk]})
    assert done.status_code == 302, done.content[:300]
    cheat_row.refresh_from_db()
    assert (cheat_row.review_state, cheat_row.review_reason, cheat_row.rank) == \
        ("disqualified", "operator", None)
    assert ChallengeParticipation.objects.get(challenge=challenge, user=honest).rank == 1

    console.post(changelist, {"action": "clear_challenge_review",
                              "_selected_action": [cheat_row.pk]})
    cheat_row.refresh_from_db()
    assert (cheat_row.review_state, cheat_row.rank) == ("clear", 1)
    # Cleared by an operator: the same day does not flag them again.
    assert _count(challenge, cheat) == (50.0, "clear")


# ------------------------------------------------ the review of F20: the cheats it found
def test_a_finished_manual_challenge_cannot_become_counted_and_pay_its_self_report(
        make_user, api):
    """Switching a finished manual challenge to `sets` ranked its hand-typed numbers."""
    from social.models import Challenge, ChallengeParticipation
    from social.tasks import advance_challenge_phases

    badge = _badge("f20_rv_sw", points=50, criteria={"type": "challenge_wins",
                                                     "target": 1})
    trainer = make_user("f20_rv_sw_t", user_type="trainer", trainer_is_verified=True)
    friend = make_user("f20_rv_sw_f")
    challenge = _challenge(trainer, "rv_sw", metric="manual", target_value=5)
    assert api(friend).post(f"/api/social/challenges/{challenge.pk}/join/", {},
                            content_type=JSON).status_code == 201
    assert api(friend).post(f"/api/social/challenges/{challenge.pk}/update_progress/",
                            {"current_value": 5}, content_type=JSON).status_code == 200
    Challenge.objects.filter(pk=challenge.pk).update(
        end_date=timezone.now() - timedelta(hours=1))

    for change in ({"metric": "sets"}, {"target_value": 1},
                   {"start_date": (timezone.now() - timedelta(days=30)).isoformat()},
                   {"end_date": (timezone.now() + timedelta(days=5)).isoformat()}):
        refused = api(trainer).patch(f"/api/social/challenges/{challenge.pk}/", change,
                                     content_type=JSON)
        assert refused.status_code == 400, (change, refused.content)
        assert refused.json()["code"] == "challenge_over", change
    # The title of a finished challenge is still the creator's.
    assert api(trainer).patch(f"/api/social/challenges/{challenge.pk}/",
                              {"title": "renamed"}, content_type=JSON).status_code == 200
    assert Challenge.objects.get(pk=challenge.pk).metric == "manual"

    _end_review_window(challenge)
    advance_challenge_phases()
    assert ChallengeParticipation.objects.get(challenge=challenge, user=friend).rank == 1
    assert not _holds(friend, badge), "a self-reported number won a prize"


def test_a_workout_back_dated_and_filled_with_todays_sets_counts_nothing(make_user):
    trainer = make_user("f20_rv_bd_t", user_type="trainer", trainer_is_verified=True)
    cheat = make_user("f20_rv_bd_c")
    ex = _exercise(trainer, "rv_bd")
    routine = _routine(trainer, "rv_bd")
    challenge = _challenge(trainer, "rv_bd", metric="workouts", target_value=100)
    _enter(challenge, cheat)
    now = timezone.now()
    number = 1
    for back in range(1, 8):
        for k in range(2):
            session = _session(cheat, routine, now - timedelta(days=back, hours=k * 2), 30)
            _sets(cheat, ex, _today(0), 3, session=session, received=now, first=number)
            number += 3
    assert _count(challenge, cheat) == (0.0, "clear")

    # A real workout yesterday, its sets trained that day and synced that evening.
    start = timezone.make_aware(datetime.combine(_today(1), time(10)))
    real = _session(cheat, routine, start, 45)
    _sets(cheat, ex, _today(1), 3, session=real, first=number)
    assert _count(challenge, cheat) == (1.0, "clear")


def test_an_unverified_trainer_cannot_crown_a_friend(make_user, api):
    """Anyone can sign up as a trainer: a throwaway trainer account's challenge is not
    ranked, and its first place wins nothing."""
    from social.models import Challenge
    from social.tasks import advance_challenge_phases

    badge = _badge("f20_rv_tt", points=50, criteria={"type": "challenge_wins",
                                                     "target": 1})
    throwaway = make_user("f20_rv_tt_t", user_type="trainer")
    friend = make_user("f20_rv_tt_f")
    ex = _exercise(throwaway, "rv_tt")
    challenge = _challenge(throwaway, "rv_tt", target_value=3)
    assert challenge.ranked is False
    assert not Challenge.objects.filter(Challenge.ranked_q(), pk=challenge.pk).exists()
    assert api(friend).get(f"/api/social/challenges/{challenge.pk}/").json()[
        "ranked"] is False
    _enter(challenge, friend)
    _sets(friend, ex, _today(1), 3)
    assert _count(challenge, friend) == (3.0, "clear")

    _end_review_window(challenge)
    advance_challenge_phases()
    assert Challenge.objects.get(pk=challenge.pk).winner_checked_at is not None
    assert not _holds(friend, badge)

    verified = make_user("f20_rv_tt_v", user_type="trainer", trainer_is_verified=True)
    assert _challenge(verified, "rv_tt_v").ranked is True


def test_a_creator_cannot_end_a_challenge_in_the_past_and_skip_the_review(make_user, api):
    from social.models import Challenge
    from social.tasks import advance_challenge_phases

    trainer = make_user("f20_rv_ed_t", user_type="trainer", trainer_is_verified=True)
    friend = make_user("f20_rv_ed_f")
    challenge = _challenge(trainer, "rv_ed", target_value=1)
    _enter(challenge, friend, value=5.0, status="completed")
    refused = api(trainer).patch(
        f"/api/social/challenges/{challenge.pk}/",
        {"end_date": (timezone.now() - timedelta(days=3)).isoformat()}, content_type=JSON)
    assert refused.status_code == 400, refused.content
    assert refused.json()["code"] == "end_date_in_past"
    advance_challenge_phases()
    assert Challenge.objects.get(pk=challenge.pk).winner_checked_at is None

    # Moving the end later still works; closing ends it now and the review runs from now.
    later = timezone.now() + timedelta(days=3)
    assert api(trainer).patch(f"/api/social/challenges/{challenge.pk}/",
                              {"end_date": later.isoformat()},
                              content_type=JSON).status_code == 200
    assert api(trainer).patch(f"/api/social/challenges/{challenge.pk}/",
                              {"status": "completed"},
                              content_type=JSON).status_code == 200
    advance_challenge_phases()
    closed = Challenge.objects.get(pk=challenge.pk)
    assert closed.winner_checked_at is None
    assert closed.end_date <= timezone.now()


def test_a_cleared_participant_is_flagged_again_for_a_new_day_over_the_threshold(
        make_user):
    from social.models import ChallengeParticipation
    from social.review import apply_review

    trainer = make_user("f20_rv_cl_t", user_type="trainer", trainer_is_verified=True)
    cheat = make_user("f20_rv_cl_c")
    ex = _exercise(trainer, "rv_cl")
    challenge = _challenge(trainer, "rv_cl", target_value=10_000)
    _enter(challenge, cheat)
    _sets(cheat, ex, _today(5), 50)
    assert _count(challenge, cheat) == (50.0, "flagged")
    apply_review(ChallengeParticipation.objects.filter(challenge=challenge, user=cheat),
                 "clear")
    row = ChallengeParticipation.objects.get(challenge=challenge, user=cheat)
    assert (row.review_state, row.review_reason, row.review_cleared_through) == \
        ("clear", "operator", _today(5))

    # The day the operator looked at does not flag again...
    _sets(cheat, ex, _today(5), 5, first=51)
    assert _count(challenge, cheat) == (55.0, "clear")
    # ... a later day over the threshold does.
    _sets(cheat, ex, _today(2), 60)
    assert _count(challenge, cheat) == (115.0, "flagged")
    row.refresh_from_db()
    assert row.review_reason == "day_over_threshold"


def test_stuck_flagged_challenges_do_not_starve_settlement(make_user, monkeypatch):
    from social import tasks
    from social.models import Challenge

    monkeypatch.setattr(tasks, "SETTLE_BATCH", 1)
    badge = _badge("f20_rv_st", points=50, criteria={"type": "challenge_wins",
                                                     "target": 1})
    client = make_user("f20_rv_st_c")
    trainer = make_user("f20_rv_st_t", user_type="trainer", trainer_is_verified=True)
    waiting, winner = make_user("f20_rv_st_w"), make_user("f20_rv_st_x")
    unranked = _challenge(client, "rv_st_u")
    _enter(unranked, client, value=50, review_state="flagged",
           review_reason="day_over_threshold")
    held = _challenge(trainer, "rv_st_h", target_value=1)
    _enter(held, waiting, value=50, status="completed", review_state="flagged",
           review_reason="day_over_threshold")
    ranked = _challenge(trainer, "rv_st_r", target_value=1)
    _enter(ranked, winner, value=5, status="completed")
    for challenge, days in ((unranked, 10), (held, 9), (ranked, 3)):
        Challenge.objects.filter(pk=challenge.pk).update(
            end_date=timezone.now() - timedelta(days=days))

    tasks.settle_winners()        # the clean ranked one first, however much older the rest
    assert Challenge.objects.get(pk=ranked.pk).winner_checked_at is not None
    assert _holds(winner, badge)
    tasks.settle_winners()        # an unranked one never waits for an operator
    assert Challenge.objects.get(pk=unranked.pk).winner_checked_at is not None
    tasks.settle_winners()        # a ranked one with a flagged first place still does
    assert Challenge.objects.get(pk=held.pk).winner_checked_at is None
    assert not _holds(waiting, badge)


def test_an_on_time_set_raised_after_the_late_window_does_not_count(make_user):
    from routine.models import ExerciseSetLog

    trainer = make_user("f20_rv_ed2_t", user_type="trainer", trainer_is_verified=True)
    cheat = make_user("f20_rv_ed2_c")
    ex = _exercise(trainer, "rv_ed2")
    challenge = _challenge(trainer, "rv_ed2", metric="volume_kg", target_value=100_000)
    _enter(challenge, cheat)
    rows = _sets(cheat, ex, _today(6), 3, reps=5, weight=20.0)
    assert _count(challenge, cheat) == (300.0, "clear")

    # Raised today, six days on, the way the "log the whole day" route edits a set.
    log = ExerciseSetLog.objects.get(pk=rows[0].pk)
    log.weight, log.reps = 400.0, 30
    log.save(update_fields=["weight", "reps"])
    log.refresh_from_db()
    assert log.values_changed_at is not None
    assert _count(challenge, cheat) == (200.0, "clear")

    # Saving a set without changing its values stamps nothing.
    other = ExerciseSetLog.objects.get(pk=rows[1].pk)
    other.notes = "felt good"
    other.save()
    other.refresh_from_db()
    assert other.values_changed_at is None

    # Corrected on its own day (inside the window), it counts at its new values.
    fresh = _sets(cheat, ex, _today(0), 3, reps=5, weight=20.0, first=10)
    fixed = ExerciseSetLog.objects.get(pk=fresh[0].pk)
    fixed.weight = 30.0
    fixed.save()
    assert _count(challenge, cheat) == (200.0 + 350.0, "clear")


def test_the_consoles_leave_the_review_and_the_standing_read_only(make_user):
    from django.contrib import admin as django_admin

    from admin_dashboard.admin import ChallengeParticipationAdmin as ConsoleAdmin
    from social.admin import ChallengeParticipationAdmin as SocialAdmin
    from social.models import ChallengeParticipation

    must = {"review_state", "review_reason", "review_cleared_through", "rank",
            "current_value", "status"}
    for admin_class in (ConsoleAdmin, SocialAdmin):
        model_admin = admin_class(ChallengeParticipation, django_admin.site)
        assert must <= set(model_admin.get_readonly_fields(None)), admin_class


# ------------------------------------------------------------ the late window after the end
def test_the_review_window_waits_for_every_set_that_may_still_sync():
    """The winner cannot settle while a set of the window's last day may still arrive."""
    from social.challenge_progress import LATE_HOURS
    from social.models import Challenge

    assert Challenge.REVIEW_HOURS >= LATE_HOURS


def test_a_last_day_set_that_syncs_after_the_end_counts_until_the_late_window_closes(make_user):
    """A phone offline on the challenge's last day syncs the day after the end: those sets
    are the challenge's and count. A set that arrives after LATE_HOURS does not, and
    nothing moves the standing once the winner is settled."""
    from social.challenge_progress import LATE_HOURS, recount_for_user
    from social.models import Challenge, ChallengeParticipation

    trainer = make_user("f20_late_t", user_type="trainer", trainer_is_verified=True)
    lifter = make_user("f20_late_c")
    ex = _exercise(trainer, "late")
    challenge = _challenge(trainer, "late", metric="sets")
    _enter(challenge, lifter)
    last_day = _today(1)
    # The challenge ended at the end of yesterday; the sets were trained yesterday.
    Challenge.objects.filter(pk=challenge.pk).update(
        end_date=timezone.make_aware(datetime.combine(last_day, time(23, 59))))
    challenge.refresh_from_db()

    # Synced this morning, well inside the late window: counted by the write's recount.
    _sets(lifter, ex, last_day, 4, received=timezone.now())
    recount_for_user(lifter.pk)
    assert ChallengeParticipation.objects.get(challenge=challenge, user=lifter).current_value == 4.0

    # A set of the same day received after the window: not counted.
    _sets(lifter, ex, last_day, 2, first=5,
          received=timezone.make_aware(datetime.combine(last_day, time(23, 59)))
          + timedelta(days=1, hours=LATE_HOURS + 1))
    assert _count(challenge, lifter)[0] == 4.0

    # Once the winner is settled, nothing recounts the standing.
    Challenge.objects.filter(pk=challenge.pk).update(winner_checked_at=timezone.now())
    challenge.refresh_from_db()
    _sets(lifter, ex, last_day, 3, first=20, received=timezone.now())
    recount_for_user(lifter.pk)
    assert ChallengeParticipation.objects.get(challenge=challenge, user=lifter).current_value == 4.0
