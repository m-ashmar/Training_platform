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

from .registry import ErasureRefused, erase_user_data, export_user_data

logger = logging.getLogger(__name__)


class PersonalDataExportView(APIView):
    """GET /api/privacy/export/ — everything held about the caller (GDPR Art. 15).

    Returned as a file download rather than a page: the payload contains the user's
    full health and training history and should not sit in a browser cache.

    `{generated_at, user_id, sections: {<label>: [rows]}}`, one section per registered
    source (`privacy/sources.py`). It never discloses what the API itself withholds:
    `blocks` is `{id, blocked, created_at}` for the blocks this person MADE, never one
    made against them; `reports_made` is what `GET /api/v1/reports/mine/` pages, without
    who handled it. Identical on every build.
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

    `?dry_run=1` reports what would happen without touching anything. `GET` is the same
    preview; for a trainer, `preview.trainer_relations.active_clients` counts the clients
    they still coach, and while it is above 0 the erasure is refused.

    Rows about the caller that other people hold go too and are never counted in the
    preview or the report: blocks made against them, and the chat notices they sent,
    which sit in the other side's inbox with their name and words.

    Answers:
      200 `{message, detail}`   erased.
      400 `{error, detail, preview}`   `confirm=ERASE` missing.
      409 `code: trainer_has_active_clients` with `active_clients` — a trainer still
          coaches someone (the owner's rule: never while he has active clients). End
          each coaching first: `POST /api/auth/trainer/unassign-client/`, with
          `at_period_end` for a paid month, which then ends at the month's end.
      409 `code: erasure_refused` — a held payment could not be returned; retry later.
    Identical on every build.
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
        try:
            report = erase_user_data(request.user)
        except ErasureRefused as exc:
            if exc.code == "trainer_has_active_clients":
                logger.warning("Erasure refused for trainer %s: %s active clients",
                               user_id, exc.extra.get("active_clients"))
                return Response(
                    {"error": _("You still coach active clients. End your coaching with "
                                "each of them before deleting your account."),
                     "code": "trainer_has_active_clients",
                     "active_clients": exc.extra.get("active_clients", 0)},
                    status=status.HTTP_409_CONFLICT,
                )
            # Rolled back whole, and safe to retry: the escrow could not return a held
            # trainer charge, and deleting its record anyway would lose the money for
            # good.
            logger.exception("Erasure refused for user %s", user_id)
            return Response(
                {"error": _("Your data could not be erased right now because a payment "
                            "held for your trainer could not be returned. Nothing was "
                            "changed; please try again later."),
                 "code": "erasure_refused"},
                status=status.HTTP_409_CONFLICT,
            )
        logger.warning("Personal data erased for user %s: %s", user_id, report)
        return Response({"message": _("Your personal data has been erased."), "detail": report})
