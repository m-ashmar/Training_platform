"""BACKEND_BUGS B-074..B-081 and B-086: what the app session found wiring the community
screens (challenges, leaderboards, follows, posts, profiles, achievements, search),
2026-09-24 — one test per row, each asserting the behaviour the row asked for.

B-086 is asserted as far as it was built: the three surfaces read one function and
agree. The owner's rule for ties (they share a place) is not made yet — the tests in
`tests/test_side_bugs_social.py` pin the current one — so nothing here pins which rule
it is, only that every surface answers the same place.
"""
from datetime import datetime, timedelta

import pytest
from django.utils import timezone

from tests.test_home import _data, _home, _log_sets, _program

pytestmark = pytest.mark.django_db

JSON = "application/json"


@pytest.fixture(autouse=True)
def a_cold_cache():
    """The home payload and the challenge list are cached; every test starts cold."""
    from django.core.cache import cache

    from training_platform.cache import private_cache

    private_cache().clear()
    cache.clear()
    yield
    private_cache().clear()
    cache.clear()


def _running_challenge(owner, tag, **kw):
    """A challenge whose window is open right now."""
    from social.models import Challenge

    now = timezone.now()
    fields = dict(
        title=f"community {tag}", description="a test challenge", challenge_type="workout",
        creator=owner, start_date=now - timedelta(days=2), end_date=now + timedelta(days=5),
        status="active", target_value=100_000.0, unit="kg")
    fields.update(kw)
    return Challenge.objects.create(**fields)


def _enter(challenge, user, value, *, joined_at, rank=None):
    """A participation written the way a seed or an operator writes one: the value and
    a stored `rank` of its own, and nothing re-ranked."""
    from social.models import ChallengeParticipation

    row = ChallengeParticipation.objects.create(
        user=user, challenge=challenge, current_value=value, rank=rank)
    ChallengeParticipation.objects.filter(pk=row.pk).update(joined_at=joined_at)
    return row


def _badge(key, *, points=10, name=None, **kw):
    from achievements.models import Achievement

    return Achievement.objects.create(
        key=key, name=name or f"badge {key}", description="a test badge",
        category="challenge", criteria={"type": "post_count", "target": 10_000},
        points=points, **kw)


def _board(api, person, challenge, **query):
    resp = api(person).get(f"/api/social/challenges/{challenge.pk}/leaderboard/", query)
    assert resp.status_code == 200, resp.content
    return resp.json()


# ------------------------------------------------------------------------ B-086
def test_one_standing_answers_one_place_on_every_surface(make_user, api):
    """Live on samer (70, 70, 48, 0): `user_progress.rank` said 2, the leaderboard row
    3 and the home card 3 — the Challenge screen's «مركزك» contradicted the board row
    under it. The Feed card read the stored column, which a seed had written."""
    owner = make_user("b086_owner", user_type="trainer")
    lina, maher = make_user("b086_lina"), make_user("b086_maher")
    samer, rami = make_user("b086_samer"), make_user("b086_rami")
    challenge = _running_challenge(owner, "b086")
    base = timezone.now() - timedelta(days=1)
    # Stored ranks as the live data had them: dense, and stale.
    _enter(challenge, lina, 70.0, joined_at=base, rank=1)
    _enter(challenge, maher, 70.0, joined_at=base + timedelta(minutes=1), rank=1)
    _enter(challenge, samer, 48.0, joined_at=base + timedelta(minutes=2), rank=2)
    _enter(challenge, rami, 0.0, joined_at=base + timedelta(minutes=3), rank=3)

    board = {row["user"]["username"]: row["rank"]
             for row in _board(api, samer, challenge)["leaderboard"]}
    for person in (lina, maher, samer, rami):
        detail = api(person).get(f"/api/social/challenges/{challenge.pk}/")
        assert detail.status_code == 200, detail.content
        feed_rank = detail.json()["user_progress"]["rank"]
        home_rank = _data(_home(api, person), "challenge_rank")["rank"]
        me = _board(api, person, challenge)["me"]["rank"]
        assert feed_rank == board[person.username] == home_rank == me, (
            person.username, feed_rank, board[person.username], home_rank, me)

    # Two people are ahead of samer under any rule for ties.
    assert board["b086_samer"] == 3


