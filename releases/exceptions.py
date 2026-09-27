"""Refusals of `GET /api/v1/app/latest`. `default_code` is the contract; the text names no
URL, price or build of another channel."""
from django.utils.translation import gettext_lazy as _
from rest_framework import status
from rest_framework.exceptions import APIException


class ReleaseChannelInvalid(APIException):
    """`?channel=` names no channel that has releases (`web` is the hub, not a build).
    The same fact, and the same code, as a field the server offers choices for."""
    status_code = status.HTTP_400_BAD_REQUEST
    default_code = "invalid_choice"
    default_detail = _("There are no app releases for this channel.")


class ChannelMismatch(APIException):
    """A store build asked about another channel's releases."""
    status_code = status.HTTP_400_BAD_REQUEST
    default_code = "channel_mismatch"
    default_detail = _("This version of the app can only ask about its own releases.")


class BuildInvalid(APIException):
    status_code = status.HTTP_400_BAD_REQUEST
    default_code = "build_invalid"
    default_detail = _("The build number must be a whole number.")
