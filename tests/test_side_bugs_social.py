"""`SIDE_BUGS.md` rows in the `social` and `ai_assistant` apps, one test per row.

Each test names the defect it closes and fails against the behaviour that was there:

    S-021  `_update_challenge_rankings` wrote `rank` only from `update_progress`,
           ordered by `-current_value` alone, one full-row `save()` per participant —
           so a joiner who never logged kept `rank=NULL`, tied scores were ordered by
           whatever Postgres returned, and a finished board could still be renumbered.
           `achievements/engine.py challenge_wins` keys the Challenge Winner badge on
           `status='completed'` AND `rank=1`.
    S-027  `interaction_actions` advertised a `leave` action `ChallengeViewSet` did not
           define, so joining was permanent and `participants_count` only ever grew
    S-033  a post's `comments_count` counted comments hidden by moderation, which
           `CommentViewSet` refuses to serve and which nothing decrements on a hide
    S-034  `_iteration_status` sent five bare English literals to the client as
           `status` frames, mid-answer, to an Arabic reader
"""
from datetime import timedelta

import pytest
from django.utils import timezone

pytestmark = pytest.mark.django_db

JSON = "application/json"


# ------------------------------------------------------------------------ helpers
def _running_challenge(owner, tag, **kw):
    """A challenge whose window is open right now."""
    from social.models import Challenge

    now = timezone.now()
    fields = dict(
        title=f"side {tag}", description="a test challenge", challenge_type="workout",
        creator=owner, start_date=now - timedelta(days=2), end_date=now + timedelta(days=5),
        status="active", target_value=100.0, unit="workouts")
    fields.update(kw)
    return Challenge.objects.create(**fields)


def _enter(challenge, user, value, *, joined_at=None):
    """A participation with a chosen value and, when asked, a chosen join moment.

    `joined_at` is `auto_now_add`, so the only way to place two people in a known
    order is to write the column afterwards.
    """
    from social.models import ChallengeParticipation

    row = ChallengeParticipation.objects.create(
        user=user, challenge=challenge, current_value=value)
    if joined_at is not None:
        ChallengeParticipation.objects.filter(pk=row.pk).update(joined_at=joined_at)
        row.refresh_from_db()
    return row


def _places(challenge):
    """{username: rank} straight from the stored column."""
    from social.models import ChallengeParticipation

    return {p.user.username: p.rank
            for p in ChallengeParticipation.objects.filter(
                challenge=challenge).select_related("user")}


def _rank_now(challenge):
    from social.views import ChallengeViewSet

    return ChallengeViewSet()._update_challenge_rankings(challenge)


# ------------------------------------------------------------------------- S-021
def test_a_tie_is_broken_the_same_way_the_leaderboard_breaks_it(make_user, api):
    """S-021. Ranks came out of `order_by('-current_value')`, which is not an order:
    two participants on the same number were handed places in whatever sequence the
    database returned them. `GET challenges/{id}/leaderboard/` and the home card's
    `challenge_rank` (`home/sections.py`) both break that tie by who joined earlier,
    and the stored column has to give the same answer — the Challenge Winner badge
    reads it."""
    owner = make_user("s21_tie_owner", user_type="trainer")
    challenge = _running_challenge(owner, "tie")

    base = timezone.now() - timedelta(days=1)
    # Inserted in the OPPOSITE order to the one they joined in, so a ranking that
    # falls back on the physical order of the table gets it wrong.
    late = make_user("s21_late")
    early = make_user("s21_early")
    _enter(challenge, late, 10.0, joined_at=base + timedelta(minutes=5))
    _enter(challenge, early, 10.0, joined_at=base)
    leader = make_user("s21_leader")
    _enter(challenge, leader, 40.0, joined_at=base + timedelta(minutes=9))

    _rank_now(challenge)
    assert _places(challenge) == {
        "s21_leader": 1, "s21_early": 2, "s21_late": 3}

    # And the board the app is shown says the same thing.
    board = api(early).get(f"/api/social/challenges/{challenge.pk}/leaderboard/")
    assert board.status_code == 200, board.content[:300]
    assert [(row["rank"], row["user"]["username"])
            for row in board.json()["leaderboard"]] == [
        (1, "s21_leader"), (2, "s21_early"), (3, "s21_late")]


