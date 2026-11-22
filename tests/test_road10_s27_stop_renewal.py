"""Road to 10 S2.7, the owner's D12: the client's «لا تجدّد بعد هذا الشهر».

A flag on the coaching the client sets and unsets (`POST /api/v1/coaching/renewal/`). When
the paid month ends the coaching ends instead of renewing: no charge, no hold, and the
trainer keeps what the month already paid. The three-day reminder says which of the two
will happen; the trainer sees the flag on the client's file; setting or unsetting it
twice is harmless; a trainer still cannot end a paid month the client is in.
See users/coaching.py `set_renewal` and `renew`.
"""
from datetime import timedelta
from decimal import Decimal
from unittest import mock

import pytest

from tests.test_coaching_months import (OWNER_RATE, _accepted, _balance, _deliver_first_program,
                                        _end_month, _escrow, _platform)

pytestmark = pytest.mark.django_db

JSON = "application/json"
ROUTE = "/api/v1/coaching/renewal/"


@pytest.fixture(autouse=True)
def owners_rules(settings):
    settings.COACHING_COMMISSION_RATE = OWNER_RATE
    settings.COACHING_PERIOD_DAYS = 30
    settings.COACHING_RENEWAL_REMINDER_DAYS = 3
    settings.COACHING_FROZEN_REMINDER_DAYS = (3, 7, 14, 30)


def _paid(make_user, api, tag, *, funds="1000.00"):
    """A client in a paid month 1 (100.00: 80.00 to the trainer, 20.00 to the platform)."""
    trainer, client, relation = _accepted(make_user, api, tag, funds=funds)
    _deliver_first_program(api, trainer, client)
    relation.refresh_from_db()
    return trainer, client, relation


def _switch(api, client, renew, **headers):
    return api(client).post(ROUTE, {"renew": renew}, content_type=JSON, **headers)


def _money(trainer, client):
    return (_balance(client), _balance(trainer), _platform(), _escrow())


# ------------------------------------------------------------------ the month ends
def test_the_coaching_ends_with_the_month_and_nothing_is_charged(make_user, api,
                                                                 django_capture_on_commit_callbacks):
    from users.coaching import renew
    from users.models import CoachingEnding, CoachingPeriod, TrainerClientRelation
    from wallet.models import Transaction

    trainer, client, relation = _paid(make_user, api, "s27end")
    month = relation.periods.get()
    resp = _switch(api, client, False)
    assert resp.status_code == 200, resp.content
    shown = resp.json()["coaching"]
    assert (shown["stop_renewal"], shown["status"], shown["renews_at"]) == (True, "active", None)
    assert shown["ends_at"] is not None and shown["ending"] is False
    # The answer is the object coaching/current/ answers, price included on apk.
    assert shown == api(client).get("/api/v1/coaching/current/").json()["coaching"]
    assert shown["coaching_price"] == "100.00"

    before = _money(trainer, client)
    assert before[1] == Decimal("80.00"), "month 1 was paid out to the trainer"
    ledger_before = Transaction.objects.count()
    _end_month(relation)
    with mock.patch("notifications.domain.dispatcher.emit_event") as sent, \
            django_capture_on_commit_callbacks(execute=True):
        assert renew(relation.pk) == "ended"
    # No charge, no hold, no refund: not one ledger row, not one cent moved.
    assert _money(trainer, client) == before
    assert Transaction.objects.count() == ledger_before
    assert CoachingPeriod.objects.filter(pk=month.pk, status="settled").exists(), \
        "the month already paid stays the trainer's"
    assert CoachingPeriod.objects.filter(client=client).count() == 1
    assert not TrainerClientRelation.objects.filter(trainer=trainer, client=client).exists()
    ending = CoachingEnding.objects.get(client=client)
    assert (ending.ended_by, ending.scheduled, ending.reason) == ("client", True, None)
    told = sorted((type(c.args[0]).__name__, c.args[0].recipient_id) for c in sent.call_args_list)
    assert told == sorted([("CoachingFinishedEvent", client.pk),
                           ("CoachingFinishedEvent", trainer.pk)])
    assert api(client).get("/api/v1/coaching/current/").json() == {"coaching": None}
    # A second run (a retry) finds nothing to do and moves nothing.
    assert renew(relation.pk) == "gone"
    assert _money(trainer, client) == before


