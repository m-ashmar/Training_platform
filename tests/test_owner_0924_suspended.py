"""The owner's decisions of 2026-09-24 for the social surfaces and weekly goals.

* Decision 8 (P0): "Hide suspended accounts from leaderboards. A suspended account should
  not continue appearing publicly by name/photo. Historical statistics can remain
  internally, but the public leaderboard should exclude suspended users." One predicate
  (`users/visibility.py`), read by every surface that shows one person to another.
* Decision 5: a weekly workout goal's target is calculated from the person's plan, and
  a conflicting typed number is refused.
* Decision 9, for the social app: every refusal names its own `code`.

Each test asserts the new behaviour; each fails on the code before this change.
"""
import json
from datetime import timedelta

import pytest
from django.utils import timezone

pytestmark = pytest.mark.django_db

JSON = "application/json"


@pytest.fixture(autouse=True)
def a_cold_cache():
    """The home payload, the challenge list and `/api/achievements/` are cached."""
    from django.core.cache import cache

    from training_platform.cache import private_cache, public_cache

    for store in (private_cache(), public_cache(), cache):
        store.clear()
    yield
    for store in (private_cache(), public_cache(), cache):
        store.clear()


def _suspend(user, capture, active=False):
    """Suspend (or reinstate) the way an operator does: a `save()` of the account."""
    with capture(execute=True):
        user.is_active = active
        user.save()


def _running_challenge(owner, tag, **kw):
    from social.models import Challenge

    now = timezone.now()
    fields = dict(
        title=f"suspended {tag}", description="a test challenge", challenge_type="workout",
        creator=owner, start_date=now - timedelta(days=2), end_date=now + timedelta(days=5),
        status="active", target_value=100.0, unit="kg")
    fields.update(kw)
    return Challenge.objects.create(**fields)


def _enter(challenge, user, value, *, joined_at, status="joined"):
    from social.models import ChallengeParticipation

    row = ChallengeParticipation.objects.create(
        user=user, challenge=challenge, current_value=value, status=status)
    ChallengeParticipation.objects.filter(pk=row.pk).update(joined_at=joined_at)
    return row


def _rerank(challenge):
    from social.standing import write_places

    return write_places(challenge)


def _board(api, person, challenge):
    resp = api(person).get(f"/api/social/challenges/{challenge.pk}/leaderboard/")
    assert resp.status_code == 200, resp.content
    return resp.json()


# ------------------------------------------------------------------ decision 8
def test_a_suspended_participant_holds_no_place_on_any_surface(
        make_user, api, django_capture_on_commit_callbacks):
    """The board, its `me` row, `user_progress.rank`, the home card and the stored `rank`
    all close the gap the suspended account leaves — at the moment of the suspension —
    and reinstating the account puts it back."""
    from social.models import ChallengeParticipation
    from tests.test_home import _data, _home

    owner = make_user("s8_owner", user_type="trainer")
    lina, maher, samer = make_user("s8_lina"), make_user("s8_maher"), make_user("s8_samer")
    challenge = _running_challenge(owner, "standing")
    base = timezone.now() - timedelta(days=1)
    _enter(challenge, lina, 70.0, joined_at=base)
    held = _enter(challenge, maher, 60.0, joined_at=base + timedelta(minutes=1))
    _enter(challenge, samer, 48.0, joined_at=base + timedelta(minutes=2))
    _rerank(challenge)
    assert [row["user"]["username"] for row in _board(api, samer, challenge)["leaderboard"]] \
        == ["s8_lina", "s8_maher", "s8_samer"]

    _suspend(maher, django_capture_on_commit_callbacks)

    board = _board(api, samer, challenge)
    assert [(row["user"]["username"], row["rank"]) for row in board["leaderboard"]] == \
        [("s8_lina", 1), ("s8_samer", 2)]
    assert board["me"]["rank"] == 2
    detail = api(samer).get(f"/api/social/challenges/{challenge.pk}/").json()
    assert detail["user_progress"]["rank"] == 2
    card = _data(_home(api, samer), "challenge_rank")
    assert (card["rank"], card["participants"]) == (2, 2)
    # The stored ranks moved with the suspension itself — nobody logged or joined.
    stored = dict(ChallengeParticipation.objects.filter(challenge=challenge)
                  .values_list("user__username", "rank"))
    assert stored == {"s8_lina": 1, "s8_maher": None, "s8_samer": 2}
    # The history stays internally.
    held.refresh_from_db()
    assert held.current_value == 60.0

    _suspend(maher, django_capture_on_commit_callbacks, active=True)
    assert [(row["user"]["username"], row["rank"])
            for row in _board(api, samer, challenge)["leaderboard"]] == \
        [("s8_lina", 1), ("s8_maher", 2), ("s8_samer", 3)]
    held.refresh_from_db()
    assert held.rank == 2


