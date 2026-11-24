"""The fix run's RTN-1, G6 (2026-11-24): a routine has 1..365 days. `Routine.days` had no
bound and `RoutineSerializer` added none, while assigning builds one `RoutineProgress` row
per day per client in a Python list: `days: 2000000000` took the server's memory in one
request (`generate` already capped 365, a bought program 365, a hand-written routine
nothing). Now 400 `validation_error` with `field_errors.days[0].code`
`days_out_of_range` on the API, and the model's validators on every other write (the
console's included) through `validate_row`. Migration 0038 is validators only: no SQL.

The refusals fail on the code before; the others hold what stays.
"""
import uuid

import pytest

pytestmark = pytest.mark.django_db

JSON = "application/json"
ROUTINES = "/api/routine/routines/"


def _name(tag):
    return f"g6_{tag}_{uuid.uuid4().hex[:6]}"


def _field_code(resp, field):
    return resp.json()["field_errors"][field][0]["code"]


@pytest.mark.parametrize("days", [0, 366, 2000000000])
def test_a_routine_outside_1_to_365_days_is_refused(make_user, api, days):
    from routine.models import Routine

    trainer = make_user(_name("t"), user_type="trainer")
    resp = api(trainer).post(ROUTINES, {"name": _name("r"), "days": days},
                             content_type=JSON)
    assert resp.status_code == 400, resp.content
    assert _field_code(resp, "days") == "days_out_of_range"
    assert not Routine.objects.filter(created_by=trainer).exists()


def test_an_edit_past_the_bound_is_refused_and_changes_nothing(make_user, api):
    from routine.models import Routine

    trainer = make_user(_name("t2"), user_type="trainer")
    routine = Routine.objects.create(name=_name("r2"), created_by=trainer, days=4)
    resp = api(trainer).patch(f"{ROUTINES}{routine.pk}/", {"days": 400},
                              content_type=JSON)
    assert (resp.status_code, _field_code(resp, "days")) == (400, "days_out_of_range")
    routine.refresh_from_db()
    assert routine.days == 4


@pytest.mark.parametrize("days", [1, 365])
def test_the_bounds_themselves_are_accepted(make_user, api, days):
    trainer = make_user(_name("t3"), user_type="trainer")
    resp = api(trainer).post(ROUTINES, {"name": _name("r3"), "days": days},
                             content_type=JSON)
    assert resp.status_code == 201, resp.content
    assert resp.json()["days"] == days


def test_no_days_still_takes_the_default(make_user, api):
    trainer = make_user(_name("t4"), user_type="trainer")
    resp = api(trainer).post(ROUTINES, {"name": _name("r4")}, content_type=JSON)
    assert resp.status_code == 201, resp.content
    assert resp.json()["days"] == 3


def test_every_other_writer_meets_the_model_bound(make_user):
    """The console and any service write through `save()`, which runs `validate_row`."""
    from django.core.exceptions import ValidationError

    from routine.models import MAX_ROUTINE_DAYS, Routine

    trainer = make_user(_name("t5"), user_type="trainer")
    with pytest.raises(ValidationError) as refused:
        Routine.objects.create(name=_name("r5"), created_by=trainer,
                               days=MAX_ROUTINE_DAYS + 1)
    assert "days" in refused.value.message_dict
    assert not Routine.objects.filter(created_by=trainer).exists()
