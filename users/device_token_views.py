from rest_framework import status
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from drf_yasg.utils import swagger_auto_schema
from drf_yasg import openapi
from .models import DeviceToken
import logging

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
            return Response({'error': 'Token is required'}, status=status.HTTP_400_BAD_REQUEST)
        
        try:
            # Check if token exists
            device_token = DeviceToken.objects.filter(token=token).first()
            
            if device_token:
                if device_token.user != request.user:
                    # Token exists but belongs to another user - reassignment needed
                    previous_user = device_token.user
                    device_token.user = request.user
                    device_token.save()
                    logger.info(f"Reassigned FCM token from user {previous_user.id} to user {request.user.id}")
                    return Response({'message': 'Token reassigned successfully'}, status=status.HTTP_200_OK)
                else:
                    # Token exists and belongs to current user
                    return Response({'message': 'Token already registered'}, status=status.HTTP_200_OK)
            else:
                # Token does not exist - create new
                DeviceToken.objects.create(user=request.user, token=token)
                logger.info(f"Registered new FCM token for user {request.user.id}")
                return Response({'message': 'Token registered successfully'}, status=status.HTTP_201_CREATED)
                
        except Exception as e:
            logger.error(f"Error registering FCM token: {e}")
            return Response({'error': 'Failed to register token'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

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
            return Response({'error': 'Token is required'}, status=status.HTTP_400_BAD_REQUEST)
            
        try:
            deleted_count, _ = DeviceToken.objects.filter(
                user=request.user,
                token=token
            ).delete()
            
            if deleted_count > 0:
                logger.info(f"Unregistered FCM token for user {request.user.id}")
                return Response({'message': 'Token unregistered successfully'}, status=status.HTTP_200_OK)
            else:
                return Response({'error': 'Token not found'}, status=status.HTTP_404_NOT_FOUND)
                
        except Exception as e:
            logger.error(f"Error unregistering FCM token: {e}")
            return Response({'error': 'Failed to unregister token'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
