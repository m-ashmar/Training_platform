"""SIDE_BUGS rows of the payments area, wave 1 of the owner's fix run (2026-09-28).

* S-234 (b): one checkout the pollers or the sweep cannot process — any exception, not
  only the refusals they expected — no longer aborts the whole run. Rows are read
  oldest-first (the open poller) or newest-first (the late poller), so one such row
  stopped every checkout of every customer behind it from being confirmed or expired
  for the whole 72 h lookback. The key collision that made a real row raise is part (a)
  of the row, closed in the routine products area; here the row's undeliverable program
  is simulated at the one call that raised (`routine.products.grant_checkout`), because
  what is proved is that NO row, for any reason, stops the others.
* S-238: every row of the poller's held pass is touched once it has been asked about,
  whatever the answer, so a row re-held on every ask cannot pin the front of the pass.

Every test here fails on 554c604.
"""
from datetime import timedelta

import pytest
from django.db import IntegrityError
from django.utils import timezone

from tests.test_fx_subs import _person, _plan, shamcash  # noqa: F401
from tests.test_hub_checkout import _aged, _product_session, owners_rate  # noqa: F401
from tests.test_payment_review import _account, _open_checkout, _poll, _sweep, _txn
from tests.test_routine_products import _buy, _fund

pytestmark = pytest.mark.django_db


def _undeliverable(monkeypatch, payment_id):
    """The program of `payment_id`'s checkout cannot be written: its delivery raises the
    IntegrityError the purchase-key collision raised (SIDE_BUGS S-234 (a))."""
    import routine.products as products

    real = products.grant_checkout

    def grant(payment):
        if str(payment.pk) == str(payment_id):
            raise IntegrityError('duplicate key value violates unique constraint '
                                 '"routine_purchase_key_per_buyer"')
        return real(payment)

    monkeypatch.setattr(products, "grant_checkout", grant)


def _status(payment_id):
    from subscription.models import Payment

    return Payment.objects.get(pk=payment_id).status


# ============================================================ S-234 (b)
def test_s234_one_row_that_cannot_be_delivered_never_stops_the_open_poller(
        make_user, api, shamcash, monkeypatch, django_capture_on_commit_callbacks):
    _trainer, _buyer, _c, _product_id, body = _product_session(make_user, api, "s234o")
    _aged(body["id"], timedelta(minutes=5))          # the oldest: read first
    _c2, _sub, other = _open_checkout(api, make_user, "s234o_other",
                                      _plan("s234o", "13000.00"))
    _undeliverable(monkeypatch, body["id"])
    _account(monkeypatch, [
        _txn("tx-s234o-x", "2500.00", note=body["reference"]),
        _txn("tx-s234o-y", "13000.00", note=other.gateway_transaction_reference)])

    with django_capture_on_commit_callbacks(execute=True):
        result = _poll()

    assert _status(other.pk) == "completed", "the row behind the broken one was confirmed"
    assert _status(body["id"]) == "pending", "the broken row is left for the next run"
    assert (result["checked"], result["completed"]) == (2, 1)


def test_s234_one_row_that_cannot_be_delivered_never_stops_the_late_poller(
        make_user, api, shamcash, monkeypatch, django_capture_on_commit_callbacks):
    from subscription.services.payment_service import PaymentService
    from subscription.tasks import poll_expired_checkouts

    _c, _sub, other = _open_checkout(api, make_user, "s234l_other",
                                     _plan("s234l", "13000.00"), age=timedelta(hours=3))
    _trainer, _buyer, _c2, _product_id, body = _product_session(make_user, api, "s234l")
    _aged(body["id"], timedelta(hours=2))            # the newest: read first
    PaymentService.expire_payment(other.pk, "window closed")
    PaymentService.expire_payment(body["id"], "window closed")
    _undeliverable(monkeypatch, body["id"])
    _account(monkeypatch, [
        _txn("tx-s234l-x", "2500.00", note=body["reference"]),
        _txn("tx-s234l-y", "13000.00", note=other.gateway_transaction_reference)])

    with django_capture_on_commit_callbacks(execute=True):
        result = poll_expired_checkouts()

    assert _status(other.pk) == "completed"
    assert _status(body["id"]) == "expired"
    assert (result["checked"], result["completed"]) == (2, 1)


