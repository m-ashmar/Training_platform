"""Every payment transition is an immutable event, and the money notices of the spec's
table (section 7; PAYMENTS_COMPLIANCE_PLAN step 5, the remainder).

"Six states, one direction, and every transition writes an immutable event row (who or
what, when, from, to, evidence)." And the notifications "the brief says are missing":
payment confirmed (push, in-app, receipt), and trainer fee held / released / refunded
(push, both sides). "Notification text sent to play and ios channels ... states the
fact ... and carries no renew button, price or link."
"""
import hashlib
import hmac
import importlib
import json
import os
import re
import time
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from django.db import DatabaseError, connection, transaction
from django.utils import timezone

from tests.test_backend_bugs_app_0923b import _plan
from tests.test_fx_subs import WEBHOOK_SECRET, _checkout, _paid, _person, _post, _subscribe, shamcash  # noqa: F401
from tests.test_payment_review import _account, _open_checkout, _poll, _txn

pytestmark = pytest.mark.django_db

D = Decimal
JSON = "application/json"
REPO = Path(__file__).resolve().parent.parent


def _events(payment):
    from subscription.models import PaymentEvent

    return list(PaymentEvent.objects.filter(payment=payment).order_by("created_at", "id"))


def _steps(payment):
    return [(e.from_status, e.to_status, e.actor_role) for e in _events(payment)]


def _signed_webhook(reference, status="completed", amount="10000.00", currency="SYP"):
    """A delivery signed with the secret the `shamcash` fixture configures."""
    from django.test import Client

    body = json.dumps({"data": {
        "status": status, "amount": amount, "currency": currency, "reference": reference,
        "transaction_id": f"tx-{reference}-{status}", "event_id": f"ev-{reference}-{status}",
    }}).encode()
    ts = str(int(time.time()))
    signature = hmac.new(WEBHOOK_SECRET.encode(), f"{ts}.".encode() + body,
                         hashlib.sha256).hexdigest()
    return Client().post("/api/subscription/webhook/shamcash/", data=body,
                         content_type=JSON, HTTP_X_SHAMCASH_SIGNATURE=signature,
                         HTTP_X_SHAMCASH_TIMESTAMP=ts)


def _notices(user, event_type):
    from notifications.models import Notification

    return list(Notification.objects.filter(recipient=user, event_type=event_type))


def _rendered(notice, language):
    """The title and body a device would show, in `language`."""
    from django.utils import translation

    from notifications.channels.fcm import _resolve_event_template
    from notifications.template_resolver import NotificationTemplateResolver

    with translation.override(language):
        return NotificationTemplateResolver.render(
            event_type=notice.event_type, template=_resolve_event_template(notice.event_type),
            context=notice.metadata.get("context", {}), recipient_id=notice.recipient_id)


def _states_the_fact_only(notice):
    """No amount, no link, no call to buy — in both languages."""
    for language in ("en", "ar"):
        title, body = _rendered(notice, language)
        text = f"{title} {body}"
        assert "%(" not in text, text
        assert not re.search(r"\d{3,}|http|www\.|SYP|USD|ل\.س", text), text
    title, body = _rendered(notice, "ar")
    assert re.search(r"[؀-ۿ]", title + body), "the Arabic text is not in the catalogue"


# =========================================================== the event of each step
def test_a_checkout_opened_from_the_app_is_its_first_event(make_user, api, shamcash):
    """OPEN: session created — by the customer, from the build that asked."""
    from subscription.models import Payment

    plan = _plan("pe_open", "10000.00")
    user = _person(make_user, "pe_open")
    c, sub = _subscribe(api, user, plan)
    opened_resp = _post(c, "/api/subscription/v1/gateways/",
                        {"gateway": "shamcash", "subscription_id": str(sub.id)},
                        HTTP_X_APP_VERSION="1.4.0")
    assert opened_resp.status_code == 200, opened_resp.content
    payment = Payment.objects.get(subscription=sub)

    (opened,) = _events(payment)
    assert (opened.from_status, opened.to_status, opened.actor_role, opened.actor_id) == \
        ("", "pending", "customer", user.pk)
    assert opened.backfill is False
    assert opened.evidence["reference"] == payment.gateway_transaction_reference
    assert (opened.evidence["amount"], opened.evidence["currency"],
            opened.evidence["money_type"]) == ("10000.00", "SYP", "PLATFORM_SUB")
    assert (opened.evidence["channel"], opened.evidence["app_version"]) == ("apk", "1.4.0")

    # A second tap hands back the same checkout and writes nothing: nothing moved.
    assert _checkout(c, sub).status_code == 200
    assert len(_events(payment)) == 1


