"""The console cannot start a coaching (the owner, 2026-11-24).

"The console must NOT be able to approve a coaching with no consent and no charge."
`CustomUserAdmin.save_model` synced the relation to a changed `assigned_trainer` with
`update_or_create(status='approved')`: an operator who picked a trainer turned the
client's pending request — or a rejected one, or nothing at all — into a running
coaching the client never agreed to, with no month taken. Now a trainer the client has
no approved coaching with is a field error on `assigned_trainer`, and nothing is
written: the relations, the pointer, escrow and every wallet stay as they were. A
coaching starts only in the app: the client asks, the trainer accepts, month 1 is
taken (users/trainer_relations.py).

What the console still does: put a stale pointer back in step with the coaching that
is running (writing the pointer only, never the relation), and clear a pointer no
coaching stands behind.
"""
import re
from decimal import Decimal
from unittest import mock

import pytest

from tests.test_coaching_months import (  # noqa: F401  (owners_rate is an autouse fixture)
    _accepted, _fund, owners_rate,
)
from tests.test_console_repoint_refused import _console, _post_with, _state

pytestmark = pytest.mark.django_db

JSON = "application/json"


def _charging_trainer(make_user, tag):
    return make_user(f"ccs_t_{tag}", user_type="trainer", trainer_hourly_rate=Decimal("100.00"),
                     first_name="Rami")


def _client(make_user, tag):
    client = make_user(f"ccs_c_{tag}", first_name="Sam")
    _fund(client, "1000.00")
    return client


def _asked(make_user, api, tag):
    """A client whose request to a charging trainer is waiting for the trainer."""
    from users.models import TrainerClientRelation

    trainer, client = _charging_trainer(make_user, tag), _client(make_user, tag)
    assert api(client).post("/api/auth/client/request-trainer/",
                            {"trainer_id": trainer.pk, "note": "my knee"},
                            content_type=JSON).status_code == 200
    relation = TrainerClientRelation.objects.get(trainer=trainer, client=client)
    assert relation.status == "pending"
    return trainer, client, relation


def _refused(resp, trainer):
    assert resp.status_code == 200, resp.status_code          # the form again, no 500
    html = resp.content.decode()
    assert f"no running coaching with {trainer.full_name}, and the console cannot" in html, \
        html[:3000]


def test_a_pending_request_is_not_approved_by_the_console(make_user, api):
    """The path the owner closed: it approved the request and took nothing."""
    from users.models import TrainerClientRelation

    trainer, client, relation = _asked(make_user, api, "pending")
    before = _state(trainer, client)
    _refused(_post_with(_console(make_user), client, assigned_trainer=str(trainer.pk)),
             trainer)
    assert _state(trainer, client) == before
    row = TrainerClientRelation.objects.get(pk=relation.pk)
    assert (row.status, row.initiated_by, row.client_note, row.hold_reference) == \
        ("pending", "client", "my knee", "")


def test_a_rejected_request_is_not_approved_by_the_console(make_user, api):
    from users.models import TrainerClientRelation

    trainer, client, relation = _asked(make_user, api, "rejected")
    assert api(trainer).post("/api/auth/trainer/respond-to-request/",
                             {"request_id": relation.pk, "action": "reject"},
                             content_type=JSON).status_code == 200
    before = _state(trainer, client)
    _refused(_post_with(_console(make_user), client, assigned_trainer=str(trainer.pk)),
             trainer)
    assert _state(trainer, client) == before
    assert TrainerClientRelation.objects.get(pk=relation.pk).status == "rejected"


def test_a_trainer_the_client_never_asked_is_not_assigned(make_user, api):
    from users.models import TrainerClientRelation

    trainer, client = _charging_trainer(make_user, "none"), _client(make_user, "none")
    before = _state(trainer, client)
    _refused(_post_with(_console(make_user), client, assigned_trainer=str(trainer.pk)),
             trainer)
    assert _state(trainer, client) == before
    assert not TrainerClientRelation.objects.filter(client=client).exists()


def test_the_save_itself_refuses_when_the_form_is_bypassed(make_user, api):
    """The re-check under the lock: `save_model` reached with a change the form did not
    refuse (the coaching ended between the form and the save, or a caller skipped it)."""
    from django.contrib.admin.sites import site
    from django.core.exceptions import ValidationError

    from users.admin import CustomUserAdmin
    from users.models import CustomUser, TrainerClientRelation

    trainer, client, relation = _asked(make_user, api, "bypass")
    before = _state(trainer, client)
    client.assigned_trainer = trainer
    with pytest.raises(ValidationError,
                       match=re.escape(f"no running coaching with {trainer.full_name},")):
        CustomUserAdmin(CustomUser, site).save_model(
            mock.Mock(), client, mock.Mock(changed_data=["assigned_trainer"]), True)
    assert _state(trainer, client) == before
    assert TrainerClientRelation.objects.get(pk=relation.pk).status == "pending"


def test_a_stale_pointer_goes_back_in_step_and_the_coaching_is_not_rewritten(
        make_user, api):
    """The one thing the console still does, and it writes the pointer only: the
    coaching's author, its note and both of its ends are the coaching's."""
    from users.models import CustomUser, TrainerClientRelation

    trainer, client, relation = _accepted(make_user, api, "ccs_ok")
    TrainerClientRelation.objects.filter(pk=relation.pk).update(
        client_note="from the client", client_stops_renewal=True)
    before = TrainerClientRelation.objects.filter(pk=relation.pk).values().get()
    CustomUser.objects.filter(pk=client.pk).update(assigned_trainer=None)   # out of step
    resp = _post_with(_console(make_user), client, assigned_trainer=str(trainer.pk))
    assert resp.status_code == 302, resp.content.decode()[:2000]
    assert CustomUser.objects.get(pk=client.pk).assigned_trainer_id == trainer.pk
    assert TrainerClientRelation.objects.filter(pk=relation.pk).values().get() == before


def test_a_pointer_no_coaching_stands_behind_can_be_cleared(make_user, api):
    from users.models import CustomUser

    trainer, client, _relation = _asked(make_user, api, "clear")
    CustomUser.objects.filter(pk=client.pk).update(assigned_trainer=trainer)  # stale
    resp = _post_with(_console(make_user), client, assigned_trainer="")
    assert resp.status_code == 302, resp.content.decode()[:2000]
    assert CustomUser.objects.get(pk=client.pk).assigned_trainer_id is None
