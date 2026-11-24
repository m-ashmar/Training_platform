"""G5 (the backlog re-verification, 2026-11-24): an empty or retired routine no longer
pays the trainer month 1.

Assigning a coached client their first programme is what moves the month held in escrow
on acceptance to the trainer (`routine.assignment.settle_first_month`). `assign_to_client`
and `assign_routine` checked neither that the routine had exercises nor that it was
active: a trainer could accept, assign an empty or retired routine, and be paid month 1
for nothing. Only `templates/{id}/generate/` refused an empty template
(`template_empty`). Now `assign_routine` refuses both, under the routine's lock and
before the client is added or anything is settled: 400 `routine_empty` /
`routine_inactive`, no ledger row, the month still held. A routine with exercises still
pays month 1, exactly once.
"""
from decimal import Decimal

import pytest

from tests.routine_rows import deliverable_routine
from tests.test_coaching_months import (  # noqa: F401  (owners_rate is an autouse fixture)
    _accepted, _balance, _escrow, _platform, owners_rate,
)

pytestmark = pytest.mark.django_db

JSON = "application/json"


def _assign(api, trainer, routine, client):
    return api(trainer).post(f"/api/routine/routines/{routine.pk}/assign_to_client/",
                             {"client_id": client.pk}, content_type=JSON)


def _money(trainer, client):
    from wallet.models import Transaction

    return (Transaction.objects.count(), _balance(trainer), _balance(client), _escrow(),
            _platform())


def _held(relation):
    (month,) = relation.periods.all()
    return month.status, month.payout_reference


@pytest.mark.parametrize("why", ["routine_empty", "routine_inactive"])
def test_an_empty_or_retired_routine_is_refused_and_pays_nothing(make_user, api, why):
    from routine.models import Routine

    trainer, client, relation = _accepted(make_user, api, f"g5_{why}")
    if why == "routine_empty":
        routine = Routine.objects.create(name="Empty", created_by=trainer, days=3)
    else:
        routine = deliverable_routine(trainer, "Retired")
        Routine.objects.filter(pk=routine.pk).update(is_active=False)
    before, held = _money(trainer, client), _held(relation)
    assert held[0] == "held"

    resp = _assign(api, trainer, routine, client)
    assert (resp.status_code, resp.json()["code"], resp.json()["routine_id"]) == \
        (400, why, routine.pk), resp.content
    assert resp.json()["error"]
    assert _money(trainer, client) == before, "no ledger row, no balance moved"
    assert _held(relation) == held, "month 1 is still held"
    assert not routine.assigned_to.filter(pk=client.pk).exists()


def test_the_refusal_answers_in_arabic(make_user, api):
    from routine.models import Routine

    trainer, client, _relation = _accepted(make_user, api, "g5_ar")
    routine = Routine.objects.create(name="Empty", created_by=trainer, days=1)
    resp = api(trainer).post(f"/api/routine/routines/{routine.pk}/assign_to_client/",
                             {"client_id": client.pk}, content_type=JSON,
                             HTTP_ACCEPT_LANGUAGE="ar")
    assert resp.json()["error"] == "لا يحتوي هذا البرنامج على تمارين بعد. أضف تمارين قبل إسناده."


def test_the_service_refuses_before_the_client_is_added(make_user, api):
    """The guard is in `assign_routine`, which `generate` calls too."""
    from routine.assignment import RoutineEmpty, assign_routine
    from routine.models import Routine

    trainer, client, relation = _accepted(make_user, api, "g5_service")
    routine = Routine.objects.create(name="Empty", created_by=trainer, days=1)
    before, held = _money(trainer, client), _held(relation)
    with pytest.raises(RoutineEmpty):
        assign_routine(routine, client, trainer)
    assert _money(trainer, client) == before and _held(relation) == held
    assert not routine.assigned_to.exists()


def test_a_routine_with_exercises_still_pays_month_one_exactly_once(make_user, api):
    from routine.models import Routine
    from wallet.models import Transaction

    trainer, client, relation = _accepted(make_user, api, "g5_pays")
    # Refused first, then the trainer adds an exercise: the month waited, and is paid once.
    empty = Routine.objects.create(name="Empty", created_by=trainer, days=1)
    assert _assign(api, trainer, empty, client).status_code == 400
    escrow_before, platform_before = _escrow(), _platform()

    routine = deliverable_routine(trainer)
    assert _assign(api, trainer, routine, client).status_code == 200
    (month,) = relation.periods.all()
    assert (month.status, month.commission, month.trainer_share) == \
        ("settled", Decimal("20.00"), Decimal("80.00"))
    assert _balance(trainer) == Decimal("80.00")
    assert escrow_before - _escrow() == Decimal("100.00")
    assert _platform() - platform_before == Decimal("20.00")
    paid = Transaction.objects.filter(reference_id__in=[month.payout_reference,
                                                        month.commission_reference])
    assert paid.count() == 2

    # A second programme pays nothing more.
    rows = Transaction.objects.count()
    assert _assign(api, trainer, deliverable_routine(trainer, "Second"), client) \
        .status_code == 200
    assert _balance(trainer) == Decimal("80.00") and Transaction.objects.count() == rows