def test_the_poller_confirming_it_and_an_operator_refunding_it_are_two_more_events(
        make_user, api, shamcash, monkeypatch, django_capture_on_commit_callbacks):
    from django.test import Client

    plan = _plan("pe_paid", "10000.00")
    c, sub, payment = _open_checkout(api, make_user, "pe_paid", plan)
    _paid(monkeypatch, payment.gateway_transaction_reference, "10000.00")
    with django_capture_on_commit_callbacks(execute=True):
        assert _poll()["completed"] == 1

    payment.refresh_from_db()
    assert payment.status == "completed"
    assert _steps(payment) == [("", "pending", "customer"),
                               ("pending", "completed", "poller")]
    confirmed = _events(payment)[-1]
    assert confirmed.actor_id is None
    assert (confirmed.evidence["transaction_id"], confirmed.evidence["amount"],
            confirmed.evidence["currency"]) == \
        (f"txn-{payment.gateway_transaction_reference}", "10000.00", "SYP")
    assert "gateway_data" not in confirmed.evidence, "the raw payload is the payment's"

    operator = make_user("pe_paid_op", user_type="admin")
    console = Client()
    console.force_login(operator)
    with django_capture_on_commit_callbacks(execute=True):
        console.post("/dj-admin/subscription/payment/",
                     {"action": "mark_as_refunded", "_selected_action": [str(payment.id)]})
    payment.refresh_from_db()
    assert payment.status == "refunded"
    refunded = _events(payment)[-1]
    assert (refunded.from_status, refunded.to_status, refunded.actor_role,
            refunded.actor_id) == ("completed", "refunded", "operator", operator.pk)
    assert refunded.evidence["reason"] == "Refunded by an administrator"


def test_a_webhook_is_the_gateway_and_check_my_payment_is_the_customer(
        make_user, api, shamcash, monkeypatch, django_capture_on_commit_callbacks):
    plan = _plan("pe_hook", "10000.00")
    _c, _sub, hooked = _open_checkout(api, make_user, "pe_hook", plan)
    with django_capture_on_commit_callbacks(execute=True):
        assert _signed_webhook(hooked.gateway_transaction_reference).status_code == 200
    assert _steps(hooked)[-1] == ("pending", "completed", "gateway")
    assert _events(hooked)[-1].evidence["event_id"] == \
        f"ev-{hooked.gateway_transaction_reference}-completed"

    c, sub, asked = _open_checkout(api, make_user, "pe_ask", plan)
    _paid(monkeypatch, asked.gateway_transaction_reference, "10000.00")
    with django_capture_on_commit_callbacks(execute=True):
        answer = c.post(f"/api/subscription/v1/payments/{asked.id}/reconcile/")
    assert answer.status_code == 200 and answer.json()["status"] == "completed"
    last = _events(asked)[-1]
    assert (last.from_status, last.to_status, last.actor_role, last.actor_id) == \
        ("pending", "completed", "customer", sub.user_id)