def test_the_challenge_winner_badge_never_goes_to_a_suspended_account(
        make_user, django_capture_on_commit_callbacks):
    """A leader suspended while the challenge runs hands rank 1 to the next person, who
    is the winner checked at the close; one suspended after the close keeps the record
    but is never awarded."""
    from achievements.engine import AchievementEngine
    from social.models import ChallengeParticipation
    from social.tasks import award_challenge_winners

    owner = make_user("s8w_owner", user_type="trainer")
    leader, runner = make_user("s8w_leader"), make_user("s8w_runner")
    challenge = _running_challenge(owner, "winner", target_value=50.0)
    base = timezone.now() - timedelta(days=1)
    _enter(challenge, leader, 90.0, joined_at=base, status="completed")
    _enter(challenge, runner, 80.0, joined_at=base + timedelta(minutes=1), status="completed")
    _rerank(challenge)

    _suspend(leader, django_capture_on_commit_callbacks)
    type(challenge).objects.filter(pk=challenge.pk).update(
        end_date=timezone.now() - timedelta(minutes=1))           # the clock closes it
    assert ChallengeParticipation.objects.get(challenge=challenge, user=runner).rank == 1
    assert award_challenge_winners([challenge.pk]) == 1
    assert AchievementEngine._get_metric_value(runner, "challenge_wins", {}) == 1
    assert AchievementEngine._get_metric_value(leader, "challenge_wins", {}) == 0

    # Suspended AFTER the close: the record keeps its rank 1, and still no badge.
    later = _running_challenge(owner, "winner-later", target_value=50.0)
    _enter(later, runner, 90.0, joined_at=base, status="completed")
    _rerank(later)
    type(later).objects.filter(pk=later.pk).update(
        end_date=timezone.now() - timedelta(minutes=1))
    _suspend(runner, django_capture_on_commit_callbacks)
    assert ChallengeParticipation.objects.get(challenge=later, user=runner).rank == 1
    assert award_challenge_winners([later.pk]) == 0
    assert AchievementEngine._get_metric_value(runner, "challenge_wins", {}) == 0


def test_the_points_leaderboard_leaves_a_suspended_account_out(
        make_user, api, django_capture_on_commit_callbacks):
    """`/api/achievements/leaderboard/` and `user_rank`: no place, the gap closes, and
    the cached board is dropped at the suspension."""
    from achievements.models import Achievement, UserAchievement

    top, second, me = make_user("s8p_top"), make_user("s8p_second"), make_user("s8p_me")
    for index, (person, points) in enumerate(((top, 50), (second, 30), (me, 10))):
        badge = Achievement.objects.create(
            key=f"s8p_badge_{index}", name=f"badge {index}", description="a test badge",
            category="social", criteria={"type": "post_count", "target": 10_000},
            points=points)
        UserAchievement.objects.create(user=person, achievement=badge)

    def board():
        resp = api(me).get("/api/achievements/leaderboard/")
        assert resp.status_code == 200, resp.content
        body = resp.json()
        return [(row["username"], row["rank"]) for row in body["leaderboard"]], \
            body["user_rank"]

    assert board() == ([("s8p_top", 1), ("s8p_second", 2), ("s8p_me", 3)], 3)
    _suspend(top, django_capture_on_commit_callbacks)
    assert board() == ([("s8p_second", 1), ("s8p_me", 2)], 2)
    assert UserAchievement.objects.filter(user=top).count() == 1, "badges are kept"
    _suspend(top, django_capture_on_commit_callbacks, active=True)
    assert board()[0][0] == ("s8p_top", 1)


