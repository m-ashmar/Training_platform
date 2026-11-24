"""The fix run's SOC-2, S-254 (2026-11-24): a challenge's `active_participants`, documented
as live, reaches every reader's cached challenge list at once when someone's first
counted set makes them active. The recount dropped only the lists of the people whose
place moved, so a reader outside the moved standing read the old figure for the list's
120 s. The first test fails on the code before.
"""
from datetime import timedelta

import pytest
from django.utils import timezone

from tests.test_owner_decisions_0924_challenges import _challenge, _enter, _exercise, _sets

pytestmark = pytest.mark.django_db

LIST = "/api/social/challenges/"


@pytest.fixture(autouse=True)
def a_cold_cache():
    from django.core.cache import cache

    from training_platform.cache import private_cache, public_cache

    for store in (private_cache(), public_cache(), cache):
        store.clear()
    yield
    for store in (private_cache(), public_cache(), cache):
        store.clear()


def _active(api, reader, challenge):
    rows = api(reader).get(LIST).json()
    rows = rows["results"] if isinstance(rows, dict) else rows
    (row,) = [r for r in rows if r["id"] == challenge.pk]
    return row["active_participants"]


def test_a_new_active_participant_reaches_a_reader_whose_place_did_not_move(make_user, api):
    from social.challenge_progress import recount_for_user

    trainer = make_user("s254_t", user_type="trainer", trainer_is_verified=True)
    leader, newcomer = make_user("s254_lead"), make_user("s254_new")
    challenge = _challenge(trainer, "s254")
    lift = _exercise(trainer, "s254")
    yesterday = timezone.localdate() - timedelta(days=1)
    _enter(challenge, leader)
    _sets(leader, lift, yesterday, 6)
    recount_for_user(leader.pk)
    _enter(challenge, newcomer)

    assert _active(api, leader, challenge) == 1          # the leader's list is cached
    _sets(newcomer, lift, yesterday, 2)                  # first counted sets: 2nd place
    recount_for_user(newcomer.pk)
    assert _active(api, leader, challenge) == 2, "the leader's list kept the old figure"


def test_a_recount_that_moves_no_one_across_zero_keeps_the_lists(make_user, api):
    """Only a number crossing zero changes `active_participants`: a leader adding sets
    leaves the other readers' lists alone (their own version still drops theirs)."""
    from social.challenge_cache import EVERYONE, version
    from social.challenge_progress import recount_for_user

    trainer = make_user("s254b_t", user_type="trainer", trainer_is_verified=True)
    leader = make_user("s254b_lead")
    challenge = _challenge(trainer, "s254b")
    lift = _exercise(trainer, "s254b")
    yesterday = timezone.localdate() - timedelta(days=1)
    _enter(challenge, leader)
    _sets(leader, lift, yesterday, 3)
    recount_for_user(leader.pk)
    before = version(EVERYONE)
    _sets(leader, lift, yesterday, 3, first=4)
    recount_for_user(leader.pk)
    assert version(EVERYONE) == before
