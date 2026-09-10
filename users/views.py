# users/views.py
# Password reset is handled via OTP-based REST API endpoints (PasswordResetRequestView, etc.).
from dj_rest_auth.views import LoginView
from dj_rest_auth.registration.views import RegisterView  # Correct import
from .serializers import (
    CustomLoginSerializer, CustomRegisterSerializer, UserDetailsSerializer,
    TrainerProfileSerializer, ClientProfileSerializer
)
from rest_framework.permissions import AllowAny
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny
from rest_framework.views import APIView
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework import status
from django.utils.translation import gettext as _
from rest_framework_simplejwt.tokens import RefreshToken
from rest_framework_simplejwt.exceptions import TokenError
from .models import CustomUser
from rest_framework import viewsets, mixins
from routine.permissions import IsTrainerOfApprovedClient
from routine.serializers import ClientProfileViewSerializer
import logging
from rest_framework.exceptions import PermissionDenied
from notifications.domain.dispatcher import emit_event
from notifications.domain.trainer_client_events import (
    TrainerAssignmentRequestEvent,
    TrainerUnassignmentEvent,
    ClientRequestReceivedEvent,
    ClientRequestApprovedEvent,
    ClientRequestRejectedEvent,
    ClientRequestCancelledEvent,
    ClientUnassignedTrainerEvent,
)
from django.core.cache import cache
from training_platform.i18n import LanguageContext, CACHE_VERSION
from rest_framework_simplejwt.views import TokenObtainPairView
from rest_framework_simplejwt.serializers import TokenObtainPairSerializer
from rest_framework.parsers import MultiPartParser, FormParser
from django.db import IntegrityError, transaction
from django.http import Http404
from rest_framework.exceptions import (NotAuthenticated, NotFound, PermissionDenied,
                                       ValidationError as DRFValidationError)
from training_platform.api_exceptions import PASSTHROUGH_EXCEPTIONS
from training_platform.query_params import body_dict

logger = logging.getLogger(__name__)

class CustomLoginView(LoginView):
    serializer_class = CustomLoginSerializer
    permission_classes = [AllowAny]
    def get_response(self):
        """Return JWTs, because JWT is the only thing this API accepts.

        dj-rest-auth's LoginView answers with {"key": "<DRF Token>"}, and
        DEFAULT_AUTHENTICATION_CLASSES contains JWTAuthentication ONLY — no
        TokenAuthentication anywhere. The key therefore authenticated nothing: every
        subsequent request came back 401 whether the client sent it as `Token …`,
        `Bearer …` or bare. Login, the primary entry point, handed out a dead
        credential while /api/auth/verify-otp/ two endpoints away returned working
        access/refresh pairs.

        `key` is kept in the response, equal to the access token, so nothing that
        already reads that field breaks.
        """
        from rest_framework_simplejwt.tokens import RefreshToken

        original_response = super().get_response()
        user = self.request.user

        refresh = RefreshToken.for_user(user)
        access = str(refresh.access_token)

        original_response.data.update({
            'access': access,
            'refresh': str(refresh),
            'key': access,  # legacy field, now a usable credential
            'user': {
                'id': user.id,
                'username': user.username,
                'email': user.email,
                'first_name': user.first_name,
                'last_name': user.last_name,
                'user_type': user.user_type,
                'profile_picture': user.profile_picture.url if user.profile_picture else None,
            },
        })
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
            refresh_token = body_dict(request).get('refresh_token')
            
            if not refresh_token:
                return Response(
                    {'error': _('Refresh token is required')}, 
                    status=status.HTTP_400_BAD_REQUEST
                )
            
            # Blacklist the refresh token
            token = RefreshToken(refresh_token)
            token.blacklist()
            
            return Response(
                {'message': _('Successfully logged out')}, 
                status=status.HTTP_200_OK
            )
            
        except TokenError:
            return Response(
                {'error': _('Invalid refresh token')}, 
                status=status.HTTP_400_BAD_REQUEST
            )
        except PASSTHROUGH_EXCEPTIONS:
            # Control-flow exceptions carry their own correct status (404/403/401/400).
            # The broad handler below turned every one of them into a 500, which is the
            # wrong contract for the client and hides real faults in the error budget.
            raise
        except Exception as e:
            return Response(
                {'error': _('Logout failed')}, 
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
            return Response({'message': _('Details updated successfully!')}, status=status.HTTP_200_OK)
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
            'message': _('Registration successful. Please check your email for OTP verification code.'),
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
                {'error': _('Email and OTP code are required')},
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
            'message': _('Email verified successfully. Your account has been activated.'),
            'user': user_info,
            'access': str(refresh.access_token),
            'refresh': str(refresh),
        }, status=status.HTTP_200_OK)

