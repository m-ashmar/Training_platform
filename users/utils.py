import hashlib
import logging
import secrets
from django.conf import settings
from django.utils import timezone
from datetime import timedelta
from django.core.mail import send_mail
from django.db import InterfaceError, OperationalError
from django.template.loader import render_to_string
from django.utils.translation import gettext_lazy as _lazy

#: What the caller is told when a code does not match. Translatable, because this API
#: serves English and Arabic and these were the last auth messages hardcoded in English.
_INVALID_OTP = _lazy("Invalid or expired code. Please request a new one.")
_OTP_EXPIRED = _lazy("This code has expired. Please request a new one.")
_OTP_LOCKED_OUT = _lazy("Too many failed attempts. Please try again in 15 minutes.")

logger = logging.getLogger(__name__)

def send_push_notification(user, title, message, data=None):
    """
    Send push notification to user's devices via FCM.
    
    Args:
        user: CustomUser instance
        title: Notification title
        message: Notification message
        data: Optional data payload
    
    Returns:
        bool: True if notification was sent successfully, False otherwise
    """
    from .models import DeviceToken
    from social.firebase_service import FirebaseNotificationService
    
    try:
        # Only send to ACTIVE tokens — inactive rows are soft-deleted invalid
        # tokens; sending to them wastes FCM quota and returns errors.
        tokens = list(
            DeviceToken.objects.filter(user=user, is_active=True).values_list('token', flat=True)
        )

        if not tokens:
            logger.info(f"No device tokens found for user {user.id}")
            return False
            
        # Use the singleton service
        service = FirebaseNotificationService()
        
        success_count = service.send_multicast(
            tokens=tokens,
            title=title,
            body=message,
            data=data
        )
        
        logger.info(f"Push notification sent to user {user.id}: {title} - {message} ({success_count}/{len(tokens)} successful)")
        return success_count > 0
        
    except Exception as e:
        logger.error(f"Error sending push notification to user {user.id}: {str(e)}")
        return False

def generate_otp():
    """
    Generate a cryptographically secure random 6-digit OTP code.
    Uses secrets module (CSPRNG) instead of random (Mersenne Twister).
    
    Returns:
        str: 6-digit OTP code
    """
    return str(secrets.randbelow(900000) + 100000)


#: Arabic-Indic and Extended Arabic-Indic (Persian) digits -> 0-9. An Arabic keyboard
#: types these, and a code or phone number is the same number either way.
_EASTERN_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789")


def western_digits(value):
    """`value` with every Arabic-Indic digit written as 0-9 (None stays None)."""
    return value.translate(_EASTERN_DIGITS) if isinstance(value, str) else value


def _hash_otp(otp_code: str) -> str:
    """Keyed hash of an OTP.

    Digits are made Western first: "١٢٣٤٥٦" typed on an Arabic keyboard is the code
    123456, and hashing it as typed failed the comparison and burned an attempt
    (BACKEND_BUGS.md B-015).

    A bare sha256 over a 6-digit code is reversible instantly from a database dump —
    the whole domain is 10^6 values, so a precomputed table breaks every stored code.
    HMAC with SECRET_KEY means a dump alone is not enough.
    """
    import hmac

    from django.conf import settings

    return hmac.new(
        settings.SECRET_KEY.encode('utf-8'),
        western_digits(otp_code or '').strip().encode('utf-8'),
        hashlib.sha256,
    ).hexdigest()

#: How long a sign-up or reset code is good for. The rule itself is unchanged (10
#: minutes, API_CONTRACT.md §1); it is named because the email's seal expires with it.
OTP_LIFETIME_MINUTES = 10


