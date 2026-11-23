"""Data-subject rights endpoints.

Both are derived from the registry, so an app that registers its data gets export and
erasure without touching this module.
"""
import logging

from django.db import transaction
from django.utils.translation import gettext as _
from rest_framework import permissions, status
from rest_framework.response import Response
from rest_framework.views import APIView

from .registry import ErasureRefused, erase_user_data

logger = logging.getLogger(__name__)


class PersonalDataExportView(APIView):
    """GET /api/privacy/export/ — everything held about the caller (GDPR Art. 15).

    Every role: the registry's sources are keyed on the person whichever side they
    are (a trainer's coaching relations included); checked for a trainer 2026-11-22.

    Returned as a file download rather than a page: the payload contains the user's
    full health and training history and should not sit in a browser cache.

    `{generated_at, user_id, sections: {<label>: [rows]}}`, one section per registered
    source (`privacy/sources.py`). It never discloses what the API itself withholds:
    `blocks` is `{id, blocked, created_at}` for the blocks this person MADE, never one
    made against them; `reports_made` is what `GET /api/v1/reports/mine/` pages, without
    who handled it. `ai_training_data` is every anonymous copy kept to improve the AI
    coach while "help us improve" was on, exactly as stored: `{created_at, retain_until,
    user_context_snapshot, user_message, tools_called, tool_results, ai_response,
    user_feedback}`, with the ranges and the scrubbed text and without the keyed
    pseudonym it is stored under. The list is empty when none is kept. Identical on
    every build.

    The document is written a page of rows at a time (`registry.write_user_data`,
    `PRIVACY_BATCH_ROWS`) into a spool that moves to a temporary file past
    `PRIVACY_EXPORT_SPOOL_BYTES`; a small export is answered as one body, a large one is
    streamed from that file. Either way it is the same JSON document with the same
    headers — a long-time user's export used to be built whole in memory, every row a
    dict, and could take a 512 MB machine down with it (the production-readiness audit,
    2026-09-25).
    """

    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        import tempfile

        from django.conf import settings
        from django.http import HttpResponse, StreamingHttpResponse

        from .registry import write_user_data

        limit = int(getattr(settings, "PRIVACY_EXPORT_SPOOL_BYTES", 8 * 1024 * 1024))
        spool = tempfile.SpooledTemporaryFile(max_size=limit, mode="w+b")
        try:
            write_user_data(request.user, spool)
        except BaseException:
            spool.close()
            raise
        logger.info("Personal-data export generated for user %s", request.user.pk)
        if not getattr(spool, "_rolled", False):     # still in memory: under the limit
            spool.seek(0)
            body = spool.read()
            spool.close()
            response = HttpResponse(body, content_type="application/json")
        else:
            spool.seek(0)
            response = StreamingHttpResponse(_chunks(spool), content_type="application/json")
            # Closed when the response is, whether or not it was read to the end.
            response._resource_closers.append(spool.close)
        response["Content-Disposition"] = (
            f'attachment; filename="personal-data-{request.user.pk}.json"'
        )
        response["Cache-Control"] = "no-store"
        return response


#: How much of a spooled export one streamed chunk carries.
EXPORT_CHUNK_BYTES = 64 * 1024


async def _chunks(spool):
    """The spooled export, a chunk at a time. An ASYNC iterator on purpose: under ASGI
    (Daphne) Django consumes a synchronous one by reading it whole into a list first,
    which would put the file back into memory."""
    from asgiref.sync import sync_to_async

    read = sync_to_async(spool.read, thread_sensitive=False)
    while True:
        chunk = await read(EXPORT_CHUNK_BYTES)
        if not chunk:
            break
        yield chunk


def money_left(user) -> dict:
    """The money an account still holds, as deletion has to tell the person BEFORE they
    confirm (R4, 2026-11-23). The privacy policy says it: "If there is money in your
    wallet, contact us first to get it back" — erasure keeps wallets and ledgers
    (financial records) but nobody can spend a deleted account's balance, and the
    preview said only `{"rows": 1, "action": "keep"}`.

    `wallet` — what each of the person's wallets holds, the non-zero ones only.
    `held_coaching_months` — months of coaching held in escrow for a trainer that the
    erasure returns to the client's wallet first (the wallet then holds more).
    `agent_cash` — an agent only: the cash they collected for the platform and have not
    settled; deleting the account does not settle it.
    `contact_support` — any of the three is non-zero.

    Amounts are decimal strings, each beside its currency. Read-only, no lock."""
    from django.db.models import Sum

    from users.models import CoachingPeriod
    from wallet.models import AgentReceivableEntry, Wallet

    wallet = [{"currency": w.currency, "amount": str(w.balance)}
              for w in (Wallet.objects.filter(owner=user).exclude(balance=0)
                        .order_by("currency", "id"))]
    held = CoachingPeriod.objects.filter(client=user, status="held").count()
    agent_cash = [{"currency": row["currency"], "amount": str(row["total"])}
                  for row in (AgentReceivableEntry.objects.filter(agent__user=user)
                              .values("currency").annotate(total=Sum("amount"))
                              .order_by("currency"))
                  if row["total"]]
    return {"wallet": wallet, "held_coaching_months": held, "agent_cash": agent_cash,
            "contact_support": bool(wallet or held or agent_cash)}


