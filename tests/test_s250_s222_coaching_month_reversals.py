"""SIDE_BUGS S-250 and S-222 (PAY-1): a coaching month's money moves once, and a held
month support gives back is given back with its month.

S-250: `POST wallet/admin/reversal/` accepted one leg of a paid coaching month — the
trainer's payout or the platform's commission — and put it back into the per-currency
escrow that backs every client's hold while the month stayed paid and the client
unrefunded (measured: both legs of month 1 reversed left 100.00 in escrow no hold owns).
The same endpoint reversed the CHARGE of a month already paid out or given back, which
paid the client out of other clients' holds. Both are now `409 coaching_month_leg`.

S-222: reversing the charge of a month still HELD is support's recovery for a stuck hold
and still works, but the month read `held` until the hold was next touched and nobody
was told. It is now marked `refunded`, the coaching forgets the hold, and both people get
`coaching_fee_refunded`. A hold reversed some other way is caught by `_move_hold` as
before, which now tells both people too.
"""
from datetime import timedelta
from decimal import Decimal

import pytest
from django.utils import timezone

from tests.test_coaching_months import (  # noqa: F401  (owners_rate is an autouse fixture)
    _accepted, _balance, _deliver_first_program, _escrow, _platform, owners_rate,
)

pytestmark = pytest.mark.django_db

JSON = "application/json"
REVERSAL = "/api/wallet/admin/reversal/"


def _reverse(api, admin, row, key, **extra):
    return api(admin).post(REVERSAL, {"reference_id": row.reference_id,
                                      "idempotency_key": key, "reason": "support"},
                           content_type=JSON, **extra)


def _row(reference):
    from wallet.models import Transaction

    return Transaction.objects.get(reference_id=reference)


def _books(trainer, client):
    from wallet.models import Transaction

    return (Transaction.objects.count(), _balance(trainer), _balance(client), _escrow(),
            _platform())


def _notices(user, event_type):
    from notifications.models import Notification

    return list(Notification.objects.filter(recipient=user, event_type=event_type))


def _refused(resp, period):
    body = resp.json()
    assert (resp.status_code, body["code"]) == (409, "coaching_month_leg"), resp.content
    assert body["error"]
    assert body["period"] == {"id": period.pk, "number": period.number,
                              "status": period.status}


# ------------------------------------------------------------------------- S-250
@pytest.mark.parametrize("leg", ["payout", "commission", "charge"])
def test_no_part_of_a_paid_month_is_reversed(make_user, api, leg):
    trainer, client, relation = _accepted(make_user, api, f"s250_{leg}")
    _deliver_first_program(api, trainer, client)
    (month,) = relation.periods.all()
    assert month.status == "settled"
    row = _row({"payout": month.payout_reference, "commission": month.commission_reference,
                "charge": month.hold_reference}[leg])
    before = _books(trainer, client)

    _refused(_reverse(api, make_user(f"s250_{leg}_admin", user_type="admin"), row,
                      f"s250-{leg}"), month)
    assert _books(trainer, client) == before, "nothing moved"
    month.refresh_from_db()
    assert month.status == "settled"


@pytest.mark.parametrize("leg", ["release", "charge"])
def test_no_part_of_a_month_given_back_is_reversed(make_user, api, leg):
    """Given back by the coaching (an unassign before the first programme): the client
    must not be refunded twice, nor the release undone into escrow."""
    from wallet.models import Transaction

    trainer, client, relation = _accepted(make_user, api, f"s250_back_{leg}")
    (month,) = relation.periods.all()
    assert api(client).post("/api/auth/client/unassign-trainer/",
                            content_type=JSON).status_code == 200
    month.refresh_from_db()
    assert month.status == "refunded" and _balance(client) == Decimal("1000.00")
    release = Transaction.objects.get(metadata__hold_reference=month.hold_reference,
                                      metadata__purpose="trainer_hold_release")
    row = release if leg == "release" else _row(month.hold_reference)
    before = _books(trainer, client)

    _refused(_reverse(api, make_user(f"s250_back_{leg}_admin", user_type="admin"), row,
                      f"s250-back-{leg}"), month)
    assert _books(trainer, client) == before
    assert _balance(client) == Decimal("1000.00"), "refunded once"


