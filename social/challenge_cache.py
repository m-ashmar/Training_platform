"""When everyone's cached challenge list must go (SIDE_BUGS S-189, S-194).

`ChallengeViewSet.list` keeps each person's list for 120 s, under a key carrying two
versions: the person's own (`challenges_ver:user:<id>`, bumped when something only
they see moves — their join, their progress) and this module's `EVERYONE`, bumped when
a challenge row itself changes for every reader. Only the first existed, so:

* an operator hiding a challenge (the console's hide action, a `save()`) left it in
  every list for up to two minutes, while its detail answered 404 at once (S-189);
* a creator's update, close or delete, and anyone's join or leave
  (`participants_count`), reached only the acting person's list (S-194).

A `post_save` / `post_delete` on `Challenge` covers every write through the model
(create, edit, close, hide, unhide, delete); the views call `bump_everyone()` beside
the writes that go around it (`.update()` of `participants_count` on join and leave,
of `end_date` on a close, of `status` by the hourly phase task).

The bump happens at once AND again after the commit: a list read between the two
(inside the writer's transaction, it still sees the old row) would otherwise be cached
for the full 120 s. A bump that fails (the cache is down) is logged and swallowed — it
must never fail the write; stale entries expire on their TTL.
"""
import logging

from django.core.cache import cache
from django.db import transaction
from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver

logger = logging.getLogger(__name__)

#: The version every reader's cached list carries.
EVERYONE = "challenges_ver:all"


def user_key(user_id) -> str:
    """One person's version (`ChallengeViewSet._challenges_cache_version`)."""
    return f"challenges_ver:user:{user_id}"


def version(key) -> int:
    """The current value of a version counter, started at 1."""
    value = cache.get(key)
    if value is None:
        value = 1
        cache.set(key, value, timeout=None)
    return value


def bump(key) -> None:
    try:
        try:
            cache.incr(key)
        except ValueError:
            # No counter yet: seeding it at 2 skips whatever v1 entries exist.
            cache.set(key, 2, timeout=None)
    except Exception:
        logger.warning("could not bump %s; cached lists expire on their TTL", key,
                       exc_info=True)


def bump_after_commit(key) -> None:
    """Now, and once more when the current transaction commits (see the docstring)."""
    bump(key)
    if transaction.get_connection().in_atomic_block:
        transaction.on_commit(lambda: bump(key))


def bump_everyone() -> None:
    bump_after_commit(EVERYONE)


@receiver(post_save, sender='social.Challenge',
          dispatch_uid='social_challenge_lists_on_save')
@receiver(post_delete, sender='social.Challenge',
          dispatch_uid='social_challenge_lists_on_delete')
def drop_every_list_on_a_challenge_write(sender, instance, **kwargs):
    bump_everyone()


@receiver(post_save, sender='users.TrainerClientRelation',
          dispatch_uid='social_challenge_lists_on_relation_save')
@receiver(post_delete, sender='users.TrainerClientRelation',
          dispatch_uid='social_challenge_lists_on_relation_delete')
def drop_the_clients_list_on_a_relation_change(sender, instance, **kwargs):
    """A coach's `my_clients` challenges appear in a client's list once the relation is
    approved (BACKEND_BUGS B-162): not two minutes later."""
    if instance.client_id:
        bump_after_commit(user_key(instance.client_id))
