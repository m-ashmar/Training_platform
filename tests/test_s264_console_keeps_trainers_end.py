"""SIDE_BUGS S-264: an operator re-pointing a running coaching in the console keeps the
trainer's scheduled end.

`CustomUserAdmin.save_model` syncs the relation when `assigned_trainer` changes. On a
coaching already running it wrote `ends_at_period_end=False`, so an operator fixing a
stale pointer cancelled the trainer's «end when this month ends»: the coaching renewed
and the client was charged another month. S2.7 already kept the client's «do not renew»
on that path; the trainer's end is kept the same way now, and reset only when the save
makes a NEW coaching.
"""
from decimal import Decimal
from unittest import mock

import pytest

from tests.test_coaching_months import (  # noqa: F401  (owners_rate is an autouse fixture)
    _accepted, _balance, _deliver_first_program, _end_month, owners_rate,
)

pytestmark = pytest.mark.django_db

JSON = "application/json"


def _repoint(client, trainer):
    """The console's save of the client with `assigned_trainer` changed."""
    from django.contrib.admin.sites import site

    from users.admin import CustomUserAdmin
    from users.models import CustomUser

    client.refresh_from_db()
    client.assigned_trainer = trainer
    form = mock.Mock(changed_data=["assigned_trainer"])
    CustomUserAdmin(CustomUser, site).save_model(mock.Mock(), client, form, True)


def test_a_repoint_keeps_the_trainers_scheduled_end_and_nothing_is_charged(
        make_user, api, django_capture_on_commit_callbacks):
    from users.coaching import renew
    from users.models import CustomUser, TrainerClientRelation

    trainer, client, relation = _accepted(make_user, api, "s264")
    _deliver_first_program(api, trainer, client)
    scheduled = api(trainer).post("/api/auth/trainer/unassign-client/",
                                  {"client_id": client.pk, "at_period_end": True},
                                  content_type=JSON)
    assert scheduled.status_code == 200 and scheduled.json()["scheduled"] is True

    CustomUser.objects.filter(pk=client.pk).update(assigned_trainer=None)   # out of step
    _repoint(client, trainer)
    row = TrainerClientRelation.objects.get(pk=relation.pk)
    assert (row.status, row.ends_at_period_end) == ("approved", True)

    before = _balance(client)
    _end_month(relation)
    with mock.patch("notifications.domain.dispatcher.emit_event"), \
            django_capture_on_commit_callbacks(execute=True):
        assert renew(relation.pk) == "ended"
    assert _balance(client) == before == Decimal("900.00")
    assert not TrainerClientRelation.objects.filter(pk=relation.pk).exists()


def test_a_new_coaching_the_operator_makes_starts_without_an_end(make_user, api):
    from users.models import TrainerClientRelation

    trainer, client, relation = _accepted(make_user, api, "s264new")
    other = make_user("s264_t2", user_type="trainer", first_name="Lina")
    TrainerClientRelation.objects.create(trainer=other, client=client, status="rejected",
                                         initiated_by="client", ends_at_period_end=True)
    # The first coaching already over (re-pointing straight from one approved coaching to
    # another is the one-approved-per-client constraint's, not this row's).
    TrainerClientRelation.objects.filter(pk=relation.pk).update(status="rejected")
    _repoint(client, other)
    new = TrainerClientRelation.objects.get(trainer=other, client=client)
    assert (new.status, new.ends_at_period_end) == ("approved", False)
