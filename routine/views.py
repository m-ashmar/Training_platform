from rest_framework import viewsets, status, permissions, generics, serializers
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.pagination import PageNumberPagination
from training_platform.pagination import StandardPagination
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
    IsClientOrAssignedTrainer,  # Added for RoutineProgressViewSet
    # The one predicate for "this trainer may see this client": an APPROVED relation.
    accessible_user_ids,
)
import logging
from .services import send_notification
from django.utils import timezone
from datetime import timedelta

# How far back the trainer's client-progress screen looks. The weekly statistics
# use the last seven days; the activity list shows the month around them.
RECENT_ACTIVITY_DAYS = 30
from rest_framework.permissions import IsAuthenticated
from django_filters.rest_framework import DjangoFilterBackend
from rest_framework.parsers import MultiPartParser, FormParser
from rest_framework.views import APIView
from users.models import CustomUser
from rest_framework.exceptions import PermissionDenied
from django.db import transaction
from django.http import Http404
from rest_framework.exceptions import (APIException, NotAuthenticated, NotFound,
                                       PermissionDenied,
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


class StandardResultsSetPagination(StandardPagination):
    # Subclasses the project paginator so a bad `page_size` gets the same 400 here
    # as everywhere else; this class exists only for the `page` handling below.
    # 25, matching `REST_FRAMEWORK['PAGE_SIZE']` and what API_CONTRACT.md publishes.
    # This said 20, so the contract's documented page size was wrong for every list it
    # paginates and a client sizing its own buffers from the docs got it wrong too.
    page_size = 25
    page_size_query_param = 'page_size'
    max_page_size = 100

    def paginate_queryset(self, queryset, request, view=None):
        """A bad `page` is a 400 naming the parameter, not a 404.

        DRF answers `NotFound("Invalid page.")` for both a non-numeric page and one past
        the end, while the hand-rolled paginator on `food/list` answers 400 for the first
        and clamps the second. So a client could not write one handler for a paging
        error — and on a LIST endpoint a 404 is indistinguishable from "the thing you
        asked about is gone".
        """
        from rest_framework.exceptions import NotFound, ValidationError

        try:
            return super().paginate_queryset(queryset, request, view)
        except NotFound as exc:
            raise ValidationError({self.page_query_param: [str(exc.detail)]}) from exc


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


def _int_or_none(value):
    """An id from a request body or query string, or None if it is not one.

    These were passed straight to the ORM, where a filter on an integer column raises
    `ValueError` — which is not a DRF exception, so it left the view as an unhandled
    500 on a URL or body any client can send.
    """
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


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
            # `created_by__in=trainers`, from the approved relations — not
            # `created_by=user.assigned_trainer`, the display pointer.
            from users.models import TrainerClientRelation
            trainers = (TrainerClientRelation.objects
                        .filter(client=user, status='approved')
                        .values_list('trainer_id', flat=True))
            return base_qs.filter(
                models.Q(created_by__isnull=True)
                | models.Q(created_by_id__in=trainers)
                | models.Q(routine_exercises__routine__assigned_to=user)
            ).distinct()

    def perform_create(self, serializer):
        """Override to assign the exercise to the creator."""
        serializer.save(created_by=self.request.user)

    def destroy(self, request, *args, **kwargs):
        """Refuse to delete an exercise clients have logged work against.

        This guard was written for Routine and attached here, where it could never
        fire: nothing PROTECTs an Exercise. `UserExerciseProgress.exercise` and
        `ExerciseSetLog` both CASCADE, so deleting one exercise silently erased every
        client's history for it and changed their past volume and streaks. Refuse, and
        offer the retire path instead.
        """
        exercise = self.get_object()
        if exercise.progress.exists():
            return Response(
                {'error': _('Clients have recorded work against this exercise, so it '
                            'cannot be deleted. Set is_active=false to retire it.')},
                status=status.HTTP_409_CONFLICT,
            )
        return super().destroy(request, *args, **kwargs)


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
            # (re-encoded file, detected extension). These — never the uploads — are what
            # is stored: the originals were saved byte for byte under the client's own
            # filename, so a JPEG with HTML appended, named `x.html`, was kept as it came.
            validated_photos = []
            if media_photos:
                from training_platform.file_security import process_uploaded_image
                from django.core.exceptions import ValidationError as DjangoValidationError
                for i, photo in enumerate(media_photos):
                    # Same content validation as the main image — header checks alone
                    # let arbitrary payloads through.
                    try:
                        safe_photo, photo_ext = process_uploaded_image(photo, max_bytes=5 * 1024 * 1024)
                        validated_photos.append((safe_photo, photo_ext))
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
            for i, (photo_file, photo_ext) in enumerate(validated_photos):
                # For uploaded photos, we need to save them and create a URL
                # Since ExerciseMedia.content expects URLs, we'll store the file path.
                # A server-made name with the DETECTED format's extension.
                photo_name = f"exercise_media_{exercise.id}_{i}.{photo_ext}"
                
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
    
    A routine is ONE client's private program (the owner, 2026-09-19). Reuse goes
    through templates (`templates/{id}/generate/`) or `routines/{id}/copy/`; see
    `routine/assignment.py`. `POST /routines/` writes an unassigned routine and ignores
    `assigned_to`; assign with `routines/{id}/assign_to_client/`.

    List filter: `?source_template=<template id>` — the copies generated from it.
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
        # `progress` is NOT prefetched: the serializer counted those rows in Python
        # "to avoid a DB hit", which forced every one of them into memory — twenty
        # routines x twenty clients x thirty days is 12,000 rows fetched to produce
        # twenty percentages. They are counted in the query now.
        queryset = self.queryset.select_related('created_by').prefetch_related(
            'routine_exercises__exercise__media',
            'assigned_to',
        ).annotate(
            client_count=models.Count('assigned_to', filter=models.Q(assigned_to__user_type='client'), distinct=True),
            progress_total=models.Count('progress', distinct=True),
            progress_done=models.Count('progress', filter=models.Q(progress__status='completed'), distinct=True)
        # annotate() with an aggregate adds a GROUP BY, and Django then drops
        # Meta.ordering from the SQL entirely — the query came back with GROUP BY and
        # no ORDER BY at all. Paging a routine list in whatever order Postgres felt
        # like meant page 2 could repeat a routine from page 1 and skip another
        # outright. Re-apply the model's total order explicitly.
        ).order_by('-created_at', '-id')

        # `?source_template=<id>`: the private copies written from one template, e.g.
        # "which of my clients got this program". Narrows; never widens what you see.
        source = int_param(self.request.query_params, 'source_template')
        if source is not None:
            queryset = queryset.filter(source_template_id=source)

        if user.is_admin:
            # Admins can see all routines
            return queryset.all()
        elif user.is_trainer:
            # Trainers can see routines they created and routines assigned to their clients
            # `accessible_user_ids` is the one predicate that means "this trainer may see
        # this client": it requires an APPROVED relation. These querysets filtered on
        # the `assigned_trainer` pointer instead, which is a second definition of the
        # same thing — and two definitions drift. Dropping the database does not fix
        # it, because fresh rows flow through the same code.
            ids = accessible_user_ids(user)
            if ids is None:
                return queryset.all()
            return queryset.filter(
                models.Q(created_by=user) |
                models.Q(assigned_to__id__in=ids)
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

    def destroy(self, request, *args, **kwargs):
        """A routine with recorded progress cannot be deleted.

        `RoutineProgress.routine` is PROTECT, and a row is created for every day of
        every assignment, so every assigned routine is protected — and `ProtectedError`
        is not a DRF exception, so deleting one was an unhandled 500 rather than a
        message. Retiring is the operation the trainer actually wants.
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

    @action(detail=True, methods=['post'], permission_classes=[permissions.IsAuthenticated])
    def copy(self, request, pk=None):
        """POST /api/routine/routines/{id}/copy/ — a fresh copy of your routine, for
        another client.

        A routine belongs to one client (BACKEND_BUGS B-001), so a trainer who wants the
        same program for someone else copies it: same days and exercises, no assignee,
        starting today. Only the trainer who wrote it (or an admin); 403 otherwise.
        201 with the new routine.
        """
        from .assignment import copy_routine

        original = self.get_object()
        if not (original.created_by_id == request.user.id
                or getattr(request.user, 'is_admin', False)):
            return Response({'error': _('Only the trainer who wrote this routine can copy it.'),
                             'code': 'permission_denied'}, status=status.HTTP_403_FORBIDDEN)
        copy = copy_routine(original)
        return Response(RoutineSerializer(copy, context={'request': request}).data,
                        status=status.HTTP_201_CREATED)

    @action(detail=True, methods=['post'], permission_classes=[IsTrainerOrAdminForAssignment])
    def assign_to_client(self, request, pk=None):
        """POST /api/routine/routines/{id}/assign_to_client/ — make this routine one
        client's program. Body: `client_id`.

        Trainers (for an approved client of theirs) and admins. A routine belongs to one
        client (the owner, 2026-09-19): the first assignment also settles the coaching's
        first month, and the client is notified in their language.

        200 `{message, routine_id, client_id, assignment_date}`.
        400 `client_id` missing; 400 the client is not an approved client of yours; 400
        already assigned to this client (nothing is paid twice).
        404 no such client.
        409 `routine_belongs_to_another_client` with `copy_url`: another client is on
        it, or another client has trained on it (their history stays with it) — copy it
        with `routines/{id}/copy/` instead. A trainer's own tries on a draft do not
        count. Identical on every build.
        """
        routine = self.get_object()
        client_id = _int_or_none(request.data.get('client_id'))
        
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
            
            # One client per routine: a coach writes a routine FOR a client, and clients
            # never share one (owner, 2026-09-19; BACKEND_BUGS B-001). A second client
            # gets their own copy (templates exist for reuse). `assign_routine` checks
            # under a lock on the routine — two racing assignments for different clients
            # both passed an unlocked check — and also refuses a routine someone else
            # has trained on. Then it settles the hold taken for THIS client, once: this
            # used to move the trainer's CURRENT hourly rate out of the shared escrow
            # wallet on every assignment, with no link to any hold.
            from .assignment import RoutineTaken, assign_routine
            try:
                assign_routine(routine, client, request.user)
            except RoutineTaken:
                return Response(
                    {"error": _("This routine already belongs to another client. "
                                "Create a copy for this client instead."),
                     "code": "routine_belongs_to_another_client",
                     "routine_id": routine.id,
                     "copy_url": f"/api/routine/routines/{routine.id}/copy/"},
                    status=status.HTTP_409_CONFLICT)

            # Log the assignment
            logger.info(
                f"Routine {routine.id} assigned to client {client.id} by trainer {request.user.id}"
            )
            
            # Trigger notification to client about new assignment, in their language.
            from .services import notify_assignment
            notify_assignment(client, routine)
            
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
        client_id = _int_or_none(request.data.get('client_id'))
        
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
        """POST /api/routine/routines/{id}/update_progress/ — set a day's status for today.

        Body: `day`, `status` (`not_started | in_progress | completed | skipped`), and
        `confirm_unlogged` (bool). Completing a day does not require every set to be
        logged, but the person must be warned first (owner, 2026-09-19): while any
        prescribed set of that day is unlogged today, `completed` answers
        409 `unlogged_sets` with `{prescribed, logged, unlogged}`, and the app sends the
        same request again with `confirm_unlogged: true` once the person has seen it.
        Other codes: 400 `bad_status`; 403 when the routine is not yours; 403
        `coaching_frozen` / `coaching_unpaid` / `coaching_ended` (with `trainer_id`) while the coaching this
        routine came from is unpaid or over — readable, not trainable (the owner).
        """
        routine = self.get_object()
        user = request.user
        day = request.data.get("day")
        status = request.data.get("status")

        if user not in routine.assigned_to.all():
            return Response({"error": _("You are not authorized to update this routine.")}, status=403)
        _require_can_train(user, routine)

        if not day or not status:
            return Response({"error": _("Day and status are required.")}, status=400)

        # Parsed once, and before it is used. `int(day)` sat unguarded in the comparison
        # below, so `{"day": "abc"}` was a ValueError and a 500 rather than a 400.
        try:
            day_number = int(str(day).strip())
        except (TypeError, ValueError):
            return Response({"error": _("Day must be a whole number.")}, status=400)

        # A declared status, not any string. `choices` is a form-layer rule and
        # `update_or_create` never calls `full_clean`, so 'banana' was stored and
        # returned with a 200 — a row that is neither complete nor incomplete, invisible
        # to every `filter(status=...)` in analytics and a value the client's switch has
        # no case for.
        valid_statuses = [choice[0] for choice in RoutineProgress.STATUS_CHOICES]
        if status not in valid_statuses:
            return Response(
                {"error": _("status must be one of: %(options)s")
                 % {"options": ", ".join(valid_statuses)}, "code": "bad_status"},
                status=400)

        # Validate if the day exists in the routine's exercises
        valid_days = [exercise.day for exercise in routine.routine_exercises.all()]
        if day_number not in valid_days:
            return Response({"error": _("Day %(day)s is not part of the routine.") % {"day": day}}, status=400)

        if status == 'completed' and not _truthy(request.data.get('confirm_unlogged')):
            counts = _day_set_counts(routine, user, day_number)
            if counts['unlogged']:
                return Response({
                    'error': _('Some sets of this day are not logged yet.'),
                    'code': 'unlogged_sets', **counts}, status=409)

        try:
            updated_progress = routine.update_progress(user=user, day=day_number, status=status)
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
        
        # The APPROVED relation, not `request.user.clients` — the reverse accessor of
        # `CustomUser.assigned_trainer`, which routine/permissions.py forbids as an
        # access test. It was inverted here: a DISMISSED trainer at whom the pointer
        # still aimed was told they had a client, and the approved trainer was told they
        # had none. Only a count leaks today because the body is a stub, which is
        # exactly why it matters — whatever fills it in inherits the predicate.
        from .permissions import accessible_user_ids
        from users.models import CustomUser

        clients = CustomUser.objects.filter(
            id__in=accessible_user_ids(request.user) or [], user_type='client')
        
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
    A client's create or update answers 403 `coaching_frozen` / `coaching_unpaid` / `coaching_ended` (with
    `trainer_id`) while the coaching the routine came from is unpaid or over.
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
        _require_can_train(user, routine, serializer.validated_data.get('date'))
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

    def perform_update(self, serializer):
        """A client's own day, refused like a create while the coaching is unpaid or
        over (403 `coaching_frozen` / `coaching_unpaid` / `coaching_ended`). A trainer's correction is not."""
        instance = serializer.instance
        _require_can_train(self.request.user, instance.routine,
                           serializer.validated_data.get('date', instance.date))
        serializer.save()

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
            ids = accessible_user_ids(user)
            return base_qs if ids is None else base_qs.filter(user_id__in=ids)
        else:
            return base_qs.filter(user=user)



def _records_in_window(user, start_date) -> int:
    """How many exercises `user` set a record in from `start_date` on.

    A record is a set heavier than every set of that exercise logged before it —
    including earlier sets inside the window, so a first month of steady progress counts.
    The very first set of an exercise is where records are measured from, not one.
    """
    key = 'user_exercise_progress__exercise'
    weighted = ExerciseSetLog.objects.filter(user_exercise_progress__user=user, weight__gt=0)
    exercises = list(weighted.filter(date__gte=start_date).order_by()
                     .values_list(key, flat=True).distinct())
    if not exercises:
        return 0
    best: dict = {}
    with_record = set()
    for exercise, day, weight in (weighted.filter(**{f'{key}__in': exercises})
                                  .order_by('date', 'id').values_list(key, 'date', 'weight')):
        if exercise in best and weight > best[exercise] and day >= start_date:
            with_record.add(exercise)
        best[exercise] = max(best.get(exercise, weight), weight)
    return len(with_record)

def _truthy(value) -> bool:
    return value is True or str(value).strip().lower() in ('1', 'true', 'yes')


def _day_set_counts(routine, user, day) -> dict:
    """Prescribed, logged and unlogged sets of one routine day, for today.

    Logged means a set log today for one of the day's exercises, counted at most up to
    what that exercise prescribes (a tenth set does not make up for a missing exercise).
    """
    from django.db.models import Count

    from .models import UserExerciseProgress

    today = timezone.localdate()
    prescribed = {}
    for exercise_id, sets in (routine.routine_exercises.filter(day=day)
                              .values_list('exercise_id', 'sets')):
        # An exercise with no set count still asks for one (a treadmill run).
        prescribed[exercise_id] = prescribed.get(exercise_id, 0) + max(sets or 0, 1)
    logged = dict(ExerciseSetLog.objects
                  .filter(user_exercise_progress__user=user,
                          user_exercise_progress__exercise_id__in=list(prescribed),
                          date=today)
                  .order_by().values('user_exercise_progress__exercise_id')
                  .annotate(n=Count('id'))
                  .values_list('user_exercise_progress__exercise_id', 'n'))
    # "Log a whole day" (bulk-complete) records completed sets without set logs.
    for exercise_id, done in (UserExerciseProgress.objects
                              .filter(user=user, exercise_id__in=list(prescribed), date=today)
                              .values_list('exercise_id', 'completed_sets')):
        logged[exercise_id] = max(logged.get(exercise_id, 0), done or 0)
    total = sum(prescribed.values())
    done = sum(min(logged.get(e, 0), n) for e, n in prescribed.items())
    return {'prescribed': total, 'logged': done, 'unlogged': total - done}

def _require_can_train(user, routine, at=None):
    """403 `coaching_frozen` / `coaching_unpaid` / `coaching_ended` when a client may see this routine but
    not train on it (the owner, 2026-09-19; users/coaching.py `training_refusal`)."""
    from users.coaching import require_can_train
    require_can_train(user, routine, at)


def _require_can_train_exercise(user, exercise_id, at=None):
    """The same rule for a write that names only an exercise (a progress row, or a set
    logged without a session): refused when EVERY routine of this client that holds
    the exercise is refused. An exercise on none of their routines is theirs to log.
    Asked once per trainer, not per routine: the answer depends on the pair alone, and a
    long-term client holds many private copies from the same trainer."""
    from users.coaching import _trainer_refusal, raise_refusal

    if not getattr(user, 'is_client', False) or exercise_id is None:
        return
    trainers = set(Routine.objects.filter(assigned_to=user,
                                          routine_exercises__exercise_id=exercise_id)
                   .values_list('created_by_id', flat=True))
    first = None
    for trainer_id in sorted(t for t in trainers if t is not None):
        if trainer_id == user.pk:
            return
        refusal = _trainer_refusal(user.pk, trainer_id, at)
        if refusal is None:
            return
        first = first or (refusal, trainer_id)
    if first is not None:
        raise_refusal(*first)


def _idempotent_create(view, request, model, owner_of, create):
    """Create — or, for a retry, answer with the row the first attempt already wrote.

    The app logs sets and workouts offline and syncs them later, and a retry whose first
    attempt DID land (the answer was lost) used to write a second row and double the
    volume (BACKEND_BUGS B-003). A request may carry `client_uuid`, the app's own id for
    the row: the first request with it is created (201); any later one answers 200 with
    that row, unchanged. An id that belongs to someone else's row answers
    409 `idempotency_conflict` and says nothing about the row.
    """
    import uuid as _uuid

    from .permissions import can_access_user_data

    raw = request.data.get('client_uuid') if hasattr(request.data, 'get') else None
    try:
        # As DRF's UUIDField reads it: a string, or the 128-bit integer form.
        key = (None if raw in (None, '') else
               _uuid.UUID(int=raw) if isinstance(raw, int) and not isinstance(raw, bool)
               else _uuid.UUID(str(raw)))
    except (ValueError, TypeError, AttributeError):
        key = None      # malformed: the serializer answers for it

    def replay():
        found = model.objects.filter(client_uuid=key).first()
        if found is None:
            return None
        owner_id, user = owner_of(found), request.user
        if owner_id == user.id or getattr(user, 'is_admin', False) or (
                user.is_trainer and owner_id and can_access_user_data(user, owner_id)):
            return Response(view.get_serializer(found).data, status=status.HTTP_200_OK)
        return Response({'error': _('This id belongs to another record.'),
                         'code': 'idempotency_conflict'}, status=status.HTTP_409_CONFLICT)

    if key is not None:
        answer = replay()
        if answer is not None:
            return answer
    try:
        with transaction.atomic():
            return create()
    except IntegrityError:
        # Two attempts raced; the unique index let one through. Answer with its row.
        answer = replay() if key is not None else None
        if answer is None:
            raise
        return answer

class ExerciseSetLogViewSet(viewsets.ModelViewSet):
    """
    ViewSet for logging individual exercise sets.

    POST may carry `client_uuid` (the app's id for the set): the first POST with it
    answers 201, a retry answers 200 with the same row, and an id already used by
    someone else's set answers 409 `idempotency_conflict`. `date` is writable, so a set
    logged offline keeps its day.

    A client's new set answers 403 `coaching_frozen` / `coaching_unpaid` / `coaching_ended` (with
    `trainer_id`) while the coaching its routine came from is unpaid or over, unless the
    set is dated inside a paid month (training done offline while paid still syncs).
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
        routine_id = _int_or_none(self.request.query_params.get('routine_id'))
        if routine_id:
            base_qs = base_qs.filter(workout_session__routine_id=routine_id)
        
        if user.is_admin:
            return base_qs
        elif user.is_trainer:
            ids = accessible_user_ids(user)
            return (base_qs if ids is None
                    else base_qs.filter(user_exercise_progress__user_id__in=ids))
        else:
            return base_qs.filter(user_exercise_progress__user=user)

    def create(self, request, *args, **kwargs):
        return _idempotent_create(
            self, request, ExerciseSetLog,
            lambda s: (s.user_exercise_progress.user_id if s.user_exercise_progress_id
                       else s.workout_session.user_id if s.workout_session_id else None),
            lambda: super(ExerciseSetLogViewSet, self).create(request, *args, **kwargs))

    def perform_create(self, serializer):
        # After the idempotent replay in `create`: a set that landed while the coaching
        # was paid is answered, not refused. Offline sets carry their own date.
        # A set in a session is judged when the session began; one without is judged
        # on the date it is stored with (the model's default is today), never on an
        # older progress row's date, which let a frozen client log today's sets.
        data = serializer.validated_data
        session = data.get('workout_session')
        progress = data.get('user_exercise_progress')
        if session is not None:
            _require_can_train(self.request.user, session.routine, session.start_time)
        elif progress is not None:
            _require_can_train_exercise(self.request.user, progress.exercise_id,
                                        data.get('date') or timezone.localdate())
        try:
            serializer.save()
        except IntegrityError as e:
            if 'client_uuid' in str(e):
                raise       # a retry racing its first attempt: `create` answers it
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
        routine_id = _int_or_none(request.query_params.get('routine_id'))
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
        403 `coaching_frozen` / `coaching_unpaid` / `coaching_ended` (with `trainer_id`): the coaching this
        routine came from is unpaid or over — readable, not trainable (the owner).
        """
        user = request.user
        routine_id = _int_or_none(request.data.get('routine_id'))
        day = _int_or_none(request.data.get('day'))
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

        _require_can_train(user, routine, date)

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
    A client opens sessions on routines assigned to them; a trainer may open one for an
    approved client (`user` in the body). The owner PATCHes their session.

    While the coaching a routine came from is unpaid (frozen) or over, the routine stays
    readable and opening or editing a session on it answers 403 `coaching_frozen` /
    `coaching_unpaid` / `coaching_ended` with `trainer_id` (the owner, 2026-09-19). A
    session begun while
    paid can still be finished, and an offline one dated inside a paid month still syncs.
    
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

    def create(self, request, *args, **kwargs):
        """POST: open a session. Body: `routine`, `day`, optional `client_uuid` (the
        app's id: a retry answers 200 with the first row; someone else's id answers 409
        `idempotency_conflict`) and optional `start_time` for a workout logged offline
        (at most 30 days back: `start_time_in_future`, `start_time_too_old`); `end_time`
        is then checked against it. Field codes (400): `routine_inactive` on `routine`,
        `day_out_of_range` / `day_has_no_exercises` on `day`."""
        def create_it():
            response = super(WorkoutSessionViewSet, self).create(request, *args, **kwargs)
            if response.status_code == status.HTTP_201_CREATED and \
                    response.data.get('status') == 'completed':
                # A workout synced already finished gets what finishing it online gets.
                self._completed(WorkoutSession.objects.get(pk=response.data['id']), response)
            return response

        return _idempotent_create(self, request, WorkoutSession, lambda s: s.user_id, create_it)

    def perform_create(self, serializer):
        """
        Handles creation of a workout session, assigning to the correct user.
        Input: serializer (validated data), request.user
        Output: Saves WorkoutSession instance
        """
        logger = logging.getLogger(__name__)
        logger.debug(f"Creating WorkoutSession with data: {serializer.validated_data} by user {self.request.user}")
        routine = serializer.validated_data.get('routine')
        at = serializer.validated_data.get('start_time')
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
                _require_can_train(user_obj, routine, at)
                serializer.save(user=user_obj)
                logger.debug(f"WorkoutSession assigned to user {user_obj}")
                return
        _require_can_train(self.request.user, routine, at)
        serializer.save(user=self.request.user)
        logger.debug(f"WorkoutSession assigned to user {self.request.user}")

    def perform_update(self, serializer):
        # Judged at the session's own start: finishing a workout begun while the coaching
        # was paid is allowed after it freezes.
        instance = serializer.instance
        _require_can_train(self.request.user, instance.routine, instance.start_time)
        serializer.save()

    def partial_update(self, request, *args, **kwargs):
        """
        PATCH: Used by client to mark session as completed, etc.
        Triggers notification on session completion.
        """
        # Was it already finished BEFORE this request? The branch below keyed on
        # "the payload says completed", not on a transition, so a double-tapped finish
        # button congratulated the client twice, told the trainer twice, and queued the
        # milestone walk twice.
        was_completed = self.get_object().status == 'completed'
        response = super().partial_update(request, *args, **kwargs)
        instance = self.get_object()
        # If session is marked as completed, notify trainer and client
        if (not was_completed and 'status' in request.data
                and request.data['status'] == 'completed'):
            self._completed(instance, response)
        return response

    def _completed(self, instance, response):
        """What finishing a workout does, whether it is finished by PATCH or created
        already finished by an offline sync (BACKEND_BUGS B-003)."""
        # Stamp end_time so `duration` is computable. Nothing set this before,
        # so every completed session reported duration = None forever. Never more
        # than the longest a session may last: an offline session is dated when it
        # began, and "now" may be days after that.
        if instance.end_time is None:
            cap = instance.start_time + timedelta(
                seconds=WorkoutSessionSerializer.MAX_SESSION_SECONDS)
            instance.end_time = min(timezone.now(), cap)
            instance.save(update_fields=['end_time'])
            response.data['end_time'] = instance.end_time
            response.data['duration'] = int(
                (instance.end_time - instance.start_time).total_seconds())
        # Notify client (self), in their own language (BACKEND_BUGS B-030).
        from .services import in_recipients_language, routine_name_for
        client = instance.user
        send_notification(
            user=client,
            notif_type="session_completed",
            message=in_recipients_language(client, lambda: _(
                "Well done! You finished a workout on %(routine)s.")
                % {'routine': routine_name_for(instance.routine, client)}),
            related_object=instance
        )
        # Notify the trainer this client actually has an APPROVED relation with.
        # Reading `assigned_trainer` would tell a dismissed trainer what their former
        # client trained today, which is the client's data, not theirs.
        from users.models import TrainerClientRelation
        for trainer in (t.trainer for t in TrainerClientRelation.objects
                        .filter(client=instance.user, status='approved')
                        .select_related('trainer')):
            # The client's name, not their username, in the trainer's language.
            send_notification(
                user=trainer,
                notif_type="session_completed",
                message=in_recipients_language(trainer, lambda: _(
                    "%(name)s finished a workout on %(routine)s.")
                    % {'name': client.full_name or client.username,
                       'routine': routine_name_for(instance.routine, trainer)}),
                related_object=instance
            )

        # Milestones. Deferred to on_commit so a rolled-back completion cannot
        # congratulate anyone, and pushed to the worker because computing a streak
        # walks backwards day by day through the activity log.
        from django.db import transaction

        _user_id = instance.user_id
        transaction.on_commit(lambda: _queue_milestones(_user_id))


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
        # Validated against the three the view actually implements. Anything else fell
        # through to the week branch AND was interpolated into a response key, so
        # `?period=zzz` answered 200 with a field literally named `zzz_volume`: the
        # caller chose the shape of our response, and a client asking for a period we do
        # not support was told nothing.
        period = request.query_params.get('period', 'week')
        if period not in ('week', 'month', 'year'):
            return Response(
                {'error': _('period must be one of: week, month, year.')},
                status=status.HTTP_400_BAD_REQUEST)
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

        # Calendar days, not a datetime subtracted from now. `date__gte` compares a
        # DateField against a datetime, which truncates: a 7-day window starting at
        # this instant covers EIGHT calendar dates, so a client training daily scored
        # 8/7 and the consistency figure read 114%.
        today = timezone.localdate()

        # Determine time ranges
        if period == 'month':
            days_in_period = 30
        elif period == 'year':
            days_in_period = 365
        else:  # week
            days_in_period = 7
        start_date = today - timedelta(days=days_in_period - 1)
        prev_start = start_date - timedelta(days=days_in_period)

        # Current Period Data
        setlogs = ExerciseSetLog.objects.filter(
            user_exercise_progress__user=user,
            date__gte=start_date
        )
        
        total_volume = setlogs.aggregate(
            total=Sum(F('weight') * F('reps'))
        )['total'] or 0
        
        # Bounded by the window, so it can never exceed it.
        days_trained = min(setlogs.values('date').distinct().count(), days_in_period)
        
        # Personal records set in this period: exercises whose heaviest set in the window
        # beats every earlier set of that exercise. A first-ever log is where a record is
        # measured from, not a record. This was the literal 0 (BACKEND_BUGS B-013).
        prs = _records_in_window(user, start_date)

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
        routine_id = _int_or_none(request.query_params.get('routine_id'))
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
        # Grouped and counted by the database. This walked every RoutineProgress row
        # the caller could see and tallied it in Python — a trainer with twenty
        # clients on thirty-day routines across a year loaded roughly 200,000 rows
        # into the worker to return at most 500 summaries. The `[:500]` cap bounded
        # the response, not the query.
        LIMIT = 500
        # Counted over DAYS of the routine, not over rows of this table. There are two
        # keying conventions in `RoutineProgress` and they collide: the assignment
        # receiver scaffolds one row per day all stamped with `routine.start_date`, while
        # the completion path writes at the ACTUAL training date, and
        # `(user, routine, day, date)` is unique — so every scaffold except the one whose
        # date happened to coincide is stranded as `not_started` for ever. Counting rows
        # therefore gave a client who trained perfectly on all 7 days of a 7-day routine
        # 7 completed out of 13 rows: 53.85%, permanently, with no amount of training
        # able to reach 100.
        #
        # `days` is now the number of distinct DAYS attempted and `completed` the number
        # of distinct days with a completed row, which is the question the field name
        # asks and is immune to how many rows a day accumulated.
        grouped = (qs.values('routine_id', 'user_id')
                     .annotate(days=Count('day', distinct=True),
                               completed=Count('day', distinct=True,
                                               filter=Q(status='completed')))
                     .order_by('routine_id', 'user_id'))
        total = grouped.count()
        rows = [{
            'routine_id': row['routine_id'],
            'user_id': row['user_id'],
            'days': row['days'],
            'completed': row['completed'],
            'completion_rate': round(100 * row['completed'] / row['days'], 2) if row['days'] else 0,
        } for row in grouped[:LIMIT]]
        return Response({
            'count': total,
            'next': None,
            'previous': None,
            'results': rows,
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
        if period not in ('day', 'week', 'month'):
            return Response(
                {'error': _('period must be one of: day, week, month.')},
                status=status.HTTP_400_BAD_REQUEST)
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
            ids = accessible_user_ids(request.user)
            clients = CustomUser.objects.filter(id__in=ids or [])
        else:
            clients = CustomUser.objects.filter(user_type='client')
        
        # Prefetch related data to avoid N+1 queries
        # `exercise_progress__set_logs` was prefetched here and never read: the
        # aggregates below run their own queries. For twenty clients with a year of
        # training that was ~36k progress rows and ~110k set logs pulled into the
        # worker to produce a page of summaries.
        clients = clients.select_related('assigned_trainer').prefetch_related(
            'routine_progress'
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
        
        # Streaks are CONSECUTIVE CALENDAR DAYS. This grouped by `day`, which is the
        # ordinal position inside a routine (1..N), not a date: `day` repeats every
        # time a routine cycles, so a client training daily for a month scored a
        # streak of one to three. The analytics endpoint beside this one already
        # counted dates correctly and disagreed with it.
        streak_lookup = {}
        streak_rows = (RoutineProgress.objects
                       .filter(user__in=clients, status='completed')
                       .values_list('user_id', 'date')
                       .order_by('user_id', 'date')
                       .distinct())
        previous_user = previous_date = None
        run = best = 0
        for user_id, day_date in streak_rows:
            if user_id != previous_user:
                if previous_user is not None:
                    streak_lookup[previous_user] = best
                previous_user, previous_date = user_id, day_date
                run = best = 1
                continue
            run = run + 1 if (day_date - previous_date).days == 1 else 1
            best = max(best, run)
            previous_date = day_date
        if previous_user is not None:
            streak_lookup[previous_user] = best
        
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
        # `copy` and `generate` are POSTs that are read-only with respect to the
        # source template: one creates a new template owned by the caller, the other
        # a routine for the caller's client. Using a public template is the documented
        # workflow, and requiring ownership refused it.
        if getattr(view, 'action', None) in ('copy', 'generate'):
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
        """POST /api/routine/templates/{id}/generate/ — a private routine per client.

        The owner's model: the template is reused for as many clients as the trainer
        likes, and every client gets their OWN routine — a copy of the values, never a
        link. Editing the template later, or one client's copy, changes nobody else's.
        Trainers only, on their own template or a public one, for their approved clients.

        Body:
          client_id       one approved client (int). Answer: 201, the routine.
          client_ids      several (a list of 1-50 ints) instead of client_id. Every id is
                          checked first and nothing is written if one is refused.
                          Answer: 201 `{"routines": [...]}`, one per client, in the order
                          given.
          assign          default true. `false` writes ONE unassigned routine (a draft):
                          no client needed, no notification, no payment. The trainer
                          personalises it with `routine-exercises/` (add, remove, swap,
                          per-day sets/reps/weight/notes) and then calls
                          `routines/{id}/assign_to_client/`.
          customizations  optional `{exercise_id: {sets 1-100, reps 1-1000,
                          rest_time 0-3600, weight 0-1000}}`, applied to that exercise on
                          every day it appears.
          name            optional; replaces the template's name on the copy.
          days            optional 1-365 (default: the template's days; a longer routine
                          repeats the template's days in order). Or start_date and
                          end_date (YYYY-MM-DD) to derive it.

        Assigning also settles the coaching's first month, as assign_to_client does: a
        program delivered from a template starts the paid month like any other.

        Codes: 400 bad_request (client_id missing, dates or days malformed), 400
        `bad_customization` (with `key`), 400 `invalid_client_ids`, 400
        `template_empty` (no exercises), 400 `client_ids_need_assign`, 400
        `generate_too_large` (clients x days above `max_routine_days`, 1000), 403
        permission_denied (not your client), 403 `not_your_client` (with
        `client_ids`, the list form), 404 not_found (no such client). Identical on
        every build; no price and no purchase is involved.
        """
        from .assignment import (
            MAX_CLIENTS_PER_GENERATE,
            MAX_ROUTINE_DAYS_PER_GENERATE,
            BadCustomization,
            assign_routine,
            clean_customizations,
            routine_from_template,
        )
        from .permissions import can_access_user_data
        from .serializers import RoutineSerializer

        template = self.get_object()
        assign = str(request.data.get('assign', 'true')).strip().lower() not in ('false', '0', 'no')

        raw_ids = request.data.get('client_ids')
        clients = []
        if raw_ids is not None:
            if not assign:
                return Response({'error': _('A draft is written for no one; assign it '
                                            'afterwards with assign_to_client.'),
                                 'code': 'client_ids_need_assign'},
                                status=status.HTTP_400_BAD_REQUEST)
            ids = ([_int_or_none(v) for v in raw_ids] if isinstance(raw_ids, list) else [None])
            if (not ids or None in ids or len(set(ids)) != len(ids)
                    or len(ids) > MAX_CLIENTS_PER_GENERATE):
                return Response({'error': _('client_ids must be a list of 1 to %(n)s '
                                            'different client ids.')
                                 % {'n': MAX_CLIENTS_PER_GENERATE},
                                 'code': 'invalid_client_ids'},
                                status=status.HTTP_400_BAD_REQUEST)
            found = {c.pk: c for c in CustomUser.objects.filter(pk__in=ids, user_type='client')}
            refused = [i for i in ids if i not in found
                       or not can_access_user_data(request.user, i)]
            if refused or not request.user.is_trainer:
                return Response({'error': _('You can only assign routines to your own '
                                            'clients.'),
                                 'code': 'not_your_client',
                                 'client_ids': refused or ids},
                                status=status.HTTP_403_FORBIDDEN)
            clients = [found[i] for i in ids]
        elif assign or request.data.get('client_id') not in (None, ''):
            client_id = _int_or_none(request.data.get('client_id'))
            if not client_id:
                return Response({'detail': _('client_id required')}, status=400)
            # A client, not whoever has that id: a trainer's own id passed
            # `can_access_user_data` (it admits the caller) and wrote a routine assigned
            # to the trainer.
            client = CustomUser.objects.filter(pk=client_id, user_type='client').first()
            if client is None:
                return Response({'detail': _('Client not found')}, status=404)
            # Only a trainer, and only for an approved client of theirs. This read
            # `client.assigned_trainer_id`, the denormalised display pointer that
            # routine/permissions.py opens by forbidding as an access test — it was the
            # pointer that once granted access without consent.
            if not (request.user.is_trainer and can_access_user_data(request.user, client.pk)):
                return Response({'detail': _('Permission denied. You can only assign routines to your own clients.')}, status=403)
            if assign:
                clients = [client]

        # Parsed, not assumed, and bounded. `strptime` on "2026-13-45" raised into the
        # generic handler as a 500, and `days: 500000` built half a million exercise
        # rows in memory before bulk_create was ever reached. Dates are parsed whenever
        # they are sent: with `days` present, a raw start_date string used to reach the
        # model unparsed.
        MAX_GENERATED_DAYS = 365
        start_date = request.data.get('start_date') or None
        end_date = request.data.get('end_date') or None
        d1 = parse_date(str(start_date)) if start_date else None
        d2 = parse_date(str(end_date)) if end_date else None
        if (start_date and not d1) or (end_date and not d2):
            return Response({'error': 'start_date and end_date must be YYYY-MM-DD'},
                            status=status.HTTP_400_BAD_REQUEST)
        if d1 and d2 and d2 < d1:
            return Response({'error': 'end_date is before start_date'},
                            status=status.HTTP_400_BAD_REQUEST)
        routine_days = request.data.get('days')
        if routine_days not in (None, ''):
            try:
                routine_days = int(routine_days)
            except (TypeError, ValueError):
                return Response({'error': 'days must be a whole number'},
                                status=status.HTTP_400_BAD_REQUEST)
        elif d1 and d2:
            routine_days = (d2 - d1).days + 1
        else:
            routine_days = template.days
        if not 1 <= int(routine_days) <= MAX_GENERATED_DAYS:
            return Response({'error': f'days must be between 1 and {MAX_GENERATED_DAYS}'},
                            status=status.HTTP_400_BAD_REQUEST)
        routine_days = int(routine_days)
        if len(clients) > 1 and len(clients) * routine_days > MAX_ROUTINE_DAYS_PER_GENERATE:
            return Response({'error': _('Too much at once: split the clients over more than '
                                        'one request.'),
                             'code': 'generate_too_large',
                             'max_routine_days': MAX_ROUTINE_DAYS_PER_GENERATE},
                            status=status.HTTP_400_BAD_REQUEST)

        try:
            customizations = clean_customizations(request.data.get('customizations'))
        except BadCustomization as exc:
            return Response({'error': exc.message, 'code': 'bad_customization',
                             'key': exc.key}, status=status.HTTP_400_BAD_REQUEST)
        if not template.routinetemplateexercise_set.exists():
            return Response({'error': _('This template has no exercises yet.'),
                             'code': 'template_empty'}, status=status.HTTP_400_BAD_REQUEST)
        name = str(request.data.get('name') or '').strip()[:255] or None

        def write():
            return routine_from_template(
                template, created_by=request.user, days=routine_days,
                customizations=customizations, name=name, start_date=d1, end_date=d2)

        context = {'request': request}
        if not clients:
            draft = write()
            return Response(RoutineSerializer(draft, context=context).data, status=201)

        # Rows first, then the client: the m2m receiver counts the rows into each day's
        # progress. One fresh routine per client, never shared (`assign_routine`).
        from .services import notify_assignment
        routines = []
        for client in clients:
            routine = write()
            assign_routine(routine, client, request.user)
            notify_assignment(client, routine)
            routines.append(routine)

        if raw_ids is None:
            return Response(RoutineSerializer(routines[0], context=context).data, status=201)
        return Response({'routines': RoutineSerializer(routines, many=True, context=context).data},
                        status=201)

    @action(detail=True, methods=['post'], permission_classes=[IsTrainerOrReadOnly])
    def copy(self, request, pk=None):
        """POST /api/routine/templates/{id}/copy/ — a private copy of a template.

        Your own template (to make a variant), or another trainer's PUBLIC one. 400 for
        someone else's private template. 201 with the copy: private, in your library,
        both languages of the name prefixed "Copy of".

        Copying your own template answered 400 "Cannot copy your own template", so a
        trainer could not make a variant of their own program to reuse.
        """
        from django.conf import settings
        from django.utils.translation import override

        template = self.get_object()
        if not template.is_public and template.created_by_id != request.user.id:
            return Response({'detail': _('Can only copy public templates')}, status=400)

        names = {}
        for language, _label in settings.LANGUAGES:
            with override(language):
                original = getattr(template, f'name_{language}', None) or template.name
                names[f'name_{language}'] = (_('Copy of %(name)s') % {'name': original})[:255]
        with transaction.atomic():
            new_template = RoutineTemplate.objects.create(
                **names,
                **{f'description_{language}': getattr(template, f'description_{language}', '')
                   or '' for language, _label in settings.LANGUAGES},
                goal=template.goal,
                days=template.days,
                is_public=False,  # Always private when copied
                created_by=request.user,
            )
            RoutineTemplateExercise.objects.bulk_create([
                RoutineTemplateExercise(
                    template=new_template, exercise_id=t_ex.exercise_id, sets=t_ex.sets,
                    reps=t_ex.reps, rest_time=t_ex.rest_time, day=t_ex.day, order=t_ex.order)
                for t_ex in template.routinetemplateexercise_set.all()])

        return Response(RoutineTemplateSerializer(new_template, context={'request': request}).data,
                        status=201)

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


class ProgressExists(APIException):
    """400 `progress_exists`: `{detail, error, code, id}` — the row already written for
    this exercise and date."""
    status_code = status.HTTP_400_BAD_REQUEST
    default_code = 'progress_exists'

    def __init__(self, existing_id):
        super().__init__({'detail': _('A progress record for this user, exercise, and '
                                      'date already exists.')})
        self.detail['id'] = existing_id

    def get_codes(self):
        return self.default_code


class UserExerciseProgressViewSet(viewsets.ModelViewSet):
    """
    API endpoint for managing UserExerciseProgress objects (per-user, per-exercise progress).
    Allows trainers, admins, and clients to create, view, and update their own progress records.
    A client's create answers 403 `coaching_frozen` / `coaching_unpaid` / `coaching_ended` when every routine
    of theirs holding the exercise comes from a coaching that is unpaid or over.
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
            ids = accessible_user_ids(user)
            return self.queryset if ids is None else self.queryset.filter(user_id__in=ids)
        else:
            return self.queryset.filter(user=user)

    def perform_create(self, serializer):
        """400 `progress_exists` with the existing row's `id` when this exercise already
        has a row for that date: the app adopts it (BACKEND_BUGS B-031). It answered
        `validation_error`, the code of every real validation failure."""
        exercise = serializer.validated_data.get('exercise')
        _require_can_train_exercise(self.request.user, getattr(exercise, 'pk', None),
                                    serializer.validated_data.get('date'))
        date = serializer.validated_data.get('date') or timezone.localdate()
        existing = UserExerciseProgress.objects.filter(
            user=self.request.user, exercise=exercise, date=date).only('pk').first()
        if existing is not None:
            raise ProgressExists(existing.pk)
        try:
            with transaction.atomic():
                serializer.save(user=self.request.user)
        except IntegrityError as e:
            if 'unique constraint' in str(e).lower():
                existing = UserExerciseProgress.objects.filter(
                    user=self.request.user, exercise=exercise, date=date).only('pk').first()
                raise ProgressExists(existing.pk if existing else None)
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
        # With the request, so `exercise.image` is absolute as it is everywhere else
        # (BACKEND_BUGS B-032): without it the same exercise came back as a relative
        # path here and a full URL from `routines/`.
        serializer = UserDailySummarySerializer(queryset, many=True, context={'request': request})
        
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
        
        403 `coaching_frozen` / `coaching_unpaid` / `coaching_ended` (with `trainer_id`): the coaching this
        routine came from is unpaid or over — readable, not trainable (the owner).
        """
        user = request.user
        routine_id = _int_or_none(request.data.get('routine_id'))
        day = _int_or_none(request.data.get('day'))
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

        _require_can_train(user, routine, date)

        exercises = RoutineExercise.objects.filter(routine=routine, day=day)
        if not exercises.exists():
            return Response(
                {'error': _('Day %(day)s is not part of this routine.') % {'day': day}},
                status=status.HTTP_400_BAD_REQUEST)
        results = []
        errors = []
        written = []
        from .models import refresh_routine_progress, suspend_progress_recalc
        for rex in exercises:
            try:
                # Savepoint: same reason as the set-log loop above — this one keeps
                # going after a failure, so the failed write must not take the
                # surrounding transaction with it. The per-row recalculation is
                # suspended: with no set log naming a session it walked EVERY routine
                # holding the exercise (BACKEND_BUGS B-011); it runs once below, scoped
                # to the routine and day this request is about.
                with transaction.atomic(), suspend_progress_recalc():
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
                written.append(obj)
            except IntegrityError as e:
                if 'unique constraint' in str(e).lower():
                    errors.append({'exercise': rex.exercise.name, 'error': 'Duplicate progress for this user, exercise, and date.'})
                else:
                    logger.exception("bulk progress write failed for %s", rex.exercise_id)
                    errors.append({'exercise': rex.exercise.name,
                                   'error': _('Could not record progress for this exercise.')})
        if written:
            # One pass decides the whole day from every exercise's progress.
            refresh_routine_progress(written[-1], routine_scope=routine, day_scope=day)
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

    # Digits only. `[^/.]+` matched anything, so /client-progress/abc/ reached the
    # ORM as a filter on an integer column and raised ValueError — a 500 on a URL any
    # crawler can construct.
    @action(detail=False, methods=['get'], url_path='(?P<client_id>[0-9]+)')
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
                     # Bounded by TIME, not by a slice. This was ordered but unbounded,
                     # so a client with three hundred sessions materialised all of them
                     # and every set log for a view whose statistics only look back seven
                     # days. The slice that replaced it made the endpoint a permanent 500:
                     # Django applies the NESTED prefetch by filtering the outer queryset,
                     # and "Cannot filter a query once a slice has been taken" fired on
                     # every request, for every trainer, whether or not the client had
                     # trained. A window is the bound this view actually wants — the
                     # weekly statistics use seven days and the activity list shows a
                     # month — and unlike a slice it composes with the nested prefetch.
                     'workout_sessions',
                     queryset=WorkoutSession.objects.filter(
                        status='completed',
                        end_time__gte=timezone.now() - timedelta(days=RECENT_ACTIVITY_DAYS),
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
        # Same unread prefetch as the dashboard above, and the client list was
        # unbounded. The serializer issues its own queries per client, so the only
        # thing this bought was memory.
        ids = accessible_user_ids(user)
        clients = (CustomUser.objects
                   .filter(id__in=ids or [], user_type='client')
                   .select_related('assigned_trainer')[:100])
        
        # One collection pass for the whole roster. Serialising each client on its own
        # asked the same handful of questions per client, so a full roster cost ~509
        # queries on one page load.
        clients = list(clients)
        activity = RecentActivitySerializer.collect(clients)
        recent_activity = [
            RecentActivitySerializer(client, context={"activity": activity}).data
            for client in clients
        ]
        
        return Response({
            'trainer_id': user.id,
            'trainer_name': user.username,
            'client_count': len(recent_activity),
            'recent_activity': recent_activity
        })