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

# Addresses never reach a log line in clear (training_platform/log_privacy.py).
from training_platform.log_privacy import mask_email, redact_emails

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

def send_otp_email(user, otp_code):
    """
    Send OTP code to user's email address.
    
    Args:
        user: CustomUser instance
        otp_code: 6-digit OTP code to send
    
    Returns:
        bool: True if email was sent successfully, False otherwise
    """
    from training_platform.i18n import LanguageContext
    from django.utils.translation import gettext as _, get_language
    from django.utils.translation import get_language_bidi

    try:
        with LanguageContext.for_user(user):
            subject = str(_('Verify Your Email - Training Platform'))
            
            # Try to render HTML template, fallback to plain text
            try:
                html_message = render_to_string('emails/otp_verification.html', {
                    'user': user,
                    'otp_code': otp_code,
                    'LANGUAGE_CODE': get_language(),
                    'LANGUAGE_BIDI': get_language_bidi(),
                })
                plain_message = str(_("Your OTP verification code is: %(code)s\n\nThis code will expire in 10 minutes.\n\nIf you didn't request this code, please ignore this email.")) % {"code": otp_code}
            except Exception as e:
                logger.warning(f"Could not render email template: {e}. Using plain text.")
                html_message = None
                plain_message = str(_("Your OTP verification code is: %(code)s\n\nThis code will expire in 10 minutes.\n\nIf you didn't request this code, please ignore this email.")) % {"code": otp_code}
            
            from_email = getattr(settings, 'DEFAULT_FROM_EMAIL', 'noreply@trainingplatform.com')
            
            send_mail(
                subject=subject,
                message=plain_message,
                from_email=from_email,
                recipient_list=[user.email],
                html_message=html_message,
                fail_silently=False,
            )
        
        logger.info(f"OTP email sent to {mask_email(user.email)} for user {user.id}")
        return True
        
    except Exception as e:
        logger.error(f"Error sending OTP email to {mask_email(user.email)} for user {user.id}: "
                     f"{redact_emails(str(e))}")
        return False

def create_otp(user):
    """
    Create a new OTP verification record for a user (registration).
    
    Args:
        user: CustomUser instance
    
    Returns:
        OTPVerification: Created OTP verification instance
    """
    from .models import OTPVerification
    
    # Invalidate any existing unverified OTPs for this user
    OTPVerification.objects.filter(
        user=user,
        is_verified=False,
        purpose='registration',
        expires_at__gt=timezone.now()
    ).update(is_verified=True)  # Mark as used to invalidate
    
    # Generate new OTP and store its hash (never plaintext)
    otp_code = generate_otp()
    otp_hash = _hash_otp(otp_code)
    expires_at = timezone.now() + timedelta(minutes=10)
    
    otp = OTPVerification.objects.create(
        user=user,
        otp_code=otp_hash,  # keyed HMAC-SHA256, never the plaintext code
        email=user.email,
        purpose='registration',
        expires_at=expires_at
    )
    
    # Send OTP email
    send_otp_email(user, otp_code)
    
    logger.info(f"OTP created for user {user.id} ({mask_email(user.email)})")
    return otp


def create_password_reset_otp(user):
    """
    Create a new OTP verification record for password reset.
    
    Args:
        user: CustomUser instance
    
    Returns:
        OTPVerification: Created OTP verification instance
    """
    from .models import OTPVerification
    
    # Invalidate any existing unverified password reset OTPs for this user
    OTPVerification.objects.filter(
        user=user,
        is_verified=False,
        purpose='password_reset',
        expires_at__gt=timezone.now()
    ).update(is_verified=True)
    
    # Generate new OTP and store its hash (never plaintext)
    otp_code = generate_otp()
    otp_hash = _hash_otp(otp_code)
    expires_at = timezone.now() + timedelta(minutes=10)
    
    otp = OTPVerification.objects.create(
        user=user,
        otp_code=otp_hash,  # keyed HMAC-SHA256, never the plaintext code
        email=user.email,
        purpose='password_reset',
        expires_at=expires_at
    )
    
    # Send password reset email
    send_password_reset_email(user, otp_code)
    
    logger.info(f"Password reset OTP created for user {user.id} ({mask_email(user.email)})")
    return otp


def send_password_reset_email(user, otp_code):
    """
    Send password reset OTP code to user's email address.
    
    Args:
        user: CustomUser instance
        otp_code: 6-digit OTP code
    
    Returns:
        bool: True if email was sent successfully
    """
    from training_platform.i18n import LanguageContext
    from django.utils.translation import gettext as _, get_language
    from django.utils.translation import get_language_bidi

    try:
        with LanguageContext.for_user(user):
            subject = str(_('Password Reset - Training Platform'))
            
            try:
                html_message = render_to_string('emails/password_reset_otp.html', {
                    'user': user,
                    'otp_code': otp_code,
                    'LANGUAGE_CODE': get_language(),
                    'LANGUAGE_BIDI': get_language_bidi(),
                })
                plain_message = str(_(
                    "Your password reset code is: %(code)s\n\n"
                    "This code will expire in 10 minutes.\n\n"
                    "If you didn't request a password reset, please ignore this email."
                )) % {"code": otp_code}
            except Exception as e:
                logger.warning(f"Could not render password reset template: {e}. Using plain text.")
                html_message = None
                plain_message = str(_(
                    "Your password reset code is: %(code)s\n\n"
                    "This code will expire in 10 minutes.\n\n"
                    "If you didn't request a password reset, please ignore this email."
                )) % {"code": otp_code}
            
            from_email = getattr(settings, 'DEFAULT_FROM_EMAIL', 'noreply@trainingplatform.com')
            
            send_mail(
                subject=subject,
                message=plain_message,
                from_email=from_email,
                recipient_list=[user.email],
                html_message=html_message,
                fail_silently=False,
            )
        
        logger.info(f"Password reset email sent to {mask_email(user.email)} for user {user.id}")
        return True
        
    except Exception as e:
        logger.error(f"Error sending password reset email to {mask_email(user.email)} for user "
                     f"{user.id}: {redact_emails(str(e))}")
        return False


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
        logger.warning(f"OTP lockout active for {mask_email(email)} ({purpose})")
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
            logger.warning(f"Failed OTP attempt {attempts}/5 for {mask_email(email)} ({purpose})")
            
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
        
        logger.info(f"OTP ({purpose}) verified for user {user.id} ({mask_email(email)})")
        return True, matched_otp, None, None, None

    except CustomUser.DoesNotExist:
        # Anti-enumeration: same error message whether user exists or not
        _record_otp_failure(email, purpose)
        return False, None, str(_INVALID_OTP), 'otp_invalid', None
    except (OperationalError, InterfaceError):
        # The database, not the code. Answering "invalid" during an outage tells the user
        # their correct code is wrong and sends them to ask for another one.
        logger.exception("OTP verification could not reach the database for %s",
                         mask_email(email))
        raise
    except Exception as e:
        # The same answer as a wrong code, and counted as one: a distinct answer here
        # told a caller which addresses have accounts.
        logger.error(f"Error verifying OTP for {mask_email(email)}: {redact_emails(str(e))}")
        _record_otp_failure(email, purpose)
        return False, None, str(_INVALID_OTP), 'otp_invalid', None