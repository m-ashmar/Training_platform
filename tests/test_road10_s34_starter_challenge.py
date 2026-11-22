"""Road to 10 S3.4: a challenge to join on day one, and a limit on hosting.

- `manage.py create_starter_challenge --creator <staff>` opens the platform's starter
  challenge: community, auto-counted (`training_days`), ranked, Arabic and English, and
  running now. Run again, it creates nothing while one is open.
- One member hosts at most `CHALLENGE_HOST_LIMIT` (3, the owner, 2026-11-22) challenges
  at once; the next create answers 409 `challenge_host_limit` with the limit. Any
  member may still create one.

On the code before this change the command does not exist and a fourth challenge is
created.
"""
import io
import uuid
from datetime import timedelta

import pytest
from django.core.management import CommandError, call_command
from django.utils import timezone

pytestmark = pytest.mark.django_db

JSON = "application/json"


def _name(tag):
    return f"s34_{tag}_{uuid.uuid4().hex[:6]}"


@pytest.fixture(autouse=True)
def _fresh_lists():
    from django.core.cache import cache

    cache.clear()
    yield
    cache.clear()


def _body(n=0, **extra):
    now = timezone.now()
    body = {"title": f"Hosted {n}", "description": "Sets this month",
            "challenge_type": "strength",
            "start_date": (now - timedelta(hours=1)).isoformat(),
            "end_date": (now + timedelta(days=10)).isoformat(),
            "target_value": 10, "unit": "sets", "metric": "manual"}
    body.update(extra)
    return body


def _create(api, who, n=0, **extra):
    return api(who).post("/api/social/challenges/", _body(n, **extra), content_type=JSON)


def _run(*args):
    out = io.StringIO()
    call_command("create_starter_challenge", *args, stdout=out)
    return out.getvalue()


# ------------------------------------------------------------------ the starter
def test_the_operator_opens_a_starter_every_member_can_join(make_user, api):
    from social.models import Challenge

    operator = make_user(_name("ops"), is_staff=True)
    client = make_user(_name("c"))
    out = _run("--creator", operator.username)
    assert "Created 1" in out

    starter = Challenge.objects.get(creator=operator)
    assert starter.audience == Challenge.AUDIENCE_COMMUNITY
    assert starter.metric == "training_days" and starter.auto_counted
    assert starter.ranked
    assert starter.is_active
    assert starter.target_value == 12
    assert (starter.end_date - starter.start_date).days == 30
    assert starter.title_ar and starter.title_en and starter.title_ar != starter.title_en
    assert starter.description_ar and starter.description_en

    listed = api(client).get("/api/social/challenges/").json()
    rows = listed["results"] if isinstance(listed, dict) else listed
    assert starter.pk in {row["id"] for row in rows}
    joined = api(client).post(f"/api/social/challenges/{starter.pk}/join/", {},
                              content_type=JSON)
    assert joined.status_code == 201, joined.content


def test_a_second_run_creates_nothing_while_one_is_open(make_user):
    from social.models import Challenge

    operator = make_user(_name("ops2"), is_staff=True)
    _run("--creator", operator.username)
    again = _run("--creator", operator.email)
    assert "Created 0" in again
    assert Challenge.objects.filter(creator=operator).count() == 1

    _run("--creator", operator.username, "--force")
    assert Challenge.objects.filter(creator=operator).count() == 2


def test_a_closed_starter_lets_the_next_one_open(make_user):
    from social.models import Challenge

    operator = make_user(_name("ops3"), is_staff=True)
    _run("--creator", operator.username)
    Challenge.objects.filter(creator=operator).update(status="completed")
    assert "Created 1" in _run("--creator", operator.username, "--days", "14",
                               "--target", "6")
    newest = Challenge.objects.filter(creator=operator).order_by("-id").first()
    assert newest.target_value == 6
    assert "6 أيام" in newest.title_ar


def test_the_creator_must_be_an_active_operator(make_user):
    member = make_user(_name("member"))
    with pytest.raises(CommandError, match="is not staff"):
        _run("--creator", member.username)
    with pytest.raises(CommandError, match="No active account"):
        _run("--creator", "nobody-at-all")
    operator = make_user(_name("ops4"), is_staff=True)
    with pytest.raises(CommandError, match="--target must be"):
        _run("--creator", operator.username, "--days", "5", "--target", "9")


# ------------------------------------------------------------------ the limit
def test_a_member_hosts_at_most_three_at_once(make_user, api):
    from social.models import Challenge

    client = make_user(_name("host"))
    for n in range(3):
        made = _create(api, client, n)
        assert made.status_code == 201, made.content

    refused = _create(api, client, 3)
    assert refused.status_code == 409, refused.content
    body = refused.json()
    assert body["code"] == "challenge_host_limit"
    assert body["limit"] == 3
    assert body["hosting"] == 3
    assert Challenge.objects.filter(creator=client).count() == 3


def test_closing_one_or_its_end_frees_a_place(make_user, api):
    from social.models import Challenge

    client = make_user(_name("free"))
    ids = [_create(api, client, n).json()["id"] for n in range(3)]
    assert _create(api, client, 3).status_code == 409

    closed = api(client).patch(f"/api/social/challenges/{ids[0]}/",
                               {"status": "completed"}, content_type=JSON)
    assert closed.status_code == 200, closed.content
    assert _create(api, client, 4).status_code == 201

    # One that ended by its date does not count either.
    Challenge.objects.filter(pk=ids[1]).update(
        start_date=timezone.now() - timedelta(days=5),
        end_date=timezone.now() - timedelta(days=1))
    assert _create(api, client, 5).status_code == 201
    assert _create(api, client, 6).status_code == 409


def test_an_upcoming_one_counts_and_a_hidden_one_does_not(make_user, api):
    from social.models import Challenge

    client = make_user(_name("soon"))
    later = timezone.now() + timedelta(days=3)
    for n in range(3):
        assert _create(api, client, n, start_date=later.isoformat(),
                       end_date=(later + timedelta(days=7)).isoformat()).status_code == 201
    assert _create(api, client, 3).status_code == 409

    hidden =Challenge.objects.filter(creator=client).order_by("id").first()
    Challenge.objects.filter(pk=hidden.pk).update(is_hidden=True)
    assert _create(api, client, 4).status_code == 201


def test_a_trainer_is_limited_the_same_and_staff_are_not(make_user, api):
    trainer = make_user(_name("t"), user_type="trainer", first_name="Rami")
    for n in range(3):
        assert _create(api, trainer, n).status_code == 201
    refused = _create(api, trainer, 3, audience="my_clients")
    assert refused.status_code == 409 and refused.json()["code"] == "challenge_host_limit"

    operator = make_user(_name("staff"), is_staff=True)
    for n in range(4):
        assert _create(api, operator, n).status_code == 201


def test_the_limit_is_a_setting(make_user, api, settings):
    settings.CHALLENGE_HOST_LIMIT = 1
    client = make_user(_name("one"))
    assert _create(api, client, 0).status_code == 201
    refused = _create(api, client, 1)
    assert refused.status_code == 409 and refused.json()["limit"] == 1

    settings.CHALLENGE_HOST_LIMIT = 0
    assert _create(api, client, 2).status_code == 201


def test_the_default_is_the_owners_three():
    from django.conf import settings

    assert settings.CHALLENGE_HOST_LIMIT == 3
