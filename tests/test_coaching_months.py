"""Coaching by the month, with the platform's commission cut from every month
(owner, 2026-09-19). See users/coaching.py.

The shared setup turns the commission off for the older tests (tests/conftest.py); every
test here sets the owner's rate itself.
"""
from datetime import timedelta
from decimal import Decimal
from unittest import mock

import pytest
from django.utils import timezone

pytestmark = pytest.mark.django_db

JSON = "application/json"
OWNER_RATE = Decimal("0.20")


@pytest.fixture(autouse=True)
def owners_rate(settings):
    settings.COACHING_COMMISSION_RATE = OWNER_RATE
    settings.COACHING_PERIOD_DAYS = 30
    settings.COACHING_RENEWAL_GRACE_DAYS = 3


def _fund(user, amount, owner_type="client"):
    from wallet.models import Wallet

    wallet, _created = Wallet.objects.get_or_create(owner=user, defaults={"owner_type": owner_type})
    wallet.balance = Decimal(amount)
    wallet.save()
    return wallet


def _balance(user):
    from wallet.models import Wallet
    wallet = Wallet.objects.filter(owner=user).first()
    return wallet.balance if wallet else Decimal("0.00")


def _platform():
    from wallet.utils import get_platform_revenue_wallet
    return get_platform_revenue_wallet().balance


def _escrow():
    from wallet.utils import get_escrow_wallet
    return get_escrow_wallet().balance


def _accepted(make_user, api, tag, *, rate="100.00", funds="1000.00"):
    """A client who asked, and a trainer who accepted: month 1 is held."""
    from users.models import TrainerClientRelation

    trainer = make_user(f"cm_t_{tag}", user_type="trainer", trainer_hourly_rate=Decimal(rate),
                        first_name="Rami")
    client = make_user(f"cm_c_{tag}", first_name="Sam")
    _fund(client, funds)
    assert api(client).post("/api/auth/client/request-trainer/", {"trainer_id": trainer.pk},
                            content_type=JSON).status_code == 200
    relation = TrainerClientRelation.objects.get(trainer=trainer, client=client)
    answered = api(trainer).post("/api/auth/trainer/respond-to-request/",
                                 {"request_id": relation.pk, "action": "approve"},
                                 content_type=JSON)
    assert answered.status_code == 200, answered.content
    relation.refresh_from_db()
    return trainer, client, relation


def _deliver_first_program(api, trainer, client):
    from routine.models import Routine

    routine = Routine.objects.create(name="Month one", created_by=trainer, days=3)
    resp = api(trainer).post(f"/api/routine/routines/{routine.pk}/assign_to_client/",
                             {"client_id": client.pk}, content_type=JSON)
    assert resp.status_code == 200, resp.content


def _end_month(relation, *, ago=timedelta(minutes=1)):
    """Move the latest month's end into the past, as if 30 days went by."""
    period = relation.periods.order_by("-number").first()
    shift = period.ends_at - (timezone.now() - ago)
    type(period).objects.filter(pk=period.pk).update(starts_at=period.starts_at - shift,
                                                     ends_at=period.ends_at - shift)
    period.refresh_from_db()
    return period