def test_posts_and_comments_of_a_suspended_account_leave_every_thread(make_user, api):
    """The feed, the post list, `?author=`, the detail route, likes and comments — and the
    comment badge agrees with the thread it opens."""
    from social.models import Comment, Post

    gone, other, reader = make_user("s8c_gone"), make_user("s8c_other"), make_user("s8c_reader")
    theirs = Post.objects.create(author=gone, content="by the suspended account",
                                 visibility="public")
    kept = Post.objects.create(author=other, content="by someone else", visibility="public")
    Comment.objects.create(post=kept, author=gone, content="a reply by the suspended one")
    Post.objects.filter(pk=kept.pk).update(comments_count=1)
    type(gone).objects.filter(pk=gone.pk).update(is_active=False)

    ids = {row["id"] for row in api(reader).get("/api/social/posts/").json()["results"]}
    assert theirs.pk not in ids and kept.pk in ids
    feed = api(reader).get("/api/social/posts/feed/").json()["posts"]
    assert theirs.pk not in {row["id"] for row in feed}
    assert api(reader).get("/api/social/posts/", {"author": gone.pk}).json()["results"] == []
    assert api(reader).get(f"/api/social/posts/{theirs.pk}/").status_code == 404
    assert api(reader).post(f"/api/social/posts/{theirs.pk}/like/").status_code == 404
    comment = api(reader).post("/api/social/comments/", data=json.dumps(
        {"post": theirs.pk, "content": "hello"}), content_type=JSON)
    assert comment.status_code == 404 and comment.json()["code"] == "not_found"

    thread = api(reader).get("/api/social/comments/", {"post": kept.pk}).json()["results"]
    assert thread == []
    listed = api(reader).get(f"/api/social/posts/{kept.pk}/").json()
    assert listed["comments_count"] == 0, "the badge opens on the thread it counts"

    type(gone).objects.filter(pk=gone.pk).update(is_active=True)
    assert api(reader).get(f"/api/social/posts/{theirs.pk}/").status_code == 200
    assert len(api(reader).get("/api/social/comments/", {"post": kept.pk})
               .json()["results"]) == 1


def test_follow_lists_and_profile_counts_leave_a_suspended_account_out(make_user, api):
    """Followers, following, the follow rows and the counts on a profile agree; search and
    the profile itself already refused an inactive account and still do."""
    from social.models import UserFollow

    me = make_user("s8f_me", email_verified=True)
    gone = make_user("s8f_gone", email_verified=True, first_name="Gone")
    friend, fan = make_user("s8f_friend"), make_user("s8f_fan")
    for follower, following in ((me, gone), (me, friend), (gone, me), (fan, me)):
        UserFollow.objects.create(follower=follower, following=following)
    type(gone).objects.filter(pk=gone.pk).update(is_active=False)

    following = api(me).get("/api/social/follows/following/").json()
    assert [row["username"] for row in following["following"]] == ["s8f_friend"]
    assert following["count"] == 1
    followers = api(me).get("/api/social/follows/followers/").json()
    assert [row["username"] for row in followers["followers"]] == ["s8f_fan"]
    rows = api(me).get("/api/social/follows/").json()["results"]
    assert gone.pk not in {row["follower"]["id"] for row in rows} | \
        {row["following"]["id"] for row in rows}
    profile = api(friend).get(f"/api/social/users/public-profile/{me.pk}/").json()
    assert (profile["followers_count"], profile["following_count"]) == (1, 1)

    assert api(me).get(f"/api/social/users/public-profile/{gone.pk}/").status_code == 404
    found = api(me).get("/api/social/users/public-profile/search/", {"q": "s8f_go"}).json()
    assert found["results"] == []
    # The follow is kept: reinstating lists it again.
    assert UserFollow.objects.filter(follower=me, following=gone).exists()
    type(gone).objects.filter(pk=gone.pk).update(is_active=True)
    assert api(me).get("/api/social/follows/following/").json()["count"] == 2


def test_a_suspended_creator_is_not_shown_on_their_challenge(make_user, api):
    """The challenge stays for its participants; its creator is served null."""
    gone, reader = make_user("s8k_gone"), make_user("s8k_reader")
    challenge = _running_challenge(gone, "creator")
    type(gone).objects.filter(pk=gone.pk).update(is_active=False)

    detail = api(reader).get(f"/api/social/challenges/{challenge.pk}/")
    assert detail.status_code == 200 and detail.json()["creator"] is None
    listed = {row["id"]: row for row in api(reader).get("/api/social/challenges/")
              .json()["results"]}
    assert listed[challenge.pk]["creator"] is None