def _otp_seal():
    """The cipher a code is sealed with between the request and the worker.

    The code has to reach the worker that emails it, through the broker — a Redis whose
    keys any operator, backup or `MONITOR` can read. The row keeps only a keyed hash, so
    the plain code would otherwise exist nowhere but there. Fernet (authenticated,
    timestamped) under a key derived from SECRET_KEY, which the web and worker processes
    share and the broker never sees; SECRET_KEY_FALLBACKS still open a seal made just
    before a key rotation.
    """
    import base64

    from cryptography.fernet import Fernet, MultiFernet

    keys = [settings.SECRET_KEY, *(getattr(settings, "SECRET_KEY_FALLBACKS", None) or [])]
    return MultiFernet([
        Fernet(base64.urlsafe_b64encode(
            hashlib.sha256(b"users.otp-email-seal:" + key.encode("utf-8")).digest()))
        for key in keys if key
    ])


def seal_otp_code(otp_code):
    """`otp_code` sealed for the broker (see `_otp_seal`). A new token every call."""
    return _otp_seal().encrypt(str(otp_code).encode("utf-8")).decode("ascii")


def open_otp_code(sealed):
    """The code inside `sealed`, or None when it is forged, damaged or older than the
    code's own lifetime — a seal copied out of the broker later opens to nothing."""
    from cryptography.fernet import InvalidToken

    try:
        return _otp_seal().decrypt(str(sealed).encode("ascii"),
                                   ttl=OTP_LIFETIME_MINUTES * 60).decode("utf-8")
    except (InvalidToken, UnicodeError, TypeError, ValueError):
        return None


def _brand():
    """The name the account emails go out under, in the active language: "Yalla Gym" /
    "يلا جيم", or PLATFORM_NAME verbatim — the same rule as the receipt
    (`subscription.receipts`). They said "Training Platform", which is no product."""
    from django.utils.translation import gettext as _

    return str(getattr(settings, "PLATFORM_NAME", "") or _("Yalla Gym"))


def _send_code_email(user, *, subject, plain_message, template, otp_code):
    """Render `template` in the ACTIVE language and send it with `plain_message`.

    Raises when the send fails — the worker's task retries it and reports the last
    failure (users/tasks.py). This used to log a line and `return False`, and the caller
    never looked, so a sign-up whose code never left looked exactly like one that did.
    """
    from django.utils.translation import get_language, get_language_bidi

    try:
        html_message = render_to_string(template, {
            'user': user,
            'otp_code': otp_code,
            'brand': _brand(),
            'LANGUAGE_CODE': get_language(),
            'LANGUAGE_BIDI': get_language_bidi(),
        })
    except Exception:
        # A broken template must not cost the person their code: the plain text part
        # carries it on its own.
        logger.warning("Account email template %s did not render; sending plain text",
                       template, exc_info=True)
        html_message = None
    send_mail(
        subject=subject,
        message=plain_message,
        from_email=settings.DEFAULT_FROM_EMAIL,
        recipient_list=[user.email],
        html_message=html_message,
        fail_silently=False,
    )
    return True


def send_otp_email(user, otp_code):
    """Email the sign-up code, in the recipient's SAVED language (`preferred_language`).

    Not the request's language and not the server's default: this runs in the worker,
    where there is no request, and the person reads it in the language they chose —
    saved at sign-up (users/serializers.py `CustomRegisterSerializer`) and switchable on
    `user/update/`. Returns True; RAISES when the send fails (see `_send_code_email`).
    """
    from training_platform.i18n import LanguageContext
    from django.utils.translation import gettext as _

    with LanguageContext.for_user(user):
        _send_code_email(
            user,
            subject=_('Verify your email - %(brand)s') % {'brand': _brand()},
            plain_message=_(
                "Your OTP verification code is: %(code)s\n\nThis code will expire in 10 "
                "minutes.\n\nIf you didn't request this code, please ignore this email."
            ) % {"code": otp_code},
            template='emails/otp_verification.html',
            otp_code=otp_code,
        )
    logger.info("Sign-up code email sent for user %s", user.pk)
    return True


