"""`manage.py seed_walk_states` (Road to 10, the app's walk-throughs): the framework.

It refuses outside the development settings and without its password; a second run puts
every walk account back into its starting state (fresh accounts, the old ones retired,
nothing deleted); `--only` resets one state alone; no account outside `walk.*` changes;
and the manifest matches the database and holds no secret.
"""
import io
import json
import uuid

import pytest
from django.core.management import CommandError, call_command

pytestmark = pytest.mark.django_db

PASSWORD = "Walk-Throughs-2026!"


@pytest.fixture
def walk_env(monkeypatch, tmp_path):
    monkeypatch.setenv("WALK_ACCOUNTS_PASSWORD", PASSWORD)
    return tmp_path / "walk_states.json"


@pytest.fixture
def probe_state():
    """A second state, registered for the test only: a trainer and a client of it."""
    from users.walk import STATES, load_states, state

    load_states()

    @state("framework_probe", area="inbox", role="trainer",
           summary="A trainer and one client (the framework's own test).")
    def framework_probe(walk):
        coach = walk.account()
        client = walk.account("client")
        walk.ids(client=client.pk, coach=coach.pk)

    yield "framework_probe"
    STATES.pop("framework_probe", None)


def _run(manifest, *args):
    out = io.StringIO()
    call_command("seed_walk_states", "--manifest", str(manifest), *args, stdout=out)
    return json.loads(manifest.read_text(encoding="utf-8")), out.getvalue()


def _shape(user):
    """What the member_with_post state looks like for its main account."""
    from social.models import Comment, Post

    posts = Post.objects.filter(author=user)
    return {"active": user.is_active, "posts": posts.count(),
            "comments_on_them": Comment.objects.filter(post__in=posts).count(),
            "role": user.user_type, "verified": user.email_verified}


def test_it_refuses_under_production_settings(walk_env, settings):
    settings.SETTINGS_MODULE = "training_platform.settings_production"
    with pytest.raises(CommandError, match="development-only"):
        _run(walk_env)
    assert not walk_env.exists()


def test_it_refuses_without_its_password(monkeypatch, tmp_path):
    monkeypatch.delenv("WALK_ACCOUNTS_PASSWORD", raising=False)
    with pytest.raises(CommandError, match="WALK_ACCOUNTS_PASSWORD"):
        _run(tmp_path / "walk.json")


def test_a_second_run_gives_the_same_starting_state(walk_env):
    from users.models import CustomUser

    first, _out = _run(walk_env)
    entry = first["states"]["member_with_post"]
    before = CustomUser.objects.get(pk=entry["ids"]["user"])
    assert before.email == "walk.member_with_post@yalla.test"
    assert before.check_password(PASSWORD)
    shape = _shape(before)
    assert shape == {"active": True, "posts": 1, "comments_on_them": 1, "role": "client",
                     "verified": True}

    second, _out = _run(walk_env)
    again = second["states"]["member_with_post"]
    assert again["ids"]["user"] != entry["ids"]["user"]
    fresh = CustomUser.objects.get(pk=again["ids"]["user"])
    assert fresh.email == entry["email"]
    assert _shape(fresh) == shape

    # The old account is retired, not deleted: off its address, switched off, and
    # everything it wrote still there.
    before.refresh_from_db()
    assert before.is_active is False and before.email.startswith("walk.retired.")
    assert not before.has_usable_password()
    assert _shape(before)["posts"] == 1


def test_only_resets_one_state(walk_env, probe_state):
    first, _out = _run(walk_env)
    assert set(first["states"]) >= {"member_with_post", probe_state}
    second, _out = _run(walk_env, "--only", "member_with_post")
    assert second["states"][probe_state] == first["states"][probe_state]
    assert second["states"]["member_with_post"]["ids"]["user"] != \
        first["states"]["member_with_post"]["ids"]["user"]


def test_an_unknown_state_is_refused(walk_env):
    with pytest.raises(CommandError, match="No such walk state"):
        _run(walk_env, "--only", "nothing_like_it")


def test_no_account_outside_walk_is_touched(walk_env, make_user):
    """The builders' accounts (`samer@yalla.test` ...), a person, and an address that
    only LOOKS like a walk one."""
    from users.models import CustomUser

    others = [make_user(f"wf_{tag}_{uuid.uuid4().hex[:4]}") for tag in ("person", "coach")]
    for email in ("samer@yalla.test", "walkman@yalla.test", "walk.member_with_post@gmail.com"):
        user = make_user(f"wf_{uuid.uuid4().hex[:8]}")
        CustomUser.objects.filter(pk=user.pk).update(email=email)
        others.append(user)
    snapshot = {u.pk: CustomUser.objects.filter(pk=u.pk).values().get() for u in others}

    _run(walk_env)
    _run(walk_env)
    for pk, row in snapshot.items():
        now = CustomUser.objects.filter(pk=pk).values().get()
        assert now == row, f"account {pk} changed"


def test_the_manifest_matches_the_database_and_holds_no_secret(walk_env, probe_state):
    from users.models import CustomUser

    manifest, _out = _run(walk_env)
    text = walk_env.read_text(encoding="utf-8")
    assert PASSWORD not in text
    assert "password" not in text.lower() and "token" not in text.lower()
    for name, entry in manifest["states"].items():
        assert entry["name"] == name
        main = CustomUser.objects.get(pk=entry["ids"]["user"])
        assert (main.email, main.user_type, main.is_active) == (entry["email"],
                                                                entry["role"], True)
        for account in entry["accounts"].values():
            user = CustomUser.objects.get(pk=account["id"])
            assert (user.email, user.user_type, user.is_active) == (account["email"],
                                                                    account["role"], True)
            assert user.email.startswith("walk.") and user.email.endswith("@yalla.test")
