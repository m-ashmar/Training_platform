"""
notifications/channels/email.py — the email channel.

The payments spec, section 7, lists an email for five events, and only for these:

  | event                                   | email                                   |
  |-----------------------------------------|-----------------------------------------|
  | Payment confirmed                       | yes, with the PDF receipt attached      |
  | Plan expires in 7 days, 3 days, 1 day   | the 7-day notice only                   |
  | Plan expired                            | yes, once for each cover that ends      |
  | Withdrawal requested, paid, failed      | yes                                     |

Two ways in:

* An inbox notification whose `event_type` is in `NOTIFICATION_EMAILS`
  (`subscription_expired`, `withdrawal_requested`, `withdrawal_paid`,
  `withdrawal_failed`) also queues an email: `NotificationService._dispatch_channels`
  calls `from_notification`.
* Two emails have no inbox row of their own, and are queued where their event happens
  with `queue`: the receipt, from `payment_completed`
  (`notifications/listeners/email_listeners.py`), and the 7-day notice, from
  `subscription.send_renewal_reminders`.

Built on the account emails' plumbing (`users/tasks.py`, `users/utils.py`): the same
SMTP settings (`EMAIL_*`, `DEFAULT_FROM_EMAIL`), the same brand (`_brand`), the same
language rule — the recipient's SAVED `preferred_language`, read when the email is
written, not when it was queued — the same line between a failure worth retrying and a
refusal no retry changes (`_TRANSIENT`, `_is_permanent`), and the same single report of
an email given up on (`report_account_email_failure`).

What the request does, and what the worker does
-----------------------------------------------
`queue` writes one `EmailDelivery` row, keyed so the same email is never owed twice, and
publishes `notifications.send_email` AFTER the commit. Rendering (the PDF included) and
SMTP happen in the worker (`deliver`), never in a request. A transient failure is retried
with backoff (`notifications.tasks.EMAIL_RETRY_COUNTDOWNS`); a row whose message was lost
(the broker down at the commit, a worker killed mid-send) is picked up by
`notifications.sweep_unsent_emails`.

Who gets one
------------
Only an account that is active, has a VERIFIED address (`email_verified`, set by the
sign-up code) and has not been erased. And only when the person's notification
preferences let email through for that kind (`UserNotificationPreference.
is_channel_enabled(..., 'email')`: the `all` row, then the type's own row; `is_enabled:
false` stops push and email, `channels.email: false` stops email only). Both are asked
when the email is queued AND again when it is sent.

What an email may say
---------------------
Emails are not per build, so every one is written for the strictest build: the FACT and
nothing else. No price of anything to buy, no renew button, no link to pay, no link at
all (section 7). The receipt is a record of money already paid, as on the receipt screen
(`subscription.views.PaymentReceiptView`, identical on every build): its amount is on the
attached document, not in the email's text.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import timedelta
from math import ceil
from typing import Callable

from django.conf import settings
from django.db import transaction
from django.db.models import F
from django.utils import timezone
from django.utils.translation import get_language
from django.utils.translation import gettext as _
from django.utils.translation import ngettext

logger = logging.getLogger(__name__)

#: The address erasure writes over a person's own (`training_platform/privacy/
#: sources.py`). Such an account is inactive as well; this is the second lock.
ERASED_DOMAIN = "@invalid.local"

#: A receipt email goes out for a payment confirmed at most this long ago. The signal it
#: hangs on is sent again for a payout still owed (`PaymentService.redeliver_payout`,
#: `subscription.settle_owed_payouts`), which can be long after: "your payment was
#: confirmed" belongs to the confirmation, not to a payout sweep.
RECEIPT_EMAIL_MAX_AGE = timedelta(days=3)

#: Attempts an email gets in all — the task's retries, and the sweep's re-publishing of
#: a row whose message was lost — before it is recorded `failed` and reported.
EMAIL_MAX_ATTEMPTS = 8


class EmailUndelivered(Exception):
    """An email was given up on — already recorded (`EmailDelivery.status = failed`) and
    reported once with its real cause. Listed in the task's `throws`, so the worker does
    not log it a second time."""


class TransientEmailFailure(Exception):
    """This attempt failed for a reason another attempt can cure; the row is queued
    again and the task retries. The cause is chained."""


# --------------------------------------------------------------------- who gets one
def refusal(user, preference: str) -> str | None:
    """Why `user` must not be emailed about `preference`, or None when they may be.

    `inactive` (suspended or erased), `unverified` (the address was never confirmed by
    the sign-up code), `no_address`, `muted` (their notification preferences stop email
    for this type).
    """
    from notifications.models import UserNotificationPreference

    if user is None or not user.is_active:
        return "inactive"
    if not getattr(user, "email_verified", False):
        return "unverified"
    address = (user.email or "").strip()
    if not address or address.lower().endswith(ERASED_DOMAIN):
        return "no_address"
    if not UserNotificationPreference.is_channel_enabled(user, preference, "email"):
        return "muted"
    return None


# ------------------------------------------------------------------ what one says
@dataclass
class Composed:
    """One email, in the active language: a title (the subject, and the heading), the
    paragraphs, the facts as label/value rows, and the attachments."""
    title: str
    paragraphs: list[str]
    rows: list[tuple[str, str]] = field(default_factory=list)
    attachments: list[tuple[str, bytes, str]] = field(default_factory=list)


@dataclass(frozen=True)
class EmailKind:
    """`name` is `EmailDelivery.kind`. `preference` is the `event_type` whose
    notification preferences govern it. `compose(delivery)` writes it in the active
    language, or returns None when it is no longer true (the payment was refunded, the
    plan was renewed): nothing is sent then."""
    name: str
    preference: str
    compose: Callable


def _local_date(user, moment) -> str:
    """`moment` as the recipient's calendar date, in digits every script reads."""
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

    if moment is None:
        return ""
    try:
        zone = ZoneInfo(getattr(user, "preferred_timezone", "") or settings.TIME_ZONE)
    except (ZoneInfoNotFoundError, ValueError):
        zone = ZoneInfo("UTC")
    return timezone.localtime(moment, zone).strftime("%Y-%m-%d")