# ------------------------------------------------------------------- month one
def test_month_one_is_held_on_accept_and_split_when_the_first_program_arrives(make_user, api):
    from wallet.models import Transaction

    trainer, client, relation = _accepted(make_user, api, "m1")
    (month,) = relation.periods.all()
    assert (month.number, month.status, month.price, month.commission_rate) == \
        (1, "held", Decimal("100.00"), OWNER_RATE)
    assert _balance(client) == Decimal("900.00")
    platform_before, escrow_before = _platform(), _escrow()

    _deliver_first_program(api, trainer, client)
    month.refresh_from_db()
    assert (month.status, month.commission, month.trainer_share) == \
        ("settled", Decimal("20.00"), Decimal("80.00"))
    assert _balance(trainer) == Decimal("80.00")
    assert _platform() - platform_before == Decimal("20.00")
    assert escrow_before - _escrow() == Decimal("100.00")
    assert month.ends_at - month.starts_at == timedelta(days=30)
    assert abs((timezone.now() - month.starts_at).total_seconds()) < 60

    legs = Transaction.objects.filter(reference_id__in=[month.payout_reference,
                                                        month.commission_reference])
    assert {t.money_type for t in legs} == {"COACHING_1TO1"}
    assert {t.metadata["purpose"] for t in legs} == {"trainer_assignment_settlement",
                                                     "coaching_commission"}

    # A second program pays nothing more: the month is already paid out.
    _deliver_first_program(api, trainer, client)
    assert _balance(trainer) == Decimal("80.00") and relation.periods.count() == 1


def test_the_commission_is_rounded_to_the_cent_and_the_trainer_gets_the_rest():
    from users.coaching import split

    for price in ("33.33", "0.01", "12000.00", "99.99"):
        commission, share = split(Decimal(price), OWNER_RATE)
        assert commission + share == Decimal(price)
        assert commission == (Decimal(price) * OWNER_RATE).quantize(Decimal("0.01"))
    assert split(Decimal("33.33"), OWNER_RATE) == (Decimal("6.67"), Decimal("26.66"))


def test_a_month_is_split_at_the_rate_it_was_charged_at(make_user, api, settings):
    trainer, client, relation = _accepted(make_user, api, "rate")
    settings.COACHING_COMMISSION_RATE = Decimal("0.35")   # changed after the hold
    _deliver_first_program(api, trainer, client)
    assert _balance(trainer) == Decimal("80.00")


def test_leaving_before_the_first_program_returns_everything(make_user, api):
    trainer, client, relation = _accepted(make_user, api, "leave")
    platform_before = _platform()
    resp = api(client).post("/api/auth/client/unassign-trainer/", {}, content_type=JSON)
    assert resp.status_code == 200, resp.content
    assert _balance(client) == Decimal("1000.00") and _platform() == platform_before
    from users.models import CoachingPeriod
    (month,) = CoachingPeriod.objects.filter(client=client)
    assert month.status == "refunded" and month.relation_id is None


# ---------------------------------------------------------------------- renewal
def test_the_next_month_is_charged_split_and_starts_where_the_last_ended(
        make_user, api, django_capture_on_commit_callbacks):
    from users.coaching import renew_due

    trainer, client, relation = _accepted(make_user, api, "renew")
    _deliver_first_program(api, trainer, client)
    first = _end_month(relation)
    platform_before = _platform()

    with mock.patch("notifications.domain.dispatcher.emit_event") as sent, \
            django_capture_on_commit_callbacks(execute=True):
        result = renew_due()
    assert result == {"counts": {"renewed": 1, "not_due": 1}, "failed": []}
    second = relation.periods.get(number=2)
    assert (second.status, second.starts_at, second.commission, second.trainer_share) == \
        ("settled", first.ends_at, Decimal("20.00"), Decimal("80.00"))
    assert _balance(client) == Decimal("800.00")
    assert _balance(trainer) == Decimal("160.00")
    assert _platform() - platform_before == Decimal("20.00")
    assert [type(c.args[0]).__name__ for c in sent.call_args_list] == ["CoachingRenewedEvent"]

    # Idempotent: the month is charged once however often the job runs.
    assert renew_due()["counts"] == {}
    assert relation.periods.count() == 2 and _balance(client) == Decimal("800.00")


def test_a_renewal_charges_the_price_the_client_agreed_to(make_user, api):
    from users.coaching import renew_due

    trainer, client, relation = _accepted(make_user, api, "agreed")
    _deliver_first_program(api, trainer, client)
    trainer.trainer_hourly_rate = Decimal("500.00")   # raised after the client agreed
    trainer.save()
    _end_month(relation)
    renew_due()
    assert relation.periods.get(number=2).price == Decimal("100.00")


