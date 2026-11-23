"""Road to 10 R5: the accounts a store reviewer signs in with, on the current line.

ROAD_TO_10 Part 4, R5: "a client with a coach, a programme and a plan; a trainer with a
client. Ordinary accounts on the live server, given in the review notes, behaving like
any other. Done when: they exist and stay alive." `manage.py seed_review_accounts` makes
them; this holds what it makes to the rules that landed after it was written — the
trainer is named and LISTED (S0.3), the programme's schedule answers and is not over
(S0.1, the owner's D15: a programme past its dates is `finished`), the plan is active and
the coaching month is paid — and to "stay alive": the owner runs the command again before
each submission, weeks after the first run, and it must still hand the reviewer a
programme that is running and a coaching that is paid, not a finished programme and a
frozen month. Nothing here is a review-mode: the accounts are read like any other.
"""
from __future__ import annotations

import io
from datetime import timedelta
from decimal import Decimal

import pytest
from django.core.management import call_command
from django.utils import timezone

pytestmark = pytest.mark.django_db

HOME = "/api/v1/home"


@pytest.fixture
def storage_roots(settings, tmp_path):
    settings.MEDIA_ROOT = str(tmp_path / "media")
    settings.PRIVATE_MEDIA_ROOT = str(tmp_path / "private")
    return tmp_path


@pytest.fixture
def review_env(monkeypatch):
    """The two variables, with a password made up at run time: none is written here."""
    import secrets

    password = f"Rv-{secrets.token_urlsafe(18)}!7"
    monkeypatch.setenv("REVIEW_ACCOUNTS_EMAIL", "store-review@yalla-review.test")
    monkeypatch.setenv("REVIEW_ACCOUNTS_PASSWORD", password)
    return password


@pytest.fixture
def seeded(db, seeded_catalogue, review_env, storage_roots, settings,
           django_capture_on_commit_callbacks):
    """`bootstrap_platform`, then the seed, as the owner runs them on a new server."""
    settings.COACHING_COMMISSION_RATE = Decimal("0.20")
    call_command("bootstrap_platform", "--skip-food-catalogue", stdout=io.StringIO())
    with django_capture_on_commit_callbacks(execute=True):
        _seed()
    return _people()


def _seed():
    out = io.StringIO()
    call_command("seed_review_accounts", stdout=out)
    return out.getvalue()


def _people():
    from users.models import CustomUser

    return {u.username.removeprefix("review_"): u
            for u in CustomUser.objects.filter(groups__name="Store review accounts")}


def _home(api, person, channel="apk"):
    from training_platform.cache import private_cache

    private_cache().clear()
    resp = api(person).get(HOME, HTTP_X_APP_CHANNEL=channel)
    assert resp.status_code == 200, resp.content
    return {s["type"]: s["data"] for s in resp.json()["sections"]}


def _programme(client):
    from routine.models import Routine

    (routine,) = Routine.objects.filter(assigned_to=client)
    return routine


def _assert_alive(api, people):
    """What a reviewer must find, on every build the app ships."""
    from subscription.models import Subscription
    from users.coaching import running_month, training_refusal
    from users.models import TrainerClientRelation

    client, trainer = people["client"], people["trainer"]

    # The trainer: named, verified, LISTED in the directory (S0.3), with a client.
    roster = api(client).get("/api/v1/trainers/?page_size=100").json()
    card = next((row for row in roster["results"] if row["id"] == trainer.pk), None)
    assert card is not None, "the review trainer is not listed"
    assert card["first_name"] == "رامي"
    assert TrainerClientRelation.objects.filter(trainer=trainer, client=client,
                                                status="approved").exists()

    # The plan.
    subscription = Subscription.objects.get(user=client)
    assert subscription.is_active and not subscription.is_frozen

    # The coaching month is paid and the routine is trainable.
    relation = TrainerClientRelation.objects.get(trainer=trainer, client=client)
    month = running_month(relation)
    assert month is not None and month.frozen_at is None, "the coaching month is not paid"
    routine = _programme(client)
    assert training_refusal(client, routine) is None

    # The programme: its schedule answers, today is a day of it, it is not over (D15).
    resp = api(client).get(f"/api/routine/routines/{routine.pk}/schedule/")
    assert resp.status_code == 200, resp.content
    schedule = resp.json()
    assert schedule["finished"] is False, schedule
    assert schedule["refusal"] is None
    assert schedule["today"] is not None and schedule["today"]["state"] in (
        "today", "done", "short", "rest"), schedule
    assert schedule["weeks_total"] and 1 <= schedule["week"] <= schedule["weeks_total"]

    # Home's hero says the same on every build: the money rules never touch training.
    for channel in ("apk", "play", "ios"):
        hero = _home(api, client, channel)["workout_hero"]
        assert hero["finished"] is False and hero["day"] == schedule["today"]["day"]
        assert hero["cta"]["state"] in ("start", "continue", "rest"), (channel, hero["cta"])


