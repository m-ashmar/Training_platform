"""`SIDE_BUGS.md` rows in social, achievements, challenges and home — one test per row.

Each test names the row it closes and fails against the code that was there:

    S-043  deleting a comment never took it off the post's `comments_count`
    S-044  `challenge_wins` counted a first place in a challenge still running
    S-045  the home card counted a DROPPED rival as ahead of the viewer
    S-073  both follow paths ignored blocks (and `follow_user` pushed to the blocker)
    S-088  the achievements leaderboard: a query per row, no block filter, no total order
    S-098  `home/sections.py LONGEST_SESSION`, an alias nothing read
    S-099  a frozen coaching showed `refused` over a session the server lets you finish
    S-100  a tombstoned newest coach message dropped the whole home card
    S-127  `/api/social/achievements/*` read tables nothing live writes
    S-149  the home cache outlived a session start, a tombstone, a read, a block and a
           relation change
"""
from datetime import timedelta

import pytest
from django.utils import timezone

from tests.test_coaching_months import OWNER_RATE
from tests.test_home import _coached, _data, _freeze, _home, _message, _program

pytestmark = pytest.mark.django_db

JSON = "application/json"


@pytest.fixture(autouse=True)
def owners_rules(settings):
    """The coaching month the freeze tests below run on (the owner, 2026-09-19)."""
    settings.COACHING_COMMISSION_RATE = OWNER_RATE
    settings.COACHING_PERIOD_DAYS = 30
    settings.COACHING_RENEWAL_REMINDER_DAYS = 3
    settings.COACHING_FROZEN_REMINDER_DAYS = (3, 7, 14, 30)
    settings.SUBSCRIPTION_RENEWAL_NOTICE_DAYS = 3


@pytest.fixture(autouse=True)
def a_cold_cache():
    """The home payload is cached for 120 s in the private cache, which outlives a test."""
    from training_platform.cache import private_cache

    private_cache().clear()
    yield
    private_cache().clear()


def _rows(body):
    return body["results"] if isinstance(body, dict) and "results" in body else body


def _running_challenge(owner, tag, **kw):
    from social.models import Challenge

    now = timezone.now()
    fields = dict(
        title=f"fx {tag}", description="a test challenge", challenge_type="workout",
        creator=owner, start_date=now - timedelta(days=2), end_date=now + timedelta(days=5),
        status="active", target_value=10.0, unit="workouts")
    fields.update(kw)
    return Challenge.objects.create(**fields)


def _badge(key, *, points=10, criteria=None, category="challenge", **kw):
    from achievements.models import Achievement

    return Achievement.objects.create(
        key=key, name=f"badge {key}", description="a test badge", category=category,
        criteria=criteria or {"type": "post_count", "target": 10_000}, points=points, **kw)


# ------------------------------------------------------------------------- S-043
def test_s043_deleting_a_comment_takes_it_and_its_replies_off_the_badge(make_user, api):
    from social.models import Comment, Post

    author = make_user("fx43_author")
    commenter = make_user("fx43_commenter")
    post = Post.objects.create(author=author, content="hello", visibility="public")

    made = api(commenter).post("/api/social/comments/",
                               {"post": post.pk, "content": "first!"}, content_type=JSON)
    assert made.status_code == 201, made.content
    reply = api(author).post("/api/social/comments/",
                             {"post": post.pk, "content": "thanks",
                              "parent_comment": made.json()["id"]}, content_type=JSON)
    assert reply.status_code == 201, reply.content
    post.refresh_from_db()
    assert post.comments_count == 2

    gone = api(commenter).delete(f"/api/social/comments/{made.json()['id']}/")
    assert gone.status_code == 204, gone.content

    post.refresh_from_db()
    assert not Comment.objects.filter(post=post).exists(), "the reply goes with its parent"
    assert post.comments_count == 0, "a deleted comment stayed on the badge"
    detail = api(author).get(f"/api/social/posts/{post.pk}/").json()
    assert detail["comments_count"] == 0


