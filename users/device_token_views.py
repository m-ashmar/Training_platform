from rest_framework import status
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from django.utils.translation import gettext as _
from drf_yasg.utils import swagger_auto_schema
from drf_yasg import openapi
from .models import DeviceToken
import logging
from django.http import Http404
from rest_framework.exceptions import (NotAuthenticated, NotFound, PermissionDenied,
                                       ValidationError as DRFValidationError)
from training_platform.api_exceptions import PASSTHROUGH_EXCEPTIONS

logger = logging.getLogger(__name__)

class FCMTokenView(APIView):
    """
    API endpoint to manage Firebase Cloud Messaging (FCM) device tokens.
    """
    permission_classes = [IsAuthenticated]

    @swagger_auto_schema(
        operation_description="Register a new FCM device token",
        request_body=openapi.Schema(
            type=openapi.TYPE_OBJECT,
            required=['token'],
            properties={
                'token': openapi.Schema(type=openapi.TYPE_STRING, description='FCM Device Token'),
            }
        ),
        responses={201: 'Token registered', 200: 'Token already exists', 400: 'Invalid input'}
    )
    def post(self, request):
        """Body: `token` (required, text up to 255), optional `platform`
        (android | ios | web), `app_version` (up to 50), `device_id` (up to 255).
        201 registered, 200 already registered or moved to this account. 400
        `token_required` without a token (with `field`; the code `DELETE` answers — it
        was the generic `bad_request`, the owner, 2026-09-24), `field_too_long` /
        `invalid_field` with `field` — these
        reached the database unchecked and came back as a generic 500, so the device
        silently never registered (BACKEND_BUGS B-025).

        **The build is recorded** (Road to 10 R1, 2026-11-24): send `X-App-Channel` and
        `X-App-Version` here as on every route. The token keeps the channel it was
        registered from (`DeviceToken.app_channel`), and the version from the header
        when the body carries no `app_version`. A push then gives this device the text
        its build is shown (`notifications/build_gate.py`): on a store build three
        notices read differently or are not pushed (see `GET /api/social/notifications/`).
        A token registered with no known channel is the strictest build, or the ios
        build when `platform` is `ios`. Re-registering an existing token records the
        channel again; a request that declares none leaves the recorded one."""
        token = request.data.get('token')
        if not token:
            message = _('Token is required')
            return Response({'error': message, 'detail': message,
                             'code': 'token_required', 'field': 'token'},
                            status=status.HTTP_400_BAD_REQUEST)
        for name, limit in (('token', 255), ('app_version', 50), ('device_id', 255)):
            value = request.data.get(name)
            if value in (None, ''):
                continue
            if not isinstance(value, str):
                return Response({'error': _('%(field)s must be text.') % {'field': name},
                                 'code': 'invalid_field', 'field': name},
                                status=status.HTTP_400_BAD_REQUEST)
            if len(value) > limit:
                return Response({'error': _('%(field)s is longer than %(limit)s characters.')
                                 % {'field': name, 'limit': limit},
                                 'code': 'field_too_long', 'field': name},
                                status=status.HTTP_400_BAD_REQUEST)

        # Optional device metadata sent by the mobile client.
        # Text only, compared lower-cased: a list raised TypeError (unhashable) as a 500,
        # and "iOS" was stored as android.
        platform = request.data.get('platform')
        platform = platform.strip().lower() if isinstance(platform, str) else ''
        if platform not in dict(DeviceToken.PLATFORM_CHOICES):
            platform = 'android'
        app_version = request.data.get('app_version')
        device_id = request.data.get('device_id')
        # The build this device runs (Road to 10 R1): the request's own declaration, the
        # one every route reads (`policy.channels`). The version from the header when the
        # body has none.
        from policy.channels import VERSION_HEADER, channel_of

        app_channel = channel_of(request)
        if not app_version:
            header = (request.headers.get(VERSION_HEADER) or '').strip()
            app_version = header[:50] or None

        try:
            # Check if token exists
            device_token = DeviceToken.objects.filter(token=token).first()

            if device_token:
                reassigned = device_token.user_id != request.user.id
                previous_user_id = device_token.user_id
                device_token.user = request.user
                # Re-registering ALWAYS reactivates: a token previously soft-deleted
                # as invalid would otherwise stay inactive forever and the device
                # would silently never receive push again.
                device_token.is_active = True
                device_token.platform = platform
                if app_channel:
                    device_token.app_channel = app_channel
                if app_version:
                    device_token.app_version = app_version
                if device_id:
                    device_token.device_id = device_id
                device_token.save()

                if reassigned:
                    logger.info(f"Reassigned FCM token from user {previous_user_id} to user {request.user.id}")
                    return Response({'message': _('Token reassigned successfully')}, status=status.HTTP_200_OK)
                return Response({'message': _('Token already registered')}, status=status.HTTP_200_OK)
            else:
                # Token does not exist - create new
                DeviceToken.objects.create(
                    user=request.user,
                    token=token,
                    platform=platform,
                    app_channel=app_channel,
                    app_version=app_version,
                    device_id=device_id,
                    is_active=True,
                )
                logger.info(f"Registered new FCM token for user {request.user.id}")
                return Response({'message': _('Token registered successfully')}, status=status.HTTP_201_CREATED)

        except PASSTHROUGH_EXCEPTIONS:
            # These carry their own status; the broad handler below made them 500s.
            raise
        except Exception as e:
            logger.error(f"Error registering FCM token: {e}")
            return Response({'error': _('Failed to register token')}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    @swagger_auto_schema(
        operation_description="Unregister an FCM device token",
        request_body=openapi.Schema(
            type=openapi.TYPE_OBJECT,
            required=['token'],
            properties={
                'token': openapi.Schema(type=openapi.TYPE_STRING, description='FCM Device Token to remove'),
            }
        ),
        responses={200: 'Token removed', 404: 'Token not found'}
    )
    def delete(self, request):
        """Unregister one of this account's device tokens (push master switch off, sign
        out). Body (JSON): `token` (required).

        200: `{message}`.

        | status | `code` | when |
        |---|---|---|
        | `400` | `token_required` | no `token`, with `field` (BACKEND_BUGS B-054: it had no code of its own) |
        | `404` | `token_not_found` | this account has no such token — already gone, or never registered here (B-054) |
        | `401` | `not_authenticated`, `token_not_valid` | no token; an expired or invalid one |
        | `500` | `server_error` | the database failed |
        """
        token = request.data.get('token')
        if not token:
            message = _('Token is required')
            return Response({'error': message, 'detail': message,
                             'code': 'token_required', 'field': 'token'},
                            status=status.HTTP_400_BAD_REQUEST)

        try:
            deleted_count, deleted_info = DeviceToken.objects.filter(
                user=request.user,
                token=token
            ).delete()
            
            if deleted_count > 0:
                logger.info(f"Unregistered FCM token for user {request.user.id}")
                return Response({'message': _('Token unregistered successfully')}, status=status.HTTP_200_OK)
            else:
                message = _('Token not found')
                return Response({'error': message, 'detail': message,
                                 'code': 'token_not_found'},
                                status=status.HTTP_404_NOT_FOUND)
                
        except PASSTHROUGH_EXCEPTIONS:
            # These carry their own status; the broad handler below made them 500s.
            raise
        except Exception as e:
            logger.error(f"Error unregistering FCM token: {e}")
            return Response({'error': _('Failed to unregister token')}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
