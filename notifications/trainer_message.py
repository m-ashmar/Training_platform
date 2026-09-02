"""
`custom` — a trainer sending a free-text message to one of their clients.

The event type and its template were registered from the start with no way to produce
one: there was no endpoint, no service call, nothing. A trainer could assign routines
and diet plans but could not say a word to the person following them.

Authorisation is the whole risk here. This is the only endpoint where one user writes
text that lands on another user's lock screen, so it is restricted to trainers, and
only to clients with an **approved** TrainerClientRelation — pending and rejected are
not enough.
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

    Body: {"client_id": 42, "message": "Rest day tomorrow, focus on mobility."}
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

        if not user.is_admin and not TrainerClientRelation.objects.filter(
            trainer=user, client=client, status="approved"
        ).exists():
            return Response(
                {"detail": _("No such client."), "code": "not_found"},
                status=status.HTTP_404_NOT_FOUND,
            )

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
            # The only remaining cause is the client disabling `custom` notifications.
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