def test_a_short_wallet_is_not_frozen_when_the_client_stopped(make_user, api):
    """The flag is read before the money: a client who asked to stop is never frozen,
    whatever the balance."""
    from users.coaching import renew
    from users.models import TrainerClientRelation

    trainer, client, relation = _paid(make_user, api, "s27short", funds="100.00")
    assert _balance(client) == Decimal("0.00")
    assert _switch(api, client, False).status_code == 200
    _end_month(relation)
    with mock.patch("notifications.domain.dispatcher.emit_event"):
        assert renew(relation.pk) == "ended"
    assert not TrainerClientRelation.objects.filter(pk=relation.pk).exists()
    assert _balance(client) == Decimal("0.00")


def test_switched_back_the_month_renews_and_is_charged_once(make_user, api):
    from users.coaching import renew

    trainer, client, relation = _paid(make_user, api, "s27back")
    assert _switch(api, client, False).status_code == 200
    resp = _switch(api, client, True)
    assert resp.status_code == 200, resp.content
    shown = resp.json()["coaching"]
    assert (shown["stop_renewal"], shown["ends_at"]) == (False, None)
    assert shown["renews_at"] is not None

    client_before, trainer_before = _balance(client), _balance(trainer)
    _end_month(relation)
    with mock.patch("notifications.domain.dispatcher.emit_event"):
        assert renew(relation.pk) == "renewed"
        assert renew(relation.pk) == "not_due", "a retried run charges nothing more"
    assert client_before - _balance(client) == Decimal("100.00")
    assert _balance(trainer) - trainer_before == Decimal("80.00")
    assert relation.periods.count() == 2


def test_month_one_not_yet_started_runs_and_then_ends(make_user, api):
    from users.coaching import renew

    trainer, client, relation = _accepted(make_user, api, "s27held")
    resp = _switch(api, client, False)
    assert resp.status_code == 200, resp.content
    shown = resp.json()["coaching"]
    assert (shown["status"], shown["stop_renewal"], shown["ends_at"]) == \
        ("awaiting_first_program", True, None)
    _deliver_first_program(api, trainer, client)
    shown = api(client).get("/api/v1/coaching/current/").json()["coaching"]
    assert (shown["status"], shown["stop_renewal"], shown["renews_at"]) == ("active", True, None)
    assert shown["ends_at"] is not None
    _end_month(relation)
    with mock.patch("notifications.domain.dispatcher.emit_event"):
        assert renew(relation.pk) == "ended"
    assert _balance(client) == Decimal("900.00"), "month 1 only"


def test_after_the_end_the_trainers_routine_is_readable_not_trainable(make_user, api):
    from tests.test_coaching_freeze import _coached
    from users.coaching import renew

    trainer, client, relation, routine, *_ = _coached(make_user, api, "s27rt")
    assert api(client).get(f"/api/routine/routines/{routine.pk}/").status_code == 200
    assert _switch(api, client, False).status_code == 200
    _end_month(relation)
    with mock.patch("notifications.domain.dispatcher.emit_event"):
        assert renew(relation.pk) == "ended"
    started = api(client).post("/api/routine/workout-sessions/",
                               {"routine": routine.pk, "day": 1}, content_type=JSON)
    assert (started.status_code, started.json().get("code")) == (403, "coaching_ended")
    assert api(client).get(f"/api/routine/routines/{routine.pk}/").status_code == 200, \
        "still readable after the end"