def test_a_hold_and_each_operator_decision_are_events_naming_the_operator(
        make_user, api, shamcash, monkeypatch, django_capture_on_commit_callbacks):
    from subscription.models import TransferReview
    from subscription.services.payment_service import PaymentService

    plan = _plan("pe_hold", "10000.00")
    _c, _sub, approved = _open_checkout(api, make_user, "pe_hold_a", plan)
    _c, _sub, rejected = _open_checkout(api, make_user, "pe_hold_r", plan)
    _account(monkeypatch, [
        _txn("tx-pe-a", "9500.00", note=approved.gateway_transaction_reference),
        _txn("tx-pe-r", "9000.00", note=rejected.gateway_transaction_reference)])
    with django_capture_on_commit_callbacks(execute=True):
        assert _poll()["held"] == 2

    held = _events(approved)[-1]
    review = TransferReview.objects.get(transaction_id="tx-pe-a")
    assert (held.from_status, held.to_status, held.actor_role) == \
        ("pending", "needs_review", "poller")
    assert (held.evidence["review_id"], held.evidence["reason"], held.evidence["amount"],
            held.evidence["transaction_id"]) == \
        (str(review.pk), "amount_differs", "9500.00", "tx-pe-a")

    operator = make_user("pe_hold_op", user_type="admin")
    with django_capture_on_commit_callbacks(execute=True):
        PaymentService.approve_review(review.id, operator, "short by 500, accepted")
        PaymentService.reject_review(TransferReview.objects.get(transaction_id="tx-pe-r").id,
                                     operator, "returned to sender by hand")

    done = _events(approved)[-1]
    assert (done.from_status, done.to_status, done.actor_role, done.actor_id) == \
        ("needs_review", "completed", "operator", operator.pk)
    assert (done.evidence["review_id"], done.evidence["note"], done.evidence["price"],
            done.evidence["amount"]) == \
        (str(review.pk), "short by 500, accepted", "10000.00", "9500.00")
    assert _steps(rejected)[-1] == ("needs_review", "rejected", "operator")
    assert _events(rejected)[-1].evidence["note"] == "returned to sender by hand"


def test_the_poller_closing_a_checkout_is_an_event_with_its_reason(
        make_user, api, shamcash, monkeypatch, settings):
    settings.PAYMENT_CHECKOUT_TTL_MINUTES = 60
    plan = _plan("pe_exp", "10000.00")
    _c, _sub, payment = _open_checkout(api, make_user, "pe_exp", plan,
                                       age=timedelta(hours=2))
    _account(monkeypatch, [])
    assert _poll()["expired"] == 1
    last = _events(payment)[-1]
    assert (last.from_status, last.to_status, last.actor_role) == \
        ("pending", "expired", "poller")
    assert last.evidence["reason"] == "Not completed within its window"


def test_a_step_that_rolls_back_leaves_no_event(make_user, api, shamcash):
    from subscription.models import Payment
    from subscription.services.payment_service import PaymentService

    plan = _plan("pe_rb", "10000.00")
    _c, _sub, payment = _open_checkout(api, make_user, "pe_rb", plan)
    before = _steps(payment)
    with pytest.raises(RuntimeError):
        with transaction.atomic():
            PaymentService.expire_payment(payment.id, "window closed")
            raise RuntimeError("the caller failed after the transition")
    assert Payment.objects.get(pk=payment.pk).status == "pending"
    assert _steps(payment) == before


def test_status_moves_in_one_line_and_that_line_writes_the_event():
    """The event is only as complete as the list of places a status moves. There is one:
    `PaymentService._move`. Any other assignment — in the service or anywhere else —
    would be a transition with no record."""
    source = (REPO / "subscription/services/payment_service.py").read_text()
    assigned = re.findall(r"^\s*payment\.status\s*=", source, re.M)
    assert len(assigned) == 1, assigned
    move = source.split("def _move(", 1)[1].split("\n    @", 1)[0]
    assert re.search(r"payment\.status\s*=\s*to_status", move)
    assert "PaymentEvent.objects.create(" in move

    skip = {".venv", "_excluded", "tests", "mobile", "flutter", "yalla_gym", ".claude",
            ".git", "node_modules", "__pycache__", "migrations"}
    offenders, seen = [], 0
    for root, dirs, files in os.walk(REPO):
        dirs[:] = [d for d in dirs if d not in skip]
        for name in files:
            if not name.endswith(".py"):
                continue
            path = Path(root) / name
            rel = path.relative_to(REPO).as_posix()
            if rel == "subscription/services/payment_service.py":
                continue
            seen += 1
            text = path.read_text(errors="ignore")
            for pattern in (r"\bpayment\.status\s*=[^=]",
                            r"Payment\.objects[^\n]*\.update\([^)]*\bstatus\s*="):
                offenders += [f"{rel}: {m.group(0)}" for m in re.finditer(pattern, text)]
    assert seen > 100, seen
    assert not offenders, offenders


