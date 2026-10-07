"""Repairs to wave 1's wallet rows, from the adversarial review of merge/w1 (2026-10-07).

Every test here fails on 39ae624 (the merged wave-1 head) and passes after.

| Test | Finding |
|---|---|
| `test_r1_*` | B-163's transfer note was a new one-to-one text channel a block did not close |
| `test_r6_*` | the transfer's answer carried the trainer's whole wallet balance to the payer |
"""
from decimal import Decimal

import pytest

from tests.test_w1_wallet import NOTE, TRANSACTIONS, _code, _pair, _row, _send

pytestmark = pytest.mark.django_db

D = Decimal


def _block(blocker, blocked):
    from moderation.models import Block

    return Block.objects.create(blocker=blocker, blocked=blocked)


# ===========================================================================
# R-1: a block closes the transfer, and hides a note sent before it
# ===========================================================================

def test_r1_a_blocked_pair_cannot_transfer_in_either_direction(make_user, api, settings):
    """A trainer blocked a client, and the client could still put «I know where you
    train» in front of them for 0.01 a time: the transfer ran only the banned terms on
    the note. Now a block either way is `403 user_blocked` before anything moves or the
    key is reserved — the same answer whoever blocked, as a trainer request gets."""
    from wallet.models import CoachingTransfer, Wallet

    settings.COACHING_COMMISSION_RATE = D("0.20")
    client, trainer = _pair(make_user, "w1r1b")

    block = _block(trainer, client)
    refused = _send(api, client, trainer, "w1r1b-1", amount="1.00",
                    note="I know where you train")
    assert _code(refused) == (403, "user_blocked"), refused.content
    # Nothing moved, nothing was written, and the trainer's ledger is empty.
    assert Wallet.objects.get(owner=client).balance == D("8000.00")
    assert Wallet.objects.get(owner=trainer).balance == D("0.00")
    assert not CoachingTransfer.objects.filter(client=client).exists()
    assert _row(api, trainer) == []

    # The other direction answers the same, without a note too: the client's name
    # landing in the trainer's ledger is itself the contact.
    block.delete()
    other = _block(client, trainer)
    assert _code(_send(api, client, trainer, "w1r1b-2", amount="1.00")) == \
        (403, "user_blocked")
    assert not CoachingTransfer.objects.filter(client=client).exists()

    # The key was never reserved: once the block is lifted, the same key goes through.
    other.delete()
    sent = _send(api, client, trainer, "w1r1b-1", amount="1.00", note="جلسة السبت")
    assert sent.status_code == 200, sent.content


def test_r1_a_note_sent_before_a_block_is_hidden_while_it_stands(make_user, api, settings):
    """A note sent before the block stayed on the trainer's row, beside the client's
    name. It now reads null for both of them while either has blocked the other — the
    way an invitation's note is blanked — and shows again when the block is lifted."""
    settings.COACHING_COMMISSION_RATE = D("0.20")
    client, trainer = _pair(make_user, "w1r1n")
    ref = _send(api, client, trainer, "w1r1n-1", note=NOTE).json()["reference_id"]
    (theirs,) = _row(api, trainer)
    payout_ref = theirs["reference_id"]
    # Read once first, so a cached read (django-cachalot) is what the block must undo.
    assert theirs["note"] == NOTE
    assert _row(api, client, ref)["note"] == NOTE

    def notes():
        return (_row(api, trainer, payout_ref)["note"],
                api(trainer).get(f"{TRANSACTIONS}{payout_ref}/").json()["note"],
                _row(api, client, ref)["note"],
                api(client).get(f"{TRANSACTIONS}{ref}/").json()["note"])

    for blocker, blocked in ((trainer, client), (client, trainer)):
        block = _block(blocker, blocked)
        assert notes() == (None, None, None, None), (blocker.username, notes())
        block.delete()
        assert notes() == (NOTE, NOTE, NOTE, NOTE)

    # A block between other people hides nothing here.
    stranger = make_user("w1r1n_x")
    _block(trainer, stranger)
    _block(stranger, client)
    assert notes() == (NOTE, NOTE, NOTE, NOTE)


# ===========================================================================
# R-6: the answer names this payment's share, never the trainer's balance
# ===========================================================================

def test_r6_the_answer_names_this_payments_share_not_the_trainers_balance(
        make_user, api, settings):
    """`trainer_balance` was `str(trainer_wallet.balance)`: any client, coached by the
    trainer or not, learned the trainer's whole wallet for 0.01. The key stays (the
    contract is additive), and it is now what THIS payment gave the trainer."""
    from tests.test_routine_products import _fund
    from wallet.models import CoachingTransfer, Wallet

    settings.COACHING_COMMISSION_RATE = D("0.20")
    client, trainer = _pair(make_user, "w1r6")
    _fund(trainer, "7777.00", "trainer")

    sent = _send(api, client, trainer, "w1r6-1", amount="5000.00")
    assert sent.status_code == 200, sent.content
    record = CoachingTransfer.objects.get(charge_reference=sent.json()["reference_id"])
    assert sent.json()["trainer_balance"] == str(record.trainer_share) == "4000.00"
    assert Wallet.objects.get(owner=trainer).balance == D("11777.00")

    tiny = _send(api, client, trainer, "w1r6-2", amount="0.01")
    assert tiny.status_code == 200, tiny.content
    assert tiny.json()["trainer_balance"] == "0.01"
    assert "7777" not in tiny.content.decode() and "11777" not in tiny.content.decode()
    # The client's own balance is still theirs to see.
    assert tiny.json()["client_balance"] == "2999.99"
    # A replay answers what the first request answered, byte for byte.
    again = _send(api, client, trainer, "w1r6-2", amount="0.01")
    assert again.json() == tiny.json()