def test_months_missed_while_the_job_was_down_are_caught_up(make_user, api):
    from users.coaching import renew_due

    trainer, client, relation = _accepted(make_user, api, "catchup")
    _deliver_first_program(api, trainer, client)
    _end_month(relation, ago=timedelta(days=31))
    renew_due()
    assert list(relation.periods.order_by("number").values_list("number", flat=True)) == [1, 2, 3]
    assert _balance(client) == Decimal("700.00")


def test_an_unpaid_month_reminds_once_then_ends_the_coaching_after_the_grace(
        make_user, api, django_capture_on_commit_callbacks):
    from users.coaching import renew
    from users.models import CoachingPeriod, CustomUser, TrainerClientRelation

    trainer, client, relation = _accepted(make_user, api, "unpaid", funds="100.00")
    _deliver_first_program(api, trainer, client)
    CustomUser.objects.filter(pk=client.pk).update(assigned_trainer=trainer)
    first = _end_month(relation)
    assert _balance(client) == Decimal("0.00")

    with mock.patch("notifications.domain.dispatcher.emit_event") as sent, \
            django_capture_on_commit_callbacks(execute=True):
        assert renew(relation.pk) == "reminded"
        assert renew(relation.pk) == "due"
    assert [type(c.args[0]).__name__ for c in sent.call_args_list] == ["CoachingPaymentDueEvent"]
    first.refresh_from_db()
    assert first.renewal_reminder_sent_at is not None

    after_grace = first.ends_at + timedelta(days=3, minutes=1)
    with mock.patch("notifications.domain.dispatcher.emit_event") as sent, \
            django_capture_on_commit_callbacks(execute=True):
        assert renew(relation.pk, now=after_grace) == "ended"
    assert not TrainerClientRelation.objects.filter(pk=relation.pk).exists()
    client.refresh_from_db()
    assert client.assigned_trainer_id is None
    assert CoachingPeriod.objects.filter(client=client).count() == 1, "paid months are kept"
    ended = [c.args[0] for c in sent.call_args_list]
    assert sorted(e.recipient_id for e in ended) == sorted([client.pk, trainer.pk])


def test_a_top_up_inside_the_grace_keeps_the_coaching(make_user, api):
    from users.coaching import renew

    trainer, client, relation = _accepted(make_user, api, "topup", funds="100.00")
    _deliver_first_program(api, trainer, client)
    _end_month(relation)
    with mock.patch("notifications.domain.dispatcher.emit_event"):
        assert renew(relation.pk) == "reminded"
        _fund(client, "250.00")
        assert renew(relation.pk) == "renewed"
    assert _balance(client) == Decimal("150.00")


def test_a_free_trainer_has_no_months(make_user, api):
    from users.coaching import due_relation_ids

    trainer, client, relation = _accepted(make_user, api, "free", rate="0.00")
    assert relation.periods.count() == 0
    assert relation.pk not in due_relation_ids()


def test_the_beat_task_runs_the_renewal(make_user, api):
    from users.tasks import renew_coaching_periods

    trainer, client, relation = _accepted(make_user, api, "beat")
    _deliver_first_program(api, trainer, client)
    _end_month(relation)
    with mock.patch("notifications.domain.dispatcher.emit_event"):
        assert renew_coaching_periods()["renewed"] == 1