class ResendOTPView(APIView):
    """
    View to resend OTP code to user's email.
    Rate limited to 3 requests per hour per IP (django-ratelimit) + soft cache counter.
    Anti-enumeration: always returns 200 with the same message.
    """
    permission_classes = [AllowAny]

    def post(self, request):
        """Resend OTP code"""
        from django_ratelimit.decorators import ratelimit
        from django_ratelimit.exceptions import Ratelimited
        from .models import CustomUser
        from .utils import create_otp
        from django.core.cache import cache
        import hashlib

        _GENERIC_MSG = _('If this email is registered and unverified, a new OTP has been sent.')

        email = request.data.get('email')
        if not email:
            return Response(
                {'error': _('Email is required')},
                status=status.HTTP_400_BAD_REQUEST
            )

        # --- Layer 1: ratelimit_cache (DB1, Redis-backed, survives app cache flush) ---
        # Key: sha256 of email+IP so we don't store PII in Redis keys.
        # Use trusted proxy-chain IP — raw X-Forwarded-For is client-spoofable.
        from training_platform.middleware import get_trusted_client_ip
        client_ip = get_trusted_client_ip(request)
        rate_key = hashlib.sha256(f"{email}:{client_ip}".encode()).hexdigest()[:32]
        rl_cache_key = f"rl:otp_resend:{rate_key}"
        from training_platform.cache import ratelimit_cache
        rl = ratelimit_cache()
        rl_count = rl.get(rl_cache_key, 0)
        if rl_count >= 3:
            logger.warning(f"OTP resend rate limit hit for hash {rate_key}")
            return Response(
                {'error': _('Too many OTP requests. Please wait 1 hour before requesting again.')},
                status=status.HTTP_429_TOO_MANY_REQUESTS
            )
        try:
            rl.incr(rl_cache_key)
        except ValueError:
            rl.set(rl_cache_key, 1, 3600)

        # --- Anti-enumeration: look up user silently ---
        try:
            user = CustomUser.objects.get(email=email)
        except CustomUser.DoesNotExist:
            # Do NOT reveal whether the email exists
            logger.info(f"OTP resend requested for unknown email (hash: {rate_key})")
            return Response({'message': _GENERIC_MSG}, status=status.HTTP_200_OK)

        # Already verified accounts: still return 200 (don't confirm email status)
        if user.is_active:
            logger.info(f"OTP resend requested for already-active user {user.id}")
            return Response({'message': _GENERIC_MSG}, status=status.HTTP_200_OK)

        # Create and send new OTP
        try:
            create_otp(user)
            logger.info(f"OTP resent for user {user.id}")
        except PASSTHROUGH_EXCEPTIONS:
            # Control-flow exceptions carry their own correct status (404/403/401/400).
            # The broad handler below turned every one of them into a 500, which is the
            # wrong contract for the client and hides real faults in the error budget.
            raise
        except Exception as e:
            logger.error(f"Failed to resend OTP for user {user.id}: {str(e)}")
            return Response(
                {'error': _('Failed to send OTP. Please try again later.')},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )

        return Response({'message': _GENERIC_MSG}, status=status.HTTP_200_OK)


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
                {'error': _('This endpoint is only for trainers')}, 
                status=status.HTTP_403_FORBIDDEN
            )
        
        serializer = TrainerProfileSerializer(request.user)
        return Response(serializer.data, status=status.HTTP_200_OK)
    
    def post(self, request):
        """Update trainer profile"""
        if not request.user.is_trainer:
            return Response(
                {'error': _('This endpoint is only for trainers')}, 
                status=status.HTTP_403_FORBIDDEN
            )
        
        serializer = TrainerProfileSerializer(request.user, data=request.data, partial=True)
        if serializer.is_valid():
            serializer.save()
            return Response({'message': _('Trainer profile updated successfully!')}, status=status.HTTP_200_OK)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

class TrainerClientsView(APIView):
    """View for trainer to manage their clients"""
    permission_classes = [IsAuthenticated]
    
    def get(self, request):
        """Get all clients assigned to this trainer"""
        if not request.user.is_trainer:
            return Response(
                {'error': _('This endpoint is only for trainers')}, 
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
                {'error': _('client_id is required')}, 
                status=status.HTTP_400_BAD_REQUEST
            )
        
        try:
            from .models import TrainerClientRelation
            client = CustomUser.objects.get(id=client_id, user_type='client')
            
            # Check if relation already exists
            # An INVITATION the client must accept, not an assignment. This created a
            # pending row that the same trainer could then approve at
            # trainer/respond-to-request/, so any trainer could attach any unassigned
            # client to themselves in two calls and read that person's body metrics,
            # training history and diet plan without ever being asked. `initiated_by`
            # is what makes the approval belong to the other side.
            # In a savepoint: `unique_pending_trainer_request_per_client` is a partial
            # unique index, and catching its IntegrityError outside an atomic block
            # leaves the surrounding transaction unusable for everything after it.
            with transaction.atomic():
                relation, created = TrainerClientRelation.objects.get_or_create(
                    trainer=request.user,
                    client=client,
                    defaults={'status': 'pending', 'initiated_by': 'trainer',
                              'agreed_rate': getattr(request.user, 'trainer_hourly_rate', None)}
                )
            
            if not created:
                if relation.status == 'approved':
                    return Response(
                        {'error': _('Client is already assigned to you')}, 
                        status=status.HTTP_400_BAD_REQUEST
                    )
                elif relation.status == 'pending':
                    return Response(
                        {'error': _('Client assignment request is already pending')}, 
                        status=status.HTTP_400_BAD_REQUEST
                    )
                elif relation.status == 'rejected':
                    # Update status to pending for new request
                    relation.status = 'pending'
                    relation.initiated_by = 'trainer'
                    relation.agreed_rate = getattr(request.user, 'trainer_hourly_rate', None)
                    relation.save(update_fields=['status', 'initiated_by', 'agreed_rate',
                                                 'updated_at'])
            
            # On commit, like the client-side path beside it: a notification queued
            # inside an open transaction can be delivered before the row exists, or
            # after a rollback for a request that was never saved.
            _actor, _recipient = request.user.id, client.id
            _name = getattr(request.user, 'full_name', None) or request.user.username
            transaction.on_commit(lambda: emit_event(TrainerAssignmentRequestEvent(
                actor_id=_actor, recipient_id=_recipient, trainer_name=_name)))
            logger.info(f"Trainer {request.user.id} requested assignment of client {client.id}")
            
            # Invalidate client cache
            ClientProfileViewSet.invalidate_client_cache(client.id, request.user.id)
            
            return Response({
                'message': _('Assignment request sent successfully.'),
                'client_id': client.id,
                'status': 'pending'
            }, status=status.HTTP_200_OK)
            
        except IntegrityError:
            # `unique_pending_trainer_request_per_client` is a partial unique index, so
            # inviting a client who already has an outstanding request raised straight
            # past the only handler here and became a 500.
            return Response(
                {'error': _('That client already has an outstanding request.')},
                status=status.HTTP_409_CONFLICT
            )
        except CustomUser.DoesNotExist:
            return Response(
                {'error': _('Client not found')},
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
                {'error': _('client_id is required')}, 
                status=status.HTTP_400_BAD_REQUEST
            )
        
        try:
            from .models import TrainerClientRelation
            client = CustomUser.objects.get(id=client_id, user_type='client')
            
            # Check if relation exists
            try:
                # APPROVED, and only this trainer's own. Matching any status let a
                # trainer create a pending row for someone else's client and then
                # "unassign" them: the other trainer's approved row survived while
                # `assigned_trainer` went null, so the two disagreed and the real
                # trainer silently lost diet access to their own client.
                relation = TrainerClientRelation.objects.get(
                    trainer=request.user,
                    client=client,
                    status='approved'
                )

                if client.assigned_trainer_id == request.user.id:
                    client.assigned_trainer = None
                    client.save(update_fields=['assigned_trainer'])

                # Refund first, and only then let go of the record. `release_hold`
                # raises when it cannot move the money, and deleting the relation
                # anyway would destroy the only record of whose money is in escrow.
                from .trainer_relations import HoldFailed, release_hold
                try:
                    release_hold(relation, request.user)
                except HoldFailed as exc:
                    logger.error("Refund failed for relation %s: %s", relation.id, exc)
                    return Response(
                        {'error': _('We could not return the held payment. '
                                    'Nothing was changed; please try again.')},
                        status=status.HTTP_409_CONFLICT
                    )
                # Every row for this pair, not just the approved one: a stale pending
                # or rejected row left the client's own request-status screen
                # contradicting itself, and could re-trigger the block on choosing
                # another trainer.
                TrainerClientRelation.objects.filter(
                    trainer=request.user, client=client).delete()
                
                # Emit domain event — notification handled at delivery boundary
                emit_event(TrainerUnassignmentEvent(
                    actor_id=request.user.id,
                    recipient_id=client.id,
                    trainer_name=getattr(request.user, 'full_name', None) or request.user.username,
                ))
                logger.info(f"Trainer {request.user.id} unassigned client {client.id}")
                
                # Invalidate client cache
                ClientProfileViewSet.invalidate_client_cache(client.id, request.user.id)
                
                return Response({
                    'message': _('Client unassigned successfully.'),
                    'client_id': client.id
                }, status=status.HTTP_200_OK)
                
            except TrainerClientRelation.DoesNotExist:
                return Response(
                    {'error': _('Client is not assigned to you')}, 
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
                {'error': _('This endpoint is only for clients')}, 
                status=status.HTTP_403_FORBIDDEN
            )
        
        serializer = ClientProfileSerializer(request.user)
        return Response(serializer.data, status=status.HTTP_200_OK)
    
    def post(self, request):
        """Update client profile"""
        if not request.user.is_client:
            return Response(
                {'error': _('This endpoint is only for clients')}, 
                status=status.HTTP_403_FORBIDDEN
            )
        
        serializer = ClientProfileSerializer(request.user, data=request.data, partial=True)
        if serializer.is_valid():
            serializer.save()
            return Response({'message': _('Client profile updated successfully!')}, status=status.HTTP_200_OK)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

