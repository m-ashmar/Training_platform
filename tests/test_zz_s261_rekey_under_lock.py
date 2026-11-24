"""SIDE_BUGS S-261 (PAY-3): `manage.py rekey_address_digests` never gives a newly saved
address its predecessor's digest.

The command read a saved payer address, computed its digest under the current key, and
wrote it back by primary key alone. A customer replacing their address in between
(`payer_address.save`, which locks the row) committed the new address with its own
digest, and the command's write then put the OLD address's digest on it: that
customer's transfers stopped matching their saved address. Each row is now re-read under
its lock and its digest computed from the address it holds then.

In a `test_zz_` file: two connections are the race (`transaction=True` truncates every
table when it ends; see tests/test_zz_concurrency.py). Nothing seeded by a migration is
relied on.
"""
import threading
import time

import pytest

OLD, NEW = "shamoldaddr001", "shamnewaddr002"


def _saved_under_an_old_key(make_user, tag):
    from subscription import payer_address
    from subscription.models import PayerAddress

    user = make_user(f"s261_{tag}")
    payer_address.save(user, OLD, OLD)
    # As if written before a SECRET_KEY rotation: a digest the current key does not give.
    PayerAddress.objects.filter(user=user).update(address_digest="0" * 64)
    return user


@pytest.mark.django_db(transaction=True)
def test_a_rotation_rewrites_a_stale_digest(make_user):
    from django.core.management import call_command

    from subscription import payer_address
    from subscription.models import PayerAddress

    user = _saved_under_an_old_key(make_user, "rotate")
    call_command("rekey_address_digests")
    row = PayerAddress.objects.get(user=user)
    assert row.address_digest == payer_address.address_digest(OLD)


@pytest.mark.django_db(transaction=True)
def test_an_address_replaced_during_the_run_keeps_its_own_digest(make_user):
    from django.core.management import call_command
    from django.db import connections, transaction

    from subscription import payer_address
    from subscription.models import PayerAddress

    user = _saved_under_an_old_key(make_user, "race")
    started, outcome = threading.Event(), {}

    def replace():
        try:
            with transaction.atomic():
                payer_address.save(user, NEW, NEW)   # holds the row until this commits
                started.set()
                time.sleep(1.5)
        except Exception as exc:                     # recorded for the assertion
            outcome["replace"] = exc
        finally:
            connections.close_all()

    holder = threading.Thread(target=replace)
    holder.start()
    assert started.wait(timeout=30)
    try:
        call_command("rekey_address_digests")
    finally:
        holder.join(timeout=60)
    assert "replace" not in outcome, outcome
    row = PayerAddress.objects.get(user=user)
    assert row.address == NEW
    assert row.address_digest == payer_address.address_digest(NEW), \
        "the new address carries the old one's digest"