# ------------------------------------------------------------------------ B-074
def test_the_board_draws_names_pictures_the_trainer_ring_and_my_own_row(make_user, api):
    """Rows carried only `{id, username}`, capped at 10, and the caller ranked lower had
    no row at all."""
    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    owner = make_user("b074_owner", user_type="trainer")
    challenge = _running_challenge(owner, "b074")
    base = timezone.now() - timedelta(days=1)
    coach = make_user("b074_coach", user_type="trainer", first_name="Rami",
                      last_name="Haddad", profile_picture="profile_pictures/rami.jpg")
    _enter(challenge, coach, 500.0, joined_at=base)
    for index in range(2):
        _enter(challenge, make_user(f"b074_small_{index}"), 400.0 - index,
               joined_at=base + timedelta(minutes=index + 1))
    me = make_user("b074_me", first_name="Samer")
    _enter(challenge, me, 1.0, joined_at=base + timedelta(minutes=30))

    _board(api, me, challenge)  # warm: a write invalidates the ORM cache
    with CaptureQueriesContext(connection) as small:
        _board(api, me, challenge)

    for index in range(10):
        _enter(challenge, make_user(f"b074_more_{index}"), 300.0 - index,
               joined_at=base + timedelta(minutes=index + 10))
    body = _board(api, me, challenge)
    with CaptureQueriesContext(connection) as large:
        body = _board(api, me, challenge)
    assert len(large.captured_queries) <= len(small.captured_queries) + 2, \
        (len(small.captured_queries), len(large.captured_queries))

    rows = body["leaderboard"]
    assert len(rows) == 10, "the cap stays 10 by default"
    top = rows[0]["user"]
    assert (top["first_name"], top["last_name"], top["user_type"]) == \
        ("Rami", "Haddad", "trainer")
    assert top["profile_picture"].startswith("http") and \
        "/profile_pictures/rami.jpg" in top["profile_picture"]
    assert me.pk not in [row["user"]["id"] for row in rows]
    assert body["me"]["rank"] == 14 and body["me"]["user"]["first_name"] == "Samer"
    assert body["me"]["current_value"] == 1.0

    everyone = _board(api, me, challenge, limit=50)["leaderboard"]
    assert [row["rank"] for row in everyone] == list(range(1, 15))


# ------------------------------------------------------------------------ B-075
def test_challenge_progress_is_counted_from_logged_training(
        make_user, api, django_capture_on_commit_callbacks):
    """`update_progress` was the only write to `current_value`: a challenge that the
    design counts from logged sets never moved unless its participant typed a number."""
    from social.models import ChallengeParticipation

    trainer, client, routine, rows = _program(make_user, "b075", sets=5, reps=10,
                                              weight=40)
    challenge = _running_challenge(trainer, "b075", metric="volume_kg",
                                   target_value=1_000.0)

    # Training already logged inside the window counts from the moment of joining.
    with django_capture_on_commit_callbacks(execute=True):
        _log_sets(client, api, rows[0], 1, reps=10, weight=40)
    joined = api(client).post(f"/api/social/challenges/{challenge.pk}/join/", {},
                              content_type=JSON)
    assert joined.status_code == 201, joined.content
    row = ChallengeParticipation.objects.get(user=client, challenge=challenge)
    assert row.current_value == 400.0

    # Every set logged afterwards moves it, with no manual step.
    with django_capture_on_commit_callbacks(execute=True):
        _log_sets(client, api, rows[0], 2, reps=10, weight=40, start=2)
    row.refresh_from_db()
    assert row.current_value == 1_200.0
    assert row.progress_percentage == 100.0 and row.status == "completed"

    # The other measures, from the same training: sets and days from the set logs, a
    # retried upload of set 3 (no client id) counted once; workouts from sessions.
    from routine.models import WorkoutSession

    counted = {}
    for metric in ("sets", "training_days", "workouts"):
        counted[metric] = _running_challenge(trainer, f"b075{metric}", metric=metric,
                                             target_value=50.0)
        assert api(client).post(f"/api/social/challenges/{counted[metric].pk}/join/", {},
                                content_type=JSON).status_code == 201
    with django_capture_on_commit_callbacks(execute=True):
        _log_sets(client, api, rows[0], 1, reps=10, weight=40, start=3)
        WorkoutSession.objects.create(user=client, routine=routine, day=1,
                                      status="completed",
                                      start_time=timezone.now() - timedelta(hours=1),
                                      end_time=timezone.now())
    assert {metric: ChallengeParticipation.objects.get(
        user=client, challenge=counted[metric]).current_value for metric in counted} == \
        {"sets": 3.0, "training_days": 1.0, "workouts": 1.0}
    row.refresh_from_db()
    assert row.current_value == 1_200.0, "a re-sent set is one set"

    detail = api(client).get(f"/api/social/challenges/{challenge.pk}/").json()
    assert detail["auto_counted"] is True and detail["metric"] == "volume_kg"
    assert detail["user_progress"]["current_value"] == 1_200.0
    assert _board(api, client, challenge)["me"]["current_value"] == 1_200.0

    # The manual log is refused for what the server counts, and a hand-typed number
    # does not replace the counted one.
    typed = api(client).post(f"/api/social/challenges/{challenge.pk}/update_progress/",
                             {"current_value": 99_999}, content_type=JSON)
    assert typed.status_code == 400 and typed.json()["code"] == "progress_auto_counted"
    row.refresh_from_db()
    assert row.current_value == 1_200.0

    # A manual challenge stays manual — until its creator says what it measures, and
    # then everyone in it is counted at once, not at their next set.
    manual = _running_challenge(trainer, "b075m")
    assert api(client).get(f"/api/social/challenges/{manual.pk}/").json()[
        "auto_counted"] is False
    assert api(client).post(f"/api/social/challenges/{manual.pk}/join/", {},
                            content_type=JSON).status_code == 201
    switched = api(trainer).patch(f"/api/social/challenges/{manual.pk}/",
                                  {"metric": "sets"}, content_type=JSON)
    assert switched.status_code == 200, switched.content
    assert switched.json()["auto_counted"] is True
    assert ChallengeParticipation.objects.get(
        user=client, challenge=manual).current_value == 3.0


