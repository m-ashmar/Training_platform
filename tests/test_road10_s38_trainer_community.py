"""Road to 10 S3.8 (the owner's D11): trainers in the community.

A trainer reads the feed, posts, comments, and creates and runs a challenge. Every
community and challenge route is exercised here with a TRAINER's token: what a trainer
may do answers its success status, and anything refused answers with a `code`, never a
bare 403.
"""
import uuid
from datetime import timedelta

import pytest
from django.utils import timezone

pytestmark = pytest.mark.django_db

JSON = "application/json"


def _name(tag):
    return f"s38_{tag}_{uuid.uuid4().hex[:6]}"


def _coded(resp):
    """A refusal carries its `code` (never a bare 403)."""
    if resp.status_code >= 400:
        body = resp.json()
        assert body.get("code"), resp.content
    return resp


def _challenge_body(**extra):
    now = timezone.now()
    body = {"title": "Coach's sets", "description": "One month of sets",
            "challenge_type": "strength",
            "start_date": (now - timedelta(hours=1)).isoformat(),
            "end_date": (now + timedelta(days=20)).isoformat(),
            "target_value": 40, "unit": "sets", "metric": "manual"}
    body.update(extra)
    return body


@pytest.fixture
def people(make_user):
    trainer = make_user(_name("t"), user_type="trainer", first_name="Rami",
                        email_verified=True)
    client = make_user(_name("c"), first_name="Lina", email_verified=True)
    return trainer, client


def test_a_trainer_reads_the_feed_posts_and_likes(people, api):
    from social.models import Post

    trainer, client = people
    theirs = Post.objects.create(author=client, content="Leg day done", visibility="public")
    coach = api(trainer)

    assert _coded(coach.get("/api/social/posts/")).status_code == 200
    assert _coded(coach.get("/api/social/posts/feed/")).status_code == 200

    made = _coded(coach.post("/api/social/posts/", {"content": "Warm up before you lift"},
                             content_type=JSON))
    assert made.status_code == 201, made.content
    mine = made.json()["id"]
    assert Post.objects.get(pk=mine).author_id == trainer.pk

    assert _coded(coach.get(f"/api/social/posts/{mine}/")).status_code == 200
    edited = _coded(coach.patch(f"/api/social/posts/{mine}/",
                                {"content": "Warm up for ten minutes"}, content_type=JSON))
    assert edited.status_code == 200, edited.content
    liked = _coded(coach.post(f"/api/social/posts/{theirs.pk}/like/", {}, content_type=JSON))
    assert liked.status_code == 200, liked.content

    # The client reads the trainer's post in their own feed list.
    listed = api(client).get("/api/social/posts/").json()
    rows = listed["results"] if isinstance(listed, dict) else listed
    assert mine in {row["id"] for row in rows}

    # Someone else's post: refused with its code.
    other = _coded(coach.patch(f"/api/social/posts/{theirs.pk}/", {"content": "x"},
                               content_type=JSON))
    assert other.status_code == 403 and other.json()["code"] == "not_owner"

    assert coach.delete(f"/api/social/posts/{mine}/").status_code == 204


def test_a_trainer_comments_and_likes_a_comment(people, api):
    from social.models import Post

    trainer, client = people
    post = Post.objects.create(author=client, content="New best on squat", visibility="public")
    coach = api(trainer)

    made = _coded(coach.post("/api/social/comments/",
                             {"post": post.pk, "content": "Great depth"}, content_type=JSON))
    assert made.status_code == 201, made.content
    mine = made.json()["id"]
    assert _coded(coach.get(f"/api/social/comments/?post={post.pk}")).status_code == 200
    edited = _coded(coach.patch(f"/api/social/comments/{mine}/", {"content": "Great depth!"},
                                content_type=JSON))
    assert edited.status_code == 200, edited.content

    theirs = api(client).post("/api/social/comments/",
                              {"post": post.pk, "content": "Thanks coach"}, content_type=JSON)
    assert theirs.status_code == 201, theirs.content
    liked = _coded(coach.post(f"/api/social/comments/{theirs.json()['id']}/like/", {},
                              content_type=JSON))
    assert liked.status_code == 200, liked.content
    assert coach.delete(f"/api/social/comments/{mine}/").status_code == 204