def test_a_suspended_trainer_is_not_offered(make_user, api):
    """Not asked by id and not in a client's invitations (the roster already left them
    out, and still does)."""
    from users.models import TrainerClientRelation

    coach = make_user("s8t_coach", user_type="trainer", email_verified=True,
                      trainer_is_available=True)
    client = make_user("s8t_client", email_verified=True)
    TrainerClientRelation.objects.create(trainer=coach, client=client, status="pending",
                                         initiated_by="trainer", agreed_rate=0)
    type(coach).objects.filter(pk=coach.pk).update(is_active=False)

    asked = api(client).post("/api/auth/client/request-trainer/",
                             data=json.dumps({"trainer_id": coach.pk}), content_type=JSON)
    assert asked.status_code == 404 and asked.json()["code"] == "not_found"
    invites = api(client).get("/api/auth/client/trainer-invites/").json()["invitations"]
    assert invites == []
    roster = api(client).get("/api/v1/trainers/").json()["results"]
    assert coach.pk not in {card["id"] for card in roster}


# ------------------------------------------------------------------ decision 9 (social)
def test_every_social_refusal_names_its_own_code(make_user, api):
    """Each of these answered only its status default (`bad_request`,
    `permission_denied`, `validation_error` / `invalid`) — or a 500."""
    from social.models import Comment, Post, UserFollow

    me, other = make_user("s9_me"), make_user("s9_other")
    theirs = Post.objects.create(author=other, content="theirs", visibility="public")
    mine = Post.objects.create(author=me, content="mine", visibility="public")

    missing = api(me).get("/api/social/users/public-profile/by_username/")
    assert (missing.status_code, missing.json()["code"]) == (400, "username_required")
    nobody = api(me).get("/api/social/users/public-profile/by_username/",
                         {"username": "s9_nobody"})
    assert (nobody.status_code, nobody.json()["code"]) == (404, "not_found")

    edit = api(me).patch(f"/api/social/posts/{theirs.pk}/", data=json.dumps(
        {"content": "mine now"}), content_type=JSON)
    assert (edit.status_code, edit.json()["code"]) == (403, "not_owner")

    their_follow = UserFollow.objects.create(follower=other, following=me)
    undo = api(me).delete(f"/api/social/follows/{their_follow.pk}/")
    assert (undo.status_code, undo.json()["code"]) == (403, "not_your_follow")

    first = api(me).post("/api/social/follows/", data=json.dumps(
        {"following_id": other.pk}), content_type=JSON)
    assert first.status_code == 201, first.content
    again = api(me).post("/api/social/follows/", data=json.dumps(
        {"following_id": other.pk}), content_type=JSON)
    assert (again.status_code, again.json()["code"]) == (400, "already_following")
    own = api(me).post("/api/social/follows/", data=json.dumps(
        {"following_id": me.pk}), content_type=JSON)
    assert own.status_code == 400
    assert own.json()["field_errors"]["following_id"][0]["code"] == "cannot_follow_yourself"

    paged = api(me).get("/api/social/notifications/", {"page": 2})
    assert (paged.status_code, paged.json()["code"]) == (400, "cursor_paginated")

    elsewhere = Comment.objects.create(post=theirs, author=other, content="there")
    reply = api(me).post("/api/social/comments/", data=json.dumps(
        {"post": mine.pk, "content": "a reply", "parent_comment": elsewhere.pk}),
        content_type=JSON)
    assert reply.status_code == 400
    assert reply.json()["field_errors"]["parent_comment"][0]["code"] == "reply_not_in_thread"

    challenge = _running_challenge(me, "codes")
    reopened = api(me).patch(f"/api/social/challenges/{challenge.pk}/", data=json.dumps(
        {"status": "active"}), content_type=JSON)
    assert reopened.status_code == 400
    assert reopened.json()["field_errors"]["status"][0]["code"] == "status_follows_dates"


