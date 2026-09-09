"""Concurrency, in its own file because of what it costs.

These tests need `django_db(transaction=True)`: real threads only see each other's work
once it is COMMITTED, and pytest-django's ordinary semantics wrap each test in one
transaction and roll it back, so two threads would each look at an empty database.

The price is that Django TRUNCATES every table when such a test finishes — the food
catalogue and the rows the migrations seeded included. No fixture can reliably put those
back: a repair written from inside the next test's transaction is rolled back with that
test, and Django's own `serialized_rollback` restores a snapshot taken when the catalogue
was already committed, so it collides with the rows that are still there.

So these live in a file that sorts AFTER `test_regression_gate.py`. `pytest tests/` runs
them; it just runs them last, and the truncation lands on nothing. Anything added here
has to be able to survive being the last thing that happens.
"""
import threading
from decimal import Decimal

import pytest

from tests.test_regression_gate import _api_client_for, _chooser, _trainer_and_client


@pytest.mark.django_db(transaction=True)
def test_two_generations_at_once_leave_one_plan_and_one_clear_answer(seeded_catalogue):
    """A double-tap on Generate is the ordinary case, and it must leave ONE plan.

    "Generate again" replaces the plan it overlaps — that is what `save_plan` is written
    to do, and it takes `select_for_update` on the client's row so the supersede and the
    insert cannot interleave. Both callers therefore succeed and the second plan is the
    live one; what must never happen is two active plans over one window, because
    `Meal`'s uniqueness is scoped to the PLAN, so both would write a breakfast for the
    same day and the client would see it twice.

    The refusal this test used to expect came from a cross-row check in `DietPlan.clean()`
    that fired only when the timing was unlucky — the same request refused or accepted
    depending on microseconds. The rule is enforced by the lock now, so the outcome is the
    same every time.
    """
    import threading

    from django.db import connections

    from diet.models import DietPlan
    from diet.services.plan_generation import generate_and_save

    user, _chosen = _chooser("race-generate")
    results, barrier = [], threading.Barrier(2)

    def go(i):
        try:
            barrier.wait(timeout=30)
            results.append(("ok", generate_and_save(user, duration_days=2, seed_salt=f"r{i}")[1].id))
        except Exception as exc:
            results.append(("err", f"{type(exc).__name__}: {exc}"))
        finally:
            connections.close_all()

    threads = [threading.Thread(target=go, args=(i,)) for i in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=120)

    assert len(results) == 2, results
    # The invariant, whichever caller won.
    assert DietPlan.objects.filter(user=user, is_active=True).count() == 1, results
    # And both callers were answered. `kinds == ["err","ok"] or ["ok","ok"]` accepted
    # the case where the race did not happen at all, which is most of the value of a
    # concurrency test given away.
    assert sorted(k for k, _v in results) == ["ok", "ok"], results
    # Two plans exist; exactly one is live, and it is the one the winner returned.
    live = DietPlan.objects.get(user=user, is_active=True)
    assert live.id in [v for k, v in results if k == "ok"], (live.id, results)
    assert DietPlan.objects.filter(user=user).count() == 2, "both callers built a plan"


@pytest.mark.django_db(transaction=True)
def test_one_hold_pays_a_trainer_once_however_many_callers(seeded_catalogue, make_user):
    """Four concurrent settlements of one hold must move the money once. The escrow
    wallet is shared, so a second payout is drawn from other clients' deposits."""
    import threading
    from decimal import Decimal

    from django.db import connections

    from users.models import TrainerClientRelation
    from users.trainer_relations import settle_hold
    from wallet.models import Transaction, Wallet

    trainer, client_user, t_api, c_api = _trainer_and_client(make_user, "race-money")
    trainer.trainer_hourly_rate = Decimal("100.00")
    trainer.save()
    wallet, _created = Wallet.objects.get_or_create(
        owner=client_user, defaults={"owner_type": "client"})
    wallet.balance = Decimal("500.00")
    wallet.save()
    c_api.post("/api/auth/client/request-trainer/", {"trainer_id": trainer.id},
               content_type="application/json")
    relation = TrainerClientRelation.objects.get(trainer=trainer, client=client_user)
    t_api.post("/api/auth/trainer/respond-to-request/",
               {"request_id": relation.id, "action": "approve"}, content_type="application/json")
    relation.refresh_from_db()
    assert relation.hold_reference

    barrier = threading.Barrier(4)

    def settle(_i):
        try:
            barrier.wait(timeout=30)
            settle_hold(TrainerClientRelation.objects.get(pk=relation.pk), trainer)
        except Exception:
            pass
        finally:
            connections.close_all()

    threads = [threading.Thread(target=settle, args=(i,)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=120)

    assert Wallet.objects.get(owner=trainer).balance == Decimal("100.00")
    assert Transaction.objects.filter(
        metadata__purpose="trainer_assignment_settlement",
        metadata__trainer_id=trainer.id).count() == 1




@pytest.mark.django_db(transaction=True)
def test_one_transfer_is_reversed_once_however_many_administrators_try(make_user):
    """The "already reversed" guard read `metadata__original_reference` in one transaction
    and moved money in another, with no lock on the row it was reading. Two administrators
    hitting Reverse on the same reference both passed it: measured with real threads, one
    100.00 transfer came out of the trainer's wallet twice. An idempotency key does not
    help — it is scoped to `(created_by, key)`, so a second admin, or the same admin with
    a fresh key, is a different key.

    The refusal must also be the RIGHT refusal. Placed after the balance check, the second
    caller failed with "Insufficient balance" — an accident of the destination wallet
    being empty, which stops happening the moment that wallet holds other money.
    """
    from django.db import connections
    from wallet.models import Transaction, Wallet, move_funds_atomic

    client_user = make_user("rev-client")
    trainer = make_user("rev-trainer", user_type="trainer")
    cw, _ = Wallet.objects.get_or_create(owner=client_user, defaults={"owner_type": "client"})
    tw, _ = Wallet.objects.get_or_create(owner=trainer, defaults={"owner_type": "trainer"})
    cw.balance = Decimal("500.00"); cw.save()
    tw.balance = Decimal("0.00"); tw.save()
    tx = move_funds_atomic(cw, tw, Decimal("100.00"), actor_id=client_user.id, tx_type="transfer")

    barrier, results = threading.Barrier(4), []

    def go():
        try:
            barrier.wait(timeout=30)
            move_funds_atomic(tw, cw, tx.amount, actor_id=client_user.id,
                              tx_type="reversal", reverses=tx)
            results.append("moved")
        except Exception as exc:
            results.append(type(exc).__name__)
        finally:
            connections.close_all()

    threads = [threading.Thread(target=go) for _ in range(4)]
    for t in threads: t.start()
    for t in threads: t.join(timeout=60)

    cw.refresh_from_db(); tw.refresh_from_db()
    assert results.count("moved") == 1, results
    assert results.count("AlreadyReversed") == 3, results
    assert Transaction.objects.filter(reverses=tx).count() == 1
    assert cw.balance == Decimal("500.00") and tw.balance == Decimal("0.00")


