"""Scheduled work on user rows, and the account emails (sign-up and reset codes)."""
import logging
import smtplib

from celery import shared_task
from celery.exceptions import SoftTimeLimitExceeded
from django.db import InterfaceError, OperationalError

from training_platform.task_retries import no_retry_on_timeout

logger = logging.getLogger(__name__)


# =========================================================== the account emails
#: Seconds before each retry of an account email that did not go out: about seven and a
#: half minutes in all, inside the code's ten-minute life (`users.utils.
#: OTP_LIFETIME_MINUTES`) — a retry after that could only deliver a dead code.
EMAIL_RETRY_COUNTDOWNS = (10, 30, 60, 120, 240)

#: Failures another try can cure: the network and the mail server's temporary answers
#: (`smtplib` errors are OSErrors; a 5xx is taken out by `_is_permanent`), a send that ran
#: out of time, a database blip.
_TRANSIENT = (OSError, SoftTimeLimitExceeded, OperationalError, InterfaceError)


class AccountEmailUndelivered(Exception):
    """An account email was given up on — and already logged and alerted, once, with its
    real cause (`users.utils.report_account_email_failure`).

    Listed in the task's `throws`, so the worker records the task as failed without a
    second error line, and Sentry's Celery integration skips it: one failure, one alert.
    """


class _RowNotVisible(Exception):
    """The code's row cannot be seen: the transaction that queued it has not committed
    yet, or it rolled back, or the account was erased since."""


def _is_permanent(exc):
    """A refusal no retry can change: every recipient refused (no such mailbox), or a 5xx
    answer (authentication, sender, content). Reported at once instead of five times."""
    if isinstance(exc, smtplib.SMTPRecipientsRefused):
        return True
    return isinstance(exc, smtplib.SMTPResponseException) and 500 <= exc.smtp_code < 600


@shared_task(
    bind=True,
    name="users.send_account_code_email",
    max_retries=len(EMAIL_RETRY_COUNTDOWNS),
    acks_late=True,
    ignore_result=True,
    # A single SMTP operation is bounded by EMAIL_TIMEOUT (10 s); this bounds the whole
    # send, well under the 300 s every other task gets, so a hung one frees its slot.
    soft_time_limit=60,
    time_limit=90,
    throws=(AccountEmailUndelivered,),
)
def send_account_code_email(self, otp_id, sealed_code):
    """Email a sign-up or password-reset code — the worker's half of
    `users.utils.queue_otp_email`.

    `otp_id` is the `OTPVerification` row, `sealed_code` the code sealed for the broker
    (`users.utils.seal_otp_code`). Sends nothing when the seal is older than the code's
    lifetime, or the row has been replaced by a newer code, used or has expired: a retry
    can run minutes late, and an email with a dead code arriving after the live one reads
    as the live one.

    The email is written in the recipient's SAVED language (`preferred_language`), read
    now — so a switch between the request and this send counts.

    A transient failure is retried (`EMAIL_RETRY_COUNTDOWNS`). A permanent one, or the
    last retry's, is logged and alerted once (`report_account_email_failure`) and raises
    `AccountEmailUndelivered`. Returns "sent", "stale" (nothing to send) or "gone" (the
    row never appeared).
    """
    from django.utils import timezone

    from .models import OTPVerification
    from .utils import (open_otp_code, report_account_email_failure, send_otp_email,
                        send_password_reset_email)

    code = open_otp_code(sealed_code)
    if code is None:
        logger.warning("Account email not sent: its code expired before it could be "
                       "(otp=%s)", otp_id)
        return "stale"

    otp = None
    try:
        otp = OTPVerification.objects.select_related("user").filter(pk=otp_id).first()
        if otp is None:
            raise _RowNotVisible(otp_id)
        if otp.is_verified or otp.expires_at <= timezone.now():
            logger.info("Account email not sent: its code was replaced, used or expired "
                        "(otp=%s user=%s)", otp.pk, otp.user_id)
            return "stale"
        send = send_otp_email if otp.purpose == "registration" else send_password_reset_email
        send(otp.user, code)
        return "sent"
    except _RowNotVisible as exc:
        if self.request.retries >= self.max_retries:
            logger.warning("Account email not sent: its code's row never appeared "
                           "(otp=%s) — rolled back, or the account was erased", otp_id)
            return "gone"
        raise self.retry(exc=exc, countdown=EMAIL_RETRY_COUNTDOWNS[self.request.retries])
    except Exception as exc:
        purpose = getattr(otp, "purpose", "unknown")
        user_id = getattr(otp, "user_id", None)
        if (isinstance(exc, _TRANSIENT) and not _is_permanent(exc)
                and self.request.retries < self.max_retries):
            logger.warning("Account email failed, retrying: purpose=%s user=%s attempt=%s "
                           "(%s)", purpose, user_id, self.request.retries + 1,
                           type(exc).__name__)
            raise self.retry(exc=exc,
                             countdown=EMAIL_RETRY_COUNTDOWNS[self.request.retries])
        report_account_email_failure(purpose, user_id, exc, stage="send")
        raise AccountEmailUndelivered(
            f"{purpose} email for user {user_id} was not delivered") from exc