# ------------------------------------------------------------------------ B-076
def test_join_and_update_progress_refusals_carry_a_code(make_user, api):
    """'Challenge is full' and the `update_progress` 400s answered no `code`, while
    `leave` answers `not_participating` / `challenge_closed` for the same situations."""
    owner = make_user("b076_owner", user_type="trainer")
    first, second = make_user("b076_first"), make_user("b076_second")
    full = _running_challenge(owner, "b076f", max_participants=1)
    assert api(first).post(f"/api/social/challenges/{full.pk}/join/", {},
                           content_type=JSON).status_code == 201
    refused = api(second).post(f"/api/social/challenges/{full.pk}/join/", {},
                               content_type=JSON)
    assert refused.status_code == 400 and refused.json()["code"] == "challenge_full"

    def log(person, challenge, body):
        return api(person).post(f"/api/social/challenges/{challenge.pk}/update_progress/",
                                body, content_type=JSON)

    open_one = _running_challenge(owner, "b076o")
    assert log(second, open_one, {"current_value": 5}).json()["code"] == "not_participating"
    assert api(second).post(f"/api/social/challenges/{open_one.pk}/join/", {},
                            content_type=JSON).status_code == 201
    assert log(second, open_one, {}).json()["code"] == "progress_required"
    assert log(second, open_one, {"current_value": "lots"}).json()["code"] == \
        "invalid_progress"
    assert log(second, open_one, {"current_value": 5}).status_code == 200

    from social.models import Challenge

    Challenge.objects.filter(pk=open_one.pk).update(
        end_date=timezone.now() - timedelta(minutes=1))
    closed = log(second, open_one, {"current_value": 6})
    assert closed.status_code == 400 and closed.json()["code"] == "challenge_closed"


# ------------------------------------------------------------------------ B-077
def test_another_members_profile_shows_featured_badges_points_and_join_month(
        make_user, api):
    """The artboard draws «أبرز إنجازاته», the points column and «منذ أيار»; the public
    profile carried none of them, and `is_featured` was readable only on your own
    `achievements/my/`."""
    from achievements.models import UserAchievement
    from moderation.models import Block

    viewer = make_user("b077_viewer")
    laith = make_user("b077_laith")
    type(laith).objects.filter(pk=laith.pk).update(
        date_joined=timezone.make_aware(datetime(2026, 5, 17, 9, 30)))
    hundred = _badge("b077_hundred", points=50)
    streak = _badge("b077_streak", points=30)
    hidden = _badge("b077_secret", points=20, is_secret=True)
    plain = _badge("b077_plain", points=5)
    for badge, featured in ((hundred, True), (streak, True), (hidden, True),
                            (plain, False)):
        UserAchievement.objects.create(user=laith, achievement=badge, is_featured=featured)

    profile = api(viewer).get(f"/api/social/users/public-profile/{laith.pk}/")
    assert profile.status_code == 200, profile.content
    body = profile.json()
    assert {row["id"] for row in body["featured_achievements"]} == {hundred.pk, streak.pk}, \
        "a secret badge reached someone who has not earned it"
    assert body["community_points"] == 105
    assert body["member_since"] == "2026-05-01"

    # Their own profile shows the secret one they chose to feature.
    own = api(laith).get(f"/api/social/users/public-profile/{laith.pk}/").json()
    assert hidden.pk in {row["id"] for row in own["featured_achievements"]}

    # And a block still hides all of it.
    Block.objects.create(blocker=laith, blocked=viewer)
    assert api(viewer).get(
        f"/api/social/users/public-profile/{laith.pk}/").status_code == 404