def test_s234_one_transfer_that_cannot_be_delivered_never_stops_the_sweep(
        make_user, api, shamcash, monkeypatch, django_capture_on_commit_callbacks):
    from subscription.models import TransferReview
    from subscription.services.payment_service import PaymentService

    _trainer, _buyer, _c, _product_id, body = _product_session(make_user, api, "s234s")
    _aged(body["id"], timedelta(hours=2))
    PaymentService.expire_payment(body["id"], "window closed")
    _c2, _sub, other = _open_checkout(api, make_user, "s234s_other",
                                      _plan("s234s", "13131.00"))
    _undeliverable(monkeypatch, body["id"])
    # The closed checkout's own exact transfer first (the sweep settles it), then a
    # transfer with no reference whose amount fits exactly one open checkout.
    _account(monkeypatch, [_txn("tx-s234s-x", "2500.00", note=body["reference"]),
                           _txn("tx-s234s-y", "13131.00", note="")])

    with django_capture_on_commit_callbacks(execute=True):
        result = _sweep()

    assert _status(other.pk) == "needs_review", "the transfer after the broken one held"
    assert TransferReview.objects.get(transaction_id="tx-s234s-y").payment_id == other.pk
    assert _status(body["id"]) == "expired"
    assert result["held"] == 1


# ============================================================ S-238
def _held(api, make_user, name, price):
    """An open plan checkout, a minute old (a near-miss transfer the test lists then
    holds it for review)."""
    _c, _sub, payment = _open_checkout(api, make_user, name, _plan(name, price))
    return payment


def test_s238_a_held_row_whose_near_miss_is_still_on_the_account_does_not_pin_the_pass(
        make_user, api, shamcash, monkeypatch, settings,
        django_capture_on_commit_callbacks):
    """Held batch of one. A is held for amount_differs and its near-miss transfer stays
    on the account; B is held too, then its exact transfer arrives. A was never touched
    by the needs_review branch, so it stayed first in the pass and B never completed."""
    from subscription.models import Payment

    settings.PAYMENT_POLL_HELD_BATCH = 1
    a = _held(api, make_user, "s238a_a", "10000.00")
    b = _held(api, make_user, "s238a_b", "20000.00")
    near = [_txn("tx-s238a-a", "9000.00", note=a.gateway_transaction_reference),
            _txn("tx-s238a-b", "19000.00", note=b.gateway_transaction_reference)]
    _account(monkeypatch, near)
    with django_capture_on_commit_callbacks(execute=True):
        _poll()
    assert (_status(a.pk), _status(b.pk)) == ("needs_review", "needs_review")

    long_ago = timezone.now() - timedelta(hours=1)
    Payment.objects.filter(pk=a.pk).update(updated_at=long_ago)
    Payment.objects.filter(pk=b.pk).update(updated_at=long_ago + timedelta(minutes=1))
    _account(monkeypatch, [*near, _txn("tx-s238a-b-exact", "20000.00",
                                       note=b.gateway_transaction_reference)])

    with django_capture_on_commit_callbacks(execute=True):
        _poll()                                        # asks about A, and touches it
    assert Payment.objects.get(pk=a.pk).updated_at > long_ago + timedelta(minutes=1)
    with django_capture_on_commit_callbacks(execute=True):
        _poll()                                        # asks about B
    assert _status(b.pk) == "completed"
    assert _status(a.pk) == "needs_review"