def test_a_trainer_creates_and_runs_a_challenge(people, api):
    from social.models import Challenge, ChallengeParticipation

    trainer, client = people
    coach = api(trainer)

    made = _coded(coach.post("/api/social/challenges/", _challenge_body(), content_type=JSON))
    assert made.status_code == 201, made.content
    cid = made.json()["id"]
    assert Challenge.objects.get(pk=cid).creator_id == trainer.pk

    assert _coded(coach.get("/api/social/challenges/")).status_code == 200
    assert _coded(coach.get(f"/api/social/challenges/{cid}/")).status_code == 200
    edited = _coded(coach.patch(f"/api/social/challenges/{cid}/",
                                {"description": "Log every set"}, content_type=JSON))
    assert edited.status_code == 200, edited.content

    # A member joins it; the trainer reads the board.
    assert api(client).post(f"/api/social/challenges/{cid}/join/", {},
                            content_type=JSON).status_code == 201
    board = _coded(coach.get(f"/api/social/challenges/{cid}/leaderboard/"))
    assert board.status_code == 200, board.content

    # The trainer takes part in their own challenge too.
    joined = _coded(coach.post(f"/api/social/challenges/{cid}/join/", {}, content_type=JSON))
    assert joined.status_code == 201, joined.content
    logged = _coded(coach.post(f"/api/social/challenges/{cid}/update_progress/",
                               {"current_value": 12}, content_type=JSON))
    assert logged.status_code == 200, logged.content
    left = _coded(coach.post(f"/api/social/challenges/{cid}/leave/", {}, content_type=JSON))
    assert left.status_code == 200, left.content
    assert ChallengeParticipation.objects.filter(challenge_id=cid, user=client).exists()

    # And closes it.
    closed = _coded(coach.patch(f"/api/social/challenges/{cid}/", {"status": "completed"},
                                content_type=JSON))
    assert closed.status_code == 200, closed.content
    assert Challenge.objects.get(pk=cid).status == "completed"


def test_a_trainer_hosts_a_challenge_for_own_clients_only(people, api):
    from users.models import TrainerClientRelation

    trainer, client = people
    TrainerClientRelation.objects.create(trainer=trainer, client=client, status="approved",
                                         initiated_by="client", responded_at=timezone.now())
    made = _coded(api(trainer).post("/api/social/challenges/",
                                    _challenge_body(audience="my_clients"), content_type=JSON))
    assert made.status_code == 201, made.content
    cid = made.json()["id"]
    assert api(client).post(f"/api/social/challenges/{cid}/join/", {},
                            content_type=JSON).status_code == 201


def test_a_trainer_joins_someone_elses_challenge(people, api):
    trainer, client = people
    made = api(client).post("/api/social/challenges/", _challenge_body(), content_type=JSON)
    assert made.status_code == 201, made.content
    cid = made.json()["id"]
    coach = api(trainer)
    assert _coded(coach.post(f"/api/social/challenges/{cid}/join/", {},
                             content_type=JSON)).status_code == 201
    other = _coded(coach.patch(f"/api/social/challenges/{cid}/", {"description": "mine"},
                               content_type=JSON))
    assert other.status_code == 403 and other.json()["code"] == "not_owner"


