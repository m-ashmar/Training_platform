"""An operator's review of a challenge standing (owner, 2026-09-24, F20).

A recount flags a participant whose training on a single day is above a review
threshold (`social/challenge_progress.py REVIEW_THRESHOLDS`). An operator then decides,
from the console (`social/admin.py`, `admin_dashboard/admin.py`, both on
`ChallengeParticipation`):

* **clear review** — `review_state='clear'`, `review_reason='operator'`: the person
  keeps their place and can win; recounts do not flag them again;
* **disqualify** — `review_state='disqualified'`, `review_reason='operator'`: the
  person holds no place anywhere (`social/standing.py in_standing`), cannot rejoin or
  leave (`challenge_disqualified`), and their number is still recounted.

Either decision re-ranks the challenge's standing, even a finished one
(`write_places(settling=True)`: removing a cheater from a finished board is what the
review window is for), drops the cached lists and home cards of everyone whose place
moved, and then settles the winner of any challenge whose review window has already
closed (`social/tasks.py settle_winners`) — so clearing a flagged first place after the
window awards the badge at once, and disqualifying one hands the check to the next.
"""
from django.db import transaction


def apply_review(participations, state) -> int:
    """Set `state` ('clear' or 'disqualified') on `participations`, reason `operator`.

    Returns how many rows changed. One locked pass per challenge touched.
    """
    from home.views import bump_stamp

    from .models import Challenge, ChallengeParticipation
    from .standing import write_places
    from .tasks import settle_winners
    from .views import ChallengeViewSet

    if state not in (ChallengeParticipation.REVIEW_CLEAR,
                     ChallengeParticipation.REVIEW_DISQUALIFIED):
        raise ValueError(state)
    rows = list(participations.values_list('id', 'challenge_id', 'user_id'))
    by_challenge = {}
    for pk, challenge_id, user_id in rows:
        by_challenge.setdefault(challenge_id, []).append((pk, user_id))

    changed, touched = 0, set()
    for challenge_id in sorted(by_challenge):
        with transaction.atomic():
            challenge = (Challenge.objects.select_for_update()
                         .filter(pk=challenge_id).first())
            if challenge is None:
                continue
            ids = [pk for pk, _user in by_challenge[challenge_id]]
            changed += ChallengeParticipation.objects.filter(pk__in=ids).update(
                review_state=state, review_reason=ChallengeParticipation.REASON_OPERATOR)
            touched.update(user for _pk, user in by_challenge[challenge_id])
            write_places(challenge, moved_users=touched, settling=True)
    for user_id in sorted(touched):
        ChallengeViewSet.invalidate_challenges_cache_for(user_id)
        bump_stamp(user_id)
    settle_winners(challenge_ids=sorted(by_challenge))
    return changed


# ------------------------------------------------------------ the console's actions
def _review_action(name, description, state, message):
    def action(modeladmin, request, queryset):
        from training_platform.admin_utils import log_admin_action

        rows = list(queryset.select_related('user', 'challenge'))
        count = apply_review(queryset.model.objects.filter(pk__in=[r.pk for r in rows]),
                             state)
        for row in rows:
            log_admin_action(request, row, message)
        modeladmin.message_user(request, f"{description}: {count} updated.")

    action.__name__ = name
    action.short_description = description
    return action


clear_review = _review_action(
    'clear_challenge_review', 'Clear review (keeps place, can win)', 'clear',
    'Challenge review cleared by an operator')
disqualify = _review_action(
    'disqualify_from_challenge', 'Disqualify from the challenge', 'disqualified',
    'Disqualified from the challenge by an operator')

#: What both consoles show and filter on for a participation.
PARTICIPATION_LIST_DISPLAY = ('user', 'challenge', 'current_value', 'progress_percentage',
                              'rank', 'review_state', 'review_reason')
PARTICIPATION_LIST_FILTER = ('review_state', 'review_reason', 'challenge')