# ------------------------------------------------------------------------ B-078
def test_catalogue_rows_say_featured_and_my_rows_name_the_badge(make_user, api):
    """Catalogue rows had no `is_featured`; `my/` rows were keyed by the earned row with
    no achievement id, while `feature/` takes the achievement id — and names are not
    unique, so matching by name read a duplicate as not featured."""
    from achievements.models import UserAchievement

    person = make_user("b078_person")
    first = _badge("b078_first", name="Same name")
    second = _badge("b078_second", name="Same name")
    UserAchievement.objects.create(user=person, achievement=first)
    UserAchievement.objects.create(user=person, achievement=second)

    featured = api(person).post(f"/api/achievements/{second.pk}/feature/", {},
                                content_type=JSON)
    assert featured.status_code == 200 and featured.json()["is_featured"] is True

    catalogue = {row["id"]: row for row in
                 api(person).get("/api/achievements/").json()["achievements"]}
    assert catalogue[second.pk]["is_featured"] is True
    assert catalogue[first.pk]["is_featured"] is False

    mine = api(person).get("/api/achievements/my/").json()["achievements"]
    by_badge = {row["achievement_id"]: row for row in mine}
    assert set(by_badge) == {first.pk, second.pk}
    assert by_badge[second.pk]["is_featured"] is True
    assert by_badge[first.pk]["is_featured"] is False

    unearned = _badge("b078_unearned")
    refused = api(person).post(f"/api/achievements/{unearned.pk}/feature/", {},
                               content_type=JSON)
    assert refused.status_code == 404 and refused.json()["code"] == "achievement_not_earned"


# ------------------------------------------------------------------------ B-079
def test_follow_refusals_carry_codes_a_bad_id_is_a_400_and_inactive_is_missing(
        make_user, api):
    """Self-follow's 400 and the 404 had no `code`; `"abc"` reached
    `CustomUser.objects.get(id=...)` and was a 500; following an inactive account
    succeeded."""
    from social.models import UserFollow

    me = make_user("b079_me")
    gone = make_user("b079_gone")
    type(gone).objects.filter(pk=gone.pk).update(is_active=False)

    def follow(user_id, route="follow_user"):
        return api(me).post(f"/api/social/follows/{route}/", {"user_id": user_id},
                            content_type=JSON)

    own = follow(me.pk)
    assert own.status_code == 400 and own.json()["code"] == "cannot_follow_yourself"
    for bad in ("abc", None, -3, True):
        answer = follow(bad)
        assert answer.status_code == 400 and answer.json()["code"] == "invalid_user_id", bad
    missing = follow(10 ** 8)
    assert missing.status_code == 404 and missing.json()["code"] == "user_not_found"

    inactive = follow(gone.pk)
    assert inactive.status_code == 404 and inactive.json()["code"] == "user_not_found"
    assert not UserFollow.objects.filter(follower=me, following=gone).exists()

    assert follow("abc", "unfollow_user").json()["code"] == "invalid_user_id"
    assert follow(10 ** 8, "unfollow_user").json()["code"] == "user_not_found"
    assert follow(gone.pk, "unfollow_user").status_code == 200, \
        "a follow of an inactive account must stay undoable"


