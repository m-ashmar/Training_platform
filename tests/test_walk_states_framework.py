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

from tests.walk_fixtures import WALK_PASSWORD as PASSWORD
from tests.walk_fixtures import walk_platform  # noqa: F401  (the shared fixture)

pytestmark = pytest.mark.django_db


@pytest.fixture
def walk_env(walk_platform, tmp_path):
    """A database as a dev server has it after bootstrap_platform, media in tmp_path,
    the password set (`tests/walk_fixtures.py`), and a manifest path."""
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


_capture = None


@pytest.fixture(autouse=True)
def _commits(django_capture_on_commit_callbacks):
    """Every run's after-commit work (recounts, notices) runs, as it does outside a test."""
    global _capture
    _capture = django_capture_on_commit_callbacks
    yield
    _capture = None


def _run(manifest, *args):
    out = io.StringIO()
    with _capture(execute=True):
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

    # Every state comes back with the same shape: the same ids recorded, the same
    # accounts, all of them fresh.
    for name, was in first["states"].items():
        now = second["states"][name]
        assert set(now["ids"]) == set(was["ids"]), name
        assert set(now["accounts"]) == set(was["accounts"]), name
        assert not {a["id"] for a in now["accounts"].values()} & \
            {a["id"] for a in was["accounts"].values()}, name

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


def test_nothing_but_the_command_knows_the_walk_accounts():
    """Rule zero: a walk account is served what any account is. No source outside the
    command and `users/walk/` imports the walk package or names its address prefix."""
    from tests.test_bootstrap_commands import _first_party_sources

    allowed = ("users/walk/", "users/management/commands/seed_walk_states.py")
    readers = sorted(str(rel) for rel, text in _first_party_sources()
                     if ("users.walk" in text or "@yalla.test" in text
                         or "walk_states" in text)
                     and not str(rel).startswith(allowed))
    assert readers == []


def test_it_refuses_once_on_a_database_not_bootstrapped(monkeypatch, tmp_path):
    """ONE message before any state, naming what is missing; nothing written."""
    from users.models import CustomUser

    monkeypatch.setenv("WALK_ACCOUNTS_PASSWORD", PASSWORD)
    manifest = tmp_path / "walk.json"
    with pytest.raises(CommandError, match="has not been bootstrapped") as refused:
        _run(manifest)
    assert "bootstrap_platform" in str(refused.value)
    assert "starter exercises" in str(refused.value)
    assert not manifest.exists()
    assert not CustomUser.objects.filter(email__startswith="walk.").exists()


def test_it_refuses_a_database_not_on_this_machine(walk_env, monkeypatch):
    """The development settings read DB_HOST from the environment: the module alone does
    not keep the command off a remote server."""
    from django.db import connections

    monkeypatch.setitem(connections["default"].settings_dict, "HOST", "db.example.com")
    with pytest.raises(CommandError, match="not this machine"):
        _run(walk_env)
    assert not walk_env.exists()


def _what_real_people_see(make_user, api, tag):
    """What a real client and a real operator are shown, as the ids of walk accounts in
    it, and the operator's queue counts."""
    from social.models import Challenge
    from users.models import CustomUser
    from wallet.models import AgentProfile

    person = make_user(f"wf_real_{tag}_{uuid.uuid4().hex[:4]}")
    operator = make_user(f"wf_ops_{tag}_{uuid.uuid4().hex[:4]}", user_type="admin")
    retired = set(CustomUser.objects.filter(email__startswith="walk.retired.")
                  .values_list("pk", flat=True))

    def ids(path, key="id"):
        body = api(person).get(path).json()
        rows = body["results"] if isinstance(body, dict) and "results" in body else body
        return {row[key] for row in rows if isinstance(row, dict) and key in row}

    trainers = ids("/api/v1/trainers/?page_size=100")
    challenges = ids("/api/social/challenges/?page_size=100")
    hosts = set(Challenge.objects.filter(pk__in=challenges).values_list("creator_id",
                                                                          flat=True))
    agents = set(AgentProfile.objects.filter(pk__in=ids("/api/wallet/agents/?page_size=100"))
                 .values_list("user_id", flat=True))
    from routine.products import catalogue_for

    sellers = set(catalogue_for(person).values_list("trainer_id", flat=True))
    queues = {row["key"]: row["count"] for row in
              api(operator).get("/api/v1/ops/queue-counts/").json()["queues"]}
    return {"retired_seen": {"directory": trainers & retired, "challenges": hosts & retired,
                             "agents": agents & retired, "catalogue": sellers & retired},
            "queues": queues}