class PublicTrainersListView(APIView):
    """Public view to get all available trainers without authentication"""
    permission_classes = [AllowAny]
    
    def get(self, request):
        """Get all available trainers (public endpoint). Supports ?search=name to filter by name."""
        from django.db.models import Count, Q
        from routine.views import StandardResultsSetPagination
        
        trainers = CustomUser.objects.filter(
            user_type='trainer',
            trainer_is_available=True,
            is_active=True
        ).annotate(
            client_count_anno=Count('clients')
        )
        
        # Name search filter: ?search=<name>
        search_query = request.query_params.get('search', '').strip()
        if search_query:
            trainers = trainers.filter(
                Q(first_name__icontains=search_query) |
                Q(last_name__icontains=search_query) |
                Q(username__icontains=search_query)
            )
        
        trainers = trainers.order_by('id')

        
        paginator = StandardResultsSetPagination()
        page = paginator.paginate_queryset(trainers, request, view=self)
        
        if page is not None:
            trainer_data = []
            for trainer in page:
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
            return paginator.get_paginated_response({
                'available_trainers': trainer_data,
                'trainer_count': trainers.count()
            })
            
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
    """Roster totals for the signed-in app.

    This answered anonymously with the platform's trainer count and how many clients
    have one. Neither is personal data, but together they publish roster size and
    adoption to anyone who asks, and both move with the business. The mobile client
    reads them after sign-in, so authentication costs it nothing.
    """
    permission_classes = [IsAuthenticated]
    
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
                {'error': _('This endpoint is only for clients')}, 
                status=status.HTTP_403_FORBIDDEN
            )
        
        # Get all available trainers with client count annotated
        # Use Count('clients') to get the number of reverse relationships
        from django.db.models import Count, Q
        from routine.views import StandardResultsSetPagination
        
        trainers = CustomUser.objects.filter(
            user_type='trainer',
            trainer_is_available=True,
            is_active=True
        ).annotate(
            client_count_anno=Count('clients')
        )
        
        # Name search filter: ?search=<name>
        search_query = request.query_params.get('search', '').strip()
        if search_query:
            trainers = trainers.filter(
                Q(first_name__icontains=search_query) |
                Q(last_name__icontains=search_query) |
                Q(username__icontains=search_query)
            )
        
        trainers = trainers.order_by('id')

        
        paginator = StandardResultsSetPagination()
        page = paginator.paginate_queryset(trainers, request, view=self)
        
        if page is not None:
            trainer_data = []
            for trainer in page:
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
                    'client_count': trainer.client_count_anno,
                })
            return paginator.get_paginated_response({
                'client_id': request.user.id,
                'available_trainers': trainer_data,
                'trainer_count': trainers.count()
            })
        
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
                'client_count': trainer.client_count_anno,
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
        cache_key = LanguageContext.cache_key("trainer", user.id, "approved_clients")
        from training_platform.cache import private_cache
        cached = private_cache().get(cache_key)
        if cached is None:
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
            # Serialize to plain list of PKs — never cache raw QuerySet objects
            cached = list(queryset.values_list('id', flat=True))
            private_cache().set(cache_key, cached, timeout=300)  # 5 min
        # Reconstruct queryset from cached IDs so DRF can apply further filters
        return self.queryset.filter(id__in=cached)

    def retrieve(self, request, *args, **kwargs):
        pk = kwargs.get('pk')
        # Enforce permission at object level for trainers: must be approved relation
        # Fetch from base queryset (all clients), not the filtered one, so we can return 403 instead of 404
        try:
            obj = self.queryset.get(pk=pk)
        except self.queryset.model.DoesNotExist:
            return Response({'error': _('Client not found')}, status=status.HTTP_404_NOT_FOUND)
        # Object permission is already enforced by permission_classes but ensure 403 explicitly
        has_perm = all(perm().has_object_permission(request, self, obj) for perm in self.permission_classes)
        if not has_perm:
            return Response({'error': _('Forbidden')}, status=status.HTTP_403_FORBIDDEN)
        from training_platform.cache import private_cache
        cache_key = LanguageContext.cache_key("client_profile", pk)
        data = private_cache().get(cache_key)
        if data is not None:
            return Response(data)
        serializer = self.get_serializer(obj)
        data = serializer.data
        private_cache().set(cache_key, data, timeout=300)
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
            
        except PASSTHROUGH_EXCEPTIONS:
            # Control-flow exceptions carry their own correct status (404/403/401/400).
            # The broad handler below turned every one of them into a 500, which is the
            # wrong contract for the client and hides real faults in the error budget.
            raise
        except Exception as e:
            logger.error(f"Error listing client profiles: {str(e)}")
            return Response(
                {'error': _('An error occurred while listing client profiles')}, 
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )

    @staticmethod
    def invalidate_client_cache(client_id, trainer_id=None):
        """Invalidate cache for all language variants."""
        from django.conf import settings
        for lang_code, _ in settings.LANGUAGES:
            cache.delete(f"client_profile:{client_id}:{lang_code}:{CACHE_VERSION}")
            if trainer_id:
                cache.delete(f"trainer:{trainer_id}:approved_clients:{lang_code}:{CACHE_VERSION}")