class PersonalDataEraseView(APIView):
    """DELETE /api/privacy/erase/ — erasure request (GDPR Art. 17).

    Financial records are preserved by design: `Wallet.owner` is PROTECT so a deletion
    cannot destroy a balance, and payment history has to survive. Everything else is
    deleted or anonymised, and the account is deactivated.

    `?dry_run=1` reports what would happen without touching anything. `GET` is the same
    preview; for a trainer, `preview.trainer_relations.active_clients` counts the clients
    they still coach, and while it is above 0 the erasure is refused.

    **The money left** (R4, 2026-11-23). `GET`, the 400 `confirmation_required` and the
    200 each carry `money` beside the rest: `{wallet: [{currency, amount}],
    held_coaching_months, agent_cash: [{currency, amount}], contact_support}` (see
    `money_left`; only non-zero amounts are listed). Deleting is not refused for money:
    while `contact_support` is true the app says, before the person confirms, that the
    balance is not spendable after deletion and support returns it on request — the
    privacy policy's "contact us first to get it back". On the 200 it is what remains
    after the erasure (a held month already returned into `wallet`). For an agent,
    `agent_cash` is the platform's cash they still hold: deleting does not settle it.
    The same for every role and on every build.

    The anonymous AI training copies (`ai_training_data`) are found by the keyed
    pseudonym they are stored under and deleted, and counted like any other source.

    Rows about the caller that other people hold go too and are never counted in the
    preview or the report: blocks made against them, and the chat notices they sent,
    which sit in the other side's inbox with their name and words.

    Bounded however long the person has used the app: rows are deleted and anonymised a
    page of `PRIVACY_BATCH_ROWS` at a time, in the one transaction, and the stored files
    of what was deleted (chat photos, voice notes, videos, media-kit pictures, the
    profile picture) leave storage AFTER the commit, through a background task that
    retries what storage refuses — not one storage call per file inside this request
    (`training_platform/storage_cleanup.py`). A trainer's verification documents are the
    exception: deleted from storage first, synchronously, so storage refusing them still
    refuses the erasure (`erasure_refused`, below).

    Answers:
      200 `{message, detail, money}`   erased.
      400 `confirmation_required` `{error, detail, code, preview, money}` — `confirm=ERASE`
          missing (it answered the status default `bad_request` until the owner's
          decision of 2026-09-24): show the preview and ask the user to confirm.
      409 `trainer_has_active_clients` with `active_clients` — a trainer still
          coaches someone (the owner's rule: never while he has active clients).
          "Coaches" is an APPROVED relation with the caller as the trainer
          (`users/trainer_relations.py`); a pending request does not refuse it — it is
          withdrawn and its hold returned with the account. End each coaching first: `POST /api/auth/trainer/unassign-client/`, with
          `at_period_end` for a paid month, which then ends at the month's end.
      409 `erasure_refused` — a held payment could not be returned, or a trainer's
          verification document could not be deleted from storage (the error says
          which); nothing is erased, retry later.
    Identical on every build.
    """

    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        """Preview — what an erasure would remove."""
        return Response({"preview": erase_user_data(request.user, dry_run=True),
                         "money": money_left(request.user)})

    @transaction.atomic
    def delete(self, request):
        confirm = request.query_params.get("confirm") or request.data.get("confirm")
        if confirm != "ERASE":
            return Response(
                {
                    "error": _("Confirmation required."),
                    "detail": _("Repeat the request with confirm=ERASE. This cannot be undone."),
                    "code": "confirmation_required",
                    "preview": erase_user_data(request.user, dry_run=True),
                    "money": money_left(request.user),
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
            # good — or storage refused to delete an identity document, and deleting its
            # row anyway would leave the file where nothing could ever find it.
            logger.exception("Erasure refused for user %s", user_id)
            if exc.extra.get("cause") == "storage":
                message = _("Your data could not be erased right now because a document "
                            "you sent for verification could not be deleted. Please try "
                            "again later.")
            else:
                message = _("Your data could not be erased right now because a payment "
                            "held for your trainer could not be returned. Nothing was "
                            "changed; please try again later.")
            return Response({"error": message, "code": "erasure_refused"},
                            status=status.HTTP_409_CONFLICT)
        logger.warning("Personal data erased for user %s: %s", user_id, report)
        return Response({"message": _("Your personal data has been erased."), "detail": report,
                         "money": money_left(request.user)})