def _moment(value):
    from django.utils.dateparse import parse_datetime

    return parse_datetime(value) if isinstance(value, str) else value


def _money(amount, currency: str) -> str:
    from decimal import Decimal

    return f"{Decimal(amount):,.2f} {currency}"


def _notification_text(event_type: str, context: dict, recipient_id) -> tuple[str, str]:
    """The push's own title and body for `event_type`, in the active language — the one
    place a notification template is resolved (`NotificationTemplateResolver`). The
    email says what the push says, and adds the facts beneath it."""
    from notifications.channels.fcm import _resolve_event_template
    from notifications.template_resolver import NotificationTemplateResolver

    return NotificationTemplateResolver.render(
        event_type=event_type, template=_resolve_event_template(event_type),
        context=context, recipient_id=recipient_id)


def _compose_receipt(delivery) -> Composed | None:
    """Payment confirmed, with the receipt the payment history offers, as a PDF."""
    from subscription.models import Payment
    from subscription.receipts import receipt_data, render_pdf
    from subscription.services.payment_service import PaymentService

    payment = (Payment.objects
               .select_related("subscription", "subscription__plan", "subscription__user")
               .filter(pk=delivery.context.get("payment_id")).first())
    if (payment is None or payment.status != "completed"
            or payment.subscription.user_id != delivery.recipient_id):
        return None
    # The one generator of invoice numbers (see `PaymentReceiptView`).
    invoice = PaymentService.ensure_invoice_number(payment)
    data = receipt_data(payment, invoice_number=invoice)
    item = data["item"]
    what = (f"{item['plan_name']} · {item['duration_days']} " + _("days")
            if item.get("duration_days") else str(item["plan_name"]))
    return Composed(
        title=_("Payment confirmed"),
        paragraphs=[_("We received your payment for %(item)s. Your receipt is attached "
                      "to this email.") % {"item": what}],
        rows=[(_("Invoice number"), invoice),
              (_("Date"), _local_date(delivery.recipient, data["issued_at"])),
              (_("Item"), what)],
        attachments=[(f"{invoice}.pdf", render_pdf(data), "application/pdf")],
    )


