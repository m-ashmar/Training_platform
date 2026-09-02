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
        token = request.data.get('token')
        if not token:
            return Response({'error': _('Token is required')}, status=status.HTTP_400_BAD_REQUEST)

        # Optional device metadata sent by the mobile client.
        platform = request.data.get('platform') or 'android'
        if platform not in dict(DeviceToken.PLATFORM_CHOICES):
            platform = 'android'
        app_version = request.data.get('app_version')
        device_id = request.data.get('device_id')

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
                    app_version=app_version,
                    device_id=device_id,
                    is_active=True,
                )
                logger.info(f"Registered new FCM token for user {request.user.id}")
                return Response({'message': _('Token registered successfully')}, status=status.HTTP_201_CREATED)

        except (Http404, NotFound, PermissionDenied, NotAuthenticated, DRFValidationError):
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
        token = request.data.get('token')
        if not token:
            return Response({'error': _('Token is required')}, status=status.HTTP_400_BAD_REQUEST)
            
        try:
            deleted_count, deleted_info = DeviceToken.objects.filter(
                user=request.user,
                token=token
            ).delete()
            
            if deleted_count > 0:
                logger.info(f"Unregistered FCM token for user {request.user.id}")
                return Response({'message': _('Token unregistered successfully')}, status=status.HTTP_200_OK)
            else:
                return Response({'error': _('Token not found')}, status=status.HTTP_404_NOT_FOUND)
                
        except (Http404, NotFound, PermissionDenied, NotAuthenticated, DRFValidationError):
            # These carry their own status; the broad handler below made them 500s.
            raise
        except Exception as e:
            logger.error(f"Error unregistering FCM token: {e}")
            return Response({'error': _('Failed to unregister token')}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
