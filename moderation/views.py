"""Reporting, blocking and AI consent — the endpoints the app calls.

Every route here is authenticated and behaves identically on every build: a person can
report and block on `apk` exactly as on `play` and `ios`, because a store review will
try it on the store build. Nothing in this module reads `X-App-Channel`.

The error shape is the project's: `{"error": <translated>, "code": "<stable>"}` with
the HTTP status. The app branches on `code` and never on the message.
"""
import logging

from django.db import IntegrityError, transaction
from django.utils import timezone
from django.utils.translation import gettext as _
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from routine.views import StandardResultsSetPagination

from .models import AIConsent, Block, Report
from .serializers import (
    AIConsentSerializer,
    AIConsentWriteSerializer,
    BlockCreateSerializer,
    BlockedUserSerializer,
    ReportCreateSerializer,
    ReportSerializer,
)
from .targets import author_id_of, visible_target

logger = logging.getLogger(__name__)


class ReportCreateView(APIView):
    """POST /api/v1/reports/ — report a person, a post, a comment, a routine, a diet
    plan, an exercise or a message from the AI coach.

    Headers: `Authorization: Bearer <access>`, `Accept-Language`. Identical on every
    `X-App-Channel`.

    Body: `{target_type, target_id, reason, note?}`
      `target_type`: `user` | `chat_message` | `routine` | `diet_plan` | `exercise` |
                     `social_post` | `comment`
      `reason`: `spam` | `harassment` | `nudity` | `violence` | `scam` |
                `impersonation` | `other`
      `note`: the reporter's own words, up to 1000 characters, optional.

    Answers:
      201 `{"id": <report id>, "status": "open"}`
      400 `validation_error` — an unknown `target_type` or `reason`, a missing
          `target_id`, or a `note` over the cap; the offending field is in
          `field_errors`.
      403 `cannot_report_yourself` — your own account, or something you wrote. Content
          the platform generated FOR you (your diet plan, the AI's replies) is not
          yours and can be reported.
      404 `target_not_found` — the target does not exist, or the reporter is not
          allowed to see it. ONE answer for both, on purpose: telling them apart turns
          this endpoint into a way to discover which private routines and chat
          messages exist.
      409 `already_reported` — this person already has an open report on this target.
          The same target may be reported again once the first one has been handled.

    ORDER MATTERS in the app's "report and block" flow: call this FIRST, then
    `POST /api/v1/blocks/`. A blocked person's content is no longer visible to the
    reporter, and what they cannot see they cannot report — this route would answer
    `target_not_found`.
    """

    permission_classes = [IsAuthenticated]

    def post(self, request):
        serializer = ReportCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        target = visible_target(data["target_type"], data["target_id"], request.user)
        if target is None:
            return Response(
                {"error": _("There is nothing here to report."),
                 "code": "target_not_found"},
                status=status.HTTP_404_NOT_FOUND)

        if author_id_of(data["target_type"], target) == request.user.pk:
            return Response(
                {"error": _("You cannot report yourself."),
                 "code": "cannot_report_yourself"},
                status=status.HTTP_403_FORBIDDEN)

        try:
            # The partial unique index is the authority, not a prior SELECT: two taps
            # on a slow connection are two concurrent requests, and a check-then-insert
            # lets both through. `atomic()` keeps the IntegrityError from poisoning the
            # surrounding transaction the test client wraps every request in.
            with transaction.atomic():
                report = Report.objects.create(
                    reporter=request.user, target_type=data["target_type"],
                    target_id=data["target_id"], reason=data["reason"],
                    note=data.get("note", ""))
        except IntegrityError:
            return Response(
                {"error": _("You have already reported this. It is being looked at."),
                 "code": "already_reported"},
                status=status.HTTP_409_CONFLICT)

        logger.info("report %s filed on %s", report.pk, data["target_type"])
        return Response({"id": report.pk, "status": report.status},
                        status=status.HTTP_201_CREATED)


class MyReportsView(APIView):
    """GET /api/v1/reports/mine/ — what this person reported, and what came of it.

    Headers: `Authorization: Bearer <access>`, `Accept-Language`.
    Query: `page`, `page_size` (25 by default, 100 at most).

    Answers 200 `{count, next, previous, results: [{id, target_type, target_id,
    reason, note, status, resolution_note, handled_at, created_at}]}`, newest first.
    `status` is `open` | `reviewing` | `actioned` | `dismissed`; `resolution_note` is
    what the operator wrote and is empty until they do. Who handled it is not
    returned — the outcome is the reporter's business, the operator's name is not.
    """

    permission_classes = [IsAuthenticated]

    def get(self, request):
        reports = Report.objects.filter(reporter=request.user)
        paginator = StandardResultsSetPagination()
        page = paginator.paginate_queryset(reports, request, view=self)
        return paginator.get_paginated_response(
            ReportSerializer(page, many=True).data)


