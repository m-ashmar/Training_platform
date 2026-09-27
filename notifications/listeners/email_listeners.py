"""The receipt email on a confirmed payment (the payments spec, section 7: "Payment
confirmed — email: receipt attached").

Hung on `payment_completed`, the signal `PaymentService` sends AFTER the commit of a
completion (`subscription/services/payment_service.py`). It is not a domain event and it
writes no inbox row: this is the email alone (`notifications/channels/email.py`).

That signal is sent again for a payout still owed (`PaymentService.redeliver_payout`,
`subscription.settle_owed_payouts`), so the email is keyed on the payment — one receipt
email per payment, however often the signal fires — and none goes out for a payment
confirmed longer ago than `RECEIPT_EMAIL_MAX_AGE`.

NEVER raises. Its neighbour on this signal (`wallet.signals.handle_payment_completed`)
moves the trainer's money, and a receiver that raises stops the receivers after it and
fails the caller: an email must not cost a payout or a gateway's 200.
"""
import logging

from django.dispatch import receiver
from django.utils import timezone

from subscription.services.payment_service import payment_completed

logger = logging.getLogger(__name__)


@receiver(payment_completed, dispatch_uid="notifications.email_payment_receipt")
def email_payment_receipt(sender, payment, subscription=None, **kwargs):
    from notifications.channels import email
    from subscription.receipts import issued_at

    try:
        if payment.status != "completed":
            return None
        confirmed = issued_at(payment)
        if confirmed is not None and timezone.now() - confirmed > email.RECEIPT_EMAIL_MAX_AGE:
            return None
        payer = (subscription or payment.subscription).user
        return email.queue(payer, "payment_receipt", key=f"payment_receipt:{payment.pk}",
                           context={"payment_id": str(payment.pk)})
    except Exception:
        logger.exception("The receipt email for payment %s could not be queued",
                         getattr(payment, "pk", None))
        return None