# ------------------------------------------------------------------------- S-073
def test_s073_a_blocked_pair_cannot_follow_each_other_by_either_path(make_user, api):
    from moderation.models import Block
    from social.models import UserFollow

    blocker = make_user("fx73_blocker")
    blocked = make_user("fx73_blocked")
    Block.objects.create(blocker=blocker, blocked=blocked)

    for person, target in ((blocked, blocker), (blocker, blocked)):
        viaaction = api(person).post("/api/social/follows/follow_user/",
                                     {"user_id": target.pk}, content_type=JSON)
        assert (viaaction.status_code, viaaction.json().get("code")) == (403, "user_blocked"), \
            viaaction.content
        viacreate = api(person).post("/api/social/follows/",
                                     {"following_id": target.pk}, content_type=JSON)
        assert (viacreate.status_code, viacreate.json().get("code")) == (403, "user_blocked"), \
            viacreate.content

    assert not UserFollow.objects.filter(follower__in=[blocker, blocked]).exists()

    # Lifting the block lifts the refusal: nothing else about following changed.
    Block.objects.filter(blocker=blocker, blocked=blocked).delete()
    again = api(blocked).post("/api/social/follows/follow_user/",
                              {"user_id": blocker.pk}, content_type=JSON)
    assert again.status_code == 201, again.content


# ------------------------------------------------------------------------- S-127
def test_s127_the_social_achievement_routes_serve_the_badges_the_engine_awards(
        make_user, api):
    from achievements.models import UserAchievement

    person = make_user("fx127_person")
    earned = _badge("fx127_earned", points=30, category="social")
    secret = _badge("fx127_secret", points=5, category="social", is_secret=True)
    UserAchievement.objects.create(user=person, achievement=earned)

    mine = api(person).get("/api/social/achievements/user_achievements/")
    assert mine.status_code == 200, mine.content
    body = mine.json()
    assert [row["achievement"]["id"] for row in body["achievements"]] == [earned.pk], \
        "an awarded badge never reached the social route"
    assert body["total_points"] == 30
    assert body["achievements"][0]["achievement"]["name"] == earned.name

    one = api(person).get(f"/api/social/achievements/{earned.pk}/")
    assert one.status_code == 200, one.content
    assert (one.json()["id"], one.json()["points"]) == (earned.pk, 30)
    # A secret badge is shown to nobody who has not earned it.
    assert api(person).get(f"/api/social/achievements/{secret.pk}/").status_code == 404

    import importlib.util

    assert importlib.util.find_spec("social.services") is None, "the dead engine is back"


