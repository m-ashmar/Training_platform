"""
`custom` — a trainer sending a free-text message to one of their clients.

The event type and its template were registered from the start with no way to produce
one: there was no endpoint, no service call, nothing. A trainer could assign routines
and diet plans but could not say a word to the person following them.

Authorisation is the whole risk here. This is one of two endpoints where one user writes
text that lands on another user's lock screen (the other is the chat), so it is
restricted to trainers, and only to clients with an **approved** TrainerClientRelation —
pending and rejected are not enough — and it answers to the same two rules the chat
does (SIDE_BUGS S-066): nobody writes to a person they have blocked or who has blocked
them, and a coaching that is frozen, unpaid or ended is silent in both directions (the
owner's freeze rule, 2026-09-19). Before those checks this route was the way around
both: a trainer the chat refused could still put text on the client's lock screen.
"""

from __future__ import annotations

import logging
import uuid

from django.utils.translation import gettext as _
from rest_framework import serializers, status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

logger = logging.getLogger(__name__)

MAX_MESSAGE_LENGTH = 500


class TrainerMessageSerializer(serializers.Serializer):
    client_id = serializers.IntegerField()
    message = serializers.CharField(max_length=MAX_MESSAGE_LENGTH, trim_whitespace=True)

    def validate_message(self, value):
        value = (value or "").strip()
        if not value:
            raise serializers.ValidationError(_("Message cannot be empty."))
        # Push payloads are flattened to strings and rendered into a template; newlines
        # and control characters have no place in a notification body.
        if any(ord(c) < 32 and c not in "\n" for c in value):
            raise serializers.ValidationError(_("Message contains invalid characters."))
        return " ".join(value.split())


class TrainerMessageView(APIView):
    """
    POST /api/notifications/message-client/

    Auth: a trainer (or an admin). Body:
    `{"client_id": 42, "message": "Rest day tomorrow, focus on mobility."}` — `message`
    at most 500 characters, one line. Nothing here differs by build.

    Answers:
      201 `{"message", "delivered": true, "message_id"}` — written to the client's
          inbox and pushed (unless they muted `custom`, which still keeps the row).
      400 `validation_error` — empty message, control characters, over 500.
      403 `permission_denied` — the caller is not a trainer or an admin.
      404 `not_found` — no such active client, or not one this trainer has an APPROVED
          relation with. One answer for both, so ids cannot be walked.
      403 `user_blocked` — either of the two has blocked the other. The same answer
          whichever side blocked, as on every other route that refuses a block.
      403 `coaching_frozen` / `coaching_unpaid` / `coaching_ended` (with
          `trainer_id`) — the coaching cannot be used right now. The same codes and
          shape the chat and every training write answer with.

    An admin is not a party to a coaching: the relation, block and coaching checks
    apply to trainers.
    """

    permission_classes = [IsAuthenticated]

    def post(self, request):
        from django.contrib.auth import get_user_model
        from users.models import TrainerClientRelation

        from notifications.services import NotificationService

        user = request.user
        if not (getattr(user, "is_trainer", False) or getattr(user, "is_admin", False)):
            return Response(
                {"detail": _("Only trainers can message clients."), "code": "permission_denied"},
                status=status.HTTP_403_FORBIDDEN,
            )

        serializer = TrainerMessageSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        client_id = serializer.validated_data["client_id"]
        message = serializer.validated_data["message"]

        User = get_user_model()
        try:
            client = User.objects.get(pk=client_id, user_type="client", is_active=True)
        except User.DoesNotExist:
            # Same response as an unrelated client, so this cannot be used to discover
            # which user ids exist.
            return Response(
                {"detail": _("No such client."), "code": "not_found"},
                status=status.HTTP_404_NOT_FOUND,
            )

        if not user.is_admin:
            if not TrainerClientRelation.objects.filter(
                trainer=user, client=client, status="approved"
            ).exists():
                return Response(
                    {"detail": _("No such client."), "code": "not_found"},
                    status=status.HTTP_404_NOT_FOUND,
                )
            # The chat's two rules, asked the way the chat asks them (S-066).
            from moderation.exceptions import UserIsBlocked
            from moderation.services import is_blocked
            from users.coaching import raise_refusal, refusals_for

            if is_blocked(user, client):
                raise UserIsBlocked()
            raise_refusal(refusals_for(user.pk, [client.pk]).get(client.pk), user.pk)

        # A unique id per message: the dedup key is
        # (recipient, event_type, related_object_id), so a shared id would let a
        # trainer send exactly one message ever and silently drop the rest.
        message_id = uuid.uuid4().hex

        notification = NotificationService.create_and_send(
            recipient=client,
            actor=user,
            event_type="custom",
            related_object_id=message_id,
            metadata={
                "context": {"message": message},
                "data": {
                    "type": "custom",
                    "message_id": message_id,
                    "trainer_id": str(user.pk),
                },
            },
        )

        if notification is None:
            # Not reached by a muted type any more: the message is kept in the client's
            # inbox and only its push is withheld (BACKEND_BUGS B-014). Kept for any
            # other path that yields no row.
            return Response(
                {"message": _("The client has turned off messages of this kind."),
                 "delivered": False},
                status=status.HTTP_200_OK,
            )

        logger.info("trainer %s messaged client %s (%s)", user.pk, client.pk, message_id)
        return Response(
            {"message": _("Message sent."), "delivered": True, "message_id": message_id},
            status=status.HTTP_201_CREATED,
        )
