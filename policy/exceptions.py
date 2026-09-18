"""Refusals the app can branch on. `default_code` is the contract; the text is for logs
and for a developer reading the response, and carries no price, link or instruction."""
from django.utils.translation import gettext_lazy as _
from rest_framework import status
from rest_framework.exceptions import APIException


class AppChannelRequired(APIException):
    status_code = status.HTTP_400_BAD_REQUEST
    default_code = "app_channel_required"
    default_detail = _("This request must declare the app channel it comes from.")


class NotAvailableOnChannel(APIException):
    status_code = status.HTTP_403_FORBIDDEN
    default_code = "not_available_on_channel"
    default_detail = _("This is not available in this version of the app.")