# ------------------------------------------------------------------ idempotency
def test_setting_or_unsetting_twice_is_harmless(make_user, api):
    from users.models import CoachingEnding, TrainerClientRelation

    trainer, client, relation = _paid(make_user, api, "s27twice")
    first = _switch(api, client, False)
    stamp = TrainerClientRelation.objects.get(pk=relation.pk).updated_at
    again = _switch(api, client, "false")
    assert (first.status_code, again.status_code) == (200, 200)
    assert first.json() == again.json()
    assert TrainerClientRelation.objects.get(pk=relation.pk).updated_at == stamp, \
        "a repeat writes nothing"
    assert _switch(api, client, True).status_code == 200
    stamp = TrainerClientRelation.objects.get(pk=relation.pk).updated_at
    assert _switch(api, client, "true").status_code == 200
    assert TrainerClientRelation.objects.get(pk=relation.pk).updated_at == stamp
    row = TrainerClientRelation.objects.get(pk=relation.pk)
    assert (row.client_stops_renewal, row.status) == (False, "approved")
    assert not CoachingEnding.objects.filter(client=client).exists(), \
        "switching records no ending; only the end does"


# ------------------------------------------------------------------ the reminder
def test_the_three_day_reminder_says_the_coaching_ends(make_user, api,
                                                      django_capture_on_commit_callbacks):
    from notifications.models import Notification
    from users.coaching import remind_upcoming

    trainer, client, relation = _paid(make_user, api, "s27rm")
    _t2, renewing, _r2 = _paid(make_user, api, "s27rm2")
    assert _switch(api, client, False).status_code == 200
    period = relation.periods.get()
    now = period.ends_at - timedelta(days=3) + timedelta(minutes=5)
    with django_capture_on_commit_callbacks(execute=True):
        assert remind_upcoming(now=now) == 2
        assert remind_upcoming(now=now + timedelta(hours=1)) == 0, "once per month"
    kinds = set(Notification.objects.filter(recipient=client).values_list("event_type", flat=True))
    assert "coaching_ending_soon" in kinds and "coaching_renewal_soon" not in kinds
    others = set(Notification.objects.filter(recipient=renewing)
                 .values_list("event_type", flat=True))
    assert "coaching_renewal_soon" in others and "coaching_ending_soon" not in others
    _end_month(relation, ago=-timedelta(days=2))     # ends in two days
    shown = api(client).get("/api/v1/coaching/current/").json()["coaching"]
    assert (shown["ending_reminder"], shown["renewal_reminder"]) == (True, False)


def test_the_ending_notice_carries_no_price_and_reads_in_arabic(make_user):
    from django.utils import translation

    from notifications.channels.fcm import _resolve_event_template

    template = _resolve_event_template("coaching_ending_soon")
    context = {"name": "Rami", "days": 3}
    with translation.override("ar"):
        arabic = f"{template.title} {template.body % context}"
    with translation.override("en"):
        english = f"{template.title} {template.body % context}"
    assert arabic != english and "ينتهي" in arabic
    for text in (arabic, english):
        assert "http" not in text and "SYP" not in text and "100" not in text, text


# ------------------------------------------------------------------ the trainer
def test_the_trainer_sees_the_choice_on_the_clients_file(make_user, api):
    trainer, client, relation = _paid(make_user, api, "s27file")
    path = f"/api/auth/trainer/client-profile/{client.pk}/"
    first = api(trainer).get(path)
    assert first.status_code == 200, first.content
    assert first.json()["coaching"]["stop_renewal"] is False
    assert _switch(api, client, False).status_code == 200
    seen = api(trainer).get(path).json()["coaching"]     # live, not the cached profile
    assert (seen["stop_renewal"], seen["status"], seen["renews_at"]) == (True, "active", None)
    assert seen["ends_at"] is not None
    assert not {"coaching_price", "coaching_currency", "money_type"} & set(seen)
    assert _switch(api, client, True).status_code == 200
    assert api(trainer).get(path).json()["coaching"]["stop_renewal"] is False


def test_a_trainer_still_cannot_end_the_paid_month(make_user, api):
    from users.models import TrainerClientRelation

    trainer, client, relation = _paid(make_user, api, "s27tr")
    assert _switch(api, client, False).status_code == 200
    resp = api(trainer).post("/api/auth/trainer/unassign-client/", {"client_id": client.pk},
                             content_type=JSON)
    assert (resp.status_code, resp.json()["code"]) == (409, "coaching_month_active")
    row = TrainerClientRelation.objects.get(pk=relation.pk)
    assert (row.status, row.client_stops_renewal) == ("approved", True)