# ============================================================== immutability
def test_an_event_cannot_be_changed_or_deleted_by_any_path(make_user, api, shamcash):
    from subscription.models import ImmutablePaymentEvent, PaymentEvent

    plan = _plan("pe_imm", "10000.00")
    _c, _sub, payment = _open_checkout(api, make_user, "pe_imm", plan)
    (event,) = _events(payment)
    event.to_status = "completed"
    with pytest.raises(ImmutablePaymentEvent):
        event.save()
    with pytest.raises(ImmutablePaymentEvent):
        event.delete()
    if connection.vendor != "postgresql":
        pytest.skip("the append-only trigger is PostgreSQL-only")
    for statement in (lambda: PaymentEvent.objects.filter(pk=event.pk).update(
                          to_status="completed"),
                      lambda: PaymentEvent.objects.filter(pk=event.pk).update(
                          evidence={}),
                      lambda: PaymentEvent.objects.filter(pk=event.pk).delete()):
        with pytest.raises(DatabaseError, match="append-only"):
            with transaction.atomic():
                statement()
    assert PaymentEvent.objects.get(pk=event.pk).to_status == "pending"

    # The one change allowed: the acting account is deleted (`on_delete=SET_NULL`).
    PaymentEvent.objects.filter(pk=event.pk).update(actor=None)
    assert PaymentEvent.objects.get(pk=event.pk).actor_id is None
    with connection.cursor() as cursor:
        cursor.execute("SELECT tgname FROM pg_trigger WHERE tgrelid = "
                       "'subscription_paymentevent'::regclass AND NOT tgisinternal")
        assert "subscription_payment_event_append_only" in {n for (n,) in cursor.fetchall()}


# ================================================================== backfill
def test_the_backfill_gives_every_older_payment_one_marked_event_and_runs_once(make_user):
    from django.apps import apps

    from subscription.models import Payment, PaymentEvent, Subscription

    migration = importlib.import_module(
        "subscription.migrations.0016_every_payment_transition_is_an_immutable_event")
    plan = _plan("pe_back", "10000.00")
    sub = Subscription.objects.create(user=make_user("pe_back"), plan=plan, status="active")
    long_ago = timezone.now() - timedelta(days=40)
    paid_at = timezone.now() - timedelta(days=30)
    held_at = timezone.now() - timedelta(days=2)

    def old(status, **extra):
        return Payment.objects.create(subscription=sub, amount=plan.price, currency="SYP",
                                      status=status, payment_method="shamcash", **extra)

    rows = {
        "pending": old("pending"),
        "completed": old("completed", completed_at=paid_at),
        "needs_review": old("needs_review",
                            metadata={"review": {"held_at": held_at.isoformat()}}),
        "refunded": old("refunded"),
    }
    Payment.objects.filter(pk__in=[p.pk for p in rows.values()]).update(
        created_at=long_ago, updated_at=long_ago + timedelta(days=1))
    recorded = old("pending")
    PaymentEvent.objects.create(payment=recorded, to_status="pending", actor_role="customer")

    migration.backfill(apps, None)

    for status, payment in rows.items():
        (event,) = _events(payment)
        assert (event.from_status, event.to_status, event.actor_role, event.backfill,
                event.actor_id) == ("", status, "system", True, None)
    assert _events(rows["pending"])[0].created_at == long_ago
    assert _events(rows["completed"])[0].created_at == paid_at
    assert _events(rows["completed"])[0].evidence["at_from"] == "completed_at"
    assert _events(rows["needs_review"])[0].created_at == held_at
    assert _events(rows["refunded"])[0].created_at == long_ago + timedelta(days=1)
    assert [e.backfill for e in _events(recorded)] == [False], \
        "a payment that already has its history is left alone"

    count = PaymentEvent.objects.count()
    migration.backfill(apps, None)
    assert PaymentEvent.objects.count() == count, "a second run adds nothing"


