"""SIDE_BUGS S-184 and S-252 (PAY-2): a cancellation is not a payment.

`POST v1/subscriptions/{id}/cancel/` wrote `Payment.objects.create(status='cancelled',
amount=0)` itself — outside PaymentService (the one place a payment's state moves), with
no PaymentEvent, in the PLAN's currency rather than the buyer's — and `GET v1/payments/`
listed it in the person's history (the app had learned to hide it). It now writes no
payment: the fact goes to the operator's audit log (`subscription.cancelled`), and the
free-text reason, which that log would keep after an erasure, is not kept.
"""
import pathlib
import re

import pytest

from tests.test_road10_s213_wallet_plan import _person, _plan, _subscription

pytestmark = pytest.mark.django_db

JSON = "application/json"
ROOT = pathlib.Path(__file__).resolve().parent.parent


def _cancel(api, user, sub, **body):
    return api(user).post(f"/api/subscription/v1/subscriptions/{sub.pk}/cancel/", body,
                          content_type=JSON)


def _audits(user):
    from wallet.models import WalletAuditLog

    return list(WalletAuditLog.objects.filter(actor=user, event_type="subscription.cancelled"))


@pytest.mark.parametrize("immediate", [True, False])
def test_a_cancellation_writes_no_payment_and_is_audited(make_user, api, immediate):
    from subscription.models import Payment

    user = _person(make_user, f"s184_{immediate}")
    sub = _subscription(user, _plan(f"s184 {immediate}"), status="active")
    history = api(user).get("/api/subscription/v1/payments/").json()

    resp = _cancel(api, user, sub, reason="moving to another city", immediate=immediate)
    assert resp.status_code == 200, resp.content
    assert not Payment.objects.filter(subscription=sub).exists()
    assert api(user).get("/api/subscription/v1/payments/").json() == history
    (entry,) = _audits(user)
    assert entry.payload == {"subscription_id": str(sub.pk), "plan_id": sub.plan_id,
                             "immediate": immediate, "reason_given": True}
    assert "moving" not in str(entry.payload), "the free text is not kept"


def test_turning_renewal_off_twice_is_one_cancellation(make_user, api):
    user = _person(make_user, "s184_twice")
    sub = _subscription(user, _plan("s184 twice"), status="active")
    assert _cancel(api, user, sub).status_code == 200
    assert _cancel(api, user, sub).status_code == 200
    assert len(_audits(user)) == 1


def test_only_the_payment_service_creates_a_payment():
    """The source scan in tests/test_payment_events.py matches status writes only, which
    is how this row went unseen. Every `Payment(...)` / `Payment.objects.create(...)` in
    the server's own code is in subscription/services/payment_service.py."""
    pattern = re.compile(r"\bPayment\.objects\.create\(|(?<![\w.])Payment\(")
    found = []
    for path in ROOT.rglob("*.py"):
        rel = path.relative_to(ROOT).as_posix()
        if rel.startswith(("tests/", ".venv", "_excluded/", "mobile/", "flutter/",
                           "yalla_gym/")) or "/migrations/" in rel:
            continue
        for number, line in enumerate(path.read_text(errors="ignore").splitlines(), 1):
            code = line.split("#", 1)[0]
            if pattern.search(code) and not code.lstrip().startswith("class "):
                found.append(f"{rel}:{number}")
    assert found and all(f.startswith("subscription/services/payment_service.py:")
                         for f in found), found