class BlockListCreateView(APIView):
    """GET/POST /api/v1/blocks/ — the people this person has hidden.

    Headers: `Authorization: Bearer <access>`, `Accept-Language`.

    GET — 200 `{count, next, previous, results: [{user_id, username, created_at}]}`,
    newest first. Only the blocks this person MADE: a block someone else made is never
    disclosed, because "you have been blocked" is a message a blocker did not send.
    Query: `page`, `page_size`.

    POST `{user_id}` —
      201 `{"user_id": <id>, "blocked": true}`, and again 201 (idempotent) if the
          block already existed, so a retried request is not an error.
      400 `validation_error` — `user_id` missing or not a number.
      400 `cannot_block_yourself`
      404 `user_not_found` — no such active account.

    The effect is symmetric and immediate: neither account appears in the other's feed,
    post and comment lists, follower lists, notifications, public profiles, trainer
    roster or trainer matches, and neither can send the other a trainer request. The
    block is owned by whoever made it — only they can lift it.
    """

    permission_classes = [IsAuthenticated]

    def get(self, request):
        blocks = (Block.objects.filter(blocker=request.user)
                  .select_related("blocked"))
        paginator = StandardResultsSetPagination()
        page = paginator.paginate_queryset(blocks, request, view=self)
        return paginator.get_paginated_response(
            BlockedUserSerializer(page, many=True).data)

    def post(self, request):
        from users.models import CustomUser

        serializer = BlockCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        target_id = serializer.validated_data["user_id"]

        if target_id == request.user.pk:
            return Response(
                {"error": _("You cannot block yourself."),
                 "code": "cannot_block_yourself"},
                status=status.HTTP_400_BAD_REQUEST)

        target = CustomUser.objects.filter(pk=target_id, is_active=True).first()
        if target is None:
            return Response(
                {"error": _("No such account."), "code": "user_not_found"},
                status=status.HTTP_404_NOT_FOUND)

        # Idempotent: blocking twice is the same state, and the app retries.
        Block.objects.get_or_create(blocker=request.user, blocked=target)
        return Response({"user_id": target.pk, "blocked": True},
                        status=status.HTTP_201_CREATED)


class BlockDeleteView(APIView):
    """DELETE /api/v1/blocks/{user_id}/ — lift a block this person made.

    Headers: `Authorization: Bearer <access>`, `Accept-Language`.

    Answers:
      204 — the block is gone (and 204 as well if it was never there: unblocking
            somebody you had not blocked leaves exactly the state the caller asked for).
      404 `block_not_found` is NOT used, for that reason.

    Lifting a block cannot lift the OTHER person's block of you, and does not tell them
    anything happened.
    """

    permission_classes = [IsAuthenticated]

    def delete(self, request, user_id):
        Block.objects.filter(blocker=request.user, blocked_id=user_id).delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class AIConsentView(APIView):
    """GET/POST /api/v1/ai-consent/ — whether the AI coach may read this person's data.

    Headers: `Authorization: Bearer <access>`, `Accept-Language`.

    GET — 200 `{granted, policy_version, granted_at, revoked_at}`. Never asked reads as
    `{"granted": false, "policy_version": "", "granted_at": null, "revoked_at": null}`,
    which is how the app tells "has not been asked" from "asked and said no"
    (`revoked_at` set).

    POST `{granted: true|false, policy_version}` —
      200 the same body as GET, with the new state.
      400 `validation_error` — `granted` missing, or `policy_version` missing while
          granting (it is ignored when revoking: a revocation withdraws every version).

    Until it is granted, everything that answers as the AI coach refuses with
    `ai_consent_required`: 403 on the REST routes under `/api/ai/`, and over the
    WebSocket `{"type": "error", "code": "ai_consent_required", "content": "..."}` on
    `ws/ai/chat/` without closing the socket, so the app can show the consent sheet and
    carry on in the same connection. Revoking takes effect on the very next message —
    nothing caches the answer.
    """

    permission_classes = [IsAuthenticated]

    def get(self, request):
        row = AIConsent.objects.filter(user=request.user).first() or AIConsent(
            user=request.user)
        return Response(AIConsentSerializer(row).data)

    def post(self, request):
        serializer = AIConsentWriteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        now = timezone.now()

        row, _created = AIConsent.objects.get_or_create(user=request.user)
        if data["granted"]:
            # Re-granting clears the revocation rather than keeping both stamps, so
            # `is_active` can never read a row as granted and revoked at once.
            row.granted_at = now
            row.revoked_at = None
            row.policy_version = data["policy_version"].strip()
        else:
            row.revoked_at = now
        row.save(update_fields=["granted_at", "revoked_at", "policy_version",
                                "updated_at"])
        return Response(AIConsentSerializer(row).data)