# ======================================================= what the app is shown
def test_the_status_and_detail_reads_serve_the_events_without_the_evidence(
        make_user, api, shamcash, monkeypatch, django_capture_on_commit_callbacks):
    plan = _plan("pe_read", "10000.00")
    c, sub, payment = _open_checkout(api, make_user, "pe_read", plan)
    _account(monkeypatch, [_txn("tx-pe-read", "9000.00",
                                note=payment.gateway_transaction_reference,
                                sender="shamcash:0999000111")])
    with django_capture_on_commit_callbacks(execute=True):
        _poll()

    status = c.get(f"/api/subscription/v1/payments/{payment.id}/status/")
    assert status.status_code == 200, status.content
    events = status.json()["events"]
    assert [(e["from_status"], e["to_status"], e["by"], e["backfill"]) for e in events] == \
        [(None, "pending", "customer", False), ("pending", "needs_review", "poller", False)]
    assert set(events[0]) == {"from_status", "to_status", "at", "by", "backfill"}
    text = json.dumps(events)
    assert "9000" not in text and "0999000111" not in text and "tx-pe-read" not in text

    detail = c.get(f"/api/subscription/v1/payments/{payment.id}/").json()
    assert detail["events"] == events
    listed = c.get("/api/subscription/v1/payments/").json()
    rows = listed.get("results", listed) if isinstance(listed, dict) else listed
    assert rows and all("events" not in row for row in rows)

    # A history carries no price: the same events on every build.
    for channel in ("play", "ios", "web"):
        other = c.get(f"/api/subscription/v1/payments/{payment.id}/status/",
                      HTTP_X_APP_CHANNEL=channel).json()["events"]
        assert other == events, channel


def test_operators_read_every_event_with_its_evidence_in_the_console(
        make_user, api, shamcash, monkeypatch, django_capture_on_commit_callbacks):
    from django.test import Client

    plan = _plan("pe_console", "10000.00")
    _c, _sub, payment = _open_checkout(api, make_user, "pe_console", plan)
    _account(monkeypatch, [_txn("tx-pe-console", "9000.00",
                                note=payment.gateway_transaction_reference)])
    with django_capture_on_commit_callbacks(execute=True):
        _poll()
    operator = make_user("pe_console_op", user_type="admin")
    console = Client()
    console.force_login(operator)
    page = console.get(f"/dj-admin/subscription/payment/{payment.id}/change/")
    assert page.status_code == 200
    html = page.content.decode()
    # The inline draws the choices' labels; the evidence is the JSON the service wrote.
    assert "Needs review" in html and "Poller" in html and "Customer" in html
    assert "tx-pe-console" in html and "amount_differs" in html
    assert "paymentevent_set" not in html and "events-0-DELETE" not in html


def test_the_export_lists_the_steps_and_erasure_keeps_them(
        make_user, api, shamcash, monkeypatch, django_capture_on_commit_callbacks):
    from training_platform.privacy.registry import export_user_data

    plan = _plan("pe_export", "10000.00")
    _c, sub, payment = _open_checkout(api, make_user, "pe_export", plan)
    sections = export_user_data(sub.user)["sections"]
    (row,) = sections["payment_events"]
    assert (row["from_status"], row["to_status"], row["actor_role"]) == \
        ("", "pending", "customer")
    assert "evidence" not in row and "actor" not in row


# ============================================================ payment_confirmed
def test_a_confirmed_payment_tells_the_customer_once_with_the_receipt(
        make_user, api, shamcash, monkeypatch, django_capture_on_commit_callbacks):
    from subscription.services.payment_service import PaymentService

    plan = _plan("pe_conf", "10000.00")
    c, sub, payment = _open_checkout(api, make_user, "pe_conf", plan)
    _paid(monkeypatch, payment.gateway_transaction_reference, "10000.00")
    with django_capture_on_commit_callbacks(execute=True):
        _poll()
    payment.refresh_from_db()

    (notice,) = _notices(sub.user, "payment_confirmed")
    data = notice.metadata["data"]
    assert data == {"type": "payment_confirmed", "payment_id": str(payment.id),
                    "invoice_number": payment.invoice_number}
    assert payment.invoice_number
    assert c.get(f"/api/subscription/v1/payments/{data['payment_id']}/receipt/"
                 ).status_code == 200, "the receipt the notice points at is there"
    _states_the_fact_only(notice)

    # A replay of the same money grants nothing and tells nobody again.
    with django_capture_on_commit_callbacks(execute=True):
        PaymentService.complete_payment(payment.id, {
            "amount": "10000.00", "currency": "SYP", "status": "completed",
            "transaction_id": f"txn-{payment.gateway_transaction_reference}"})
    assert len(_notices(sub.user, "payment_confirmed")) == 1