def test_after_a_second_run_nothing_retired_is_seen(walk_env, make_user, api):
    """Item by item, where a real person looks: the trainer directory, the challenge list,
    the agents near you, the programmes on sale — and the operator's queues, which a reset
    must not fill (an open report, a withdrawal, a verification, a product in review left
    by a retired walk account is still on the real operator's desk)."""
    _run(walk_env)
    first = _what_real_people_see(make_user, api, "one")
    _run(walk_env)
    second = _what_real_people_see(make_user, api, "two")
    assert all(not seen for seen in second["retired_seen"].values()), second["retired_seen"]
    assert second["queues"] == first["queues"], (first["queues"], second["queues"])


# ------------------------------------------------------------------ the developers' database
def _developers_shape():
    """What the ordinary `@dev.test` accounts of `walk_platform` hold: their own rows."""
    from django.db.models import Q

    from subscription.models import Subscription
    from tests.walk_fixtures import DEVELOPERS_DOMAIN
    from users.models import CoachingPeriod, CustomUser, TrainerClientRelation
    from wallet.models import Transaction, Wallet

    people = CustomUser.objects.filter(email__endswith=f"@{DEVELOPERS_DOMAIN}")
    ids = list(people.values_list("pk", flat=True))
    assert len(ids) == 4, "walk_platform makes four ordinary accounts"
    return {
        "people": sorted(people.values_list("email", "is_active", "user_type",
                                            "preferred_language")),
        "wallets": sorted(Wallet.objects.filter(owner_id__in=ids)
                          .values_list("owner_id", "currency", "balance")),
        "ledger": Transaction.objects.filter(Q(source_wallet__owner_id__in=ids)
                                             | Q(destination_wallet__owner_id__in=ids)).count(),
        "relations": sorted(TrainerClientRelation.objects.filter(client_id__in=ids)
                            .values_list("client_id", "status")),
        "periods": sorted(CoachingPeriod.objects.filter(client_id__in=ids)
                          .values_list("client_id", "number", "status", "ends_at")),
        "subscriptions": sorted(Subscription.objects.filter(user_id__in=ids)
                                .values_list("user_id", "status", "plan_id", "end_date")),
    }


def test_every_state_builds_on_a_database_shaped_like_the_developers(walk_env):
    """The first real run, on the app builders' database (2026-11-24), stopped at its 12th
    state: it took "the cheapest plan on sale" by its number, got `Pro (USD)` at 12.00,
    and could not price dollars in pounds, as no operator had set a rate. Our databases
    had only bootstrap's plans. `walk_platform` is now shaped like theirs (its own plans
    in both currencies, no rate, ordinary accounts with their own plans and coachings):
    every state builds there, and the ordinary accounts are as they were."""
    from users.walk import load_states

    before = _developers_shape()
    manifest, _out = _run(walk_env)
    assert set(manifest["states"]) == set(load_states())
    assert _developers_shape() == before


