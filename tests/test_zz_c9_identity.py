"""Cycle 9, identity: the races around a trainer relation's escrow hold.

Real threads and committed rows, so `django_db(transaction=True)` — see the note at the
top of `tests/test_zz_concurrency.py` for why these files sort last.

The windows are narrow (about 10 ms on localhost), so each test does not hope for an
overlap: it holds one side at the exact point the defect needs, with an event, and
releases the other side into it. Without the fix every one of these fails on every run.
"""
import threading
import time
from decimal import Decimal
from unittest import mock

import pytest

from tests.test_regression_gate import _trainer_and_client

WAIT = 15  # seconds; generous, a hang must fail rather than stall the suite


def _run(*targets):
    from django.db import connections

    results = {}

    def wrap(name, fn):
        def go():
            try:
                results[name] = fn()
            except Exception as exc:  # a lost race must be recorded, not crash the thread
                results[name] = exc
            finally:
                connections.close_all()
        return go

    threads = [threading.Thread(target=wrap(n, f)) for n, f in targets]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=WAIT * 4)
    return results


def _fund(user, amount):
    from wallet.models import Wallet
    wallet, _created = Wallet.objects.get_or_create(owner=user, defaults={"owner_type": user.user_type})
    wallet.balance = Decimal(amount)
    wallet.save()
    return wallet


def _balance(user):
    from wallet.models import Wallet
    return Wallet.objects.get(owner=user).balance


def _waiting_on_a_lock() -> bool:
    """Is anything in this database blocked on a row lock right now? Lets a test step
    forward the moment the other side is stuck, instead of guessing at a sleep."""
    from django.db import connection

    with connection.cursor() as cur:
        cur.execute("SELECT count(*) FROM pg_stat_activity WHERE "
                    "datname = current_database() AND wait_event_type = 'Lock'")
        return bool(cur.fetchone()[0])


def _open_holds_equal_escrow(escrow_before):
    """Escrow moved by exactly the money that open holds say it holds."""
    from django.db.models import Sum

    from users.models import TrainerClientRelation
    from wallet.models import Transaction
    from wallet.utils import get_escrow_wallet

    refs = TrainerClientRelation.objects.exclude(hold_reference="").values_list(
        "hold_reference", flat=True)
    held = Transaction.objects.filter(reference_id__in=list(refs)).aggregate(
        s=Sum("amount"))["s"] or Decimal("0")
    return get_escrow_wallet().balance - escrow_before, held


# ----------------------------------------------------------- approve vs reject
@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("side", ["trainer", "client"])
def test_a_reject_that_read_before_an_approve_committed_cannot_undo_it(make_user, side):
    """Both reject paths read the row without the lock approval takes and then wrote it
    unconditionally. The trainer path's full `save()` put back `hold_reference=''` over
    the hold approval had just committed; the client path left the hold on a rejected
    row. Measured: both calls 200, client charged 100.00, relation 'rejected', escrow
    100.00 over its open holds, and every unassign/cancel endpoint answered without a
    refund. Here the approval is held inside its transaction, after the row lock, while
    the reject is sent."""
    import users.trainer_relations as tr
    from users.models import TrainerClientRelation
    from wallet.utils import get_escrow_wallet

    trainer, client_user, t_api, c_api = _trainer_and_client(make_user, f"c9-ar-{side}")
    trainer.trainer_hourly_rate = Decimal("100.00")
    trainer.save()
    _fund(client_user, "1000.00")
    escrow_before = get_escrow_wallet().balance

    if side == "trainer":
        assert c_api.post("/api/auth/client/request-trainer/", {"trainer_id": trainer.id},
                          content_type="application/json").status_code == 200
        path, actor_api = "/api/auth/trainer/respond-to-request/", t_api
    else:
        assert t_api.post("/api/auth/trainer/assign-client/", {"client_id": client_user.id},
                          content_type="application/json").status_code == 200
        path, actor_api = "/api/auth/client/respond-to-invite/", c_api
    relation = TrainerClientRelation.objects.get(trainer=trainer, client=client_user)

    approving = threading.Event()
    real_take_hold = tr.take_hold

    def slow_take_hold(rel, actor):
        approving.set()          # the approval holds the row lock now
        time.sleep(0.6)          # and keeps its transaction open
        return real_take_hold(rel, actor)

    def approve():
        return actor_api.post(path, {"request_id": relation.id, "action": "approve"},
                              content_type="application/json").status_code

    def reject():
        assert approving.wait(WAIT)
        return actor_api.post(path, {"request_id": relation.id, "action": "reject"},
                              content_type="application/json").status_code

    with mock.patch.object(tr, "take_hold", slow_take_hold):
        results = _run(("approve", approve), ("reject", reject))

    relation.refresh_from_db()
    assert results["approve"] == 200, results
    assert results["reject"] in (404, 409), f"both answered success: {results}"
    assert relation.status == "approved" and relation.hold_reference, \
        (relation.status, relation.hold_reference)
    assert _balance(client_user) == Decimal("900.00")
    moved, held = _open_holds_equal_escrow(escrow_before)
    assert moved == held == Decimal("100.00"), (moved, held)