def test_the_notice_is_sent_even_when_the_payout_behind_it_fails(
        make_user, api, shamcash, monkeypatch, django_capture_on_commit_callbacks):
    """The notice is registered before the payout and cannot be stopped by it: the
    payment committed, so the customer is told, and the payout's failure still raises
    for its own retry."""
    from subscription.services.payment_service import payment_completed

    plan = _plan("pe_boom", "10000.00")
    _c, sub, payment = _open_checkout(api, make_user, "pe_boom", plan)
    _paid(monkeypatch, payment.gateway_transaction_reference, "10000.00")

    def boom(**kwargs):
        raise RuntimeError("payout failed")

    payment_completed.connect(boom, dispatch_uid="pe_boom")
    try:
        with pytest.raises(RuntimeError, match="payout failed"):
            with django_capture_on_commit_callbacks(execute=True):
                _poll()
    finally:
        payment_completed.disconnect(dispatch_uid="pe_boom")
    assert len(_notices(sub.user, "payment_confirmed")) == 1


# ============================================================== coaching fees
def _accepted(make_user, api, tag, django_capture_on_commit_callbacks):
    """A client asked, the trainer accepted: month 1 is held (users/coaching.py)."""
    from users.models import TrainerClientRelation
    from wallet.models import Wallet

    trainer = make_user(f"pe_t_{tag}", user_type="trainer", trainer_hourly_rate=D("100.00"),
                        first_name="Rami", last_name="Haddad")
    client = make_user(f"pe_c_{tag}", first_name="Sami", last_name="Nasser")
    Wallet.objects.filter(owner=client).update(balance=D("1000.00"))
    assert api(client).post("/api/auth/client/request-trainer/", {"trainer_id": trainer.pk},
                            content_type=JSON).status_code == 200
    relation = TrainerClientRelation.objects.get(trainer=trainer, client=client)
    with django_capture_on_commit_callbacks(execute=True):
        answered = api(trainer).post("/api/auth/trainer/respond-to-request/",
                                     {"request_id": relation.pk, "action": "approve"},
                                     content_type=JSON)
    assert answered.status_code == 200, answered.content
    relation.refresh_from_db()
    return trainer, client, relation


def _fee(user, event_type, month=None):
    notices = _notices(user, event_type)
    if month is not None:
        notices = [n for n in notices if n.metadata["data"]["month"] == str(month)]
    return notices


def test_month_one_held_then_paid_out_tells_both_people(
        make_user, api, settings, django_capture_on_commit_callbacks):
    from routine.models import Routine

    settings.COACHING_COMMISSION_RATE = D("0.20")
    trainer, client, relation = _accepted(make_user, api, "m1",
                                          django_capture_on_commit_callbacks)
    (month,) = relation.periods.all()

    (to_client,) = _fee(client, "coaching_fee_held")
    (to_trainer,) = _fee(trainer, "coaching_fee_held")
    assert to_client.metadata["data"] == {
        "type": "coaching_fee_held", "period_id": str(month.pk), "month": "1",
        "role": "client", "user_id": str(trainer.pk)}
    assert (to_trainer.metadata["data"]["role"], to_trainer.metadata["data"]["user_id"]) == \
        ("trainer", str(client.pk))
    assert to_client.metadata["context"]["name"] == "Rami Haddad"
    assert to_trainer.metadata["context"]["name"] == "Sami Nasser"
    _states_the_fact_only(to_client)
    assert not _fee(client, "coaching_fee_released")

    routine = Routine.objects.create(name="Month one", created_by=trainer, days=3)
    with django_capture_on_commit_callbacks(execute=True):
        sent = api(trainer).post(f"/api/routine/routines/{routine.pk}/assign_to_client/",
                                 {"client_id": client.pk}, content_type=JSON)
    assert sent.status_code == 200, sent.content
    (released_client,) = _fee(client, "coaching_fee_released")
    (released_trainer,) = _fee(trainer, "coaching_fee_released")
    assert (released_client.metadata["data"]["role"],
            released_trainer.metadata["data"]["role"]) == ("client", "trainer")
    assert released_trainer.metadata["context"]["month"] == 1
    _states_the_fact_only(released_trainer)