# ------------------------------------------------------------------------ B-080
def test_a_post_carries_the_finished_workout_or_the_badge_it_shares(make_user, api):
    """`content_type`/`object_id` were on the model and neither serialized nor
    writable, so the share card publishing attached was dropped."""
    from achievements.models import UserAchievement
    from routine.models import ExerciseSetLog, UserExerciseProgress, WorkoutSession

    trainer, client, routine, rows = _program(make_user, "b080")
    stranger = make_user("b080_stranger")
    began = timezone.now() - timedelta(minutes=52)
    session = WorkoutSession.objects.create(user=client, routine=routine, day=1,
                                            status="completed", start_time=began,
                                            end_time=began + timedelta(minutes=52))
    progress = UserExerciseProgress.objects.create(
        user=client, exercise=rows[0].exercise, date=timezone.localdate(), target_sets=3)
    for number, (weight, reps) in enumerate(((60, 10), (60, 8), (0, 12)), start=1):
        ExerciseSetLog.objects.create(user_exercise_progress=progress,
                                      workout_session=session, set_number=number,
                                      weight=weight, reps=reps, date=progress.date)

    def publish(person, **share):
        return api(person).post("/api/social/posts/",
                                {"content": "five of six this week", **share},
                                content_type=JSON)

    made = publish(client, share_workout_session=session.pk)
    assert made.status_code == 201, made.content
    assert made.json()["post_type"] == "workout"
    card = {"kind": "workout",
            "workout": {"title": routine.name, "volume_kg": 1080.0, "minutes": 52}}
    assert made.json()["share"] == card

    listed = api(stranger).get("/api/social/posts/").json()["results"]
    assert [p["share"] for p in listed if p["id"] == made.json()["id"]] == [card]

    stolen = publish(stranger, share_workout_session=session.pk)
    assert stolen.status_code == 400
    assert stolen.json()["field_errors"]["share_workout_session"][0]["code"] == \
        "share_not_yours"
    running = WorkoutSession.objects.create(user=client, routine=routine, day=1)
    unfinished = publish(client, share_workout_session=running.pk)
    assert unfinished.json()["field_errors"]["share_workout_session"][0]["code"] == \
        "share_workout_unfinished"

    badge = _badge("b080_badge", points=25)
    unearned = publish(client, share_achievement=badge.pk)
    assert unearned.json()["field_errors"]["share_achievement"][0]["code"] == \
        "share_badge_not_earned"
    UserAchievement.objects.create(user=client, achievement=badge)
    shared = publish(client, share_achievement=badge.pk)
    assert shared.status_code == 201, shared.content
    assert shared.json()["share"]["kind"] == "achievement"
    assert shared.json()["share"]["achievement"]["id"] == badge.pk


# ------------------------------------------------------------------------ B-081
def test_find_people_by_name_or_username_prefix_without_the_hidden_ones(make_user, api):
    """`by_username/` was an exact, case-sensitive username match and nothing searched
    names; the Find screen needs a real search."""
    from moderation.models import Block

    me = make_user("b081_me")
    laith = make_user("LaithN_b081", first_name="Laith", last_name="Najjar")
    hamdan = make_user("hamdan_b081", first_name="Laith", last_name="Hamdan",
                       user_type="trainer")
    ali = make_user("ali_b081", first_name="Laith", last_name="Ali")
    blocker = make_user("blocker_b081", first_name="Laith", last_name="Blocker")
    gone = make_user("gone_b081", first_name="Laith", last_name="Gone")
    type(gone).objects.filter(pk=gone.pk).update(is_active=False)
    Block.objects.create(blocker=me, blocked=ali)
    Block.objects.create(blocker=blocker, blocked=me)

    def search(q, **extra):
        return api(me).get("/api/social/users/public-profile/search/", {"q": q, **extra})

    found = search("laith")
    assert found.status_code == 200, found.content
    body = found.json()
    assert {"count", "next", "previous", "results"} <= set(body)
    names = [row["username"] for row in body["results"]]
    assert names[0] == "LaithN_b081", "a username match comes before name matches"
    assert set(names) == {"LaithN_b081", "hamdan_b081"}, \
        "an inactive account, or one blocked either way, was listed"

    assert [row["username"] for row in search("@laithn_B").json()["results"]] == \
        ["LaithN_b081"], "case-insensitive, and a leading @ is not part of the name"
    assert [row["username"] for row in search("laith h").json()["results"]] == \
        ["hamdan_b081"]
    row = search("HAMDAN").json()["results"][0]
    assert set(row) == {"id", "username", "user_type", "first_name", "last_name",
                        "profile_picture_url", "followers_count", "is_following",
                        "member_since"}
    assert row["user_type"] == "trainer"
    assert search("b081_me").json()["count"] == 0, "you do not find yourself"

    short = search(" l ")
    assert short.status_code == 400 and short.json()["code"] == "query_too_short"
    assert short.json()["min_length"] == 2