# ------------------------------------------------------------------------- S-044
def test_s044_the_winner_badge_waits_for_the_challenge_to_end(make_user, api,
                                                             django_capture_on_commit_callbacks):
    from achievements.engine import AchievementEngine, trigger_achievement_check
    from achievements.models import UserAchievement
    from social.models import Challenge, ChallengeParticipation
    from social.tasks import advance_challenge_phases

    badge = _badge("fx44_winner", points=100,
                   criteria={"type": "challenge_wins", "target": 1})
    owner = make_user("fx44_owner", user_type="trainer")
    leader = make_user("fx44_leader")
    runner = make_user("fx44_runner")
    # Setup (owner, 2026-09-24, F20): only a RANKED challenge — counted by the server,
    # created by a trainer or staff — can award the badge, and only after its review
    # window (`Challenge.REVIEW_HOURS`). The two challenges below are counted ones
    # (`metric="sets"`), and "the clock runs out" moves past the review window too.
    challenge = _running_challenge(owner, "s044", metric="sets")
    review = timedelta(hours=Challenge.REVIEW_HOURS)
    ChallengeParticipation.objects.create(user=leader, challenge=challenge,
                                          current_value=10.0, status="completed", rank=1)
    ChallengeParticipation.objects.create(user=runner, challenge=challenge,
                                          current_value=4.0, rank=2)

    def holds(person):
        return UserAchievement.objects.filter(user=person, achievement=badge).exists()

    # First place in a challenge that is still running is a lead, not a win.
    trigger_achievement_check(leader, "challenge_joined")
    assert AchievementEngine._get_metric_value(leader, "challenge_wins", {}) == 0
    assert not holds(leader), "the badge fired on a temporary first place"

    # The clock runs out; the hourly phase task closes it and checks its winner.
    Challenge.objects.filter(pk=challenge.pk).update(
        end_date=timezone.now() - review - timedelta(minutes=5))
    advance_challenge_phases()
    assert Challenge.objects.get(pk=challenge.pk).status == "completed"
    assert holds(leader), "nobody checked the winner when the challenge closed"
    assert not holds(runner)

    # A creator closing a challenge early closes it too, and its winner is checked then.
    early = make_user("fx44_early")
    second = _running_challenge(owner, "s044b", metric="sets")
    ChallengeParticipation.objects.create(user=early, challenge=second,
                                          current_value=10.0, status="completed", rank=1)
    assert not holds(early)
    with django_capture_on_commit_callbacks(execute=True):
        closed = api(owner).patch(f"/api/social/challenges/{second.pk}/",
                                  {"status": "completed"}, content_type=JSON)
    assert closed.status_code == 200, closed.content
    # The close ended it now; its review window then runs out (F20).
    Challenge.objects.filter(pk=second.pk).update(
        end_date=timezone.now() - review - timedelta(minutes=5))
    advance_challenge_phases()
    assert holds(early)

    # A cancelled challenge was never won.
    cancelled = _running_challenge(owner, "s044c", status="cancelled",
                                   end_date=timezone.now() - timedelta(days=1))
    loser = make_user("fx44_cancelled")
    ChallengeParticipation.objects.create(user=loser, challenge=cancelled,
                                          current_value=10.0, status="completed", rank=1)
    assert AchievementEngine._get_metric_value(loser, "challenge_wins", {}) == 0


# ------------------------------------------------------------------------- S-045
def test_s045_a_dropped_rival_holds_no_place_anywhere(make_user, api):
    from social.models import ChallengeParticipation
    from social.views import ChallengeViewSet

    owner = make_user("fx45_owner", user_type="trainer")
    person = make_user("fx45_person")
    quitter = make_user("fx45_quitter")
    ahead = make_user("fx45_ahead")
    challenge = _running_challenge(owner, "s045", participants_count=3)
    mine = ChallengeParticipation.objects.create(user=person, challenge=challenge,
                                                 current_value=12.0)
    gone = ChallengeParticipation.objects.create(user=quitter, challenge=challenge,
                                                 current_value=20.0, status="dropped", rank=1)
    front = ChallengeParticipation.objects.create(user=ahead, challenge=challenge,
                                                  current_value=15.0)

    card = _data(_home(api, person), "challenge_rank")
    assert card is not None
    assert card["rank"] == 2, "a dropped rival was still counted ahead"

    board = api(person).get(f"/api/social/challenges/{challenge.pk}/leaderboard/").json()
    names = [row["user"]["username"] for row in board["leaderboard"]]
    assert names == ["fx45_ahead", "fx45_person"]
    assert [row["rank"] for row in board["leaderboard"]] == [1, card["rank"]]

    ChallengeViewSet()._update_challenge_rankings(challenge)
    ranks = {row.pk: row.rank for row in ChallengeParticipation.objects.filter(
        challenge=challenge)}
    assert ranks == {front.pk: 1, mine.pk: 2, gone.pk: None}, \
        "the quitter kept rank 1, and with it the winner badge's rank"