def test_the_refusal_names_every_gap_at_once_and_writes_nothing(walk_env):
    """What bootstrap writes AND what the states need of this database (each area's
    `@needs`), named in ONE refusal before anything is written, each gap once. Here: the
    channel capabilities are gone, and the only plans with the diet left on sale are in
    dollars, which a new wallet (pounds) cannot pay without a rate."""
    from policy.models import ChannelCapabilities
    from subscription.models import SubscriptionPlan
    from users.models import CustomUser

    ChannelCapabilities.objects.all().delete()
    SubscriptionPlan.objects.filter(currency="SYP", has_diet_access=True).update(
        is_active=False)
    with pytest.raises(CommandError, match="nothing was written") as refused:
        _run(walk_env)
    message = str(refused.value)
    assert "channel capabilities" in message and "bootstrap_platform" in message
    # In the words a state's own refusal uses (`shared.cheapest_plan`), and once, though
    # three areas need it (coaching_frozen, plan_lapsed, premium_no_diet_plan).
    diet = "no paid subscription plan on sale in SYP with the diet (the currency a new wallet takes)"
    plain = "no paid subscription plan on sale in SYP (the currency a new wallet takes)"
    assert message.count(diet) == 1 and message.count(plain) == 1, message
    assert not walk_env.exists()
    assert not CustomUser.objects.filter(email__startswith="walk.").exists()


def test_a_need_is_checked_only_for_the_states_that_have_it(walk_env):
    """`--only` a state that needs no plan runs on a database with no pound plan with the
    diet; the state that needs one is refused, naming it."""
    from subscription.models import SubscriptionPlan

    SubscriptionPlan.objects.filter(currency="SYP", has_diet_access=True).update(
        is_active=False)
    manifest, _out = _run(walk_env, "--only", "member_with_post")
    assert set(manifest["states"]) == {"member_with_post"}
    with pytest.raises(CommandError, match="on sale in SYP with the diet"):
        _run(walk_env, "--only", "premium_no_diet_plan")


def test_the_settings_a_state_moves_money_under_are_checked_up_front(walk_env, settings):
    """Not bootstrap data but the environment: a new agent's caps in the base currency,
    and the payout minimum and daily cap for it. Named before anything is written; the
    payout ones only for the states that request a withdrawal."""
    from users.models import CustomUser

    settings.AGENT_DEFAULT_DAILY_LIMIT = 0
    settings.PAYOUT_DAILY_CAP = {"USD": 5000}
    with pytest.raises(CommandError, match="nothing was written") as refused:
        _run(walk_env)
    message = str(refused.value)
    assert "AGENT_DEFAULT_DAILY_LIMIT above 0" in message
    assert "a SYP entry above 0 in the setting PAYOUT_DAILY_CAP" in message
    assert "PAYOUT_MIN_AMOUNT" not in message
    assert not CustomUser.objects.filter(email__startswith="walk.").exists()

    settings.AGENT_DEFAULT_DAILY_LIMIT = 250000
    manifest, _out = _run(walk_env, "--only", "member_with_post")
    assert set(manifest["states"]) == {"member_with_post"}
    with pytest.raises(CommandError, match="PAYOUT_DAILY_CAP"):
        _run(walk_env, "--only", "trainer_withdrawal_requested")


def test_keep_going_writes_what_succeeds_and_names_every_failure(walk_env, monkeypatch):
    """By default the run stops at the first state that fails (its transaction rolled
    back). `--keep-going` builds the next one, writes it, and at the end names every
    failed state with its reason and fails the command (a non-zero exit)."""
    import dataclasses

    from users.models import CustomUser
    from users.walk import STATES, load_states

    load_states()

    def broken(walk):
        walk.account()
        raise RuntimeError("the probe's own failure")

    monkeypatch.setitem(STATES, "member_with_post",
                        dataclasses.replace(STATES["member_with_post"], build=broken))
    both = ("--only", "member_with_post", "--only", "operator_staff")

    with pytest.raises(CommandError, match="walk state member_with_post failed"):
        _run(walk_env, *both)
    assert not walk_env.exists()
    assert not CustomUser.objects.filter(email__startswith="walk.").exists()

    with pytest.raises(CommandError) as failed:
        _run(walk_env, *both, "--keep-going")
    assert "1 walk state(s) reset, 1 failed" in str(failed.value)
    assert "member_with_post: the probe's own failure" in str(failed.value)
    manifest = json.loads(walk_env.read_text(encoding="utf-8"))
    assert set(manifest["states"]) == {"operator_staff"}
    assert not CustomUser.objects.filter(email="walk.member_with_post@yalla.test").exists()
    assert CustomUser.objects.filter(email="walk.operator_staff@yalla.test",
                                     is_active=True).exists()
