"""SIDE_BUGS S-266: a FROZEN coaching the trainer scheduled to end is woken by the hourly
run and ended, with nothing charged and nothing held.

`_due` woke a frozen coaching when the wallet covered the month, a reminder step came
round, or (S2.7) the client asked not to renew. One the TRAINER had scheduled to end was
not woken: it stayed frozen for days after the month it was meant to end with. `renew`
already ends it before reading any money; the run now gets it there. The flag reaches a
frozen coaching through the default admin site's relation form (every field of the row
is editable there), which is how this test sets it.
"""
from decimal import Decimal
from unittest import mock

import pytest

from tests.test_coaching_months import (  # noqa: F401  (owners_rate is an autouse fixture)
    _accepted, _balance, _deliver_first_program, _end_month, _escrow, _fund, _platform,
    owners_rate,
)

pytestmark = pytest.mark.django_db


def _frozen_with_the_trainers_end(make_user, api, tag, django_capture_on_commit_callbacks):
    from users.coaching import renew
    from users.models import TrainerClientRelation

    trainer, client, relation = _accepted(make_user, api, tag)
    _deliver_first_program(api, trainer, client)
    _fund(client, "50.00")                       # short of the 100.00 month
    _end_month(relation)
    with mock.patch("notifications.domain.dispatcher.emit_event"), \
            django_capture_on_commit_callbacks(execute=True):
        assert renew(relation.pk) == "frozen"
    TrainerClientRelation.objects.filter(pk=relation.pk).update(ends_at_period_end=True)
    return trainer, client, relation


def test_the_hourly_run_wakes_it_and_ends_it_with_nothing_charged(
        make_user, api, django_capture_on_commit_callbacks):
    from users.coaching import due_relation_ids, renew_due
    from users.models import TrainerClientRelation

    trainer, client, relation = _frozen_with_the_trainers_end(
        make_user, api, "s266", django_capture_on_commit_callbacks)
    assert relation.pk in due_relation_ids()
    money = (_balance(client), _balance(trainer), _platform(), _escrow())
    with mock.patch("notifications.domain.dispatcher.emit_event"), \
            django_capture_on_commit_callbacks(execute=True):
        renew_due()
    assert not TrainerClientRelation.objects.filter(pk=relation.pk).exists()
    assert (_balance(client), _balance(trainer), _platform(), _escrow()) == money


def test_a_top_up_landing_meanwhile_is_not_charged(
        make_user, api, django_capture_on_commit_callbacks):
    from users.coaching import due_relation_ids, renew_due
    from users.models import TrainerClientRelation

    trainer, client, relation = _frozen_with_the_trainers_end(
        make_user, api, "s266top", django_capture_on_commit_callbacks)
    _fund(client, "5000.00")                     # a top-up arrives while it is frozen
    assert relation.pk in due_relation_ids()
    escrow_before, platform_before = _escrow(), _platform()
    with mock.patch("notifications.domain.dispatcher.emit_event"), \
            django_capture_on_commit_callbacks(execute=True):
        renew_due()
    assert not TrainerClientRelation.objects.filter(pk=relation.pk).exists()
    assert _balance(client) == Decimal("5000.00")
    assert (_escrow(), _platform()) == (escrow_before, platform_before)


def test_a_frozen_coaching_with_no_end_and_nothing_due_still_sleeps(
        make_user, api, django_capture_on_commit_callbacks):
    """The wake is for an end only: an ordinary frozen coaching with no top-up and no
    reminder step is still left alone, as before."""
    from users.coaching import due_relation_ids
    from users.models import TrainerClientRelation

    _trainer, _client, relation = _frozen_with_the_trainers_end(
        make_user, api, "s266sleep", django_capture_on_commit_callbacks)
    TrainerClientRelation.objects.filter(pk=relation.pk).update(ends_at_period_end=False)
    assert relation.pk not in due_relation_ids()
