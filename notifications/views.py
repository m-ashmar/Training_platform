"""
notifications/views.py — user-facing notification preference API.

Notification *listing* lives at /api/social/notifications/ (NotificationViewSet,
which reads the canonical notifications.Notification store). This module exposes
the preference controls, which previously had no API at all: the model was
consulted at send time by NotificationService but was only editable in Django
admin, so users could never manage their own notification settings.
"""
import logging

from rest_framework import serializers, status, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from django.utils.translation import gettext as _

from .models import UserNotificationPreference

logger = logging.getLogger(__name__)


class UserNotificationPreferenceSerializer(serializers.ModelSerializer):
    class Meta:
        model = UserNotificationPreference
        fields = ['id', 'event_type', 'is_enabled', 'channels']
        read_only_fields = ['id']

    def validate_channels(self, value):
        if not isinstance(value, dict):
            raise serializers.ValidationError(_("channels must be an object, e.g. {\"fcm\": true}"))
        allowed = {'fcm', 'email'}
        unknown = set(value.keys()) - allowed
        if unknown:
            raise serializers.ValidationError(
                _("Unknown channel(s): %(bad)s") % {'bad': ', '.join(sorted(unknown))}
            )
        for k, v in value.items():
            if not isinstance(v, bool):
                raise serializers.ValidationError(_("Channel values must be true/false."))
        return value

    def validate_event_type(self, value):
        value = (value or '').strip()
        if not value:
            raise serializers.ValidationError(_("event_type is required."))
        return value


class NotificationPreferenceViewSet(viewsets.ModelViewSet):
    """
    CRUD for the caller's own notification preferences.

    GET    /api/notifications/preferences/            list own preferences
    POST   /api/notifications/preferences/            create/update one (upsert by event_type)
    PATCH  /api/notifications/preferences/{id}/       update one
    DELETE /api/notifications/preferences/{id}/       revert to default (enabled)
    GET    /api/notifications/preferences/event_types/  discoverable event types
    """
    serializer_class = UserNotificationPreferenceSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        # Always scoped to the caller — no cross-user access.
        return UserNotificationPreference.objects.filter(user=self.request.user)

    def perform_create(self, serializer):
        serializer.save(user=self.request.user)

    def create(self, request, *args, **kwargs):
        """Upsert: re-POSTing an existing event_type updates it instead of 400-ing
        on the unique (user, event_type) constraint."""
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        event_type = serializer.validated_data['event_type']
        existing = UserNotificationPreference.objects.filter(
            user=request.user, event_type=event_type
        ).first()
        if existing:
            update = self.get_serializer(existing, data=request.data, partial=True)
            update.is_valid(raise_exception=True)
            update.save()
            return Response(update.data, status=status.HTTP_200_OK)
        serializer.save(user=request.user)
        return Response(serializer.data, status=status.HTTP_201_CREATED)

    @action(detail=False, methods=['get'])
    def event_types(self, request):
        """List the event types that can be toggled (from the FCM registry)."""
        from .channels.fcm import EVENT_CLASS_REGISTRY
        return Response({'event_types': sorted(EVENT_CLASS_REGISTRY.keys()) + ['all']})
