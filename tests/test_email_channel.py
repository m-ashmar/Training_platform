"""The email channel of the notification system (the payments spec, section 7).

The spec's table, and what each row's email is asserted to do here:

  * Payment confirmed — the email carries the PDF receipt; one per payment, however often
    `payment_completed` fires (it fires again for an owed payout).
  * Plan expires in 7 days, 3 days, 1 day — "7-day only": one email a week ahead, once per
    cover; the 3-day PUSH is exactly what it was.
  * Plan expired — one email, with the first lapsed notice of a cover, not with each of
    the notices on days 0/3/7/14/30.
  * Withdrawal requested, paid, failed — an email with each inbox notice.

And across all of them: written in the recipient's SAVED `preferred_language`, read when
the worker writes it; a plain-text and an HTML part; sent by the worker after the commit,
never by the request; never to an unverified, suspended or erased account; stopped by the
person's notification preferences; retried when the mail server fails for a while,
recorded and reported once when it refuses for good; a message lost on the way is
published again by the hourly sweep. Every email is store-safe: no link, no price of
anything to buy (emails are not per build).

The mail backend is Django's locmem one (`mail.outbox`), which pytest-django installs.
"""
import smtplib
import uuid
from datetime import timedelta
from decimal import Decimal

import pytest
from django.core import mail
from django.utils import timezone

pytestmark = pytest.mark.django_db

JSON = "application/json"
ARABIC = range(0x0600, 0x0700)


def _tag(prefix):
    return f"{prefix}_{uuid.uuid4().hex[:8]}"


def _has_arabic(text):
    return any(ord(ch) in ARABIC for ch in text)


def _html(message):
    (html, mimetype), = message.alternatives
    assert mimetype == "text/html"
    return html


def _store_safe(message):
    """No link and no price of anything to buy, in either part."""
    for part in (message.body, _html(message), message.subject):
        assert "http" not in part and "href" not in part and "www." not in part, part


def _deliveries(user, kind=None):
    from notifications.models import EmailDelivery

    rows = EmailDelivery.objects.filter(recipient=user)
    return list(rows.filter(kind=kind) if kind else rows)


@pytest.fixture(autouse=True)
def owners_rules(settings):
    settings.SUBSCRIPTION_RENEWAL_NOTICE_DAYS = 3
    settings.SUBSCRIPTION_LAPSED_REMINDER_DAYS = (0, 3, 7, 14, 30)
    settings.SUBSCRIPTION_EXPIRY_EMAIL_DAYS = 7
    settings.DEFAULT_FROM_EMAIL = "Yalla Gym <no-reply@yalla.test>"


@pytest.fixture
def queued(monkeypatch):
    """What `queue` hands the broker, instead of the broker: the worker's side is run by
    hand (`_work`), exactly the task a worker would run."""
    from notifications.tasks import send_notification_email

    calls = []
    monkeypatch.setattr(send_notification_email, "apply_async",
                        lambda args=None, kwargs=None, **options: calls.append(tuple(args)))
    return calls


def _work(call):
    from notifications.tasks import send_notification_email

    return send_notification_email.apply(args=call).get()


@pytest.fixture
def worker_retries(settings):
    """Let the eager executor RETRY the way a worker does (see `tests/
    test_prod_services.py` `worker_retries`): with `CELERY_TASK_EAGER_PROPAGATES` on,
    Celery hands a `Retry` back to the caller and no retry ever runs."""
    from notifications.tasks import send_notification_email

    settings.CELERY_TASK_EAGER_PROPAGATES = False
    assert send_notification_email.app.conf.task_eager_propagates is False


# ------------------------------------------------------------------ payment confirmed
def _plan():
    from subscription.models import SubscriptionPlan

    tag = uuid.uuid4().hex[:6]
    return SubscriptionPlan.objects.create(
        name_en=f"Premium {tag}", name_ar=f"بريميوم {tag}", plan_type="premium",
        description="d",
        price=Decimal("25000.00"), currency="SYP", duration_days=30, is_active=True)


def _open_payment(user):
    from subscription.models import Payment, Subscription

    plan = _plan()
    subscription = Subscription.objects.create(user=user, plan=plan, status="pending")
    return Payment.objects.create(subscription=subscription, amount=plan.price,
                                  currency=plan.currency, payment_method="shamcash",
                                  status="pending")


def _complete(payment):
    from subscription.services.payment_service import PaymentService

    PaymentService.complete_payment(payment.id, {
        "amount": payment.amount, "currency": payment.currency, "status": "completed",
        "transaction_id": f"tx-{payment.id}", "reference": f"YG-{str(payment.id)[:8]}",
    })
    payment.refresh_from_db()
    return payment


