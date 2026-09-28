"""BACKEND_BUGS B-159 and B-160: why a coaching ended, kept for the operators.

The client's leave sheet asks «(اختياري، يساعدنا)» and the trainer's remove sheet
«يراه فريق المنصة فقط»; both send `reason`, and neither view read it — the relation row
is deleted when the coaching ends, so nothing recorded why anyone left.

The owner (2026-09-28): the codes are the ones on each sheet; operators see them (the
console, read-only), never the trainer, and a trainer's reason never reaches the client;
kept anonymised on erasure; optional — an unknown value is kept as none, never refused.

The coachings here are made the way the app makes them: the client asks, the trainer
accepts (month 1 held, `users.coaching.open_period`), and the first program settles it.
"""
from __future__ import annotations

from decimal import Decimal

import pytest

pytestmark = pytest.mark.django_db

JSON = "application/json"


@pytest.fixture(autouse=True)
def owners_rate(settings):
    settings.COACHING_COMMISSION_RATE = Decimal("0.20")
    settings.COACHING_PERIOD_DAYS = 30


def _fund(user, amount):
    from wallet.models import Wallet

    wallet, _created = Wallet.objects.get_or_create(owner=user,
                                                    defaults={"owner_type": "client"})
    wallet.balance = Decimal(amount)
    wallet.save()


def _accepted(make_user, api, tag):
    """A client who asked, and a trainer who accepted: month 1 is held."""
    from users.models import TrainerClientRelation

    trainer = make_user(f"b159_t_{tag}", user_type="trainer",
                        trainer_hourly_rate=Decimal("100.00"))
    client = make_user(f"b159_c_{tag}")
    _fund(client, "1000.00")
    asked = api(client).post("/api/auth/client/request-trainer/", {"trainer_id": trainer.pk},
                             content_type=JSON)
    assert asked.status_code == 200, asked.content
    relation = TrainerClientRelation.objects.get(trainer=trainer, client=client)
    answered = api(trainer).post("/api/auth/trainer/respond-to-request/",
                                 {"request_id": relation.pk, "action": "approve"},
                                 content_type=JSON)
    assert answered.status_code == 200, answered.content
    relation.refresh_from_db()
    assert relation.periods.get().status == "held"
    return trainer, client, relation


def _deliver_first_program(api, trainer, client):
    """The first program: month 1 is paid out and starts running."""
    from routine.models import Routine

    routine = Routine.objects.create(name="Month one", created_by=trainer, days=3)
    resp = api(trainer).post(f"/api/routine/routines/{routine.pk}/assign_to_client/",
                             {"client_id": client.pk}, content_type=JSON)
    assert resp.status_code == 200, resp.content


# ============================================================================ B-159
def test_b159_a_client_leaving_records_their_reason(make_user, api):
    from users.models import CoachingEnding, TrainerClientRelation

    trainer, client, relation = _accepted(make_user, api, "leave")
    resp = api(client).post("/api/auth/client/unassign-trainer/", {"reason": "price"},
                            content_type=JSON)
    assert resp.status_code == 200, resp.content
    # The answer is unchanged.
    assert set(resp.json()) == {"message", "previous_trainer_id", "previous_trainer_name"}
    assert not TrainerClientRelation.objects.filter(pk=relation.pk).exists()

    (ending,) = CoachingEnding.objects.filter(client=client)
    assert (ending.trainer_id, ending.ended_by, ending.reason, ending.scheduled) == \
        (trainer.pk, "client", "price", False)


def test_b159_the_reason_is_optional_and_an_unknown_one_is_kept_as_none(make_user, api):
    from users.models import CoachingEnding

    url = "/api/auth/client/unassign-trainer/"
    _t, quiet, _r = _accepted(make_user, api, "quiet")
    assert api(quiet).post(url, {}, content_type=JSON).status_code == 200
    # A trainer's code is not a client's, and a made-up one is not refused either.
    _t, odd, _r = _accepted(make_user, api, "odd")
    assert api(odd).post(url, {"reason": "roster_full"}, content_type=JSON).status_code == 200
    _t, listy, _r = _accepted(make_user, api, "listy")
    assert api(listy).post(url, {"reason": ["price"]}, content_type=JSON).status_code == 200

    rows = {e.client_id: e for e in CoachingEnding.objects.all()}
    assert {rows[u.pk].reason for u in (quiet, odd, listy)} == {None}
    assert {rows[u.pk].ended_by for u in (quiet, odd, listy)} == {"client"}


# ============================================================================ B-160
def test_b160_a_trainer_ending_coaching_records_the_reason_for_the_operator(make_user, api):
    from users.models import CoachingEnding, TrainerClientRelation

    url = "/api/auth/trainer/unassign-client/"

    # (a) Month 1 only held: the coaching ends now.
    trainer, client, relation = _accepted(make_user, api, "now")
    resp = api(trainer).post(url, {"client_id": client.pk, "reason": "no_reply"},
                             content_type=JSON)
    assert resp.status_code == 200, resp.content
    assert not TrainerClientRelation.objects.filter(pk=relation.pk).exists()
    (ending,) = CoachingEnding.objects.filter(client=client)
    assert (ending.trainer_id, ending.ended_by, ending.reason, ending.scheduled) == \
        (trainer.pk, "trainer", "no_reply", False)

    # (b) A paid month running: refused (409) writes nothing; scheduling the end at the
    # month's close writes one row, and asking again writes no second.
    trainer, client, relation = _accepted(make_user, api, "later")
    _deliver_first_program(api, trainer, client)
    refused = api(trainer).post(url, {"client_id": client.pk, "reason": "roster_full"},
                                content_type=JSON)
    assert (refused.status_code, refused.json()["code"]) == (409, "coaching_month_active")
    assert not CoachingEnding.objects.filter(client=client).exists()
    for _attempt in range(2):
        resp = api(trainer).post(url, {"client_id": client.pk, "at_period_end": True,
                                       "reason": "roster_full"}, content_type=JSON)
        assert resp.status_code == 200 and resp.json()["scheduled"] is True, resp.content
    (ending,) = CoachingEnding.objects.filter(client=client)
    assert (ending.trainer_id, ending.ended_by, ending.reason, ending.scheduled) == \
        (trainer.pk, "trainer", "roster_full", True)
    assert TrainerClientRelation.objects.filter(pk=relation.pk, status="approved").exists()


# ================================================================ who sees it, and erasure
def test_b159_b160_operators_see_the_endings_read_only_and_erasure_keeps_them_anonymised(
        make_user, api):
    from django.test import Client

    from training_platform.privacy.registry import erase_user_data
    from users.models import CoachingEnding

    trainer = make_user("b159_t_erase", user_type="trainer")
    client = make_user("b159_c_erase")
    ending = CoachingEnding.record(trainer=trainer, client=client, ended_by="client",
                                   reason="slow_replies")

    console = Client()
    console.force_login(make_user("b159_operator", user_type="admin"))
    listing = console.get("/dj-admin/users/coachingending/")
    assert listing.status_code == 200 and b"Slow replies (client)" in listing.content
    assert console.get("/dj-admin/users/coachingending/add/").status_code == 403

    erase_user_data(client)
    ending.refresh_from_db()
    assert (ending.trainer_id, ending.client_id) == (None, None)
    assert (ending.ended_by, ending.reason) == ("client", "slow_replies")
