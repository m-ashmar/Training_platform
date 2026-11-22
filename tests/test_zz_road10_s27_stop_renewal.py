"""Road to 10 S2.7 under real concurrency (committed rows, real threads): the client's
«do not renew» racing the renewal run, and two runs racing each other on a coaching
that stops.

In a `test_zz_` file because `django_db(transaction=True)` truncates every table when it
finishes; see the note at the top of tests/test_zz_concurrency.py.
"""
import threading
from datetime import timedelta
from decimal import Decimal
from unittest import mock

import pytest
from django.utils import timezone


def _paid_month_just_ended(make_user, settings, tag, *, stop):
    from users.models import CoachingPeriod, TrainerClientRelation
    from users.trainer_relations import settle_hold, take_hold
    from wallet.models import Wallet

    settings.COACHING_COMMISSION_RATE = Decimal("0.20")
    trainer = make_user(f"s27z_t_{tag}", user_type="trainer",
                        trainer_hourly_rate=Decimal("100.00"))
    client = make_user(f"s27z_c_{tag}")
    wallet, _created = Wallet.objects.get_or_create(owner=client, defaults={"owner_type": "client"})
    wallet.balance = Decimal("1000.00")
    wallet.save()
    relation = TrainerClientRelation.objects.create(
        trainer=trainer, client=client, status="approved", initiated_by="client",
        agreed_rate=Decimal("100.00"), agreed_currency=wallet.currency)
    take_hold(relation, client)
    relation.refresh_from_db()
    settle_hold(relation, trainer)
    month = CoachingPeriod.objects.get(relation=relation)
    CoachingPeriod.objects.filter(pk=month.pk).update(
        starts_at=timezone.now() - timedelta(days=30, minutes=1),
        ends_at=timezone.now() - timedelta(minutes=1))
    TrainerClientRelation.objects.filter(pk=relation.pk).update(client_stops_renewal=stop)
    return trainer, client, relation


def _race(*targets):
    from django.db import connections

    barrier = threading.Barrier(len(targets))
    outcomes = {}

    def wrap(name, fn):
        def run():
            try:
                barrier.wait(timeout=30)
                outcomes[name] = fn()
            except Exception as exc:          # recorded, so the assertion shows it
                outcomes[name] = repr(exc)
            finally:
                connections.close_all()
        return run

    with mock.patch("notifications.domain.dispatcher.emit_event"):
        threads = [threading.Thread(target=wrap(name, fn)) for name, fn in targets]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=120)
    return outcomes


@pytest.mark.django_db(transaction=True)
def test_two_runs_on_a_stopping_coaching_end_it_once_and_move_nothing(make_user, settings):
    from users.coaching import renew
    from users.models import CoachingEnding, CoachingPeriod, TrainerClientRelation
    from wallet.models import Transaction, Wallet

    trainer, client, relation = _paid_month_just_ended(make_user, settings, "runs", stop=True)
    ledger = Transaction.objects.count()
    outcomes = _race(("a", lambda: renew(relation.pk)), ("b", lambda: renew(relation.pk)))
    assert sorted(outcomes.values()) == ["ended", "gone"], outcomes
    assert Transaction.objects.count() == ledger, "no charge, no hold, no refund"
    assert Wallet.objects.get(owner=client).balance == Decimal("900.00")
    assert Wallet.objects.get(owner=trainer).balance == Decimal("80.00")
    assert not TrainerClientRelation.objects.filter(pk=relation.pk).exists()
    assert CoachingPeriod.objects.filter(client=client).count() == 1
    assert CoachingEnding.objects.filter(client=client).count() == 1


@pytest.mark.django_db(transaction=True)
def test_switching_back_while_the_run_ends_it_is_one_or_the_other(make_user, settings):
    """The client taps «renew» again just as the hourly run reaches the month's end. The
    relation row lock serialises them: either the run ended the coaching first (nothing
    charged, the switch answers `no_coaching`) or the switch landed first (the month is
    charged exactly once). Never a charge on an ended coaching, never two."""
    from users.coaching import renew, set_renewal
    from users.models import CoachingPeriod, TrainerClientRelation
    from wallet.models import Wallet

    trainer, client, relation = _paid_month_just_ended(make_user, settings, "flip", stop=True)

    def switch_back():
        _relation, code = set_renewal(client, renew=True)
        return code or "switched"

    outcomes = _race(("run", lambda: renew(relation.pk)), ("switch", switch_back))
    balance = Wallet.objects.get(owner=client).balance
    months = CoachingPeriod.objects.filter(client=client).count()
    if outcomes["run"] == "ended":
        assert outcomes["switch"] == "no_coaching", outcomes
        assert (balance, months) == (Decimal("900.00"), 1)
        assert not TrainerClientRelation.objects.filter(pk=relation.pk).exists()
    else:
        assert outcomes == {"run": "renewed", "switch": "switched"}, outcomes
        assert (balance, months) == (Decimal("800.00"), 2)
        assert TrainerClientRelation.objects.get(pk=relation.pk).client_stops_renewal is False
    # A retried run afterwards changes nothing.
    with mock.patch("notifications.domain.dispatcher.emit_event"):
        assert renew(relation.pk) in ("gone", "not_due")
    assert Wallet.objects.get(owner=client).balance == balance