# ------------------------------------------------------------------------- S-088
def test_s088_the_achievement_leaderboard_is_one_query_ordered_and_block_aware(
        make_user, api):
    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    from achievements.models import UserAchievement
    from moderation.models import Block

    viewer = make_user("fx88_viewer")
    ten = _badge("fx88_ten", points=10)
    twenty = _badge("fx88_twenty", points=20)
    tied_a = make_user("fx88_tied_a")
    tied_b = make_user("fx88_tied_b")
    top = make_user("fx88_top")
    for person, badge in ((tied_a, ten), (tied_b, ten), (top, twenty)):
        UserAchievement.objects.create(user=person, achievement=badge)
    Block.objects.create(blocker=viewer, blocked=top)

    def board():
        resp = api(viewer).get("/api/achievements/leaderboard/?limit=50")
        assert resp.status_code == 200, resp.content
        return resp.json()["leaderboard"]

    rows = [row for row in board() if row["username"].startswith("fx88_")]
    assert "fx88_top" not in [row["username"] for row in rows], \
        "a blocked account's name reached the person who blocked it"
    tied = [row for row in rows if row["username"] in ("fx88_tied_a", "fx88_tied_b")]
    assert [row["user_id"] for row in tied] == sorted([tied_a.pk, tied_b.pk])
    assert [row["rank"] for row in tied] == [2, 3], "ranks are counted before the drop"

    # The cost does not follow the number of rows on the board. Each side is measured
    # on a second, warm call: a write invalidates the ORM cache (cachalot) for the
    # table, so a cold call after the inserts would measure cachalot, not the board.
    # Users are never cached (`users_customuser` is uncachable), so a query per row
    # shows on a warm call as surely as on a cold one.
    board()
    with CaptureQueriesContext(connection) as small:
        board()
    for index in range(8):
        extra = make_user(f"fx88_extra_{index}")
        UserAchievement.objects.create(user=extra, achievement=ten)
    assert len(board()) >= 10
    with CaptureQueriesContext(connection) as large:
        board()
    assert len(large.captured_queries) <= len(small.captured_queries) + 2, \
        (len(small.captured_queries), [q["sql"][:120] for q in large.captured_queries])


# ------------------------------------------------------------------------- S-098
def test_s098_home_holds_no_second_name_for_the_session_rule():
    import home.sections as sections
    from routine.sessions import running

    assert not hasattr(sections, "LONGEST_SESSION")
    assert not hasattr(sections, "SESSION_MAX_SECONDS")
    assert sections.running_sessions is running


# ------------------------------------------------------------------------- S-099
def test_s099_a_session_begun_while_paid_is_continued_after_the_freeze(make_user, api):
    from home.sections import current_routine
    from routine.models import WorkoutSession

    trainer, client, relation, routine, rows = _coached(make_user, api, "fx99",
                                                        funds="100.00")
    today = current_routine(client, timezone.localdate())
    begun = api(client).post("/api/routine/workout-sessions/",
                             {"routine": today.pk, "day": 1}, content_type=JSON)
    assert begun.status_code == 201, begun.content
    first = _freeze(relation, ago=timedelta(hours=2))
    WorkoutSession.objects.filter(pk=begun.json()["id"]).update(
        start_time=first.ends_at - timedelta(minutes=30))

    hero = _data(_home(api, client), "workout_hero")
    assert hero["cta"] == {"state": "continue", "session_id": begun.json()["id"]}, \
        "a dead button over a workout the server still accepts"
    assert hero["session"]["id"] == begun.json()["id"]

    # The server agrees: the workout it offered to continue can be finished.
    finished = api(client).patch(f"/api/routine/workout-sessions/{begun.json()['id']}/",
                                 {"status": "completed"}, content_type=JSON)
    assert finished.status_code == 200, finished.content

    # And with no open session the freeze still refuses, as before.
    from training_platform.cache import private_cache

    private_cache().clear()
    hero = _data(_home(api, client), "workout_hero")
    assert hero["cta"] == {"state": "refused", "refusal": "coaching_frozen"}