def queue_otp_email(otp, otp_code):
    """Hand the email carrying `otp_code` to the worker; never raise into the request.

    The request used to send it itself, through SMTP with no timeout: a stalled mail
    server held the sign-up request open, and a failure was swallowed. Now the request
    only publishes a task (bounded: one retry, and the broker connect timeout in
    settings_base) and answers exactly as before. The code travels SEALED
    (`seal_otp_code`) and the row by id, so the worker can tell a replaced or expired
    code and send nothing.

    No `transaction.on_commit`: every caller runs in autocommit (there is no
    ATOMIC_REQUESTS), so the row is committed when this runs, and the task retries a row
    it cannot see yet for any future caller that is not.

    A task that could not be PUBLISHED is logged and alerted here (the person can ask for
    a new code); a send that fails in the worker is retried and reported there.
    """
    from celery.exceptions import Retry

    from .tasks import AccountEmailUndelivered, send_account_code_email

    try:
        send_account_code_email.apply_async(
            args=(otp.pk, seal_otp_code(otp_code)),
            retry=True,
            retry_policy={"max_retries": 1, "interval_start": 0, "interval_step": 1,
                          "interval_max": 1},
        )
    except AccountEmailUndelivered:
        # Eager mode (development, tests) runs the task inline, and its final failure
        # propagates here — already logged and alerted by the task itself.
        return False
    except Retry as retry:
        # Eager mode with task_eager_propagates (settings_base) hands a retry back to the
        # caller instead of running it — there is no broker to re-deliver through. The
        # send failed and will not be tried again: report it as the worker would after
        # its last retry. A real worker never gets here.
        report_account_email_failure(otp.purpose, otp.user_id, retry.exc or retry,
                                     stage="send")
        return False
    except Exception as exc:
        report_account_email_failure(otp.purpose, otp.user_id, exc, stage="queue")
        return False
    logger.info("Account email queued: purpose=%s user=%s otp=%s",
                otp.purpose, otp.user_id, otp.pk)
    return True


def report_account_email_failure(purpose, user_id, exc, *, stage):
    """Log and alert an account email that did not go out: ONE error line, naming the
    account by id — never its address — and one Sentry event when Sentry is configured.

    Sentry's logging integration turns this error record into an event, and the explicit
    capture below is dropped as a duplicate of it (same exception object); the explicit
    one is there so the alert does not depend on that integration staying enabled.
    """
    logger.error("An account email was not delivered: purpose=%s user=%s stage=%s (%s)",
                 purpose, user_id, stage, type(exc).__name__,
                 exc_info=(type(exc), exc, exc.__traceback__))
    try:
        import sentry_sdk

        sentry_sdk.capture_exception(exc)
    except Exception:
        logger.debug("Sentry capture failed", exc_info=True)


def _issue_otp(user, purpose):
    """Replace `user`'s pending `purpose` code with a new one, and queue its email.

    The rules are unchanged: any pending code of that purpose is retired (marked used),
    the new one is six digits from the CSPRNG, stored only as a keyed hash, good for
    `OTP_LIFETIME_MINUTES`. What changed is the email: the request no longer sends it —
    it is queued for the worker (`queue_otp_email`), which sends it in the recipient's
    saved language, retries a failed send, and reports one that never leaves.
    """
    from .models import OTPVerification

    OTPVerification.objects.filter(
        user=user,
        is_verified=False,
        purpose=purpose,
        expires_at__gt=timezone.now()
    ).update(is_verified=True)  # Mark as used to invalidate

    otp_code = generate_otp()
    otp = OTPVerification.objects.create(
        user=user,
        otp_code=_hash_otp(otp_code),  # keyed HMAC-SHA256, never the plaintext code
        email=user.email,
        purpose=purpose,
        expires_at=timezone.now() + timedelta(minutes=OTP_LIFETIME_MINUTES),
    )
    queue_otp_email(otp, otp_code)
    logger.info("OTP created: purpose=%s user=%s", purpose, user.pk)
    return otp


def create_otp(user):
    """Issue a new sign-up (`registration`) code for `user` and queue its email.

    Returns the `OTPVerification` row. See `_issue_otp`.
    """
    return _issue_otp(user, 'registration')


