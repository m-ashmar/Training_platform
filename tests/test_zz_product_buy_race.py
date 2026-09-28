"""A double tap on «اشترِ البرنامج»: two buys with the same key at once (BACKEND_BUGS
B-155). Needs committed data across threads, so it sorts after the rest (see
tests/test_zz_concurrency.py); a thread test flushes every table.

`ProductBuyView` documents that the second request waits for the first on the product's
row lock and then answers the same purchase — never `request_in_flight`. This guards
that: it passes on 554c604 too, and fails if the wait or the replay is ever lost.
"""
import threading
import time
from decimal import Decimal

import pytest

from tests.test_routine_products import _client, _published


@pytest.fixture(autouse=True)
def owners_rate(settings):
    settings.COACHING_COMMISSION_RATE = Decimal("0.20")


@pytest.mark.django_db(transaction=True)
def test_zz_two_buys_with_one_key_at_once_answer_one_purchase(make_user, api, monkeypatch):
    from django.db import connections

    from routine import products
    from wallet.models import Transaction

    _trainer, _template, product_id = _published(make_user, api, "race")
    buyer = _client(make_user, "race")

    second_started = threading.Event()
    original_write_copy = products._write_copy

    def slow_first_copy(product, who, purchase):
        # The first buy holds the product's lock here until the second is under way.
        if threading.current_thread().name == "first":
            second_started.wait(timeout=10)
            time.sleep(0.5)
        return original_write_copy(product, who, purchase)

    monkeypatch.setattr(products, "_write_copy", slow_first_copy)
    results, errors = {}, []

    def buy(name):
        try:
            if name == "second":
                second_started.set()
            purchase, created = products.buy(product_id, buyer, idempotency_key="tap")
            results[name] = (purchase.pk, created)
        except Exception as exc:  # noqa: BLE001 - reported below
            errors.append(repr(exc))
        finally:
            connections.close_all()

    first = threading.Thread(target=buy, args=("first",), name="first")
    first.start()
    time.sleep(0.2)
    second = threading.Thread(target=buy, args=("second",), name="second")
    second.start()
    first.join(timeout=30)
    second.join(timeout=30)

    assert not errors, errors
    assert results["first"][0] == results["second"][0], results
    assert sorted(created for _pk, created in results.values()) == [False, True]
    assert Transaction.objects.filter(metadata__purpose="routine_product_charge",
                                      metadata__purchase_id=results["first"][0]).count() == 1