def test_a_trainer_follows_finds_and_reports(people, api):
    from social.models import Post

    trainer, client = people
    coach = api(trainer)
    followed = _coded(coach.post("/api/social/follows/follow_user/", {"user_id": client.pk},
                                 content_type=JSON))
    assert followed.status_code in (200, 201), followed.content
    assert _coded(coach.get("/api/social/follows/followers/")).status_code == 200
    assert _coded(coach.get("/api/social/follows/following/")).status_code == 200
    unfollowed = _coded(coach.post("/api/social/follows/unfollow_user/",
                                   {"user_id": client.pk}, content_type=JSON))
    assert unfollowed.status_code == 200, unfollowed.content

    found = _coded(coach.get("/api/social/users/public-profile/search/?q=Lina"))
    assert found.status_code == 200, found.content
    profile = _coded(coach.get(
        f"/api/social/users/public-profile/by_username/?username={client.username}"))
    assert profile.status_code == 200, profile.content
    assert _coded(coach.get("/api/social/achievements/")).status_code == 200

    post = Post.objects.create(author=client, content="spam spam", visibility="public")
    filed = _coded(coach.post("/api/v1/reports/",
                              {"target_type": "social_post", "target_id": post.pk,
                               "reason": "spam"}, content_type=JSON))
    assert filed.status_code == 201, filed.content


def test_what_a_trainer_may_not_do_answers_with_a_code(people, api, make_user):
    """Another coach's clients-only challenge: 403 `challenge_clients_only` on join, and
    not listed; someone else's comment: 403 `not_owner`."""
    from social.models import Comment, Post

    trainer, client = people
    rival = make_user(_name("rival"), user_type="trainer", first_name="Omar")
    made = api(rival).post("/api/social/challenges/", _challenge_body(audience="my_clients"),
                           content_type=JSON)
    assert made.status_code == 201, made.content
    cid = made.json()["id"]
    coach = api(trainer)
    refused = _coded(coach.post(f"/api/social/challenges/{cid}/join/", {}, content_type=JSON))
    assert refused.status_code == 403 and refused.json()["code"] == "challenge_clients_only"
    listed = coach.get("/api/social/challenges/").json()
    rows = listed["results"] if isinstance(listed, dict) else listed
    assert cid not in {row["id"] for row in rows}

    post = Post.objects.create(author=client, content="Rest day", visibility="public")
    theirs = Comment.objects.create(post=post, author=client, content="Sore legs")
    other = _coded(coach.patch(f"/api/social/comments/{theirs.pk}/", {"content": "x"},
                               content_type=JSON))
    assert other.status_code == 403 and other.json()["code"] == "not_owner"


# ---------------------------------------------------- the routes the tests above miss
# The phase-3 review: the docstring above says every community and challenge route is
# exercised with a trainer's token, and 19 method-and-path pairs were not. Each answers
# a trainer as it answers a client (no route refuses by role); this sweep holds that.