def test_a_confirmed_payment_emails_its_receipt_as_a_pdf_in_the_payers_language(
        make_user, django_capture_on_commit_callbacks):
    from subscription.services.payment_service import PaymentService, payment_completed

    payer = make_user(_tag("em_pay"), preferred_language="ar", first_name="سارة")
    payment = _open_payment(payer)
    with django_capture_on_commit_callbacks(execute=True):
        payment = _complete(payment)

    (message,) = mail.outbox
    assert message.to == [payer.email]
    assert message.from_email == "Yalla Gym <no-reply@yalla.test>"
    (name, content, mimetype), = message.attachments
    assert mimetype == "application/pdf" and name == f"{payment.invoice_number}.pdf"
    assert content.startswith(b"%PDF-") and content.rstrip().endswith(b"%%EOF")

    # Arabic, because that is the language the payer saved — in the subject, both parts
    # and the page direction.
    assert "تم تأكيد الدفع" in message.subject and "يلا جيم" in message.subject
    assert "بريميوم" in message.body and payment.invoice_number in message.body
    assert "مرحبًا سارة،" in message.body
    html = _html(message)
    assert 'dir="rtl"' in html and 'lang="ar"' in html and "بريميوم" in html
    _store_safe(message)
    # The amount is on the attached receipt, not in the email's words.
    assert "25,000" not in message.body and "25000" not in message.body

    (delivery,) = _deliveries(payer, "payment_receipt")
    assert (delivery.status, delivery.language, delivery.attempts) == ("sent", "ar", 1)
    assert delivery.sent_at is not None and delivery.notification is None

    # `payment_completed` fires again for a payout still owed: one receipt email, ever.
    with django_capture_on_commit_callbacks(execute=True):
        payment_completed.send(sender=PaymentService, payment=payment,
                               subscription=payment.subscription)
        PaymentService.redeliver_payout(payment)
    assert len(mail.outbox) == 1 and len(_deliveries(payer)) == 1


def test_the_language_is_the_one_saved_when_the_worker_writes_it(
        make_user, queued, django_capture_on_commit_callbacks):
    payer = make_user(_tag("em_lang"), preferred_language="en")
    with django_capture_on_commit_callbacks(execute=True):
        _complete(_open_payment(payer))
    assert mail.outbox == [], "the email was sent before the worker ran"
    (call,) = queued

    type(payer).objects.filter(pk=payer.pk).update(preferred_language="ar")
    assert _work(call) == "sent"
    (message,) = mail.outbox
    assert _has_arabic(message.subject) and 'dir="rtl"' in _html(message)

    english = make_user(_tag("em_en"), preferred_language="en")
    with django_capture_on_commit_callbacks(execute=True):
        _complete(_open_payment(english))
    _work(queued[-1])
    message = mail.outbox[-1]
    assert message.subject == "Payment confirmed - Yalla Gym"
    assert not _has_arabic(message.body) and 'dir="ltr"' in _html(message)
    assert message.attachments[0][2] == "application/pdf"


def test_a_receipt_for_a_payment_confirmed_long_ago_is_not_emailed(
        make_user, django_capture_on_commit_callbacks):
    from subscription.models import Payment
    from subscription.services.payment_service import PaymentService, payment_completed

    payer = make_user(_tag("em_old"))
    payment = _complete(_open_payment(payer))           # no commit: nothing queued
    Payment.objects.filter(pk=payment.pk).update(
        completed_at=timezone.now() - timedelta(days=10))
    payment.refresh_from_db()
    with django_capture_on_commit_callbacks(execute=True):
        payment_completed.send(sender=PaymentService, payment=payment,
                               subscription=payment.subscription)
    assert mail.outbox == [] and _deliveries(payer) == []


# ----------------------------------------------------------------- who is emailed
def test_no_email_to_an_unverified_suspended_or_erased_account(
        make_user, django_capture_on_commit_callbacks):
    unverified = make_user(_tag("em_unv"), email_verified=False)
    suspended = make_user(_tag("em_sus"))
    erased = make_user(_tag("em_era"))
    payments = [_open_payment(user) for user in (unverified, suspended, erased)]
    type(suspended).objects.filter(pk=suspended.pk).update(is_active=False)
    type(erased).objects.filter(pk=erased.pk).update(
        email=f"retired+{erased.pk}-x@invalid.local")
    with django_capture_on_commit_callbacks(execute=True):
        for payment in payments:
            _complete(payment)
    assert mail.outbox == []
    assert all(_deliveries(user) == [] for user in (unverified, suspended, erased))