@shared_task(
    name="users.refresh_ages_from_birth_dates",
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_backoff_max=600,
    retry_jitter=True,
    max_retries=3,
)
@no_retry_on_timeout
def refresh_ages_from_birth_dates(after=None):
    """Bring `age` in line with `date_of_birth` for everyone whose age has moved.

    `age` is derived on every save, so a row goes stale only on a birthday nobody saved
    it on. Scans the rows that have a date of birth and writes only the ones that
    differ, so a missed night is caught up by the next run. Idempotent.

    A page of `PERIODIC_SCAN_SIZE` rows per invocation (the work per row is a
    comparison), the rest handed on to a fresh invocation (`training_platform.batching`):
    one scan of every user under the 300 s soft limit is the shape the
    production-readiness audit (2026-09-25) found in the daily jobs. Keyset pages, not
    `.iterator()`, whose server-side cursor does not survive a transaction-pooling
    connection pooler. Returns how many ages THIS invocation refreshed.
    """
    from training_platform.batching import Batch, scan_size

    from .models import CustomUser, age_on

    batch = Batch(size=scan_size())
    stale = []
    for user in batch.page(CustomUser.objects.filter(date_of_birth__isnull=False)
                           .only("id", "date_of_birth", "age"), after):
        if batch.spent():
            break
        batch.done(user.pk)
        age = age_on(user.date_of_birth)
        if user.age != age:
            user.age = age
            stale.append(user)
    CustomUser.objects.bulk_update(stale, ["age"], batch_size=500)
    if stale:
        logger.info("Refreshed age for %d users", len(stale))
    batch.hand_on(refresh_ages_from_birth_dates)
    return len(stale)


@shared_task(
    name="users.renew_coaching_periods",
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_backoff_max=600,
    retry_jitter=True,
    max_retries=3,
)
@no_retry_on_timeout
def renew_coaching_periods(phase="remind", after=None):
    """Remind 3 days ahead, charge every coaching month that has ended, freeze what the
    wallet cannot cover and keep reminding it.

    See users/coaching.py. Every step is idempotent, so a retried run charges nothing
    twice; a relation that failed is retried with the rest. Returns the renewal counts,
    plus `renewal_reminders` when any "renews soon" notice went out — this invocation's.

    In pages (`training_platform.batching`): at most `PERIODIC_BATCH_SIZE` relations an
    invocation — the reminders, then the on-time renewals, then the frozen ones with
    something to do — and the rest handed on to a fresh invocation with `phase` and the
    last relation id done. A month-end with thousands of renewals in one task under the
    300 s soft limit was cut off part-way, and the relations that sorted later waited an
    hour, or several (the production-readiness audit, 2026-09-25). The continuation is
    enqueued BEFORE a failure is raised for the retry, so one bad relation does not hold
    the rest back; the retry re-runs this page only.
    """
    from training_platform.batching import Batch

    from .coaching import remind_upcoming, renew_due

    batch = Batch()
    counts: dict = {}
    if phase == "remind":
        reminded = remind_upcoming(batch=batch, after=after)
        if reminded:
            counts['renewal_reminders'] = reminded
        if batch.hand_on(renew_coaching_periods, phase="remind"):
            logger.info("Coaching renewals: %s", counts)
            return counts
        phase, after = "on_time", None
    result = renew_due(batch=batch, phase=phase, after=after)
    counts = {**result['counts'], **counts}
    following = result.get('next')
    if following:
        renew_coaching_periods.apply_async(kwargs=following)
    if counts:
        logger.info("Coaching renewals: %s", counts)
    if result['failed']:
        raise RuntimeError(f"coaching renewal failed for relations {result['failed']}")
    return counts