def test_s238_a_held_row_its_completion_re_holds_does_not_pin_the_pass(
        make_user, api, shamcash, monkeypatch, settings,
        django_capture_on_commit_callbacks):
    """A product checkout expired, its buyer bought the program from the wallet, and
    the late exact transfer held it (`already_owned`). Every ask re-runs the refusal and
    re-holds it without saving the row; it stayed first in the held pass."""
    from subscription.models import Payment
    from subscription.tasks import poll_expired_checkouts

    settings.PAYMENT_POLL_HELD_BATCH = 1
    _trainer, buyer, _c, product_id, body = _product_session(make_user, api, "s238o")
    _aged(body["id"], timedelta(hours=2))
    _account(monkeypatch, [])
    with django_capture_on_commit_callbacks(execute=True):
        _poll()
    assert _status(body["id"]) == "expired"
    _fund(buyer, "10000.00")
    assert _buy(api, buyer, product_id).status_code == 200
    owned = [_txn("tx-s238o", "2500.00", note=body["reference"])]
    _account(monkeypatch, owned)
    with django_capture_on_commit_callbacks(execute=True):
        poll_expired_checkouts()
    assert _status(body["id"]) == "needs_review"

    b = _held(api, make_user, "s238o_b", "20000.00")
    near_b = _txn("tx-s238o-b", "19000.00", note=b.gateway_transaction_reference)
    _account(monkeypatch, [*owned, near_b])
    with django_capture_on_commit_callbacks(execute=True):
        _poll()                         # B held in the open pass (A may be asked too)
    assert _status(b.pk) == "needs_review"

    long_ago = timezone.now() - timedelta(hours=1)
    Payment.objects.filter(pk=body["id"]).update(updated_at=long_ago)
    Payment.objects.filter(pk=b.pk).update(updated_at=long_ago + timedelta(minutes=1))
    _account(monkeypatch, [*owned, near_b, _txn("tx-s238o-b-exact", "20000.00",
                                                note=b.gateway_transaction_reference)])
    for _run in range(2):
        with django_capture_on_commit_callbacks(execute=True):
            _poll()
    assert _status(b.pk) == "completed"
    assert _status(body["id"]) == "needs_review", "an operator still decides the owned one"


# ============================================================ S-232
OLD_KEY = "w1-s232-the-key-before-the-rotation-" + "x" * 30
NEW_KEY = "w1-s232-the-key-after-the-rotation-" + "y" * 30
ADDRESS = "0a3f91c27be455d0"


def _rotate(settings):
    """The owner's rotation (DEPLOY_CHECKLIST section 4): the old key moves to
    `DJANGO_OLD_SECRET_KEY_1`, i.e. `SECRET_KEY_FALLBACKS`, and a new one is set."""
    settings.SECRET_KEY_FALLBACKS = [OLD_KEY]
    settings.SECRET_KEY = NEW_KEY


def test_s232_a_saved_payer_address_still_matches_after_a_key_rotation(
        make_user, api, shamcash, settings):
    from subscription import payer_address

    settings.SECRET_KEY, settings.SECRET_KEY_FALLBACKS = OLD_KEY, []
    _c, sub, payment = _open_checkout(api, make_user, "s232_payer",
                                      _plan("s232", "4321.00"))
    customer = sub.user
    payer_address.save(customer, ADDRESS, ADDRESS)
    assert payer_address.owners_of_sender({"sender": ADDRESS}) == {customer.pk}

    _rotate(settings)
    assert payer_address.owners_of_sender({"sender": "0A3F-91C2-7BE4-55D0"}) == \
        {customer.pk}, "the row saved under the old key is still this customer's"
    assert payer_address.sender_matches({"sender": ADDRESS}, [payment.pk]) == \
        [str(payment.pk)], "the queue's evidence still names the customer's checkout"

    # A stranger who saves the same address after the rotation does not become its
    # only owner: an address saved by two accounts points at nobody, as before.
    stranger = _person(make_user, "s232_stranger")
    payer_address.save(stranger, ADDRESS, ADDRESS)
    assert payer_address.owners_of_sender({"sender": ADDRESS}) == {customer.pk, stranger.pk}


