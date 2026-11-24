"""Walk states: community (`users/walk/__init__.py` says how to add one).

Owned by the phase 3 worker: challenges, posts and comments, reports, follows.
"""
from . import state
from .framework import on_retire


@on_retire
def _hide_what_a_retired_walk_account_hosted(users):
    """A challenge outlives its creator's account in the lists (its creator is only
    served as null, `users/visibility.py`), so every run would add the old walk
    challenges to the community list. The operator's own switch takes them down:
    `is_hidden`, which hides a challenge from every list and stops every join. Walk data
    only — `users` are walk accounts being retired."""
    from social.challenge_cache import bump_everyone
    from social.models import Challenge

    hosted = list(Challenge.objects.filter(creator__in=users, is_hidden=False))
    for challenge in hosted:
        challenge.is_hidden = True
        challenge.save(update_fields=["is_hidden"])
    if hosted:
        bump_everyone()


@state("member_with_post", area="community", role="client",
       summary="A client with a post of their own and a comment on it from another member.")
def member_with_post(walk):
    from social.models import Comment, Post

    me = walk.account()
    friend = walk.account("friend", first_name="رنا", last_name="حمدان", gender="Female")
    post = Post.objects.create(author=me, content="أنهيت أول أسبوع من برنامجي اليوم.")
    comment = Comment.objects.create(post=post, author=friend, content="بالتوفيق!")
    walk.ids(post=post.pk, comment=comment.pk, friend=friend.pk)