def test_someone_who_joined_and_logged_nothing_still_has_a_place(make_user, api):
    """S-021. `rank` was written only by `update_progress`, so in a challenge nobody
    has logged in yet every participant sat at NULL — and a participant on zero is in
    last place, not in no place."""
    from social.models import ChallengeParticipation

    owner = make_user("s21_zero_owner", user_type="trainer")
    challenge = _running_challenge(owner, "zero")
    quiet = make_user("s21_quiet")

    joined = api(quiet).post(f"/api/social/challenges/{challenge.pk}/join/", {},
                             content_type=JSON)
    assert joined.status_code == 201, joined.content[:300]

    row = ChallengeParticipation.objects.get(user=quiet, challenge=challenge)
    assert row.current_value == 0
    assert row.rank == 1, "the only participant is in first place, not in none"

    # And a second silent joiner is placed behind them rather than left at NULL.
    quieter = make_user("s21_quieter")
    assert api(quieter).post(f"/api/social/challenges/{challenge.pk}/join/", {},
                             content_type=JSON).status_code == 201
    assert _places(challenge) == {"s21_quiet": 1, "s21_quieter": 2}


def test_ranking_a_challenge_costs_two_queries_not_one_per_participant(make_user):
    """S-021. One full-row `save()` per participant: a 200-person challenge wrote 200
    rows on every single progress log, and each write clobbered every other column
    from an in-memory copy."""
    from django.db import connection as _conn
    from django.test.utils import CaptureQueriesContext

    owner = make_user("s21_cost_owner", user_type="trainer")
    challenge = _running_challenge(owner, "cost")
    base = timezone.now() - timedelta(days=1)
    for index in range(12):
        _enter(challenge, make_user(f"s21_cost_{index}"), float(index),
               joined_at=base + timedelta(minutes=index))

    with CaptureQueriesContext(_conn) as queries:
        _rank_now(challenge)

    # One SELECT and one bulk UPDATE. It used to be one SELECT and twelve UPDATEs,
    # and the twelve grew with the challenge.
    assert len(queries) <= 4, [q["sql"][:90] for q in queries]
    assert _places(challenge)["s21_cost_11"] == 1


def test_two_ranking_passes_over_the_same_data_agree(make_user, api):
    """S-021. Stability is the whole point: the badge is awarded off `rank=1`, so the
    same standing has to produce the same places every time it is computed."""
    owner = make_user("s21_stable_owner", user_type="trainer")
    challenge = _running_challenge(owner, "stable")
    base = timezone.now() - timedelta(days=1)
    _enter(challenge, make_user("s21_st_a"), 5.0, joined_at=base + timedelta(minutes=3))
    _enter(challenge, make_user("s21_st_b"), 5.0, joined_at=base + timedelta(minutes=1))
    _enter(challenge, make_user("s21_st_c"), 0.0, joined_at=base + timedelta(minutes=2))

    first = _rank_now(challenge)
    snapshot = _places(challenge)
    second = _rank_now(challenge)

    assert first == second
    assert _places(challenge) == snapshot
    assert snapshot == {"s21_st_b": 1, "s21_st_a": 2, "s21_st_c": 3}


def test_a_finished_challenge_is_not_renumbered_afterwards(make_user):
    """S-021. Once the window closes the standing is a record. Nothing may quietly
    move it — least of all the Challenge Winner badge, which keys on `rank=1`."""
    from social.models import Challenge, ChallengeParticipation

    owner = make_user("s21_over_owner", user_type="trainer")
    challenge = _running_challenge(owner, "over")
    base = timezone.now() - timedelta(days=1)
    winner = make_user("s21_over_winner")
    runner = make_user("s21_over_runner")
    _enter(challenge, winner, 50.0, joined_at=base)
    _enter(challenge, runner, 10.0, joined_at=base + timedelta(minutes=1))
    _rank_now(challenge)
    assert _places(challenge) == {"s21_over_winner": 1, "s21_over_runner": 2}

    # The clock runs out, and then the numbers change underneath.
    Challenge.objects.filter(pk=challenge.pk).update(
        end_date=timezone.now() - timedelta(hours=1))
    ChallengeParticipation.objects.filter(user=runner, challenge=challenge).update(
        current_value=900.0)
    over = Challenge.objects.get(pk=challenge.pk)
    assert over.lifecycle_status == "completed"

    _rank_now(over)
    assert _places(over) == {"s21_over_winner": 1, "s21_over_runner": 2}