def test_s232_a_payout_address_saved_before_a_rotation_is_not_saved_twice(
        make_user, settings):
    from payouts import services
    from payouts.models import PayoutMethod

    settings.SECRET_KEY, settings.SECRET_KEY_FALLBACKS = OLD_KEY, []
    trainer = make_user("s232_trainer", user_type="trainer", trainer_is_verified=True)
    first = services.add_method(trainer, kind="shamcash", address=ADDRESS,
                                address_confirm=ADDRESS, holder_name="Rami")
    _rotate(settings)
    with pytest.raises(services.Refused) as refused:
        services.add_method(trainer, kind="shamcash", address="0a3f 91c2 7be4 55d0",
                            address_confirm=ADDRESS, holder_name="Rami")
    assert refused.value.code == "payout_method_exists"
    assert list(PayoutMethod.objects.filter(trainer=trainer).values_list("pk", flat=True)) \
        == [first.pk]


def test_s232_a_withdrawal_confirmed_after_a_rotation_verifies_its_method(
        make_user, api, settings, django_capture_on_commit_callbacks):
    from payouts import services
    from payouts.models import PayoutMethod
    from tests.test_trainer_withdrawals import _fund as _fund_wallet
    from tests.test_trainer_withdrawals import _operator, _request, _shamcash, _trainer

    settings.SECRET_KEY, settings.SECRET_KEY_FALLBACKS = OLD_KEY, []
    trainer = _trainer(make_user)
    _fund_wallet(trainer, "100000.00", "trainer")
    method = _shamcash(api, trainer)
    made = _request(api, trainer, "40000").json()
    operator = _operator(make_user)
    services.approve(made["id"], operator)
    services.mark_sent(made["id"], operator, "SC-TX-S232")

    _rotate(settings)
    with django_capture_on_commit_callbacks(execute=True):
        services.confirm(made["id"], operator)
    assert PayoutMethod.objects.get(pk=method["id"]).verified_at is not None


def test_s232_rekey_rewrites_old_digests_so_the_old_key_can_be_dropped(
        make_user, settings):
    import io

    from django.core.management import call_command

    from payouts import services
    from payouts.models import PayoutMethod
    from subscription import payer_address
    from subscription.models import PayerAddress

    settings.SECRET_KEY, settings.SECRET_KEY_FALLBACKS = OLD_KEY, []
    customer = _person(make_user, "s232_rekey")
    payer_address.save(customer, ADDRESS, ADDRESS)
    trainer = make_user("s232_rekey_t", user_type="trainer", trainer_is_verified=True)
    method = services.add_method(trainer, kind="shamcash", address=ADDRESS,
                                 address_confirm=ADDRESS, holder_name="Rami")
    old_digest = PayerAddress.objects.get(user=customer).address_digest

    _rotate(settings)
    dry = io.StringIO()
    call_command("rekey_address_digests", "--dry-run", stdout=dry)
    assert "would rewrite 1" in dry.getvalue()
    assert PayerAddress.objects.get(user=customer).address_digest == old_digest, \
        "a dry run writes nothing"
    call_command("rekey_address_digests", stdout=io.StringIO())

    settings.SECRET_KEY_FALLBACKS = []                  # the old key is dropped
    assert payer_address.owners_of_sender({"sender": ADDRESS}) == {customer.pk}
    assert PayoutMethod.objects.get(pk=method.pk).address_digest == \
        services.address_digest(ADDRESS)
    with pytest.raises(services.Refused):
        services.add_method(trainer, kind="shamcash", address=ADDRESS,
                            address_confirm=ADDRESS, holder_name="Rami")


# ============================================================ S-241
VICTIM_ADDRESS = "5ee0d00d1234abcd"