# ------------------------------------------------------------------ refusals and builds
def test_the_refusals_name_their_codes(make_user, api):
    trainer, client, relation = _paid(make_user, api, "s27ref")
    assert api(trainer).post(ROUTE, {"renew": False}, content_type=JSON).json()["code"] == \
        "clients_only"
    for body in ({}, {"renew": "maybe"}, {"renew": 1}):
        resp = api(client).post(ROUTE, body, content_type=JSON)
        assert (resp.status_code, resp.json()["code"], resp.json()["field"]) == \
            (400, "renew_required", "renew"), body
    loner = make_user("s27_loner")
    resp = _switch(api, loner, False)
    assert (resp.status_code, resp.json()["code"]) == (404, "no_coaching")

    # Support returned month 1 (`unpaid`): nothing renews until support resolves it.
    relation.periods.update(status="refunded")
    resp = _switch(api, client, False)
    assert (resp.status_code, resp.json()["code"]) == (409, "nothing_to_renew")


def test_a_free_coaching_has_nothing_to_stop(make_user, api):
    trainer, client, relation = _accepted(make_user, api, "s27free", rate="0.00")
    assert not relation.periods.exists() and not relation.hold_reference
    resp = _switch(api, client, False)
    assert (resp.status_code, resp.json()["code"]) == (409, "nothing_to_renew")
    assert _switch(api, client, True).status_code == 200


def test_a_frozen_coaching_can_be_stopped_and_ends_without_a_charge(make_user, api):
    """A frozen coaching renews the moment the wallet covers the month, so stopping it
    matters: the next hourly run wakes it at once and ends it, even after a top-up."""
    from tests.test_coaching_freeze import _freeze
    from tests.test_coaching_months import _fund
    from users.coaching import due_relation_ids, renew_due
    from users.models import TrainerClientRelation

    trainer, client, relation = _paid(make_user, api, "s27frz", funds="100.00")
    _freeze(relation)
    assert relation.pk not in due_relation_ids(), "an idle frozen coaching is not woken"
    resp = _switch(api, client, False)
    assert resp.status_code == 200, resp.content
    assert (resp.json()["coaching"]["status"], resp.json()["coaching"]["stop_renewal"]) == \
        ("frozen", True)
    assert relation.pk in due_relation_ids(), "a stopped frozen coaching is woken at once"
    _fund(client, "500.00")                      # a top-up lands before the run
    trainer_before = _balance(trainer)
    with mock.patch("notifications.domain.dispatcher.emit_event"):
        result = renew_due()
    assert result["counts"].get("ended") == 1, result
    assert not TrainerClientRelation.objects.filter(pk=relation.pk).exists()
    assert (_balance(client), _balance(trainer)) == (Decimal("500.00"), trainer_before)
    from users.models import CoachingPeriod
    assert CoachingPeriod.objects.filter(client=client).count() == 1, "no month was charged"


def test_withdrawn_on_a_frozen_coaching_it_renews_once_the_wallet_covers_it(make_user, api):
    from tests.test_coaching_freeze import _freeze
    from tests.test_coaching_months import _fund
    from users.coaching import renew_due

    trainer, client, relation = _paid(make_user, api, "s27frz2", funds="100.00")
    _freeze(relation)
    assert _switch(api, client, False).status_code == 200
    assert _switch(api, client, True).json()["coaching"]["stop_renewal"] is False
    _fund(client, "100.00")
    with mock.patch("notifications.domain.dispatcher.emit_event"):
        assert renew_due()["counts"].get("renewed") == 1
    assert _balance(client) == Decimal("0.00")


# ------------------------------------------------------------------ a new coaching
def test_a_reused_row_starts_a_new_coaching_that_renews(make_user, api):
    """A declined row sent again is the same row: a stop left on it from an earlier
    coaching is not the new coaching's."""
    from tests.test_coaching_months import _fund
    from users.models import TrainerClientRelation

    trainer = make_user("s27_re_t", user_type="trainer", trainer_hourly_rate=Decimal("100.00"),
                        first_name="Rami")
    client = make_user("s27_re_c", first_name="Sam")
    _fund(client, "1000.00")
    row = TrainerClientRelation.objects.create(trainer=trainer, client=client,
                                               status="rejected", initiated_by="client",
                                               client_stops_renewal=True)
    assert api(client).post("/api/auth/client/request-trainer/", {"trainer_id": trainer.pk},
                            content_type=JSON).status_code == 200
    answered = api(trainer).post("/api/auth/trainer/respond-to-request/",
                                 {"request_id": row.pk, "action": "approve"},
                                 content_type=JSON)
    assert answered.status_code == 200, answered.content
    row.refresh_from_db()
    assert (row.pk, row.status, row.client_stops_renewal) == (row.pk, "approved", False)