# ------------------------------------------------------------------------- S-027
def test_a_challenge_can_be_left(make_user, api):
    """S-027. `interaction_actions` named a `leave` action the viewset never defined,
    so the route did not exist: joining was permanent, and `participants_count` was
    only ever recomputed on the way in."""
    from social.models import Challenge, ChallengeParticipation

    owner = make_user("s27_owner", user_type="trainer")
    challenge = _running_challenge(owner, "leave")
    player = make_user("s27_player")
    client = api(player)

    assert client.post(f"/api/social/challenges/{challenge.pk}/join/", {},
                       content_type=JSON).status_code == 201
    assert Challenge.objects.get(pk=challenge.pk).participants_count == 1

    left = client.post(f"/api/social/challenges/{challenge.pk}/leave/", {},
                       content_type=JSON)
    assert left.status_code == 200, left.content[:300]
    assert left.json()["code"] == "left_challenge"
    assert not ChallengeParticipation.objects.filter(
        user=player, challenge=challenge).exists()
    assert Challenge.objects.get(pk=challenge.pk).participants_count == 0

    again = client.post(f"/api/social/challenges/{challenge.pk}/leave/", {},
                        content_type=JSON)
    assert again.status_code == 400
    assert again.json()["code"] == "not_participating"


def test_leaving_closes_the_gap_in_the_standing(make_user, api):
    """S-027 with S-021. A departure that left the ranks alone would leave a board
    reading 1, 3, 4 — and `participants_count` disagreeing with the rows."""
    from social.models import Challenge

    owner = make_user("s27_gap_owner", user_type="trainer")
    challenge = _running_challenge(owner, "gap")
    base = timezone.now() - timedelta(days=1)
    _enter(challenge, make_user("s27_first"), 30.0, joined_at=base)
    quitter = make_user("s27_quitter")
    _enter(challenge, quitter, 20.0, joined_at=base + timedelta(minutes=1))
    _enter(challenge, make_user("s27_third"), 10.0, joined_at=base + timedelta(minutes=2))
    _rank_now(challenge)
    assert _places(challenge)["s27_third"] == 3

    assert api(quitter).post(f"/api/social/challenges/{challenge.pk}/leave/", {},
                             content_type=JSON).status_code == 200
    assert _places(challenge) == {"s27_first": 1, "s27_third": 2}
    assert Challenge.objects.get(pk=challenge.pk).participants_count == 2


def test_a_finished_challenge_cannot_be_left(make_user, api):
    """S-027. Leaving after the end would rewrite a board that has already decided a
    badge, so it is refused with the same `code` `join` answers with."""
    owner = make_user("s27_shut_owner", user_type="trainer")
    now = timezone.now()
    challenge = _running_challenge(owner, "shut", start_date=now - timedelta(days=9),
                                   end_date=now - timedelta(days=1))
    player = make_user("s27_shut_player")
    _enter(challenge, player, 5.0)

    refused = api(player).post(f"/api/social/challenges/{challenge.pk}/leave/", {},
                               content_type=JSON)
    assert refused.status_code == 400
    assert refused.json()["code"] == "challenge_closed"


# ------------------------------------------------------------------------- S-033
def _comment(post, author, text):
    from social.models import Comment

    return Comment.objects.create(post=post, author=author, content=text)