def _proven(user):
    from subscription.models import PayerAddress

    return PayerAddress.objects.get(user=user).verified_at


def test_s241_an_unproven_saved_address_cannot_pull_a_strangers_transfer_onto_a_checkout(
        make_user, api, shamcash, monkeypatch, django_capture_on_commit_callbacks):
    """The review's attack: A saves the victim's sending address (nothing checks it) and
    holds an expired checkout of the same price. V's is the ONLY open checkout the
    amount fits. V's reference-less transfer from that address was held on A's checkout,
    A was told, and V's was left to expire."""
    from notifications.models import Notification
    from subscription import payer_address
    from subscription.models import TransferReview
    from subscription.services.payment_service import PaymentService

    plan = _plan("s241a", "7777.00")
    _ca, sub_a, attacker_checkout = _open_checkout(api, make_user, "s241_attacker", plan,
                                                   age=timedelta(hours=2))
    PaymentService.expire_payment(attacker_checkout.pk, "window closed")
    attacker = sub_a.user
    payer_address.save(attacker, VICTIM_ADDRESS, VICTIM_ADDRESS)
    _cv, sub_v, victim_checkout = _open_checkout(api, make_user, "s241_victim", plan)
    _account(monkeypatch, [_txn("tx-s241a", "7777.00", note="", sender=VICTIM_ADDRESS)])

    with django_capture_on_commit_callbacks(execute=True):
        assert _sweep()["held"] == 1

    review = TransferReview.objects.get(transaction_id="tx-s241a")
    assert (review.reason, review.payment_id) == ("reference_missing", victim_checkout.pk)
    assert (_status(victim_checkout.pk), _status(attacker_checkout.pk)) == \
        ("needs_review", "expired")
    assert not Notification.objects.filter(recipient=attacker,
                                           event_type="payment_needs_review").exists()
    assert _proven(attacker) is None


def test_s241_an_unproven_match_is_evidence_and_never_picks_among_several(
        make_user, api, shamcash, monkeypatch, django_capture_on_commit_callbacks):
    from subscription import payer_address
    from subscription.models import TransferReview

    plan = _plan("s241b", "7878.00")
    _cv, _sv, victim_checkout = _open_checkout(api, make_user, "s241b_victim", plan)
    _ca, sub_a, attacker_checkout = _open_checkout(api, make_user, "s241b_attacker", plan)
    payer_address.save(sub_a.user, VICTIM_ADDRESS, VICTIM_ADDRESS)
    _account(monkeypatch, [_txn("tx-s241b", "7878.00", note="", sender=VICTIM_ADDRESS)])

    with django_capture_on_commit_callbacks(execute=True):
        assert _sweep()["queued"] == 1

    review = TransferReview.objects.get(transaction_id="tx-s241b")
    assert (review.reason, review.payment_id) == ("several_candidates", None)
    assert review.sender_match_payment_ids == [str(attacker_checkout.pk)], \
        "the match is still written down for the operator"
    assert (_status(victim_checkout.pk), _status(attacker_checkout.pk)) == \
        ("pending", "pending")


