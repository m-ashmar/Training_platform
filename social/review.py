"""An operator's review of a challenge standing (owner, 2026-09-24, F20).

A recount flags a participant whose training on a single day is above a review
threshold (`social/challenge_progress.py REVIEW_THRESHOLDS`). An operator then decides,
from the console — `/dj-admin/challenges/challengeparticipationproxy/` (the class is
`admin_dashboard/admin.py ChallengeParticipationAdmin`, registered on the proxy by
`challenges/admin.py`) and `/admin/social/challengeparticipation/` (`social/admin.py`):

* **clear review** — `review_state='clear'`, `review_reason='operator'`: the person
  keeps their place and can win. `review_cleared_through` records the latest day
  above a threshold at the moment of the clear: recounts flag them again only for a
  LATER day above a threshold;
* **disqualify** — `review_state='disqualified'`, `review_reason='operator'`: the
  person holds no place anywhere (`social/standing.py in_standing`), cannot rejoin or
  leave (`challenge_disqualified`), and their number is still recounted.

Either decision re-ranks the challenge's standing, even a finished one whose winner
is not settled yet (`write_places(settling=True)`: removing a cheater from a finished
board is what the review window is for), drops the cached lists and home cards of
everyone whose place moved, and then settles the winner of any challenge whose review
window has already closed (`social/tasks.py settle_winners`) — so clearing a flagged
first place after the window awards the badge at once, and disqualifying one hands the
check to the next. Once the winner IS settled the stored ranks are the record: a later
decision changes the person's review state (and so what live surfaces show, through
`in_standing`) but never the stored ranks, which the Challenge Winner badge reads.
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
            over = {}
            if state == ChallengeParticipation.REVIEW_CLEAR and challenge.auto_counted:
                from .challenge_progress import measure_challenge

                # Its metric, window and (for `exercise_reps`, B-161) its exercise.
                over = measure_challenge(
                    challenge, [user for _pk, user in by_challenge[challenge_id]])[1]
            # Row by row through `save()`, as every console action here: a bulk
            # UPDATE skips the model (tests/test_regression_gate.py).
            for row in ChallengeParticipation.objects.filter(pk__in=ids).order_by('id'):
                row.review_state = state
                row.review_reason = ChallengeParticipation.REASON_OPERATOR
                fields = ['review_state', 'review_reason']
                if state == ChallengeParticipation.REVIEW_CLEAR:
                    row.review_cleared_through = over.get(row.user_id)
                    fields.append('review_cleared_through')
                row.save(update_fields=fields)
                changed += 1
            touched.update(user for _pk, user in by_challenge[challenge_id])
            # A settled standing is the record (`social/tasks.py settle_winner`): a
            # review after the settlement changes the person's state, never the stored
            # ranks. Re-ranking it re-read who is visible NOW, so a suspension after
            # the settlement moved the record and a second person could win (R1).
            write_places(challenge, moved_users=touched,
                         settling=challenge.winner_checked_at is None)
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
    # A review decision changes the participation: the change permission, not the view
    # permission an action with none declared is offered to (Road to 10 round 4).
    action.allowed_permissions = ('change',)
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
#: Read-only on both change forms. A form save takes no challenge lock, re-ranks
#: nothing and settles nothing, and a recount running beside it would overwrite the
#: review with its own copy: review decisions go through the two actions above
#: (`apply_review`), and the number, place and completion follow from the count.
PARTICIPATION_READONLY = ('status', 'current_value', 'progress_percentage', 'rank',
                          'completed_at', 'review_state', 'review_reason',
                          'review_cleared_through')
