"""The owner's decision of 2026-09-24: the Challenge Winner badge needs at least
`MIN_PARTICIPANTS_FOR_WINNER` (5) ACTIVE participants.

"Active" (`social/standing.py active_participants`): in the standing — not dropped, not
disqualified, the account publicly visible — AND a counted number above zero at the
settlement, so a trainer cannot make a winner by padding a challenge with idle accounts.
Fewer, and the challenge settles with no winner (`social/tasks.py _settle_winner`),
which records the count it decided on (`Challenge.settled_active_participants`); the badge
engine reads that record (`achievements/engine.py challenge_wins`), never a recount. Challenge rows serve `winner_min_participants` and the live
`active_participants`, annotated once per list page.

Each test fails on the code before this change.
"""
from datetime import timedelta

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from tests.test_fx_social import _badge

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def a_cold_cache():
    """The challenge list is cached."""
    from django.core.cache import cache

    from training_platform.cache import private_cache, public_cache

    for store in (private_cache(), public_cache(), cache):
        store.clear()
    yield
    for store in (private_cache(), public_cache(), cache):
        store.clear()


# ------------------------------------------------------------------ helpers
def _challenge(creator, tag, **kw):
    """A RANKED challenge (counted by the server, created by a verified trainer)."""
    from social.models import Challenge

    now = timezone.now()
    fields = dict(title=f"min {tag}", description="winner minimum",
                  challenge_type="workout", creator=creator,
                  start_date=now - timedelta(days=8), end_date=now + timedelta(days=1),
                  status="active", target_value=10.0, unit="sets", metric="sets")
    fields.update(kw)
    return Challenge.objects.create(**fields)


def _field(make_user, challenge, tag, values, *, winner_value=20.0):
    """The winner (reached the target, joined first) plus one participant per value in
    `values`, in that order; returns (winner, [others])."""
    from social.models import ChallengeParticipation

    winner = make_user(f"min_{tag}_w")
    ChallengeParticipation.objects.create(user=winner, challenge=challenge,
                                          current_value=winner_value, status="completed")
    others = []
    for n, value in enumerate(values):
        person = make_user(f"min_{tag}_{n}")
        ChallengeParticipation.objects.create(user=person, challenge=challenge,
                                              current_value=value, status="joined")
        others.append(person)
    return winner, others


def _past_review(challenge):
    from social.models import Challenge

    Challenge.objects.filter(pk=challenge.pk).update(
        end_date=timezone.now() - timedelta(hours=Challenge.REVIEW_HOURS, minutes=5))


def _holds(person, badge):
    from achievements.models import UserAchievement

    return UserAchievement.objects.filter(user=person, achievement=badge).exists()


def _settle(challenge):
    from social.tasks import settle_winners

    _past_review(challenge)
    return settle_winners(challenge_ids=[challenge.pk])


def _win_badge(key):
    return _badge(key, points=50, criteria={"type": "challenge_wins", "target": 1})


def _trainer(make_user, tag):
    return make_user(f"min_{tag}_t", user_type="trainer", trainer_is_verified=True)


# ------------------------------------------------------------------ settlement
def test_four_active_participants_settle_the_challenge_with_no_winner(make_user, api):
    from social.models import Challenge

    badge = _win_badge("min_four")
    challenge = _challenge(_trainer(make_user, "four"), "four")
    winner, _others = _field(make_user, challenge, "four", [5.0, 4.0, 3.0])

    assert _settle(challenge) == 1
    assert Challenge.objects.get(pk=challenge.pk).winner_checked_at is not None
    assert not _holds(winner, badge)
    row = api(winner).get(f"/api/social/challenges/{challenge.pk}/").json()
    assert row["winner_final"] is True
    assert (row["winner_min_participants"], row["active_participants"]) == (5, 4)


def test_five_active_participants_crown_the_first_place(make_user, api):
    badge = _win_badge("min_five")
    challenge = _challenge(_trainer(make_user, "five"), "five")
    winner, others = _field(make_user, challenge, "five", [5.0, 4.0, 3.0, 2.0])

    assert _settle(challenge) == 1
    assert _holds(winner, badge)
    assert not any(_holds(person, badge) for person in others)
    row = api(winner).get(f"/api/social/challenges/{challenge.pk}/").json()
    assert (row["winner_final"], row["active_participants"]) == (True, 5)


def test_joiners_who_never_trained_do_not_make_a_winner(make_user, api):
    badge = _win_badge("min_idle")
    challenge = _challenge(_trainer(make_user, "idle"), "idle")
    winner, _others = _field(make_user, challenge, "idle", [5.0, 4.0, 3.0, 0.0, 0.0])

    row = api(winner).get(f"/api/social/challenges/{challenge.pk}/").json()
    assert (row["winner_min_participants"], row["active_participants"]) == (5, 4)
    assert _settle(challenge) == 1
    assert not _holds(winner, badge)


def test_a_disqualified_participant_does_not_count(make_user):
    from social.models import ChallengeParticipation

    badge = _win_badge("min_dq")
    challenge = _challenge(_trainer(make_user, "dq"), "dq")
    winner, others = _field(make_user, challenge, "dq", [5.0, 4.0, 3.0, 2.0])
    ChallengeParticipation.objects.filter(challenge=challenge, user=others[-1]).update(
        review_state=ChallengeParticipation.REVIEW_DISQUALIFIED)

    assert _settle(challenge) == 1
    assert not _holds(winner, badge)


