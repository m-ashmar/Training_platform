"""A checkout's purchase key is the server's own (SIDE_BUGS S-234, part a).

`routine.products.grant_checkout` writes the purchase a confirmed checkout delivers
under the key `checkout:<payment id>`, in the same column and per-buyer uniqueness a
buyer's wallet keys use (`routine_purchase_key_per_buyer`). On 554c604 the wallet buy
took ANY 1-64 character key, so a buyer who bought another program keyed
`checkout:<their session id>` and then paid the session made its delivery raise
IntegrityError inside the completion — and the poller, which reads oldest first, stopped
at that row on every run: nobody's checkout after it was confirmed or expired. The test
below fails there: the wallet buy answers 200 and the poll raises.
"""
from decimal import Decimal

import pytest

from tests.test_fx_subs import _person, shamcash  # noqa: F401
from tests.test_hub_checkout import SESSIONS, WEB, _aged, _poll, _session
from tests.test_payment_review import _account, _txn
from tests.test_routine_products import _buy, _fund, _published

pytestmark = pytest.mark.django_db

D = Decimal


@pytest.fixture(autouse=True)
def owners_rate(settings):
    settings.COACHING_COMMISSION_RATE = D("0.20")


def test_a_buyer_cannot_spend_a_checkouts_key_and_the_checkout_is_delivered(
        make_user, api, shamcash, monkeypatch, django_capture_on_commit_callbacks):
    from routine.models import RoutinePurchase

    _t1, _tpl1, session_product = _published(make_user, api, "s234a")
    _t2, _tpl2, other_product = _published(make_user, api, "s234b")
    buyer = _person(make_user, "s234_buyer")
    _fund(buyer, "5000.00")
    c = api(buyer)
    opened = _session(c, "ROUTINE_PRODUCT", session_product)
    assert opened.status_code == 201, opened.content
    session = opened.json()

    # The server's key, in any case, is refused before anything is read or moved.
    for key in (f"checkout:{session['id']}", f"CHECKOUT:{session['id']}"):
        resp = _buy(api, buyer, other_product, key=key)
        assert resp.status_code == 400, resp.content
        body = resp.json()
        assert body["code"] == "validation_error"
        assert body["field_errors"]["idempotency_key"][0]["code"] == "idempotency_key_reserved"
    assert not RoutinePurchase.objects.filter(buyer=buyer).exists()
    assert _buy(api, buyer, other_product, key="checkout-like-but-not").status_code == 200

    # The session's money arrives, and the poller delivers it.
    _aged(session["id"])
    _account(monkeypatch, [_txn("tx-s234", "2500.00", note=session["reference"])])
    with django_capture_on_commit_callbacks(execute=True):
        assert _poll()["completed"] == 1
    delivered = RoutinePurchase.objects.get(buyer=buyer, product_id=session_product)
    assert (delivered.paid_with, delivered.idempotency_key) == \
        ("shamcash", f"checkout:{session['id']}")
    assert c.get(f"{SESSIONS}{session['id']}/", **WEB).json()["status"] == "confirmed"
