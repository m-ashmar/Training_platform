from rest_framework import viewsets, status, permissions, generics, serializers
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.pagination import PageNumberPagination
from django.db import models, IntegrityError, transaction
from django.db.models import Sum, Avg, Count, F, Q, Prefetch
from django.utils import timezone
from django.utils.dateparse import parse_date
from django.utils.translation import gettext as _
from datetime import datetime, timedelta, date
from .models import (
    Routine, Exercise, RoutineExercise, RoutineProgress, ExerciseSetLog, WorkoutSession,
    RoutineTemplate, RoutineTemplateExercise, UserExerciseProgress, ExerciseMedia
)
from .serializers import (
    ExerciseSerializer, ExerciseCreateWithImageSerializer, RoutineSerializer, 
    RoutineExerciseSerializer, UserExerciseProgressSerializer, RoutineProgressSerializer,
    UserRoutineSerializer, ExerciseSetLogSerializer, TrainerRoutineSerializer,
    ClientProfileViewSerializer, WorkoutSessionSerializer, RoutineTemplateSerializer,
    RoutineTemplateExerciseSerializer, ExerciseMediaSerializer,
    DetailedClientProgressSerializer, RecentActivitySerializer, WorkoutSessionDetailSerializer
)
from .permissions import (
    IsAdminOrOwnerOrReadOnly, 
    IsTrainerOrAdmin,
    IsTrainerOrAdminForAssignment,
    IsSetLogCreatorOrTrainerOrAdmin,
    IsRoutineOwnerOrAssigned,
    IsClientOrAssignedTrainer # Added for RoutineProgressViewSet
)
import logging
from .services import send_notification
from django.utils import timezone
from datetime import timedelta
from rest_framework.permissions import IsAuthenticated
from django_filters.rest_framework import DjangoFilterBackend
from rest_framework.parsers import MultiPartParser, FormParser
from rest_framework.views import APIView
from users.models import CustomUser
from rest_framework.exceptions import PermissionDenied
from django.db import transaction
from django.http import Http404
from rest_framework.exceptions import (NotAuthenticated, NotFound, PermissionDenied,
                                       ValidationError as DRFValidationError)
from training_platform.api_exceptions import PASSTHROUGH_EXCEPTIONS
from training_platform.query_params import int_param

logger = logging.getLogger(__name__)

# --- Real-world bounds for workout logging -----------------------------------
# Heaviest sanctioned powerlifting total is well under 600 kg; 1000 leaves head-room
# while rejecting typos and junk. Backlog window allows correcting a missed session.
MAX_SETS_PER_REQUEST = 50
MAX_WEIGHT_KG = 1000
MAX_REPS_PER_SET = 1000
MAX_BACKLOG_DAYS = 365


class StandardResultsSetPagination(PageNumberPagination):
    page_size = 20
    page_size_query_param = 'page_size'
    max_page_size = 100