def create_password_reset_otp(user):
    """Issue a new `password_reset` code for `user` and queue its email.

    Returns the `OTPVerification` row. See `_issue_otp`.
    """
    return _issue_otp(user, 'password_reset')


def send_password_reset_email(user, otp_code):
    """Email the password-reset code, in the recipient's SAVED language.

    Whatever language the reset was REQUESTED in: the person reads the email in the one
    they chose (see `send_otp_email`). Returns True; RAISES when the send fails.
    """
    from training_platform.i18n import LanguageContext
    from django.utils.translation import gettext as _

    with LanguageContext.for_user(user):
        _send_code_email(
            user,
            subject=_('Password reset - %(brand)s') % {'brand': _brand()},
            plain_message=_(
                "Your password reset code is: %(code)s\n\n"
                "This code will expire in 10 minutes.\n\n"
                "If you didn't request a password reset, please ignore this email."
            ) % {"code": otp_code},
            template='emails/password_reset_otp.html',
            otp_code=otp_code,
        )
    logger.info("Password reset email sent for user %s", user.pk)
    return True


def _get_otp_attempt_cache_key(email: str, purpose: str) -> str:
    """Cache key for OTP attempt tracking (hashed email — no PII in keys).

    The login counter below already hashed it; this one carried the address in clear, so
    every Redis key listing, slow-log line and monitoring sample named who was verifying.
    """
    email_hash = hashlib.sha256((email or '').strip().lower().encode('utf-8')).hexdigest()[:32]
    return f"otp_attempts:{purpose}:{email_hash}"


def _check_otp_lockout(email: str, purpose: str) -> tuple:
    """
    Check if the user is locked out from OTP verification.
    Returns (is_locked_out: bool, attempts_remaining: int).
    Uses ratelimit_cache (DB1) — isolated from session store.
    """
    from training_platform.cache import ratelimit_cache

    cache_key = _get_otp_attempt_cache_key(email, purpose)
    attempts = ratelimit_cache().get(cache_key, 0)
    max_attempts = 5

    if attempts >= max_attempts:
        return True, 0
    return False, max_attempts - attempts


def _record_otp_failure(email: str, purpose: str) -> int:
    """
    Record a failed OTP attempt. Returns the new total attempts count.
    Lockout window: 15 minutes.
    Uses ratelimit_cache (DB1) — not flushable via app cache clear.
    """
    from training_platform.cache import ratelimit_cache

    cache_key = _get_otp_attempt_cache_key(email, purpose)
    rl = ratelimit_cache()
    lockout_seconds = 15 * 60  # 15 minutes

    try:
        attempts = rl.incr(cache_key)
    except ValueError:
        rl.set(cache_key, 1, lockout_seconds)
        attempts = 1

    return attempts


def _clear_otp_attempts(email: str, purpose: str):
    """Clear OTP attempt counter after successful verification."""
    from training_platform.cache import ratelimit_cache
    ratelimit_cache().delete(_get_otp_attempt_cache_key(email, purpose))


def _get_login_attempt_cache_key(email: str) -> str:
    """Cache key for login brute-force tracking (hashed email — no PII in keys)."""
    email_hash = hashlib.sha256(email.strip().lower().encode('utf-8')).hexdigest()[:32]
    return f"login_attempts:{email_hash}"


def check_login_lockout(email: str) -> tuple:
    """
    Return (is_locked_out: bool, attempts_remaining: int) for login.
    Lockout after 5 failed attempts within the cooldown window — matches the
    project auth standard (5-attempt lockout, 15-minute cooldown).
    Uses ratelimit_cache (DB1) — isolated from session store.
    """
    from training_platform.cache import ratelimit_cache
    attempts = ratelimit_cache().get(_get_login_attempt_cache_key(email), 0)
    max_attempts = 5
    if attempts >= max_attempts:
        return True, 0
    return False, max_attempts - attempts