def test_a_renewal_is_refused_whole(make_user, api):
    """A renewal is charged and paid out in one step, so neither its charge nor its
    payout is ever a held month."""
    from users.coaching import renew

    trainer, client, relation = _accepted(make_user, api, "s250_renew")
    _deliver_first_program(api, trainer, client)
    first = relation.periods.get(number=1)
    shift = first.ends_at - (timezone.now() - timedelta(minutes=1))
    type(first).objects.filter(pk=first.pk).update(starts_at=first.starts_at - shift,
                                                   ends_at=first.ends_at - shift)
    assert renew(relation.pk) == "renewed"
    second = relation.periods.get(number=2)
    admin = make_user("s250_renew_admin", user_type="admin")
    before = _books(trainer, client)
    for name, reference in (("charge", second.hold_reference),
                            ("payout", second.payout_reference)):
        _refused(_reverse(api, admin, _row(reference), f"s250-renew-{name}"), second)
    assert _books(trainer, client) == before


def test_the_refusal_answers_in_arabic(make_user, api):
    trainer, client, relation = _accepted(make_user, api, "s250_ar")
    _deliver_first_program(api, trainer, client)
    (month,) = relation.periods.all()
    resp = _reverse(api, make_user("s250_ar_admin", user_type="admin"),
                    _row(month.payout_reference), "s250-ar", HTTP_ACCEPT_LANGUAGE="ar")
    assert resp.json()["error"] == \
        "هذا جزء من شهر تدريب دُفعت أمواله أو أُعيدت، ولا يمكن عكسه وحده."


# ------------------------------------------------------------------------- S-222
def test_a_held_month_given_back_by_support_is_recorded_and_told(
        make_user, api, django_capture_on_commit_callbacks):
    trainer, client, relation = _accepted(make_user, api, "s222_back")
    (month,) = relation.periods.all()
    assert (month.status, _balance(client)) == ("held", Decimal("900.00"))
    escrow_before = _escrow()

    with django_capture_on_commit_callbacks(execute=True):
        resp = _reverse(api, make_user("s222_admin", user_type="admin"),
                        _row(month.hold_reference), "s222-back")
    assert resp.status_code == 200, resp.content
    assert _balance(client) == Decimal("1000.00")
    assert escrow_before - _escrow() == Decimal("100.00")
    month.refresh_from_db()
    relation.refresh_from_db()
    assert month.status == "refunded", "the month no longer reads held"
    assert relation.hold_reference == "", "the coaching forgot the hold"
    assert len(_notices(client, "coaching_fee_refunded")) == 1
    assert len(_notices(trainer, "coaching_fee_refunded")) == 1

    # The first programme now pays nothing, and the escrow is untouched.
    books = _books(trainer, client)
    _deliver_first_program(api, trainer, client)
    assert _books(trainer, client) == books
    month.refresh_from_db()
    assert month.status == "refunded"


def test_a_settlement_that_won_the_lock_makes_the_reversal_refuse(make_user, api):
    """The month is re-read under the relation's and the month's locks: when the
    settlement paid it out first, the reversal moves nothing."""
    from django.db import transaction

    from wallet.views import _MonthNotHeld, _reverse_held_month

    trainer, client, relation = _accepted(make_user, api, "s222_race")
    (month,) = relation.periods.all()
    charge = _row(month.hold_reference)          # read while the month was held
    _deliver_first_program(api, trainer, client)  # ... then the settlement ran
    before = _books(trainer, client)
    with pytest.raises(_MonthNotHeld), transaction.atomic():
        _reverse_held_month(charge, actor_id=None, reason="race")
    assert _books(trainer, client) == before


def test_a_hold_reversed_another_way_is_told_when_the_coaching_finds_it(
        make_user, api, django_capture_on_commit_callbacks):
    """`_move_hold`'s already-reversed branch marks the month refunded and now tells
    both people, as a release does."""
    from wallet.models import move_funds_atomic

    trainer, client, relation = _accepted(make_user, api, "s222_old")
    (month,) = relation.periods.all()
    charge = _row(month.hold_reference)
    # A reversal written before S-222: the money back, the month and the coaching untold.
    move_funds_atomic(charge.destination_wallet, charge.source_wallet, charge.amount,
                      actor_id=None, tx_type="reversal", money_type=charge.money_type,
                      metadata={"original_reference": charge.reference_id},
                      reverses=charge)
    with django_capture_on_commit_callbacks(execute=True):
        _deliver_first_program(api, trainer, client)
    month.refresh_from_db()
    assert month.status == "refunded"
    assert _balance(client) == Decimal("1000.00") and _balance(trainer) == Decimal("0.00")
    assert len(_notices(client, "coaching_fee_refunded")) == 1
    assert len(_notices(trainer, "coaching_fee_refunded")) == 1
