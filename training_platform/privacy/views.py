"""Data-subject rights endpoints.

Both are derived from the registry, so an app that registers its data gets export and
erasure without touching this module.
"""
import logging

from django.db import transaction
from django.http import JsonResponse
from django.utils.translation import gettext as _
from rest_framework import permissions, status
from rest_framework.response import Response
from rest_framework.views import APIView

from .registry import erase_user_data, export_user_data

logger = logging.getLogger(__name__)


class PersonalDataExportView(APIView):
    """GET /api/privacy/export/ — everything held about the caller (GDPR Art. 15).

    Returned as a file download rather than a page: the payload contains the user's
    full health and training history and should not sit in a browser cache.
    """

    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        data = export_user_data(request.user)
        logger.info("Personal-data export generated for user %s", request.user.pk)
        response = JsonResponse(data, json_dumps_params={"indent": 2})
        response["Content-Disposition"] = (
            f'attachment; filename="personal-data-{request.user.pk}.json"'
        )
        response["Cache-Control"] = "no-store"
        return response


class PersonalDataEraseView(APIView):
    """DELETE /api/privacy/erase/ — erasure request (GDPR Art. 17).

    Financial records are preserved by design: `Wallet.owner` is PROTECT so a deletion
    cannot destroy a balance, and payment history has to survive. Everything else is
    deleted or anonymised, and the account is deactivated.

    `?dry_run=1` reports what would happen without touching anything.
    """

    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        """Preview — what an erasure would remove."""
        return Response({"preview": erase_user_data(request.user, dry_run=True)})

    @transaction.atomic
    def delete(self, request):
        confirm = request.query_params.get("confirm") or request.data.get("confirm")
        if confirm != "ERASE":
            return Response(
                {
                    "error": _("Confirmation required."),
                    "detail": _("Repeat the request with confirm=ERASE. This cannot be undone."),
                    "preview": erase_user_data(request.user, dry_run=True),
                },
                status=status.HTTP_400_BAD_REQUEST,
            )
        user_id = request.user.pk
        report = erase_user_data(request.user)
        logger.warning("Personal data erased for user %s: %s", user_id, report)
        return Response({"message": _("Your personal data has been erased."), "detail": report})
