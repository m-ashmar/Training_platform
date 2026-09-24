"""Sign-in refusals the app branches on (BACKEND_BUGS B-004). `code` is the contract."""
from django.utils.translation import gettext_lazy as _
from rest_framework import status
from rest_framework.exceptions import APIException


class TooManyAttempts(APIException):
    """Five failed sign-ins lock the address for 15 minutes (users/utils.py).

    400, as the lockout always answered, now with its own `code` and the seconds left:
    `{detail, error, code: "too_many_attempts", retry_after_seconds, non_field_errors}`.
    `non_field_errors` is kept for callers that read the old shape.
    """
    status_code = status.HTTP_400_BAD_REQUEST
    default_code = 'too_many_attempts'

    def __init__(self, seconds_left):
        message = _("Too many failed login attempts. Please try again in 15 minutes.")
        super().__init__({'detail': message, 'non_field_errors': [message]})
        # After `super()`: DRF turns every value it is given into text, and the app reads
        # a number. `wait` also makes DRF send a `Retry-After` header.
        self.detail['retry_after_seconds'] = self.wait = int(seconds_left)

    def get_codes(self):
        # One code for the whole refusal; the number above has none to walk.
        return self.default_code



class AccountDisabled(APIException):
    """The password is right and the account is suspended or retired (owner, 2026-09-19:
    "they should see account suspended").

    Only said to someone who proved the password, so it tells a stranger nothing: a wrong
    password on a suspended account still reads as a wrong password. The same code
    `verify-otp` answers with.
    """
    status_code = status.HTTP_403_FORBIDDEN
    default_code = 'account_disabled'
    default_detail = _("This account is suspended. Please contact support.")


class _TrainingRefused(APIException):
    """403 with the trainer named: `{detail, error, code, trainer_id}`. No price, so the
    body is the same on every build."""
    status_code = status.HTTP_403_FORBIDDEN

    def __init__(self, trainer_id):
        super().__init__({'detail': self.default_detail})
        self.detail['trainer_id'] = trainer_id

    def get_codes(self):
        return self.default_code


class CoachingFrozen(_TrainingRefused):
    """The coaching is paused: the renewal found the wallet short (the owner,
    2026-09-19). The routine stays readable; starting or logging it waits until a month
    is paid, which the hourly renewal charges as soon as the wallet covers it."""
    default_code = 'coaching_frozen'
    default_detail = _("Your coaching is paused until the next month is paid. You can "
                       "still see your routine.")


class CoachingUnpaid(_TrainingRefused):
    """The coaching stands, but support returned the payment for its month (status
    `unpaid` on `coaching/current/`): nothing is paid until support resolves it, and
    asking for the trainer again is not the way out."""
    default_code = 'coaching_unpaid'
    default_detail = _("Your coaching is on hold until support resolves its payment. You "
                       "can still see your routine.")


class CoachingEnded(_TrainingRefused):
    """The coaching this routine came from has ended. The routine stays readable; it
    can be trained on again after a new coaching with the trainer starts."""
    default_code = 'coaching_ended'
    default_detail = _("Your coaching with this trainer has ended. You can still see "
                       "this routine; start a new coaching to train on it.")


def disabled_account_with_password(email, password):
    """The disabled account `email` names, if `password` is its password; else None."""
    if not email or not password:
        return None
    # Lower-cased or as typed, never `iexact` (users/utils.py `find_by_email`).
    from .utils import find_by_email
    user = find_by_email(email, is_active=False)
    if user is None:
        # The same one hash every failed sign-in costs here, so a suspended address is
        # not slower to refuse than any other: timing must not tell a stranger either.
        from django.contrib.auth.hashers import make_password
        make_password(password)
        return None
    return user if user.check_password(password) else None


class ProductRefunded(APIException):
    """A training write against the buyer's copy of a routine product whose purchase an
    operator refunded (`routine/products.py refund`, which also takes the copy away).
    `{detail, error, code: "product_refunded"}` — no trainer: a purchase is not a
    coaching, and no price, so the body is the same on every build."""
    status_code = status.HTTP_403_FORBIDDEN
    default_code = 'product_refunded'
    default_detail = _("This program was refunded, so it can no longer be trained on.")
