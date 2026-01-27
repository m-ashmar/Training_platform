# users/views.py
# NOTE: Password reset endpoints are handled by Django's built-in auth views in urls.py.
# TODO: Implement email/phone verification for production readiness.
from dj_rest_auth.views import LoginView
from dj_rest_auth.registration.views import RegisterView  # Correct import
from .serializers import (
    CustomLoginSerializer, CustomRegisterSerializer, UserDetailsSerializer,
    TrainerProfileSerializer, ClientProfileSerializer, DeviceTokenSerializer
)
from rest_framework.permissions import AllowAny
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny
from rest_framework.views import APIView
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework import status
from rest_framework_simplejwt.tokens import RefreshToken
from rest_framework_simplejwt.exceptions import TokenError
from .models import CustomUser, DeviceToken
from rest_framework import viewsets, mixins
from routine.permissions import IsTrainerOfApprovedClient
from routine.serializers import ClientProfileViewSerializer
import logging
from rest_framework.exceptions import PermissionDenied
from users.utils import send_push_notification
from django.core.cache import cache
from rest_framework_simplejwt.views import TokenObtainPairView
from rest_framework_simplejwt.serializers import TokenObtainPairSerializer
from rest_framework.parsers import MultiPartParser, FormParser

logger = logging.getLogger(__name__)

class CustomLoginView(LoginView):
    serializer_class = CustomLoginSerializer
    permission_classes = [AllowAny]
    def get_response(self):
        original_response = super().get_response()
        user = self.request.user  # Retrieve the authenticated user
        user_info = {
            'id': user.id,
            'username': user.username,
            'email': user.email,
            'first_name': user.first_name,
            'last_name': user.last_name,
            'user_type': user.user_type,
            'profile_picture': user.profile_picture.url if user.profile_picture else None,
        }
        original_response.data.update({'user': user_info})  # Add user info to the response
        return original_response

class JWTAuthLogoutView(APIView):
    """
    JWT Logout View - Blacklists the refresh token and returns success response.
    This is the proper way to logout with JWT authentication.
    """
    permission_classes = [IsAuthenticated]
    
    def post(self, request):
        try:
            # Get the refresh token from the request
            refresh_token = request.data.get('refresh_token')
            
            if not refresh_token:
                return Response(
                    {'error': 'Refresh token is required'}, 
                    status=status.HTTP_400_BAD_REQUEST
                )
            
            # Blacklist the refresh token
            token = RefreshToken(refresh_token)
            token.blacklist()
            
            return Response(
                {'message': 'Successfully logged out'}, 
                status=status.HTTP_200_OK
            )
            
        except TokenError:
            return Response(
                {'error': 'Invalid refresh token'}, 
                status=status.HTTP_400_BAD_REQUEST
            )
        except Exception as e:
            return Response(
                {'error': 'Logout failed'}, 
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )
    
class UpdateUserDetailsView(APIView):
    serializer_class = UserDetailsSerializer
    permission_classes = [IsAuthenticated]

    def get(self, request):
        """Retrieve user details."""
        user = request.user
        serializer = UserDetailsSerializer(user)
        return Response(serializer.data, status=status.HTTP_200_OK)

    def post(self, request):
        """Update user details."""
        user = request.user
        serializer = UserDetailsSerializer(user, data=request.data, partial=True)
        if serializer.is_valid():
            serializer.save()
            return Response({'message': 'Details updated successfully!'}, status=status.HTTP_200_OK)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
    
    
class UserDetailsView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        """Retrieve the authenticated user's details."""
        user = request.user
        user_data = {
            'id': user.id,
            'username': user.username,
            'email': user.email,
            'first_name': user.first_name,
            'last_name': user.last_name,
            'profile_picture': user.profile_picture.url if user.profile_picture else None,
        }
        return Response(user_data, status=status.HTTP_200_OK)

class CustomRegisterView(RegisterView):
    serializer_class = CustomRegisterSerializer
    permission_classes = [AllowAny]
    
    def perform_create(self, serializer):
        """
        Override perform_create to bypass allauth's complete_signup
        which tries to redirect inactive users to account_inactive URL.
        """
        user = serializer.save(self.request)
        # User is already set to is_active=False in the serializer
        return user
    
    def create(self, request, *args, **kwargs):
        """
        Override create to handle registration without allauth's complete_signup flow.
        """
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        
        # Create user (will be inactive)
        user = self.perform_create(serializer)
        
        # Generate and send OTP for email verification
        from .utils import create_otp
        try:
            create_otp(user)
            logger.info(f"OTP sent to {user.email} for user {user.id}")
        except Exception as e:
            logger.error(f"Failed to send OTP to {user.email}: {str(e)}")
            # Continue even if OTP sending fails (user can request resend)
        
        # Return response without tokens (user needs to verify OTP first)
        user_info = {
            'id': user.id,
            'username': user.username,
            'email': user.email,
            'first_name': user.first_name,
            'last_name': user.last_name,
            'user_type': user.user_type,
            'profile_picture': user.profile_picture.url if user.profile_picture else None,
        }
        
        headers = self.get_success_headers(serializer.data)
        
        return Response({
            'user': user_info,
            'message': 'Registration successful. Please check your email for OTP verification code.',
            'requires_verification': True
        }, status=status.HTTP_201_CREATED, headers=headers)