def test_the_comment_badge_counts_only_what_the_viewer_can_open(make_user, api):
    """S-033. `comments_count` is denormalised over every row, `CommentViewSet` serves
    `is_hidden=False` only, and nothing decrements the column when a moderator hides a
    comment — so a post whose replies had all been hidden still advertised them and
    opened on an empty thread. The block-aware subtraction that was already here did
    not cover moderation, and a blocked author's hidden comment must be subtracted
    once, not twice."""
    from moderation.models import Block
    from social.models import Comment, Post

    author = make_user("s33_author")
    viewer = make_user("s33_viewer")
    plain = make_user("s33_plain")
    muted = make_user("s33_muted")

    post = Post.objects.create(author=author, content="hello", visibility="public")
    _comment(post, plain, "readable")
    hidden_by_moderation = _comment(post, make_user("s33_rude"), "hidden")
    _comment(post, muted, "from a blocked account")
    both = _comment(post, muted, "blocked AND hidden")
    Post.objects.filter(pk=post.pk).update(comments_count=4)

    Comment.objects.filter(pk__in=[hidden_by_moderation.pk, both.pk]).update(
        is_hidden=True)
    Block.objects.create(blocker=viewer, blocked=muted)

    client = api(viewer)
    thread = client.get(f"/api/social/comments/?post={post.pk}")
    assert thread.status_code == 200, thread.content[:300]
    body = thread.json()
    rows = body["results"] if isinstance(body, dict) and "results" in body else body
    assert len(rows) == 1, rows

    detail = client.get(f"/api/social/posts/{post.pk}/")
    assert detail.status_code == 200, detail.content[:300]
    assert detail.json()["comments_count"] == len(rows)

    listed = client.get("/api/social/posts/")
    assert listed.status_code == 200
    page = listed.json()
    cards = page["results"] if isinstance(page, dict) and "results" in page else page
    mine = [card for card in cards if card["id"] == post.pk]
    assert mine and mine[0]["comments_count"] == len(rows)


def test_hiding_a_comment_drops_it_from_the_badge_for_everyone(make_user, api):
    """S-033. No block involved: the correction has to run for every viewer, because
    moderation does not depend on who is looking."""
    from social.models import Comment, Post

    author = make_user("s33_solo_author")
    talker = make_user("s33_solo_talker")
    post = Post.objects.create(author=author, content="hi", visibility="public")
    comment = _comment(post, talker, "one reply")
    Post.objects.filter(pk=post.pk).update(comments_count=1)

    client = api(make_user("s33_solo_viewer"))
    assert client.get(f"/api/social/posts/{post.pk}/").json()["comments_count"] == 1

    Comment.objects.filter(pk=comment.pk).update(is_hidden=True)
    assert client.get(f"/api/social/posts/{post.pk}/").json()["comments_count"] == 0


def test_correcting_the_comment_badge_stays_one_query_for_the_page(make_user, api):
    """S-033. The correction is a correlated subquery inside the page's own query
    (`subquery_count`), so it must not grow with the number of posts on the page."""
    from django.db import connection as _conn
    from django.test.utils import CaptureQueriesContext

    from social.models import Post

    author = make_user("s33_qc_author")
    viewer = make_user("s33_qc_viewer")
    client = api(viewer)

    def measure(count, tag):
        for index in range(count):
            post = Post.objects.create(author=author, content=f"{tag}{index}",
                                       visibility="public")
            _comment(post, author, "reply")
            Post.objects.filter(pk=post.pk).update(comments_count=1)
        client.get("/api/social/posts/")  # warm
        with CaptureQueriesContext(_conn) as queries:
            assert client.get("/api/social/posts/").status_code == 200
        return len(queries)

    few = measure(2, "a")
    many = measure(15, "b")
    assert many <= few + 2, (few, many)


# ------------------------------------------------------------------------- S-034
def test_the_assistants_status_frames_are_not_bare_english(make_user):
    """S-034. `_iteration_status` returned five English literals that went straight to
    the client as `status` frames, so an Arabic reader watched "Thinking..." and
    "Almost done..." scroll past in the middle of an Arabic answer. Every frame now
    carries a `code` the app renders itself, and the `content` beside it is
    translated for a build that predates the code."""
    from django.utils import translation

    from ai_assistant.services.chat_service import ITERATION_STATUSES, ChatService

    service = ChatService()

    codes = [service._iteration_status(i)["code"] for i in range(len(ITERATION_STATUSES))]
    assert codes == ["thinking", "checking_data", "analyzing", "looking_deeper",
                     "almost_done"]
    # A loop longer than the table repeats the last frame instead of running off it.
    assert service._iteration_status(99)["code"] == "almost_done"

    with translation.override("ar"):
        for index in range(len(ITERATION_STATUSES)):
            frame = service._iteration_status(index)
            assert frame["type"] == "status"
            assert any("؀" <= ch <= "ۿ" for ch in frame["content"]), frame

    with translation.override("en"):
        assert service._iteration_status(0)["content"] == "Thinking..."
