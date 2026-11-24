"""G5's console path (2026-11-24): an operator can no longer pay a trainer month 1 by
putting a coached client on an empty or retired routine in the console.

G5 closed the API (`assign_routine`, 400 `routine_empty` / `routine_inactive`), but the
console's routine form (`routine/admin.py` `OneClientRoutineAdminMixin.save_related`)
settled the coaching's first month for every client it added, whatever the routine held.
Now the console applies the API's rule (`routine.assignment.refuse_undeliverable`): the
form refuses on `assigned_to`, in the console's language, and the save asks again under
the routine's lock, so nothing half-applies — no client added, no ledger row, month 1
still held. A routine with exercises still pays month 1 through the console, once.
"""
from decimal import Decimal

import pytest

from tests.routine_rows import deliverable_routine
from tests.test_coaching_months import (  # noqa: F401  (owners_rate is an autouse fixture)
    _accepted, _balance, _escrow, _platform, owners_rate,
)
from tests.test_fx3_g5_assignment_guard import _held, _money

pytestmark = pytest.mark.django_db

CHANGE = "/dj-admin/routine/routine/{}/change/"


@pytest.fixture
def operator(make_user):
    user = make_user("g5c_operator", user_type="admin")
    user.is_superuser = True
    user.save()
    return user


def _form_data(routine, assignees, *, is_active=None):
    data = {"name": routine.name, "description": routine.description or "",
            "difficulty_level": routine.difficulty_level,
            "estimated_duration": routine.estimated_duration,
            "start_date": routine.start_date.isoformat(),
            "end_date": routine.end_date.isoformat() if routine.end_date else "",
            "assigned_to": [person.pk for person in assignees],
            "exercises": (list(routine.exercises.values_list("pk", flat=True))
                          if routine.pk else [])}
    if routine.is_active if is_active is None else is_active:
        data["is_active"] = "on"
    return data


def _console_save(client, operator, routine, assignees, *, lang=None, **form):
    client.force_login(operator)
    extra = {"HTTP_ACCEPT_LANGUAGE": lang} if lang else {}
    return client.post(CHANGE.format(routine.pk),
                       {**_form_data(routine, assignees, **form), "_save": "Save"}, **extra)


def _assignment_errors(resp):
    assert resp.status_code == 200, (resp.status_code, resp.get("Location"))
    form = resp.context["adminform"].form
    return [(error.code, " ".join(error.messages))
            for error in form.errors.as_data().get("assigned_to", [])]


@pytest.mark.parametrize("why", ["routine_empty", "routine_inactive"])
def test_the_console_refuses_an_empty_or_retired_routine_and_pays_nothing(
        make_user, api, client, operator, why):
    from routine.models import Routine

    trainer, member, relation = _accepted(make_user, api, f"g5c_{why}")
    if why == "routine_empty":
        routine = Routine.objects.create(name="Empty", created_by=trainer, days=3)
    else:
        routine = deliverable_routine(trainer, "Retired")
        Routine.objects.filter(pk=routine.pk).update(is_active=False)
        routine.refresh_from_db()
    before, held = _money(trainer, member), _held(relation)
    assert held[0] == "held"

    resp = _console_save(client, operator, routine, [member])
    [(code, message)] = _assignment_errors(resp)
    assert code == why and message
    assert _money(trainer, member) == before, "no ledger row, no balance moved"
    assert _held(relation) == held, "month 1 is still held"
    assert not routine.assigned_to.filter(pk=member.pk).exists()


def test_retiring_and_assigning_in_one_save_is_refused(make_user, api, client, operator):
    trainer, member, relation = _accepted(make_user, api, "g5c_retire_now")
    routine = deliverable_routine(trainer, "Retire now")
    before, held = _money(trainer, member), _held(relation)

    resp = _console_save(client, operator, routine, [member], is_active=False)
    [(code, _message)] = _assignment_errors(resp)
    assert code == "routine_inactive"
    routine.refresh_from_db()
    assert routine.is_active, "the refused save changed nothing"
    assert _money(trainer, member) == before and _held(relation) == held
    assert not routine.assigned_to.exists()


