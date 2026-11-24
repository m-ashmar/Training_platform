"""The fix run's SOC-2, S-255 (2026-11-24): the transfer review queue's "Sender = saved
payer address" tick is set only for an address the customer PROVED (a transfer from it
once confirmed one of their own checkouts by its exact reference), and a column beside
it says "saved, not proven" for a self-declared one. The tick read
`TransferReview.sender_match_payment_ids`, which is written from any saved address, so an
address anyone can type ticked exactly like a proven one (`likely_payment` alone asks for
proof since S-241). The first test fails on the code before at the tick itself; the
"Sender address" column is new.
"""
import pytest

from tests.test_fx_subs import _plan, shamcash  # noqa: F401
from tests.test_payment_review import _account, _open_checkout, _sweep, _txn

pytestmark = pytest.mark.django_db

ADDRESS = "5ee0d00d1234abcd"


def _queued(api, make_user, monkeypatch, commit, tag, price):
    """A customer who saved ADDRESS, two open checkouts of the price (so the transfer is
    queued `several_candidates`, the match written down), and the review row."""
    from subscription import payer_address
    from subscription.models import TransferReview

    plan = _plan(tag, price)
    _c1, sub, mine = _open_checkout(api, make_user, f"{tag}_saver", plan)
    _open_checkout(api, make_user, f"{tag}_other", plan)
    payer_address.save(sub.user, ADDRESS, ADDRESS)
    _account(monkeypatch, [_txn(f"tx-{tag}", price, note="", sender=ADDRESS)])
    with commit(execute=True):
        assert _sweep()["queued"] == 1
    review = TransferReview.objects.get(transaction_id=f"tx-{tag}")
    assert review.sender_match_payment_ids == [str(mine.pk)]
    return sub.user, review


def _console():
    from admin_dashboard.admin import admin_site
    from subscription.models import TransferReview

    return admin_site._registry[TransferReview]


def test_an_unproven_saved_address_does_not_tick(make_user, api, shamcash, monkeypatch,
                                                 django_capture_on_commit_callbacks):
    _saver, review = _queued(api, make_user, monkeypatch,
                             django_capture_on_commit_callbacks, "s255a", "6161.00")
    console = _console()
    assert console.sender_is_saved_payer(review) is False
    assert console.sender_address_proof(review) == "saved, not proven"


def test_a_proven_one_ticks(make_user, api, shamcash, monkeypatch,
                            django_capture_on_commit_callbacks):
    from django.utils import timezone

    from subscription.models import PayerAddress

    saver, review = _queued(api, make_user, monkeypatch,
                            django_capture_on_commit_callbacks, "s255b", "6262.00")
    PayerAddress.objects.filter(user=saver).update(verified_at=timezone.now())
    console = _console()
    assert console.sender_is_saved_payer(review) is True
    assert console.sender_address_proof(review) == "proven"


def test_no_saved_address_is_a_dash(make_user, api, shamcash, monkeypatch,
                                    django_capture_on_commit_callbacks):
    from subscription.models import TransferReview

    _saver, review = _queued(api, make_user, monkeypatch,
                             django_capture_on_commit_callbacks, "s255c", "6363.00")
    TransferReview.objects.filter(pk=review.pk).update(sender_match_payment_ids=[])
    review.refresh_from_db()
    console = _console()
    assert console.sender_is_saved_payer(review) is False
    assert console.sender_address_proof(review) == "—"