# ---------------------------------------------------------------- what the app sees
def test_the_client_sees_each_state_of_their_coaching(make_user, api):
    from users.coaching import renew

    trainer, client, relation = _accepted(make_user, api, "view", funds="100.00")
    look = lambda: api(client).get("/api/v1/coaching/current/").json()["coaching"]  # noqa: E731

    held = look()
    assert (held["status"], held["period"], held["trainer_id"]) == \
        ("awaiting_first_program", None, trainer.pk)
    assert (held["coaching_price"], held["money_type"]) == ("100.00", "COACHING_1TO1")

    _deliver_first_program(api, trainer, client)
    active = look()
    assert active["status"] == "active" and active["period"]["number"] == 1
    assert active["renews_at"] == active["period"]["ends_at"] and active["grace_until"] is None

    _end_month(relation)
    with mock.patch("notifications.domain.dispatcher.emit_event"):
        renew(relation.pk)
    due = look()
    assert due["status"] == "payment_due" and due["grace_until"] is not None

    ios = api(client).get("/api/v1/coaching/current/", HTTP_X_APP_CHANNEL="ios").json()
    assert "coaching_price" not in ios["coaching"] and "money_type" not in ios["coaching"]


def test_only_a_client_with_a_trainer_has_coaching(make_user, api):
    assert api(make_user("cm_lonely")).get("/api/v1/coaching/current/").json() == \
        {"coaching": None}
    refused = api(make_user("cm_coach", user_type="trainer")).get("/api/v1/coaching/current/")
    assert refused.status_code == 403 and refused.json()["code"] == "clients_only"
    assert api().get("/api/v1/coaching/current/").status_code == 401


# --------------------------------------------------------- found in review
def test_a_suspended_trainer_is_not_paid_and_the_coaching_ends(make_user, api,
                                                               django_capture_on_commit_callbacks):
    from users.coaching import renew
    from users.models import CustomUser, TrainerClientRelation

    trainer, client, relation = _accepted(make_user, api, "suspended")
    _deliver_first_program(api, trainer, client)
    _end_month(relation)
    CustomUser.objects.filter(pk=trainer.pk).update(is_active=False)
    with mock.patch("notifications.domain.dispatcher.emit_event"), \
            django_capture_on_commit_callbacks(execute=True):
        assert renew(relation.pk) == "ended"
    assert _balance(client) == Decimal("900.00"), "nothing was charged"
    assert not TrainerClientRelation.objects.filter(pk=relation.pk).exists()


def test_a_renewal_costs_what_the_last_month_cost_even_without_an_agreed_rate(make_user, api):
    """`agreed_rate` can be empty (a trainer with no rate when asked); month 1 then took
    the trainer's rate at approval. Renewals follow the month, not the trainer's price."""
    from users.coaching import renew_due
    from users.models import TrainerClientRelation

    trainer, client, relation = _accepted(make_user, api, "norate")
    TrainerClientRelation.objects.filter(pk=relation.pk).update(agreed_rate=None)
    _deliver_first_program(api, trainer, client)
    trainer.trainer_hourly_rate = Decimal("300.00")
    trainer.save()
    _end_month(relation)
    with mock.patch("notifications.domain.dispatcher.emit_event"):
        renew_due()
    assert relation.periods.get(number=2).price == Decimal("100.00")


def test_between_a_months_end_and_the_hourly_run_the_coaching_is_still_active(make_user, api):
    trainer, client, relation = _accepted(make_user, api, "window")
    _deliver_first_program(api, trainer, client)
    ended = _end_month(relation)
    body = api(client).get("/api/v1/coaching/current/").json()["coaching"]
    assert body["status"] == "active" and body["grace_until"] is None
    assert body["renews_at"] is not None
    assert ended.renewal_reminder_sent_at is None


def test_coaching_paid_as_one_fee_before_months_existed_is_active_and_never_renewed(make_user, api):
    from users.coaching import due_relation_ids
    from users.models import TrainerClientRelation

    trainer = make_user("cm_t_legacy", user_type="trainer", trainer_hourly_rate=Decimal("100.00"))
    client = make_user("cm_c_legacy")
    relation = TrainerClientRelation.objects.create(
        trainer=trainer, client=client, status="approved", initiated_by="client",
        agreed_rate=Decimal("100.00"))   # paid out before 0041: no hold, no month
    body = api(client).get("/api/v1/coaching/current/").json()["coaching"]
    assert body["status"] == "active" and body["period"] is None
    assert relation.pk not in due_relation_ids()