class RecentActivityProgressView(APIView):
    """
    GET /api/routine/v1/analytics/recent-progress/

    Returns the last 7 calendar days of the authenticated user's active workout
    time (in seconds), including days with 0 duration.

    Implementation details:
    - Duration is aggregated at DB level via TruncDate + Sum (no Python loops)
    - Day boundaries are computed in the server's local timezone (timezone-aware)
    - Only completed sessions with a valid end_time are counted
    - Results are cached per-user in private_cache (Redis DB3) for 120s
    - Cache is busted on WorkoutSession completion via post_save signal
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        from django.db.models import ExpressionWrapper, DurationField
        from django.db.models.functions import TruncDate
        # NOTE: the module is training_platform.cache — there is no cache_backends
        # module. The wrong import raised ImportError on every request here.
        from training_platform.cache import private_cache
        from datetime import timedelta, date

        cache_key = f"recent_progress:{request.user.id}"
        cached = private_cache().get(cache_key)
        if cached is not None:
            return Response(cached)

        # Timezone-aware 7-day window aligned to local calendar days
        now = timezone.localtime()
        start_of_today = now.replace(hour=0, minute=0, second=0, microsecond=0)
        window_start = start_of_today - timedelta(days=6)

        # DB-level aggregation: push all computation to Postgres
        qs = (
            WorkoutSession.objects
            .filter(
                user=request.user,
                status='completed',
                end_time__isnull=False,       # guard against active/null sessions
                start_time__gte=window_start,
            )
            .annotate(
                day=TruncDate('start_time'),
                session_duration=ExpressionWrapper(
                    F('end_time') - F('start_time'),
                    output_field=DurationField()
                )
            )
            .values('day')
            .annotate(total_duration=Sum('session_duration'))
        )

        # Build a lookup map: date → total seconds
        results_map = {}
        for entry in qs:
            total_secs = entry['total_duration'].total_seconds() if entry['total_duration'] else 0
            results_map[entry['day']] = int(total_secs)

        # Deterministic 7-day output — always include every day, even zeros
        recent_progress = []
        for i in range(7):
            day: date = (window_start + timedelta(days=i)).date()
            recent_progress.append({
                "date": day.isoformat(),          # "2026-04-11" — clean, unambiguous
                "duration": results_map.get(day, 0)
            })

        data = {"recent_progress": recent_progress}
        private_cache().set(cache_key, data, 120)
        return Response(data)


class ExerciseViewSet(viewsets.ModelViewSet):

    """ViewSet for managing exercises."""
    queryset = Exercise.objects.all()
    serializer_class = ExerciseSerializer
    permission_classes = [IsAdminOrOwnerOrReadOnly]
    pagination_class = StandardResultsSetPagination

    def get_queryset(self):
        """Filter exercises based on user role with optimized queries."""
        user = self.request.user
        
        # Optimize: prefetch media to avoid N+1 in serializer
        base_qs = self.queryset.prefetch_related('media').select_related('created_by')
        
        if user.is_admin:
            return base_qs
        elif user.is_trainer:
            return base_qs.filter(
                models.Q(created_by=user) | models.Q(created_by__isnull=True)
            )
        else:
            # Clients: global exercises, plus anything their own trainer created,
            # plus anything appearing in a routine assigned to them.
            # This branch previously returned base_qs unfiltered, so every client
            # could see every trainer's private custom exercises.
            return base_qs.filter(
                models.Q(created_by__isnull=True)
                | models.Q(created_by=user.assigned_trainer)
                | models.Q(routine_exercises__routine__assigned_to=user)
            ).distinct()

    def perform_create(self, serializer):
        """Override to assign the exercise to the creator."""
        serializer.save(created_by=self.request.user)

    def destroy(self, request, *args, **kwargs):
        """
        Refuse to delete a routine that has recorded progress — deleting it would
        erase the client's training history. Deactivate it instead.
        """
        from django.db.models import ProtectedError
        try:
            return super().destroy(request, *args, **kwargs)
        except ProtectedError:
            return Response(
                {'error': _('This routine has recorded client progress and cannot be '
                            'deleted. Set is_active=false to retire it instead.')},
                status=status.HTTP_409_CONFLICT,
            )


class ExerciseCreateWithImageView(APIView):
    """
    Enhanced endpoint for creating exercises with optional image upload and media content.
    
    Accepts multipart/form-data with exercise data and optional media files/URLs.
    
    Supported media types:
    - Main exercise image (demonstration image)
    - Additional photos (uploaded files)  
    - Videos (URLs to video content)
    - Text descriptions (additional text content)
    
    Request format:
    - name (required): Exercise name
    - description (required): Exercise description  
    - target_muscle (required): Target muscle group
    - difficulty_level (optional): beginner/intermediate/advanced/expert
    - image (optional): Main demonstration image file
    - media_photos (optional): Additional photo files (multiple)
    - media_videos (optional): Video URLs (comma-separated)
    - media_texts (optional): Additional text content (comma-separated)
    """
    permission_classes = [IsAuthenticated]
    parser_classes = [MultiPartParser, FormParser]

    # Atomic: these writes were independent, so a failure part-way left the
    # records inconsistent (a half-applied password reset either locks the
    # user out or leaves a consumed token usable).
    @transaction.atomic
    def post(self, request):
        """Create a new exercise with optional image and media content"""
        from .models import ExerciseMedia
        import re
        
        try:
            # Validate required fields
            required_fields = ['name', 'description', 'target_muscle']
            for field in required_fields:
                if not request.data.get(field):
                    return Response(
                        {'error': f'Missing required field: {field}'},
                        status=status.HTTP_400_BAD_REQUEST
                    )

            # Get the main exercise image
            image_file = request.FILES.get('image')
            
            # Validate main image file if provided
            if image_file:
                # Validate file type
                # Validate CONTENT (magic bytes), cap pixel dimensions and re-encode.

                # The client Content-Type header was the only previous check and is

                # spoofable — PHP/SVG payloads were accepted and stored as .php/.svg.

                from training_platform.file_security import process_uploaded_image

                from django.core.exceptions import ValidationError as DjangoValidationError

                try:

                    image_file, _img_ext = process_uploaded_image(image_file, max_bytes=5 * 1024 * 1024)

                except DjangoValidationError as e:

                    return Response(

                        {'error': e.messages[0] if getattr(e, 'messages', None) else str(e)},

                        status=status.HTTP_400_BAD_REQUEST)

            # Validate additional media photos if provided
            media_photos = request.FILES.getlist('media_photos')
            if media_photos:
                from training_platform.file_security import process_uploaded_image
                from django.core.exceptions import ValidationError as DjangoValidationError
                validated_photos = []
                for i, photo in enumerate(media_photos):
                    # Same content validation as the main image — header checks alone
                    # let arbitrary payloads through.
                    try:
                        safe_photo, _ext = process_uploaded_image(photo, max_bytes=5 * 1024 * 1024)
                        validated_photos.append(safe_photo)
                    except DjangoValidationError as e:
                        msg = e.messages[0] if getattr(e, 'messages', None) else str(e)
                        return Response(
                            {'error': f'Media photo {i+1}: {msg}'},
                            status=status.HTTP_400_BAD_REQUEST
                        )

            # Validate video URLs if provided
            media_videos = request.data.get('media_videos', '')
            video_urls = []
            if media_videos:
                video_urls = [url.strip() for url in media_videos.split(',') if url.strip()]
                # Basic URL validation
                url_pattern = re.compile(
                    r'^https?://'  # http:// or https://
                    r'(?:(?:[A-Z0-9](?:[A-Z0-9-]{0,61}[A-Z0-9])?\.)+[A-Z]{2,6}\.?|'  # domain...
                    r'localhost|'  # localhost...
                    r'\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})'  # ...or ip
                    r'(?::\d+)?'  # optional port
                    r'(?:/?|[/?]\S+)$', re.IGNORECASE)
                
                for i, url in enumerate(video_urls):
                    if not url_pattern.match(url):
                        return Response(
                            {'error': f'Invalid video URL format: {url}'},
                            status=status.HTTP_400_BAD_REQUEST
                        )

            # Get additional text content
            media_texts = request.data.get('media_texts', '')
            text_contents = []
            if media_texts:
                text_contents = [text.strip() for text in media_texts.split('||') if text.strip()]

            # Create exercise data
            exercise_data = {
                'name': request.data.get('name'),
                'description': request.data.get('description'),
                'target_muscle': request.data.get('target_muscle'),
                'difficulty_level': request.data.get('difficulty_level', 'beginner'),
            }

            # Create the exercise
            exercise = Exercise.objects.create(
                created_by=request.user,
                **exercise_data
            )

            # Set main image if provided
            if image_file:
                exercise.image = image_file
                exercise.save()

            # Create additional media records
            media_created = []
            
            # Handle additional photo uploads
            for i, photo_file in enumerate(media_photos):
                # For uploaded photos, we need to save them and create a URL
                # Since ExerciseMedia.content expects URLs, we'll store the file path
                photo_name = f"exercise_media_{exercise.id}_{i}_{photo_file.name}"
                
                # Save photo to media storage (you might want to use a specific upload path)
                from django.core.files.storage import default_storage
                import os
                
                # Create a proper path for exercise media
                photo_path = f"exercise_media/{exercise.id}/{photo_name}"
                saved_path = default_storage.save(photo_path, photo_file)
                photo_url = default_storage.url(saved_path)
                
                # Create ExerciseMedia record
                media_record = ExerciseMedia.objects.create(
                    exercise=exercise,
                    media_type='photo',
                    content=photo_url,
                    title=f"Exercise Photo {i+1}",
                    description=f"Additional demonstration photo for {exercise.name}",
                    order=i
                )
                media_created.append(media_record)
            
            # Handle video URLs
            for i, video_url in enumerate(video_urls):
                media_record = ExerciseMedia.objects.create(
                    exercise=exercise,
                    media_type='video',
                    content=video_url,
                    title=f"Exercise Video {i+1}",
                    description=f"Demonstration video for {exercise.name}",
                    order=len(media_photos) + i
                )
                media_created.append(media_record)
            
            # Handle text content
            for i, text_content in enumerate(text_contents):
                media_record = ExerciseMedia.objects.create(
                    exercise=exercise,
                    media_type='text',
                    content=text_content,
                    title=f"Additional Instructions {i+1}",
                    description=f"Additional text content for {exercise.name}",
                    order=len(media_photos) + len(video_urls) + i
                )
                media_created.append(media_record)

            # Serialize and return response
            serializer = ExerciseCreateWithImageSerializer(exercise, context={'request': request})
            
            return Response({
                'message': _('Exercise created successfully'),
                'exercise': serializer.data,
                'media_created': len(media_created),
                'media_breakdown': {
                    'photos': len(media_photos),
                    'videos': len(video_urls), 
                    'texts': len(text_contents)
                }
            }, status=status.HTTP_201_CREATED)

        except PASSTHROUGH_EXCEPTIONS:
            # Control-flow exceptions carry their own correct status (404/403/401/400).
            # The broad handler below turned every one of them into a 500, which is the
            # wrong contract for the client and hides real faults in the error budget.
            raise
        except Exception as e:
            logger.error(f"Error creating exercise with media: {str(e)}")
            return Response(
                {'error': f'Failed to create exercise: {str(e)}'},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )


class RoutineViewSet(viewsets.ModelViewSet):
    """
    Enhanced ViewSet for managing routines with improved assignment logic.
    
    Features:
    - Trainers can create and assign routines only to their approved clients
    - Assignment validation ensures only approved trainer-client relationships
    - Comprehensive error handling and logging
    - Support for notifications on assignment
    
    TODO: Add routine templates and cloning functionality
    TODO: Implement routine sharing between trainers
    TODO: Add routine analytics and performance tracking
    """
    queryset = Routine.objects.all()
    serializer_class = RoutineSerializer
    permission_classes = [IsTrainerOrAdmin]
    pagination_class = StandardResultsSetPagination

    def get_permissions(self):
        if self.action in ['list', 'retrieve']:
            return [IsRoutineOwnerOrAssigned()]
        return super().get_permissions()

    def get_queryset(self):
        """Filter routines based on user role"""
        user = self.request.user
        
        # Base optimization for all queries
        queryset = self.queryset.select_related('created_by').prefetch_related(
            'routine_exercises__exercise__media',
            'assigned_to',
            'progress'
        ).annotate(
            client_count=models.Count('assigned_to', filter=models.Q(assigned_to__user_type='client'), distinct=True)
        # annotate() with an aggregate adds a GROUP BY, and Django then drops
        # Meta.ordering from the SQL entirely — the query came back with GROUP BY and
        # no ORDER BY at all. Paging a routine list in whatever order Postgres felt
        # like meant page 2 could repeat a routine from page 1 and skip another
        # outright. Re-apply the model's total order explicitly.
        ).order_by('-created_at', '-id')
        
        if user.is_admin:
            # Admins can see all routines
            return queryset.all()
        elif user.is_trainer:
            # Trainers can see routines they created and routines assigned to their clients
            return queryset.filter(
                models.Q(created_by=user) | 
                models.Q(assigned_to__assigned_trainer=user)
            ).distinct()
        else:
            # Clients can see routines assigned to them
            return queryset.filter(assigned_to=user)

    def get_serializer_class(self):
        """Use different serializers based on user role"""
        if self.request.user.is_trainer and self.action in ['list', 'retrieve']:
            return TrainerRoutineSerializer
        return RoutineSerializer

    def perform_create(self, serializer):
        """Override to assign the routine to the creator."""
        serializer.save(created_by=self.request.user)

    @action(detail=True, methods=['post'], permission_classes=[IsTrainerOrAdminForAssignment])
    def assign_to_client(self, request, pk=None):
        """
        Enhanced routine assignment to a client with improved validation.
        
        Requirements:
        - Only trainers and admins can assign routines
        - Client must have an approved TrainerClientRelation with the trainer
        - Assignment triggers notifications
        
        TODO: Add assignment scheduling and conflict checking
        TODO: Implement assignment limits per client
        TODO: Add assignment history tracking
        """
        routine = self.get_object()
        client_id = request.data.get('client_id')
        
        if not client_id:
            return Response(
                {"error": "client_id is required"}, 
                status=status.HTTP_400_BAD_REQUEST
            )
        
        try:
            from users.models import CustomUser, TrainerClientRelation
            
            # Get the client
            client = CustomUser.objects.get(id=client_id, user_type='client')
            
            # Enhanced validation for trainer-client relationship
            if request.user.is_trainer:
                # Check if trainer-client relation is approved
                relation = TrainerClientRelation.objects.filter(
                    trainer=request.user, 
                    client=client, 
                    status='approved'
                ).first()
                
                if not relation:
                    logger.warning(
                        f"Trainer {request.user.id} attempted to assign routine {routine.id} "
                        f"to unapproved client {client.id}"
                    )
                    return Response(
                        {
                            "error": "You can only assign routines to your approved clients.",
                            "details": f"Client {client.username} is not in your approved client list."
                        }, 
                        status=status.HTTP_400_BAD_REQUEST
                    )
            
            # Check if routine is already assigned to this client
            if client in routine.assigned_to.all():
                return Response(
                    {
                        "error": "Routine is already assigned to this client",
                        "routine_id": routine.id,
                        "client_id": client.id
                    }, 
                    status=status.HTTP_400_BAD_REQUEST
                )
            
            # Assign routine to client
            routine.assigned_to.add(client)

            # On assignment, settle escrow to trainer wallet
            try:
                from wallet.models import Wallet, move_funds_atomic
                from wallet.utils import get_escrow_wallet
                trainer_charge = getattr(request.user, 'trainer_hourly_rate', None) or 0
                if trainer_charge and trainer_charge > 0:
                    escrow = get_escrow_wallet()
                    trainer_wallet, _created = Wallet.objects.get_or_create(owner=request.user, defaults={"owner_type": "trainer"})
                    move_funds_atomic(escrow, trainer_wallet, trainer_charge, actor_id=request.user.id, tx_type='transfer', metadata={'purpose': 'trainer_assignment_settlement', 'client_id': client.id, 'routine_id': routine.id})
            except Exception as e:
                logger.error(f"Escrow settlement failed: {str(e)}")
            
            # Log the assignment
            logger.info(
                f"Routine {routine.id} assigned to client {client.id} by trainer {request.user.id}"
            )
            
            # Trigger notification to client about new assignment
            send_notification(
                user=client,
                notif_type="routine_assignment",
                message=f"You have been assigned a new routine: {routine.name}",
                related_object=routine
            )
            
            return Response({
                "message": _("Routine assigned successfully."),
                "routine_id": routine.id,
                "client_id": client.id,
                "assignment_date": routine.updated_at
            }, status=status.HTTP_200_OK)
            
        except CustomUser.DoesNotExist:
            return Response(
                {"error": "Client not found"}, 
                status=status.HTTP_404_NOT_FOUND
            )
        except PASSTHROUGH_EXCEPTIONS:
            # Control-flow exceptions carry their own correct status (404/403/401/400).
            # The broad handler below turned every one of them into a 500, which is the
            # wrong contract for the client and hides real faults in the error budget.
            raise
        except Exception as e:
            logger.error(f"Error assigning routine {routine.id} to client {client_id}: {str(e)}")
            return Response(
                {"error": "An error occurred while assigning the routine"}, 
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )

    @action(detail=True, methods=['post'], permission_classes=[IsTrainerOrAdminForAssignment])
    def unassign_from_client(self, request, pk=None):
        """
        Enhanced routine unassignment from a client.
        
        Requirements:
        - Only trainers and admins can unassign routines
        - Validation ensures proper authorization
        
        TODO: Add unassignment notifications
        TODO: Implement unassignment history tracking
        """
        routine = self.get_object()
        client_id = request.data.get('client_id')
        
        if not client_id:
            return Response(
                {"error": "client_id is required"}, 
                status=status.HTTP_400_BAD_REQUEST
            )
        
        try:
            from users.models import CustomUser, TrainerClientRelation
            client = CustomUser.objects.get(id=client_id, user_type='client')
            
            # Enhanced validation for trainer-client relationship
            if request.user.is_trainer:
                # Check if trainer-client relation exists and is approved
                relation = TrainerClientRelation.objects.filter(
                    trainer=request.user, 
                    client=client, 
                    status='approved'
                ).first()
                
                if not relation:
                    return Response(
                        {
                            "error": "You can only unassign routines from your approved clients.",
                            "details": f"Client {client.username} is not in your approved client list."
                        }, 
                        status=status.HTTP_400_BAD_REQUEST
                    )
            
            # Check if routine is actually assigned to this client
            if client not in routine.assigned_to.all():
                return Response(
                    {
                        "error": "Routine is not assigned to this client",
                        "routine_id": routine.id,
                        "client_id": client.id
                    }, 
                    status=status.HTTP_400_BAD_REQUEST
                )
            
            # Unassign routine from client
            routine.assigned_to.remove(client)
            
            # Log the unassignment
            logger.info(
                f"Routine {routine.id} unassigned from client {client.id} by trainer {request.user.id}"
            )
            
            # TODO: Trigger push notification to client about unassignment
            # self._send_unassignment_notification(client, routine)
            
            return Response({
                "message": f"Routine '{routine.name}' successfully unassigned from {client.username}",
                "routine_id": routine.id,
                "client_id": client.id,
                "unassignment_date": routine.updated_at
            }, status=status.HTTP_200_OK)
            
        except CustomUser.DoesNotExist:
            return Response(
                {"error": "Client not found"}, 
                status=status.HTTP_404_NOT_FOUND
            )
        except PASSTHROUGH_EXCEPTIONS:
            # Control-flow exceptions carry their own correct status (404/403/401/400).
            # The broad handler below turned every one of them into a 500, which is the
            # wrong contract for the client and hides real faults in the error budget.
            raise
        except Exception as e:
            logger.error(f"Error unassigning routine {routine.id} from client {client_id}: {str(e)}")
            return Response(
                {"error": "An error occurred while unassigning the routine"}, 
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )

    @action(detail=True, methods=['post'], permission_classes=[permissions.IsAuthenticated])
    def update_progress(self, request, pk=None):
        """Custom action to update progress for a user on a routine."""
        routine = self.get_object()
        user = request.user
        day = request.data.get("day")
        status = request.data.get("status")

        if user not in routine.assigned_to.all():
            return Response({"error": _("You are not authorized to update this routine.")}, status=403)

        if not day or not status:
            return Response({"error": _("Day and status are required.")}, status=400)

        # Validate if the day exists in the routine's exercises
        valid_days = [exercise.day for exercise in routine.routine_exercises.all()]
        if int(day) not in valid_days:
            return Response({"error": _("Day %(day)s is not part of the routine.") % {"day": day}}, status=400)

        try:
            updated_progress = routine.update_progress(user=user, day=int(day), status=status)
        except ValueError as e:
            return Response({"error": _("Invalid progress update.")}, status=400)

        return Response(
            {"message": _("Progress updated successfully."), "updated_progress": updated_progress},
            status=200
        )

    @action(detail=False, methods=['get'])
    def my_clients_progress(self, request):
        """Get progress for all clients assigned to this trainer"""
        if not request.user.is_trainer:
            return Response(
                {"error": "This endpoint is only for trainers"}, 
                status=status.HTTP_403_FORBIDDEN
            )
        
        # Get all clients assigned to this trainer
        clients = request.user.clients.all()
        
        # Get routines created by this trainer
        trainer_routines = self.queryset.filter(created_by=request.user)
        
        # TODO: Implement comprehensive progress tracking
        # TODO: Add progress analytics and reporting
        # TODO: Implement progress notifications
        
        return Response({
            "trainer_id": request.user.id,
            "client_count": clients.count(),
            "routine_count": trainer_routines.count(),
            "message": _("Progress tracking endpoint - implementation pending")
        }, status=status.HTTP_200_OK)

    def _send_assignment_notification(self, client, routine):
        """
        Send notification to client about routine assignment.
        
        TODO: Implement push notification system
        TODO: Add email notifications
        TODO: Add in-app notification storage
        """
        # Placeholder for notification logic
        logger.info(f"Notification sent to client {client.id} about routine {routine.id} assignment")
        pass

    def _send_unassignment_notification(self, client, routine):
        """
        Send notification to client about routine unassignment.
        
        TODO: Implement push notification system
        TODO: Add email notifications
        TODO: Add in-app notification storage
        """
        # Placeholder for notification logic
        logger.info(f"Notification sent to client {client.id} about routine {routine.id} unassignment")
        pass


class RoutineExerciseViewSet(viewsets.ModelViewSet):
    """
    API endpoint for managing RoutineExercise objects.
    Only trainers (owners) and admins can create/update/delete. All users can read.
    """
    queryset = RoutineExercise.objects.select_related(
        'routine', 'routine__created_by', 'exercise'
    ).prefetch_related('exercise__media')
    serializer_class = RoutineExerciseSerializer
    permission_classes = [IsAdminOrOwnerOrReadOnly]
    pagination_class = StandardResultsSetPagination

    def get_queryset(self):
        """
        Restrict to routines the caller is assigned to or created (admins see all).
        Previously unscoped: IsAdminOrOwnerOrReadOnly returns True for all reads, so
        any authenticated user could list every routine's exercise composition.
        """
        user = self.request.user
        if getattr(user, 'is_admin', False):
            return self.queryset
        return self.queryset.filter(
            Q(routine__assigned_to=user) | Q(routine__created_by=user)
        ).distinct()


class RoutineProgressViewSet(viewsets.ModelViewSet):
    """
    ViewSet for managing routine progress.
    Clients can only see their own progress.
    Trainers can see their clients' progress.
    """
    queryset = RoutineProgress.objects.all()
    serializer_class = RoutineProgressSerializer
    permission_classes = [IsTrainerOrAdmin | IsClientOrAssignedTrainer]
    pagination_class = StandardResultsSetPagination
    filter_backends = [DjangoFilterBackend]
    filterset_fields = ['routine', 'user', 'day', 'status']

    def perform_create(self, serializer):
        """Stamp the owner and refuse progress against someone else's routine.

        `user` is NOT NULL and was never writable nor injected, so every POST here
        died with an IntegrityError (500). Taking it from the request (rather than the
        payload) also prevents logging progress on another user's behalf.
        """
        routine = serializer.validated_data.get('routine')
        user = self.request.user
        if routine is not None and not (
            getattr(user, 'is_admin', False)
            or routine.created_by_id == user.id
            or routine.assigned_to.filter(pk=user.pk).exists()
        ):
            raise PermissionDenied('This routine is not assigned to you.')
        # (user, routine, day, date) is UNIQUE. Re-logging the same day is a normal
        # client action (correcting a set, finishing later), so upsert instead of
        # letting the constraint surface as a 500.
        data = dict(serializer.validated_data)
        routine_obj = data.pop('routine', None)
        day = data.pop('day', None)
        date = data.pop('date', None) or timezone.localdate()
        obj, _created = RoutineProgress.objects.update_or_create(
            user=user, routine=routine_obj, day=day, date=date, defaults=data,
        )
        serializer.instance = obj

    def get_queryset(self):
        """Filter progress based on user role with optimized queries."""
        user = self.request.user
        
        # Optimize: add select_related for frequently accessed relations
        base_qs = self.queryset.select_related(
            'user', 'routine', 'routine__created_by'
        ).prefetch_related(
            'routine__routine_exercises__exercise'
        )
        
        if user.is_admin:
            return base_qs
        elif user.is_trainer:
            return base_qs.filter(user__assigned_trainer=user)
        else:
            return base_qs.filter(user=user)


class ExerciseSetLogViewSet(viewsets.ModelViewSet):
    """
    ViewSet for logging individual exercise sets.
    """
    queryset = ExerciseSetLog.objects.all()
    serializer_class = ExerciseSetLogSerializer
    permission_classes = [IsSetLogCreatorOrTrainerOrAdmin]
    pagination_class = StandardResultsSetPagination
    filter_backends = [DjangoFilterBackend]
    filterset_fields = ['workout_session', 'date']

    def get_queryset(self):
        """
        Returns filtered set logs based on user role with optimized queries.
        """
        user = self.request.user
        
        # Optimize: add select_related for frequently accessed relations
        base_qs = self.queryset.select_related(
            'user_exercise_progress',
            'user_exercise_progress__user',
            'user_exercise_progress__exercise',
            'workout_session',
            'workout_session__routine'
        )
        
        # Optional filter by routine_id
        routine_id = self.request.query_params.get('routine_id')
        if routine_id:
            base_qs = base_qs.filter(workout_session__routine_id=routine_id)
        
        if user.is_admin:
            return base_qs
        elif user.is_trainer:
            return base_qs.filter(user_exercise_progress__user__assigned_trainer=user)
        else:
            return base_qs.filter(user_exercise_progress__user=user)

    def perform_create(self, serializer):
        try:
            serializer.save()
        except IntegrityError as e:
            if 'unique constraint' in str(e).lower():
                raise serializers.ValidationError({'detail': _('A set log for this progress, set number, and date already exists.')})
            raise

    @action(detail=False, methods=['get'], url_path='my-progress')
    def my_progress(self, request):
        """
        Returns aggregated progress for the authenticated client.
        Optional query params:
        - group_by=exercise: group/aggregate by exercise
        - routine_id: filter by specific routine
        Output: List of dicts with exercise/routine stats for charts/analytics
        """
        user = request.user
        if not user.is_authenticated or not user.is_client:
            return Response({'error': _('Only clients can view their own progress.')}, status=403)
        group_by = request.query_params.get('group_by')
        routine_id = request.query_params.get('routine_id')
        qs = self.get_queryset().filter(user_exercise_progress__user=user)
        if routine_id:
            qs = qs.filter(workout_session__routine_id=routine_id)
        if group_by == 'exercise':
            # Aggregate per exercise
            data = (
                qs.values('user_exercise_progress__exercise__name')
                .annotate(
                    total_volume=Sum(F('weight') * F('reps')),
                    sets_completed=Count('id'),
                    average_weight=Avg('weight'),
                    average_reps=Avg('reps'),
                )
                .order_by('user_exercise_progress__exercise__name')
            )
            # Rename keys for frontend clarity
            result = [
                {
                    'exercise': d['user_exercise_progress__exercise__name'],
                    'total_volume': d['total_volume'] or 0,
                    'sets_completed': d['sets_completed'],
                    'average_weight': round(d['average_weight'] or 0, 2),
                    'average_reps': round(d['average_reps'] or 0, 2),
                }
                for d in data
            ]
            return Response(result)
        else:
            # Default: return all set logs for the client (optionally filtered by routine)
            return Response(self.get_serializer(qs, many=True).data)

    @action(detail=False, methods=['post'], url_path='bulk-create')
    def bulk_create(self, request):
        """
        Bulk-create set logs for a user/routine/day.
        Input: routine_id, day, date, sets, weight, reps
        """
        user = request.user
        routine_id = request.data.get('routine_id')
        day = request.data.get('day')
        date = request.data.get('date')
        if not routine_id or not day or not date:
            return Response({'error': _('routine_id, day, and date are required.')}, status=400)

        # --- Real-world input validation -------------------------------------
        # Previously unvalidated: negative weight (-500 kg), 100000 reps, dates in
        # 2030 or 1900, and an uncapped `sets` (500 sets => 502 rows, ~11s request).
        try:
            sets = int_param(request.data, 'sets', default=1, minimum=1, maximum=100)
            weight = float(request.data.get('weight', 0))
            reps = int_param(request.data, 'reps', default=10, minimum=1, maximum=1000)
        except (TypeError, ValueError):
            return Response({'error': _('sets, weight and reps must be numeric.')},
                            status=status.HTTP_400_BAD_REQUEST)

        if not (1 <= sets <= MAX_SETS_PER_REQUEST):
            return Response(
                {'error': _('sets must be between 1 and %(max)d.') % {'max': MAX_SETS_PER_REQUEST}},
                status=status.HTTP_400_BAD_REQUEST)
        if not (0 <= weight <= MAX_WEIGHT_KG):
            return Response(
                {'error': _('weight must be between 0 and %(max)d kg.') % {'max': MAX_WEIGHT_KG}},
                status=status.HTTP_400_BAD_REQUEST)
        if not (1 <= reps <= MAX_REPS_PER_SET):
            return Response(
                {'error': _('reps must be between 1 and %(max)d.') % {'max': MAX_REPS_PER_SET}},
                status=status.HTTP_400_BAD_REQUEST)

        parsed_date = parse_date(str(date))
        if parsed_date is None:
            return Response({'error': _('date must be in YYYY-MM-DD format.')},
                            status=status.HTTP_400_BAD_REQUEST)
        today = timezone.localdate()
        if parsed_date > today:
            return Response({'error': _('Cannot log a workout in the future.')},
                            status=status.HTTP_400_BAD_REQUEST)
        if (today - parsed_date).days > MAX_BACKLOG_DAYS:
            return Response(
                {'error': _('Cannot log a workout more than %(d)d days in the past.') % {'d': MAX_BACKLOG_DAYS}},
                status=status.HTTP_400_BAD_REQUEST)
        date = parsed_date
        from .models import Routine, RoutineExercise, UserExerciseProgress, ExerciseSetLog
        try:
            routine = Routine.objects.get(id=routine_id)
        except Routine.DoesNotExist:
            return Response({'error': _('Routine not found.')}, status=404)

        # AUTHORIZATION: the caller must be entitled to this routine. Without this,
        # any authenticated user could pass an arbitrary routine_id and both create
        # progress rows against a routine never assigned to them AND read back every
        # exercise name of another trainer's private routine from the response.
        if not (routine.assigned_to.filter(id=user.id).exists()
                or routine.created_by_id == user.id
                or user.is_admin):
            return Response({'error': _('You do not have access to this routine.')},
                            status=status.HTTP_403_FORBIDDEN)

        exercises = RoutineExercise.objects.filter(routine=routine, day=day)
        if not exercises.exists():
            # Previously a nonexistent day returned 200 {"count": 0} — a silent
            # no-op. update_progress() validates the day, so this is now consistent.
            return Response(
                {'error': _('Day %(day)s is not part of this routine.') % {'day': day}},
                status=status.HTTP_400_BAD_REQUEST)
        results = []
        errors = []
        # ATOMIC: previously each progress row and set log was written outside any
        # transaction, so a mid-loop failure committed partial data while still
        # returning 200 with a partial `errors` array.
        from .models import suspend_progress_recalc, recalc_progress_for
        touched_progress = []
        # PERFORMANCE: the post_save receiver on ExerciseSetLog recomputes the whole
        # RoutineProgress tree (~16 queries) for EVERY set log. Logging 10 sets across
        # 3 exercises cost 493 queries. Suppress it during the bulk write and run it
        # once per affected progress row afterwards.
        with transaction.atomic(), suspend_progress_recalc():
            for rex in exercises.select_related('exercise'):
                # target_sets is the TRAINER'S PRESCRIPTION (RoutineExercise.sets), never
                # the number the client just logged. Taking it from the request made the
                # target whatever the client last claimed, so `completed >= target` was
                # true by construction: one set of one rep at 0 kg marked a prescribed
                # 3x10 exercise complete, and that fed RoutineProgress, analytics,
                # streaks and achievements. `routine/serializers.py` already reported the
                # prescription here, so the two disagreed about the same number.
                prescribed = rex.sets or 0
                progress, _created = UserExerciseProgress.objects.get_or_create(
                    user=user,
                    exercise=rex.exercise,
                    date=date,
                    defaults={'completed_sets': sets, 'target_sets': prescribed}
                )
                # completed_sets follows what was logged; target_sets follows the routine.
                updates = []
                if progress.completed_sets != sets:
                    progress.completed_sets = sets
                    updates.append('completed_sets')
                if progress.target_sets != prescribed:
                    progress.target_sets = prescribed
                    updates.append('target_sets')
                if not _created and updates:
                    progress.save(update_fields=updates)

                touched_progress.append(progress)

                for set_num in range(1, sets + 1):
                    try:
                        # Savepoint: this loop continues after a failed write. Without
                        # one, the first IntegrityError aborts the whole transaction
                        # under any enclosing atomic() and every later set in this
                        # request fails with TransactionManagementError instead of
                        # being recorded in `errors`.
                        with transaction.atomic():
                            # update_or_create, not get_or_create: re-submitting a set with a
                            # corrected weight/reps previously returned success while silently
                            # discarding the correction, so users could never fix a mistake.
                            log, created = ExerciseSetLog.objects.update_or_create(
                                user_exercise_progress=progress,
                                set_number=set_num,
                                date=date,
                                defaults={'weight': weight, 'reps': reps}
                            )
                        results.append({'exercise': rex.exercise.name, 'set_number': set_num,
                                        'created': created, 'id': log.id})
                    except IntegrityError as e:
                        if 'unique constraint' in str(e).lower():
                            errors.append({'exercise': rex.exercise.name, 'set_number': set_num,
                                           'error': 'Duplicate set log for this progress, set number, and date.'})
                        else:
                            logger.exception("bulk set-log failed for %s set %s", rex.exercise_id, set_num)
                            errors.append({'exercise': rex.exercise.name, 'set_number': set_num,
                                           'error': _('Could not record this set.')})

        # Run the suppressed recomputation ONCE per affected progress row — outside
        # the suspension block, otherwise it would be muted along with the rest.
        for prog in touched_progress:
            recalc_progress_for(prog)

        return Response({'results': results, 'errors': errors, 'count': len(results)})


class WorkoutSessionViewSet(viewsets.ModelViewSet):
    """
    API endpoint for managing WorkoutSession objects.
    Only owners (clients) can PATCH their session; only trainers/admins can create.
    
    Input: API requests with session data (POST), session updates (PATCH)
    Output: JSON responses with session data
    """
    queryset = WorkoutSession.objects.select_related(
        'user', 'routine', 'routine__created_by'
    ).prefetch_related(
        'set_logs',
        'set_logs__user_exercise_progress__exercise'
    )
    serializer_class = WorkoutSessionSerializer
    filter_backends = [DjangoFilterBackend]
    filterset_fields = ['user', 'routine', 'status']
    
    def get_permissions(self):
        from .permissions import IsSessionOwnerOrTrainerOrAdmin, IsRoutineOwnerOrAssigned
        if self.action == 'create' or self.request.method == 'POST':
            return [IsRoutineOwnerOrAssigned()]
        return [IsSessionOwnerOrTrainerOrAdmin()]

    def get_queryset(self):
        """
        Scope sessions to the caller. Previously this viewset exposed
        WorkoutSession.objects.all() with no filtering, and `filterset_fields`
        includes `user`, so any authenticated account could list or enumerate
        every user's workout history via ?user=<id>.
        """
        from .permissions import accessible_user_ids
        allowed_ids = accessible_user_ids(self.request.user)
        if allowed_ids is None:  # admin
            return self.queryset
        return self.queryset.filter(user_id__in=allowed_ids)

    def perform_create(self, serializer):
        """
        Handles creation of a workout session, assigning to the correct user.
        Input: serializer (validated data), request.user
        Output: Saves WorkoutSession instance
        """
        logger = logging.getLogger(__name__)
        logger.debug(f"Creating WorkoutSession with data: {serializer.validated_data} by user {self.request.user}")
        if self.request.user.is_trainer or self.request.user.is_staff:
            user_id = self.request.data.get('user')
            if user_id:
                from users.models import CustomUser
                from .permissions import can_access_user_data
                from rest_framework.exceptions import PermissionDenied
                # A trainer may only create sessions for their OWN approved clients.
                if not can_access_user_data(self.request.user, user_id):
                    raise PermissionDenied(_('You may only create sessions for your approved clients.'))
                user_obj = CustomUser.objects.get(pk=user_id)
                serializer.save(user=user_obj)
                logger.debug(f"WorkoutSession assigned to user {user_obj}")
                return
        serializer.save(user=self.request.user)
        logger.debug(f"WorkoutSession assigned to user {self.request.user}")

    def partial_update(self, request, *args, **kwargs):
        """
        PATCH: Used by client to mark session as completed, etc.
        Triggers notification on session completion.
        """
        response = super().partial_update(request, *args, **kwargs)
        instance = self.get_object()
        # If session is marked as completed, notify trainer and client
        if 'status' in request.data and request.data['status'] == 'completed':
            # Stamp end_time so `duration` is computable. Nothing set this before,
            # so every completed session reported duration = None forever.
            if instance.end_time is None:
                instance.end_time = timezone.now()
                instance.save(update_fields=['end_time'])
                response.data['end_time'] = instance.end_time
            # Notify client (self)
            send_notification(
                user=instance.user,
                notif_type="session_completed",
                message=f"Congratulations! You completed your workout session for routine: {instance.routine.name}",
                related_object=instance
            )
            # Notify trainer if exists
            trainer = getattr(instance.user, 'assigned_trainer', None)
            if trainer:
                send_notification(
                    user=trainer,
                    notif_type="session_completed",
                    message=f"Your client {instance.user.username} completed a workout session for routine: {instance.routine.name}",
                    related_object=instance
                )

            # Milestones. Deferred to on_commit so a rolled-back completion cannot
            # congratulate anyone, and pushed to the worker because computing a streak
            # walks backwards day by day through the activity log.
            from django.db import transaction

            _user_id = instance.user_id
            transaction.on_commit(lambda: _queue_milestones(_user_id))
        return response


def _queue_milestones(user_id):
    """Hand milestone evaluation to the worker, never to the request.

    Failures here are logged and dropped: a broken milestone check must not turn a
    successfully completed workout into a 500 for the user who just finished it.
    """
    try:
        from notifications.tasks import award_progress_milestones

        award_progress_milestones.delay(user_id)
    except Exception:
        logger.warning("could not queue milestone check for user %s", user_id, exc_info=True)


class AnalyticsViewSet(viewsets.ViewSet):
    """
    Analytics endpoints for user training data.
    Provides summary stats: total volume, PRs, workout frequency.
    Input: GET params period=week|month, user_id (optional, trainer/admin only)
    Output: JSON summary of analytics
    """
    permission_classes = [permissions.IsAuthenticated]

    @action(detail=False, methods=['get'])
    def summary(self, request):
        """
        Returns analytics summary for the user (or specified user_id if trainer/admin).
        Input: period=week|month, user_id (optional)
        Output: JSON with total_volume, days_trained, prs per exercise
        Get analytics summary for a specific period.
        
        Input: Query param 'period' (week, month, year)
        Output: JSON with total volume, days trained, PRs, comparisons, top muscles.
        """
        from datetime import timedelta
        period = request.query_params.get('period', 'week')
        user_id = request.query_params.get('user_id')
        user = request.user
        
        if user_id:
            # AUTHORIZATION: a caller-supplied user_id must be self, an approved
            # client (trainer), or admin. Without this any authenticated account
            # could read any other user's training data.
            from .permissions import can_access_user_data
            if not can_access_user_data(request.user, user_id):
                return Response({'error': _('You do not have access to this user\'s data.')},
                                status=status.HTTP_403_FORBIDDEN)
            try:
                from users.models import CustomUser
                user = CustomUser.objects.get(pk=user_id)
            except CustomUser.DoesNotExist:
                return Response({'error': _('User not found')}, status=404)

        now = timezone.now()
        
        # Determine time ranges
        if period == 'month':
            start_date = now - timedelta(days=30)
            prev_start = start_date - timedelta(days=30)
            days_in_period = 30
        elif period == 'year':
            start_date = now - timedelta(days=365)
            prev_start = start_date - timedelta(days=365)
            days_in_period = 365
        else: # week
            start_date = now - timedelta(days=7)
            prev_start = start_date - timedelta(days=7)
            days_in_period = 7

        # Current Period Data
        setlogs = ExerciseSetLog.objects.filter(
            user_exercise_progress__user=user,
            date__gte=start_date
        )
        
        total_volume = setlogs.aggregate(
            total=Sum(F('weight') * F('reps'))
        )['total'] or 0
        
        days_trained = setlogs.values('date').distinct().count()
        
        # PRs (Personal Records) in this period - simplified check
        # Ideally would check if weight > max(previous weights) for each exercise
        # For now, we'll return a placeholder or check simplified logic if available
        # Implementation of true PR tracking requires checking history per exercise
        prs = 0 

        # Comparison Data (Previous Period)
        prev_setlogs = ExerciseSetLog.objects.filter(
            user_exercise_progress__user=user,
            date__gte=prev_start,
            date__lt=start_date
        )
        
        prev_volume = prev_setlogs.aggregate(
            total=Sum(F('weight') * F('reps'))
        )['total'] or 0
        
        volume_change = 0
        if prev_volume > 0:
            volume_change = round(((total_volume - prev_volume) / prev_volume) * 100, 1)
        elif total_volume > 0:
            volume_change = 100.0 # From 0 to something is 100% (or infinite) increase

        # Top Muscles Worked
        muscle_data = setlogs.values(
            'user_exercise_progress__exercise__target_muscle'
        ).annotate(
            count=Count('id'),
            volume=Sum(F('weight') * F('reps'))
        ).order_by('-volume')[:5]
        
        top_muscles = [
            {
                'muscle': m['user_exercise_progress__exercise__target_muscle'],
                'sets': m['count'],
                'volume': m['volume'] or 0
            } for m in muscle_data if m['user_exercise_progress__exercise__target_muscle']
        ]

        # Consistency Score (Days trained / Total days)
        consistency_score = round((days_trained / days_in_period) * 100, 1)

        return Response({
            f'{period}_volume': total_volume,
            'volume_change_percent': volume_change,
            'days_trained': days_trained,
            'prs': prs,
            'top_muscles': top_muscles,
            'avg_sets_per_session': round(setlogs.count() / max(days_trained, 1), 1),
            'consistency_score': consistency_score
        })

    @action(detail=False, methods=['get'])
    def completion(self, request):
        """
        Completion rates per routine and per client.
        Input: routine_id (optional), user_id (optional)
        Output: JSON with completion % per routine/client
        """
        from .models import RoutineProgress, Routine
        routine_id = request.query_params.get('routine_id')
        user_id = request.query_params.get('user_id')
        qs = RoutineProgress.objects.all()
        # DEFAULT SCOPE: without this, calling the endpoint with no parameters
        # returned every user's routine progress to any authenticated caller.
        from .permissions import accessible_user_ids
        allowed_ids = accessible_user_ids(request.user)
        if allowed_ids is not None:  # None == admin, unrestricted
            qs = qs.filter(user_id__in=allowed_ids)
        if routine_id:
            qs = qs.filter(routine_id=routine_id)
        if user_id:
            # AUTHORIZATION: a caller-supplied user_id must be self, an approved
            # client (trainer), or admin. Without this any authenticated account
            # could read any other user's training data.
            from .permissions import can_access_user_data
            if not can_access_user_data(request.user, user_id):
                return Response({'error': _('You do not have access to this user\'s data.')},
                                status=status.HTTP_403_FORBIDDEN)
            qs = qs.filter(user_id=user_id)
        # Aggregate by routine and user
        data = {}
        for rp in qs:
            key = f"routine_{rp.routine_id}_user_{rp.user_id}"
            if key not in data:
                data[key] = {'routine_id': rp.routine_id, 'user_id': rp.user_id, 'days': 0, 'completed': 0}
            data[key]['days'] += 1
            if rp.status == 'completed':
                data[key]['completed'] += 1
        # Calculate completion %
        for v in data.values():
            v['completion_rate'] = round(100 * v['completed'] / v['days'], 2) if v['days'] else 0
        # Match the platform-wide list shape {count, next, previous, results} and
        # cap the payload — this was a hand-rolled dict with no bound, so a trainer
        # with many clients could pull an unbounded response.
        rows = list(data.values())
        return Response({
            'count': len(rows),
            'next': None,
            'previous': None,
            'results': rows[:500],
        })

    @action(detail=False, methods=['get'])
    def streaks(self, request):
        """
        Get current and max streaks (consecutive days with completed sets) for a user.
        Input: user_id (optional)
        Output: JSON with current_streak, max_streak
        """
        from .models import RoutineProgress
        from users.models import CustomUser
        user_id = request.query_params.get('user_id')
        user = request.user
        if user_id:
            # AUTHORIZATION: a caller-supplied user_id must be self, an approved
            # client (trainer), or admin. Without this any authenticated account
            # could read any other user's training data.
            from .permissions import can_access_user_data
            if not can_access_user_data(request.user, user_id):
                return Response({'error': _('You do not have access to this user\'s data.')},
                                status=status.HTTP_403_FORBIDDEN)
            try:
                user = CustomUser.objects.get(pk=user_id)
            except CustomUser.DoesNotExist:
                return Response({'error': _('User not found.')}, status=404)

        # Streaks are computed over DISTINCT calendar training days, using the
        # real `date` field. It previously used `updated_at` (auto_now), so editing
        # an old record moved that workout to today.
        training_days = sorted(set(
            RoutineProgress.objects.filter(user=user, status='completed')
            .values_list('date', flat=True)
        ))

        longest_streak = 0
        run = 0
        prev = None
        for d in training_days:
            if d is None:
                continue
            run = run + 1 if (prev is not None and (d - prev).days == 1) else 1
            longest_streak = max(longest_streak, run)
            prev = d

        # "Current" must mean current: if the last training day is neither today
        # nor yesterday the streak is broken and the answer is 0. Previously this
        # returned the trailing historical run, so a user who stopped a month ago
        # was still told they had an active streak.
        today = timezone.localdate()
        current_streak = 0
        if training_days and training_days[-1] is not None:
            if (today - training_days[-1]).days <= 1:
                current_streak = run

        return Response({
            'user_id': user.id,
            'current_streak': current_streak,
            'max_streak': longest_streak,
            'last_training_date': training_days[-1] if training_days else None,
        })

    @action(detail=False, methods=['get'])
    def trends(self, request):
        """
        Get training volume and completion trends over time (per day/week/month).
        Input: user_id (optional), period=day|week|month
        Output: JSON with date/period and volume/completion
        """
        from .models import ExerciseSetLog, RoutineProgress
        from django.db.models.functions import TruncDay, TruncWeek, TruncMonth
        user_id = request.query_params.get('user_id') or request.user.id
        # AUTHORIZATION: same rule as the other analytics actions — a supplied
        # user_id must be self, an approved client, or admin.
        from .permissions import can_access_user_data
        if not can_access_user_data(request.user, user_id):
            return Response({'error': _('You do not have access to this user\'s data.')},
                            status=status.HTTP_403_FORBIDDEN)
        period = request.query_params.get('period', 'day')
        # Training volume trend
        setlogs = ExerciseSetLog.objects.filter(user_exercise_progress__user_id=user_id)
        if period == 'week':
            setlogs = setlogs.annotate(period=TruncWeek('date'))
        elif period == 'month':
            setlogs = setlogs.annotate(period=TruncMonth('date'))
        else:
            setlogs = setlogs.annotate(period=TruncDay('date'))
        volume_trend = setlogs.values('period').annotate(
            total_volume=Sum(F('weight') * F('reps'))
        ).order_by('period')
        # Completion trend
        rps = RoutineProgress.objects.filter(user_id=user_id)
        if period == 'week':
            rps = rps.annotate(period=TruncWeek('updated_at'))
        elif period == 'month':
            rps = rps.annotate(period=TruncMonth('updated_at'))
        else:
            rps = rps.annotate(period=TruncDay('updated_at'))
        completion_trend = rps.values('period').annotate(
            completed=Count('id', filter=Q(status='completed')),
            total=Count('id')
        ).order_by('period')
        return Response({'volume_trend': list(volume_trend), 'completion_trend': list(completion_trend)})

    @action(detail=False, methods=['get'])
    def admin_dashboard(self, request):
        """
        Admin/trainer dashboard: summary stats for all clients.
        Output: JSON with client stats (volume, completion, streaks)
        """
        from .models import RoutineProgress, ExerciseSetLog
        from users.models import CustomUser
        from django.db.models import Q, Count, Sum, F, Max
        from django.utils import timezone
        from datetime import timedelta
        
        if not (request.user.is_trainer or request.user.is_admin):
            return Response({'error': _('Permission denied.')}, status=403)
        
        # Get all clients for this trainer/admin with optimized queries
        if request.user.is_trainer:
            clients = CustomUser.objects.filter(assigned_trainer=request.user)
        else:
            clients = CustomUser.objects.filter(user_type='client')
        
        # Prefetch related data to avoid N+1 queries
        clients = clients.select_related('assigned_trainer').prefetch_related(
            'routine_progress',
            'exercise_progress__set_logs'
        )
        
        # Calculate date range for volume (last 30 days)
        thirty_days_ago = timezone.localdate() - timedelta(days=30)
        
        # Bulk aggregate volume data for all clients
        volume_data = ExerciseSetLog.objects.filter(
            user_exercise_progress__user__in=clients,
            date__gte=thirty_days_ago
        ).values('user_exercise_progress__user').annotate(
            total_volume=Sum(F('weight') * F('reps'))
        )
        
        # Create volume lookup dictionary
        volume_lookup = {item['user_exercise_progress__user']: item['total_volume'] for item in volume_data}
        
        # Bulk aggregate completion data for all clients
        completion_data = RoutineProgress.objects.filter(
            user__in=clients
        ).values('user').annotate(
            total_days=Count('id'),
            completed_days=Count('id', filter=Q(status='completed'))
        )
        
        # Create completion lookup dictionary
        completion_lookup = {}
        for item in completion_data:
            user_id = item['user']
            total_days = item['total_days']
            completed_days = item['completed_days']
            completion_lookup[user_id] = {
                'total_days': total_days,
                'completed_days': completed_days,
                'completion_rate': round(100 * completed_days / total_days, 2) if total_days else 0
            }
        
        # Bulk aggregate streak data for all clients
        streak_data = RoutineProgress.objects.filter(
            user__in=clients,
            status='completed'
        ).values('user', 'day').order_by('user', 'day')
        
        # Calculate streaks for each client
        streak_lookup = {}
        current_user = None
        current_streak = 0
        max_streak = 0
        last_completed_day = None
        
        for item in streak_data:
            user_id = item['user']
            day = item['day']
            
            if current_user != user_id:
                # Save previous user's streak data
                if current_user is not None:
                    streak_lookup[current_user] = max_streak
                
                # Reset for new user
                current_user = user_id
                current_streak = 1
                max_streak = 1
                last_completed_day = day
            else:
                # Same user, check if consecutive
                if last_completed_day is not None and day == last_completed_day + 1:
                    current_streak += 1
                    max_streak = max(max_streak, current_streak)
                else:
                    current_streak = 1
                last_completed_day = day
        
        # Save last user's streak data
        if current_user is not None:
            streak_lookup[current_user] = max_streak
        
        # Build dashboard response
        dashboard = []
        for client in clients:
            client_id = client.id
            
            # Get volume data
            total_volume = volume_lookup.get(client_id, 0) or 0
            
            # Get completion data
            completion_info = completion_lookup.get(client_id, {
                'total_days': 0,
                'completed_days': 0,
                'completion_rate': 0
            })
            
            # Get streak data
            max_streak = streak_lookup.get(client_id, 0)
            
            dashboard.append({
                'client_id': client_id,
                'username': client.username,
                'total_volume': total_volume,
                'completion_rate': completion_info['completion_rate'],
                'max_streak': max_streak
            })
        
        return Response({'dashboard': dashboard})


class IsTrainerOrReadOnly(IsAuthenticated):
    """Allow only trainers to create/edit/delete, others read-only.

    Object level: a trainer may only modify/delete templates they CREATED. The
    list queryset intentionally includes other trainers' public templates so they
    can be viewed and copied — without this object check that read access also
    granted write access, letting any trainer rename or delete another trainer's
    public template.
    """
    def has_permission(self, request, view):
        return super().has_permission(request, view) and (request.user.is_trainer or request.method in ('GET', 'HEAD', 'OPTIONS'))

    def has_object_permission(self, request, view, obj):
        if request.method in ('GET', 'HEAD', 'OPTIONS'):
            return True
        # `copy` is a POST but is read-only with respect to the source template —
        # it creates a NEW template owned by the caller. Copying a public template
        # is the intended workflow, so it must not require ownership.
        if getattr(view, 'action', None) == 'copy':
            return True
        if getattr(request.user, 'is_admin', False):
            return True
        return getattr(obj, 'created_by_id', None) == request.user.id


class RoutineTemplateViewSet(viewsets.ModelViewSet):
    """
    ViewSet for managing RoutineTemplates (CRUD, filter, share, generate routines).
    
    Visibility Rules:
    - Admins can see all templates
    - Trainers can see:
      * Their own templates (public and private)
      * Public templates from other trainers
    - Clients can see:
      * Public templates from all trainers
    - Other trainers cannot see private templates from other trainers
    
    TODO: Add tag filtering, search, usage metrics
    """
    queryset = RoutineTemplate.objects.all()
    serializer_class = RoutineTemplateSerializer
    permission_classes = [IsTrainerOrReadOnly]
    pagination_class = StandardResultsSetPagination

    def get_queryset(self):
        """
        Filter templates based on user role and visibility rules.
        
        Rules:
        - Admins: All templates
        - Trainers: Own templates + public templates from others
        - Clients: Public templates only
        - Other users: Public templates only
        """
        user = self.request.user
        qs = super().get_queryset().select_related('created_by').prefetch_related(
            'routinetemplateexercise_set',
            'routinetemplateexercise_set__exercise',
            'routinetemplateexercise_set__exercise__media'
        )

        # Apply role-based filtering
        if user.is_admin:
            # Admins can see all templates
            pass
        elif user.is_trainer:
            # Trainers can see their own templates + public templates from others
            qs = qs.filter(
                models.Q(created_by=user) |  # Own templates (public and private)
                models.Q(is_public=True)     # Public templates from others
            )
        else:
            # Clients and other users can only see public templates
            qs = qs.filter(is_public=True)
        
        # Apply additional filters from query parameters
        goal = self.request.query_params.get('goal')
        if goal:
            qs = qs.filter(goal__iexact=goal)
        
        is_public = self.request.query_params.get('is_public')
        if is_public is not None:
            qs = qs.filter(is_public=(is_public.lower() == 'true'))
        
        # Filter by creator if specified
        created_by = self.request.query_params.get('created_by')
        if created_by:
            qs = qs.filter(created_by__username__icontains=created_by)
        
        # Add search functionality
        search = self.request.query_params.get('search')
        if search:
            qs = qs.filter(
                models.Q(name__icontains=search) |
                models.Q(description__icontains=search) |
                models.Q(goal__icontains=search)
            )
        
        return qs.distinct()

    def perform_create(self, serializer):
        """Override to assign the template to the creator."""
        serializer.save(created_by=self.request.user)

    @action(detail=True, methods=['post'], permission_classes=[IsTrainerOrReadOnly])
    def generate(self, request, pk=None):
        """
        Generate a Routine for a client from this template.
        Input: client_id, optional customizations (sets, reps, rest_time per exercise)
        Output: Created Routine and RoutineExercises
        
        Security: Only trainers can generate routines for their own clients
        """
        template = self.get_object()
        client_id = request.data.get('client_id')
        
        if not client_id:
            return Response({'detail': _('client_id required')}, status=400)
        
        from users.models import CustomUser
        try:
            client = CustomUser.objects.get(pk=client_id)
        except CustomUser.DoesNotExist:
            return Response({'detail': _('Client not found')}, status=404)
        
        # Security check: Only trainers can assign to their own clients
        if not (request.user.is_trainer and client.assigned_trainer_id == request.user.id):
            return Response({'detail': _('Permission denied. You can only assign routines to your own clients.')}, status=403)
        
        # Determine total duration for the routine
        start_date = request.data.get('start_date')
        end_date = request.data.get('end_date')
        routine_days = request.data.get('days')

        if start_date and end_date and not routine_days:
            from datetime import datetime
            d1 = datetime.strptime(start_date, '%Y-%m-%d').date()
            d2 = datetime.strptime(end_date, '%Y-%m-%d').date()
            routine_days = max(1, (d2 - d1).days + 1)
        elif routine_days:
            routine_days = int(routine_days)
        else:
            routine_days = template.days

        # Create Routine for client
        from .models import Routine, RoutineExercise
        routine_data = {
            'name': template.name,
            'description': f"{template.description} (Goal: {template.goal})",
            'days': routine_days,
            'created_by': request.user
        }
        if start_date:
            routine_data['start_date'] = start_date
        if end_date:
            routine_data['end_date'] = end_date

        routine = Routine.objects.create(**routine_data)
        routine.assigned_to.set([client])
        
        # Copy exercises with expansion loop
        customizations = request.data.get('customizations', {})
        template_exercises = list(template.routinetemplateexercise_set.select_related('exercise').all())
        new_routine_exercises = []
        cycle_length = template.days or 1
        
        for client_day in range(1, routine_days + 1):
            cycle_day = ((client_day - 1) % cycle_length) + 1
            exercises_for_day = [ex for ex in template_exercises if ex.day == cycle_day]
            
            for t_ex in exercises_for_day:
                ex_id = str(t_ex.exercise_id)
                ex_custom = customizations.get(ex_id, {})
                new_routine_exercises.append(
                    RoutineExercise(
                        routine=routine,
                        exercise=t_ex.exercise,
                        sets=ex_custom.get('sets', t_ex.sets),
                        reps=ex_custom.get('reps', t_ex.reps),
                        rest_time=ex_custom.get('rest_time', t_ex.rest_time),
                        day=client_day,
                        order=t_ex.order
                    )
                )
                
        if new_routine_exercises:
            RoutineExercise.objects.bulk_create(new_routine_exercises)
        
        # Send notification to client
        from .services import send_notification
        send_notification(
            user=client,
            notif_type="routine_assignment",
            message=f"A new routine '{routine.name}' has been assigned to you.",
            related_object=routine
        )
        
        from .serializers import RoutineSerializer
        return Response(RoutineSerializer(routine).data, status=201)

    @action(detail=True, methods=['post'], permission_classes=[IsTrainerOrReadOnly])
    def copy(self, request, pk=None):
        """
        Copy a public template to create a private version for the trainer.
        Only works with public templates from other trainers.
        """
        template = self.get_object()
        
        # Only allow copying public templates from other trainers
        if not template.is_public:
            return Response({'detail': _('Can only copy public templates')}, status=400)
        
        if template.created_by == request.user:
            return Response({'detail': _('Cannot copy your own template')}, status=400)
        
        # Create a copy
        new_template = RoutineTemplate.objects.create(
            name=f"Copy of {template.name}",
            description=template.description,
            goal=template.goal,
            days=template.days,  # Copy days from template
            is_public=False,  # Always private when copied
            created_by=request.user
        )
        
        # Copy exercises
        for t_ex in template.routinetemplateexercise_set.all():
            RoutineTemplateExercise.objects.create(
                template=new_template,
                exercise=t_ex.exercise,
                sets=t_ex.sets,
                reps=t_ex.reps,
                rest_time=t_ex.rest_time,
                day=t_ex.day,  # Copy day from template exercise
                order=t_ex.order
            )
        
        return Response(RoutineTemplateSerializer(new_template).data, status=201)

    @action(detail=False, methods=['get'])
    def my_templates(self, request):
        """
        Get templates created by the current trainer.
        Only for trainers.
        """
        if not request.user.is_trainer:
            return Response({'detail': _('Only trainers can access this endpoint')}, status=403)
        
        templates = self.get_queryset().filter(created_by=request.user)
        page = self.paginate_queryset(templates)
        if page is not None:
            return self.get_paginated_response(self.get_serializer(page, many=True).data)
        return Response(self.get_serializer(templates, many=True).data)

    @action(detail=False, methods=['get'])
    def public_templates(self, request):
        """
        Get all public templates from all trainers.
        Available to all authenticated users.
        """
        # Every public template on the platform, from every trainer. This is the one
        # that grows without bound as the platform grows, and it returned the whole
        # table as a bare array with no envelope and no page links.
        templates = self.get_queryset().filter(is_public=True)
        page = self.paginate_queryset(templates)
        if page is not None:
            return self.get_paginated_response(self.get_serializer(page, many=True).data)
        return Response(self.get_serializer(templates, many=True).data)


class RoutineTemplateExerciseSerializer(serializers.ModelSerializer):
    """
    Serializer for routine template exercises. Uses the original model's fields.
    """
    from .serializers import ExerciseSerializer
    exercise_details = ExerciseSerializer(source='exercise', read_only=True)

    class Meta:
        model = RoutineTemplateExercise
        fields = ['id', 'template', 'exercise', 'exercise_details', 'sets', 'reps', 'rest_time', 'day', 'order']


class RoutineTemplateExerciseViewSet(viewsets.ReadOnlyModelViewSet):
    """
    API endpoint for viewing RoutineTemplateExercise objects with pagination.
    ReadOnly since trainers usually manage these via the template or custom actions.
    """
    queryset = RoutineTemplateExercise.objects.select_related('exercise').prefetch_related('exercise__media')
    serializer_class = RoutineTemplateExerciseSerializer
    permission_classes = [permissions.IsAuthenticated]
    pagination_class = StandardResultsSetPagination
    filter_backends = [DjangoFilterBackend]
    filterset_fields = ['template', 'day']

    def get_queryset(self):
        user = self.request.user
        qs = super().get_queryset()
        
        # Security: restrict to own templates or public templates
        if user.is_admin:
            return qs
        if user.is_trainer:
            return qs.filter(
                models.Q(template__created_by=user) | 
                models.Q(template__is_public=True)
            )
        return qs.filter(template__is_public=True)


class UserExerciseProgressViewSet(viewsets.ModelViewSet):
    """
    API endpoint for managing UserExerciseProgress objects (per-user, per-exercise progress).
    Allows trainers, admins, and clients to create, view, and update their own progress records.
    """
    queryset = UserExerciseProgress.objects.all()
    serializer_class = UserExerciseProgressSerializer
    pagination_class = StandardResultsSetPagination
    filter_backends = [DjangoFilterBackend]
    filterset_fields = ['exercise', 'date']

    def get_serializer_class(self):
        """
        Use detailed serializer for read operations to avoid N+1 queries on client.
        """
        if self.action in ['list', 'retrieve']:
            from .serializers import UserExerciseProgressDetailSerializer
            return UserExerciseProgressDetailSerializer
        return UserExerciseProgressSerializer

    def get_permissions(self):
        # An @action(permission_classes=[...]) declaration wins. Overriding
        # get_permissions() discarded it silently, so `bulk-complete` — declared
        # IsAuthenticated and documented as the client's own "mark my day done" call —
        # actually ran under IsAdminOrOwnerOrReadOnly, which requires is_trainer or
        # is_staff for a POST. Every client got 403 from the endpoint written for them.
        handler = getattr(self, self.action, None) if self.action else None
        declared = getattr(handler, 'kwargs', {}).get('permission_classes')
        if declared:
            return [permission() for permission in declared]
        if self.action == 'create':
            return [IsAuthenticated()]
        return [IsAdminOrOwnerOrReadOnly()]

    def get_queryset(self):
        user = self.request.user
        if user.is_admin:
            return self.queryset.all()
        elif user.is_trainer:
            return self.queryset.filter(user__assigned_trainer=user)
        else:
            return self.queryset.filter(user=user)

    def perform_create(self, serializer):
        try:
            serializer.save(user=self.request.user)
        except IntegrityError as e:
            if 'unique constraint' in str(e).lower():
                raise serializers.ValidationError({'detail': _('A progress record for this user, exercise, and date already exists.')})
            raise

    @action(detail=False, methods=['get'], url_path='daily-summary')
    def daily_summary(self, request):
        """
        Get a full summary of exercises performed on a specific date.
        Returns detailed stats including sets, reps, weight, and volume.
        """
        date_str = request.query_params.get('date')
        if not date_str:
            date_str = timezone.localdate().isoformat()
        
        try:
            # Validate date format
            target_date = datetime.strptime(date_str, '%Y-%m-%d').date()
        except ValueError:
            return Response({'error': _('Invalid date format. Use YYYY-MM-DD.')}, status=400)
            
        queryset = self.get_queryset().filter(date=target_date).select_related('exercise').prefetch_related('set_logs')
        
        # Use the specific detailed serializer
        from .serializers import UserDailySummarySerializer
        serializer = UserDailySummarySerializer(queryset, many=True)
        
        return Response({
            'date': date_str,
            'total_exercises': queryset.count(),
            'exercises': serializer.data
        })

    @action(detail=False, methods=['post'], url_path='bulk-complete', permission_classes=[permissions.IsAuthenticated])
    def bulk_complete(self, request):
        """
        Mark all exercises for a given routine and day as done for the current user.
        Input: routine_id, day, date, completed_sets, target_sets
        """
        user = request.user
        routine_id = request.data.get('routine_id')
        day = request.data.get('day')
        date = request.data.get('date')
        completed_sets = int_param(request.data, 'completed_sets', default=1, minimum=0,
                                   maximum=MAX_SETS_PER_REQUEST)
        skipped = request.data.get('skipped', False)
        if not routine_id or not day or not date:
            return Response({'error': _('routine_id, day, and date are required.')}, status=400)

        parsed_date = parse_date(str(date))
        if parsed_date is None:
            return Response({'error': _('date must be in YYYY-MM-DD format.')},
                            status=status.HTTP_400_BAD_REQUEST)
        today = timezone.localdate()
        if parsed_date > today:
            return Response({'error': _('Cannot log a workout in the future.')},
                            status=status.HTTP_400_BAD_REQUEST)
        if (today - parsed_date).days > MAX_BACKLOG_DAYS:
            return Response(
                {'error': _('Cannot log a workout more than %(d)d days in the past.') % {'d': MAX_BACKLOG_DAYS}},
                status=status.HTTP_400_BAD_REQUEST)
        date = parsed_date

        from .models import Routine, RoutineExercise, UserExerciseProgress
        try:
            routine = Routine.objects.get(id=routine_id)
        except Routine.DoesNotExist:
            return Response({'error': _('Routine not found.')}, status=404)

        # AUTHORIZATION: the same check bulk-create already makes, and for the same two
        # reasons. Without it any caller could write progress against a routine never
        # assigned to them, AND read every exercise name of another trainer's private
        # routine straight out of the `results` array.
        if not (routine.assigned_to.filter(id=user.id).exists()
                or routine.created_by_id == user.id
                or user.is_admin):
            return Response({'error': _('You do not have access to this routine.')},
                            status=status.HTTP_403_FORBIDDEN)

        exercises = RoutineExercise.objects.filter(routine=routine, day=day)
        if not exercises.exists():
            return Response(
                {'error': _('Day %(day)s is not part of this routine.') % {'day': day}},
                status=status.HTTP_400_BAD_REQUEST)
        results = []
        errors = []
        for rex in exercises:
            try:
                # Savepoint: same reason as the set-log loop above — this one keeps
                # going after a failure, so the failed write must not take the
                # surrounding transaction with it.
                with transaction.atomic():
                    obj, created = UserExerciseProgress.objects.update_or_create(
                        user=user,
                        exercise=rex.exercise,
                        date=date,
                        defaults={
                            'completed_sets': completed_sets,
                            # The prescription, never a number the caller sent. Taking it
                            # from the request made `completed >= target` true by
                            # construction, so a day could be marked complete by claiming
                            # a target of 1.
                            'target_sets': rex.sets or 0,
                            'skipped': skipped
                        }
                    )
                results.append({'exercise': rex.exercise.name, 'created': created, 'id': obj.id})
            except IntegrityError as e:
                if 'unique constraint' in str(e).lower():
                    errors.append({'exercise': rex.exercise.name, 'error': 'Duplicate progress for this user, exercise, and date.'})
                else:
                    logger.exception("bulk progress write failed for %s", rex.exercise_id)
                    errors.append({'exercise': rex.exercise.name,
                                   'error': _('Could not record progress for this exercise.')})
        return Response({'results': results, 'errors': errors, 'count': len(results)})


class ExerciseImageUploadView(APIView):
    """
    Dedicated endpoint for uploading exercise images.
    Accepts multipart/form-data with image file.
    """
    permission_classes = [IsAuthenticated]
    parser_classes = [MultiPartParser, FormParser]

    def post(self, request, exercise_id):
        """Upload an image for a specific exercise"""
        try:
            # Get the exercise
            try:
                exercise = Exercise.objects.get(id=exercise_id)
            except Exercise.DoesNotExist:
                return Response(
                    {'error': 'Exercise not found'},
                    status=status.HTTP_404_NOT_FOUND
                )

            # Check if user has permission to modify this exercise.
            # Global exercises belong to the shared catalog: admin-only. The old
            # guard was `not is_global and created_by != user`, which short-circuits
            # to False for global exercises, leaving them writable by ANY user.
            _is_admin = getattr(request.user, 'is_admin', False) or request.user.is_staff
            if exercise.created_by_id:
                # Owned exercise: the creator (or an admin) may modify it. Keyed on
                # ownership rather than is_global, because the "created_by => not
                # global" invariant lives only in Exercise.clean(), which .create()
                # never runs — so an owned row can still carry is_global=True.
                allowed = (exercise.created_by_id == request.user.id) or _is_admin
            else:
                # No owner => shared global catalog => admin only.
                allowed = _is_admin
            if not allowed:
                return Response(
                    {'error': 'You do not have permission to modify this exercise'},
                    status=status.HTTP_403_FORBIDDEN
                )

            # Get the uploaded file
            image_file = request.FILES.get('image')
            
            if not image_file:
                return Response(
                    {'error': 'No image file provided. Please include a file with key "image"'},
                    status=status.HTTP_400_BAD_REQUEST
                )

            # Content-based validation (magic bytes) + pixel cap + re-encode.
            # The Content-Type header alone is spoofable: a PHP shell sent as
            # image/jpeg was previously stored as exercise_N_xxxx.php.
            from training_platform.file_security import process_uploaded_image
            from django.core.exceptions import ValidationError as DjangoValidationError
            try:
                safe_file, img_ext = process_uploaded_image(image_file, max_bytes=5 * 1024 * 1024)
            except DjangoValidationError as e:
                return Response(
                    {'error': e.messages[0] if getattr(e, 'messages', None) else str(e)},
                    status=status.HTTP_400_BAD_REQUEST
                )

            # Store under the DETECTED format's extension, never the client filename.
            exercise.image.save(f"exercise.{img_ext}", safe_file, save=False)
            exercise.save()

            # Return the updated exercise info
            return Response({
                'message': _('Exercise image uploaded successfully'),
                'exercise': {
                    'id': exercise.id,
                    'name': exercise.name,
                    'description': exercise.description,
                    'target_muscle': exercise.target_muscle,
                    'image_url': exercise.image.url if exercise.image else None,
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

    def delete(self, request, exercise_id):
        """Remove exercise image"""
        try:
            # Get the exercise
            try:
                exercise = Exercise.objects.get(id=exercise_id)
            except Exercise.DoesNotExist:
                return Response(
                    {'error': 'Exercise not found'},
                    status=status.HTTP_404_NOT_FOUND
                )

            # Check if user has permission to modify this exercise.
            # Global exercises belong to the shared catalog: admin-only. The old
            # guard was `not is_global and created_by != user`, which short-circuits
            # to False for global exercises, leaving them writable by ANY user.
            _is_admin = getattr(request.user, 'is_admin', False) or request.user.is_staff
            if exercise.created_by_id:
                # Owned exercise: the creator (or an admin) may modify it. Keyed on
                # ownership rather than is_global, because the "created_by => not
                # global" invariant lives only in Exercise.clean(), which .create()
                # never runs — so an owned row can still carry is_global=True.
                allowed = (exercise.created_by_id == request.user.id) or _is_admin
            else:
                # No owner => shared global catalog => admin only.
                allowed = _is_admin
            if not allowed:
                return Response(
                    {'error': 'You do not have permission to modify this exercise'},
                    status=status.HTTP_403_FORBIDDEN
                )

            if exercise.image:
                # See users/views.py: the post-commit receiver owns file removal.
                exercise.image = None
                exercise.save()
                
                return Response({
                    'message': _('Exercise image removed successfully')
                }, status=status.HTTP_200_OK)
            else:
                return Response({
                    'message': _('No image to remove')
                }, status=status.HTTP_404_NOT_FOUND)
                
        except PASSTHROUGH_EXCEPTIONS:
            # Control-flow exceptions carry their own correct status (404/403/401/400).
            # The broad handler below turned every one of them into a 500, which is the
            # wrong contract for the client and hides real faults in the error budget.
            raise
        except Exception as e:
            return Response(
                {'error': f'Failed to remove exercise image: {str(e)}'},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )


class ExerciseAddMediaView(APIView):
    """
    Add media (videos, photos, text) to existing exercises via URLs.
    Accepts JSON with media items to add to an exercise.
    
    Request format:
    {
        "media_items": [
            {
                "media_type": "video",
                "content": "https://youtube.com/watch?v=example",
                "title": "Exercise Tutorial",
                "description": "Step-by-step guide",
                "order": 1
            },
            {
                "media_type": "photo", 
                "content": "https://example.com/exercise-photo.jpg",
                "title": "Form Check",
                "description": "Proper form demonstration",
                "order": 2
            },
            {
                "media_type": "text",
                "content": "Keep your back straight and engage your core",
                "title": "Form Tips",
                "description": "Important form cues",
                "order": 3
            }
        ]
    }
    """
    permission_classes = [IsAuthenticated]
    
    def post(self, request, exercise_id):
        """Add media items to an existing exercise"""
        try:
            # Get the exercise
            try:
                exercise = Exercise.objects.get(id=exercise_id)
            except Exercise.DoesNotExist:
                return Response(
                    {'error': 'Exercise not found'},
                    status=status.HTTP_404_NOT_FOUND
                )

            # Check if user has permission to modify this exercise.
            # Global exercises belong to the shared catalog: admin-only. The old
            # guard was `not is_global and created_by != user`, which short-circuits
            # to False for global exercises, leaving them writable by ANY user.
            _is_admin = getattr(request.user, 'is_admin', False) or request.user.is_staff
            if exercise.created_by_id:
                # Owned exercise: the creator (or an admin) may modify it. Keyed on
                # ownership rather than is_global, because the "created_by => not
                # global" invariant lives only in Exercise.clean(), which .create()
                # never runs — so an owned row can still carry is_global=True.
                allowed = (exercise.created_by_id == request.user.id) or _is_admin
            else:
                # No owner => shared global catalog => admin only.
                allowed = _is_admin
            if not allowed:
                return Response(
                    {'error': 'You do not have permission to modify this exercise'},
                    status=status.HTTP_403_FORBIDDEN
                )

            # Get media items from request
            media_items = request.data.get('media_items', [])
            
            if not media_items:
                return Response(
                    {'error': 'No media items provided. Please include "media_items" array'},
                    status=status.HTTP_400_BAD_REQUEST
                )

            # Validate and create media items
            created_media = []
            errors = []
            
            for i, item in enumerate(media_items):
                try:
                    # Validate required fields
                    media_type = item.get('media_type')
                    content = item.get('content')
                    
                    if not media_type or not content:
                        errors.append(f"Item {i+1}: media_type and content are required")
                        continue
                    
                    # Validate media type
                    valid_types = ['video', 'photo', 'text']
                    if media_type not in valid_types:
                        errors.append(f"Item {i+1}: Invalid media_type. Must be one of {valid_types}")
                        continue
                    
                    # Validate content (basic URL validation for video/photo)
                    if media_type in ['video', 'photo']:
                        if not content.startswith(('http://', 'https://')):
                            errors.append(f"Item {i+1}: content must be a valid URL for {media_type}")
                            continue
                    
                    # Create media item
                    media_record = ExerciseMedia.objects.create(
                        exercise=exercise,
                        media_type=media_type,
                        content=content,
                        title=item.get('title', ''),
                        description=item.get('description', ''),
                        order=item.get('order', 0)
                    )
                    
                    created_media.append({
                        'id': media_record.id,
                        'media_type': media_record.media_type,
                        'content': media_record.content,
                        'title': media_record.title,
                        'description': media_record.description,
                        'order': media_record.order
                    })
                    
                except Exception as e:
                    errors.append(f"Item {i+1}: {str(e)}")
            
            # Return response
            response_data = {
                'message': f'Successfully added {len(created_media)} media items to exercise "{exercise.name}"',
                'exercise_id': exercise.id,
                'exercise_name': exercise.name,
                'created_media': created_media,
                'total_media_count': exercise.media.count()
            }
            
            if errors:
                response_data['errors'] = errors
                response_data['partial_success'] = True
                return Response(response_data, status=status.HTTP_207_MULTI_STATUS)
            else:
                return Response(response_data, status=status.HTTP_201_CREATED)

        except PASSTHROUGH_EXCEPTIONS:
            # Control-flow exceptions carry their own correct status (404/403/401/400).
            # The broad handler below turned every one of them into a 500, which is the
            # wrong contract for the client and hides real faults in the error budget.
            raise
        except Exception as e:
            return Response(
                {'error': f'Failed to add media: {str(e)}'},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )

    def get(self, request, exercise_id):
        """Get all media for an exercise"""
        try:
            # Get the exercise
            try:
                exercise = Exercise.objects.get(id=exercise_id)
            except Exercise.DoesNotExist:
                return Response(
                    {'error': 'Exercise not found'},
                    status=status.HTTP_404_NOT_FOUND
                )

            # Get all media for this exercise
            media_items = exercise.media.all().order_by('order', 'created_at')
            
            media_data = []
            for media in media_items:
                media_data.append({
                    'id': media.id,
                    'media_type': media.media_type,
                    'content': media.content,
                    'title': media.title,
                    'description': media.description,
                    'order': media.order,
                    'created_at': media.created_at
                })
            
            return Response({
                'exercise_id': exercise.id,
                'exercise_name': exercise.name,
                'media_count': len(media_data),
                'media_items': media_data
            }, status=status.HTTP_200_OK)

        except PASSTHROUGH_EXCEPTIONS:
            # Control-flow exceptions carry their own correct status (404/403/401/400).
            # The broad handler below turned every one of them into a 500, which is the
            # wrong contract for the client and hides real faults in the error budget.
            raise
        except Exception as e:
            return Response(
                {'error': f'Failed to get media: {str(e)}'},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )

    def delete(self, request, exercise_id):
        """Delete specific media items from an exercise"""
        try:
            # Get the exercise
            try:
                exercise = Exercise.objects.get(id=exercise_id)
            except Exercise.DoesNotExist:
                return Response(
                    {'error': 'Exercise not found'},
                    status=status.HTTP_404_NOT_FOUND
                )

            # Check if user has permission to modify this exercise.
            # Global exercises belong to the shared catalog: admin-only. The old
            # guard was `not is_global and created_by != user`, which short-circuits
            # to False for global exercises, leaving them writable by ANY user.
            _is_admin = getattr(request.user, 'is_admin', False) or request.user.is_staff
            if exercise.created_by_id:
                # Owned exercise: the creator (or an admin) may modify it. Keyed on
                # ownership rather than is_global, because the "created_by => not
                # global" invariant lives only in Exercise.clean(), which .create()
                # never runs — so an owned row can still carry is_global=True.
                allowed = (exercise.created_by_id == request.user.id) or _is_admin
            else:
                # No owner => shared global catalog => admin only.
                allowed = _is_admin
            if not allowed:
                return Response(
                    {'error': 'You do not have permission to modify this exercise'},
                    status=status.HTTP_403_FORBIDDEN
                )

            # Get media IDs to delete
            media_ids = request.data.get('media_ids', [])
            
            if not media_ids:
                return Response(
                    {'error': 'No media_ids provided. Please include "media_ids" array'},
                    status=status.HTTP_400_BAD_REQUEST
                )

            # Delete media items
            deleted_count = 0
            errors = []
            
            for media_id in media_ids:
                try:
                    media_item = ExerciseMedia.objects.get(id=media_id, exercise=exercise)
                    media_item.delete()
                    deleted_count += 1
                except ExerciseMedia.DoesNotExist:
                    errors.append(f"Media item {media_id} not found")
                except Exception as e:
                    errors.append(f"Failed to delete media {media_id}: {str(e)}")
            
            response_data = {
                'message': f'Successfully deleted {deleted_count} media items from exercise "{exercise.name}"',
                'exercise_id': exercise.id,
                'deleted_count': deleted_count,
                'remaining_media_count': exercise.media.count()
            }
            
            if errors:
                response_data['errors'] = errors
                response_data['partial_success'] = True
                return Response(response_data, status=status.HTTP_207_MULTI_STATUS)
            else:
                return Response(response_data, status=status.HTTP_200_OK)

        except PASSTHROUGH_EXCEPTIONS:
            # Control-flow exceptions carry their own correct status (404/403/401/400).
            # The broad handler below turned every one of them into a 500, which is the
            # wrong contract for the client and hides real faults in the error budget.
            raise
        except Exception as e:
            return Response(
                {'error': f'Failed to delete media: {str(e)}'},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )


class TrainerClientProgressViewSet(viewsets.ViewSet):
    permission_classes = [IsTrainerOrAdmin]

    @action(detail=False, methods=['get'], url_path='(?P<client_id>[^/.]+)')
    def client_progress(self, request, client_id=None):
        """
        Optimized client progress with prefetched data.
        Returns:
            - Client profile overview
            - Recent completion stats
            - Volume/Strength trends (efficiently calculated)
            - Detailed recent sessions
        """
        user = request.user
        
        # 1. Validate permissions
        if not (user.is_trainer or user.is_staff):
             # Trainers can only see assigned clients, admins see all
             pass # Permission class IsTrainerForClient handles this if applied, but let's double check relation if needed.
             # Actually, the view permission IsTrainerForClient should handle the access control per object, 
             # but here we are using a custom action on the LIST view, so we need to valid manually or rely on the queryset filter.
             # We'll rely on the manual check below for robust security.

        try:
             # 2. Optimized Database Query
             # Fetch client with all necessary related data in one go
             from users.models import CustomUser, TrainerClientRelation
             from django.db.models import Prefetch
             
             # Verify relationship first
             if user.is_trainer:
                 is_approved = TrainerClientRelation.objects.filter(
                     trainer=user, 
                     client_id=client_id, 
                     status='approved'
                 ).exists()
                 if not is_approved:
                     return Response(
                         {"error": "You can only view progress for your approved clients."}, 
                         status=status.HTTP_403_FORBIDDEN
                     )

             # Main optimized query
             client = CustomUser.objects.select_related(
                 'assigned_trainer'
             ).prefetch_related(
                 # Prefetch recent completed sessions with their set logs and exercises
                 Prefetch(
                     'workout_sessions',
                     queryset=WorkoutSession.objects.filter(
                        status='completed'
                     ).order_by('-end_time').prefetch_related(
                         Prefetch(
                             'set_logs',
                             queryset=ExerciseSetLog.objects.select_related(
                                 'user_exercise_progress__exercise'
                             ).order_by('set_number')
                         )
                     )
                 ),
                 # Prefetch routine progress
                 'routine_progress'
             ).get(id=client_id, user_type='client')
             
             # 3. Efficient In-Memory Calculation (No new DB queries)
             recent_sessions = client.workout_sessions.all() # Uses prefetch cache
             
             # Calculate Weekly Stats (Python-side to use prefetch)
             now = timezone.now()
             week_start = now - timedelta(days=7)
             
             week_sessions = [s for s in recent_sessions if s.end_time and s.end_time >= week_start]
             week_volume = 0
             for session in week_sessions:
                 for log in session.set_logs.all(): # Uses prefetch cache
                     if log.weight and log.reps:
                         week_volume += log.weight * log.reps
                         
             # Format Recent Activity
             formatted_sessions = []
             for session in recent_sessions:
                 session_volume = sum(
                     (l.weight * l.reps) for l in session.set_logs.all() if l.weight and l.reps
                 )
                 
                 exercises_done = set()
                 for log in session.set_logs.all():
                     if log.user_exercise_progress and log.user_exercise_progress.exercise:
                         exercises_done.add(log.user_exercise_progress.exercise.name)
                         
                 formatted_sessions.append({
                     'id': session.id,
                     'date': session.end_time,
                     'duration': session.duration,
                     'volume': session_volume,
                     'routine_name': session.routine.name if session.routine else "Custom Workout",
                     'exercises': list(exercises_done)
                 })

             return Response({
                 'client_info': {
                     'id': client.id,
                     'name': client.full_name or client.username,
                     'joined': client.date_joined,
                     'goal': getattr(client, 'client_goals', 'Not set')
                 },
                 'weekly_stats': {
                     'sessions_count': len(week_sessions),
                     'total_volume': week_volume,
                 },
                 'recent_activity': formatted_sessions
             })

        except CustomUser.DoesNotExist:
            return Response({
                'error': 'Client not found.'},
                status=status.HTTP_404_NOT_FOUND
            )
        if not latest_progress:
            return Response({
                'client_info': {
                    'id': client.id,
                    'name': client.username,
                    'full_name': client.full_name,
                    'profile_picture': client.profile_picture.url if client.profile_picture else None,
                    'last_workout': None,
                    'total_workouts': 0,
                    'completion_rate': 0.0
                },
                'recent_sessions': [],
                'progress_summary': {
                    'this_week': {'sessions': 0, 'total_volume': 0, 'exercises_completed': 0},
                    'this_month': {'sessions': 0, 'total_volume': 0, 'exercises_completed': 0}
                }
            })
        
        # Use the enhanced serializer
        serializer = DetailedClientProgressSerializer(latest_progress)
        return Response(serializer.data)

    @action(detail=False, methods=['get'], url_path='recent')
    def recent_progress(self, request):
        """
        Get recent progress for all clients assigned to this trainer.
        Returns enhanced summary with detailed activity information.
        """
        user = request.user
        if not user.is_trainer:
            return Response(
                {'error': 'Only trainers can access this endpoint.'}, 
                status=403
            )
        
        # Get all clients assigned to this trainer
        clients = CustomUser.objects.filter(
            assigned_trainer=user, user_type='client'
        ).prefetch_related(
            'workout_sessions',
            'exercise_progress__set_logs',
            'routine_progress'
        )
        
        recent_activity = []
        for client in clients:
            # Use the enhanced serializer for each client
            serializer = RecentActivitySerializer(client)
            recent_activity.append(serializer.data)
        
        return Response({
            'trainer_id': user.id,
            'trainer_name': user.username,
            'client_count': len(recent_activity),
            'recent_activity': recent_activity
        })