# NOTE: DeviceTokenRegisterView was removed — it was never routed (users/urls.py
# maps device-token/ to FCMTokenView) and its update_or_create never reactivated
# a soft-deleted token. FCMTokenView in users/device_token_views.py is the single
# device-token registration entry point.


class CustomTokenObtainPairSerializer(TokenObtainPairSerializer):
    """Custom JWT token serializer that includes user information"""
    
    def validate(self, attrs):
        # Authenticate user first
        from django.contrib.auth import authenticate
        from .models import CustomUser
        from .utils import check_login_lockout, record_login_failure, clear_login_attempts
        from rest_framework_simplejwt.exceptions import AuthenticationFailed
        from rest_framework import serializers

        email = attrs.get('email') or attrs.get('username')  # Support both email and username
        password = attrs.get('password')

        # Per-account brute-force lockout (10 failures → 15-min cooldown)
        if email:
            is_locked, _remaining = check_login_lockout(email)
            if is_locked:
                raise serializers.ValidationError(
                    {"detail": _("Too many failed login attempts. Please try again in 15 minutes.")}
                )

        if email and password:
            try:
                user = CustomUser.objects.get(email=email)
            except CustomUser.DoesNotExist:
                user = None

            if user and not user.is_active:
                raise serializers.ValidationError(
                    {
                        "detail": _("Please verify your email address before logging in. Check your inbox for the OTP code."),
                        "requires_verification": True,
                        "email": user.email
                    }
                )

        # Call the parent validate method to get the standard token response
        try:
            data = super().validate(attrs)
        except AuthenticationFailed:
            if email:
                record_login_failure(email)
            raise

        # Successful auth — reset the failure counter
        if email:
            clear_login_attempts(email)
        
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
    
    # Atomic: these writes were independent, so a failure part-way left the
    # records inconsistent (a half-applied password reset either locks the
    # user out or leaves a consumed token usable).
    @transaction.atomic
    def post(self, request):
        """Client requests to be assigned to a trainer"""
        if not request.user.is_client:
            return Response(
                {'error': _('This endpoint is only for clients')}, 
                status=status.HTTP_403_FORBIDDEN
            )
        
        trainer_id = request.data.get('trainer_id')
        message = request.data.get('message', '')
        
        if not trainer_id:
            return Response(
                {'error': _('trainer_id is required')}, 
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
                    client_wallet, _created = Wallet.objects.get_or_create(owner=request.user, defaults={"owner_type": "client"})
                    if client_wallet.balance < trainer_charge:
                        return Response(
                            {'error': _('Insufficient wallet balance to request this trainer'), 'required': str(trainer_charge), 'balance': str(client_wallet.balance)},
                            status=status.HTTP_402_PAYMENT_REQUIRED
                        )
            except Exception:
                # Optional side effect: swallowing this silently is what made the
                # surrounding failures invisible in logs. Control flow is unchanged.
                logger.debug('suppressed non-fatal error', exc_info=True)
            
            # Check if trainer is available
            if not trainer.trainer_is_available:
                return Response(
                    {'error': _('This trainer is not currently accepting new clients')}, 
                    status=status.HTTP_400_BAD_REQUEST
                )
            
            # Enforce single-trainer rule: if client already assigned, block new requests
            if request.user.assigned_trainer is not None and request.user.assigned_trainer_id != trainer.id:
                return Response(
                    {'error': _('You are already assigned to a trainer. Unassign first to request another trainer.')},
                    status=status.HTTP_400_BAD_REQUEST
                )

            # Also block if there is any other pending request with a different trainer
            # Only the client's OWN outstanding request blocks them. Counting a
            # trainer's unanswered invitation here let any trainer freeze a client out
            # of choosing anybody, by inviting them and never withdrawing it.
            has_pending_elsewhere = TrainerClientRelation.objects.filter(
                client=request.user,
                status='pending',
                initiated_by='client',
            ).exclude(trainer_id=trainer.id).exists()
            if has_pending_elsewhere:
                return Response(
                    {'error': _('You already have a pending request with another trainer. Please wait for a response or cancel it first.')},
                    status=status.HTTP_400_BAD_REQUEST
                )
            
            # Check if relation already exists
            relation, created = TrainerClientRelation.objects.get_or_create(
                trainer=trainer,
                client=request.user,
                defaults={'status': 'pending', 'initiated_by': 'client',
                          # The price the client is agreeing to, right now. Read again
                          # at approval it was whatever the trainer had since set it to.
                          'agreed_rate': getattr(trainer, 'trainer_hourly_rate', None)}
            )
            
            if not created:
                if relation.status == 'approved':
                    return Response(
                        {'error': _('You are already assigned to this trainer')}, 
                        status=status.HTTP_400_BAD_REQUEST
                    )
                elif relation.status == 'pending':
                    return Response(
                        {'error': _('Request is already pending with this trainer')}, 
                        status=status.HTTP_400_BAD_REQUEST
                    )
                elif relation.status == 'rejected':
                    # Allow new request after rejection
                    relation.status = 'pending'
                    relation.initiated_by = 'client'
                    relation.agreed_rate = getattr(trainer, 'trainer_hourly_rate', None)
                    relation.save(update_fields=['status', 'initiated_by', 'agreed_rate',
                                                 'updated_at'])
            
            # Emit domain event — notification handled at delivery boundary.
            # On commit: this runs inside an open transaction, and a message queued
            # before the commit can be picked up before the row exists, or delivered
            # after a rollback for a request that was never saved.
            transaction.on_commit(lambda: emit_event(ClientRequestReceivedEvent(
                actor_id=request.user.id,
                recipient_id=trainer.id,
                client_name=request.user.full_name or request.user.username,
            )))
            
            logger.info(f"Client {request.user.id} requested trainer {trainer.id}")
            
            return Response({
                'message': _('Request sent to trainer successfully.'),
                'trainer_id': trainer.id,
                'status': 'pending'
            }, status=status.HTTP_200_OK)
            
        except CustomUser.DoesNotExist:
            return Response(
                {'error': _('Trainer not found')}, 
                status=status.HTTP_404_NOT_FOUND
            )
        except PASSTHROUGH_EXCEPTIONS:
            # Control-flow exceptions carry their own correct status (404/403/401/400).
            # The broad handler below turned every one of them into a 500, which is the
            # wrong contract for the client and hides real faults in the error budget.
            raise
        except Exception as e:
            logger.error(f"Error in client request: {str(e)}")
            return Response(
                {'error': _('An error occurred while sending the request')}, 
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )


class ClientUnassignTrainerView(APIView):
    """
    View for clients to unassign themselves from their current trainer.
    
    Features:
    - Client can remove their assigned trainer
    - Cleans up TrainerClientRelation records
    - Sends notification to the trainer
    - Invalidates relevant caches
    
    Endpoint: POST /api/users/client/unassign-trainer/
    """
    permission_classes = [IsAuthenticated]
    
    def post(self, request):
        """Client unassigns their current trainer"""
        if not request.user.is_client:
            return Response(
                {'error': _('This endpoint is only for clients')}, 
                status=status.HTTP_403_FORBIDDEN
            )
        
        try:
            from .models import TrainerClientRelation
            
            # Check if client has an assigned trainer
            if not request.user.assigned_trainer:
                return Response(
                    {'error': _('You do not have an assigned trainer')}, 
                    status=status.HTTP_400_BAD_REQUEST
                )
            
            trainer = request.user.assigned_trainer
            trainer_id = trainer.id
            trainer_name = trainer.full_name or trainer.username
            
            # Remove the TrainerClientRelation record
            relation = None
            try:
                relation = TrainerClientRelation.objects.get(
                    trainer=trainer,
                    client=request.user,
                    status='approved'
                )
                # Refund first, and only then let go of the record.
                from .trainer_relations import HoldFailed, release_hold
                try:
                    release_hold(relation, request.user)
                except HoldFailed as exc:
                    logger.error("Refund failed for relation %s: %s", relation.id, exc)
                    return Response(
                        {'error': _('We could not return your held payment. '
                                    'Nothing was changed; please try again.')},
                        status=status.HTTP_409_CONFLICT
                    )
                TrainerClientRelation.objects.filter(
                    trainer=trainer, client=request.user).delete()
            except TrainerClientRelation.DoesNotExist:
                # Relation may not exist, but we still proceed to clear assigned_trainer
                # Optional side effect: swallowing this silently is what made the
                # surrounding failures invisible in logs. Control flow is unchanged.
                logger.debug('suppressed non-fatal error', exc_info=True)

            # Clear assigned trainer
            request.user.assigned_trainer = None
            request.user.save(update_fields=['assigned_trainer'])
            
            # Emit domain event — notification handled at delivery boundary
            emit_event(ClientUnassignedTrainerEvent(
                actor_id=request.user.id,
                recipient_id=trainer.id,
                client_name=request.user.full_name or request.user.username,
            ))
            
            logger.info(f"Client {request.user.id} unassigned from trainer {trainer_id}")
            
            # Invalidate caches
            ClientProfileViewSet.invalidate_client_cache(request.user.id, trainer_id)
            
            return Response({
                'message': _('Successfully unassigned from trainer.'),
                'previous_trainer_id': trainer_id,
                'previous_trainer_name': trainer_name
            }, status=status.HTTP_200_OK)
            
        except PASSTHROUGH_EXCEPTIONS:
            # Control-flow exceptions carry their own correct status (404/403/401/400).
            # The broad handler below turned every one of them into a 500, which is the
            # wrong contract for the client and hides real faults in the error budget.
            raise
        except Exception as e:
            logger.error(f"Error unassigning trainer: {str(e)}")
            return Response(
                {'error': _('An error occurred while unassigning the trainer')}, 
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )


class ClientCancelTrainerRequestView(APIView):
    """
    View for clients to cancel their pending trainer request.
    
    Features:
    - Client can cancel a pending request to a trainer
    - Allows client to request a different trainer after cancelling
    - Sends notification to the trainer about cancellation
    
    Endpoint: POST /api/users/client/cancel-trainer-request/
    """
    permission_classes = [IsAuthenticated]
    
    def post(self, request):
        """Client cancels their pending trainer request"""
        if not request.user.is_client:
            return Response(
                {'error': _('This endpoint is only for clients')}, 
                status=status.HTTP_403_FORBIDDEN
            )
        
        try:
            from .models import TrainerClientRelation
            
            # Find pending request for this client
            pending_request = TrainerClientRelation.objects.filter(
                client=request.user,
                status='pending'
            ).select_related('trainer').first()
            
            if not pending_request:
                return Response(
                    {'error': _('You do not have a pending trainer request')}, 
                    status=status.HTTP_400_BAD_REQUEST
                )
            
            trainer = pending_request.trainer
            trainer_id = trainer.id
            trainer_name = trainer.full_name or trainer.username
            
            # Delete the pending request
            pending_request.delete()
            
            # Emit domain event — notification handled at delivery boundary
            emit_event(ClientRequestCancelledEvent(
                actor_id=request.user.id,
                recipient_id=trainer.id,
                client_name=request.user.full_name or request.user.username,
            ))
            
            logger.info(f"Client {request.user.id} cancelled pending request to trainer {trainer_id}")
            
            return Response({
                'message': _('Request cancelled successfully.'),
                'cancelled_trainer_id': trainer_id,
                'cancelled_trainer_name': trainer_name
            }, status=status.HTTP_200_OK)
            
        except PASSTHROUGH_EXCEPTIONS:
            # Control-flow exceptions carry their own correct status (404/403/401/400).
            # The broad handler below turned every one of them into a 500, which is the
            # wrong contract for the client and hides real faults in the error budget.
            raise
        except Exception as e:
            logger.error(f"Error cancelling trainer request: {str(e)}")
            return Response(
                {'error': _('An error occurred while cancelling the request')}, 
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
                {'error': _('This endpoint is only for trainers')}, 
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
            
        except PASSTHROUGH_EXCEPTIONS:
            # Control-flow exceptions carry their own correct status (404/403/401/400).
            # The broad handler below turned every one of them into a 500, which is the
            # wrong contract for the client and hides real faults in the error budget.
            raise
        except Exception as e:
            logger.error(f"Error fetching pending requests: {str(e)}")
            return Response(
                {'error': _('An error occurred while fetching pending requests')}, 
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )


class ClientRespondToTrainerInviteView(APIView):
    """A client accepts or declines a trainer's invitation.

    The other half of `trainer/assign-client/`. Without it a trainer-initiated request
    had no legitimate way to be approved at all — which is why the trainer endpoint
    used to approve its own, and that was a takeover of any unassigned client.

    POST {"request_id": <id>, "action": "approve" | "reject"}
    """
    permission_classes = [IsAuthenticated]

    def post(self, request):
        from .models import TrainerClientRelation

        if not request.user.is_client:
            return Response({'error': _('This endpoint is only for clients')},
                            status=status.HTTP_403_FORBIDDEN)
        # str() before .lower(): a JSON number, boolean or object here used to be an
        # AttributeError and a 500, on the path a client uses to accept a trainer.
        action = str(body_dict(request).get('action') or '').lower()
        if action not in ('approve', 'reject'):
            return Response({'error': _('action must be approve or reject')},
                            status=status.HTTP_400_BAD_REQUEST)
        try:
            relation = TrainerClientRelation.objects.select_related('trainer').get(
                id=request.data.get('request_id'), client=request.user, status='pending')
        except (TrainerClientRelation.DoesNotExist, ValueError, TypeError):
            return Response({'error': _('Invitation not found or already processed')},
                            status=status.HTTP_404_NOT_FOUND)

        if relation.initiated_by == 'unknown':
            return Response(
                {'error': _('This invitation predates the current consent flow. '
                            'Ask your trainer to send it again.')},
                status=status.HTTP_409_CONFLICT)
        # Symmetric to the trainer side: you approve what the OTHER party asked for.
        if relation.initiated_by != 'trainer':
            return Response({'error': _('You made this request. The trainer has to accept it.')},
                            status=status.HTTP_403_FORBIDDEN)

        trainer_id, trainer = relation.trainer_id, relation.trainer
        if action == 'reject':
            relation.status = 'rejected'
            relation.save(update_fields=['status', 'updated_at'])
            transaction.on_commit(lambda: emit_event(ClientRequestRejectedEvent(
                actor_id=request.user.id, recipient_id=trainer_id,
                trainer_name=trainer.full_name or trainer.username)))
            return Response({'message': _('Invitation declined.'),
                             'trainer_id': trainer_id, 'status': 'rejected'})

        refusal, relation = approve_trainer_relation(relation, request.user)
        if refusal is not None:
            return refusal
        transaction.on_commit(lambda: emit_event(ClientRequestApprovedEvent(
            actor_id=request.user.id, recipient_id=trainer_id,
            trainer_name=trainer.full_name or trainer.username)))
        logger.info(f"Client {request.user.id} accepted trainer {trainer_id}")
        return Response({'message': _('Invitation accepted.'),
                         'trainer_id': trainer_id, 'status': 'approved'})


class ClientPendingTrainerInvitesView(APIView):
    """Invitations waiting on this client, so the app can show them."""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        from .models import TrainerClientRelation

        if not request.user.is_client:
            return Response({'error': _('This endpoint is only for clients')},
                            status=status.HTTP_403_FORBIDDEN)
        rows = (TrainerClientRelation.objects
                .filter(client=request.user, status='pending', initiated_by='trainer')
                .select_related('trainer').order_by('-created_at')[:50])
        return Response({'invitations': [{
            'request_id': r.id,
            'trainer_id': r.trainer_id,
            'trainer_name': r.trainer.full_name or r.trainer.username,
            'agreed_rate': r.agreed_rate,
            'created_at': r.created_at,
        } for r in rows]})


def approve_trainer_relation(relation, actor):
    """Approve a pending trainer/client relation, whichever side is accepting.

    One implementation, because there are two sides and the approval has to do the
    same things on both: check nobody else already owns this client, take the agreed
    charge into escrow, flip the row and set `assigned_trainer`. A second copy would
    drift, and the half that drifted would be the one granting access to another
    person's body metrics.

    Everything happens inside one transaction, under a row lock on the relation, and
    the charge is taken BEFORE the relation is granted — a failure to take it refuses
    the approval instead of granting a relationship nobody paid for.

    Returns (response_or_None, relation). A response means refuse with it.
    """
    from rest_framework import status as http
    from rest_framework.response import Response

    from .models import TrainerClientRelation
    from .trainer_relations import HoldFailed, take_hold

    with transaction.atomic():
        relation = (TrainerClientRelation.objects
                    .select_for_update()
                    .select_related('trainer', 'client')
                    .get(pk=relation.pk))
        client, trainer = relation.client, relation.trainer

        if relation.status != 'pending':
            return Response({'error': _('This request has already been answered')},
                            status=http.HTTP_409_CONFLICT), relation
        # A trainer who has since been deactivated cannot be accepted weeks later.
        if not (trainer.is_active and trainer.is_trainer):
            return Response({'error': _('That trainer is no longer available')},
                            status=http.HTTP_409_CONFLICT), relation

        locked_client = CustomUser.objects.select_for_update().get(pk=client.pk)
        if locked_client.assigned_trainer_id and locked_client.assigned_trainer_id != trainer.id:
            return Response({'error': _('Client is already assigned to another trainer')},
                            status=http.HTTP_400_BAD_REQUEST), relation
        if TrainerClientRelation.objects.filter(
                client=client, status='approved').exclude(trainer=trainer).exists():
            return Response({'error': _('Client already has an approved relation with another trainer')},
                            status=http.HTTP_400_BAD_REQUEST), relation

        try:
            take_hold(relation, actor)
        except HoldFailed as exc:
            logger.warning("Trainer hold failed for relation %s: %s", relation.id, exc)
            return Response({'error': _('The trainer charge could not be held: %(why)s')
                             % {'why': str(exc)}},
                            status=http.HTTP_402_PAYMENT_REQUIRED), relation

        relation.status = 'approved'
        relation.save(update_fields=['status', 'updated_at'])
        locked_client.assigned_trainer = trainer
        locked_client.save(update_fields=['assigned_trainer'])
    return None, relation


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
                {'error': _('This endpoint is only for trainers')}, 
                status=status.HTTP_403_FORBIDDEN
            )
        
        request_id = request.data.get('request_id')
        action = request.data.get('action')  # 'approve' or 'reject'
        reason = request.data.get('reason', '')
        
        if not request_id:
            return Response(
                {'error': _('request_id is required')}, 
                status=status.HTTP_400_BAD_REQUEST
            )
        
        if action not in ['approve', 'reject']:
            return Response(
                {'error': _('action must be either "approve" or "reject"')}, 
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

            # A request is approved by the side that did NOT make it. Without this,
            # a trainer could approve their own invitation, and that was a complete
            # takeover of any unassigned client's account data.
            if relation.initiated_by == 'unknown':
                return Response(
                    {'error': _('This request predates the current consent flow. '
                                'Ask the client to send it again.')},
                    status=status.HTTP_409_CONFLICT
                )
            if relation.initiated_by != 'client':
                return Response(
                    {'error': _('You invited this client. They have to accept the invitation.')},
                    status=status.HTTP_403_FORBIDDEN
                )

            if action == 'approve':
                refusal, relation = approve_trainer_relation(relation, request.user)
                if refusal is not None:
                    return refusal

                # Emit domain event — notification handled at delivery boundary
                emit_event(ClientRequestApprovedEvent(
                    actor_id=request.user.id,
                    recipient_id=client.id,
                    trainer_name=request.user.full_name or request.user.username,
                ))
                
                logger.info(f"Trainer {request.user.id} approved client {client.id}")
                
                return Response({
                    'message': _('Request approved successfully.'),
                    'client_id': client.id,
                    'status': 'approved'
                }, status=status.HTTP_200_OK)
                
            else:  # reject
                # Reject the request
                relation.status = 'rejected'
                relation.save()
                
                # Emit domain event — notification handled at delivery boundary
                emit_event(ClientRequestRejectedEvent(
                    actor_id=request.user.id,
                    recipient_id=client.id,
                    trainer_name=request.user.full_name or request.user.username,
                ))
                
                logger.info(f"Trainer {request.user.id} rejected client {client.id}")
                
                return Response({
                    'message': _('Request rejected.'),
                    'client_id': client.id,
                    'status': 'rejected'
                }, status=status.HTTP_200_OK)
                
        except TrainerClientRelation.DoesNotExist:
            return Response(
                {'error': _('Request not found or already processed')}, 
                status=status.HTTP_404_NOT_FOUND
            )
        except PASSTHROUGH_EXCEPTIONS:
            # Control-flow exceptions carry their own correct status (404/403/401/400).
            # The broad handler below turned every one of them into a 500, which is the
            # wrong contract for the client and hides real faults in the error budget.
            raise
        except Exception as e:
            logger.error(f"Error responding to request: {str(e)}")
            return Response(
                {'error': _('An error occurred while processing the request')}, 
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
                {'error': _('This endpoint is only for clients')}, 
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
            
        except PASSTHROUGH_EXCEPTIONS:
            # Control-flow exceptions carry their own correct status (404/403/401/400).
            # The broad handler below turned every one of them into a 500, which is the
            # wrong contract for the client and hides real faults in the error budget.
            raise
        except Exception as e:
            logger.error(f"Error fetching client requests: {str(e)}")
            return Response(
                {'error': _('An error occurred while fetching requests')}, 
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
                    {'error': _('No image file provided. Please include a file with key "profile_picture"')},
                    status=status.HTTP_400_BAD_REQUEST
                )

            # Validate CONTENT, not the client-supplied Content-Type header (which is
            # trivially spoofed — PHP/HTML/SVG payloads were previously accepted and
            # stored with those extensions). This also caps pixel dimensions and
            # re-encodes, which strips EXIF/GPS and neutralises polyglot files.
            from django.core.exceptions import ValidationError as DjangoValidationError
            from training_platform.file_security import process_uploaded_image
            try:
                safe_file, ext = process_uploaded_image(image_file, max_bytes=2 * 1024 * 1024)
            except DjangoValidationError as e:
                return Response(
                    {'error': e.messages[0] if getattr(e, 'messages', None) else str(e)},
                    status=status.HTTP_400_BAD_REQUEST
                )

            # Update user's profile picture. The name uses the DETECTED format's
            # extension, never the uploaded filename.
            user = request.user
            user.profile_picture.save(f"profile.{ext}", safe_file, save=False)
            user.save()

            # Return the updated user info
            return Response({
                'message': _('Profile picture uploaded successfully'),
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

        except PASSTHROUGH_EXCEPTIONS:
            # Control-flow exceptions carry their own correct status (404/403/401/400).
            # The broad handler below turned every one of them into a 500, which is the
            # wrong contract for the client and hides real faults in the error budget.
            raise
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
                # Clearing the field is enough: the post-commit receiver in
                # training_platform.signals removes the stored file once the row
                # actually persists.
                user.profile_picture = None
                user.save()
                
                return Response({
                    'message': _('Profile picture removed successfully')
                }, status=status.HTTP_200_OK)
            else:
                return Response({
                    'message': _('No profile picture to remove')
                }, status=status.HTTP_404_NOT_FOUND)
                
        except PASSTHROUGH_EXCEPTIONS:
            # Control-flow exceptions carry their own correct status (404/403/401/400).
            # The broad handler below turned every one of them into a 500, which is the
            # wrong contract for the client and hides real faults in the error budget.
            raise
        except Exception as e:
            return Response(
                {'error': f'Failed to remove profile picture: {str(e)}'},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )

 

# ============================================================================
# PASSWORD RESET (OTP-BASED) VIEWS
# ============================================================================

class PasswordResetRequestView(APIView):
    """
    Step 1: Request a password reset OTP.
    
    Sends a 6-digit OTP to the user's email. Returns a generic success message
    regardless of whether the email exists (prevents user enumeration).
    
    Rate limited: 3 requests per hour per email via cache.
    """
    permission_classes = [AllowAny]

    def post(self, request):
        from .serializers import PasswordResetRequestSerializer
        from .utils import create_password_reset_otp
        from training_platform.cache import ratelimit_cache
        import hashlib

        serializer = PasswordResetRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        email = serializer.validated_data['email']

        # Rate limiting: max 3 requests per hour per email.
        # Use ratelimit_cache (DB1) — isolated from the session store and not
        # flushable via app cache clear. Hash the email so no PII lands in keys.
        rl = ratelimit_cache()
        email_hash = hashlib.sha256(email.strip().lower().encode('utf-8')).hexdigest()[:32]
        cache_key = f'pwd_reset_req:{email_hash}'
        request_count = rl.get(cache_key, 0)
        if request_count >= 3:
            return Response(
                {'error': _('Too many password reset requests. Please wait 1 hour before trying again.')},
                status=status.HTTP_429_TOO_MANY_REQUESTS
            )

        try:
            user = CustomUser.objects.get(email=email, is_active=True)
            create_password_reset_otp(user)
        except CustomUser.DoesNotExist:
            # Do NOT reveal whether the email exists
            # Optional side effect: swallowing this silently is what made the
            # surrounding failures invisible in logs. Control flow is unchanged.
            logger.debug('suppressed non-fatal error', exc_info=True)

        # Increment rate limit counter regardless of user existence
        try:
            rl.incr(cache_key)
        except ValueError:
            rl.set(cache_key, 1, 3600)

        logger.info(f"Password reset requested for email: {email}")

        return Response({
            'message': _('If an account with this email exists, a password reset code has been sent.'),
            'email': email,
        }, status=status.HTTP_200_OK)


class PasswordResetVerifyView(APIView):
    """
    Step 2: Verify the password reset OTP.
    
    On success, returns a single-use UUID reset token (valid for 15 minutes)
    that must be submitted with the new password.
    """
    permission_classes = [AllowAny]

    def post(self, request):
        from .serializers import PasswordResetVerifySerializer
        from .models import PasswordResetToken
        from .utils import verify_otp
        from datetime import timedelta

        serializer = PasswordResetVerifySerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        email = serializer.validated_data['email']
        otp_code = serializer.validated_data['otp_code']

        success, otp_instance, error_message = verify_otp(email, otp_code, purpose='password_reset')

        if not success:
            return Response(
                {'error': error_message},
                status=status.HTTP_400_BAD_REQUEST
            )

        user = otp_instance.user

        # Invalidate any existing unused reset tokens for this user
        PasswordResetToken.objects.filter(
            user=user, is_used=False
        ).update(is_used=True)

        # Create a new single-use token. `issue()` returns the raw value once; only its
        # keyed hash is stored, so the table cannot be read to reset someone's password.
        reset_token, raw_reset_token = PasswordResetToken.issue(user, minutes=15)

        logger.info(f"Password reset OTP verified for user {user.id} ({email})")

        return Response({
            'message': _('OTP verified successfully. Use the reset token to set your new password.'),
            'reset_token': raw_reset_token,
        }, status=status.HTTP_200_OK)


class PasswordResetConfirmView(APIView):
    """
    Step 3: Set a new password using the reset token.
    
    Consumes the single-use token and updates the user's password.
    Django password validators are enforced.
    """
    permission_classes = [AllowAny]

    # Atomic: these writes were independent, so a failure part-way left the
    # records inconsistent (a half-applied password reset either locks the
    # user out or leaves a consumed token usable).
    @transaction.atomic
    def post(self, request):
        from .serializers import PasswordResetConfirmSerializer
        from .models import PasswordResetToken

        serializer = PasswordResetConfirmSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        token_uuid = serializer.validated_data['reset_token']
        new_password = serializer.validated_data['new_password']

        reset_token = PasswordResetToken.verify(str(token_uuid))
        if reset_token is None:
            return Response(
                {'error': _('Invalid or expired reset token.')},
                status=status.HTTP_400_BAD_REQUEST
            )

        if not reset_token.is_valid():
            return Response(
                {'error': _('This reset token has already been used or has expired. Please request a new one.')},
                status=status.HTTP_400_BAD_REQUEST
            )

        # Consume the token
        from django.utils import timezone
        reset_token.is_used = True
        reset_token.used_at = timezone.now()
        reset_token.save()

        # Set the new password
        user = reset_token.user
        user.set_password(new_password)
        user.save()

        # Revoke all outstanding refresh tokens — a password reset is exactly the
        # moment (account recovery/compromise) when previously-issued tokens must
        # stop working. Access tokens still expire on their own short TTL.
        try:
            from rest_framework_simplejwt.token_blacklist.models import OutstandingToken, BlacklistedToken
            for _t in OutstandingToken.objects.filter(user=user):
                BlacklistedToken.objects.get_or_create(token=_t)
        except Exception as e:
            logger.error(f"Failed to blacklist tokens after password reset for user {user.id}: {e}")

        logger.info(f"Password reset completed for user {user.id} ({user.email})")

        return Response({
            'message': _('Your password has been reset successfully. You can now log in with your new password.'),
        }, status=status.HTTP_200_OK)
