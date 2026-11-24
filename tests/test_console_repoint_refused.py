"""The console refuses a change of `assigned_trainer` that would end another approved
coaching (the phase-3 review, round 4, 2026-11-24).

`CustomUserAdmin.save_model` synced the relation to the pointer by approving the new
trainer's row and then rejecting the client's other approved coaching with a bare UPDATE.
Pointing a client with a running coaching at a second trainer hit the one-approved-per-
client constraint first: `IntegrityError`, a 500. And had it got through, the old
coaching was rejected around `release_hold` / `end_coaching`, leaving a held month in
escrow on a rejected row; clearing the pointer did exactly that. Now either change is
a field error on `assigned_trainer` telling the operator to end that coaching first
(the trainer or the client ends it in the app, which settles its money), and nothing is
written: the relations, the pointer, escrow and every wallet stay as they were.

The relations are also locked before the user row is written, so two saves at once
cannot keep a stale end, in the lock order of users/trainer_relations.py.
"""
import pytest
from django.db import connection
from django.test import Client
from django.test.utils import CaptureQueriesContext

from tests.admin_form import change_form_data
from tests.test_coaching_months import (  # noqa: F401  (owners_rate is an autouse fixture)
    _accepted, _balance, _escrow, _platform, owners_rate,
)

pytestmark = pytest.mark.django_db


def _console(make_user):
    console = Client()
    console.force_login(make_user("repoint_operator", user_type="admin"))
    return console


def _page(client_user):
    return f"/dj-admin/users/customuser/{client_user.pk}/change/"


def _post_with(console, client_user, **changes):
    page = console.get(_page(client_user))
    assert page.status_code == 200
    data = change_form_data(page.content.decode())
    data.update(changes)
    data["_save"] = "Save"
    return console.post(_page(client_user), data)


def _state(trainer, client, *others):
    from users.models import CustomUser, TrainerClientRelation

    rows = sorted(TrainerClientRelation.objects.filter(client=client).values_list(
        "trainer_id", "status", "hold_reference", "ends_at_period_end",
        "client_stops_renewal"))
    pointer = CustomUser.objects.get(pk=client.pk).assigned_trainer_id
    money = (_balance(client), _balance(trainer), _escrow(), _platform(),
             *(_balance(o) for o in others))
    return rows, pointer, money


def test_pointing_a_coached_client_at_another_trainer_is_refused(make_user, api):
    trainer, client, _relation = _accepted(make_user, api, "repoint")   # month 1 held
    other = make_user("repoint_other", user_type="trainer", first_name="Lina")
    before = _state(trainer, client, other)
    console = _console(make_user)

    resp = _post_with(console, client, assigned_trainer=str(other.pk))
    assert resp.status_code == 200, resp.status_code          # the form again, no 500
    html = resp.content.decode()
    assert f"running coaching with {trainer.full_name}. To change trainer" in html, \
        html[:3000]
    assert _state(trainer, client, other) == before


def test_clearing_the_pointer_of_a_coached_client_is_refused(make_user, api):
    trainer, client, _relation = _accepted(make_user, api, "repoint_clear")
    before = _state(trainer, client)
    resp = _post_with(_console(make_user), client, assigned_trainer="")
    assert resp.status_code == 200, resp.status_code
    assert "running coaching with" in resp.content.decode()
    assert _state(trainer, client) == before


def test_putting_a_stale_pointer_back_in_step_still_saves(make_user, api):
    from users.models import CustomUser

    trainer, client, _relation = _accepted(make_user, api, "repoint_ok")
    CustomUser.objects.filter(pk=client.pk).update(assigned_trainer=None)
    resp = _post_with(_console(make_user), client, assigned_trainer=str(trainer.pk))
    assert resp.status_code == 302, resp.content.decode()[:2000]
    assert CustomUser.objects.get(pk=client.pk).assigned_trainer_id == trainer.pk


def test_the_relations_are_locked_before_the_user_row_is_written(make_user, api):
    from users.models import CustomUser

    trainer, client, _relation = _accepted(make_user, api, "repoint_lock")
    CustomUser.objects.filter(pk=client.pk).update(assigned_trainer=None)
    console = _console(make_user)
    page = console.get(_page(client))
    data = change_form_data(page.content.decode())
    data.update(assigned_trainer=str(trainer.pk), _save="Save")
    with CaptureQueriesContext(connection) as queries:
        assert console.post(_page(client), data).status_code == 302
    sqls = [q["sql"] for q in queries.captured_queries]
    lock = next(i for i, sql in enumerate(sqls)
                if 'FROM "users_trainerclientrelation"' in sql and "FOR UPDATE" in sql)
    write = next(i for i, sql in enumerate(sqls) if sql.startswith('UPDATE "users_customuser"'))
    assert lock < write, (sqls[lock][:160], sqls[write][:160])


def test_a_coaching_approved_after_the_form_was_checked_is_still_refused(make_user, api):
    """The re-check under the lock: `save_model` reached with a change the form did not
    see (a coaching approved in between) raises, and writes nothing."""
    from unittest import mock

    from django.contrib.admin.sites import site
    from django.core.exceptions import ValidationError

    from users.admin import CustomUserAdmin
    from users.models import CustomUser

    trainer, client, _relation = _accepted(make_user, api, "repoint_race")
    other = make_user("repoint_race_other", user_type="trainer")
    before = _state(trainer, client, other)
    client.refresh_from_db()
    client.assigned_trainer = other
    with pytest.raises(ValidationError, match="running coaching with"):
        CustomUserAdmin(CustomUser, site).save_model(
            mock.Mock(), client, mock.Mock(changed_data=["assigned_trainer"]), True)
    assert _state(trainer, client, other) == before