def test_s241_a_transfer_by_reference_proves_the_address_and_then_it_decides(
        make_user, api, shamcash, monkeypatch, django_capture_on_commit_callbacks):
    """V pays their checkout by its reference FROM the saved address: the address is
    proven. Later, with a stranger who saved the same address and a checkout of the same
    price, V's reference-less transfer is held on V's own checkout."""
    from subscription import payer_address
    from subscription.models import TransferReview
    from tests.test_hub_checkout import _session

    plan = _plan("s241c", "7979.00")
    cv, sub_v, first = _open_checkout(api, make_user, "s241c_victim", plan)
    victim = sub_v.user
    payer_address.save(victim, VICTIM_ADDRESS, VICTIM_ADDRESS)
    assert _proven(victim) is None
    _account(monkeypatch, [_txn("tx-s241c-1", "7979.00", note=first.gateway_transaction_reference,
                                sender="5EE0-D00D-1234-ABCD")])
    with django_capture_on_commit_callbacks(execute=True):
        assert _poll()["completed"] == 1
    assert _proven(victim) is not None, "a transfer from it paid V's own checkout"

    renewal = _session(cv, "PLATFORM_SUB", plan.id)
    assert (renewal.status_code, renewal.json()["kind"]) == (201, "renewal"), renewal.content
    _ca, sub_a, attacker_checkout = _open_checkout(api, make_user, "s241c_attacker", plan)
    payer_address.save(sub_a.user, VICTIM_ADDRESS, VICTIM_ADDRESS)
    _account(monkeypatch, [_txn("tx-s241c-2", "7979.00", note="", sender=VICTIM_ADDRESS)])
    with django_capture_on_commit_callbacks(execute=True):
        assert _sweep()["held"] == 1

    review = TransferReview.objects.get(transaction_id="tx-s241c-2")
    assert (review.reason, str(review.payment_id)) == ("reference_missing",
                                                       renewal.json()["id"])
    assert review.candidate_payment_ids == [str(attacker_checkout.pk)]
    assert _status(attacker_checkout.pk) == "pending"


def test_s241_an_operators_approval_proves_nothing_and_a_new_address_is_unproven(
        make_user, api, shamcash, monkeypatch, django_capture_on_commit_callbacks):
    from subscription import payer_address
    from subscription.models import TransferReview
    from subscription.services.payment_service import PaymentService
    from tests.test_routine_products import _operator

    plan = _plan("s241d", "8080.00")
    _c, sub, payment = _open_checkout(api, make_user, "s241d_payer", plan)
    payer_address.save(sub.user, VICTIM_ADDRESS, VICTIM_ADDRESS)
    # A near miss from the address (the reference, another amount), approved by hand.
    _account(monkeypatch, [_txn("tx-s241d", "8000.00", note=payment.gateway_transaction_reference,
                                sender=VICTIM_ADDRESS)])
    with django_capture_on_commit_callbacks(execute=True):
        _poll()
        review = TransferReview.objects.get(transaction_id="tx-s241d")
        PaymentService.approve_review(review.pk, _operator(make_user, "s241d"), "ok")
    assert _status(payment.pk) == "completed"
    assert _proven(sub.user) is None, "an operator's approval is not the customer's proof"

    from subscription.models import PayerAddress

    PayerAddress.objects.filter(user=sub.user).update(verified_at=timezone.now())
    payer_address.save(sub.user, VICTIM_ADDRESS, VICTIM_ADDRESS)
    assert _proven(sub.user) is not None, "saving the same address again keeps the proof"
    payer_address.save(sub.user, "1111222233334444", "1111222233334444")
    assert _proven(sub.user) is None, "another address is unproven"


def test_s241_only_the_checkouts_own_reference_proves_the_address(
        make_user, api, shamcash, django_capture_on_commit_callbacks):
    """The proof compared the transfer's reference with the checkout's AFTER the
    completion had copied the transfer's over it, so any reference at all proved the
    address. A completion whose transfer names ANOTHER reference proves nothing; one
    that names the checkout's own does (the control)."""
    from subscription import payer_address
    from subscription.services.payment_service import By, PaymentService

    plan = _plan("s241e", "8181.00")
    for name, own in (("s241e_other", False), ("s241e_own", True)):
        _c, sub, payment = _open_checkout(api, make_user, name, plan)
        payer_address.save(sub.user, VICTIM_ADDRESS, VICTIM_ADDRESS)
        reference = (payment.gateway_transaction_reference if own
                     else "SHAMCASH_1700000000000_0000000000ab")
        with django_capture_on_commit_callbacks(execute=True):
            done = PaymentService.complete_payment(payment.pk, {
                "amount": "8181.00", "currency": "SYP", "status": "completed",
                "transaction_id": f"tx-{name}", "event_id": f"tx-{name}",
                "reference": reference, "sender": VICTIM_ADDRESS}, by=By.POLLER)
        assert done.status == "completed"
        assert (_proven(sub.user) is not None) is own, name