def _compose_expiring(delivery) -> Composed | None:
    """The plan ends in about a week (the 7-day notice). Nothing when it was renewed or
    ended since: the row's `ends_at` is no longer the end."""
    from subscription.models import Subscription

    sub = Subscription.objects.filter(pk=delivery.context.get("subscription_id")).first()
    end = _moment(delivery.context.get("ends_at"))
    now = timezone.now()
    if sub is None or end is None or not sub.is_active or sub.effective_end != end \
            or end <= now:
        return None
    days = max(1, ceil((end - now) / timedelta(days=1)))
    return Composed(
        title=_("Your subscription ends soon"),
        paragraphs=[
            ngettext("Your subscription ends in %(days)s day.",
                     "Your subscription ends in %(days)s days.", days) % {"days": days},
            _("When it ends, your diet plan and routine stay saved and you can still see "
              "them. Using them needs an active subscription."),
        ],
        rows=[(_("Ends on"), _local_date(delivery.recipient, end))],
    )


def _compose_expired(delivery) -> Composed | None:
    """The plan ended. Nothing when it is active again by the time this is sent."""
    from subscription.models import Subscription

    sub = Subscription.objects.filter(pk=delivery.context.get("subscription_id")).first()
    if sub is None or sub.is_active:
        return None
    title, body = _notification_text("subscription_expired", {}, delivery.recipient_id)
    ended = _moment(delivery.context.get("ended_at"))
    return Composed(title=title, paragraphs=[body],
                    rows=[(_("Ended on"), _local_date(delivery.recipient, ended))])


def _withdrawal_composer(event_type: str):
    def compose(delivery) -> Composed | None:
        from payouts.models import Withdrawal

        withdrawal = Withdrawal.objects.filter(
            pk=delivery.context.get("withdrawal_id")).first()
        if withdrawal is None or withdrawal.trainer_id != delivery.recipient_id:
            return None
        context = {"number": withdrawal.number,
                   "reason": withdrawal.failure_reason or delivery.context.get("reason", "")}
        title, body = _notification_text(event_type, context, delivery.recipient_id)
        return Composed(title=title, paragraphs=[body],
                        rows=[(_("Withdrawal number"), withdrawal.number),
                              (_("Amount"), _money(withdrawal.amount, withdrawal.currency))])
    return compose


#: Every email this channel can send. The `preference` of the receipt is
#: `payment_confirmed`, the spec's name for the event: until a push of that type exists,
#: only the `all` preference row reaches it.
KINDS = {kind.name: kind for kind in (
    EmailKind("payment_receipt", "payment_confirmed", _compose_receipt),
    EmailKind("subscription_expiring", "subscription_expiring", _compose_expiring),
    EmailKind("subscription_expired", "subscription_expired", _compose_expired),
    EmailKind("withdrawal_requested", "withdrawal_requested",
              _withdrawal_composer("withdrawal_requested")),
    EmailKind("withdrawal_paid", "withdrawal_paid", _withdrawal_composer("withdrawal_paid")),
    EmailKind("withdrawal_failed", "withdrawal_failed",
              _withdrawal_composer("withdrawal_failed")),
)}


def email_preference_types() -> list[str]:
    """The `event_type`s a preference can set `channels.email` on to any effect."""
    return sorted({kind.preference for kind in KINDS.values()})


# -------------------------------------------------------------- the inbox's emails
def _expired_key(notification):
    """Once for each cover that ends, with its FIRST lapsed notice: the reminder repeats
    on days 0, 3, 7, 14 and 30 (`SUBSCRIPTION_LAPSED_REMINDER_DAYS`) and the email does
    not. A cover whose first notice went out before this channel existed gets none."""
    from notifications.models import Notification

    data = (notification.metadata or {}).get("data") or {}
    sub_id, ended_at = data.get("subscription_id"), data.get("ended_at")
    if not sub_id or not ended_at:
        return None, {}
    earlier = (Notification.objects
               .filter(recipient_id=notification.recipient_id,
                       event_type="subscription_expired",
                       related_object_id__startswith=f"{sub_id}:{ended_at}:",
                       created_at__lte=notification.created_at)
               .exclude(pk=notification.pk).exists())
    if earlier:
        return None, {}
    return (f"subscription_expired:{sub_id}:{ended_at}",
            {"subscription_id": sub_id, "ended_at": ended_at})