# ------------------------------------------------ user-row locks vs wallet locks
def _transfer_holding_wallets(client_user, trainer, holding, release):
    """A client transfer that has locked both wallets and written its ledger row, and
    waits at the door of COMMIT — where Postgres checks `Transaction.actor` and takes
    FOR KEY SHARE on the client's user row."""
    from django.db import transaction

    from wallet.models import Wallet, move_funds_atomic

    def go():
        with transaction.atomic():
            move_funds_atomic(Wallet.objects.get(owner=client_user),
                              Wallet.objects.get(owner=trainer), Decimal("10.00"),
                              actor_id=client_user.id, tx_type="transfer",
                              metadata={"trainer_id": trainer.id})
            holding.set()
            assert release.wait(WAIT)
            time.sleep(0.2)      # let the other side reach its wallet lock
        return "ok"
    return go


@pytest.mark.django_db(transaction=True)
def test_an_approval_and_the_same_clients_transfer_do_not_deadlock(make_user):
    """approve took FOR UPDATE on the client's user row, then the wallets; a transfer
    took the wallets, then — at COMMIT, via the deferred actor foreign key — FOR KEY
    SHARE on the same user row, which FOR UPDATE blocks. Measured: 20 deadlocks in 60
    contended pairs, transfers answering 500 with their idempotency key stuck, approvals
    answering 402 with Postgres internals in the body."""
    import users.trainer_relations as tr
    from users.models import TrainerClientRelation
    from users.views import approve_trainer_relation

    trainer, client_user, _t, c_api = _trainer_and_client(make_user, "c9-dl-approve")
    trainer.trainer_hourly_rate = Decimal("100.00")
    trainer.save()
    _fund(client_user, "1000.00")
    _fund(trainer, "0.00")
    assert c_api.post("/api/auth/client/request-trainer/", {"trainer_id": trainer.id},
                      content_type="application/json").status_code == 200
    relation = TrainerClientRelation.objects.get(trainer=trainer, client=client_user)

    wallets_held, user_row_held = threading.Event(), threading.Event()
    real_take_hold = tr.take_hold

    def take_hold_after_signalling(rel, actor):
        user_row_held.set()      # approval holds its relation and user-row locks
        return real_take_hold(rel, actor)

    def approve():
        assert wallets_held.wait(WAIT)
        refusal, _rel = approve_trainer_relation(
            TrainerClientRelation.objects.get(pk=relation.pk), trainer)
        return "ok" if refusal is None else (refusal.status_code, refusal.data)

    with mock.patch.object(tr, "take_hold", take_hold_after_signalling):
        results = _run(("transfer", _transfer_holding_wallets(
                            client_user, trainer, wallets_held, user_row_held)),
                       ("approve", approve))

    assert results == {"transfer": "ok", "approve": "ok"}, results
    relation.refresh_from_db()
    assert relation.status == "approved" and relation.hold_reference
    assert _balance(client_user) == Decimal("890.00")
    assert _balance(trainer) == Decimal("10.00")


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("move", ["settle", "release"])
def test_a_settlement_and_the_same_clients_transfer_do_not_deadlock(make_user, move):
    """`_move_hold` locked the relation with `select_related('trainer', 'client')` and no
    `of=`, which is FOR UPDATE on both user rows too, before the wallets. Routine
    assignment (settle) and unassign (release) therefore deadlocked with the same
    client's transfer or payout exactly like approval did: 6/6 at 0-4 ms offsets for
    settlement, and up to 5/5 for an unassign against that client's payment payout."""
    import wallet.utils as wallet_utils
    from users.models import TrainerClientRelation
    from users.trainer_relations import release_hold, settle_hold

    trainer, client_user, t_api, c_api = _trainer_and_client(make_user, f"c9-dl-{move}")
    trainer.trainer_hourly_rate = Decimal("100.00")
    trainer.save()
    _fund(client_user, "1000.00")
    _fund(trainer, "0.00")
    assert c_api.post("/api/auth/client/request-trainer/", {"trainer_id": trainer.id},
                      content_type="application/json").status_code == 200
    relation = TrainerClientRelation.objects.get(trainer=trainer, client=client_user)
    assert t_api.post("/api/auth/trainer/respond-to-request/",
                      {"request_id": relation.id, "action": "approve"},
                      content_type="application/json").status_code == 200

    wallets_held, relation_held = threading.Event(), threading.Event()
    real_escrow = wallet_utils.get_escrow_wallet

    def escrow_after_signalling():
        relation_held.set()      # _move_hold holds its relation lock
        return real_escrow()

    def move_the_hold():
        assert wallets_held.wait(WAIT)
        fresh = TrainerClientRelation.objects.get(pk=relation.pk)
        if move == "settle":
            settle_hold(fresh, trainer)
        else:
            release_hold(fresh, client_user)
        return "ok"

    with mock.patch.object(wallet_utils, "get_escrow_wallet", escrow_after_signalling):
        results = _run(("transfer", _transfer_holding_wallets(
                            client_user, trainer, wallets_held, relation_held)),
                       (move, move_the_hold))

    assert results == {"transfer": "ok", move: "ok"}, results
    relation.refresh_from_db()
    assert relation.hold_reference == ""
    if move == "settle":      # the 100.00 hold to the trainer, plus the 10.00 transfer
        assert (_balance(trainer), _balance(client_user)) == (Decimal("110.00"), Decimal("890.00"))
    else:                     # the 100.00 hold back to the client
        assert (_balance(trainer), _balance(client_user)) == (Decimal("10.00"), Decimal("990.00"))