def test_an_operator_repointing_a_running_coaching_keeps_the_clients_choice(make_user, api):
    from django.contrib.admin.sites import site
    from users.admin import CustomUserAdmin
    from users.models import CustomUser, TrainerClientRelation

    trainer, client, relation = _paid(make_user, api, "s27adm")
    assert _switch(api, client, False).status_code == 200
    CustomUser.objects.filter(pk=client.pk).update(assigned_trainer=None)   # out of step
    client.refresh_from_db()
    client.assigned_trainer = trainer
    form = mock.Mock(changed_data=["assigned_trainer"])
    CustomUserAdmin(CustomUser, site).save_model(mock.Mock(), client, form, True)
    row = TrainerClientRelation.objects.get(pk=relation.pk)
    assert (row.status, row.client_stops_renewal) == ("approved", True)

    # A NEW coaching the operator makes starts renewing.
    other = make_user("s27_adm_t2", user_type="trainer", first_name="Lina")
    TrainerClientRelation.objects.create(trainer=other, client=client, status="rejected",
                                         initiated_by="client", client_stops_renewal=True)
    TrainerClientRelation.objects.filter(pk=relation.pk).update(status="rejected")
    client.assigned_trainer = other
    CustomUserAdmin(CustomUser, site).save_model(mock.Mock(), client, form, True)
    new = TrainerClientRelation.objects.get(trainer=other, client=client)
    assert (new.status, new.client_stops_renewal) == ("approved", False)


def test_both_sides_stopping_end_it_once(make_user, api):
    from users.coaching import renew
    from users.models import CoachingEnding

    trainer, client, relation = _paid(make_user, api, "s27both")
    assert _switch(api, client, False).status_code == 200
    resp = api(trainer).post("/api/auth/trainer/unassign-client/",
                             {"client_id": client.pk, "at_period_end": True}, content_type=JSON)
    assert resp.status_code == 200 and resp.json()["scheduled"] is True
    shown = api(client).get("/api/v1/coaching/current/").json()["coaching"]
    assert (shown["ending"], shown["stop_renewal"], shown["renews_at"]) == (True, True, None)
    before = _money(trainer, client)
    _end_month(relation)
    with mock.patch("notifications.domain.dispatcher.emit_event"):
        assert renew(relation.pk) == "ended"
        assert renew(relation.pk) == "gone"
    assert _money(trainer, client) == before
    (ending,) = CoachingEnding.objects.filter(client=client)
    assert ending.ended_by == "trainer", "one ending: the trainer's, written when scheduled"


def test_stopping_works_on_every_build_and_restarting_only_where_coaching_is_paid(make_user, api):
    from users.models import TrainerClientRelation

    trainer, client, relation = _paid(make_user, api, "s27ios")
    resp = _switch(api, client, False, HTTP_X_APP_CHANNEL="ios")
    assert resp.status_code == 200, resp.content
    assert "coaching_price" not in resp.json()["coaching"], "ios may not show a coaching fee"
    resp = _switch(api, client, True, HTTP_X_APP_CHANNEL="ios")
    assert (resp.status_code, resp.json()["code"]) == (403, "not_available_on_channel")
    resp = _switch(api, client, True, HTTP_X_APP_CHANNEL="")
    assert (resp.status_code, resp.json()["code"]) == (400, "app_channel_required")
    assert TrainerClientRelation.objects.get(pk=relation.pk).client_stops_renewal is True
    assert _switch(api, client, True, HTTP_X_APP_CHANNEL="play").status_code == 200
    assert TrainerClientRelation.objects.get(pk=relation.pk).client_stops_renewal is False