def _withdrawal_key(notification):
    """Once per withdrawal and step, whatever number of times its event was emitted."""
    data = (notification.metadata or {}).get("data") or {}
    withdrawal_id = data.get("withdrawal_id") or notification.related_object_id
    if not withdrawal_id:
        return None, {}
    context = {"withdrawal_id": str(withdrawal_id), "number": data.get("number", "")}
    if data.get("reason"):
        context["reason"] = data["reason"]
    return f"{notification.event_type}:{withdrawal_id}", context


#: `event_type` of an inbox notification -> how its email is keyed. The kind has the
#: same name as the event type.
NOTIFICATION_EMAILS = {
    "subscription_expired": _expired_key,
    "withdrawal_requested": _withdrawal_key,
    "withdrawal_paid": _withdrawal_key,
    "withdrawal_failed": _withdrawal_key,
}


def from_notification(notification):
    """Queue the email that goes with `notification`, when its type has one. Returns
    the `EmailDelivery`, or None. Safe to call again for the same row (the FCM retry
    drain does): the key finds the first one."""
    keyer = NOTIFICATION_EMAILS.get(notification.event_type)
    if keyer is None:
        return None
    key, context = keyer(notification)
    if key is None:
        return None
    return queue(notification.recipient, notification.event_type, key=key,
                 context=context, notification=notification)


# ---------------------------------------------------------------------- the queue
def queue(recipient, kind: str, *, key: str, context: dict, notification=None):
    """Owe `recipient` the `kind` email once for `key`, and hand it to the worker after
    the commit. Returns the `EmailDelivery` (the existing one when `key` was already
    owed), or None when this person is not to be emailed (`refusal`)."""
    from notifications.models import EmailDelivery

    spec = KINDS[kind]
    reason = refusal(recipient, spec.preference)
    if reason is not None:
        logger.debug("No %s email for user %s: %s", kind, getattr(recipient, "pk", None),
                     reason)
        return None
    delivery, created = EmailDelivery.objects.get_or_create(
        recipient=recipient, dedup_key=key[:255],
        defaults={"kind": kind, "context": context, "notification": notification})
    if created:
        # Robust: a broker that cannot be reached must not fail the caller's work — the
        # payment, the withdrawal, the reminder. The row stays `queued` and the sweep
        # publishes it again.
        transaction.on_commit(lambda: publish(delivery.pk), robust=True)
    return delivery


def publish(delivery_id) -> bool:
    """Hand one queued email to the worker. Never raises: a message that could not be
    published leaves its row `queued` for `sweep_unsent_emails`."""
    from celery.exceptions import Retry

    from notifications.tasks import send_notification_email

    try:
        send_notification_email.apply_async(
            args=(delivery_id,), retry=True,
            retry_policy={"max_retries": 1, "interval_start": 0, "interval_step": 1,
                          "interval_max": 1})
    except (EmailUndelivered, Retry):
        # Eager mode (development, tests) runs the task inline; its final failure, or a
        # retry it cannot schedule, comes back here — already recorded by the task.
        return False
    except Exception:
        logger.exception("Email %s could not be queued; the sweep publishes it again",
                         delivery_id)
        return False
    return True


# --------------------------------------------------------------------- the worker
def _finish(delivery_id, status, **fields):
    from notifications.models import EmailDelivery

    EmailDelivery.objects.filter(pk=delivery_id).update(status=status, **fields)


