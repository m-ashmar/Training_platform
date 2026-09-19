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

