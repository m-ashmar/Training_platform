"""Wave 1 of the owner's fix run (2026-09-28): the wallet rows.

One test per row, each written against the journey that went wrong. Every test here
fails on 554c604.

| Test | Row |
|---|---|
| `test_s237_*` | the generic admin reversal took one leg of a program sale |
"""
from decimal import Decimal

import pytest

from tests.test_fx_subs import shamcash  # noqa: F401
from tests.test_hub_checkout import _product_session
from tests.test_routine_products import _buy, _fund, _published

pytestmark = pytest.mark.django_db

JSON = "application/json"
D = Decimal
REVERSAL = "/api/wallet/admin/reversal/"


def _code(resp):
    return resp.status_code, resp.json().get("code")


# ===========================================================================
# S-237: the generic admin reversal refuses every leg of a program sale
# ===========================================================================

def test_s237_no_leg_of_a_program_sale_is_reversed_on_its_own(
        make_user, api, settings, shamcash, django_capture_on_commit_callbacks):
    """Reversing a checkout's mint leg (None -> escrow) burned the shared per-currency
    escrow, which backs other clients' coaching holds, while the purchase stayed
    confirmed and the trainer's and the platform's shares stayed paid; the proper refund
    then refused. Every leg of a sale — the checkout's and the wallet sale's — is now
    refused with its own code, naming the purchase, and the refund still works."""
    from routine import products
    from routine.models import RoutinePurchase
    from subscription.services.payment_service import PaymentService
    from wallet.models import Transaction, Wallet
    from wallet.utils import get_escrow_wallet

    settings.COACHING_COMMISSION_RATE = D("0.20")
    operator = make_user("w1s237_op", user_type="admin")

    # Other clients' holds in escrow: the money the mint's reversal used to burn.
    escrow = get_escrow_wallet("SYP")
    Wallet.objects.filter(pk=escrow.pk).update(balance=escrow.balance + D("5000.00"))
    escrow.refresh_from_db()
    before = escrow.balance

    # A checkout's sale: None -> escrow 2,500, escrow -> trainer 2,000, -> platform 500.
    _trainer, buyer, _c, product_id, body = _product_session(make_user, api, "w1s237")
    with django_capture_on_commit_callbacks(execute=True):
        PaymentService.complete_payment(body["id"], {
            "amount": "2500.00", "currency": "SYP", "status": "completed",
            "transaction_id": "tx-w1s237"})
    checkout = RoutinePurchase.objects.get(buyer=buyer, product_id=product_id)

    # A wallet sale: buyer -> escrow, escrow -> trainer, escrow -> platform.
    _t2, _tpl, second_id = _published(make_user, api, "w1s237w")
    wallet_buyer = make_user("w1s237_wb")
    _fund(wallet_buyer, "10000.00")
    assert _buy(api, wallet_buyer, second_id, key="w1s237-buy").status_code == 200
    sale = RoutinePurchase.objects.get(buyer=wallet_buyer, product_id=second_id)

    for purchase in (checkout, sale):
        legs = list(Transaction.objects.filter(metadata__purchase_id=purchase.pk)
                    .order_by("id"))
        assert len(legs) == 3
        for n, leg in enumerate(legs):
            resp = api(operator).post(REVERSAL, {"reference_id": leg.reference_id,
                                                 "idempotency_key": f"w1s237-{purchase.pk}-{n}"},
                                      content_type=JSON)
            assert _code(resp) == (409, "routine_product_leg"), (leg.metadata, resp.content)
            assert resp.json()["purchase"] == {"id": purchase.pk, "status": "confirmed",
                                               "paid_with": purchase.paid_with}
            assert not Transaction.objects.filter(reverses=leg).exists()
        purchase.refresh_from_db()
        assert purchase.status == "confirmed"
    escrow.refresh_from_db()
    assert escrow.balance == before, "the shared escrow is untouched"

    # The door the refusal points to still works: every leg goes back together.
    products.refund(sale.pk, operator, "لم يطابق الوصف")
    sale.refresh_from_db()
    assert sale.status == "refunded"
    assert Transaction.objects.filter(metadata__purchase_id=sale.pk,
                                      tx_type="reversal").count() == 3
