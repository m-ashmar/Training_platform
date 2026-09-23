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
from django.utils import timezone
from django.utils.translation import gettext as _
from rest_framework_simplejwt.tokens import RefreshToken
from rest_framework_simplejwt.exceptions import TokenError
from .models import CustomUser
# Blocking: neither side may ask the other to coach them, be matched with them, or
# see them listed. `moderation/services.py` imports no models at module level, so it
# is safe to import here.
from moderation.services import blocked_ids, is_blocked
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


#: The longest note a client may send with a request (RequestSheet).
CLIENT_NOTE_MAX_LENGTH = 500


def _shown_coaching_price(request, value):
    """A coaching fee on a legacy payload, or None where this BUILD may not show one.

    The key stays (additive); its value is null on a build whose
    `can_show_coaching_prices` is off (`ios` today) or that declares no channel — the
    spec's "no price strings reach store builds" (BACKEND_BUGS.md B-007). The
    `/api/v1/trainers/` cards omit the key instead.
    """
    from policy.money import MoneyType
    from policy.services import can_show_price

    # Once per request, not per row: `can_show_price` reads the channel's row, and a
    # 100-row page cost 100 queries.
    shown = getattr(request, '_coaching_price_shown', None)
    if shown is None:
        shown = can_show_price(request, MoneyType.COACHING_1TO1)
        request._coaching_price_shown = shown
    return value if shown else None

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
        """Retrieve user details. `profile_picture` is an absolute URL."""
        user = request.user
        serializer = UserDetailsSerializer(user, context={'request': request})
        return Response(serializer.data, status=status.HTTP_200_OK)

    def post(self, request):
        """Update user details.

        Errors go through the API envelope, so each field error carries its `code`
        (`field_errors.<field>[0].code`), e.g. `under_minimum_age` on `date_of_birth`
        or `age`. The field keys stay at the top level as before. `profile_picture` is
        read-only here: a file sent as `profile_picture` answers 400
        `profile_picture_read_only`; upload it with `POST /api/auth/user/profile-picture/`.
        """
        user = request.user
        # Refused, not dropped (the owner, 2026-09-20; BACKEND_BUGS B-026): a picture
        # sent here was ignored with 200. FILES, not data: a client echoing the picture
        # URL it read back as text is not refused.
        if 'profile_picture' in request.FILES:
            message = _("Upload the profile picture with /api/auth/user/profile-picture/.")
            return Response({'error': message, 'detail': message,
                             'code': 'profile_picture_read_only', 'field': 'profile_picture'},
                            status=status.HTTP_400_BAD_REQUEST)
        serializer = UserDetailsSerializer(user, data=request.data, partial=True,
                                           context={'request': request})
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response({'message': _('Details updated successfully!')}, status=status.HTTP_200_OK)
    
    
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
        # The serializer leaves the account enabled and unverified; no credential is
        # issued until the address is verified.
        return user
    
    def create(self, request, *args, **kwargs):
        """
        Override create to handle registration without allauth's complete_signup flow.
        """
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        
        # Create the user: enabled, unverified, and holding no credential
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
    POST /api/auth/verify-otp/ — verify the sign-up code; answers JWT tokens.

    Body: `{email, otp_code}`. 200 `{message, user, access, refresh}`.
    Refusals: 400 `{error, detail, code}` with `code` one of `otp_invalid` (a wrong code,
    no pending code, or an unknown address — one answer for all three, so addresses
    cannot be enumerated), `otp_expired` (the right code, too late: ask for a new one) or
    `too_many_attempts` (five wrong codes for this address and purpose lock it for 15
    minutes; carries `retry_after_seconds` and `Retry-After`; the right code is refused
    too while locked). The address is matched lower-cased (BACKEND_BUGS B-021, B-033).
    403 `account_disabled`: the code is right but the account is suspended.
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
        
        from .utils import otp_refusal_response, verify_otp_detailed
        from rest_framework_simplejwt.tokens import RefreshToken

        success, otp_instance, error_message, code, retry_after = verify_otp_detailed(email, otp_code)

        if not success:
            # Its own `code` for each reason (BACKEND_BUGS B-021): `otp_invalid`,
            # `otp_expired`, `too_many_attempts` (+ `retry_after_seconds`).
            return otp_refusal_response(error_message, code, retry_after)
        
        # Verifying an email proves the address, and nothing else. It used to set
        # `is_active = True`, which is also the column a moderator clears to suspend an
        # account — so a suspended user tapped Resend, entered the code that arrived in
        # their own inbox, and was handed a working token with the suspension undone.
        user = otp_instance.user
        # It never writes `is_active` at all now. "First verification enables the
        # account" was an inference from `email_verified=False`, and that state also
        # described accounts nobody meant to enable: a spam signup a moderator had
        # disabled was re-enabled, with a token, by the code already in its inbox. A
        # signup is enabled from the moment it registers, so there is nothing here to
        # enable; an inactive account is a disabled one, whatever it has verified.
        if not user.is_active:
            return Response(
                {'error': _('This account is not available. Contact support.'),
                 'code': 'account_disabled'},
                status=status.HTTP_403_FORBIDDEN)
        if not user.email_verified:
            user.email_verified = True
            user.save(update_fields=['email_verified'])
        
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
    POST /api/auth/resend-otp/ — send a new sign-up code. Body: `{email}`.

    Anti-enumeration: 200 with the same message whether or not the address exists, is
    verified or is suspended. Limit: 3 an hour per address (lower-cased) and IP, then
    429 `{error, detail, code: "throttled", retry_after_seconds}` with `Retry-After` —
    the seconds really left in the hour (BACKEND_BUGS B-022); the same for every address.
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
        if not email or not isinstance(email, str):
            return Response(
                {'error': _('Email is required')},
                status=status.HTTP_400_BAD_REQUEST
            )
        # Stored lower-cased: `Samer@Example.com` found nobody, answered 200, sent
        # nothing and still spent one of the three requests (BACKEND_BUGS B-033).
        email = email.strip().lower()

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
            # The seconds left on the hour and a code (BACKEND_BUGS B-022).
            from training_platform.cache import seconds_left
            wait = seconds_left(rl, rl_cache_key, 3600)
            message = _('Too many OTP requests. Please wait 1 hour before requesting again.')
            return Response(
                {'error': message, 'detail': message, 'code': 'throttled',
                 'retry_after_seconds': wait},
                status=status.HTTP_429_TOO_MANY_REQUESTS, headers={'Retry-After': str(wait)}
            )
        try:
            rl.incr(rl_cache_key)
        except ValueError:
            rl.set(rl_cache_key, 1, 3600)

        # --- Anti-enumeration: look up user silently ---
        try:
            from .utils import find_by_email
            user = find_by_email(email)
            if user is None:
                raise CustomUser.DoesNotExist
        except CustomUser.DoesNotExist:
            # Do NOT reveal whether the email exists
            logger.info(f"OTP resend requested for unknown email (hash: {rate_key})")
            return Response({'message': _GENERIC_MSG}, status=status.HTTP_200_OK)

        # Already verified, or disabled: still 200, and still no OTP. This gated on
        # `is_active` alone, which was false for BOTH an unverified signup and a
        # suspended account — so suspension made an account eligible for a fresh
        # registration code. The rewrite kept `not is_active` while registration still
        # wrote `is_active=False` for "unverified", so it refused every pending signup
        # instead. A pending signup is enabled-and-unverified now (see
        # `CustomUser.email_verified`), which is exactly what this serves.
        if user.email_verified or not user.is_active:
            logger.info(f"OTP resend declined for user {user.id} "
                        f"(verified={user.email_verified}, active={user.is_active})")
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
        # Raised, not returned, so field errors keep their `code` (see UpdateUserDetailsView).
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response({'message': _('Trainer profile updated successfully!')}, status=status.HTTP_200_OK)

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

            # How many people a trainer may have outstanding invitations to at once.
            # `unique_pending_trainer_request_per_client` is scoped to client-initiated
            # rows, so trainer-initiated invitations were unlimited and could name any
            # client id — which is what turned the pending-requests list into a
            # directory of the whole client base. An invitation is a message to a
            # person, not a query, so a working trainer needs a handful and a scraper
            # needs thousands.
            OUTSTANDING_INVITE_LIMIT = 25
            outstanding = TrainerClientRelation.objects.filter(
                trainer=request.user, status='pending', initiated_by='trainer').count()
            if outstanding >= OUTSTANDING_INVITE_LIMIT:
                return Response(
                    {'error': _('You have too many invitations awaiting a reply. '
                                'Wait for some to be answered before sending more.'),
                     'code': 'too_many_invitations'},
                    status=status.HTTP_429_TOO_MANY_REQUESTS)

            client = CustomUser.objects.get(id=client_id, user_type='client')

            # The same rule as the client's own request, in the other direction: an
            # invitation is a message to a person, and a blocked person does not
            # receive messages. Answered identically whichever side made the block,
            # so the refusal never reveals that one exists.
            if is_blocked(request.user, client):
                return Response(
                    {'error': _('This is not available between these two accounts.'),
                     'code': 'user_blocked'},
                    status=status.HTTP_403_FORBIDDEN
                )

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
                              'agreed_rate': getattr(request.user, 'trainer_hourly_rate', None),
                              'requested_at': timezone.now(), 'responded_at': None}
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
                    relation.requested_at, relation.responded_at = timezone.now(), None
                    relation.client_note = ''    # the client's old note is not theirs to keep
                    relation.save(update_fields=['status', 'initiated_by', 'agreed_rate',
                                                 'requested_at', 'responded_at',
                                                 'client_note', 'updated_at'])
            
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
    """POST /api/auth/trainer/unassign-client/ — a trainer ends coaching with a client.

    Body: `client_id`, optional `at_period_end` (bool). A trainer cannot end a paid month
    the client is in (owner, 2026-09-19): while one runs this answers
    409 `coaching_month_active` with `ends_at`, and with `at_period_end: true` it instead
    schedules the end — 200 `{scheduled: true, ends_at}` — and the month is not renewed.
    Before the first program (month 1 only held) the client is released and refunded;
    409 `refund_failed` when that refund cannot be made, and nothing changes.
    """
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

                from .coaching import running_month
                # ONE transaction under the relation's lock, from the check to the
                # delete: checked in one transaction and deleted in another, a first
                # program settled in between paid out a month and the coaching was then
                # deleted anyway (users/trainer_relations.py lock order: the relation,
                # then the client's row, then wallets).
                with transaction.atomic():
                    locked = (TrainerClientRelation.objects.select_for_update(of=('self',))
                              .get(pk=relation.pk))
                    month = running_month(locked)
                    if month is not None:
                        if str(request.data.get('at_period_end')).lower() in ('1', 'true', 'yes'):
                            locked.ends_at_period_end = True
                            locked.save(update_fields=['ends_at_period_end', 'updated_at'])
                            return Response({
                                'message': _('Coaching will end when the current month ends.'),
                                'client_id': client.id, 'scheduled': True,
                                'ends_at': month.ends_at}, status=status.HTTP_200_OK)
                        return Response({
                            'error': _('The client has a paid month running. You can end the '
                                       'coaching when it finishes.'),
                            'code': 'coaching_month_active', 'ends_at': month.ends_at},
                            status=status.HTTP_409_CONFLICT)

                    locked_client = CustomUser.objects.select_for_update(no_key=True).get(pk=client.pk)
                    if locked_client.assigned_trainer_id == request.user.id:
                        locked_client.assigned_trainer = None
                        locked_client.save(update_fields=['assigned_trainer'])

                    # Refund first, and only then let go of the record. `release_hold`
                    # raises when it cannot move the money, and deleting the relation
                    # anyway would destroy the only record of whose money is in escrow.
                    from .trainer_relations import HoldFailed, release_hold
                    try:
                        release_hold(locked, request.user)
                    except HoldFailed as exc:
                        logger.error("Refund failed for relation %s: %s", relation.id, exc)
                        transaction.set_rollback(True)
                        return Response(
                            {'error': _('We could not return the held payment. '
                                        'Nothing was changed; please try again.'),
                             'code': 'refund_failed'},
                            status=status.HTTP_409_CONFLICT
                        )
                    # Every row for this pair, not just the approved one: a stale pending
                    # or rejected row left the client's own request-status screen
                    # contradicting itself, and could re-trigger the block on choosing
                    # another trainer.
                    TrainerClientRelation.objects.filter(
                        trainer=request.user, client=client).delete()
                
                # Emit domain event — notification handled at delivery boundary
                _event = TrainerUnassignmentEvent(
                    actor_id=request.user.id,
                    recipient_id=client.id,
                    trainer_name=getattr(request.user, 'full_name', None) or request.user.username,
                )
                transaction.on_commit(lambda: emit_event(_event))
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
        # Raised, not returned, so field errors keep their `code` (see UpdateUserDetailsView).
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response({'message': _('Client profile updated successfully!')}, status=status.HTTP_200_OK)