def test_every_other_route_answers_a_trainer(people, api,
                                             django_capture_on_commit_callbacks):
    from achievements.models import Achievement, UserAchievement
    from notifications.models import Notification
    from social.models import Challenge, Comment, Post, UserFollow

    trainer, client = people
    coach = api(trainer)

    # The API's own root.
    assert _coded(coach.get("/api/social/")).status_code == 200

    # Badges: one, and the trainer's own earned list.
    badge = Achievement.objects.create(
        key=_name("badge"), name="First post", description="Post once",
        category="social", criteria={"type": "post_count", "target": 1}, points=10)
    UserAchievement.objects.create(user=trainer, achievement=badge)
    one = _coded(coach.get(f"/api/social/achievements/{badge.pk}/"))
    assert one.status_code == 200 and one.json()["id"] == badge.pk, one.content
    earned = _coded(coach.get("/api/social/achievements/user_achievements/"))
    assert earned.status_code == 200, earned.content
    assert [row["achievement"]["id"] for row in earned.json()["achievements"]] == [badge.pk]

    # A challenge replaced whole (PUT) and deleted.
    made = _coded(coach.post("/api/social/challenges/", _challenge_body(), content_type=JSON))
    assert made.status_code == 201, made.content
    cid = made.json()["id"]
    put = _coded(coach.put(f"/api/social/challenges/{cid}/",
                           _challenge_body(title="Coach's sets, whole"), content_type=JSON))
    assert put.status_code == 200, put.content
    assert Challenge.objects.get(pk=cid).title == "Coach's sets, whole"
    assert coach.delete(f"/api/social/challenges/{cid}/").status_code == 204
    assert not Challenge.objects.filter(pk=cid).exists()

    # A post replaced whole.
    post = _coded(coach.post("/api/social/posts/", {"content": "Hydrate"}, content_type=JSON))
    assert post.status_code == 201, post.content
    pid = post.json()["id"]
    put = _coded(coach.put(f"/api/social/posts/{pid}/", {"content": "Hydrate, always"},
                           content_type=JSON))
    assert put.status_code == 200, put.content
    assert Post.objects.get(pk=pid).content == "Hydrate, always"

    # A comment read alone and replaced whole.
    theirs = Post.objects.create(author=client, content="Rest day", visibility="public")
    comment = _coded(coach.post("/api/social/comments/",
                                {"post": theirs.pk, "content": "Enjoy it"}, content_type=JSON))
    assert comment.status_code == 201, comment.content
    kid = comment.json()["id"]
    read = _coded(coach.get(f"/api/social/comments/{kid}/"))
    assert read.status_code == 200 and read.json()["id"] == kid, read.content
    put = _coded(coach.put(f"/api/social/comments/{kid}/",
                           {"post": theirs.pk, "content": "Enjoy it fully"}, content_type=JSON))
    assert put.status_code == 200, put.content
    assert Comment.objects.get(pk=kid).content == "Enjoy it fully"

    # The follow rows themselves: made, listed, read, replaced, patched, deleted.
    with django_capture_on_commit_callbacks(execute=True):
        follow = _coded(coach.post("/api/social/follows/", {"following_id": client.pk},
                                   content_type=JSON))
    assert follow.status_code == 201, follow.content
    fid = follow.json()["id"]
    listed = _coded(coach.get("/api/social/follows/")).json()
    rows = listed["results"] if isinstance(listed, dict) else listed
    assert fid in {row["id"] for row in rows}
    assert _coded(coach.get(f"/api/social/follows/{fid}/")).status_code == 200
    put = _coded(coach.put(f"/api/social/follows/{fid}/", {"following_id": client.pk},
                           content_type=JSON))
    assert put.status_code == 200, put.content
    patch = _coded(coach.patch(f"/api/social/follows/{fid}/", {}, content_type=JSON))
    assert patch.status_code == 200, patch.content
    assert coach.delete(f"/api/social/follows/{fid}/").status_code == 204
    assert not UserFollow.objects.filter(pk=fid).exists()

    # The trainer's community inbox: a client follows the trainer (`follow_user/` is
    # the route that sends the push).
    with django_capture_on_commit_callbacks(execute=True):
        assert api(client).post("/api/social/follows/follow_user/", {"user_id": trainer.pk},
                                content_type=JSON).status_code == 201
    note = Notification.objects.filter(recipient=trainer).order_by("-id").first()
    assert note is not None
    inbox = _coded(coach.get("/api/social/notifications/")).json()
    rows = inbox["results"] if isinstance(inbox, dict) else inbox
    assert str(note.pk) in {row["id"] for row in rows}
    assert _coded(coach.get(f"/api/social/notifications/{note.pk}/")).status_code == 200
    assert _coded(coach.get("/api/social/notifications/unread_count/")).status_code == 200
    assert _coded(coach.post(f"/api/social/notifications/{note.pk}/mark_read/", {},
                             content_type=JSON)).status_code == 200
    assert _coded(coach.post("/api/social/notifications/mark_all_read/", {},
                             content_type=JSON)).status_code == 200
    note.refresh_from_db()
    assert note.is_read is True

    # Public profiles: the list and one person.
    assert _coded(coach.get("/api/social/users/public-profile/")).status_code == 200
    profile = _coded(coach.get(f"/api/social/users/public-profile/{client.pk}/"))
    assert profile.status_code == 200 and profile.json()["id"] == client.pk, profile.content
