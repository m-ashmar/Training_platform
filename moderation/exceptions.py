"""Refusals the app branches on. `default_code` is the contract; the text is not.

The same shape `policy/exceptions.py` uses, for the same reason: the message is
translated and the code never is, so a client that switches on the message stops
working the moment the user picks Arabic.
"""
from django.utils.translation import gettext_lazy as _
from rest_framework import status
from rest_framework.exceptions import APIException


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