class PublicTrainersListView(APIView):
    """Public view to get all available trainers without authentication"""
    permission_classes = [AllowAny]
    
    def get(self, request):
        """Get all available trainers (public endpoint). Supports ?search=name to filter by name."""
        from django.db.models import Count, Q
        from routine.views import StandardResultsSetPagination
        
        # Enabled AND verified. `is_active` alone used to hide unverified signups
        # because registration wrote them inactive; a pending signup is enabled now, so
        # without `email_verified` anyone could register as a trainer with an address
        # they do not own and be offered to clients before proving it.
        trainers = CustomUser.objects.filter(
            user_type='trainer',
            trainer_is_available=True,
            is_active=True,
            email_verified=True,
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
                    'trainer_hourly_rate': _shown_coaching_price(request, trainer.trainer_hourly_rate),
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
                'trainer_hourly_rate': _shown_coaching_price(request, trainer.trainer_hourly_rate),
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
        
        # Count total active trainers. Verified too: an unverified signup is enabled,
        # but it is not a trainer anyone can use yet.
        total_trainers_count = CustomUser.objects.filter(
            user_type='trainer',
            is_active=True,
            email_verified=True,
        ).count()
        
        return Response({
            'clients_with_trainers_count': clients_with_trainers_count,
            'total_trainers_count': total_trainers_count
        }, status=status.HTTP_200_OK)

class AvailableTrainersView(APIView):
    """GET /api/auth/client/available-trainers/ — trainers a client may ask, paged.

    `?search=` filters on first name, last name and username. Trainers blocked either
    way are not listed. `email` is always null: the key is kept for builds that read
    it, and a trainer's address is never on a card (`users/trainer_directory.py`). It
    was every trainer's address, to any signed-in client (SIDE_BUGS S-152).

    403 `permission_denied` when the caller is not a client.
    """
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
        
        # Verified as well as enabled; see PublicTrainersListView.
        # Blocked either way is not listed: a trainer this client cannot ask, and a
        # trainer who has blocked them, are not "available" to them.
        trainers = CustomUser.objects.filter(
            user_type='trainer',
            trainer_is_available=True,
            is_active=True,
            email_verified=True,
        ).exclude(
            id__in=blocked_ids(request.user)
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
                    # Never the address (SIDE_BUGS S-152): the key stays, null, for
                    # builds that read it. See the class docstring.
                    'email': None,
                    'first_name': trainer.first_name,
                    'last_name': trainer.last_name,
                    'profile_picture': trainer.profile_picture.url if trainer.profile_picture else None,
                    'trainer_bio': trainer.trainer_bio,
                    'trainer_specializations': trainer.trainer_specializations,
                    'trainer_certifications': trainer.trainer_certifications,
                    'trainer_experience_years': trainer.trainer_experience_years,
                    'trainer_hourly_rate': _shown_coaching_price(request, trainer.trainer_hourly_rate),
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
                'email': None,   # never the address; see the class docstring
                'first_name': trainer.first_name,
                'last_name': trainer.last_name,
                'profile_picture': trainer.profile_picture.url if trainer.profile_picture else None,
                'trainer_bio': trainer.trainer_bio,
                'trainer_specializations': trainer.trainer_specializations,
                'trainer_certifications': trainer.trainer_certifications,
                'trainer_experience_years': trainer.trainer_experience_years,
                'trainer_hourly_rate': _shown_coaching_price(request, trainer.trainer_hourly_rate),
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
        """Invalidate cache for all language variants.

        In `private_cache()`, where `get_queryset` and `retrieve` write these keys. This
        deleted them from the default cache (Redis DB0, sessions) while they live in
        DB3, so after an assign, an unassign or a profile change the trainer kept the
        old profile and the old approved-client list for up to five minutes
        (SIDE_BUGS S-153).
        """
        from django.conf import settings
        from training_platform.cache import private_cache

        store = private_cache()
        for lang_code, _lang_name in settings.LANGUAGES:
            store.delete(f"client_profile:{client_id}:{lang_code}:{CACHE_VERSION}")
            if trainer_id:
                store.delete(f"trainer:{trainer_id}:approved_clients:{lang_code}:{CACHE_VERSION}")

# NOTE: DeviceTokenRegisterView was removed — it was never routed (users/urls.py
# maps device-token/ to FCMTokenView) and its update_or_create never reactivated
# a soft-deleted token. FCMTokenView in users/device_token_views.py is the single
# device-token registration entry point.


class CustomTokenObtainPairSerializer(TokenObtainPairSerializer):
    """Custom JWT token serializer that includes user information"""
    
    @classmethod
    def get_token(cls, user):
        """The one place this serializer mints a credential, so the verification gate
        lives here: after `authenticate` has checked the password and refused a disabled
        account, and before any token exists.

        The gate used to be a lookup ahead of `super().validate()` testing
        `not user.is_active`, and it stopped gating anything once a pending signup was
        no longer inactive: measured, register 201 then POST /token/ 200 with access and
        refresh, no code ever entered. Being ahead of the password check it also told
        anyone without a password which addresses were unverified — and called a
        suspended account "unverified" too.
        """
        from rest_framework import serializers

        if not user.email_verified:
            raise serializers.ValidationError(
                {
                    "detail": _("Please verify your email address before logging in. Check your inbox for the OTP code."),
                    "requires_verification": True,
                    "email": user.email
                }
            )
        return super().get_token(user)

    def validate(self, attrs):
        from .utils import check_login_lockout, record_login_failure, clear_login_attempts
        from rest_framework.exceptions import AuthenticationFailed
        from rest_framework import serializers

        email = attrs.get('email') or attrs.get('username')  # Support both email and username
        # The stored spelling of the address (BACKEND_BUGS B-033): `Samer@Example.com`
        # with the right password was refused, and five such tries locked the real
        # account, whose counter is keyed on the lower-cased address.
        from .utils import find_by_email
        found = find_by_email(email) if isinstance(email, str) else None
        if found is not None and self.username_field in attrs:
            attrs[self.username_field] = found.email

        # Per-account brute-force lockout (5 failures → 15-min cooldown; users/utils.py)
        if email:
            is_locked, _remaining = check_login_lockout(email)
            if is_locked:
                from .exceptions import TooManyAttempts
                from .utils import lockout_seconds_left
                raise TooManyAttempts(lockout_seconds_left(email))

        # Authenticate, then mint through `get_token` above
        try:
            data = super().validate(attrs)
        except AuthenticationFailed:
            # DRF's AuthenticationFailed: simplejwt raises the base class, and the
            # simplejwt subclass this caught never matched, so /token/ counted no failed
            # attempt and its lockout never engaged (BACKEND_BUGS B-004).
            from .exceptions import AccountDisabled, disabled_account_with_password
            if disabled_account_with_password(email, attrs.get('password')):
                raise AccountDisabled()
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
    """POST /api/auth/client/request-trainer/ — a client asks a trainer to coach them.

    Body: `trainer_id` (required), `note` (optional, up to 500 characters; `message` is
    accepted as the same field for older builds). Headers: `X-App-Channel` — asking a
    trainer who charges is agreeing to pay them, so it answers to what the build may do.

    Nothing is charged here: month 1 of the coaching is held when the trainer accepts
    (users/coaching.py). The note is kept for this request and shown to that trainer
    only (`trainer/pending-requests/`), and to the client (`client/request-status/`).

    200 `{message, trainer_id, status: "pending"}`. Refusals, each with its `code`:
    400 `trainer_id_required`, `invalid_note`, `note_too_long`, `trainer_not_accepting`,
    `already_has_trainer`, `request_pending_elsewhere`, `already_with_this_trainer`,
    `request_already_pending`, `app_channel_required` (no known `X-App-Channel`, for a
    trainer who charges); 402 `insufficient_balance` (with `required`, `balance`);
    403 `permission_denied` (the caller is not a client), `not_available_on_channel`;
    404 `not_found` (no such trainer, or a `trainer_id` that is not a number).
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
        # The note to the trainer (RequestSheet), optional. `message` is the name older
        # builds sent it under; it used to be read and thrown away.
        note = request.data.get('note', request.data.get('message', '')) or ''
        if not isinstance(note, str):
            return Response({'error': _('The note must be text.'), 'code': 'invalid_note'},
                            status=status.HTTP_400_BAD_REQUEST)
        note = note.strip()
        if len(note) > CLIENT_NOTE_MAX_LENGTH:
            return Response(
                {'error': _('The note can be at most %(max)s characters.')
                 % {'max': CLIENT_NOTE_MAX_LENGTH}, 'code': 'note_too_long'},
                status=status.HTTP_400_BAD_REQUEST)
        
        if not trainer_id:
            return Response(
                {'error': _('trainer_id is required'), 'code': 'trainer_id_required'},
                status=status.HTTP_400_BAD_REQUEST
            )
        # An id that is not a number names no trainer. The lookup below raised
        # ValueError for `"abc"` and the broad handler answered a code-less 500.
        try:
            trainer_id = int(str(trainer_id))
        except (TypeError, ValueError):
            return Response({'error': _('Trainer not found'), 'code': 'not_found'},
                            status=status.HTTP_404_NOT_FOUND)
        
        try:
            from .models import TrainerClientRelation
            trainer = CustomUser.objects.get(id=trainer_id, user_type='trainer')
            # Neither side can start a coaching relationship with someone they have
            # blocked, or who has blocked them. First, before the wallet is touched
            # and before any row is written: a block that only stops the reading half
            # still lets the person it protects be contacted.
            if is_blocked(request.user, trainer):
                return Response(
                    {'error': _('This is not available between these two accounts.'),
                     'code': 'user_blocked'},
                    status=status.HTTP_403_FORBIDDEN
                )
            # Asking a trainer who charges is agreeing to pay them, so it answers to what
            # this build may do (policy/services.py). A trainer who charges nothing can be
            # asked from any build. Outside the `try` below, which swallows everything.
            if (getattr(trainer, 'trainer_hourly_rate', None) or 0) > 0:
                from policy.money import MoneyType
                from policy.services import require_purchase_allowed
                require_purchase_allowed(request, MoneyType.COACHING_1TO1)
            # Enforce wallet balance >= trainer charge before allowing request
            try:
                from wallet.models import Wallet
                trainer_charge = getattr(trainer, 'trainer_hourly_rate', None) or 0
                if trainer_charge and trainer_charge > 0:
                    client_wallet, _created = Wallet.objects.get_or_create(owner=request.user, defaults={"owner_type": "client"})
                    if client_wallet.balance < trainer_charge:
                        return Response(
                            {'error': _('Insufficient wallet balance to request this trainer'),
                             'code': 'insufficient_balance',
                             'required': str(trainer_charge), 'balance': str(client_wallet.balance)},
                            status=status.HTTP_402_PAYMENT_REQUIRED
                        )
            except Exception:
                # Optional side effect: swallowing this silently is what made the
                # surrounding failures invisible in logs. Control flow is unchanged.
                logger.debug('suppressed non-fatal error', exc_info=True)
            
            # Check if trainer is available
            if not trainer.trainer_is_available:
                return Response(
                    {'error': _('This trainer is not currently accepting new clients'),
                     'code': 'trainer_not_accepting'},
                    status=status.HTTP_400_BAD_REQUEST
                )
            
            # Enforce single-trainer rule: if client already assigned, block new requests
            if request.user.assigned_trainer is not None and request.user.assigned_trainer_id != trainer.id:
                return Response(
                    {'error': _('You are already assigned to a trainer. Unassign first to request another trainer.'),
                     'code': 'already_has_trainer'},
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
                    {'error': _('You already have a pending request with another trainer. Please wait for a response or cancel it first.'),
                     'code': 'request_pending_elsewhere'},
                    status=status.HTTP_400_BAD_REQUEST
                )
            
            # Check if relation already exists
            relation, created = TrainerClientRelation.objects.get_or_create(
                trainer=trainer,
                client=request.user,
                defaults={'status': 'pending', 'initiated_by': 'client',
                          # The price the client is agreeing to, right now. Read again
                          # at approval it was whatever the trainer had since set it to.
                          'agreed_rate': getattr(trainer, 'trainer_hourly_rate', None),
                          'requested_at': timezone.now(), 'responded_at': None,
                          'client_note': note}
            )
            
            if not created:
                if relation.status == 'approved':
                    return Response(
                        {'error': _('You are already assigned to this trainer'),
                         'code': 'already_with_this_trainer'},
                        status=status.HTTP_400_BAD_REQUEST
                    )
                elif relation.status == 'pending':
                    return Response(
                        {'error': _('Request is already pending with this trainer'),
                         'code': 'request_already_pending'},
                        status=status.HTTP_400_BAD_REQUEST
                    )
                elif relation.status == 'rejected':
                    # Allow new request after rejection
                    relation.status = 'pending'
                    relation.initiated_by = 'client'
                    relation.agreed_rate = getattr(trainer, 'trainer_hourly_rate', None)
                    relation.requested_at, relation.responded_at = timezone.now(), None
                    # This request's note, never the declined one's.
                    relation.client_note = note
                    relation.save(update_fields=['status', 'initiated_by', 'agreed_rate',
                                                 'requested_at', 'responded_at',
                                                 'client_note', 'updated_at'])
            
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
                {'error': _('Trainer not found'), 'code': 'not_found'},
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
    
    Endpoint: POST /api/auth/client/unassign-trainer/

    409 `refund_failed` when the held month could not be returned; nothing changed.
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
                    # The code the decline path answers for the same condition.
                    return Response(
                        {'error': _('We could not return your held payment. '
                                    'Nothing was changed; please try again.'),
                         'code': 'refund_failed'},
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

            # THE CLIENT'S OWN request, and only that. The filter was `client=..., 
            # status='pending'` with `.first()` under `-created_at` ordering, so a
            # stranger's INVITATION — a different thing, which is why `initiated_by`
            # exists — was the newest row and got destroyed instead, while the client
            # stayed locked to the trainer they were trying to leave.
            #
            # Locked and re-read INSIDE the transaction, and the status re-checked. It
            # used to select with no lock and then DELETE unconditionally: a client
            # tapping Cancel while the trainer tapped Approve produced two 200s, an
            # approved-then-deleted relation, and 100.00 of the client's money sitting
            # in escrow with its only anchor — `hold_reference`, which lives on that row
            # — destroyed. `unassign-trainer` then reported success and refunded nothing.
            # 4 of 30 contended attempts in an uninstrumented run.
            with transaction.atomic():
                # `of=('self',)`: without it the join to `trainer` locked the trainer's
                # user row FOR UPDATE as well, which blocks every ledger write whose
                # actor is that trainer (the lock order in `users/trainer_relations.py`).
                pending_request = (TrainerClientRelation.objects
                                   .select_for_update(of=('self',))
                                   .select_related('trainer')
                                   .filter(client=request.user, status='pending',
                                           initiated_by='client')
                                   .first())

                if not pending_request:
                    return Response(
                        {'error': _('You do not have a pending trainer request')},
                        status=status.HTTP_400_BAD_REQUEST
                    )

                trainer = pending_request.trainer
                trainer_id = trainer.id
                trainer_name = trainer.full_name or trainer.username

                # A row that holds money is never deleted. `hold_reference` is the only
                # record of whose funds are in escrow; releasing them needs it.
                if pending_request.hold_reference:
                    logger.error(
                        "relation %s is pending but holds escrow %s; refusing to delete",
                        pending_request.id, pending_request.hold_reference)
                    return Response(
                        {'error': _('That request is being processed. Try again shortly.'),
                         'code': 'request_in_flight'},
                        status=status.HTTP_409_CONFLICT
                    )

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
    """GET /api/auth/trainer/pending-requests/ — this trainer's pending rows, newest
    first.

    A row always has `request_id`, `client_id`, `client_name`, `client_username`,
    `initiated_by` and `created_at`. Only a request the CLIENT sent also carries their
    email, picture, age, gender, goals, activity level, `requested_at`, `status` and
    `note` — and not when either of the two has since blocked the other (SIDE_BUGS
    S-154): that row stays listed, so it can still be declined, but with the short
    shape, the same as an invitation the trainer sent. Nothing says which side
    blocked or that anyone did.

    403 `permission_denied` when the caller is not a trainer.
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
            ).select_related('client').order_by('-created_at', '-id')
            
            # Either direction, one query. A block does not touch pending rows, so a
            # client who asked and then blocked this trainer stayed listed here with
            # their email, age, gender, goals, activity level and note — disclosed to
            # the very person the block is against, until somebody answered.
            blocked = blocked_ids(request.user)

            requests_data = []
            for relation in pending_relations:
                client = relation.client
                # A client who ASKED has chosen to be seen; a client who was merely
                # invited has not answered yet and must not be disclosed. Trainer-side
                # invitations are unlimited and can name any client id, so this view was
                # a directory: register as a trainer, loop client_id 1..N, read the list
                # once, and take the full name, email address, age, gender and fitness
                # goals of the entire client base. The consent boundary was enforced at
                # APPROVAL and not at disclosure.
                asked_for_this = (relation.initiated_by == 'client'
                                  and client.id not in blocked)
                row = {
                    'request_id': relation.id,
                    'client_id': client.id,
                    'client_name': client.full_name or client.username,
                    'client_username': client.username,
                    'initiated_by': relation.initiated_by,
                    'created_at': relation.created_at,
                }
                if not asked_for_this:
                    requests_data.append(row)
                    continue
                requests_data.append({
                    **row,
                    'client_email': client.email,
                    'client_profile_picture': client.profile_picture.url if client.profile_picture else None,
                    'client_age': client.age,
                    'client_gender': client.gender,
                    'client_goals': client.client_goals,
                    'client_activity_level': client.activity_level,
                    # When THIS request was sent; `created_at` for rows older than that.
                    'requested_at': relation.requested_at or relation.created_at,
                    'status': relation.status,
                    'note': relation.client_note,
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

    Refusals carry a `code` — branch on it, never on the message:

      invalid_action            400  `action` is neither "approve" nor "reject"
      request_not_found         404  no pending invitation of yours by that id
      request_predates_consent  409  sent before `initiated_by` existed; ask for a new one
      wrong_party_to_answer     403  you sent this one; the trainer accepts it
      request_already_answered  409  answered while you were answering it
      trainer_unavailable       409  the trainer was deactivated in the meantime
      user_blocked              403  one of you blocked the other after it was sent
      refund_failed             409  the hold could not be returned; nothing changed
      client_has_other_trainer  400  you are already another trainer's client
      hold_failed               402  the month could not be held (e.g. your balance);
                                     the rate's own code instead when it could not be
                                     priced (`exchange_rate_missing`,
                                     `exchange_rate_stale`, `exchange_rate_unavailable`)

    `trainer/respond-to-request/` answers with the same codes for the mirror cases, so
    the app needs one branch for both sides.
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
            return Response({'error': _('action must be approve or reject'),
                             'code': 'invalid_action'},
                            status=status.HTTP_400_BAD_REQUEST)
        try:
            relation = TrainerClientRelation.objects.select_related('trainer').get(
                id=request.data.get('request_id'), client=request.user, status='pending')
        except (TrainerClientRelation.DoesNotExist, ValueError, TypeError):
            return Response({'error': _('Invitation not found or already processed'),
                             'code': 'request_not_found'},
                            status=status.HTTP_404_NOT_FOUND)

        if relation.initiated_by == 'unknown':
            return Response(
                {'error': _('This invitation predates the current consent flow. '
                            'Ask your trainer to send it again.'),
                 'code': 'request_predates_consent'},
                status=status.HTTP_409_CONFLICT)
        # Symmetric to the trainer side: you approve what the OTHER party asked for.
        if relation.initiated_by != 'trainer':
            return Response({'error': _('You made this request. The trainer has to accept it.'),
                             'code': 'wrong_party_to_answer'},
                            status=status.HTTP_403_FORBIDDEN)

        trainer_id, trainer = relation.trainer_id, relation.trainer
        if action == 'reject':
            # Under the approval's lock, never from the unlocked copy read above.
            refusal, relation = reject_trainer_relation(relation, request.user)
            if refusal is not None:
                return refusal
            transaction.on_commit(lambda: emit_event(ClientRequestRejectedEvent(
                actor_id=request.user.id, recipient_id=trainer_id,
                trainer_name=trainer.full_name or trainer.username)))
            return Response({'message': _('Invitation declined.'),
                             'trainer_id': trainer_id, 'status': 'rejected'})

        # Accepting an invitation that carries a price is the client agreeing to pay it,
        # so it answers to what this build may do. Declining above never does.
        from .trainer_relations import price_of
        if price_of(relation) > 0:
            from policy.money import MoneyType
            from policy.services import require_purchase_allowed
            require_purchase_allowed(request, MoneyType.COACHING_1TO1)

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
                .select_related('trainer').order_by('-created_at', '-id')[:50])
        return Response({'invitations': [{
            'request_id': r.id,
            'trainer_id': r.trainer_id,
            'trainer_name': r.trainer.full_name or r.trainer.username,
            'agreed_rate': _shown_coaching_price(request, r.agreed_rate),
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

    Locks are taken in the platform's one order — the relation row, then user rows
    (FOR NO KEY UPDATE, never FOR UPDATE), then wallets inside `move_funds_atomic` —
    see "Lock order" in `users/trainer_relations.py`.

    Returns (response_or_None, relation). A response means refuse with it.
    """
    from rest_framework import status as http
    from rest_framework.response import Response

    from .models import TrainerClientRelation
    from .trainer_relations import HoldFailed, take_hold

    with transaction.atomic():
        # `of=('self',)`: the relation row only. A bare `select_for_update()` with
        # `select_related` locks every joined row too, so this took FOR UPDATE on the
        # trainer's and the client's user rows before locking any wallet — the
        # deadlock with the same client's transfer described at `locked_client`.
        locked = (TrainerClientRelation.objects
                  .select_for_update(of=('self',))
                  .select_related('trainer', 'client')
                  .filter(pk=relation.pk)
                  .first())
        if locked is None:
            # Gone while we waited for the lock — a cancel, an unassign, or the other
            # party's erasure. `.get()` here raised DoesNotExist, which is not one of
            # the passthrough exceptions, so the client-side caller answered 500.
            return Response({'error': _('Request not found or already processed'),
                             'code': 'request_not_found'},
                            status=http.HTTP_404_NOT_FOUND), relation
        # The caller checked who asked on its unlocked copy, and consent depends on it:
        # you approve what the OTHER side asked for. A row declined and re-sent from the
        # other side in between is the same pk, pending again, and would otherwise be
        # approved by the party that just sent it.
        stale_initiator = relation.initiated_by
        relation = locked
        client, trainer = relation.client, relation.trainer

        if relation.status != 'pending' or relation.initiated_by != stale_initiator:
            return Response({'error': _('This request has already been answered'),
                             'code': 'request_already_answered'},
                            status=http.HTTP_409_CONFLICT), relation
        # A trainer who has since been deactivated cannot be accepted weeks later.
        if not (trainer.is_active and trainer.is_trainer):
            return Response({'error': _('That trainer is no longer available'),
                             'code': 'trainer_unavailable'},
                            status=http.HTTP_409_CONFLICT), relation
        # Nor can a request that was sent before one of them blocked the other be
        # accepted afterwards. Refusing only the SEND would make a block trivial to
        # walk around: send the request, wait to be blocked, accept anyway. The row
        # stays pending and can be declined or cancelled in the ordinary way.
        if is_blocked(trainer, client):
            return Response({'error': _('This is not available between these two accounts.'),
                             'code': 'user_blocked'},
                            status=http.HTTP_403_FORBIDDEN), relation

        # FOR NO KEY UPDATE, which still serialises two approvals for this client but no
        # longer blocks FOR KEY SHARE. Every ledger row written with `actor=client`
        # takes FOR KEY SHARE on this row — the deferred foreign-key check at COMMIT —
        # after its wallets are locked, while this transaction held the row FOR UPDATE
        # and then waited for those same wallets. Measured: 20 deadlocks in 60 contended
        # approve/transfer pairs; transfers answered 500 with their idempotency key
        # stuck, approvals 402 with Postgres internals in the body.
        locked_client = CustomUser.objects.select_for_update(no_key=True).get(pk=client.pk)
        # One code for both: either way the client is already someone else's, and the
        # app has one thing to say about it (SIDE_BUGS S-155).
        if locked_client.assigned_trainer_id and locked_client.assigned_trainer_id != trainer.id:
            return Response({'error': _('Client is already assigned to another trainer'),
                             'code': 'client_has_other_trainer'},
                            status=http.HTTP_400_BAD_REQUEST), relation
        if TrainerClientRelation.objects.filter(
                client=client, status='approved').exclude(trainer=trainer).exists():
            return Response({'error': _('Client already has an approved relation with another trainer'),
                             'code': 'client_has_other_trainer'},
                            status=http.HTTP_400_BAD_REQUEST), relation

        try:
            take_hold(relation, actor)
        except HoldFailed as exc:
            logger.warning("Trainer hold failed for relation %s: %s", relation.id, exc)
            # The failure's own code when it has one (an `exchange_rate_*` code, set by
            # `take_hold`), `hold_failed` otherwise — this was the one refusal on the
            # approve path with no code at all.
            return Response({'error': _('The trainer charge could not be held: %(why)s')
                             % {'why': str(exc)},
                             'code': getattr(exc, 'code', None) or 'hold_failed'},
                            status=http.HTTP_402_PAYMENT_REQUIRED), relation

        relation.status = 'approved'
        relation.responded_at = timezone.now()
        # A new coaching starts renewing: a stop a trainer scheduled on an earlier
        # approval of this same row is not this one's.
        relation.ends_at_period_end = False
        relation.save(update_fields=['status', 'responded_at', 'ends_at_period_end',
                                     'updated_at'])
        _record_outcome(relation)
        locked_client.assigned_trainer = trainer
        locked_client.save(update_fields=['assigned_trainer'])
    return None, relation


def reject_trainer_relation(relation, actor):
    """Decline a pending trainer/client relation, whichever side is declining.

    The counterpart of `approve_trainer_relation`, and under the same row lock. Both
    reject paths read the row with no lock and then wrote it unconditionally, so a
    reject that had read 'pending' waited out a concurrent approval and then overwrote
    it. The trainer path's full `save()` of its stale copy also put back
    `hold_reference=''`, erasing the only link to the escrow transaction the approval
    had just committed; the client path left the hold on a rejected row. Measured: both
    calls 200, client charged 100.00, relation 'rejected', escrow 100.00 over its open
    holds, and every unassign and cancel endpoint answered without refunding — a retry
    then charged the client a second time.

    Locked, the status re-read, and only `status` written. A row that somehow carries a
    hold while pending is refunded before it is declined, because nothing is being
    granted for that money.

    Returns (response_or_None, relation). A response means refuse with it.
    """
    from rest_framework import status as http
    from rest_framework.response import Response

    from .models import TrainerClientRelation
    from .trainer_relations import HoldFailed, release_hold

    with transaction.atomic():
        locked = (TrainerClientRelation.objects
                  .select_for_update(of=('self',))
                  .filter(pk=relation.pk)
                  .first())
        if locked is None:
            # Gone while we waited for the lock; see `approve_trainer_relation`.
            return Response({'error': _('Request not found or already processed'),
                             'code': 'request_not_found'},
                            status=http.HTTP_404_NOT_FOUND), relation
        stale_initiator = relation.initiated_by   # see `approve_trainer_relation`
        relation = locked
        if relation.status != 'pending' or relation.initiated_by != stale_initiator:
            return Response({'error': _('This request has already been answered'),
                             'code': 'request_already_answered'},
                            status=http.HTTP_409_CONFLICT), relation
        if relation.hold_reference:
            try:
                release_hold(relation, actor)
            except HoldFailed as exc:
                logger.error("Refund failed while declining relation %s: %s",
                             relation.id, exc)
                return Response({'error': _('We could not return the held payment. '
                                            'Nothing was changed; please try again.'),
                                 'code': 'refund_failed'},
                                status=http.HTTP_409_CONFLICT), relation
        relation.status = 'rejected'
        relation.responded_at = timezone.now()
        relation.save(update_fields=['status', 'responded_at', 'updated_at'])
        _record_outcome(relation)
    return None, relation


def _record_outcome(relation):
    """Keep the answer beyond the life of the relation row (`TrainerRequestOutcome`).

    Called inside the answering transaction, under the relation's row lock, so the
    record and the status change commit together. A request sent before
    `requested_at` existed has no start time and is not recorded: a response time
    guessed from `created_at` would be wrong for every re-sent request.
    """
    from .models import TrainerRequestOutcome

    if relation.requested_at is None or relation.responded_at is None:
        return
    TrainerRequestOutcome.objects.create(
        trainer_id=relation.trainer_id, client_id=relation.client_id,
        initiated_by=relation.initiated_by, outcome=relation.status,
        requested_at=relation.requested_at,
        responded_at=max(relation.responded_at, relation.requested_at))


class TrainerRespondToRequestView(APIView):
    """
    Enhanced view for trainers to approve or reject client requests.
    
    Features:
    - Approve or reject client requests
    - Automatic client assignment on approval
    - Sends notifications to clients
    - Updates relationship status
    - Proper validation and error handling

    POST {"request_id": <id>, "action": "approve" | "reject"}

    Refusals carry a `code` — branch on it, never on the message. The same set as
    `client/respond-to-invite/`, from the same two shared helpers:

      request_id_required       400  no `request_id` in the body
      invalid_action            400  `action` is neither "approve" nor "reject"
      request_not_found         404  no pending request of yours by that id
      request_predates_consent  409  sent before `initiated_by` existed; ask for a new one
      wrong_party_to_answer     403  you invited them; the client accepts it
      request_already_answered  409  answered while you were answering it
      trainer_unavailable       409  the trainer was deactivated in the meantime
      user_blocked              403  one of you blocked the other after it was sent
      refund_failed             409  the hold could not be returned; nothing changed
      client_has_other_trainer  400  the client is already another trainer's
      hold_failed               402  the month could not be held (e.g. the client's
                                     balance); the rate's own code instead when it
                                     could not be priced (`exchange_rate_missing`,
                                     `exchange_rate_stale`, `exchange_rate_unavailable`)

    A `request_id` that is not a number answers `request_not_found`.

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
                {'error': _('request_id is required'), 'code': 'request_id_required'},
                status=status.HTTP_400_BAD_REQUEST
            )

        if action not in ['approve', 'reject']:
            return Response(
                {'error': _('action must be either "approve" or "reject"'),
                 'code': 'invalid_action'},
                status=status.HTTP_400_BAD_REQUEST
            )
        # An id that is not a number names no request of yours: the same answer the
        # client side gives. The lookup raised ValueError for `"abc"`, which only the
        # broad handler below caught, as a code-less 500 (SIDE_BUGS S-155).
        try:
            request_id = int(str(request_id))
        except (TypeError, ValueError):
            return Response(
                {'error': _('Request not found or already processed'),
                 'code': 'request_not_found'},
                status=status.HTTP_404_NOT_FOUND
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
                                'Ask the client to send it again.'),
                     'code': 'request_predates_consent'},
                    status=status.HTTP_409_CONFLICT
                )
            if relation.initiated_by != 'client':
                return Response(
                    {'error': _('You invited this client. They have to accept the invitation.'),
                     'code': 'wrong_party_to_answer'},
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
                # Under the approval's lock, never from the unlocked copy read above:
                # its full `save()` wrote `status='rejected', hold_reference=''` over an
                # approval that had committed in between.
                refusal, relation = reject_trainer_relation(relation, request.user)
                if refusal is not None:
                    return refusal
                
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
                {'error': _('Request not found or already processed'),
                 'code': 'request_not_found'},
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
    """GET /api/auth/client/request-status/ — every request this client has with a
    trainer, newest first, whatever its status.

    `trainer_email` is always null: the key is kept for builds that read it, and a
    trainer's address is never disclosed to a client — it was, for every trainer the
    client had ever asked, declined ones included (SIDE_BUGS S-152).

    A request to a trainer one of them has since blocked stays listed, because it is
    still this client's to cancel (`client/cancel-trainer-request/`) and, while it is
    pending, it is the one outstanding request that stops them asking anyone else.
    Of the trainer it carries nothing beyond their public card.

    403 `permission_denied` when the caller is not a client.
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
            ).select_related('trainer').order_by('-created_at', '-id')
            
            requests_data = []
            for relation in client_relations:
                trainer = relation.trainer
                requests_data.append({
                    'request_id': relation.id,
                    'trainer_id': trainer.id,
                    'trainer_name': trainer.full_name or trainer.username,
                    # Never the address (SIDE_BUGS S-152), also for a declined
                    # request: the key stays, null, for builds that read it.
                    'trainer_email': None,
                    'trainer_profile_picture': trainer.profile_picture.url if trainer.profile_picture else None,
                    'trainer_bio': trainer.trainer_bio,
                    'trainer_specializations': trainer.trainer_specializations,
                    'trainer_experience_years': trainer.trainer_experience_years,
                    'trainer_hourly_rate': _shown_coaching_price(request, trainer.trainer_hourly_rate),
                    # When THIS request was sent (a re-sent request reuses the row).
                    'requested_at': relation.requested_at or relation.created_at,
                    'status': relation.status,
                    # Additive (B-008): the answer's time ("training together since"),
                    # the price this client agreed to, and their note.
                    'responded_at': relation.responded_at,
                    'agreed_rate': _shown_coaching_price(request, relation.agreed_rate),
                    'note': relation.client_note,
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
    Accepts multipart/form-data with image file (`profile_picture`).

    400 with the refusal's code (`file_too_large`, `unsupported_format`,
    `image_dimensions_too_large`, ...); 500 `upload_failed` when storage fails — the
    body never carries the exception's text.
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
                # With the refusal's own code (`file_too_large`, `unsupported_format`,
                # `image_dimensions_too_large`, ...), not the generic `bad_request`.
                return Response(
                    {'error': e.messages[0] if getattr(e, 'messages', None) else str(e),
                     'code': getattr(e, 'code', None) or 'invalid_image'},
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
        except Exception:
            # Logged here, never sent: the exception text carried storage paths and
            # library messages, untranslated and with no code (BACKEND_BUGS B-024).
            logger.exception("Profile picture upload failed for user %s", request.user.pk)
            message = _('The picture could not be saved. Please try again.')
            return Response(
                {'error': message, 'detail': message, 'code': 'upload_failed'},
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
        except Exception:
            logger.exception("Profile picture removal failed for user %s", request.user.pk)
            message = _('The picture could not be removed. Please try again.')
            return Response(
                {'error': message, 'detail': message, 'code': 'upload_failed'},
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

    Body: `{email}`, matched lower-cased. Limit: 3 an hour per address, then 429
    `{error, detail, code: "throttled", retry_after_seconds}` with `Retry-After` — the
    seconds really left in the hour (BACKEND_BUGS B-022); the same for every address.
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
            # The seconds left on the hour and a code (BACKEND_BUGS B-022).
            from training_platform.cache import seconds_left
            wait = seconds_left(rl, cache_key, 3600)
            message = _('Too many password reset requests. Please wait 1 hour before trying again.')
            return Response(
                {'error': message, 'detail': message, 'code': 'throttled',
                 'retry_after_seconds': wait},
                status=status.HTTP_429_TOO_MANY_REQUESTS, headers={'Retry-After': str(wait)}
            )

        try:
            # Without regard to case: the address is stored lower-cased (B-033).
            from .utils import find_by_email
            user = find_by_email(email, is_active=True)
            if user is None:
                raise CustomUser.DoesNotExist
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

    Body: `{email, otp_code}`. On success, 200 `{message, reset_token}`: a single-use
    token (valid for 15 minutes) to submit with the new password.
    Refusals: 400 `{error, detail, code}` with `code` one of `otp_invalid` (a wrong code,
    no pending code, or an unknown address — one answer for all three, so addresses
    cannot be enumerated), `otp_expired` (the right code, too late: ask for a new one) or
    `too_many_attempts` (five wrong codes for this address and purpose lock it for 15
    minutes; carries `retry_after_seconds` and `Retry-After`; the right code is refused
    too while locked). The address is matched lower-cased (BACKEND_BUGS B-021, B-033).
    """
    permission_classes = [AllowAny]

    def post(self, request):
        from .serializers import PasswordResetVerifySerializer
        from .models import PasswordResetToken
        from .utils import otp_refusal_response, verify_otp_detailed
        from datetime import timedelta

        serializer = PasswordResetVerifySerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        email = serializer.validated_data['email']
        otp_code = serializer.validated_data['otp_code']

        success, otp_instance, error_message, code, retry_after = verify_otp_detailed(
            email, otp_code, purpose='password_reset')

        if not success:
            # `otp_invalid`, `otp_expired` or `too_many_attempts` (BACKEND_BUGS B-021).
            return otp_refusal_response(error_message, code, retry_after)

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

    Body: `{reset_token, new_password}`. Consumes the single-use token and updates the
    user's password. The new password answers the same validator codes as sign-up on
    `field_errors.new_password` (`password_too_short`, `password_too_common`,
    `password_entirely_numeric`; BACKEND_BUGS B-034).
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

        # The person just proved they own the address: the failures that locked them out
        # are forgiven, or they were locked out again right after resetting (B-017).
        from .utils import clear_login_attempts
        clear_login_attempts(user.email)

        logger.info(f"Password reset completed for user {user.id} ({user.email})")

        return Response({
            'message': _('Your password has been reset successfully. You can now log in with your new password.'),
        }, status=status.HTTP_200_OK)