def test_a_verification_lost_before_the_send_skips_the_email(make_user, queued,
                                                             django_capture_on_commit_callbacks):
    payer = make_user(_tag("em_late"))
    with django_capture_on_commit_callbacks(execute=True):
        _complete(_open_payment(payer))
    type(payer).objects.filter(pk=payer.pk).update(is_active=False)
    assert _work(queued[0]) == "skipped"
    assert mail.outbox == []
    (delivery,) = _deliveries(payer)
    assert (delivery.status, delivery.error) == ("skipped", "inactive")


def test_the_notification_preferences_stop_the_email(make_user, api, queued,
                                                     django_capture_on_commit_callbacks):
    from notifications.models import UserNotificationPreference

    # `all` with email off: no receipt email.
    muted = make_user(_tag("em_all"))
    UserNotificationPreference.objects.create(user=muted, event_type="all",
                                              channels={"email": False})
    with django_capture_on_commit_callbacks(execute=True):
        _complete(_open_payment(muted))
    assert queued == [] and _deliveries(muted) == []

    # The receipt's own type is settable, and its email switch works.
    own = make_user(_tag("em_own"))
    types = api(own).get("/api/notifications/preferences/event_types/").json()
    assert "payment_confirmed" in types["email_event_types"]
    assert set(types["email_event_types"]) == {
        "payment_confirmed", "subscription_expiring", "subscription_expired",
        "withdrawal_requested", "withdrawal_paid", "withdrawal_failed"}
    resp = api(own).post("/api/notifications/preferences/",
                         {"event_type": "payment_confirmed", "channels": {"email": False}},
                         content_type=JSON)
    assert resp.status_code == 201, resp.content
    with django_capture_on_commit_callbacks(execute=True):
        _complete(_open_payment(own))
    assert queued == []

    # Muted after it was queued: asked again at the send.
    late = make_user(_tag("em_mute"))
    with django_capture_on_commit_callbacks(execute=True):
        _complete(_open_payment(late))
    UserNotificationPreference.objects.create(user=late, event_type="payment_confirmed",
                                              is_enabled=False)
    assert _work(queued[-1]) == "skipped"
    assert mail.outbox == [] and _deliveries(late)[0].error == "muted"


# ------------------------------------------------------------------ the plan's end
def _subscriber(make_user, **kw):
    from tests.test_subscription_freeze import _subscriber as subscriber

    return subscriber(make_user, **kw)


def test_the_seven_day_email_goes_once_and_the_three_day_push_is_unchanged(
        make_user, django_capture_on_commit_callbacks):
    from notifications.models import Notification
    from subscription.tasks import send_renewal_reminders

    week, _ = _subscriber(make_user, ends_in=timedelta(days=6, hours=12))
    soon, _ = _subscriber(make_user, ends_in=timedelta(days=2, hours=12))
    later, _ = _subscriber(make_user, ends_in=timedelta(days=10))
    type(week).objects.filter(pk=week.pk).update(preferred_language="ar")

    with django_capture_on_commit_callbacks(execute=True):
        send_renewal_reminders()
        send_renewal_reminders()

    # The push: 3 days ahead, as before, and nothing for the week-ahead subscriber.
    assert not Notification.objects.filter(recipient=week).exists()
    (push,) = Notification.objects.filter(recipient=soon, event_type="subscription_expiring")
    assert push.metadata["context"] == {"days": 3}
    assert not Notification.objects.filter(recipient=later).exists()

    # The email: once, a week ahead, in the saved language, and store-safe.
    by_recipient = {tuple(m.to): m for m in mail.outbox}
    message = by_recipient[(week.email,)]
    assert "ينتهي اشتراكك خلال 7 أيام." in message.body
    assert message.attachments == []
    _store_safe(message)
    assert "SYP" not in message.body and "SYP" not in _html(message)
    assert (later.email,) not in by_recipient
    assert [d.status for d in _deliveries(week, "subscription_expiring")] == ["sent"]
    # A subscriber first seen inside the 3 days still gets the one email (a run missed
    # for days, or the day this shipped), and says how many days are left.
    assert "Your subscription ends in 3 days." in by_recipient[(soon.email,)].body
    assert len(mail.outbox) == 2


def test_a_renewal_before_the_send_makes_the_seven_day_email_stale(make_user, queued):
    from subscription.models import Subscription
    from subscription.tasks import send_renewal_reminders

    user, sub = _subscriber(make_user, ends_in=timedelta(days=6, hours=12))
    send_renewal_reminders()
    (delivery,) = _deliveries(user, "subscription_expiring")
    Subscription.objects.filter(pk=sub.pk).update(end_date=sub.end_date + timedelta(days=30))
    assert _work((delivery.pk,)) == "skipped"
    assert mail.outbox == []


