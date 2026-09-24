"""What the review of the 2026-09-24 merges found (build/anticheat onto the owner's
decision 8, "a suspended account holds no place").

| # | Finding | Test |
|---|---|---|
| R1 | An operator's review action re-ranked a challenge whose winner was already settled; with the visibility term the merge added, a later suspension then moved the record and a second person could earn Challenge Winner | `test_r1_*` |
"""
from datetime import timedelta

import pytest
from django.utils import timezone

from tests.test_owner_0924_suspended import _enter, _rerank, _running_challenge, _suspend

pytestmark = pytest.mark.django_db


def test_r1_a_review_after_the_settlement_never_rewrites_the_record(
        make_user, django_capture_on_commit_callbacks):
    """C settles with A first; A is suspended later (the record keeps A at 1); then an
    operator clears a flagged participant of C. That review re-ranked the finished
    standing, read A as not visible, and gave B rank 1 — the badge engine then counted a
    second win of C, for B."""
    from achievements.engine import AchievementEngine
    from social.models import Challenge, ChallengeParticipation
    from social.review import apply_review
    from social.tasks import settle_winners

    owner = make_user("r1_owner", user_type="trainer", trainer_is_verified=True)
    a, b, x = make_user("r1_a"), make_user("r1_b"), make_user("r1_x")
    challenge = _running_challenge(owner, "r1", target_value=50.0, metric="sets")
    base = timezone.now() - timedelta(days=1)
    _enter(challenge, a, 90.0, joined_at=base, status="completed")
    _enter(challenge, b, 80.0, joined_at=base + timedelta(minutes=1), status="completed")
    flagged = _enter(challenge, x, 10.0, joined_at=base + timedelta(minutes=2))
    ChallengeParticipation.objects.filter(pk=flagged.pk).update(
        review_state=ChallengeParticipation.REVIEW_FLAGGED,
        review_reason="day_over_threshold")
    _rerank(challenge)
    Challenge.objects.filter(pk=challenge.pk).update(
        end_date=timezone.now() - timedelta(hours=Challenge.REVIEW_HOURS, minutes=5))

    assert settle_winners(challenge_ids=[challenge.pk]) == 1
    assert Challenge.objects.get(pk=challenge.pk).winner_checked_at is not None

    _suspend(a, django_capture_on_commit_callbacks)          # after the settlement
    apply_review(ChallengeParticipation.objects.filter(pk=flagged.pk),
                 ChallengeParticipation.REVIEW_CLEAR)

    stored = dict(ChallengeParticipation.objects.filter(challenge=challenge)
                  .values_list("user__username", "rank"))
    assert stored["r1_a"] == 1 and stored["r1_b"] == 2, stored
    assert AchievementEngine._get_metric_value(b, "challenge_wins", {}) == 0