def test_the_seed_hands_the_reviewer_a_coached_client_and_a_listed_trainer(seeded, api):
    _assert_alive(api, seeded)


def test_a_second_run_soon_after_changes_nothing(seeded, api,
                                                 django_capture_on_commit_callbacks):
    with django_capture_on_commit_callbacks(execute=True):
        second = _seed()
    assert "nothing to change" in second, second
    _assert_alive(api, _people())


def test_run_again_weeks_later_the_programme_is_still_running(
        seeded, api, django_capture_on_commit_callbacks):
    """The programme was written to end six weeks after it started, four weeks after the
    first run. Since D15 a programme past its dates is `finished` and Home offers nothing
    to start, so a reviewer who opens the app a month later found it over. The run the
    review notes ask for before each submission keeps it running."""
    from routine.models import Routine

    routine = _programme(seeded["client"])
    Routine.objects.filter(pk=routine.pk).update(
        end_date=timezone.localdate() - timedelta(days=1))
    assert api(seeded["client"]).get(
        f"/api/routine/routines/{routine.pk}/schedule/").json()["finished"] is True

    with django_capture_on_commit_callbacks(execute=True):
        out = _seed()
    routine.refresh_from_db()
    assert routine.end_date >= timezone.localdate() + timedelta(weeks=4), out
    assert routine.start_date < timezone.localdate()          # its history is kept
    for role in ("client2", "client3"):
        assert _programme(seeded[role]).end_date > timezone.localdate()
    _assert_alive(api, _people())


def test_run_again_after_the_months_ran_out_the_coaching_is_paid(
        seeded, api, django_capture_on_commit_callbacks):
    """The demo wallets hold one or two months. When the month ends and the wallet cannot
    cover the next one, the coaching freezes like anybody's (the owner's rule) and the
    reviewer's routine cannot be started. The run before a submission tops the wallet up
    through the same agent top-up the first run made, and the renewal the hourly run
    would make charges the month — the ordinary paths, nothing review-specific."""
    from users.coaching import renew
    from users.models import TrainerClientRelation
    from wallet.models import Transaction, wallet_for

    from tests.test_coaching_months import _end_month

    client2 = seeded["client2"]
    relation = TrainerClientRelation.objects.get(trainer=seeded["trainer"], client=client2)
    assert wallet_for(client2, "client").balance < relation.periods.latest("number").price
    _end_month(relation)
    from unittest import mock
    with mock.patch("notifications.domain.dispatcher.emit_event"):
        assert renew(relation.pk) == "frozen"
    before = Transaction.objects.filter(tx_type="topup",
                                        destination_wallet=wallet_for(client2, "client")).count()

    with django_capture_on_commit_callbacks(execute=True):
        out = _seed()
    relation.refresh_from_db()
    latest = relation.periods.order_by("-number").first()
    assert latest.number == 2 and latest.frozen_at is None, out
    assert Transaction.objects.filter(
        tx_type="topup", destination_wallet=wallet_for(client2, "client")).count() == before + 1
    from users.coaching import training_refusal
    assert training_refusal(client2, _programme(client2)) is None

    # And a run when nothing is due changes nothing again.
    with django_capture_on_commit_callbacks(execute=True):
        assert "nothing to change" in _seed()