# ------------------------------------------------------------------ decision 5
def _four_day_program(make_user, tag):
    """A client on a 7-day cycle with exercises on days 1-4, started two weeks ago: it
    asks for four training days every week."""
    from home.sections import week_start
    from routine.models import RoutineExercise
    from tests.test_home import _program

    trainer, client, routine, rows = _program(make_user, tag)
    routine.days = 7
    routine.start_date = week_start(timezone.localdate()) - timedelta(days=14)
    routine.save()
    for day in (2, 3, 4):
        RoutineExercise.objects.create(routine=routine, exercise=rows[0].exercise, day=day,
                                       order=day, sets=3, reps=10, weight=20)
    return client, routine


def _goal(api, user, **body):
    fields = {"goal_type": "workout_frequency", "title": "weekly"}
    fields.update(body)
    return api(user).post("/api/analytics/goals/", data=json.dumps(fields),
                          content_type=JSON)


def test_a_weekly_goal_takes_its_target_from_the_program(make_user, api):
    """The target is the program's training days this week — whatever was stored — and
    the progress is counted against it."""
    from analytics.models import UserGoal
    from routine.models import WorkoutSession
    from tests.test_backend_bugs_app_0924_progress import _this_week_begins

    client, routine = _four_day_program(make_user, "s5_program")
    goal = UserGoal.objects.create(user=client, goal_type="workout_frequency",
                                   title="typed long ago", target_value=9)
    begins = _this_week_begins()
    for minutes in (1, 2):
        WorkoutSession.objects.create(
            user=client, routine=routine, day=1, status="completed",
            start_time=begins + timedelta(minutes=minutes),
            end_time=begins + timedelta(minutes=minutes + 40))

    body = api(client).get(f"/api/analytics/goals/{goal.pk}/").json()
    assert (body["target_value"], body["target_source"]) == (4.0, "program")
    assert (body["current_value"], body["progress_percentage"]) == (2.0, 50.0)

    created = _goal(api, client)
    assert created.status_code == 201, created.content
    assert (created.json()["target_value"], created.json()["target_source"]) == \
        (4.0, "program")
    assert _goal(api, client, target_value=4).status_code == 201


def test_a_conflicting_target_is_refused_with_the_expected_one(make_user, api):
    client, _routine = _four_day_program(make_user, "s5_conflict")

    refused = _goal(api, client, target_value=5)
    assert refused.status_code == 400
    body = refused.json()
    assert (body["code"], body["expected_target"], body["target_source"]) == \
        ("goal_target_from_plan", 4, "program")

    goal = _goal(api, client).json()
    patched = api(client).patch(f"/api/analytics/goals/{goal['id']}/", data=json.dumps(
        {"target_value": 6}), content_type=JSON)
    assert (patched.status_code, patched.json()["code"]) == (400, "goal_target_from_plan")
    renamed = api(client).patch(f"/api/analytics/goals/{goal['id']}/", data=json.dumps(
        {"title": "four a week"}), content_type=JSON)
    assert renamed.status_code == 200 and renamed.json()["target_value"] == 4.0


def test_without_a_program_the_profile_decides_and_without_either_it_is_refused(
        make_user, api):
    from analytics.models import UserGoal

    stated = make_user("s5_stated", training_days_per_week=3)
    created = _goal(api, stated)
    assert created.status_code == 201, created.content
    assert (created.json()["target_value"], created.json()["target_source"]) == \
        (3.0, "profile")
    assert _goal(api, stated, target_value=5).json()["code"] == "goal_target_from_plan"

    nothing = make_user("s5_nothing")
    refused = _goal(api, nothing, target_value=4)
    assert (refused.status_code, refused.json()["code"]) == (400, "goal_target_unavailable")
    assert _goal(api, nothing).json()["code"] == "goal_target_unavailable"
    # A goal from before: read at the target it was saved with, and says so.
    old = UserGoal.objects.create(user=nothing, goal_type="workout_frequency",
                                  title="old", target_value=4)
    body = api(nothing).get(f"/api/analytics/goals/{old.pk}/").json()
    assert (body["target_value"], body["target_source"]) == (4.0, "saved")

    # Every other type still needs its target, with the field error it always had.
    custom = api(nothing).post("/api/analytics/goals/", data=json.dumps(
        {"goal_type": "custom", "title": "mine"}), content_type=JSON)
    assert custom.status_code == 400
    assert custom.json()["field_errors"]["target_value"][0]["code"] == "required"
