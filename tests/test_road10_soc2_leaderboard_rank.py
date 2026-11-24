"""The fix run's SOC-2, S-192 (2026-11-24): the caller's `user_rank` on
`GET /api/achievements/leaderboard/` is their own row's `rank`. The board orders by
points, then the user's id for a tie; `user_rank` counted only those with more points,
so a caller tied with people of smaller ids was told a better place than their row
says. The tied case fails on the code before.
"""
import uuid

import pytest

pytestmark = pytest.mark.django_db

BOARD = "/api/achievements/leaderboard/"


def _badge(points):
    from achievements.models import Achievement

    tag = uuid.uuid4().hex[:8]
    return Achievement.objects.create(key=f"s192_{tag}", name=f"S-192 {tag}",
                                      description="test", category="milestone",
                                      points=points)


def _award(person, *badges):
    from achievements.models import UserAchievement

    for badge in badges:
        UserAchievement.objects.create(user=person, achievement=badge)


def _board(api, person):
    body = api(person).get(BOARD, {"limit": 50}).json()
    return body["user_rank"], {row["user_id"]: row["rank"] for row in body["leaderboard"]}


def test_a_tied_caller_is_told_their_own_place(make_user, api):
    hundred = _badge(100)
    tied = [make_user(f"s192_tie_{i}_{uuid.uuid4().hex[:4]}") for i in range(3)]
    for person in tied:
        _award(person, hundred)
    for person in tied:
        user_rank, rows = _board(api, person)
        assert user_rank == rows[person.pk], (person.pk, user_rank, rows)


def test_more_points_still_place_ahead(make_user, api):
    top, low = make_user(f"s192_top_{uuid.uuid4().hex[:4]}"), make_user(
        f"s192_low_{uuid.uuid4().hex[:4]}")
    _award(top, _badge(500), _badge(500))
    _award(low, _badge(1))
    top_rank, rows = _board(api, top)
    low_rank, _rows = _board(api, low)
    assert top_rank == rows[top.pk] and low_rank == rows[low.pk]
    assert top_rank < low_rank