def test_the_expired_email_goes_with_the_first_lapsed_notice_only(
        make_user, django_capture_on_commit_callbacks):
    from notifications.models import Notification
    from subscription.tasks import send_renewal_reminders

    user, sub = _subscriber(make_user, status="expired", ends_in=-timedelta(minutes=5))
    type(user).objects.filter(pk=user.pk).update(preferred_language="ar")
    end = sub.effective_end
    with django_capture_on_commit_callbacks(execute=True):
        for day in range(0, 35):
            send_renewal_reminders(now=end + timedelta(days=day, hours=1))

    assert Notification.objects.filter(recipient=user,
                                       event_type="subscription_expired").count() == 5
    (message,) = mail.outbox
    assert message.subject.startswith("انتهى اشتراكك")
    assert "خطتك الغذائية وبرنامجك التدريبي محفوظان" in message.body
    _store_safe(message)
    (delivery,) = _deliveries(user)
    assert delivery.notification.related_object_id.endswith(":0")


# --------------------------------------------------------------------- withdrawals
def test_a_withdrawal_requested_paid_and_failed_each_email_the_trainer(
        make_user, api, django_capture_on_commit_callbacks):
    from notifications.models import Notification
    from payouts import services
    from tests.test_trainer_withdrawals import _operator, _ready, _request

    trainer, _method = _ready(make_user, api)
    operator = _operator(make_user)
    with django_capture_on_commit_callbacks(execute=True):
        made = _request(api, trainer, "40000").json()
    services.approve(made["id"], operator)
    services.mark_sent(made["id"], operator, f"SC-{uuid.uuid4().hex[:8]}")
    with django_capture_on_commit_callbacks(execute=True):
        services.confirm(made["id"], operator)
        services.confirm(made["id"], operator)          # idempotent: no second email

    type(trainer).objects.filter(pk=trainer.pk).update(preferred_language="ar")
    with django_capture_on_commit_callbacks(execute=True):
        failed = _request(api, trainer, "25000").json()
    services.approve(failed["id"], operator)
    services.mark_sent(failed["id"], operator, f"SC-{uuid.uuid4().hex[:8]}")
    with django_capture_on_commit_callbacks(execute=True):
        services.fail(failed["id"], operator, "الحساب غير صحيح")

    for event_type in ("withdrawal_requested", "withdrawal_paid", "withdrawal_failed"):
        assert Notification.objects.filter(recipient=trainer, event_type=event_type).exists()
    requested, paid, requested_ar, failed_ar = mail.outbox
    assert requested.subject == "Withdrawal requested - Yalla Gym"
    assert made["number"] in requested.body and "40,000.00 SYP" in requested.body
    assert paid.subject == "Your withdrawal arrived - Yalla Gym"
    assert made["number"] in paid.body
    assert requested_ar.subject.startswith("استلمنا طلب السحب")
    assert failed_ar.subject.startswith("تعذّر إتمام طلب السحب")
    assert "الحساب غير صحيح" in failed_ar.body and failed["number"] in failed_ar.body
    assert "25,000.00 SYP" in failed_ar.body
    for message in mail.outbox:
        assert message.to == [trainer.email] and message.attachments == []
        _store_safe(message)
    kinds = sorted(d.kind for d in _deliveries(trainer))
    assert kinds == ["withdrawal_failed", "withdrawal_paid", "withdrawal_requested",
                     "withdrawal_requested"]
    assert all(d.notification is not None for d in _deliveries(trainer))


def test_the_request_only_queues_the_email_and_the_worker_sends_it(
        make_user, api, queued, django_capture_on_commit_callbacks):
    from notifications.models import EmailDelivery
    from tests.test_trainer_withdrawals import _ready, _request

    trainer, _method = _ready(make_user, api)
    with django_capture_on_commit_callbacks(execute=True):
        assert _request(api, trainer, "10000").status_code == 201
    assert mail.outbox == []
    (call,) = queued
    delivery = EmailDelivery.objects.get(pk=call[0])
    assert (delivery.recipient_id, delivery.kind, delivery.status) == \
        (trainer.pk, "withdrawal_requested", "queued")
    assert _work(call) == "sent" and len(mail.outbox) == 1
    # A second message for the same row (a redelivery, the sweep) sends nothing.
    assert _work(call) == "sent" and len(mail.outbox) == 1