def test_a_new_routine_cannot_be_given_a_client_in_the_same_save(make_user, api, operator):
    """A routine not saved yet has no exercise rows: the add form refuses the client.
    (Built with its creator set: the console's add page cannot save a routine at all
    today — `created_by` is read-only there and the model's validation reads it.)"""
    from django.test import RequestFactory

    from admin_dashboard.admin import admin_site
    from routine.models import Routine

    trainer, member, _relation = _accepted(make_user, api, "g5c_new")
    request = RequestFactory().post("/dj-admin/routine/routine/add/")
    request.user = operator
    blank = Routine(name="Made in the console", created_by=trainer)
    form = admin_site._registry[Routine].get_form(request, None, change=False)(
        _form_data(blank, [member]), instance=blank)
    assert not form.is_valid()
    assert [e.code for e in form.errors.as_data()["assigned_to"]] == ["routine_empty"]


def test_the_console_refusal_reads_in_the_console_language(make_user, api, client, operator):
    from routine.models import Routine

    trainer, member, _relation = _accepted(make_user, api, "g5c_ar")
    routine = Routine.objects.create(name="Empty", created_by=trainer, days=1)
    resp = _console_save(client, operator, routine, [member], lang="ar")
    [(code, message)] = _assignment_errors(resp)
    assert (code, message) == (
        "routine_empty", "لا يحتوي هذا البرنامج على تمارين بعد. أضف تمارين قبل إسناده.")


def test_a_routine_emptied_after_the_form_was_checked_is_refused_by_the_save(
        make_user, api, operator):
    """The form's check and the save are two reads; the save's, under the routine's lock,
    decides. Raising there undoes the whole save (the admin view is one transaction)."""
    from django.db import transaction
    from django.test import RequestFactory

    from admin_dashboard.admin import admin_site
    from routine.assignment import RoutineEmpty
    from routine.models import Routine, RoutineExercise

    trainer, member, relation = _accepted(make_user, api, "g5c_race")
    routine = deliverable_routine(trainer, "Race")
    before, held = _money(trainer, member), _held(relation)

    model_admin = admin_site._registry[Routine]
    request = RequestFactory().post(CHANGE.format(routine.pk))
    request.user = operator
    form = model_admin.get_form(request, routine, change=True)(
        _form_data(routine, [member]), instance=routine)
    assert form.is_valid(), form.errors
    RoutineExercise.objects.filter(routine=routine).delete()   # emptied meanwhile

    with pytest.raises(RoutineEmpty), transaction.atomic():
        saved = model_admin.save_form(request, form, change=True)
        model_admin.save_model(request, saved, form, True)
        model_admin.save_related(request, form, [], True)
    assert not Routine.objects.get(pk=routine.pk).assigned_to.exists()
    assert _money(trainer, member) == before and _held(relation) == held


def test_a_routine_with_exercises_pays_month_one_once_through_the_console(
        make_user, api, client, operator):
    from wallet.models import Transaction

    trainer, member, relation = _accepted(make_user, api, "g5c_pays")
    routine = deliverable_routine(trainer)
    escrow_before, platform_before = _escrow(), _platform()

    resp = _console_save(client, operator, routine, [member])
    assert resp.status_code == 302, _assignment_errors(resp)
    assert routine.assigned_to.filter(pk=member.pk).exists()
    (month,) = relation.periods.all()
    assert (month.status, month.commission, month.trainer_share) == \
        ("settled", Decimal("20.00"), Decimal("80.00"))
    assert _balance(trainer) == Decimal("80.00")
    assert escrow_before - _escrow() == Decimal("100.00")
    assert _platform() - platform_before == Decimal("20.00")

    # Saved again (renamed, the same client): nothing more is paid.
    rows = Transaction.objects.count()
    routine.name = "Month one, renamed"
    assert _console_save(client, operator, routine, [member]).status_code == 302
    assert _balance(trainer) == Decimal("80.00") and Transaction.objects.count() == rows