# ------------------------------------------------------------------------- S-100
def test_s100_a_tombstoned_newest_message_keeps_the_coach_card(make_user, api):
    from chat.services import tombstone

    trainer, client, relation, routine, rows = _coached(make_user, api, "fx100")
    _message(trainer, client, body="Rest today.")
    newest = _message(trainer, client, body="Actually, train.")
    tombstone(newest, trainer)

    card = _data(_home(api, client), "trainer_message")
    assert card is not None, "the card vanished while the chat list still counts unread"
    assert card["message"]["id"] == newest.pk
    assert (card["message"]["body"], card["message"]["deleted"]) == ("", True)
    assert card["unread_count"] == 1

    listed = api(client).get("/api/v1/chat/conversations/").json()
    thread = _rows(listed)[0]
    assert thread["unread_count"] == card["unread_count"]
    assert thread["last_message"]["deleted"] is True


# ------------------------------------------------------------------------- S-149
def test_s149_starting_a_session_shows_on_the_home_at_once(make_user, api,
                                                           django_capture_on_commit_callbacks):
    trainer, client, routine, rows = _program(make_user, "fx149s")
    assert _data(_home(api, client), "workout_hero")["cta"] == {"state": "start"}

    with django_capture_on_commit_callbacks(execute=True):
        started = api(client).post("/api/routine/workout-sessions/",
                                   {"routine": routine.pk, "day": 1}, content_type=JSON)
    assert started.status_code == 201, started.content

    hero = _data(_home(api, client), "workout_hero")
    assert hero["cta"] == {"state": "continue", "session_id": started.json()["id"]}, \
        "the cached hero still said start: a second tap opens a second session"


def test_s149_a_tombstone_a_read_and_a_block_show_on_the_home_at_once(
        make_user, api, django_capture_on_commit_callbacks):
    trainer, client, relation, routine, rows = _coached(make_user, api, "fx149c")
    _message(trainer, client, body="Rest today.")
    newest = _message(trainer, client, body="Add 2.5 kg on the press.")

    card = _data(_home(api, client), "trainer_message")
    assert (card["message"]["body"], card["unread_count"]) == ("Add 2.5 kg on the press.", 2)

    with django_capture_on_commit_callbacks(execute=True):
        assert api(trainer).delete(f"/api/v1/chat/messages/{newest.pk}/").status_code \
            in (200, 204)
    card = _data(_home(api, client), "trainer_message")
    assert card["message"]["deleted"] is True, "the home kept quoting a deleted message"
    assert card["message"]["body"] == ""

    with django_capture_on_commit_callbacks(execute=True):
        read = api(client).post(f"/api/v1/chat/conversations/{card['conversation_id']}/read/")
    assert read.status_code == 200, read.content
    assert _data(_home(api, client), "trainer_message")["unread_count"] == 0, \
        "the home badge still counted messages the client had read"

    with django_capture_on_commit_callbacks(execute=True):
        blocked = api(client).post("/api/v1/blocks/", {"user_id": trainer.pk},
                                   content_type=JSON)
    assert blocked.status_code == 201, blocked.content
    assert _data(_home(api, client), "trainer_message") is None, \
        "a coach the client just blocked stayed on the home card"

    with django_capture_on_commit_callbacks(execute=True):
        assert api(client).delete(f"/api/v1/blocks/{trainer.pk}/").status_code == 204
    assert _data(_home(api, client), "trainer_message") is not None


def test_s149_a_relation_change_retires_the_clients_home(make_user,
                                                        django_capture_on_commit_callbacks):
    from home.views import _stamp
    from users.models import TrainerClientRelation

    trainer = make_user("fx149r_trainer", user_type="trainer")
    client = make_user("fx149r_client")
    before = _stamp(client.pk)
    with django_capture_on_commit_callbacks(execute=True):
        relation = TrainerClientRelation.objects.create(trainer=trainer, client=client,
                                                        status="pending")
    created = _stamp(client.pk)
    assert created > before, "a new relation left the cached home in place"

    with django_capture_on_commit_callbacks(execute=True):
        relation.status = "approved"
        relation.save()
    assert _stamp(client.pk) > created, "an approval left the cached home in place"