@pytest.mark.django_db(transaction=True)
def test_a_transfer_that_must_wait_for_its_user_row_holds_no_wallet(make_user):
    """The other half of the one lock order: user rows before wallets on the ledger side
    too. `move_funds_atomic` locked the wallets and only met the actor's user row at
    COMMIT, through the deferred foreign-key check — so a transfer waiting on that row
    (here, a writer holding it FOR UPDATE the way plan saving does) sat on both wallets
    the whole time, and every other movement into those wallets queued behind it. It
    takes FOR KEY SHARE on the actor first now, and waits holding nothing."""
    from django.db import DatabaseError, transaction

    from users.models import CustomUser
    from wallet.models import Wallet, move_funds_atomic

    trainer = make_user("tr-c9-order", user_type="trainer")
    client_user = make_user("cl-c9-order")
    _fund(client_user, "100.00")
    trainer_wallet = _fund(trainer, "0.00")
    row_held, release = threading.Event(), threading.Event()

    def hold_user_row():
        with transaction.atomic():
            CustomUser.objects.select_for_update().get(pk=client_user.pk)
            row_held.set()
            assert release.wait(WAIT)
        return "ok"

    def transfer():
        assert row_held.wait(WAIT)
        move_funds_atomic(Wallet.objects.get(owner=client_user), trainer_wallet,
                          Decimal("10.00"), actor_id=client_user.id, tx_type="transfer")
        return "ok"

    probe = {}

    def probe_the_trainer_wallet():
        assert row_held.wait(WAIT)
        try:
            # Wait until the transfer is actually blocked on a lock, wherever it blocks.
            deadline = time.monotonic() + WAIT
            while time.monotonic() < deadline and not _waiting_on_a_lock():
                time.sleep(0.05)
            try:
                with transaction.atomic():
                    Wallet.objects.select_for_update(nowait=True).get(pk=trainer_wallet.pk)
                probe["wallet"] = "free"
            except DatabaseError:
                probe["wallet"] = "held by the waiting transfer"
        finally:
            release.set()
        return "ok"

    results = _run(("row", hold_user_row), ("transfer", transfer),
                   ("probe", probe_the_trainer_wallet))

    assert results == {"row": "ok", "transfer": "ok", "probe": "ok"}, results
    assert probe == {"wallet": "free"}, probe
    assert _balance(trainer) == Decimal("10.00")