# ============================================================ S-240
BUYER_ADDRESS = "b0b0cafe55556666"


def _two_buyers_of_one_program(make_user, api, tag):
    """Two buyers, each with an open 2,500 SYP session for the SAME program — the
    normal case for a product."""
    from tests.test_hub_checkout import _session

    _trainer, buyer_a, _c, product_id, session_a = _product_session(make_user, api, tag)
    buyer_b = _person(make_user, f"{tag}_b")
    resp = _session(api(buyer_b), "ROUTINE_PRODUCT", product_id)
    assert resp.status_code == 201, resp.content
    return buyer_a, session_a, buyer_b, resp.json()


def test_s240_a_proven_address_holds_the_buyers_product_checkout(
        make_user, api, shamcash, monkeypatch, django_capture_on_commit_callbacks):
    from subscription import payer_address
    from subscription.models import PayerAddress, TransferReview

    _a, session_a, buyer_b, session_b = _two_buyers_of_one_program(make_user, api, "s240a")
    payer_address.save(buyer_b, BUYER_ADDRESS, BUYER_ADDRESS)
    # Proven, as an earlier transfer from it by reference proves it (S-241).
    PayerAddress.objects.filter(user=buyer_b).update(verified_at=timezone.now())
    _account(monkeypatch, [_txn("tx-s240a", "2500.00", note="", sender=BUYER_ADDRESS)])

    with django_capture_on_commit_callbacks(execute=True):
        assert _sweep()["held"] == 1

    review = TransferReview.objects.get(transaction_id="tx-s240a")
    assert (review.reason, str(review.payment_id)) == ("reference_missing", session_b["id"])
    assert review.sender_match_payment_ids == [session_b["id"]]
    assert review.candidate_payment_ids == [session_a["id"]]
    assert (_status(session_b["id"]), _status(session_a["id"])) == ("needs_review", "pending")


def test_s240_the_match_on_a_product_checkout_is_written_down_for_the_operator(
        make_user, api, shamcash, monkeypatch, django_capture_on_commit_callbacks):
    """Unproven: no checkout is picked among the two, but the queue row names the
    buyer's session as the sender's (it said [] for every product checkout)."""
    from subscription import payer_address
    from subscription.models import TransferReview

    _a, _sa, buyer_b, session_b = _two_buyers_of_one_program(make_user, api, "s240b")
    payer_address.save(buyer_b, BUYER_ADDRESS, BUYER_ADDRESS)
    _account(monkeypatch, [_txn("tx-s240b", "2500.00", note="", sender=BUYER_ADDRESS)])

    with django_capture_on_commit_callbacks(execute=True):
        assert _sweep()["queued"] == 1

    review = TransferReview.objects.get(transaction_id="tx-s240b")
    assert (review.reason, review.payment_id) == ("several_candidates", None)
    assert review.sender_match_payment_ids == [session_b["id"]]


def test_s240_a_proven_address_finds_the_buyers_closed_product_checkout(
        make_user, api, shamcash, monkeypatch, django_capture_on_commit_callbacks):
    from subscription import payer_address
    from subscription.models import PayerAddress, TransferReview
    from subscription.services.payment_service import PaymentService

    _trainer, buyer, _c, _product_id, body = _product_session(make_user, api, "s240c")
    _aged(body["id"], timedelta(hours=2))
    PaymentService.expire_payment(body["id"], "window closed")
    payer_address.save(buyer, BUYER_ADDRESS, BUYER_ADDRESS)
    PayerAddress.objects.filter(user=buyer).update(verified_at=timezone.now())
    _account(monkeypatch, [_txn("tx-s240c", "2500.00", note="", sender=BUYER_ADDRESS)])

    with django_capture_on_commit_callbacks(execute=True):
        assert _sweep()["held"] == 1

    review = TransferReview.objects.get(transaction_id="tx-s240c")
    assert (review.reason, str(review.payment_id)) == ("reference_missing", body["id"])
    assert _status(body["id"]) == "needs_review"


