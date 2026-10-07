"""BACKEND_BUGS B-167: a routine product bought through a web-hub checkout is the
buyer's payment too.

A product's checkout is a `Payment` with `buyer` and `routine_product` set and NO
subscription (`payment_pays_for_one_thing`). The payment reads filtered on
`subscription__user`, so for the buyer's own product payment `GET payments/{id}/` and
`GET payments/{id}/receipt/` answered 404 `not_found` and the history list left it out;
behind the filter the receipt read `payment.subscription.user` (a 500 once the filter
let the row through), and the completion of a product returned before anything queued
the receipt email, so none was ever sent.

On 554c604 the reads below answer 404, 404 and a list without the row, and no
`payment_receipt` email is owed.
"""
from decimal import Decimal

import pytest

from tests.test_fx_subs import _paid, shamcash  # noqa: F401
from tests.test_hub_checkout import WEB, _product_session, owners_rate  # noqa: F401

pytestmark = pytest.mark.django_db

PAYMENTS = "/api/subscription/v1/payments/"


def _reconciled(make_user, api, monkeypatch, capture, tag):
    """A product session the buyer paid and confirmed with "I paid"."""
    trainer, buyer, c, product_id, body = _product_session(make_user, api, tag)
    _paid(monkeypatch, body["reference"], "2500.00")
    with capture(execute=True):
        resp = c.post(f"{PAYMENTS}{body['id']}/reconcile/", **WEB)
    assert (resp.status_code, resp.json()["status"]) == (200, "completed"), resp.content
    return trainer, buyer, c, product_id, body


def test_b167_a_product_checkout_buyer_reads_its_payment_receipt_and_history(
        make_user, api, shamcash, monkeypatch, django_capture_on_commit_callbacks):
    from notifications.models import EmailDelivery
    from routine.product_models import RoutineProduct
    from subscription.models import Payment

    _trainer, buyer, c, product_id, body = _reconciled(
        make_user, api, monkeypatch, django_capture_on_commit_callbacks, "b167")
    payment = Payment.objects.get(pk=body["id"])
    assert (payment.subscription_id, payment.buyer_id) == (None, buyer.pk)
    title = RoutineProduct.objects.get(pk=product_id).title_in("en")

    # The payment itself, with its history (the contract's `events`).
    detail = c.get(f"{PAYMENTS}{body['id']}/")
    assert detail.status_code == 200, detail.content
    assert [(e["from_status"], e["to_status"]) for e in detail.json()["events"]] == \
        [(None, "pending"), ("pending", "completed")]
    assert detail.json()["item"]["product_id"] == product_id
    assert detail.json()["item"]["product_title"] == title

    # Its receipt: the buyer is the payer, and the item is the program.
    receipt = c.get(f"{PAYMENTS}{body['id']}/receipt/")
    assert receipt.status_code == 200, receipt.content
    data = receipt.json()["receipt"]
    assert data["payer"]["id"] == buyer.pk
    assert (data["item"]["product_id"], data["item"]["product_title"],
            data["item"]["money_type"]) == (product_id, title, "ROUTINE_PRODUCT")
    assert (data["amount"], data["currency"]) == ("2500.00", "SYP")
    assert data["invoice_number"] == payment.invoice_number

    pdf = c.get(f"{PAYMENTS}{body['id']}/receipt/?format=pdf")
    assert pdf.status_code == 200, pdf.content
    assert pdf["Content-Type"] == "application/pdf"

    # The history list carries it.
    listed = c.get(PAYMENTS).json()
    rows = listed.get("results", listed) if isinstance(listed, dict) else listed
    assert body["id"] in [row["id"] for row in rows]

    # And the confirmation emailed the receipt, as a plan's does.
    assert EmailDelivery.objects.filter(recipient=buyer, kind="payment_receipt",
                                        dedup_key=f"payment_receipt:{payment.pk}").exists()


def test_b167_the_pdf_names_the_program_not_a_plan_of_none_days(
        make_user, api, shamcash, monkeypatch, django_capture_on_commit_callbacks):
    """The PDF's item line was `plan_name · duration_days days`: for a program that is
    " · None days". It is the program's title."""
    from routine.product_models import RoutineProduct
    from subscription.models import Payment
    from subscription.receipts import item_line, receipt_data, render_pdf
    from tests.test_receipts import _pdf_codepoints

    _trainer, _buyer, _c, product_id, body = _reconciled(
        make_user, api, monkeypatch, django_capture_on_commit_callbacks, "b167pdf")
    payment = Payment.objects.get(pk=body["id"])
    title = RoutineProduct.objects.get(pk=product_id).title_in("en")
    data = receipt_data(payment)
    assert data["item"]["product_title"] == title
    assert item_line(data["item"]) == title
    # Every letter of the title is drawn ("k" of "weeks" is in no label of the page).
    assert set(map(ord, title)) <= _pdf_codepoints(render_pdf(data, compress=False))


def test_b167_nobody_else_reads_the_buyers_product_payment(
        make_user, api, shamcash, monkeypatch, django_capture_on_commit_callbacks):
    trainer, _buyer, _c, _product_id, body = _reconciled(
        make_user, api, monkeypatch, django_capture_on_commit_callbacks, "b167x")
    for caller in (api(make_user("b167_stranger")), api(trainer)):
        assert caller.get(f"{PAYMENTS}{body['id']}/").status_code == 404
        resp = caller.get(f"{PAYMENTS}{body['id']}/receipt/")
        assert (resp.status_code, resp.json()["code"]) == (404, "not_found")
        listed = caller.get(PAYMENTS).json()
        rows = listed.get("results", listed) if isinstance(listed, dict) else listed
        assert body["id"] not in [row["id"] for row in rows]


def test_b167_a_plan_payment_reads_as_it_did(make_user, api, shamcash,
                                             django_capture_on_commit_callbacks):
    """The plan's row is unchanged: no product keys on its item."""
    from subscription.models import Payment, Subscription
    from subscription.services.payment_service import PaymentService
    from tests.test_fx_subs import _person, _plan

    plan = _plan("b167plan", "13000.00")
    user = _person(make_user, "b167_plan")
    sub = Subscription.objects.create(user=user, plan=plan, status="pending")
    payment = Payment.objects.create(subscription=sub, amount=Decimal("13000.00"),
                                     currency="SYP", payment_method="shamcash",
                                     status="pending")
    with django_capture_on_commit_callbacks(execute=True):
        PaymentService.complete_payment(payment.id, {
            "amount": "13000.00", "currency": "SYP", "status": "completed",
            "transaction_id": "tx-b167-plan"})
    c = api(user)
    receipt = c.get(f"{PAYMENTS}{payment.id}/receipt/").json()["receipt"]
    assert receipt["payer"]["id"] == user.pk
    assert "product_id" not in receipt["item"] and receipt["item"]["plan_id"] == plan.id
    row = c.get(f"{PAYMENTS}{payment.id}/").json()
    assert set(row["item"]) == {"plan_id", "plan_name", "plan_type", "duration_days"}