def deliver(delivery_id, *, last_try: bool = False) -> str:
    """Send one owed email, once. The worker's half of `queue`.

    Claims the row (`queued` -> `sending`) with a conditional UPDATE, so a message
    delivered twice, or a retry racing the sweep, sends it once. Then asks again whether
    this person may be emailed, writes it in their saved language, and sends it.

    Returns `sent`, `skipped`, or the row's state when it was not `queued` (`sent`,
    `sending`, ... — nothing to do) or `gone` (no such row: the account was erased).
    Raises `TransientEmailFailure` for another try (the row is `queued` again), and
    `EmailUndelivered` when it is given up on (`failed`, reported once).
    """
    from notifications.models import EmailDelivery
    from training_platform.i18n import LanguageContext
    from users.tasks import _TRANSIENT, _is_permanent
    from users.utils import report_account_email_failure

    now = timezone.now()
    claimed = EmailDelivery.objects.filter(pk=delivery_id, status=EmailDelivery.QUEUED) \
        .update(status=EmailDelivery.SENDING, attempts=F("attempts") + 1,
                last_attempt_at=now)
    if not claimed:
        return (EmailDelivery.objects.filter(pk=delivery_id)
                .values_list("status", flat=True).first() or "gone")

    delivery = EmailDelivery.objects.select_related("recipient").get(pk=delivery_id)
    last_try = last_try or delivery.attempts >= EMAIL_MAX_ATTEMPTS
    spec = KINDS.get(delivery.kind)
    if spec is None:
        _finish(delivery_id, EmailDelivery.SKIPPED, error="unknown_kind")
        return "skipped"
    recipient = delivery.recipient
    reason = refusal(recipient, spec.preference)
    if reason is not None:
        _finish(delivery_id, EmailDelivery.SKIPPED, error=reason)
        return "skipped"

    try:
        with LanguageContext.for_user(recipient):
            language = get_language()
            composed = spec.compose(delivery)
            if composed is None:
                _finish(delivery_id, EmailDelivery.SKIPPED, error="stale")
                return "skipped"
            render(recipient, composed).send(fail_silently=False)
    except Exception as exc:
        if isinstance(exc, _TRANSIENT) and not _is_permanent(exc) and not last_try:
            logger.warning("Email %s (%s) failed, retrying: user=%s attempt=%s (%s)",
                           delivery_id, delivery.kind, recipient.pk, delivery.attempts + 1,
                           type(exc).__name__)
            _finish(delivery_id, EmailDelivery.QUEUED, error=type(exc).__name__[:255])
            raise TransientEmailFailure(str(delivery_id)) from exc
        _finish(delivery_id, EmailDelivery.FAILED, error=type(exc).__name__[:255])
        report_account_email_failure(f"notification:{delivery.kind}", recipient.pk, exc,
                                     stage="send")
        raise EmailUndelivered(f"{delivery.kind} email {delivery_id} was not delivered") \
            from exc
    _finish(delivery_id, EmailDelivery.SENT, sent_at=timezone.now(), language=language,
            error="")
    logger.info("Email %s (%s) sent for user %s in %s", delivery_id, delivery.kind,
                recipient.pk, language)
    return "sent"


def render(recipient, composed: Composed):
    """The message: a plain-text part and an HTML part (`templates/emails/
    notification.html`) of the same words, in the ACTIVE language, with the
    attachments. The subject is the title and the brand, as the account emails'."""
    from django.core.mail import EmailMultiAlternatives
    from django.template.loader import render_to_string
    from django.utils.translation import get_language_bidi

    from users.utils import _brand

    brand = _brand()
    name = (recipient.first_name or "").strip()
    # The punctuation is the language's own: "Hello Sara," / "مرحبًا سارة،".
    greeting = (_("Hello %(name)s,") % {"name": name}) if name else _("Hello,")
    footer = [_("You can choose which emails you receive in the app's notification "
                "settings."),
              _("This is an automated message. Please do not reply to this email.")]

    text = [greeting, "", composed.title, ""]
    for paragraph in composed.paragraphs:
        text += [paragraph, ""]
    for label, value in composed.rows:
        if str(value):
            text.append(f"{label}: {value}")
    text += ["", *footer, "", brand]

    html = render_to_string("emails/notification.html", {
        "brand": brand, "greeting": greeting, "title": composed.title,
        "paragraphs": composed.paragraphs,
        "rows": [(label, value) for label, value in composed.rows if str(value)],
        "footer": footer, "LANGUAGE_CODE": get_language(),
        "LANGUAGE_BIDI": get_language_bidi(),
    })
    message = EmailMultiAlternatives(
        subject=f"{composed.title} - {brand}", body="\n".join(text),
        from_email=settings.DEFAULT_FROM_EMAIL, to=[recipient.email])
    message.attach_alternative(html, "text/html")
    for filename, content, mimetype in composed.attachments:
        message.attach(filename, content, mimetype)
    return message