# ============================================================ S-239
STATUS = "/api/subscription/v1/payments/{}/status/"
PAYMENTS = "/api/subscription/v1/payments/"


def _rows(resp):
    body = resp.json()
    return body.get("results", body) if isinstance(body, dict) else body


def test_s239_a_store_build_reads_no_payment_instruction_for_an_open_product_session(
        make_user, api, shamcash):
    """A buyer opens a product session on the web hub, then the iOS app polls the
    payment's status: it answered the amount and the SHAMCASH_ reference, which
    `checkout/sessions/{id}/` withholds from the same build."""
    from tests.test_hub_checkout import SESSIONS

    _trainer, _buyer, c, _product_id, body = _product_session(make_user, api, "s239p")
    for channel in ("ios", "play", ""):
        status = c.get(STATUS.format(body["id"]), HTTP_X_APP_CHANNEL=channel)
        assert status.status_code == 200, (channel, status.content)
        assert "amount" not in status.json() and "reference" not in status.json(), channel
        assert status.json()["status"] == "pending"
        session = c.get(f"{SESSIONS}{body['id']}/", HTTP_X_APP_CHANNEL=channel)
        if session.status_code == 200:
            assert "amount" not in session.json(), "the same rule as the session read"
        row = next(r for r in _rows(c.get(PAYMENTS, HTTP_X_APP_CHANNEL=channel))
                   if r["id"] == body["id"])
        assert "amount" not in row and "reference" not in row, channel
    web = c.get(STATUS.format(body["id"]), HTTP_X_APP_CHANNEL="web").json()
    assert (web["amount"], web["reference"]) == ("2500.00", body["reference"])


def test_s239_an_open_plan_checkout_on_a_store_build_and_the_record_once_paid(
        make_user, api, shamcash, django_capture_on_commit_callbacks):
    from subscription.services.payment_service import PaymentService

    c, _sub, payment = _open_checkout(api, make_user, "s239_plan", _plan("s239", "13000.00"))
    reference = payment.gateway_transaction_reference

    apk = c.get(STATUS.format(payment.pk), HTTP_X_APP_CHANNEL="apk").json()
    assert (apk["amount"], apk["reference"]) == ("13000.00", reference)
    for channel in ("ios", "play"):
        status = c.get(STATUS.format(payment.pk), HTTP_X_APP_CHANNEL=channel).json()
        assert "amount" not in status and "reference" not in status, channel
        row = next(r for r in _rows(c.get(PAYMENTS, HTTP_X_APP_CHANNEL=channel))
                   if r["id"] == str(payment.pk))
        assert "amount" not in row and "reference" not in row, channel
        detail = c.get(f"{PAYMENTS}{payment.pk}/", HTTP_X_APP_CHANNEL=channel).json()
        assert "amount" not in detail and "reference" not in detail, channel

    with django_capture_on_commit_callbacks(execute=True):
        PaymentService.complete_payment(payment.pk, {
            "amount": "13000.00", "currency": "SYP", "status": "completed",
            "transaction_id": "tx-s239", "reference": reference})
    for channel in ("ios", "play", "apk"):
        status = c.get(STATUS.format(payment.pk), HTTP_X_APP_CHANNEL=channel).json()
        assert (status["amount"], status["reference"]) == ("13000.00", reference), channel
        row = next(r for r in _rows(c.get(PAYMENTS, HTTP_X_APP_CHANNEL=channel))
                   if r["id"] == str(payment.pk))
        assert (row["amount"], row["reference"]) == ("13000.00", reference), channel
