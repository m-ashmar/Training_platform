"""Who may appear to other people — ONE predicate (the owner's decision 8, 2026-09-24).

The owner: "A suspended account should not continue appearing publicly by name/photo.
Historical statistics can remain internally, but the public leaderboard should exclude
suspended users." In this codebase an account is suspended — or erased, which also
renames it — when `CustomUser.is_active` is False (`users/models.py`, the table beside
`email_verified`). So a person is PUBLICLY VISIBLE exactly when their account is active,
and every surface that shows one person to another asks this module, not the column:

* challenge standings — `social/standing.py in_standing` (the leaderboard rows and its
  `me` row, `user_progress.rank`, the home card's `challenge_rank`, the stored `rank`
  and therefore the Challenge Winner badge);
* the community-points leaderboard — `achievements/views.py leaderboard` and the
  caller's `user_rank` (`achievements/engine.py get_user_stats`);
* posts, comments and the counts that summarise them — `social/views.py
  visible_posts_for`, `CommentViewSet`;
* follower and following lists and the counts on a profile — `UserFollowViewSet`,
  `PublicUserProfileViewSet`;
* a challenge's `creator` (served null) — `ChallengeSerializer`;
* people search and the public profile — `PublicUserProfileViewSet._visible_people`;
* trainer discovery — `users/trainer_directory.py directory_trainers` (the roster, a
  trainer's page, the matches and the first-day home's featured trainers), a client's
  request (`ClientRequestTrainerView`) and the trainer invitations a client is shown.
  The two older lists, `trainers/public/` and `client/available-trainers/`, filter the
  same column (`is_active=True`) in their own queries.

Operator views (`/dj-admin/`, the admin site) are not public and keep every account.
Nothing is deleted or rewritten when an account is suspended: the rows stay, they are
only not SHOWN, so reinstating the account (`is_active` back to True) brings the person
back everywhere, with their history. What a suspension does write is the stored
challenge `rank` of the running challenges they are in (`social/presence.py`), because
the Challenge Winner badge reads that column when a challenge closes.

Deliberately NOT part of the predicate: `email_verified` (an unverified signup has no
credential, so it cannot have posted, joined or followed anything) and `user_type`
(people search narrows to members on top of this — the owner's decision 7). A caller
that needs those adds them after this.
"""
from django.db.models import Q

#: The column that decides it. One name, so a search for it finds every surface.
FIELD = 'is_active'


def _lookup(path: str) -> str:
    return f'{path}__{FIELD}' if path else FIELD


def public_q(path: str = '') -> Q:
    """Rows whose person at `path` may be shown to other people.

    `path` is the relation to the account from the queryset's model — `'author'` on a
    post, `'user'` on a participation, `''` on `CustomUser` itself.
    """
    return Q(**{_lookup(path): True})


def hidden_q(path: str = '') -> Q:
    """The opposite of `public_q`: rows whose person may NOT be shown.

    Not `~public_q(path)`: on a nullable relation the negation would also match rows
    with no person at all.
    """
    return Q(**{_lookup(path): False})


def is_public(user) -> bool:
    """The same rule for one loaded account (False for None)."""
    return bool(user is not None and getattr(user, FIELD, False))


def public_people(queryset=None):
    """`queryset` of accounts (every account by default) less the ones not shown."""
    from .models import CustomUser

    queryset = CustomUser.objects.all() if queryset is None else queryset
    return queryset.filter(public_q())