# ------------------------------------------------------------- failures and the sweep
def test_a_mail_server_that_fails_for_a_while_is_retried_then_delivered(
        make_user, monkeypatch, worker_retries, django_capture_on_commit_callbacks):
    from django.core.mail import EmailMultiAlternatives

    real_send = EmailMultiAlternatives.send
    attempts = []

    def flaky(self, fail_silently=False):
        attempts.append(1)
        if len(attempts) == 1:
            raise smtplib.SMTPServerDisconnected("connection dropped")
        return real_send(self, fail_silently=fail_silently)

    monkeypatch.setattr(EmailMultiAlternatives, "send", flaky)
    payer = make_user(_tag("em_flaky"))
    with django_capture_on_commit_callbacks(execute=True):
        _complete(_open_payment(payer))
    assert len(attempts) == 2 and len(mail.outbox) == 1
    (delivery,) = _deliveries(payer)
    assert (delivery.status, delivery.attempts, delivery.error) == ("sent", 2, "")


def test_a_refusal_no_retry_can_change_is_recorded_once(
        make_user, monkeypatch, worker_retries, django_capture_on_commit_callbacks):
    from django.core.mail import EmailMultiAlternatives

    attempts = []

    def refused(self, fail_silently=False):
        attempts.append(1)
        raise smtplib.SMTPRecipientsRefused({self.to[0]: (550, b"no such mailbox")})

    monkeypatch.setattr(EmailMultiAlternatives, "send", refused)
    payer = make_user(_tag("em_refused"))
    with django_capture_on_commit_callbacks(execute=True):
        payment = _complete(_open_payment(payer))
    assert payment.status == "completed", "an email must never cost the payment"
    assert len(attempts) == 1 and mail.outbox == []
    (delivery,) = _deliveries(payer)
    assert (delivery.status, delivery.error) == ("failed", "SMTPRecipientsRefused")


def test_the_sweep_publishes_an_email_whose_message_was_lost(
        make_user, monkeypatch, django_capture_on_commit_callbacks):
    from notifications.models import EmailDelivery
    from notifications.tasks import send_notification_email, sweep_unsent_emails

    def broker_down(*args, **kwargs):
        raise ConnectionError("broker unreachable")

    lost = make_user(_tag("em_lost"))
    with monkeypatch.context() as patch:
        patch.setattr(send_notification_email, "apply_async", broker_down)
        with django_capture_on_commit_callbacks(execute=True):
            payment = _complete(_open_payment(lost))
    assert payment.status == "completed" and mail.outbox == []
    (delivery,) = _deliveries(lost)
    assert delivery.status == "queued"

    # A worker that died after its claim leaves the row `sending`.
    dead = make_user(_tag("em_dead"))
    with monkeypatch.context() as patch:
        patch.setattr(send_notification_email, "apply_async", lambda *a, **k: None)
        with django_capture_on_commit_callbacks(execute=True):
            _complete(_open_payment(dead))
    EmailDelivery.objects.filter(recipient=dead).update(
        status="sending", attempts=1, last_attempt_at=timezone.now())

    # Too old to be worth sending.
    stale = make_user(_tag("em_stale"))
    old = EmailDelivery.objects.create(recipient=stale, kind="payment_receipt",
                                       dedup_key="payment_receipt:old", context={})
    EmailDelivery.objects.filter(pk=old.pk).update(
        created_at=timezone.now() - timedelta(days=4))

    assert sweep_unsent_emails() == {"republished": 0, "given_up": 1}, \
        "nothing is stale yet"
    counts = sweep_unsent_emails(now=timezone.now() + timedelta(hours=1))
    assert counts == {"republished": 2, "given_up": 0}
    assert sorted(m.to[0] for m in mail.outbox) == sorted([lost.email, dead.email])
    assert {d.status for d in EmailDelivery.objects.filter(recipient__in=[lost, dead])} \
        == {"sent"}
    assert (EmailDelivery.objects.get(pk=old.pk).status,
            EmailDelivery.objects.get(pk=old.pk).error) == ("failed", "gave_up")
    # Nothing left to do.
    assert sweep_unsent_emails(now=timezone.now() + timedelta(hours=2)) == \
        {"republished": 0, "given_up": 0}


def test_every_email_is_registered_personal_data_and_the_sweep_is_scheduled(settings):
    from training_platform.celery import app
    from training_platform.privacy.registry import registry

    source = registry["notifications.EmailDelivery"]
    assert (source.user_field, source.on_erase, source.retention_days) == \
        ("recipient", "delete", 90)
    assert "email" not in (source.fields or [])
    app.loader.import_default_modules()
    entry = settings.CELERY_BEAT_SCHEDULE["sweep-unsent-emails"]
    assert entry["task"] == "notifications.sweep_unsent_emails" and entry["task"] in app.tasks
    assert "notifications.send_email" in app.tasks
