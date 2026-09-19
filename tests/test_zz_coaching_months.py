"""Monthly coaching under real concurrency (committed rows, real threads).

In a `test_zz_` file because `django_db(transaction=True)` truncates every table when it
finishes; see the note at the top of tests/test_zz_concurrency.py.
"""
import threading
from datetime import timedelta
from decimal import Decimal
from unittest import mock

import pytest
from django.utils import timezone


@pytest.mark.django_db(transaction=True)
def test_two_workers_renewing_the_same_month_charge_it_once(make_user, settings):
    """Two beat workers (or a retried run racing the next one) reach the same due month.
    The relation row lock serialises them, and the second finds the month already
    charged; the unique start per pair would stop it even without the lock."""
    from django.db import connections

    from users.coaching import renew
    from users.models import CoachingPeriod, TrainerClientRelation
    from users.trainer_relations import settle_hold, take_hold
    from wallet.models import Wallet

    settings.COACHING_COMMISSION_RATE = Decimal("0.20")
    trainer = make_user("cmz_t", user_type="trainer", trainer_hourly_rate=Decimal("100.00"))
    client = make_user("cmz_c")
    wallet, _created = Wallet.objects.get_or_create(owner=client, defaults={"owner_type": "client"})
    wallet.balance = Decimal("1000.00")
    wallet.save()
    relation = TrainerClientRelation.objects.create(
        trainer=trainer, client=client, status="approved", initiated_by="client",
        agreed_rate=Decimal("100.00"))
    take_hold(relation, client)
    relation.refresh_from_db()
    settle_hold(relation, trainer)
    month = CoachingPeriod.objects.get(relation=relation)
    CoachingPeriod.objects.filter(pk=month.pk).update(
        starts_at=timezone.now() - timedelta(days=30, minutes=1),
        ends_at=timezone.now() - timedelta(minutes=1))

    barrier = threading.Barrier(2)
    outcomes = []

    def worker():
        try:
            barrier.wait(timeout=30)
            outcomes.append(renew(relation.pk))
        except Exception as exc:          # recorded, so the assertion shows it
            outcomes.append(repr(exc))
        finally:
            connections.close_all()

    with mock.patch("notifications.domain.dispatcher.emit_event"):
        threads = [threading.Thread(target=worker) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=120)

    assert sorted(outcomes) == ["not_due", "renewed"], outcomes
    assert CoachingPeriod.objects.filter(relation=relation).count() == 2
    assert Wallet.objects.get(owner=client).balance == Decimal("800.00")
    assert Wallet.objects.get(owner=trainer).balance == Decimal("160.00")