class OTPVerificationView(APIView):
    """
    View to verify OTP code and activate user account.
    After successful verification, returns JWT tokens.
    """
    permission_classes = [AllowAny]
    
    def post(self, request):
        """Verify OTP code and activate user"""
        email = request.data.get('email')
        otp_code = request.data.get('otp_code')
        
        if not email or not otp_code:
            return Response(
                {'error': 'Email and OTP code are required'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        from .utils import verify_otp
        from rest_framework_simplejwt.tokens import RefreshToken
        
        success, otp_instance, error_message = verify_otp(email, otp_code)
        
        if not success:
            return Response(
                {'error': error_message},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        # Activate user
        user = otp_instance.user
        user.is_active = True
        user.save()
        
        # Generate JWT tokens
        refresh = RefreshToken.for_user(user)
        
        user_info = {
            'id': user.id,
            'username': user.username,
            'email': user.email,
            'first_name': user.first_name,
            'last_name': user.last_name,
            'user_type': user.user_type,
            'profile_picture': user.profile_picture.url if user.profile_picture else None,
            'is_active': user.is_active,
            'onboarding_completed': user.is_onboarding_completed,
        }
        
        logger.info(f"User {user.id} ({email}) verified and activated")
        
        return Response({
            'message': 'Email verified successfully. Your account has been activated.',
            'user': user_info,
            'access': str(refresh.access_token),
            'refresh': str(refresh),
        }, status=status.HTTP_200_OK)

class ResendOTPView(APIView):
    """
    View to resend OTP code to user's email.
    Rate limited to 3 requests per hour per email.
    """
    permission_classes = [AllowAny]
    
    def post(self, request):
        """Resend OTP code"""
        email = request.data.get('email')
        
        if not email:
            return Response(
                {'error': 'Email is required'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        from .models import CustomUser
        from .utils import create_otp
        from django.core.cache import cache
        from django.utils import timezone
        
        try:
            user = CustomUser.objects.get(email=email)
        except CustomUser.DoesNotExist:
            return Response(
                {'error': 'User with this email does not exist'},
                status=status.HTTP_404_NOT_FOUND
            )
        
        # Check if user is already verified
        if user.is_active:
            return Response(
                {'error': 'This account is already verified'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        # Rate limiting: max 3 resends per hour per email
        cache_key = f'otp_resend_{email}'
        resend_count = cache.get(cache_key, 0)
        
        if resend_count >= 3:
            return Response(
                {'error': 'Too many OTP requests. Please wait 1 hour before requesting again.'},
                status=status.HTTP_429_TOO_MANY_REQUESTS
            )
        
        # Increment counter and set expiration (1 hour)
        cache.set(cache_key, resend_count + 1, 3600)
        
        # Create and send new OTP
        try:
            create_otp(user)
            logger.info(f"OTP resent to {email} for user {user.id}")
            
            return Response({
                'message': 'OTP code has been resent to your email. Please check your inbox.',
                'email': email,
            }, status=status.HTTP_200_OK)
        except Exception as e:
            logger.error(f"Failed to resend OTP to {email}: {str(e)}")
            return Response(
                {'error': 'Failed to send OTP. Please try again later.'},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )

# ============================================================================
# TRAINER-SPECIFIC VIEWS
# ============================================================================

class TrainerProfileView(APIView):
    """View for trainer profile management"""
    permission_classes = [IsAuthenticated]
    
    def get(self, request):
        """Get trainer profile data"""
        if not request.user.is_trainer:
            return Response(
                {'error': 'This endpoint is only for trainers'}, 
                status=status.HTTP_403_FORBIDDEN
            )
        
        serializer = TrainerProfileSerializer(request.user)
        return Response(serializer.data, status=status.HTTP_200_OK)
    
    def post(self, request):
        """Update trainer profile"""
        if not request.user.is_trainer:
            return Response(
                {'error': 'This endpoint is only for trainers'}, 
                status=status.HTTP_403_FORBIDDEN
            )
        
        serializer = TrainerProfileSerializer(request.user, data=request.data, partial=True)
        if serializer.is_valid():
            serializer.save()
            return Response({'message': 'Trainer profile updated successfully!'}, status=status.HTTP_200_OK)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

class TrainerClientsView(APIView):
    """View for trainer to manage their clients"""
    permission_classes = [IsAuthenticated]
    
    def get(self, request):
        """Get all clients assigned to this trainer"""
        if not request.user.is_trainer:
            return Response(
                {'error': 'This endpoint is only for trainers'}, 
                status=status.HTTP_403_FORBIDDEN
            )
        
        clients = request.user.clients.all()
        client_data = []
        
        for client in clients:
            client_data.append({
                'id': client.id,
                'username': client.username,
                'email': client.email,
                'first_name': client.first_name,
                'last_name': client.last_name,
                'profile_picture': client.profile_picture.url if client.profile_picture else None,
                'height': client.height,
                'weight': client.weight,
                'age': client.age,
                'gender': client.gender,
                'activity_level': client.activity_level,
                'client_goals': client.client_goals,
                'client_preferences': client.client_preferences,
                'date_joined': client.date_joined,
            })
        
        return Response({
            'trainer_id': request.user.id,
            'trainer_name': request.user.full_name,
            'client_count': len(client_data),
            'clients': client_data
        }, status=status.HTTP_200_OK)

class AssignClientView(APIView):
    """View for trainer to assign a client to themselves"""
    permission_classes = [IsAuthenticated]
    
    def post(self, request):
        """Assign a client to this trainer"""
        if not request.user.is_trainer:
            return Response(
                {'error': 'This endpoint is only for trainers'}, 
                status=status.HTTP_403_FORBIDDEN
            )
        
        client_id = request.data.get('client_id')
        if not client_id:
            return Response(
                {'error': 'client_id is required'}, 
                status=status.HTTP_400_BAD_REQUEST
            )
        
        try:
            from .models import TrainerClientRelation
            client = CustomUser.objects.get(id=client_id, user_type='client')
            
            # Check if relation already exists
            relation, created = TrainerClientRelation.objects.get_or_create(
                trainer=request.user,
                client=client,
                defaults={'status': 'pending'}
            )
            
            if not created:
                if relation.status == 'approved':
                    return Response(
                        {'error': 'Client is already assigned to you'}, 
                        status=status.HTTP_400_BAD_REQUEST
                    )
                elif relation.status == 'pending':
                    return Response(
                        {'error': 'Client assignment request is already pending'}, 
                        status=status.HTTP_400_BAD_REQUEST
                    )
                elif relation.status == 'rejected':
                    # Update status to pending for new request
                    relation.status = 'pending'
                    relation.save()
            
            # TODO: Send notification to client about assignment request
            send_push_notification(
                user=client,
                title="Trainer Assignment Request",
                message=f"Trainer {getattr(request.user, 'full_name', None) or request.user.username} has requested to assign you as a client.",
                data={"trainer_id": request.user.id}
            )
            logger.info(f"Trainer {request.user.id} requested assignment of client {client.id}")
            
            # Invalidate client cache
            ClientProfileViewSet.invalidate_client_cache(client.id, request.user.id)
            
            return Response({
                'message': f'Assignment request sent to {client.username}',
                'client_id': client.id,
                'status': 'pending'
            }, status=status.HTTP_200_OK)
            
        except CustomUser.DoesNotExist:
            return Response(
                {'error': 'Client not found'}, 
                status=status.HTTP_404_NOT_FOUND
            )

class UnassignClientView(APIView):
    """View for trainer to unassign a client"""
    permission_classes = [IsAuthenticated]
    
    def post(self, request):
        """Unassign a client from this trainer"""
        if not request.user.is_trainer:
            return Response(
                {'error': 'This endpoint is only for trainers'}, 
                status=status.HTTP_403_FORBIDDEN
            )
        
        client_id = request.data.get('client_id')
        if not client_id:
            return Response(
                {'error': 'client_id is required'}, 
                status=status.HTTP_400_BAD_REQUEST
            )
        
        try:
            from .models import TrainerClientRelation
            client = CustomUser.objects.get(id=client_id, user_type='client')
            
            # Check if relation exists
            try:
                relation = TrainerClientRelation.objects.get(
                    trainer=request.user,
                    client=client
                )
                
                # Update client's assigned_trainer to None
                client.assigned_trainer = None
                client.save()
                
                # Delete the relation
                relation.delete()
                
                # TODO: Send notification to client about unassignment
                send_push_notification(
                    user=client,
                    title="Trainer Unassignment",
                    message=f"Trainer {getattr(request.user, 'full_name', None) or request.user.username} has unassigned you.",
                    data={"trainer_id": request.user.id}
                )
                logger.info(f"Trainer {request.user.id} unassigned client {client.id}")
                
                # Invalidate client cache
                ClientProfileViewSet.invalidate_client_cache(client.id, request.user.id)
                
                return Response({
                    'message': f'Client {client.username} unassigned successfully',
                    'client_id': client.id
                }, status=status.HTTP_200_OK)
                
            except TrainerClientRelation.DoesNotExist:
                return Response(
                    {'error': 'Client is not assigned to you'}, 
                    status=status.HTTP_400_BAD_REQUEST
                )
            
        except CustomUser.DoesNotExist:
            return Response(
                {'error': 'Client not found'}, 
                status=status.HTTP_404_NOT_FOUND
            )

# ============================================================================
# CLIENT-SPECIFIC VIEWS
# ============================================================================

class ClientProfileView(APIView):
    """View for client profile management"""
    permission_classes = [IsAuthenticated]
    
    def get(self, request):
        """Get client profile data"""
        if not request.user.is_client:
            return Response(
                {'error': 'This endpoint is only for clients'}, 
                status=status.HTTP_403_FORBIDDEN
            )
        
        serializer = ClientProfileSerializer(request.user)
        return Response(serializer.data, status=status.HTTP_200_OK)
    
    def post(self, request):
        """Update client profile"""
        if not request.user.is_client:
            return Response(
                {'error': 'This endpoint is only for clients'}, 
                status=status.HTTP_403_FORBIDDEN
            )
        
        serializer = ClientProfileSerializer(request.user, data=request.data, partial=True)
        if serializer.is_valid():
            serializer.save()
            return Response({'message': 'Client profile updated successfully!'}, status=status.HTTP_200_OK)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

class PublicTrainersListView(APIView):
    """Public view to get all available trainers without authentication"""
    permission_classes = [AllowAny]
    
    def get(self, request):
        """Get all available trainers (public endpoint)"""
        # Get all available trainers with client count annotated
        from django.db.models import Count
        trainers = CustomUser.objects.filter(
            user_type='trainer',
            trainer_is_available=True,
            is_active=True
        ).annotate(
            client_count_anno=Count('clients')
        )
        
        trainer_data = []
        for trainer in trainers:
            trainer_data.append({
                'id': trainer.id,
                'username': trainer.username,
                'first_name': trainer.first_name,
                'last_name': trainer.last_name,
                'profile_picture': trainer.profile_picture.url if trainer.profile_picture else None,
                'trainer_bio': trainer.trainer_bio,
                'trainer_specializations': trainer.trainer_specializations,
                'trainer_certifications': trainer.trainer_certifications,
                'trainer_experience_years': trainer.trainer_experience_years,
                'trainer_hourly_rate': trainer.trainer_hourly_rate,
                'trainer_is_verified': trainer.trainer_is_verified,
                'client_count': trainer.client_count_anno,
            })
        
        return Response({
            'available_trainers': trainer_data,
            'trainer_count': len(trainer_data)
        }, status=status.HTTP_200_OK)

class PublicTrainerClientStatsView(APIView):
    """Public view to get statistics about clients with trainers and total trainers"""
    permission_classes = [AllowAny]
    
    def get(self, request):
        """Get statistics: number of clients with trainers and total number of trainers"""
        # Count clients who have assigned trainers
        clients_with_trainers_count = CustomUser.objects.filter(
            user_type='client',
            assigned_trainer__isnull=False,
            is_active=True
        ).count()
        
        # Count total active trainers
        total_trainers_count = CustomUser.objects.filter(
            user_type='trainer',
            is_active=True
        ).count()
        
        return Response({
            'clients_with_trainers_count': clients_with_trainers_count,
            'total_trainers_count': total_trainers_count
        }, status=status.HTTP_200_OK)

class AvailableTrainersView(APIView):
    """View for clients to see available trainers"""
    permission_classes = [IsAuthenticated]
    
    def get(self, request):
        """Get all available trainers"""
        if not request.user.is_client:
            return Response(
                {'error': 'This endpoint is only for clients'}, 
                status=status.HTTP_403_FORBIDDEN
            )
        
        # Get all available trainers with client count annotated
        # Use Count('clients') to get the number of reverse relationships
        from django.db.models import Count
        
        trainers = CustomUser.objects.filter(
            user_type='trainer',
            trainer_is_available=True,
            is_active=True
        ).annotate(
            client_count_anno=Count('clients')
        )
        
        trainer_data = []
        for trainer in trainers:
            trainer_data.append({
                'id': trainer.id,
                'username': trainer.username,
                'email': trainer.email,
                'first_name': trainer.first_name,
                'last_name': trainer.last_name,
                'profile_picture': trainer.profile_picture.url if trainer.profile_picture else None,
                'trainer_bio': trainer.trainer_bio,
                'trainer_specializations': trainer.trainer_specializations,
                'trainer_certifications': trainer.trainer_certifications,
                'trainer_experience_years': trainer.trainer_experience_years,
                'trainer_hourly_rate': trainer.trainer_hourly_rate,
                'trainer_is_verified': trainer.trainer_is_verified,
                'client_count': trainer.client_count_anno, # Use annotated value
            })
        
        return Response({
            'client_id': request.user.id,
            'available_trainers': trainer_data,
            'trainer_count': len(trainer_data)
        }, status=status.HTTP_200_OK)

class ClientProfileViewSet(mixins.RetrieveModelMixin, viewsets.GenericViewSet):
    """
    Enhanced read-only viewset for trainers to view approved client profiles.
    
    Features:
    - Returns personal data: weight, height, age, gender, activity_level
    - Calculated metrics: BMI, BMR, TDEE, goals
    - Training/diet history
    - Access protected by IsTrainerOfApprovedClient permission
    
    TODO: Add comprehensive training history tracking
    TODO: Implement progress analytics and reporting
    TODO: Add goal achievement tracking
    TODO: Implement data privacy and audit logging
    """
    queryset = CustomUser.objects.filter(user_type='client')
    serializer_class = ClientProfileViewSerializer
    permission_classes = [IsTrainerOfApprovedClient]

    def get_queryset(self):
        user = self.request.user
        cache_key = f"trainer_{user.id}_approved_clients"
        queryset = cache.get(cache_key)
        if queryset is None:
            if user.is_admin:
                queryset = self.queryset.all()
            elif user.is_trainer:
                from .models import TrainerClientRelation
                approved_clients = TrainerClientRelation.objects.filter(
                    trainer=user,
                    status='approved'
                ).values_list('client_id', flat=True)
                queryset = self.queryset.filter(id__in=approved_clients)
            else:
                queryset = self.queryset.none()
            cache.set(cache_key, queryset, timeout=300)  # 5 min
        return queryset

    def retrieve(self, request, *args, **kwargs):
        pk = kwargs.get('pk')
        # Enforce permission at object level for trainers: must be approved relation
        # Fetch from base queryset (all clients), not the filtered one, so we can return 403 instead of 404
        try:
            obj = self.queryset.get(pk=pk)
        except self.queryset.model.DoesNotExist:
            return Response({'error': 'Client not found'}, status=status.HTTP_404_NOT_FOUND)
        # Object permission is already enforced by permission_classes but ensure 403 explicitly
        has_perm = all(perm().has_object_permission(request, self, obj) for perm in self.permission_classes)
        if not has_perm:
            return Response({'error': 'Forbidden'}, status=status.HTTP_403_FORBIDDEN)
        cache_key = f"client_profile_{pk}"
        data = cache.get(cache_key)
        if data is not None:
            return Response(data)
        serializer = self.get_serializer(obj)
        data = serializer.data
        cache.set(cache_key, data, timeout=300)
        return Response(data)

    def list(self, request, *args, **kwargs):
        """
        List all approved clients for the trainer.
        
        TODO: Add pagination for large client lists
        TODO: Implement filtering and search functionality
        TODO: Add sorting options
        """
        try:
            # Filter queryset to only show approved clients for trainers
            user = request.user
            if user.is_admin:
                queryset = self.get_queryset()
            elif user.is_trainer:
                from .models import TrainerClientRelation
                approved_clients = TrainerClientRelation.objects.filter(
                    trainer=user,
                    status='approved'
                ).values_list('client_id', flat=True)
                queryset = self.get_queryset().filter(id__in=approved_clients)
            else:
                queryset = self.get_queryset().none()
            
            # Log the access
            logger.info(f"Trainer {request.user.id} listed their approved clients")
            
            serializer = self.get_serializer(queryset, many=True)
            return Response({
                'trainer_id': request.user.id,
                'client_count': queryset.count(),
                'clients': serializer.data
            })
            
        except Exception as e:
            logger.error(f"Error listing client profiles: {str(e)}")
            return Response(
                {'error': 'An error occurred while listing client profiles'}, 
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )

    @staticmethod
    def invalidate_client_cache(client_id, trainer_id=None):
        cache.delete(f"client_profile_{client_id}")
        if trainer_id:
            cache.delete(f"trainer_{trainer_id}_approved_clients")

class DeviceTokenRegisterView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        token = request.data.get('token')
        if not token:
            return Response({'error': 'Token is required.'}, status=status.HTTP_400_BAD_REQUEST)
        device_token, created = DeviceToken.objects.update_or_create(
            user=request.user, token=token,
            defaults={}
        )
        serializer = DeviceTokenSerializer(device_token)
        return Response(serializer.data, status=status.HTTP_201_CREATED if created else status.HTTP_200_OK)

class CustomTokenObtainPairSerializer(TokenObtainPairSerializer):
    """Custom JWT token serializer that includes user information"""
    
    def validate(self, attrs):
        # Authenticate user first
        from django.contrib.auth import authenticate
        from .models import CustomUser
        
        email = attrs.get('email') or attrs.get('username')  # Support both email and username
        password = attrs.get('password')
        
        if email and password:
            try:
                user = CustomUser.objects.get(email=email)
            except CustomUser.DoesNotExist:
                user = None
            
            if user and not user.is_active:
                from rest_framework import serializers
                raise serializers.ValidationError(
                    {
                        "detail": "Please verify your email address before logging in. Check your inbox for the OTP code.",
                        "requires_verification": True,
                        "email": user.email
                    }
                )
        
        # Call the parent validate method to get the standard token response
        data = super().validate(attrs)
        
        # Add user information to the response
        user = self.user
        user_info = {
            'id': user.id,
            'username': user.username,
            'email': user.email,
            'first_name': user.first_name,
            'last_name': user.last_name,
            'user_type': user.user_type,
            'is_active': user.is_active,
            'onboarding_completed': user.is_onboarding_completed,
        }
        
        # Add user info to the response
        data['user'] = user_info
        return data


class CustomTokenObtainPairView(TokenObtainPairView):
    """Custom JWT token view that includes user information in the response"""
    serializer_class = CustomTokenObtainPairSerializer

# ============================================================================
# CLIENT-TRAINER REQUEST SYSTEM
# ============================================================================

class ClientRequestTrainerView(APIView):
    """
    Enhanced view for clients to request trainer assignment.
    
    Features:
    - Client can send request to specific trainer
    - Prevents duplicate requests
    - Sends notification to trainer
    - Proper error handling and validation
    
    TODO: Add request message support
    TODO: Implement request expiration
    TODO: Add request history tracking
    """
    permission_classes = [IsAuthenticated]
    
    def post(self, request):
        """Client requests to be assigned to a trainer"""
        if not request.user.is_client:
            return Response(
                {'error': 'This endpoint is only for clients'}, 
                status=status.HTTP_403_FORBIDDEN
            )
        
        trainer_id = request.data.get('trainer_id')
        message = request.data.get('message', '')
        
        if not trainer_id:
            return Response(
                {'error': 'trainer_id is required'}, 
                status=status.HTTP_400_BAD_REQUEST
            )
        
        try:
            from .models import TrainerClientRelation
            trainer = CustomUser.objects.get(id=trainer_id, user_type='trainer')
            # Enforce wallet balance >= trainer charge before allowing request
            try:
                from wallet.models import Wallet
                trainer_charge = getattr(trainer, 'trainer_hourly_rate', None) or 0
                if trainer_charge and trainer_charge > 0:
                    client_wallet, _ = Wallet.objects.get_or_create(owner=request.user, defaults={"owner_type": "client"})
                    if client_wallet.balance < trainer_charge:
                        return Response(
                            {'error': 'Insufficient wallet balance to request this trainer', 'required': str(trainer_charge), 'balance': str(client_wallet.balance)},
                            status=status.HTTP_402_PAYMENT_REQUIRED
                        )
            except Exception:
                pass
            
            # Check if trainer is available
            if not trainer.trainer_is_available:
                return Response(
                    {'error': 'This trainer is not currently accepting new clients'}, 
                    status=status.HTTP_400_BAD_REQUEST
                )
            
            # Enforce single-trainer rule: if client already assigned, block new requests
            if request.user.assigned_trainer is not None and request.user.assigned_trainer_id != trainer.id:
                return Response(
                    {'error': 'You are already assigned to a trainer. Unassign first to request another trainer.'},
                    status=status.HTTP_400_BAD_REQUEST
                )

            # Also block if there is any other pending request with a different trainer
            has_pending_elsewhere = TrainerClientRelation.objects.filter(
                client=request.user,
                status='pending'
            ).exclude(trainer_id=trainer.id).exists()
            if has_pending_elsewhere:
                return Response(
                    {'error': 'You already have a pending request with another trainer. Please wait for a response or cancel it first.'},
                    status=status.HTTP_400_BAD_REQUEST
                )
            
            # Check if relation already exists
            relation, created = TrainerClientRelation.objects.get_or_create(
                trainer=trainer,
                client=request.user,
                defaults={'status': 'pending'}
            )
            
            if not created:
                if relation.status == 'approved':
                    return Response(
                        {'error': 'You are already assigned to this trainer'}, 
                        status=status.HTTP_400_BAD_REQUEST
                    )
                elif relation.status == 'pending':
                    return Response(
                        {'error': 'Request is already pending with this trainer'}, 
                        status=status.HTTP_400_BAD_REQUEST
                    )
                elif relation.status == 'rejected':
                    # Allow new request after rejection
                    relation.status = 'pending'
                    relation.save()
            
            # Send notification to trainer
            send_push_notification(
                user=trainer,
                title="New Client Request",
                message=f"Client {request.user.full_name or request.user.username} has requested to work with you.",
                data={
                    "client_id": request.user.id,
                    "client_name": request.user.full_name or request.user.username,
                    "message": message
                }
            )
            
            logger.info(f"Client {request.user.id} requested trainer {trainer.id}")
            
            return Response({
                'message': f'Request sent to trainer {trainer.full_name or trainer.username}',
                'trainer_id': trainer.id,
                'status': 'pending'
            }, status=status.HTTP_200_OK)
            
        except CustomUser.DoesNotExist:
            return Response(
                {'error': 'Trainer not found'}, 
                status=status.HTTP_404_NOT_FOUND
            )
        except Exception as e:
            logger.error(f"Error in client request: {str(e)}")
            return Response(
                {'error': 'An error occurred while sending the request'}, 
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )


class TrainerPendingRequestsView(APIView):
    """
    Enhanced view for trainers to see pending client requests.
    
    Features:
    - Shows all pending requests for the trainer
    - Includes client information and request details
    - Supports pagination for large request lists
    - Proper filtering and sorting
    
    TODO: Add request filtering by date
    TODO: Implement request search functionality
    TODO: Add request analytics and reporting
    """
    permission_classes = [IsAuthenticated]
    
    def get(self, request):
        """Get all pending client requests for this trainer"""
        if not request.user.is_trainer:
            return Response(
                {'error': 'This endpoint is only for trainers'}, 
                status=status.HTTP_403_FORBIDDEN
            )
        
        try:
            from .models import TrainerClientRelation
            
            # Get pending requests
            pending_relations = TrainerClientRelation.objects.filter(
                trainer=request.user,
                status='pending'
            ).select_related('client').order_by('-created_at')
            
            requests_data = []
            for relation in pending_relations:
                client = relation.client
                requests_data.append({
                    'request_id': relation.id,
                    'client_id': client.id,
                    'client_name': client.full_name or client.username,
                    'client_email': client.email,
                    'client_username': client.username,
                    'client_profile_picture': client.profile_picture.url if client.profile_picture else None,
                    'client_age': client.age,
                    'client_gender': client.gender,
                    'client_goals': client.client_goals,
                    'client_activity_level': client.activity_level,
                    'requested_at': relation.created_at,
                    'status': relation.status
                })
            
            return Response({
                'trainer_id': request.user.id,
                'trainer_name': request.user.full_name or request.user.username,
                'pending_requests_count': len(requests_data),
                'pending_requests': requests_data
            }, status=status.HTTP_200_OK)
            
        except Exception as e:
            logger.error(f"Error fetching pending requests: {str(e)}")
            return Response(
                {'error': 'An error occurred while fetching pending requests'}, 
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )


class TrainerRespondToRequestView(APIView):
    """
    Enhanced view for trainers to approve or reject client requests.
    
    Features:
    - Approve or reject client requests
    - Automatic client assignment on approval
    - Sends notifications to clients
    - Updates relationship status
    - Proper validation and error handling
    
    TODO: Add approval/rejection reasons
    TODO: Implement request history tracking
    TODO: Add approval analytics
    """
    permission_classes = [IsAuthenticated]
    
    def post(self, request):
        """Trainer responds to a client request (approve/reject)"""
        if not request.user.is_trainer:
            return Response(
                {'error': 'This endpoint is only for trainers'}, 
                status=status.HTTP_403_FORBIDDEN
            )
        
        request_id = request.data.get('request_id')
        action = request.data.get('action')  # 'approve' or 'reject'
        reason = request.data.get('reason', '')
        
        if not request_id:
            return Response(
                {'error': 'request_id is required'}, 
                status=status.HTTP_400_BAD_REQUEST
            )
        
        if action not in ['approve', 'reject']:
            return Response(
                {'error': 'action must be either "approve" or "reject"'}, 
                status=status.HTTP_400_BAD_REQUEST
            )
        
        try:
            from .models import TrainerClientRelation
            
            # Get the request
            relation = TrainerClientRelation.objects.get(
                id=request_id,
                trainer=request.user,
                status='pending'
            )
            
            client = relation.client
            
            if action == 'approve':
                # Prevent approving if client already assigned to another trainer
                if client.assigned_trainer and client.assigned_trainer_id != request.user.id:
                    return Response(
                        {'error': 'Client is already assigned to another trainer'},
                        status=status.HTTP_400_BAD_REQUEST
                    )
                # Prevent multiple approved relations for same client
                already_approved_elsewhere = TrainerClientRelation.objects.filter(
                    client=client,
                    status='approved'
                ).exclude(trainer=request.user).exists()
                if already_approved_elsewhere:
                    return Response(
                        {'error': 'Client already has an approved relation with another trainer'},
                        status=status.HTTP_400_BAD_REQUEST
                    )
                # Approve the request
                relation.status = 'approved'
                relation.save()
                
                # Assign client to trainer
                client.assigned_trainer = request.user
                client.save()

                # Hold funds in escrow equal to trainer charge
                try:
                    from wallet.models import Wallet, move_funds_atomic
                    from wallet.utils import get_escrow_wallet
                    trainer_charge = getattr(request.user, 'trainer_hourly_rate', None) or 0
                    if trainer_charge and trainer_charge > 0:
                        client_wallet, _ = Wallet.objects.get_or_create(owner=client, defaults={"owner_type": "client"})
                        if client_wallet.balance < trainer_charge:
                            return Response(
                                {'error': 'Insufficient client wallet balance for trainer charge hold'},
                                status=status.HTTP_402_PAYMENT_REQUIRED
                            )
                        escrow = get_escrow_wallet()
                        move_funds_atomic(client_wallet, escrow, trainer_charge, actor_id=request.user.id, tx_type='transfer', metadata={'purpose': 'trainer_request_hold', 'trainer_id': request.user.id, 'client_id': client.id})
                except Exception as e:
                    logger.error(f"Wallet hold failed: {str(e)}")
                
                # Send approval notification to client
                send_push_notification(
                    user=client,
                    title="Trainer Request Approved!",
                    message=f"Trainer {request.user.full_name or request.user.username} has approved your request!",
                    data={
                        "trainer_id": request.user.id,
                        "trainer_name": request.user.full_name or request.user.username,
                        "status": "approved"
                    }
                )
                
                logger.info(f"Trainer {request.user.id} approved client {client.id}")
                
                return Response({
                    'message': f'Request from {client.full_name or client.username} approved successfully',
                    'client_id': client.id,
                    'status': 'approved'
                }, status=status.HTTP_200_OK)
                
            else:  # reject
                # Reject the request
                relation.status = 'rejected'
                relation.save()
                
                # Send rejection notification to client
                rejection_message = f"Trainer {request.user.full_name or request.user.username} has declined your request."
                if reason:
                    rejection_message += f" Reason: {reason}"
                
                send_push_notification(
                    user=client,
                    title="Trainer Request Declined",
                    message=rejection_message,
                    data={
                        "trainer_id": request.user.id,
                        "trainer_name": request.user.full_name or request.user.username,
                        "status": "rejected",
                        "reason": reason
                    }
                )
                
                logger.info(f"Trainer {request.user.id} rejected client {client.id}")
                
                return Response({
                    'message': f'Request from {client.full_name or client.username} rejected',
                    'client_id': client.id,
                    'status': 'rejected'
                }, status=status.HTTP_200_OK)
                
        except TrainerClientRelation.DoesNotExist:
            return Response(
                {'error': 'Request not found or already processed'}, 
                status=status.HTTP_404_NOT_FOUND
            )
        except Exception as e:
            logger.error(f"Error responding to request: {str(e)}")
            return Response(
                {'error': 'An error occurred while processing the request'}, 
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )


class ClientRequestStatusView(APIView):
    """
    Enhanced view for clients to check their request status.
    
    Features:
    - Shows all requests made by the client
    - Includes request status and trainer information
    - Supports request history tracking
    - Proper error handling
    
    TODO: Add request filtering by status
    TODO: Implement request cancellation
    TODO: Add request analytics
    """
    permission_classes = [IsAuthenticated]
    
    def get(self, request):
        """Get all requests made by this client"""
        if not request.user.is_client:
            return Response(
                {'error': 'This endpoint is only for clients'}, 
                status=status.HTTP_403_FORBIDDEN
            )
        
        try:
            from .models import TrainerClientRelation
            
            # Get all requests made by this client
            client_relations = TrainerClientRelation.objects.filter(
                client=request.user
            ).select_related('trainer').order_by('-created_at')
            
            requests_data = []
            for relation in client_relations:
                trainer = relation.trainer
                requests_data.append({
                    'request_id': relation.id,
                    'trainer_id': trainer.id,
                    'trainer_name': trainer.full_name or trainer.username,
                    'trainer_email': trainer.email,
                    'trainer_profile_picture': trainer.profile_picture.url if trainer.profile_picture else None,
                    'trainer_bio': trainer.trainer_bio,
                    'trainer_specializations': trainer.trainer_specializations,
                    'trainer_experience_years': trainer.trainer_experience_years,
                    'trainer_hourly_rate': trainer.trainer_hourly_rate,
                    'requested_at': relation.created_at,
                    'status': relation.status
                })
            
            return Response({
                'client_id': request.user.id,
                'client_name': request.user.full_name or request.user.username,
                'total_requests': len(requests_data),
                'requests': requests_data
            }, status=status.HTTP_200_OK)
            
        except Exception as e:
            logger.error(f"Error fetching client requests: {str(e)}")
            return Response(
                {'error': 'An error occurred while fetching requests'}, 
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )

class ProfilePictureUploadView(APIView):
    """
    Dedicated endpoint for uploading profile pictures.
    Accepts multipart/form-data with image file.
    """
    permission_classes = [IsAuthenticated]
    parser_classes = [MultiPartParser, FormParser]

    def post(self, request):
        """Upload a new profile picture"""
        try:
            # Get the uploaded file
            image_file = request.FILES.get('profile_picture')
            
            if not image_file:
                return Response(
                    {'error': 'No image file provided. Please include a file with key "profile_picture"'},
                    status=status.HTTP_400_BAD_REQUEST
                )

            # Validate file type
            allowed_types = ['image/jpeg', 'image/jpg', 'image/png', 'image/webp']
            if image_file.content_type not in allowed_types:
                return Response(
                    {'error': f'Invalid file type. Allowed types: {", ".join(allowed_types)}'},
                    status=status.HTTP_400_BAD_REQUEST
                )

            # Validate file size (2MB limit)
            if image_file.size > 2 * 1024 * 1024:
                return Response(
                    {'error': 'File size too large. Maximum size is 2MB'},
                    status=status.HTTP_400_BAD_REQUEST
                )

            # Update user's profile picture
            user = request.user
            user.profile_picture = image_file
            user.save()

            # Return the updated user info
            return Response({
                'message': 'Profile picture uploaded successfully',
                'profile_picture_url': user.profile_picture.url if user.profile_picture else None,
                'user': {
                    'id': user.id,
                    'username': user.username,
                    'email': user.email,
                    'first_name': user.first_name,
                    'last_name': user.last_name,
                    'user_type': user.user_type,
                    'profile_picture': user.profile_picture.url if user.profile_picture else None,
                }
            }, status=status.HTTP_200_OK)

        except Exception as e:
            return Response(
                {'error': f'Upload failed: {str(e)}'},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )

    def delete(self, request):
        """Remove profile picture"""
        try:
            user = request.user
            if user.profile_picture:
                # Delete the file from storage
                user.profile_picture.delete(save=False)
                user.profile_picture = None
                user.save()
                
                return Response({
                    'message': 'Profile picture removed successfully'
                }, status=status.HTTP_200_OK)
            else:
                return Response({
                    'message': 'No profile picture to remove'
                }, status=status.HTTP_404_NOT_FOUND)
                
        except Exception as e:
            return Response(
                {'error': f'Failed to remove profile picture: {str(e)}'},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )

 