def record_login_failure(email: str) -> int:
    """Record a failed login attempt (15-min cooldown). Returns new total count."""
    from training_platform.cache import ratelimit_cache
    key = _get_login_attempt_cache_key(email)
    rl = ratelimit_cache()
    lockout_seconds = 15 * 60
    try:
        attempts = rl.incr(key)
    except ValueError:
        rl.set(key, 1, lockout_seconds)
        attempts = 1
    return attempts


def lockout_seconds_left(email: str) -> int:
    """Seconds until a locked address may try again (the whole window if unknown)."""
    from training_platform.cache import ratelimit_cache
    try:
        ttl = ratelimit_cache().ttl(_get_login_attempt_cache_key(email))
    except Exception:
        ttl = None          # a backend without `ttl` (the in-memory one in tests)
    return int(ttl) if isinstance(ttl, (int, float)) and ttl > 0 else 15 * 60


def clear_login_attempts(email: str):
    """Clear the login attempt counter after a successful login."""
    from training_platform.cache import ratelimit_cache
    ratelimit_cache().delete(_get_login_attempt_cache_key(email))


def find_by_email(email, **filters):
    """The account a typed address names, or None (BACKEND_BUGS B-033).

    Addresses are stored lower-cased, so the lower-cased address is tried first, then the
    address exactly as typed (an operator-made account keeps its case). Never `iexact`:
    Postgres folds `ſ` to `S` and `ı` to `I` where Python's `lower()` does not, so a
    lookalike reached the real account under a different lockout and rate-limit key.
    """
    from .models import CustomUser

    typed = (email or '').strip() if isinstance(email, str) else ''
    if not typed:
        return None
    for candidate in dict.fromkeys((typed.lower(), typed)):
        try:
            return CustomUser.objects.get(email=candidate, **filters)
        except CustomUser.DoesNotExist:
            continue
    return None


def otp_lockout_seconds_left(email: str, purpose: str) -> int:
    """Seconds until a locked address may try a code again (the whole window if unknown)."""
    from training_platform.cache import ratelimit_cache
    try:
        ttl = ratelimit_cache().ttl(_get_otp_attempt_cache_key(email, purpose))
    except Exception:
        ttl = None          # a backend without `ttl` (the in-memory one in tests)
    return int(ttl) if isinstance(ttl, (int, float)) and ttl > 0 else 15 * 60


def verify_otp(email, otp_code, purpose='registration'):
    """`(success, otp, message)`; see `verify_otp_detailed` for the reason's code."""
    result = verify_otp_detailed(email, otp_code, purpose)
    return result[0], result[1], result[2]


def otp_refusal_response(message, code, retry_after):
    """The 400 a verify endpoint answers for a refused code (BACKEND_BUGS B-021):
    `{error, detail, code}` with `code` one of `otp_invalid` (wrong code, or no such
    pending code — the same answer for an unknown address), `otp_expired` (the right
    code, too late: ask for a new one) or `too_many_attempts` (five wrong codes: locked
    for 15 minutes, `retry_after_seconds` and a `Retry-After` header; while locked even
    the right code is refused, so the lock cannot be probed)."""
    from rest_framework import status as http
    from rest_framework.response import Response

    body = {'error': message, 'detail': message, 'code': code}
    headers = {}
    if retry_after:
        body['retry_after_seconds'] = retry_after
        headers['Retry-After'] = str(retry_after)
    return Response(body, status=http.HTTP_400_BAD_REQUEST, headers=headers)


