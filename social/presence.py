"""A suspension takes the person off the stored standing at once (the owner's decision 8,
2026-09-24).

Every public surface asks `users/visibility.py` when it READS, so a suspended account
leaves the boards, the feed, the follow lists and search the moment `is_active` turns
False, and comes back the moment it turns True — nothing is copied, so nothing has to be
kept in step for those.

Two things are stored, and this module keeps them in step:

* **`ChallengeParticipation.rank`** — the stored copy of each place. The Challenge
  Winner badge reads it (`rank=1`) when a challenge closes, and the places of the
  people behind a suspended leader are cached in their challenge lists and home cards.
  When an account's `is_active` changes, in either direction, every RUNNING challenge
  the person is in (not over and not cancelled — a finished standing is a record) is
  re-ranked by `social/standing.py write_places`, under the challenge's row lock, after
  the commit; the people whose place moved have those caches dropped. So the stored
  ranks are rewritten WHEN THE SUSPENSION HAPPENS, not left for the next re-rank.
* **The community-points leaderboard's response cache** (`/api/achievements/`,
  `training_platform/cache_config.py`, five minutes): its version is bumped once, so no
  viewer is served a board that still shows the account.

What triggers it: a `save()` of the account that writes `is_active` — the admin's
change form and its list editing, the bulk "deactivate" / "activate" actions
(`training_platform/admin_utils.py bulk_apply` saves row by row) and erasure
(`training_platform/privacy`, which saves the anonymised row). The challenges are
chosen at that moment, before erasure deletes the participations, and re-ranked after
the commit. A raw `queryset.update(is_active=...)` sends no signal: its stored ranks
catch up at the challenge's next re-rank (any join, leave, logged progress or recount),
and every public surface is right regardless because they read the account live.

Cost: nothing on a save that does not change `is_active` (the value loaded with the row
is remembered in memory by `post_init`, no query). On a change, one query to find the
challenges, then one ranking pass per running challenge.
"""
import logging

from django.db import transaction
from django.db.models.signals import post_init, post_save
from django.dispatch import receiver
from django.utils import timezone

from users.visibility import FIELD

logger = logging.getLogger(__name__)

#: Where the value of `is_active` the row was LOADED with is kept on the instance.
_LOADED = '_visibility_loaded'


@receiver(post_init, sender='users.CustomUser', dispatch_uid='social_presence_remember')
def remember_loaded_visibility(sender, instance, **kwargs):
    # `__dict__`, never the attribute: on a row loaded with `.only()` the field is
    # deferred, and reading it would cost a query per instance.
    instance.__dict__[_LOADED] = instance.__dict__.get(FIELD)


@receiver(post_save, sender='users.CustomUser', dispatch_uid='social_presence_changed')
def restand_on_visibility_change(sender, instance, created=False, raw=False,
                                 update_fields=None, **kwargs):
    if update_fields is not None and FIELD not in update_fields:
        return                                   # not written: the stored value stands
    before = instance.__dict__.get(_LOADED)
    now = instance.__dict__.get(FIELD)
    instance.__dict__[_LOADED] = now
    if created or raw or before is None or now is None or bool(before) == bool(now):
        return
    try:
        challenge_ids = running_challenges_of(instance.pk)
    except Exception:
        # The account's save must never fail for a board; it catches up at its next
        # re-rank.
        logger.warning("could not read the challenges of user %s", instance.pk,
                       exc_info=True)
        return
    transaction.on_commit(lambda: _after_commit(challenge_ids))


def running_challenges_of(user_id) -> list:
    """Ids of the challenges this person is in that are not over and not cancelled."""
    from django.db.models import Q

    from .models import Challenge, ChallengeParticipation

    return sorted(set(
        ChallengeParticipation.objects.filter(user_id=user_id)
        .exclude(Q(challenge__status__in=Challenge.TERMINAL_STATUSES)
                 | Q(challenge__end_date__lt=timezone.now()))
        .values_list('challenge_id', flat=True)))


def _after_commit(challenge_ids):
    from training_platform.signals import bump_cache_version

    try:
        restand(challenge_ids)
    except Exception:
        logger.warning("could not re-rank challenges %s", challenge_ids, exc_info=True)
    # The community-points board is a cached response (`/api/achievements/`).
    bump_cache_version('ACHIEVEMENT')


def restand(challenge_ids) -> int:
    """Re-rank each of these challenges from `place()`; return how many were re-ranked.

    Each under its own row lock — the one `join`, `leave`, `update_progress` and a
    recount take, so the lock order is unchanged — and `write_places` itself leaves a
    challenge that is over as it stands. Everyone whose place moved has their cached
    challenge list and home card dropped, as `ChallengeViewSet` does after a re-rank.
    """
    from home.views import bump_stamp

    from .models import Challenge
    from .standing import write_places
    from .views import ChallengeViewSet

    movers: set = set()
    done = 0
    for pk in sorted(set(challenge_ids or ())):
        with transaction.atomic():
            challenge = Challenge.objects.select_for_update().filter(pk=pk).first()
            if challenge is None:
                continue
            write_places(challenge, moved_users=movers)
            done += 1
    for user_id in sorted(movers):
        ChallengeViewSet.invalidate_challenges_cache_for(user_id)
        bump_stamp(user_id)
    return done
