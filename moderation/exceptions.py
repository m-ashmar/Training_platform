"""Refusals the app branches on. `default_code` is the contract; the text is not.

The same shape `policy/exceptions.py` uses, for the same reason: the message is
translated and the code never is, so a client that switches on the message stops
working the moment the user picks Arabic.
"""
from django.utils.translation import gettext_lazy as _
from rest_framework import status
from rest_framework.exceptions import APIException, ValidationError


class AIConsentRequired(APIException):
    """The AI coach has not been allowed to read this person's data yet."""

    status_code = status.HTTP_403_FORBIDDEN
    default_code = "ai_consent_required"
    default_detail = _("Allow the AI coach to use your data before asking it anything.")


class UserIsBlocked(APIException):
    """One of the two people has blocked the other.

    Deliberately the same answer in both directions and the same wording either way:
    saying which side blocked whom would turn a block into a notification.
    """

    status_code = status.HTTP_403_FORBIDDEN
    default_code = "user_blocked"
    default_detail = _("This is not available between these two accounts.")


class ContentNotAllowed(ValidationError):
    """A write holds a term on the banned-terms list (`moderation/ugc_filter.py`).

    A `ValidationError`, so the envelope carries `field_errors`: one entry per refused
    field, each with this code. `service_code` makes it the top-level `code` too
    (`training_platform/exception_handler.py`), where a plain field error would answer
    `validation_error`. The message never names the term: that would let anyone read
    the list one guess at a time.
    """

    status_code = status.HTTP_400_BAD_REQUEST
    default_code = "content_not_allowed"
    service_code = "content_not_allowed"
    default_detail = _("This contains words that are not allowed here. "
                       "Please change them and try again.")

    def __init__(self, fields):
        super().__init__({name: [self.default_detail] for name in fields},
                         code=self.default_code)
