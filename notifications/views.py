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
        # One of the types `event_types/` lists. A typo answered 201 and muted nothing
        # (BACKEND_BUGS B-014). The email types (`email_event_types`) count as well;
        # today each of them is a push type too (B-168).
        from notifications.channels.email import email_preference_types
        from notifications.channels.fcm import EVENT_CLASS_REGISTRY
        if (value != 'all' and value not in EVENT_CLASS_REGISTRY
                and value not in email_preference_types()):
            raise serializers.ValidationError(_("Unknown notification type."),
                                              code="unknown_event_type")
        return value


class NotificationPreferenceViewSet(viewsets.ModelViewSet):
    """
    CRUD for the caller's own notification preferences.

    GET    /api/notifications/preferences/            list own preferences
    POST   /api/notifications/preferences/            create/update one (upsert by event_type)
    PATCH  /api/notifications/preferences/{id}/       update one
    DELETE /api/notifications/preferences/{id}/       revert to default (enabled)
    GET    /api/notifications/preferences/event_types/  discoverable event types

    What a preference does: it governs DELIVERY — the push (`channels.fcm`) and the
    email (`channels.email`). `event_type: "all"` is the master switch — `is_enabled:
    false` stops every push and every email, `channels: {"fcm": false}` every push,
    `channels: {"email": false}` every email; a type's own row can then stop that type's
    the same way. The in-app inbox always receives the notification, whatever the
    settings (BACKEND_BUGS B-014). An `event_type` that is neither `all` nor one
    `event_types/` lists (in `event_types` or `email_event_types`) answers 400
    `unknown_event_type`.

    Which types send an email at all (the payments spec, section 7): the ones
    `event_types/` lists in `email_event_types` — `payment_confirmed` (the receipt, as a
    PDF), `subscription_expiring` (the 7-day notice only), `subscription_expired`,
    `withdrawal_requested`, `withdrawal_paid`, `withdrawal_failed`. Each of the six is a
    push type as well, listed in `event_types` too, and its row governs both: a plan's
    payment sends the push `payment_confirmed` beside the receipt email, so
    `channels.fcm` on that row is the push and `channels.email` the email (BACKEND_BUGS
    B-168). A routine product's web-hub checkout sends the same receipt email, governed
    by the `payment_confirmed` row, but its pushes are `routine_product_purchased` /
    `routine_product_sold`, never `payment_confirmed`. An email goes only to a verified
    address. `channels.email` on any other type is accepted and changes nothing.
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
        """The event types that can be toggled.

        200 `{"event_types": [...push types..., "all"], "email_event_types": [...]}`.
        `event_types` is every push type (the FCM registry) and `all`; `email_event_types`
        is the types that also send an email, whose `channels.email` therefore means
        something. Each of those is in `event_types` too: `payment_confirmed` is both
        the receipt's push and its email (BACKEND_BUGS B-168). Same answer on every
        build.
        """
        from .channels.email import email_preference_types
        from .channels.fcm import EVENT_CLASS_REGISTRY
        return Response({'event_types': sorted(EVENT_CLASS_REGISTRY.keys()) + ['all'],
                         'email_event_types': email_preference_types()})