def test_a_renewal_tells_the_trainer_and_leaves_the_client_to_coaching_renewed(
        make_user, api, settings, django_capture_on_commit_callbacks):
    from users.coaching import renew_due
    from users.trainer_relations import settle_hold

    settings.COACHING_COMMISSION_RATE = D("0.20")
    trainer, client, relation = _accepted(make_user, api, "renew",
                                          django_capture_on_commit_callbacks)
    with django_capture_on_commit_callbacks(execute=True):
        settle_hold(relation, trainer)
    period = relation.periods.get(number=1)
    shift = period.ends_at - (timezone.now() - timedelta(minutes=1))
    type(period).objects.filter(pk=period.pk).update(starts_at=period.starts_at - shift,
                                                     ends_at=period.ends_at - shift)

    with django_capture_on_commit_callbacks(execute=True):
        renew_due()
    assert relation.periods.filter(number=2).exists()
    assert len(_fee(trainer, "coaching_fee_released", month=2)) == 1
    assert not _fee(client, "coaching_fee_released", month=2)
    assert len(_notices(client, "coaching_renewed")) == 1
    assert not _fee(client, "coaching_fee_held", month=2), "a renewal is never held"


def test_a_held_month_returned_to_the_client_tells_both_people(
        make_user, api, django_capture_on_commit_callbacks):
    from users.trainer_relations import release_hold

    trainer, client, relation = _accepted(make_user, api, "back",
                                          django_capture_on_commit_callbacks)
    with django_capture_on_commit_callbacks(execute=True):
        assert release_hold(relation, client) is not None
    (to_client,) = _fee(client, "coaching_fee_refunded")
    (to_trainer,) = _fee(trainer, "coaching_fee_refunded")
    assert (to_client.metadata["data"]["role"], to_trainer.metadata["data"]["role"]) == \
        ("client", "trainer")
    _states_the_fact_only(to_client)


def test_an_erased_client_is_not_written_to_and_the_trainer_is_told(
        make_user, api, django_capture_on_commit_callbacks):
    from training_platform.privacy.registry import erase_user_data

    trainer, client, relation = _accepted(make_user, api, "erase",
                                          django_capture_on_commit_callbacks)
    with django_capture_on_commit_callbacks(execute=True):
        erase_user_data(client)
    assert len(_fee(trainer, "coaching_fee_refunded")) == 1
    assert not _fee(client, "coaching_fee_refunded")


def test_an_owed_share_is_announced_to_the_trainer_when_it_is_paid(
        make_user, api, settings, django_capture_on_commit_callbacks):
    """A share that has to cross a currency on a stale rate stays owed in escrow: the
    client is told month 1 was paid out, the trainer only when the share lands."""
    from users.coaching import settle_owed_payouts
    from users.models import TrainerClientRelation
    from users.trainer_relations import settle_hold
    from wallet.models import ExchangeRate, Wallet, set_currency_for

    settings.COACHING_COMMISSION_RATE = D("0.20")
    rate = ExchangeRate.objects.create(base="USD", quote="SYP", rate=D("13000"),
                                       effective_from=timezone.now() - timedelta(hours=1))
    trainer = make_user("pe_owed_t", user_type="trainer", trainer_hourly_rate=D("100.00"))
    set_currency_for(trainer, "USD", "trainer")
    client = make_user("pe_owed_c")
    set_currency_for(client, "SYP", "client")
    Wallet.objects.filter(owner=client).update(balance=D("2000000.00"))
    assert api(client).post("/api/auth/client/request-trainer/", {"trainer_id": trainer.pk},
                            content_type=JSON).status_code == 200
    relation = TrainerClientRelation.objects.get(trainer=trainer, client=client)
    with django_capture_on_commit_callbacks(execute=True):
        assert api(trainer).post("/api/auth/trainer/respond-to-request/",
                                 {"request_id": relation.pk, "action": "approve"},
                                 content_type=JSON).status_code == 200
    ExchangeRate.objects.filter(pk=rate.pk).update(
        effective_from=timezone.now() - timedelta(days=30))

    with django_capture_on_commit_callbacks(execute=True):
        settle_hold(relation, trainer)
    (month,) = relation.periods.all()
    assert month.payout_owed_since is not None
    assert len(_fee(client, "coaching_fee_released")) == 1
    assert not _fee(trainer, "coaching_fee_released")

    ExchangeRate.objects.create(base="USD", quote="SYP", rate=D("13000"),
                                effective_from=timezone.now() - timedelta(minutes=5))
    with django_capture_on_commit_callbacks(execute=True):
        assert settle_owed_payouts()["paid"] == 1
    assert len(_fee(trainer, "coaching_fee_released")) == 1
    assert len(_fee(client, "coaching_fee_released")) == 1, "the client was already told"
