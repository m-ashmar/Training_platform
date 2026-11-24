"""SIDE_BUGS S-179 (PAY-3, ruled 2026-11-24): a plan payment always reaches the
platform's revenue wallet, whatever wallet stands beside it.

`handle_payment_completed` looked the trainer's wallet up in the PAYMENT's currency (so
an untouched one took the client's currency, S-058) and, when the trainer's or the
client's wallet was in another currency, skipped EVERY leg — the platform's revenue
included — and marked the payout settled. Reachable only while `SUBSCRIPTION_TRAINER_SHARE`
is above its 0 default, so these tests raise it themselves.

The ruling (B): the trainer's wallet is read without a currency, as coaching's `pay_out`
reads it; a trainer in the other currency is not paid — the platform keeps the share and
an audit entry says so (`subscription_trainer_share_unpaid`, with the amount and the
payment); nothing is converted. A client in another currency (which the checkout rules
out, S-180) gets nothing minted in the wrong unit: the money goes straight to revenue.
"""
from decimal import Decimal

import pytest

from tests.test_road10_s213_wallet_plan import _person, _plan, _subscription

pytestmark = pytest.mark.django_db

SHARE = Decimal("0.10")


@pytest.fixture(autouse=True)
def a_trainer_share(settings):
    """Raised in these tests only: the owner's rule keeps it at 0."""
    settings.SUBSCRIPTION_TRAINER_SHARE = str(SHARE)


def _coached(make_user, tag, *, client_currency="SYP", trainer_currency="SYP",
             trainer_funds="0.00"):
    from users.models import TrainerClientRelation

    client = _person(make_user, tag, currency=client_currency, funds="0.00")
    trainer = make_user(f"{tag}_coach", user_type="trainer")
    if trainer_currency:
        from wallet.models import Wallet, set_currency_for

        set_currency_for(trainer, trainer_currency, "trainer")
        Wallet.objects.filter(owner=trainer).update(balance=Decimal(trainer_funds))
    TrainerClientRelation.objects.create(trainer=trainer, client=client, status="approved")
    return client, trainer


def _paid(user, tag, capture, amount="10000.00"):
    from subscription.models import Payment
    from subscription.services.payment_service import PaymentService

    sub = _subscription(user, _plan(tag, price=amount))
    payment = Payment.objects.create(subscription=sub, amount=Decimal(amount), currency="SYP",
                                     status="pending", payment_method="manual")
    with capture(execute=True):
        PaymentService.complete_payment(payment.id, {
            "amount": amount, "currency": "SYP", "status": "completed",
            "event_id": f"{tag}-paid"})
    payment.refresh_from_db()
    return payment


def _to_platform(payment):
    from django.db.models import Sum

    from wallet.models import Transaction
    from wallet.utils import get_platform_revenue_wallet

    return (Transaction.objects.filter(
        destination_wallet=get_platform_revenue_wallet(payment.currency),
        metadata__original_payment_id=str(payment.id)).aggregate(total=Sum("amount"))
        ["total"] or Decimal("0.00"))


def _unpaid_shares(payment):
    from wallet.models import WalletAuditLog

    return [entry.payload for entry in WalletAuditLog.objects.filter(
        event_type="subscription_trainer_share_unpaid",
        payload__payment_id=str(payment.id))]


def _balance(user):
    from wallet.models import Wallet

    return Wallet.objects.get(owner=user).balance


def test_a_trainer_in_the_same_currency_is_paid_the_share(
        make_user, django_capture_on_commit_callbacks):
    client, trainer = _coached(make_user, "s179_same")
    payment = _paid(client, "s179_same", django_capture_on_commit_callbacks)
    assert _balance(trainer) == Decimal("1000.00")
    assert _to_platform(payment) == Decimal("9000.00")
    assert _balance(client) == Decimal("0.00")
    assert _unpaid_shares(payment) == []


def test_a_trainer_in_the_other_currency_leaves_the_platform_the_whole_payment(
        make_user, django_capture_on_commit_callbacks):
    client, trainer = _coached(make_user, "s179_other", trainer_currency="USD",
                               trainer_funds="5.00")
    payment = _paid(client, "s179_other", django_capture_on_commit_callbacks)
    assert _to_platform(payment) == Decimal("10000.00"), "the platform's leg was skipped"
    assert _balance(trainer) == Decimal("5.00"), "nothing converted, nothing paid"
    assert _balance(client) == Decimal("0.00")
    (entry,) = _unpaid_shares(payment)
    assert (entry["amount"], entry["currency"], entry["trainer_id"]) == \
        ("1000.00", "SYP", trainer.pk)
    assert payment.payout_settled_at is not None


def test_an_untouched_trainer_wallet_is_not_given_the_clients_currency(
        make_user, django_capture_on_commit_callbacks):
    """Read without a currency (S-058): the client's payment no longer chooses it. An
    untouched wallet whose owner never chose, in USD, used to be relabelled SYP by the
    payment's lookup and paid in it."""
    from wallet.models import Wallet

    client, trainer = _coached(make_user, "s179_untouched", trainer_currency=None)
    Wallet.objects.filter(owner=trainer).update(currency="USD", currency_chosen_at=None)
    payment = _paid(client, "s179_untouched", django_capture_on_commit_callbacks)
    assert Wallet.objects.get(owner=trainer).currency == "USD"
    assert _to_platform(payment) == Decimal("10000.00")
    assert len(_unpaid_shares(payment)) == 1


def test_a_client_wallet_in_another_currency_still_pays_the_platform(
        make_user, django_capture_on_commit_callbacks):
    from wallet.models import WalletAuditLog

    client, _trainer = _coached(make_user, "s179_client", client_currency="USD")
    payment = _paid(client, "s179_client", django_capture_on_commit_callbacks)
    assert _to_platform(payment) == Decimal("10000.00")
    assert _balance(client) == Decimal("0.00"), "nothing minted into it in the wrong unit"
    assert WalletAuditLog.objects.filter(
        event_type="plan_payment_client_wallet_mismatch",
        payload__payment_id=str(payment.id)).count() == 1
    assert payment.payout_settled_at is not None


@pytest.mark.parametrize("client_currency, trainer_currency", [
    ("SYP", "SYP"), ("SYP", "USD"), ("USD", "SYP"), ("USD", "USD")])
def test_no_payout_is_settled_without_the_platforms_leg(
        make_user, django_capture_on_commit_callbacks, client_currency, trainer_currency):
    tag = f"s179_{client_currency}_{trainer_currency}".lower()
    client, _trainer = _coached(make_user, tag, client_currency=client_currency,
                                trainer_currency=trainer_currency)
    payment = _paid(client, tag, django_capture_on_commit_callbacks)
    assert payment.payout_settled_at is not None
    assert _to_platform(payment) > 0, "settled with no platform leg"