def verify_otp_detailed(email, otp_code, purpose='registration'):
    """`(success, otp, message, code, retry_after_seconds)`.

    The address is matched without regard to case (BACKEND_BUGS B-033): it is stored
    lower-cased, and `Samer@Example.com` counted as a wrong code toward the lockout.

    Verify an OTP code for a user with security hardening:
    - Constant-time comparison via secrets.compare_digest
    - SHA-256 hashed OTP lookup (DB stores hash, not plaintext)
    - Attempt tracking with lockout after 5 failures (15min cooldown)
    - Anti-enumeration: generic error messages
    
    Args:
        email: User's email address
        otp_code: OTP code to verify (plaintext from user)
        purpose: OTP purpose ('registration' or 'password_reset')
    
    Returns:
        tuple: (success, otp_instance or None, error_message or None, code or None,
        retry_after_seconds or None). `code`: `otp_invalid`, `otp_expired`,
        `too_many_attempts`.
    """
    from .models import OTPVerification, CustomUser

    typed = email if isinstance(email, str) else ''
    # ONE key for every spelling that can reach the account: `find_by_email` matches
    # only the lower-cased address or the address exactly as typed, and both have this
    # key. The lockout is checked and counted on it.
    email = typed.strip().lower()
    # Check lockout BEFORE doing any DB work
    is_locked, remaining = _check_otp_lockout(email, purpose)
    if is_locked:
        logger.warning(f"OTP lockout active for {email} ({purpose})")
        return (False, None, str(_OTP_LOCKED_OUT), 'too_many_attempts',
                otp_lockout_seconds_left(email, purpose))
    # A number or a list is a wrong code like any other, never a different answer: the
    # crash it caused answered "something went wrong" for a real account only.
    if not isinstance(otp_code, str):
        otp_code = str(otp_code)

    try:
        # Find user by email
        user = find_by_email(typed)
        if user is None:
            raise CustomUser.DoesNotExist

        # Hash the user-provided OTP for comparison
        otp_hash = _hash_otp(otp_code)

        # Find the most recent unverified OTP for this user with matching purpose
        candidates = OTPVerification.objects.filter(
            user=user,
            email=user.email,
            purpose=purpose,
            is_verified=False
        ).order_by('-created_at', '-id')
        
        matched_otp = None
        for otp in candidates:
            # Constant-time comparison to prevent timing attacks
            if secrets.compare_digest(otp.otp_code, otp_hash):
                matched_otp = otp
                break
        
        if not matched_otp:
            # Record failed attempt
            attempts = _record_otp_failure(email, purpose)
            logger.warning(f"Failed OTP attempt {attempts}/5 for {email} ({purpose})")
            
            # ONE message, whatever the reason. Splitting it on "does this email have a
            # pending OTP" made the endpoint an account oracle, inside the one function
            # whose docstring promises anti-enumeration: "OTP code not found" came back
            # only for an address that HAS an account and no pending code, while an
            # address with no account got "Invalid OTP code". Probing a list of emails
            # with one wrong code told the caller which of them are registered.
            return False, None, str(_INVALID_OTP), 'otp_invalid', None
        
        # Check if OTP is still valid (not expired)
        if not matched_otp.is_valid():
            # Distinguishable on purpose: the caller had to hold the real code to get
            # here, so it reveals nothing they did not know, and "request a new one" is
            # the action they need.
            return False, None, str(_OTP_EXPIRED), 'otp_expired', None
        
        # Mark OTP as verified
        matched_otp.is_verified = True
        matched_otp.verified_at = timezone.now()
        matched_otp.save()
        
        # Clear attempt counter on success
        _clear_otp_attempts(email, purpose)
        
        logger.info(f"OTP ({purpose}) verified for user {user.id} ({email})")
        return True, matched_otp, None, None, None

    except CustomUser.DoesNotExist:
        # Anti-enumeration: same error message whether user exists or not
        _record_otp_failure(email, purpose)
        return False, None, str(_INVALID_OTP), 'otp_invalid', None
    except (OperationalError, InterfaceError):
        # The database, not the code. Answering "invalid" during an outage tells the user
        # their correct code is wrong and sends them to ask for another one.
        logger.exception("OTP verification could not reach the database for %s", email)
        raise
    except Exception as e:
        # The same answer as a wrong code, and counted as one: a distinct answer here
        # told a caller which addresses have accounts.
        logger.error(f"Error verifying OTP for {email}: {str(e)}")
        _record_otp_failure(email, purpose)
        return False, None, str(_INVALID_OTP), 'otp_invalid', None