"""Two social routes the app never calls, from the read-only review of 2026-11-23, each
proven by a probe before its fix and failing on the code before it:

  HIGH    GET users/public-profile/ (the plain list) paged every active account — staff,
          top-up agents and unverified sign-ups included — which `search/` never lists
          (the owner's decision 7)
  MEDIUM  PUT/PATCH follows/{id}/ re-pointed a follow with `following_id`: onto someone
          who had blocked the caller (the block is checked on create only), or onto a
          pair the caller already had — a 500
"""
import uuid

import pytest

pytestmark = pytest.mark.django_db

JSON = "application/json"
PROFILES = "/api/social/users/public-profile/"


def _name(tag):
    return f"srr_{tag}_{uuid.uuid4().hex[:6]}"


def _every_page(api, viewer, url):
    seen = []
    while url:
        body = api(viewer).get(url).json()
        seen += [row["id"] for row in body["results"]]
        url = body.get("next")
    return seen


def test_the_profile_list_shows_only_who_search_may_show(make_user, api):
    from moderation.models import Block

    viewer = make_user(_name("viewer"), email_verified=True)
    shown = {
        "member": make_user(_name("member"), email_verified=True),
        "trainer": make_user(_name("trainer"), user_type="trainer", email_verified=True),
    }
    never = {
        "staff": make_user(_name("staff"), user_type="admin", email_verified=True),
        "agent": make_user(_name("agent"), user_type="agent", email_verified=True),
        "unverified": make_user(_name("unverified"), email_verified=False),
        "suspended": make_user(_name("suspended"), email_verified=True, is_active=False),
        "i_blocked": make_user(_name("iblocked"), email_verified=True),
        "blocked_me": make_user(_name("blockedme"), email_verified=True),
    }
    Block.objects.create(blocker=viewer, blocked=never["i_blocked"])
    Block.objects.create(blocker=never["blocked_me"], blocked=viewer)

    seen = _every_page(api, viewer, f"{PROFILES}?page_size=100")
    assert len(seen) == len(set(seen)), "a person was listed twice across pages"
    assert {person.pk for person in shown.values()} <= set(seen)
    assert not {kind for kind, person in never.items() if person.pk in seen}
    assert viewer.pk not in seen

    # The rows are the full public profile, as they always were.
    row = api(viewer).get(f"{PROFILES}?page_size=100").json()["results"][0]
    assert {"followers_count", "following_count", "posts_count", "is_following",
            "featured_achievements", "community_points", "member_since"} <= set(row)

    # Not listed is not hidden: a staff account's profile still opens by id.
    assert api(viewer).get(f"{PROFILES}{never['staff'].pk}/").status_code == 200


def test_the_list_and_search_list_the_same_people(make_user, api):
    viewer = make_user(_name("v2"), email_verified=True)
    for tag in ("zed", "zoe"):
        make_user(_name(f"zz{tag}"), email_verified=True)
    make_user(_name("zzagent"), user_type="agent", email_verified=True)
    listed = {i for i in _every_page(api, viewer, f"{PROFILES}?page_size=100")}
    found = {i for i in _every_page(api, viewer, f"{PROFILES}search/?q=srr_zz&page_size=100")}
    assert found and found <= listed


# ------------------------------------------------------------------ follows
@pytest.mark.parametrize("method", ["patch", "put"])
def test_a_follow_is_not_re_pointed_onto_someone_who_blocked_you(make_user, api, method):
    from moderation.models import Block
    from social.models import UserFollow

    me, friend, blocker = (make_user(_name(t)) for t in ("me", "friend", "blocker"))
    follow = UserFollow.objects.create(follower=me, following=friend)
    Block.objects.create(blocker=blocker, blocked=me)

    resp = getattr(api(me), method)(f"/api/social/follows/{follow.pk}/",
                                     {"following_id": blocker.pk}, content_type=JSON)
    assert (resp.status_code, resp.json()["code"]) == (400, "follow_not_editable")
    follow.refresh_from_db()
    assert follow.following_id == friend.pk
    assert not UserFollow.objects.filter(follower=me, following=blocker).exists()


@pytest.mark.parametrize("method", ["patch", "put"])
def test_a_follow_re_pointed_onto_a_pair_you_have_is_no_500(make_user, api, method):
    from social.models import UserFollow

    me, a, b = (make_user(_name(t)) for t in ("me2", "a", "b"))
    first = UserFollow.objects.create(follower=me, following=a)
    UserFollow.objects.create(follower=me, following=b)
    resp = getattr(api(me), method)(f"/api/social/follows/{first.pk}/",
                                     {"following_id": b.pk}, content_type=JSON)
    assert (resp.status_code, resp.json()["code"]) == (400, "follow_not_editable")
    assert UserFollow.objects.filter(follower=me).count() == 2


def test_someone_elses_follow_is_still_not_yours(make_user, api):
    from social.models import UserFollow

    follower, followed = make_user(_name("f")), make_user(_name("g"))
    follow = UserFollow.objects.create(follower=follower, following=followed)
    resp = api(followed).patch(f"/api/social/follows/{follow.pk}/", {}, content_type=JSON)
    assert (resp.status_code, resp.json()["code"]) == (403, "not_your_follow")
    assert api(follower).delete(f"/api/social/follows/{follow.pk}/").status_code == 204