def test_a_suspended_participant_does_not_count(make_user, django_capture_on_commit_callbacks):
    badge = _win_badge("min_susp")
    challenge = _challenge(_trainer(make_user, "susp"), "susp")
    winner, others = _field(make_user, challenge, "susp", [5.0, 4.0, 3.0, 2.0])
    with django_capture_on_commit_callbacks(execute=True):
        others[0].is_active = False
        others[0].save()

    assert _settle(challenge) == 1
    assert not _holds(winner, badge)


def test_challenge_wins_awards_nothing_below_the_minimum_even_called_directly(make_user):
    """A settled record whose first place is clear and completed: the badge engine reads
    the settlement's recorded count (`Challenge.settled_active_participants`) and refuses
    it below the minimum, and refuses a record the settlement never wrote (null) —
    whatever the participation rows say."""
    from achievements.engine import AchievementEngine
    from social.models import Challenge, ChallengeParticipation

    challenge = _challenge(_trainer(make_user, "direct"), "direct")
    winner, others = _field(make_user, challenge, "direct", [5.0, 4.0, 3.0, 2.0])
    _past_review(challenge)
    for place, person in enumerate([winner, *others], start=1):
        ChallengeParticipation.objects.filter(challenge=challenge, user=person).update(
            rank=place)

    # Five ranked rows above zero, but no settlement recorded a count.
    Challenge.objects.filter(pk=challenge.pk).update(winner_checked_at=timezone.now())
    assert AchievementEngine._get_metric_value(winner, "challenge_wins", {}) == 0
    Challenge.objects.filter(pk=challenge.pk).update(settled_active_participants=4)
    assert AchievementEngine._get_metric_value(winner, "challenge_wins", {}) == 0
    Challenge.objects.filter(pk=challenge.pk).update(settled_active_participants=5)
    assert AchievementEngine._get_metric_value(winner, "challenge_wins", {}) == 1


def test_the_settlement_records_its_count_and_a_later_erasure_keeps_the_win(make_user):
    """The verdict is taken once, at the settlement. An erased rival's participation row
    is deleted (privacy source `challenge_participation`); a recount after it would take
    back a win that was valid when it was settled."""
    from achievements.engine import AchievementEngine
    from social.models import Challenge
    from training_platform.privacy.registry import erase_user_data

    badge = _win_badge("min_erase")
    challenge = _challenge(_trainer(make_user, "erase"), "erase")
    winner, others = _field(make_user, challenge, "erase", [5.0, 4.0, 3.0, 2.0, 0.0])

    assert _settle(challenge) == 1
    assert Challenge.objects.get(pk=challenge.pk).settled_active_participants == 5
    assert _holds(winner, badge)
    assert AchievementEngine._get_metric_value(winner, "challenge_wins", {}) == 1

    erase_user_data(others[-2])            # a rival leaves after the settlement
    assert Challenge.objects.get(pk=challenge.pk).settled_active_participants == 5
    assert AchievementEngine._get_metric_value(winner, "challenge_wins", {}) == 1

    # Below the minimum the count is recorded too, and it is what the engine refuses.
    small = _challenge(_trainer(make_user, "erase2"), "erase2")
    _field(make_user, small, "erase2", [5.0, 0.0])
    assert _settle(small) == 1
    assert Challenge.objects.get(pk=small.pk).settled_active_participants == 2


# ------------------------------------------------------------------ the list
def test_the_list_counts_active_participants_in_one_query_per_page(make_user, api):
    trainer = _trainer(make_user, "list")
    viewer = make_user("min_list_viewer")
    first = _challenge(trainer, "list-0")
    _field(make_user, first, "list0", [3.0, 0.0])

    def _list():
        """(queries that read participations, the rows). The total also moves with
        things unrelated to rows (the paginator's COUNT is skipped on some requests), so
        the test counts what a per-row `active_participants` would add: a query of
        `social_challengeparticipation` for each challenge."""
        from django.core.cache import cache

        from training_platform.cache import private_cache, public_cache

        for store in (private_cache(), public_cache(), cache):
            store.clear()
        with CaptureQueriesContext(connection) as ctx:
            response = api(viewer).get("/api/social/challenges/")
        assert response.status_code == 200
        body = response.json()
        reads = [q["sql"] for q in ctx.captured_queries
                 if "social_challengeparticipation" in q["sql"]]
        return len(reads), body.get("results", body)

    one, rows = _list()
    assert {row["id"]: row["active_participants"] for row in rows} == {first.pk: 2}
    assert all(row["winner_min_participants"] == 5 for row in rows)

    more = [_challenge(trainer, f"list-{n}") for n in range(1, 5)]
    for n, challenge in enumerate(more, start=1):
        _field(make_user, challenge, f"list{n}", [1.0] * n)
    five, rows = _list()
    assert five == one, f"{one} participation reads for 1 challenge, {five} for 5"
    assert {row["id"]: row["active_participants"] for row in rows} == {
        first.pk: 2, **{c.pk: n + 1 for n, c in enumerate(more, start=1)}}