@pytest.mark.django_db(transaction=True)
def test_an_erasure_that_returns_a_hold_does_not_deadlock_with_that_clients_payout(make_user):
    """Returning the hold before erasure deletes its row puts erasure in the lock order
    for the first time — and it entered it backwards. The profile anonymisation rewrites
    `email` and `username`, both unique, and Postgres locks a row FOR UPDATE when an
    UPDATE touches such a column; that happened AFTER `release_hold` had locked the
    client's wallet. A transfer or a gateway payout for the same client takes FOR KEY
    SHARE on that row first and the wallet second. Measured on the erasure endpoint:
    "deadlock detected ... while updating tuple in relation users_customuser", the
    erasure lost, and the payout that loses instead is never retried. Here the ledger
    writer holds the user row and writes once the erasure has either refunded or is
    waiting."""
    import users.trainer_relations as tr
    from django.db import connection, transaction

    from training_platform.privacy import erase_user_data
    from users.models import CustomUser, TrainerClientRelation
    from wallet.models import Wallet, move_funds_atomic

    trainer, client_user, t_api, c_api = _trainer_and_client(make_user, "c9-dl-erase")
    trainer.trainer_hourly_rate = Decimal("100.00")
    trainer.save()
    _fund(client_user, "1000.00")
    _fund(trainer, "0.00")
    assert c_api.post("/api/auth/client/request-trainer/", {"trainer_id": trainer.id},
                      content_type="application/json").status_code == 200
    relation = TrainerClientRelation.objects.get(trainer=trainer, client=client_user)
    assert t_api.post("/api/auth/trainer/respond-to-request/",
                      {"request_id": relation.id, "action": "approve"},
                      content_type="application/json").status_code == 200

    key_held, refunded = threading.Event(), threading.Event()
    real_release = tr.release_hold

    def release_and_signal(rel, actor):
        out = real_release(rel, actor)
        refunded.set()           # the erasure holds the escrow and client wallets now
        return out

    def ledger_writer():
        with transaction.atomic():
            # What `move_funds_atomic` now does before it touches a wallet.
            with connection.cursor() as cur:
                cur.execute("SELECT 1 FROM users_customuser WHERE id = %s FOR KEY SHARE",
                            [client_user.pk])
            key_held.set()
            deadline = time.monotonic() + WAIT
            while time.monotonic() < deadline:
                if refunded.is_set() or _waiting_on_a_lock():
                    break
                time.sleep(0.05)
            time.sleep(0.3)      # let the erasure reach its next statement
            move_funds_atomic(Wallet.objects.get(owner=client_user),
                              Wallet.objects.get(owner=trainer), Decimal("10.00"),
                              actor_id=client_user.id, tx_type="transfer")
        return "ok"

    def erase():
        assert key_held.wait(WAIT)
        erase_user_data(CustomUser.objects.get(pk=client_user.pk))
        return "ok"

    with mock.patch.object(tr, "release_hold", release_and_signal):
        results = _run(("writer", ledger_writer), ("erase", erase))

    assert results == {"writer": "ok", "erase": "ok"}, results
    assert not TrainerClientRelation.objects.filter(pk=relation.pk).exists()
    assert _balance(client_user) == Decimal("990.00")   # 1000 - 100 held + 100 back - 10
    assert _balance(trainer) == Decimal("10.00")
