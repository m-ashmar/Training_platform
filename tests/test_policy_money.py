"""The four kinds of money, and where an entitlement's money came from.

Every payment and every ledger row says which kind of money it is, because the store
rules attach to the kind: a plan is a digital good, a trainer's fee is a person-to-person
service, a top-up is money moving between people. Nothing may be left to be inferred.
"""
import ast
import pathlib
from decimal import Decimal

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
SKIPPED_DIRS = {".venv", "_excluded", "tests", "migrations", "mobile", "flutter", "node_modules",
                "staticfiles", "static", "media", ".claude", ".git"}


def test_every_call_that_moves_wallet_money_names_its_kind():
    """`move_funds_atomic` falls back to WALLET_MOVE so a bare movement stays honest, but
    production code is not allowed to lean on that: a checkout for a routine product
    that forgot to say so would be filed as a wallet transfer, on the wrong side of the
    store rules."""
    unnamed = []
    for path in ROOT.rglob("*.py"):
        if SKIPPED_DIRS & set(path.relative_to(ROOT).parts):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
            if name == "move_funds_atomic" and not any(k.arg == "money_type" for k in node.keywords):
                unnamed.append(f"{path.relative_to(ROOT)}:{node.lineno}")
    assert unnamed == []


def _plan(tag):
    from django.utils import translation

    from subscription.models import SubscriptionPlan

    with translation.override("en"):
        return SubscriptionPlan.objects.create(
            name=f"money {tag}", plan_type="premium", description="money",
            price=Decimal("5000.00"), currency="SYP", duration_days=30, is_active=True)


def _pending_payment(user, plan):
    from subscription.models import Payment, Subscription

    sub = Subscription.objects.create(user=user, plan=plan, status="pending")
    return sub, Payment.objects.create(subscription=sub, amount=plan.price, currency="SYP",
                                       status="pending", payment_method="shamcash",
                                       gateway_transaction_reference=f"ref-{user.username}")


@pytest.mark.django_db
def test_a_plan_payment_and_every_leg_it_writes_are_platform_sub(
        make_user, django_capture_on_commit_callbacks):
    from subscription.services.payment_service import PaymentService
    from users.models import TrainerClientRelation
    from wallet.models import Transaction

    trainer = make_user("money_sub_t", user_type="trainer")
    client_user = make_user("money_sub_c", user_type="client")
    TrainerClientRelation.objects.create(trainer=trainer, client=client_user, status="approved")
    sub, payment = _pending_payment(client_user, _plan("sub"))
    assert payment.money_type == "PLATFORM_SUB" and sub.source == ""

    with django_capture_on_commit_callbacks(execute=True):
        PaymentService.complete_payment(payment.id, {
            "amount": "5000.00", "currency": "SYP", "status": "completed",
            "transaction_id": "money-sub-tx", "event_id": "money-sub-ev"})

    legs = Transaction.objects.filter(metadata__original_payment_id=str(payment.id))
    assert sorted(legs.values_list("tx_type", flat=True)) == ["topup", "transfer"]
    assert set(legs.values_list("money_type", flat=True)) == {"PLATFORM_SUB"}

    sub.refresh_from_db()
    assert sub.is_active and sub.source == "shamcash"

    # A refund takes the same kind of money back.
    PaymentService.refund_payment(payment.id, reason="test")
    reversals = Transaction.objects.filter(tx_type="reversal",
                                           metadata__original_payment_id=str(payment.id))
    assert reversals.count() == 2
    assert set(reversals.values_list("money_type", flat=True)) == {"PLATFORM_SUB"}


@pytest.mark.django_db
def test_a_coaching_hold_and_its_settlement_are_coaching(make_user):
    from users.models import TrainerClientRelation
    from users.trainer_relations import settle_hold, take_hold
    from wallet.models import Transaction, move_funds_atomic, wallet_for

    trainer = make_user("money_coach_t", user_type="trainer")
    client_user = make_user("money_coach_c", user_type="client")
    funded = move_funds_atomic(None, wallet_for(client_user, "client"), Decimal("500.00"),
                               tx_type="topup")
    assert funded.money_type == "WALLET_MOVE"          # a bare movement is a wallet move

    relation = TrainerClientRelation.objects.create(
        trainer=trainer, client=client_user, status="approved", agreed_rate=Decimal("120.00"))
    take_hold(relation, client_user)
    relation.refresh_from_db()
    hold = Transaction.objects.get(reference_id=relation.hold_reference)
    assert hold.money_type == "COACHING_1TO1"

    settled = settle_hold(relation, trainer)
    assert settled is not None and settled.money_type == "COACHING_1TO1"


@pytest.mark.django_db
def test_a_reversal_is_the_kind_of_money_it_reverses(make_user):
    from wallet.models import move_funds_atomic, wallet_for

    a = wallet_for(make_user("money_rev_a", user_type="client"), "client")
    b = wallet_for(make_user("money_rev_b", user_type="trainer"), "trainer")
    move_funds_atomic(None, a, Decimal("200.00"), tx_type="topup")
    original = move_funds_atomic(a, b, Decimal("80.00"), money_type="COACHING_1TO1")
    back = move_funds_atomic(b, a, Decimal("80.00"), tx_type="reversal", reverses=original)
    assert back.money_type == "COACHING_1TO1"


@pytest.mark.django_db
def test_an_unknown_kind_of_money_is_refused_by_the_code_and_by_the_database(make_user):
    from django.db import IntegrityError, transaction

    from wallet.models import Transaction, move_funds_atomic, wallet_for

    wallet = wallet_for(make_user("money_unknown", user_type="client"), "client")
    with pytest.raises(ValueError):
        move_funds_atomic(None, wallet, Decimal("10.00"), tx_type="topup", money_type="GIFT")
    with pytest.raises(IntegrityError), transaction.atomic():
        Transaction.objects.create(tx_type="topup", destination_wallet=wallet,
                                   amount=Decimal("10.00"), currency="SYP", money_type="GIFT")


@pytest.mark.django_db
def test_a_trial_says_it_is_a_trial_and_access_never_asks(make_user):
    from subscription.utils import create_trial_subscription

    user = make_user("money_trial", user_type="client")
    sub = create_trial_subscription(user, _plan("trial").id)
    assert sub.source == "trial" and sub.is_active

    # Where the money came from is recorded, never consulted: any source, same access.
    for source in ("operator_grant", "apple_iap", "google_play", ""):
        sub.source = source
        assert sub.is_active


@pytest.mark.django_db
def test_the_api_says_what_kind_of_money_and_where_it_came_from(make_user, api):
    user = make_user("money_api", user_type="client")
    sub, payment = _pending_payment(user, _plan("api"))
    c = api(user)
    row = c.get(f"/api/subscription/v1/payments/{payment.id}/").json()
    assert row["money_type"] == "PLATFORM_SUB"
    mine = c.get(f"/api/subscription/v1/subscriptions/{sub.id}/").json()
    assert mine["source"] == ""
    # And a client cannot write either.
    c.patch(f"/api/subscription/v1/subscriptions/{sub.id}/", data='{"source": "apple_iap"}',
            content_type="application/json")
    sub.refresh_from_db()
    assert sub.source == ""
