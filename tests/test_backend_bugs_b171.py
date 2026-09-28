"""BACKEND_BUGS B-171: the receipt says whether its email left.

The receipt answered `payer.email` and nothing about the confirmation email carrying
the PDF, which the channel skips for an unverified or erased address, for a customer who
muted `payment_confirmed` email, and which may still be queued, have failed, or (for a
routine product's checkout, B-167) never have been owed. The app could only guess before
saying «وصلك الإيصال بالبريد». The receipt now answers `email: {status, sent_at}`, read
from the one `EmailDelivery` the receipt email is owed under.

On 554c604 the `email` key is absent.
"""
import pytest

from tests.test_email_channel import _complete, _open_payment, _tag, _work

pytestmark = pytest.mark.django_db

RECEIPT = "/api/subscription/v1/payments/{}/receipt/"


def _email(api, user, payment, **headers):
    resp = api(user).get(RECEIPT.format(payment.pk), **headers)
    assert resp.status_code == 200, resp.content
    return resp.json()["receipt"]["email"]


def test_b171_the_receipt_says_whether_its_email_left(
        make_user, api, django_capture_on_commit_callbacks):
    from notifications.models import EmailDelivery, UserNotificationPreference

    payer = make_user(_tag("b171_sent"))
    with django_capture_on_commit_callbacks(execute=True):
        payment = _complete(_open_payment(payer))
    delivery = EmailDelivery.objects.get(recipient=payer, kind="payment_receipt")
    email = _email(api, payer, payment)
    assert email["status"] == "sent" and delivery.sent_at is not None
    assert email["sent_at"] is not None
    # The same on every build: a record, not a sale.
    for channel in ("play", "ios", "web", ""):
        assert _email(api, payer, payment, HTTP_X_APP_CHANNEL=channel) == email, channel

    muted = make_user(_tag("b171_muted"))
    UserNotificationPreference.objects.create(user=muted, event_type="payment_confirmed",
                                              channels={"email": False})
    with django_capture_on_commit_callbacks(execute=True):
        unsent = _complete(_open_payment(muted))
    assert _email(api, muted, unsent) == {"status": "none", "sent_at": None}


def test_b171_queued_skipped_and_failed_are_told_apart(
        make_user, api, monkeypatch, django_capture_on_commit_callbacks):
    from notifications.models import EmailDelivery, UserNotificationPreference
    from notifications.tasks import send_notification_email

    calls = []
    monkeypatch.setattr(send_notification_email, "apply_async",
                        lambda args=None, kwargs=None, **options: calls.append(tuple(args)))

    waiting = make_user(_tag("b171_wait"))
    with django_capture_on_commit_callbacks(execute=True):
        queued = _complete(_open_payment(waiting))
    assert _email(api, waiting, queued) == {"status": "queued", "sent_at": None}

    # Muted after it was queued: the worker skips it.
    UserNotificationPreference.objects.create(user=waiting, event_type="payment_confirmed",
                                              is_enabled=False)
    assert _work(calls[-1]) == "skipped"
    assert _email(api, waiting, queued) == {"status": "skipped", "sent_at": None}

    refused = make_user(_tag("b171_fail"))
    with django_capture_on_commit_callbacks(execute=True):
        failed = _complete(_open_payment(refused))
    EmailDelivery.objects.filter(recipient=refused).update(status="failed")
    assert _email(api, refused, failed) == {"status": "failed", "sent_at": None}


def test_b171_the_pdf_is_the_document_and_carries_no_email_status(
        make_user, api, django_capture_on_commit_callbacks):
    payer = make_user(_tag("b171_pdf"))
    with django_capture_on_commit_callbacks(execute=True):
        payment = _complete(_open_payment(payer))
    pdf = api(payer).get(RECEIPT.format(payment.pk) + "?format=pdf")
    assert pdf.status_code == 200 and pdf["Content-Type"] == "application/pdf"
