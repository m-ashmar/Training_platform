from rest_framework import viewsets, status, permissions, generics, serializers
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.pagination import PageNumberPagination
from training_platform.pagination import StandardPagination
from django.db import models, IntegrityError, transaction
from django.db.models import Sum, Avg, Count, F, Q, Prefetch
from django.utils import timezone
from django.utils.dateparse import parse_date
from django.utils.translation import get_language, gettext as _
from datetime import datetime, timedelta, date
from .models import (
    Routine, Exercise, RoutineExercise, RoutineProgress, ExerciseSetLog, WorkoutSession,
    RoutineTemplate, RoutineTemplateExercise, UserExerciseProgress, ExerciseMedia
)
from .models import EQUIPMENT_CHOICES
from .serializers import (
    ExerciseSerializer, ExerciseCreateWithImageSerializer, RoutineSerializer, 
    RoutineExerciseSerializer, UserExerciseProgressSerializer, RoutineProgressSerializer,
    UserRoutineSerializer, ExerciseSetLogSerializer, TrainerRoutineSerializer,
    WorkoutSessionSerializer, RoutineTemplateSerializer,
    ExerciseMediaSerializer,
    RecentActivitySerializer,
)
# NOT imported here any more:
#   `RoutineTemplateExerciseSerializer` — this module defined a SECOND class of that
#   name further down, which simply rebound the import (SIDE_BUGS S-029). The one in
#   this file is now `RoutineTemplateExerciseListSerializer`; the imported one is the
#   nested writer `RoutineTemplateSerializer` uses and belongs only to serializers.py.
#   `DetailedClientProgressSerializer` — imported and never used, and deleted with this
#   line (SIDE_BUGS S-037).
#   `WorkoutSessionDetailSerializer`, `ClientProfileViewSerializer` — the same, on the
#   adjacent names (SIDE_BUGS S-055). `ClientProfileViewSerializer` is still used, by
#   `users/views.py`, which imports it from serializers.py itself.
from .permissions import (
    IsAdminOrOwnerOrReadOnly, 
    IsTrainerOrAdmin,
    IsTrainerOrAdminForAssignment,
    IsSetLogCreatorOrTrainerOrAdmin,
    IsRoutineOwnerOrAssigned,
    IsClientOrAssignedTrainer,  # Added for RoutineProgressViewSet
    # The one predicate for "this trainer may see this client": an APPROVED relation.
    accessible_user_ids,
    # The one predicate for "this caller may read this exercise".
    visible_exercises_for,
)
# Resolves a whole page's exercise media in one go: whose demonstration each viewer
# sees (mediakit/resolution.py). Without the mixin a nested ExerciseSerializer asks
# per row.
from .media_context import ExerciseMediaMixin
import functools
import logging
from .services import send_notification
from django.utils import timezone
from datetime import timedelta

# How far back the trainer's client-progress screen looks. The weekly statistics
# use the last seven days; the activity list shows the month around them.
RECENT_ACTIVITY_DAYS = 30
from rest_framework.permissions import IsAuthenticated
from django_filters.rest_framework import DjangoFilterBackend
import django_filters.rest_framework as django_filters
from django import forms as django_forms
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
# One offline sync carries at most this many sets. A week away from signal is a few
# dozen; 200 is generous and still a bounded amount of work for one request, which is
# what the cap is for — `set-logs/sync/` writes a row and a progress row per entry.
MAX_SYNC_SETS = 200
# The highest set number a sync will accept. The same spirit as MAX_SETS_PER_REQUEST:
# nobody performs a hundredth set of one exercise in a day, and an unbounded number
# lets one row claim a prescription of any size.
MAX_SET_NUMBER = 100


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
    time (in seconds), including days with 0 duration, plus `adherence`: the same week
    measured against the coach's plan — sets planned, logged and missed, days with
    nothing missing, the perfect-day streak and the day that broke it
    (see `analytics/adherence/` for the full shape) — and `strength`: the headline for
    the exercise that moved most, with its projection when there is one, plus how many
    exercises are rising out of how many tracked (see `analytics/strength/`).

    Implementation details:
    - The payload itself lives in `routine/recent.py`, because `GET /api/v1/home`
      shows the same week and must not compute a second, disagreeing version of it
    - Duration is aggregated at DB level via TruncDate + Sum (no Python loops)
    - Day boundaries are computed in the server's local timezone (timezone-aware)
    - Only completed sessions with a valid end_time are counted
    - Results are cached per-user AND per-language in private_cache (Redis DB3) for 120s
    - Cache is busted on WorkoutSession completion and on a day closing, via post_save
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        from .recent import cached_recent_progress

        return Response(cached_recent_progress(request.user))


class InjuryMapView(APIView):
    """GET /api/routine/injury-map/ — which exercises each injury argues with
    (BACKEND_BUGS B-105; TrainerDayEditor's amber chip).

    200 `{version, areas: [{code, muscles}]}`: every `injury_areas` code a client can
    hold (users `INJURY_AREAS`, in that order), each with the `Exercise.target_muscle`
    keys whose exercises load that joint directly. An exercise conflicts with a client
    when its `target_muscle` is in the list of one of the client's `injury_areas`.
    `other` (a condition described in words) maps to no muscle. The map is narrow on
    purpose — a shoulder flags the overhead press, not the bench press beside it — and
    the reasons are in `routine/injuries.py`. `version` rises whenever the map changes:
    keep a copy and read it again when it does.

    A warning for the trainer, never a refusal: no write consults it. No lighter-load
    number is served; the trainer chooses the load.

    Any signed-in account (a client's own screens may show the same chip); 401
    `not_authenticated` signed out. Identical on every build.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        from .injuries import injury_map

        return Response(injury_map())


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


def _date_or_none(value):
    """A `YYYY-MM-DD` date from a request, or None if it is not one.

    `django.utils.dateparse.parse_date` answers None only for a string that is not
    SHAPED like a date; one that is shaped like a date and is not one (`2026-13-45`,
    `2026-02-30`) raises `ValueError`, which is not a DRF exception — so the `bad_date`
    refusals below were a 500 for exactly the typo they exist to catch, and on
    `set-logs/sync/` one such row failed the whole batch.
    """
    try:
        return parse_date(str(value))
    except (TypeError, ValueError):
        return None


class _WholeNumberFilter(django_filters.NumberFilter):
    """A whole number or a 400. `NumberFilter` parses a Decimal, and an integer column
    takes `int()` of it — so `?routine=1.5` would have read routine 1."""
    field_class = django_forms.IntegerField


class ExerciseFilter(django_filters.FilterSet):
    """`?target_muscle=<key>&exclude=<id>` on the exercise list (BACKEND_BUGS B-158).

    Both narrow the rows `visible_exercises_for` already allows, so neither can widen
    them. `target_muscle` is one of `Exercise.target_muscle`'s stored keys (`Biceps`,
    `Front Quads`, ...); anything else answers 400 `invalid_choice` on the field.
    `exclude` is a plain number, never a lookup of the exercise row (the S-119 oracle):
    an id that does not exist excludes nothing.
    """
    target_muscle = django_filters.ChoiceFilter(
        choices=Exercise._meta.get_field('target_muscle').choices)
    exclude = _WholeNumberFilter(field_name='pk', exclude=True)

    class Meta:
        model = Exercise
        fields = ['target_muscle', 'exclude']


def exercise_owner(user):
    """Who an exercise `user` creates belongs to: a trainer's is their own; an admin's
    is the platform catalogue's — no owner, so `is_global`, visible to everyone and
    editable by an admin — exactly what the console makes (the fix run's S-166,
    2026-11-24).

    Both create routes let an admin in ("a trainer or an admin") and then saved the admin
    as the owner, which `Exercise.clean` refuses (`trainer_required`: only a trainer owns
    an exercise): `create-with-image/` answered 500 `exercise_create_failed`."""
    if not getattr(user, 'is_trainer', False) and getattr(user, 'is_admin', False):
        return None
    return user


class ExerciseViewSet(ExerciseMediaMixin, viewsets.ModelViewSet):

    """ViewSet for managing exercises.

    A POST/PUT/PATCH carrying an `image` answers 503 `upload_busy` while the server is
    already decoding as many images as its memory holds (`training_platform/
    image_budget.py`): nothing was saved; send it again after `Retry-After` seconds
    (`retry_after_seconds` in the body).

    List filters (BACKEND_BUGS B-158): `?target_muscle=<key>` — only that muscle's
    exercises (400 `validation_error`, `field_errors.target_muscle[0].code`
    `invalid_choice`, for a value that is not one of the keys); `?exclude=<id>` — leave
    that exercise out (the one being replaced). Both only narrow what the caller may
    already read.
    """
    queryset = Exercise.objects.all()
    serializer_class = ExerciseSerializer
    permission_classes = [IsAdminOrOwnerOrReadOnly]
    pagination_class = StandardResultsSetPagination
    filter_backends = [DjangoFilterBackend]
    filterset_class = ExerciseFilter

    def get_queryset(self):
        """Filter exercises based on user role with optimized queries.

        The rule itself is `routine.permissions.visible_exercises_for` — admin sees
        all, a trainer sees own + the shared catalogue, a client sees the catalogue +
        their approved trainers' + whatever a routine assigned to them uses. It used
        to be written out here and nowhere else, which is why `ExerciseAddMediaView`,
        resolving ids on its own, had no visibility rule at all.
        """
        # Optimize: prefetch media to avoid N+1 in serializer
        base_qs = self.queryset.prefetch_related('media').select_related('created_by')
        return visible_exercises_for(self.request.user, base_qs)

    def perform_create(self, serializer):
        """The exercise is the creating trainer's own; an admin's is the platform
        catalogue's (`exercise_owner`)."""
        serializer.save(created_by=exercise_owner(self.request.user))

    def destroy(self, request, *args, **kwargs):
        """Refuse to delete an exercise clients have logged work against.

        This guard was written for Routine and attached here, where it could never
        fire: nothing PROTECTs an Exercise. `UserExerciseProgress.exercise` and
        `ExerciseSetLog` both CASCADE, so deleting one exercise silently erased every
        client's history for it and changed their past volume and streaks. Refuse, and
        offer the retire path instead.

        409 `exercise_has_history` — someone has logged work against it; retire it with
        `is_active=false` instead (the owner, 2026-09-24: every refusal names its code).

        409 `exercise_in_use` (additive, the fix run's G2, 2026-11-24), with `routines`
        and `templates` — how many of each still name it — when nobody has logged work
        against it but a routine or a template still uses it. `RoutineExercise` and
        `RoutineTemplateExercise` CASCADE too, so the delete took the exercise out of
        every routine that used it — clients' assigned programmes included — and out of
        every template, and said nothing. Take it out of those first, or retire it with
        `PATCH exercises/{id}/ {"is_active": false}` (G1): the routines and templates
        that hold it keep it, and it can no longer be added to one (`exercise_retired`).
        Checked after `exercise_has_history`, which keeps its answer.
        """
        from .models import RoutineExercise, RoutineTemplateExercise

        exercise = self.get_object()
        if exercise.progress.exists():
            return Response(
                {'error': _('Clients have recorded work against this exercise, so it '
                            'cannot be deleted. Set is_active=false to retire it.'),
                 'code': 'exercise_has_history'},
                status=status.HTTP_409_CONFLICT,
            )
        routines = (RoutineExercise.objects.filter(exercise=exercise)
                    .values('routine_id').distinct().count())
        templates = (RoutineTemplateExercise.objects.filter(exercise=exercise)
                     .values('template_id').distinct().count())
        if routines or templates:
            return Response(
                {'error': _('This exercise is still part of a routine or a template, so '
                            'it cannot be deleted. Remove it from them first, or set '
                            'is_active=false to retire it.'),
                 'code': 'exercise_in_use', 'routines': routines, 'templates': templates},
                status=status.HTTP_409_CONFLICT,
            )
        return super().destroy(request, *args, **kwargs)


def _refused_upload(error, which=None):
    """400 for an upload the guard refused, carrying the guard's OWN `code`.

    `process_uploaded_image` says exactly what was wrong — `file_too_large`,
    `invalid_mime_type`, `unreadable_image`, `unsupported_format`,
    `image_dimensions_too_large`, `image_too_many_pixels`, ... — and these routes kept
    only the sentence, so the envelope filled in `bad_request` and the app could not
    tell a photo that is too big from a file that is not a photo (SIDE_BUGS S-118). The
    profile picture route already passed it on; `invalid_image` is its fallback too.
    `which` names the photo in a batch (`media_photos`), inside the translated sentence.
    """
    reason = error.messages[0] if getattr(error, 'messages', None) else str(error)
    if which is not None:
        reason = _('Media photo %(number)d: %(reason)s') % {'number': which, 'reason': reason}
    return Response({'error': reason, 'code': getattr(error, 'code', None) or 'invalid_image'},
                    status=status.HTTP_400_BAD_REQUEST)


def _discard_stored(names):
    """Remove files this request stored before it failed. Best effort: the answer is
    already a failure, and a storage outage must not turn it into a different one."""
    from django.core.files.storage import default_storage

    for name in names:
        try:
            default_storage.delete(name)
        except Exception:  # noqa: BLE001 — logged, never raised
            logger.warning("could not remove %s after a failed exercise create", name,
                           exc_info=True)


class ExerciseCreateWithImageView(APIView):
    """
    Enhanced endpoint for creating exercises with optional image upload and media content.
    
    Accepts multipart/form-data with exercise data and optional media files/URLs.

    **Who may call it.** A trainer or an admin — `IsTrainerOrAdmin`, the rule
    `POST exercises/` already applied. Anyone else answers 403 `permission_denied`
    before a byte is read or stored: this route took any signed-in account, so a client
    or an agent reached the form (SIDE_BUGS S-117).
    
    Supported media types:
    - Main exercise image (demonstration image)
    - Additional photos (uploaded files)  
    - Videos (URLs to video content)
    - Text descriptions (additional text content)
    
    Request format:
    - name (required, or `name_ar` / `name_en` in its place): Exercise name
    - description (required): Exercise description
    - target_muscle (required): Target muscle group — a missing one of these three
      answers 400 `missing_fields` with `fields: [<name>]` and `field` naming it (the
      owner, 2026-09-24; `missing_fields`, the code every other route answers for a
      missing required field, since BACKEND_BUGS B-092 — it was `field_required`,
      minted that day and never shipped)
    - name_ar, name_en (optional, B-107): the Arabic and English names. Either one
      answers for `name`; `name` alone writes the reader's language (Accept-Language),
      and beside that language's own column the column wins. The answer carries both.
    - equipment (optional, B-107): `barbell`, `dumbbell`, `cable`, `machine` or
      `bodyweight`; any other value answers 400 `equipment_invalid` with
      `field: "equipment"`
    - difficulty_level (optional): beginner/intermediate/advanced/expert
    - image (optional): Main demonstration image file
    - media_photos (optional): Additional photo files (multiple)
    - media_videos (optional): Video URLs (comma-separated). Each must be on an
      allowed host (`settings.EXERCISE_MEDIA_ALLOWED_HOSTS`) — 400
      `media_host_not_allowed` otherwise, the same rule
      `exercises/{id}/add-media/` applies. 400 `media_url_invalid` for a
      malformed one.
    - media_texts (optional): Additional text content (separated by `||`)

    **A refused upload** (`image` or any of `media_photos`) answers 400 with the upload
    guard's own `code` — `file_too_large`, `invalid_mime_type`, `unreadable_image`,
    `unsupported_format`, `image_dimensions_too_large`, `image_too_many_pixels`, and
    the guard's other refusals; `invalid_image` when it names none (SIDE_BUGS S-118).
    For a photo the sentence says which one.

    **503 `upload_busy`**: the server is already decoding as many images as its memory
    holds (`training_platform/image_budget.py`). Nothing was created or stored; send
    the same request again after `Retry-After` seconds (`retry_after_seconds` in the
    body).

    **Extra photos are stored by NAME.** Each is saved under
    `exercise_media/<exercise id>/` and its `ExerciseMedia` row keeps the storage name;
    `media[].content` and `thumbnail` are a URL made when the exercise is SERVED, signed
    and expiring like every media URL — read it again, never store it. The row used to
    keep the signed URL itself, which on object storage died an hour after the upload
    (SIDE_BUGS S-070). Removing the media row or the exercise removes the file.

    **Who owns it.** A trainer's exercise is their own; an admin's goes to the platform
    catalogue (no owner, visible to everyone; `is_mine` false), as the console makes one
    (`exercise_owner`, S-166 — it answered 500 `exercise_create_failed`).

    A fault nobody anticipated answers 500 `exercise_create_failed` with a translated
    sentence and nothing else: the exception text used to BE the response body
    (SIDE_BUGS S-036, the same defect closed in the two media views as S-006), which
    hands whoever posted the form a stack trace's worth of internals — file paths,
    storage bucket names, SQL. The traceback goes to the log. And it writes NOTHING:
    the exercise, its media rows and every file this request stored are undone. The
    500 used to be answered from inside the atomic block, which then committed — a
    half-made exercise per failed attempt, and one more per retry (SIDE_BUGS S-057).
    """
    permission_classes = [IsAuthenticated, IsTrainerOrAdmin]
    parser_classes = [MultiPartParser, FormParser]

    # Atomic: these writes were independent, so a failure part-way left the
    # records inconsistent (a half-applied password reset either locks the
    # user out or leaves a consumed token usable).
    @transaction.atomic
    def post(self, request):
        """Create a new exercise with optional image and media content"""
        from .models import ExerciseMedia
        import re

        # Storage names this request wrote, so a failure can take them back out: the
        # rollback undoes the rows, and nothing undoes a file but this list.
        stored = []
        try:
            # Validate required fields. A name in either language answers for `name`
            # (B-107): TrainerNewExercise sends `name_ar` and `name_en`.
            names = {f: str(request.data.get(f) or '').strip()
                     for f in ('name', 'name_ar', 'name_en')}
            required_fields = ['name', 'description', 'target_muscle']
            for field in required_fields:
                present = any(names.values()) if field == 'name' else request.data.get(field)
                if not present:
                    return Response(
                        {'error': _('Missing required field: %(field)s') % {'field': field},
                         'code': 'missing_fields', 'fields': [field], 'field': field},
                        status=status.HTTP_400_BAD_REQUEST
                    )

            equipment = str(request.data.get('equipment') or '').strip() or None
            if equipment is not None and equipment not in dict(EQUIPMENT_CHOICES):
                return Response(
                    {'error': _('Equipment must be one of: %(options)s')
                     % {'options': ', '.join(code for code, _label in EQUIPMENT_CHOICES)},
                     'code': 'equipment_invalid', 'field': 'equipment'},
                    status=status.HTTP_400_BAD_REQUEST)

            # Get the main exercise image
            image_file = request.FILES.get('image')
            
            # Validate main image file if provided
            if image_file:
                # Validate CONTENT (magic bytes), cap pixel dimensions and re-encode.
                # The client Content-Type header was the only previous check and is
                # spoofable — PHP/SVG payloads were accepted and stored as .php/.svg.
                from training_platform.file_security import process_uploaded_image
                from django.core.exceptions import ValidationError as DjangoValidationError

                try:
                    image_file, _img_ext = process_uploaded_image(image_file, max_bytes=5 * 1024 * 1024)
                except DjangoValidationError as e:
                    return _refused_upload(e)

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
                        return _refused_upload(e, which=i + 1)

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
                            {'error': _('Invalid video URL format: %(url)s') % {'url': url},
                             'code': 'media_url_invalid'},
                            status=status.HTTP_400_BAD_REQUEST
                        )
                    # The same host rule `add-media/` applies (S-017). It was written
                    # for that route only, and this one creates exactly the same
                    # `ExerciseMedia` video rows from exactly the same caller-supplied
                    # URLs — so closing one door and not the other closes nothing.
                    if not _media_host_allowed(url):
                        return Response(
                            {'error': _('That link is not on a host this app plays '
                                        'media from. Upload the file instead.'),
                             'code': 'media_host_not_allowed'},
                            status=status.HTTP_400_BAD_REQUEST
                        )

            # Get additional text content
            media_texts = request.data.get('media_texts', '')
            text_contents = []
            if media_texts:
                text_contents = [text.strip() for text in media_texts.split('||') if text.strip()]

            # Create exercise data. Names as `ExerciseSerializer` writes them (B-107):
            # `name` is the reader's language unless that language's own column is
            # sent too; the untranslated `name` column keeps a name either way, the
            # default language's first; and the reader's column is named explicitly so
            # the constructor never copies the other language's name into it.
            from modeltranslation.utils import get_language as model_language

            own = f"name_{model_language()}"
            exercise_data = {
                'description': request.data.get('description'),
                'target_muscle': request.data.get('target_muscle'),
                'difficulty_level': request.data.get('difficulty_level', 'beginner'),
                'equipment': equipment,
            }
            for field in ('name_ar', 'name_en'):
                if names[field]:
                    exercise_data[field] = names[field]
            if names['name'] and own not in exercise_data:
                exercise_data[own] = names['name']
            exercise_data.setdefault(own, None)
            exercise_data['name'] = names['name_en'] or names['name_ar'] or names['name']

            # Create the exercise
            exercise = Exercise.objects.create(
                created_by=exercise_owner(request.user),
                **exercise_data
            )

            # Set main image if provided. Through `FieldFile.save`, with a server-made
            # name carrying the DETECTED format's extension — exactly as
            # `exercises/{id}/image/` stores one. The re-encoded image is a ContentFile
            # with no name, and ASSIGNING it made Django refuse the save ("must have the
            # name attribute specified"), so every request that sent `image` — the
            # point of this route — answered 500 (SIDE_BUGS S-057).
            if image_file:
                exercise.image.save(f"exercise.{_img_ext}", image_file, save=False)
                stored.append(exercise.image.name)
                exercise.save()

            # Create additional media records
            media_created = []
            
            # Handle additional photo uploads
            from django.core.files.storage import default_storage
            for i, (photo_file, photo_ext) in enumerate(validated_photos):
                # A server-made name with the DETECTED format's extension, under a
                # folder of this exercise's own.
                photo_name = f"exercise_media_{exercise.id}_{i}.{photo_ext}"
                photo_path = f"{ExerciseMedia.STORED_PHOTO_PREFIX}{exercise.id}/{photo_name}"
                saved_path = default_storage.save(photo_path, photo_file)
                stored.append(saved_path)

                # The storage NAME, never `default_storage.url(...)`: that URL is signed
                # and expires (an hour on object storage), so the row held a dead link
                # soon after it was written (SIDE_BUGS S-070). The URL is made when the
                # row is served — `ExerciseMedia.content_url`.
                media_record = ExerciseMedia.objects.create(
                    exercise=exercise,
                    media_type='photo',
                    content=saved_path,
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
            # Propagating out of the atomic block rolls the rows back; the files are
            # this function's to take back.
            _discard_stored(stored)
            raise
        except Exception:
            # The exception TEXT used to be the response body (S-036) — the same defect
            # the two media views closed as S-006, in the class that pass did not
            # cover. Logged with the traceback, answered with a code.
            #
            # And undone. Returning a Response from inside `@transaction.atomic` is a
            # NORMAL exit, so the block committed whatever had been written before the
            # fault — the exercise row at least (S-057). Mark it for rollback, and
            # remove the files the rows would have pointed at.
            transaction.set_rollback(True)
            _discard_stored(stored)
            logger.exception("exercise create-with-image failed for user %s",
                             getattr(request.user, 'pk', None))
            return Response(
                {'error': _('Could not create that exercise. Please try again.'),
                 'code': 'exercise_create_failed'},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )


class RoutineViewSet(ExerciseMediaMixin, viewsets.ModelViewSet):
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

    Who a routine is for: `assigned_ids` (BACKEND_BUGS B-152) — user ids, the key to
    match a client on. `assigned_usernames` is the same people by username, and
    `assigned_to` is display text (`"samer (Client)"`), never an id. All three follow the
    caller: the author or an admin sees every assignee, another trainer only their own
    approved clients, a client only themselves.

    A client's own copy of the platform's FREE FIRST WORKOUT (Road to 10 S2.4,
    `/api/routine/trial-workout/`) carries `trial: true` (additive; false on every other
    routine). It is read by id (`GET routines/{id}/`) and is NOT in the list: the list is
    the person's programmes, and a trial is one workout.
    """
    queryset = Routine.objects.all()
    serializer_class = RoutineSerializer
    permission_classes = [IsTrainerOrAdmin]
    pagination_class = StandardResultsSetPagination

    def get_permissions(self):
        if self.action in ['list', 'retrieve']:
            return [IsRoutineOwnerOrAssigned()]
        return super().get_permissions()

    def _answers_trainer_rows(self):
        """True when this request is answered by `TrainerRoutineSerializer`."""
        return bool(getattr(self.request.user, 'is_trainer', False)
                    and self.action in ('list', 'retrieve'))

    def get_queryset(self):
        """Filter routines based on user role.

        One plain query per page (BACKEND_BUGS B-136): no GROUP BY, no DISTINCT, no join
        to a to-many relation, no whole user row. The list used to join `assigned_to`
        and `progress` under a GROUP BY for three `COUNT(DISTINCT ...)`, select two whole
        user rows (the author, and a bought copy's product trainer, password hashes
        included) and, for a trainer, `.distinct()` over all ~160 of those columns. The
        planner's estimate for that fan-out passed `jit_above_cost`, and PostgreSQL
        spent 3-20 s compiling a query whose nodes finished in 1.4 ms.
        """
        user = self.request.user

        # `progress` is NOT prefetched: the serializer counted those rows in Python
        # "to avoid a DB hit", which forced every one of them into memory — twenty
        # routines x twenty clients x thirty days is 12,000 rows fetched to produce
        # twenty percentages. They are counted in the query, each as a correlated
        # subquery (the pattern of `serializers._used_by_counts`): one number per row,
        # no join, so no GROUP BY and no fan-out. `client_count` is not annotated: the
        # serializer answers it from the assignees the CALLER may know about
        # (`RoutineAssigneesMixin`), and a count over every assignee was never read.
        from django.db.models import Func, IntegerField, OuterRef, Subquery

        from .models import RoutineProgress

        def _progress_count(**only):
            # `COUNT(id)` as a plain function over the filtered rows: an aggregate with
            # no GROUP BY, so the subquery always answers exactly one row (0 for none).
            rows = (RoutineProgress.objects.filter(routine=OuterRef('pk'), **only)
                    .order_by()
                    .annotate(n=Func(F('id'), function='COUNT',
                                     output_field=IntegerField()))
                    .values('n'))
            return Subquery(rows, output_field=IntegerField())

        # The author and a bought copy's product trainer are read for their names
        # only (`created_by`'s text, `product.trainer.{id, name}`): fetched by id with
        # the columns those names need, never the whole row. The trainer's rows
        # (`TrainerRoutineSerializer`) show neither, so they skip the purchase joins.
        names = CustomUser.objects.only('id', 'username', 'user_type', 'first_name',
                                        'last_name')
        queryset = self.queryset.prefetch_related(
            'routine_exercises__exercise__media',
            'assigned_to',
            Prefetch('created_by', queryset=names),
        ).annotate(
            progress_total=_progress_count(),
            progress_done=_progress_count(status='completed'),
        # The model's total order, explicitly: when this was an aggregate, Django
        # dropped Meta.ordering from the GROUP BY query and pages repeated rows.
        ).order_by('-created_at', '-id')
        if not self._answers_trainer_rows():
            queryset = queryset.select_related('purchase__product').prefetch_related(
                Prefetch('purchase__product__trainer', queryset=names))

        # `?source_template=<id>`: the private copies written from one template, e.g.
        # "which of my clients got this program". Narrows; never widens what you see.
        source = int_param(self.request.query_params, 'source_template')
        if source is not None:
            queryset = queryset.filter(source_template_id=source)

        # Admins see every routine; a trainer the routines they wrote and those their
        # clients are on; anyone else the routines they are on. `readable_routines` is
        # that rule, and the chat's program card asks the same function whether a
        # client may still open the routine a card names (`chat.serializers`).
        # `accessible_user_ids` inside it is the one predicate that means "this
        # trainer may see this client": it requires an APPROVED relation. These
        # querysets filtered on the `assigned_trainer` pointer once, which is a second
        # definition of the same thing — and two definitions drift.
        from .permissions import readable_routines

        readable = readable_routines(user, queryset)
        if self.action == 'list':
            # The list is the person's PROGRAMMES. Their copy of the free first workout
            # is a workout, not one (Road to 10 S2.4): read by id, never listed.
            readable = readable.filter(trial=False)
        return readable

    def get_serializer_class(self):
        """Use different serializers based on user role"""
        if self._answers_trainer_rows():
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

        409 `routine_has_history` — retire it with `is_active=false` instead (the owner,
        2026-09-24: every refusal names its code).
        """
        from django.db.models import ProtectedError

        try:
            return super().destroy(request, *args, **kwargs)
        except ProtectedError:
            return Response(
                {'error': _('This routine has recorded client progress and cannot be '
                            'deleted. Set is_active=false to retire it instead.'),
                 'code': 'routine_has_history'},
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

    @action(detail=True, methods=['get'], permission_classes=[permissions.IsAuthenticated])
    def schedule(self, request, pk=None):
        """GET /api/routine/routines/{id}/schedule/ — which day of this programme is
        today, the week it is in, and that week's days (Road to 10 S0.1, BACKEND_BUGS
        B-197). The ONE rule: home's `workout_hero` reads the same function
        (`routine/schedule.py`), so Home, Train and the Day screen name the same day,
        week and minutes.

        Only the person the routine is assigned to (any role; in practice the client —
        a routine is one client's private copy). Anyone else, the trainer who wrote it
        included, gets 404 `not_found`, as for a routine that does not exist. 401
        without a token. No headers needed; nothing differs by build. A frozen, unpaid
        or ended coaching still READS it (the owner, 2026-09-19) and says so in
        `refusal`.

        THE RULE (the owner's D15, 2026-11-22): today is the next day not done, never
        the calendar cycle. A day is closed by its day outcome — complete is `done`,
        anything else (sets missing, skipped, swept unclosed) is `short`, and a closed
        day is never offered again. The workout days are done once each per pass; when
        all are closed the next pass begins. A missed date does not move today: the
        workout waits. Rest days are kept — after a closed day, the rest days that
        follow it in the routine are taken one calendar day each — and never owed.
        A programme past its `end_date`, or whose every day is done when it does not
        repeat (its days cover its dates, or it has no end date and more than seven
        days), is `finished`.

        200::

            {"routine_id": 12, "date": "2026-11-22",        the server's day
             "today": {"day": 3, "state": "today"} | null,
                      state: today | done | short | rest | not_started
                      (`done`/`short`: closed today; null: finished, nothing closed
                      today, or a routine with no exercises)
             "next_day": 3 | null,                the next day not done; null when
                                                  finished
             "week": 4,                           from `start_date`, 1..weeks_total;
                                                  a BOUGHT programme (sessions, not
                                                  dates): the week its sessions have
                                                  reached, = `slots_week` (2026-11-23)
             "weeks_total": 9 | null,             from the dates; a bought programme's
                                                  own weeks; null for a cycle with no end
             "finished": false,
             "starts_on": "2026-12-01" | null,    only before the programme starts
             "days_in_cycle": 63,                 `Routine.days`
             "slots_week": 1,                     which of the routine's own weeks the
                                                  slots are (the whole routine when it
                                                  has 7 days or fewer; a bought
                                                  programme's sessions a week; else 7)
             "slots": [{"day": 1, "name": "Push" | null, "muscles": ["Chest"],
                        "state": "done",
                        "date": "2026-11-17" | null,    only when the routine's days
                                                        ARE its dates
                        "closed_on": "2026-11-17" | null,
                        "counts": {"exercises": 4, "sets": 14,
                                   "estimated_minutes": 28}}, ...],
             "refusal": null | "coaching_frozen" | "coaching_unpaid" |
                        "coaching_ended" | "product_refunded"}

        Slot `state`: `done`, `short` (closed with something missing), `today`, `upcoming`,
        `rest` (no exercises that day), `missed` (only in a FINISHED programme: a day it
        ended without). `muscles` are stored keys, never labels. `estimated_minutes` is
        the routine's own formula on that day's rows (4 s a rep, the rests between sets,
        2 min between exercises) — the number home's hero answers for the same day
        (BACKEND_BUGS B-196). `refusal` is the 403 `code` a training write on this
        routine would answer now (`users/coaching.py training_refusal`).

        Codes: 404 `not_found`.
        """
        from .permissions import readable_routines
        from .schedule import build
        from users.coaching import training_refusal

        routine = readable_routines(request.user).filter(pk=pk).first()
        if routine is None or not routine.assigned_to.filter(pk=request.user.pk).exists():
            raise NotFound(_('Routine not found.'), code='not_found')
        body = build(request.user, routine, timezone.localdate()).as_dict()
        body['refusal'] = training_refusal(request.user, routine)
        return Response(body)

    @action(detail=True, methods=['post'], permission_classes=[permissions.IsAuthenticated])
    def copy_week(self, request, pk=None):
        """POST /api/routine/routines/{id}/copy_week/ — one week of this routine written
        onto another week of it, loads multiplied (BACKEND_BUGS B-103; TrainerWeek's
        «التدرّج التلقائي»: +2.5 %, +5 %, or the same loads).

        Body: `from_week`, `to_week` (whole numbers from 1; week `w` is days
        `(w-1)*7+1 .. w*7`, and only whole weeks count — a 10-day routine has one) and
        `load_factor` (optional, 0.5-1.5, default 1: `1.025` is +2.5 %).

        What is written, in ONE transaction — the whole week or nothing: every row of
        `from_week`, moved to the same weekday of `to_week`, with its sets, reps, rest,
        note, `superset_group` and `load_mode`. `weight` is multiplied and rounded to
        the nearest 0.5 kg; `load_value` too in `percent` mode (to 0.5 %); an `rpe` is
        copied as written. A factor of exactly 1 copies every number unrounded. Day
        names travel with their days and never replace a name the target day has.

        A FILLED target week is refused, never replaced: 409 `week_not_empty`. A week
        the trainer already wrote is work that one tap must not delete; to redo it,
        delete its rows (`DELETE routine-exercises/{id}/`) and copy again.

        201 `{copied, from_week, to_week, load_factor, routine_exercises, day_names}` —
        `routine_exercises` are the new rows exactly as `routine-exercises/` answers
        them, ready to merge into the grid; `day_names` is the routine's whole map.

        Only the trainer who WROTE the routine. Seen through the one predicate that
        decides what a trainer sees (`accessible_user_ids`, via this viewset's queryset):
        a routine the caller cannot see is 404; one they can see and did not write —
        the client it is for, the trainer of a client on it, an admin — is 403
        `not_routine_owner`. The coaching freeze does not refuse it, exactly as it
        refuses no other routine write: a freeze stops the CLIENT training
        (`users/coaching.training_refusal`), and a trainer may still prepare a frozen
        client's next weeks.

        | Status | `code` | When |
        |---|---|---|
        | 400 | `week_invalid` | `from_week` / `to_week` missing or not a whole number from 1; `field` names it |
        | 400 | `week_out_of_range` | a week past the routine's whole weeks; with `field` and `weeks` |
        | 400 | `same_week` | `from_week` equals `to_week` |
        | 400 | `week_empty` | `from_week` has no exercises; with `from_week` |
        | 400 | `load_factor_invalid` | `load_factor` is not a number |
        | 400 | `load_factor_out_of_range` | `load_factor` outside 0.5-1.5 |
        | 400 | `load_out_of_range` | a multiplied load would pass 1000 kg or 100 %; with `day`. Nothing is written |
        | 401 | `not_authenticated` | signed out |
        | 403 | `not_routine_owner` | you can see the routine and did not write it |
        | 404 | `not_found` | no such routine, or not one you can see |
        | 409 | `week_not_empty` | `to_week` already has exercises; with `to_week`. Nothing is written |

        Identical on every build.
        """
        from .media_context import prepare
        from .weeks import WeekCopyRefused, copy_week, parse_load_factor, parse_week

        routine = self.get_object()
        # The author AND a trainer: a bought copy of a routine product is `created_by`
        # its buyer (a client), and authorship alone let the buyer rewrite it (the
        # review, 2026-09-24).
        if (routine.created_by_id != request.user.id
                or not getattr(request.user, 'is_trainer', False)):
            return Response({'error': _('Only the trainer who wrote this routine can change it.'),
                             'code': 'not_routine_owner'}, status=status.HTTP_403_FORBIDDEN)
        data = request.data if isinstance(request.data, dict) else {}
        try:
            from_week = parse_week(data.get('from_week'), 'from_week')
            to_week = parse_week(data.get('to_week'), 'to_week')
            factor = parse_load_factor(data.get('load_factor'))
            created, day_names = copy_week(routine, from_week, to_week, factor)
        except WeekCopyRefused as refusal:
            return Response(refusal.body(), status=refusal.status)

        rows = list(RoutineExercise.objects.filter(pk__in=[r.pk for r in created])
                    .select_related('exercise', 'exercise__created_by')
                    .prefetch_related('exercise__media')
                    .order_by('day', 'order', 'id'))
        context = {'request': request, **prepare(request, rows)}
        return Response({
            'copied': len(rows),
            'from_week': from_week,
            'to_week': to_week,
            'load_factor': float(factor),
            'routine_exercises': RoutineExerciseSerializer(rows, many=True, context=context).data,
            'day_names': day_names,
        }, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=['post'], permission_classes=[IsTrainerOrAdminForAssignment])
    def assign_to_client(self, request, pk=None):
        """POST /api/routine/routines/{id}/assign_to_client/ — make this routine one
        client's program. Body: `client_id`.

        Trainers (for an approved client of theirs) and admins. A routine belongs to one
        client (the owner, 2026-09-19): the first assignment also settles the coaching's
        first month, and the client is notified in their language.

        200 `{message, routine_id, client_id, assignment_date}`.

        | Status | `code` | When |
        |---|---|---|
        | 400 | `client_id_required` | `client_id` missing or not a whole number |
        | 400 | `not_your_client` | the client is not an approved client of yours |
        | 400 | `already_assigned` | already this client's (nothing is paid twice); carries `routine_id`, `client_id` |
        | 404 | `client_not_found` | no client with that id |
        | 404 | `not_found` | no such routine, or not yours |
        | 409 | `routine_belongs_to_another_client` | with `copy_url`: another client is on it, or has trained on it (their history stays with it) — copy it with `routines/{id}/copy/` instead. A trainer's own tries on a draft do not count |
        | 500 | `routine_assign_failed` | an unexpected fault; nothing to fix in the request |

        Every refusal names its own code (the owner, 2026-09-24); the four 400/404 codes
        above answered only `bad_request` / `not_found` before. Identical on every build.
        """
        routine = self.get_object()
        client_id = _int_or_none(request.data.get('client_id'))

        if not client_id:
            return Response(
                {"error": _("client_id is required"), "code": "client_id_required"},
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
                            "error": _("You can only assign routines to your approved clients."),
                            "code": "not_your_client",
                            "details": f"Client {client.username} is not in your approved client list."
                        },
                        status=status.HTTP_400_BAD_REQUEST
                    )

            # Check if routine is already assigned to this client
            if client in routine.assigned_to.all():
                return Response(
                    {
                        "error": _("This routine is already assigned to this client."),
                        "code": "already_assigned",
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
                {"error": _("Client not found"), "code": "client_not_found"},
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
                {"error": _("An error occurred while assigning the routine."),
                 "code": "routine_assign_failed"},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )

    @action(detail=True, methods=['post'], permission_classes=[IsTrainerOrAdminForAssignment])
    def unassign_from_client(self, request, pk=None):
        """
        Enhanced routine unassignment from a client.
        
        Requirements:
        - Only trainers and admins can unassign routines
        - Validation ensures proper authorization

        Body: `client_id`. 200 `{message, routine_id, client_id, unassignment_date}`.

        | Status | `code` | When |
        |---|---|---|
        | 400 | `client_id_required` | `client_id` missing or not a whole number |
        | 400 | `not_your_client` | the client is not an approved client of yours |
        | 400 | `not_assigned` | the routine is not this client's; carries `routine_id`, `client_id` |
        | 404 | `client_not_found` | no client with that id |
        | 404 | `not_found` | no such routine, or not yours |
        | 500 | `routine_unassign_failed` | an unexpected fault |

        Every refusal names its own code (the owner, 2026-09-24). Identical on every build.

        TODO: Add unassignment notifications
        TODO: Implement unassignment history tracking
        """
        routine = self.get_object()
        client_id = _int_or_none(request.data.get('client_id'))

        if not client_id:
            return Response(
                {"error": _("client_id is required"), "code": "client_id_required"},
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
                            "error": _("You can only unassign routines from your approved clients."),
                            "code": "not_your_client",
                            "details": f"Client {client.username} is not in your approved client list."
                        },
                        status=status.HTTP_400_BAD_REQUEST
                    )

            # Check if routine is actually assigned to this client
            if client not in routine.assigned_to.all():
                return Response(
                    {
                        "error": _("This routine is not assigned to this client."),
                        "code": "not_assigned",
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
                {"error": _("Client not found"), "code": "client_not_found"},
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
                {"error": _("An error occurred while unassigning the routine."),
                 "code": "routine_unassign_failed"},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )

    @action(detail=True, methods=['post'], permission_classes=[permissions.IsAuthenticated])
    def update_progress(self, request, pk=None):
        """POST /api/routine/routines/{id}/update_progress/ — set a day's status for a date
        (today unless `date` says otherwise).

        Body: `day`, `status` (`not_started | in_progress | completed | skipped`),
        `confirm_unlogged` (bool), and two optional keys (BACKEND_BUGS B-157, additive):

        `date`       `YYYY-MM-DD`, the calendar day being closed — a LogDay for
                     yesterday closes yesterday, measured against the sets logged THAT
                     day. Inside the training window, like `set-logs/sync/`: 400
                     `bad_date`, `date_in_future`, `date_too_old`. Not sent: today.
        `quick_log`  `true` when the day was logged after the fact in one go
                     («سجّل دفعة واحدة») rather than live. Kept on the day's outcome as
                     `outcome.quick_log`, and no later close takes it away
                     (`bulk-complete` sets it too). It changes nothing else: `via`, the
                     unlogged-sets warning and the streak rules are the same.

        Completing a day does not require every set to be
        logged, but the person must be warned first (owner, 2026-09-19): while any
        prescribed set of that day is unlogged on that date, `completed` answers
        409 `unlogged_sets` with `{prescribed, logged, unlogged}`, and the app sends the
        same request again with `confirm_unlogged: true` once the person has seen it.
        Closing a day (`completed` or `skipped`) answers `outcome`: what the day planned
        against what was done — `planned_sets`, `logged_sets`, `missed_sets`,
        `planned_reps`, `logged_reps`, `complete`, `confirmed_unlogged` (the owner,
        2026-09-20), `quick_log` (B-157) and `date`. A day closed with sets missing does
        not count as a training day and breaks the streak; `analytics/adherence/` reads
        these rows.

        Other codes (every refusal names its own since the owner's decision of
        2026-09-24; the ones marked new answered `bad_request` / `permission_denied`):

        | Status | `code` | When |
        |---|---|---|
        | 400 | `day_and_status_required` (new) | `day` or `status` missing |
        | 400 | `bad_day` (new) | `day` is not a whole number |
        | 400 | `bad_status` | `status` is not one of the four |
        | 400 | `day_out_of_range` (new) | `day` is outside 1..the routine's days |
        | 400 | `day_has_no_exercises` (new) | that day of the routine has no exercises |
        | 400 | `bad_date` / `date_in_future` / `date_too_old` | `date` is not a date, is after today, or is before the training window (B-157) |
        | 403 | `routine_not_yours` (new) | the routine is not assigned to you |
        | 403 | `coaching_frozen` / `coaching_unpaid` / `coaching_ended` / `product_refunded` | with `trainer_id`, while the coaching this routine came from is unpaid or over — readable, not trainable (the owner) |
        | 404 | `not_found` | no such routine, or not one you can see |
        | 409 | `unlogged_sets` | above |
        """
        routine = self.get_object()
        user = request.user
        day = request.data.get("day")
        status = request.data.get("status")

        if user not in routine.assigned_to.all():
            return Response({"error": _("You are not authorized to update this routine."),
                             "code": "routine_not_yours"}, status=403)
        # The day being closed (B-157). Without it this route could only close TODAY,
        # so a LogDay for yesterday was measured against today's sets (409, 0 logged)
        # and, once confirmed, wrote a 0-of-N day today.
        on = None
        if request.data.get('date') not in (None, ''):
            refused = _backlog_date_refused(request.data.get('date'))
            if refused is not None:
                return refused
            on = _date_or_none(request.data.get('date'))
        _require_can_train(user, routine, on)

        if not day or not status:
            return Response({"error": _("Day and status are required."),
                             "code": "day_and_status_required"}, status=400)

        # Parsed once, and before it is used. `int(day)` sat unguarded in the comparison
        # below, so `{"day": "abc"}` was a ValueError and a 500 rather than a 400.
        try:
            day_number = int(str(day).strip())
        except (TypeError, ValueError):
            return Response({"error": _("Day must be a whole number."), "code": "bad_day"},
                            status=400)

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
            return _day_refused(routine, day_number)

        confirmed = _truthy(request.data.get('confirm_unlogged'))
        counts = (_day_set_counts(routine, user, day_number, date=on)
                  if status in ('completed', 'skipped') else None)
        if status == 'completed' and not confirmed and counts['unlogged']:
            return Response({
                'error': _('Some sets of this day are not logged yet.'),
                'code': 'unlogged_sets', **counts}, status=409)

        try:
            updated_progress = routine.update_progress(user=user, day=day_number, status=status,
                                                       progress_date=on)
        except ValueError as e:
            # `Routine.update_progress` raises it for one reason only: the day is outside
            # 1..days (a row left on a day the routine no longer has).
            return Response({"error": _("Invalid progress update."),
                             "code": "day_out_of_range"}, status=400)

        # What the day planned against what was done, kept instead of thrown away (the
        # owner, 2026-09-20). A day closed with sets missing is recorded as such, and it
        # breaks the streak; the numbers come back in `outcome` for the screen that just
        # closed it.
        outcome = None
        if counts is not None:
            from .outcomes import record_day_quietly
            via = ('skipped' if status == 'skipped'
                   else 'confirmed_unlogged' if counts['unlogged'] else 'completed')
            outcome = record_day_quietly(
                user, routine, day_number, on, via=via,
                # Only a completed day is ever warned: a skipped one saw nothing.
                confirmed_unlogged=(status == 'completed' and bool(counts['unlogged'])),
                quick_log=_truthy(request.data.get('quick_log')))

        body = {"message": _("Progress updated successfully"),
                "updated_progress": updated_progress}
        if outcome is not None:
            from .serializers import DayOutcomeSerializer
            body['outcome'] = DayOutcomeSerializer(outcome).data
        return Response(body, status=200)

    @action(detail=False, methods=['get'])
    def my_clients_progress(self, request):
        """Get progress for all clients assigned to this trainer.

        Trainers only: anyone else answers 403 `not_a_trainer` (the owner, 2026-09-24 —
        the code the trainer-only routes answer; it was the generic
        `permission_denied`)."""
        if not request.user.is_trainer:
            return Response(
                {"error": _("This endpoint is only for trainers"), "code": "not_a_trainer"},
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


class RoutineExerciseFilter(django_filters.FilterSet):
    """`?routine=<id>&day=<n>` as NUMBERS, never as a lookup of the routine row.

    See `RoutineExerciseViewSet`: a model-choice filter validates the id against every
    routine there is, and its two different answers are an existence oracle.
    """
    routine = _WholeNumberFilter(field_name='routine_id')
    day = _WholeNumberFilter(field_name='day')

    class Meta:
        model = RoutineExercise
        fields = ['routine', 'day']


class RoutineExerciseViewSet(ExerciseMediaMixin, viewsets.ModelViewSet):
    """
    API endpoint for managing RoutineExercise objects.
    Only trainers (owners) and admins can create/update/delete. All users can read.

    List filters: `?routine=<id>&day=<n>` — ONE day of one routine, which is how a
    client's workout screen wants to read it. Without them the day arrived only inside
    the whole-routine payload, though `template-exercises/` could already be filtered
    the same way (SIDE_BUGS S-030). The filters narrow what `get_queryset` has already
    scoped, so they cannot widen it: a routine that is neither yours nor assigned to
    you answers an empty page, never somebody else's programme — and so does a routine
    id that does not exist at all. Both are plain numbers: the auto-generated filter
    checked `routine` against EVERY routine in the table, so a missing id answered 400
    `Select a valid choice` while someone else's real one answered an empty 200, which
    told anyone which routine ids exist (SIDE_BUGS S-119). A value that is not a whole
    number answers 400 on that parameter.

    Paginated: `{count, next, previous, results}`, `?page_size=` up to 100.
    """
    queryset = RoutineExercise.objects.select_related(
        'routine', 'routine__created_by', 'exercise'
    ).prefetch_related('exercise__media')
    serializer_class = RoutineExerciseSerializer
    permission_classes = [IsAdminOrOwnerOrReadOnly]
    pagination_class = StandardResultsSetPagination
    filter_backends = [DjangoFilterBackend]
    filterset_class = RoutineExerciseFilter

    def get_queryset(self):
        """
        Restrict to routines the caller is assigned to or created (admins see all).
        Previously unscoped: IsAdminOrOwnerOrReadOnly returns True for all reads, so
        any authenticated user could list every routine's exercise composition.
        A bought copy of a routine product is `created_by` its buyer, so authorship
        does not grant reading it: only the assignment does, and a refund unassigns the
        buyer (`routine/products.py refund`) — the copy is revoked, not left readable row
        by row (the review, 2026-09-24).
        """
        user = self.request.user
        if getattr(user, 'is_admin', False):
            return self.queryset
        return self.queryset.filter(
            Q(routine__assigned_to=user)
            | Q(routine__created_by=user, routine__purchase__isnull=True)
        ).distinct()


class RoutineProgressViewSet(viewsets.ModelViewSet):
    """
    ViewSet for managing routine progress.
    Clients can only see their own progress.
    Trainers can see their clients' progress.
    A client's create or update answers 403 `coaching_frozen` / `coaching_unpaid` / `coaching_ended` / `product_refunded` (with
    `trainer_id`) while the coaching the routine came from is unpaid or over. A create
    naming a routine that is neither yours nor assigned to you answers 403
    `routine_not_yours` (the owner, 2026-09-24; it was the generic `permission_denied`).

    Every row carries `outcome` since 2026-09-20: what that day actually came to, or
    null while the day is still open. `{complete, via, missed_sets, planned_sets,
    logged_sets, confirmed_unlogged, quick_log}` — `quick_log` (B-157) is true for a day
    logged after the fact in one go, the coach's quick-log badge; `via` says what closed it (`completed`, `confirmed_unlogged`,
    `skipped`, `bulk_complete`, `session`, `auto`, `unclosed`), and `complete` is false
    for a day closed with sets still missing, which is the day that breaks the streak
    (the owner, 2026-09-20). The full per-exercise breakdown of one day is on
    `PATCH …/update_progress/`'s answer and in `analytics/adherence/?period=`.
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
            raise PermissionDenied(_('This routine is not assigned to you.'),
                                   code='routine_not_yours')
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
        over (403 `coaching_frozen` / `coaching_unpaid` / `coaching_ended` / `product_refunded`). A trainer's correction is not."""
        instance = serializer.instance
        _require_can_train(self.request.user, instance.routine,
                           serializer.validated_data.get('date', instance.date))
        serializer.save()

    def get_queryset(self):
        """Filter progress based on user role with optimized queries."""
        from django.db.models import OuterRef, Subquery

        from .models import DayOutcome

        user = self.request.user
        
        # Optimize: add select_related for frequently accessed relations
        base_qs = self.queryset.select_related(
            'user', 'routine', 'routine__created_by'
        ).prefetch_related(
            'routine__routine_exercises__exercise'
        )
        # What the day planned against what was done, for the same (user, routine, day,
        # date). Subqueries, not a per-row read: this list is paginated and a query per
        # row is what every other annotation here exists to avoid.
        outcome = DayOutcome.objects.filter(
            user_id=OuterRef('user_id'), routine_id=OuterRef('routine_id'),
            day=OuterRef('day'), date=OuterRef('date'))
        base_qs = base_qs.annotate(
            outcome_planned_sets=Subquery(outcome.values('planned_sets')[:1]),
            outcome_logged_sets=Subquery(outcome.values('logged_sets')[:1]),
            outcome_missed_sets=Subquery(outcome.values('missed_sets')[:1]),
            outcome_complete=Subquery(outcome.values('complete')[:1]),
            outcome_confirmed_unlogged=Subquery(outcome.values('confirmed_unlogged')[:1]),
            outcome_quick_log=Subquery(outcome.values('quick_log')[:1]),
            outcome_via=Subquery(outcome.values('via')[:1]),
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
    # `reps__gt=0` like `strength.personal_best`: a row with a weight and no reps is
    # not a set that was lifted, and the two answers must not disagree about what a
    # record is.
    weighted = ExerciseSetLog.objects.filter(user_exercise_progress__user=user,
                                             weight__gt=0, reps__gt=0)
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


def _day_refused(routine, day):
    """400 for a routine day nothing can be logged against, naming which reason.

    The codes are the ones `workout-sessions/` already answers on `day` for the same two
    reasons (`WorkoutSessionSerializer._check_routine_day`): `day_out_of_range` for a day
    outside 1..`routine.days`, `day_has_no_exercises` for a day that exists and holds
    nothing. `routines/{id}/update_progress/`, `set-logs/bulk-create/` and
    `user-exercise-progress/bulk-complete/` answered the bare `bad_request` for both (the
    owner, 2026-09-24: every refusal names its own code).
    """
    if not 1 <= day <= (routine.days or 0):
        return Response({'error': _('Day %(day)s is not part of this routine.') % {'day': day},
                         'code': 'day_out_of_range'},
                        status=status.HTTP_400_BAD_REQUEST)
    return Response({'error': _('Day %(day)s has no exercises.') % {'day': day},
                     'code': 'day_has_no_exercises'},
                    status=status.HTTP_400_BAD_REQUEST)


def _backlog_date_refused(raw):
    """The 400 for a logging `date` outside what may be logged, or None when it is fine.

    `bad_date`, `date_in_future` and `date_too_old` — the codes `set-logs/sync/` answers
    per row for the same three reasons. `bulk-create` and `bulk-complete` answered the
    bare `bad_request` for all three (the owner, 2026-09-24).
    """
    parsed = _date_or_none(raw)
    if parsed is None:
        return Response({'error': _('date must be in YYYY-MM-DD format.'), 'code': 'bad_date'},
                        status=status.HTTP_400_BAD_REQUEST)
    today = timezone.localdate()
    if parsed > today:
        return Response({'error': _('Cannot log a workout in the future.'),
                         'code': 'date_in_future'},
                        status=status.HTTP_400_BAD_REQUEST)
    if (today - parsed).days > MAX_BACKLOG_DAYS:
        return Response(
            {'error': _('Cannot log a workout more than %(d)d days in the past.')
             % {'d': MAX_BACKLOG_DAYS}, 'code': 'date_too_old'},
            status=status.HTTP_400_BAD_REQUEST)
    return None


def _routine_not_found():
    """404 `routine_not_found`: a `routine_id` in the BODY names no routine. The same code
    `set-logs/sync/` answers per row. Distinct from the `not_found` of a URL's own object,
    so the app can tell the routine it named from the route it called."""
    return Response({'error': _('Routine not found.'), 'code': 'routine_not_found'},
                    status=status.HTTP_404_NOT_FOUND)


def _routine_not_yours():
    """403 `routine_not_yours`: the routine exists and is neither yours nor assigned to
    you, so nothing may be logged against it."""
    return Response({'error': _('You do not have access to this routine.'),
                     'code': 'routine_not_yours'},
                    status=status.HTTP_403_FORBIDDEN)


def _day_set_counts(routine, user, day, date=None) -> dict:
    """Prescribed, logged and unlogged sets of one routine day (today by default).

    One counting rule, in `routine/outcomes.py`: the warning before completing a day and
    the outcome written when it closes must never disagree. The three keys this answers
    are the contract of the 409 and do not change.
    """
    from .outcomes import day_counts

    counts = day_counts(routine, user, day, date=date)
    return {'prescribed': counts['prescribed'], 'logged': counts['logged'],
            'unlogged': counts['unlogged']}


def _heaviest_per_exercise(results):
    """The heaviest set just written for each exercise, re-read in one query.

    A bulk write reports rows, not objects; a record is judged against everything logged
    before it, so only the top set of each exercise can be one and the rest need not be
    asked about.
    """
    ids = [row['id'] for row in results if row.get('id')]
    if not ids:
        return []
    top: dict = {}
    for log in (ExerciseSetLog.objects.filter(pk__in=ids)
                .select_related('user_exercise_progress__exercise')
                .order_by('pk')):
        key = log.user_exercise_progress.exercise_id
        kept = top.get(key)
        if kept is None or (log.weight or 0) > (kept.weight or 0):
            top[key] = log
    return list(top.values())


def _previous_best(best):
    """`{weight, reps, date}` of the best a personal record beat (BACKEND_BUGS B-112),
    from `strength.personal_best` / `record_beaten`; None when there is none. `date` is
    `YYYY-MM-DD`, the training day of that set."""
    if best is None:
        return None
    return {'weight': best['weight'], 'reps': best['reps'],
            'date': best['date'].isoformat() if best.get('date') else None}


def _celebrate_personal_record(log):
    """Tell someone the moment they beat their own best (the owner, 2026-09-20).

    The heaviest set of a session is the only one that can claim it, the first set of an
    exercise never can (there is nothing to beat), and the push is keyed on the set, so a
    retry of the same set never celebrates twice. A failure here is logged: the set is
    recorded either way — a missed cheer must not cost a workout.

    The answer carries the verdict (`_is_personal_record`) and, for a record, the best
    it beat (`_previous_best`, BACKEND_BUGS B-112) — both from ONE judgement,
    `strength.record_beaten`, so the «+x kg» on the record card is exactly the record
    this set beat. Both are also kept on the row, in ONE update
    (`ExerciseSetLog.personal_record`, `ExerciseSetLog.previous_best`), so a re-sent
    sync batch and the coach's review read what the client was told (B-110, B-106) —
    and a re-send never recomputes what the record beat: an earlier set corrected or
    deleted since would make the two disagree (the review of 2026-09-24).
    """
    from notifications.domain.dispatcher import emit_event
    from notifications.domain.progress_events import PersonalRecordEvent

    from .strength import record_beaten

    try:
        if log is None:
            return
        beaten = record_beaten(log)
        record = beaten is not None
        previous = _previous_best(beaten)
        log._is_personal_record = record        # the answer carries it, so the app cheers now
        log._previous_best = previous
        if (bool(getattr(log, 'personal_record', False)) != record
                or getattr(log, 'previous_best', None) != previous):
            # A savepoint: a failed write here must not break the request's transaction.
            with transaction.atomic():
                ExerciseSetLog.objects.filter(pk=log.pk).update(personal_record=record,
                                                                previous_best=previous)
            log.personal_record, log.previous_best = record, previous
        if not record:
            return
        progress = log.user_exercise_progress
        event = PersonalRecordEvent(
            recipient_id=progress.user_id, exercise_id=progress.exercise_id,
            exercise_name=progress.exercise.name,
            weight=f"{float(log.weight):g}", reps=int(log.reps), set_log_id=log.pk)
        transaction.on_commit(lambda: emit_event(event))
    except Exception:       # noqa: BLE001 — the set stands whatever happens here
        logger.warning("could not check a personal record for set %s",
                       getattr(log, 'pk', None), exc_info=True)


def _require_can_train(user, routine, at=None):
    """403 `coaching_frozen` / `coaching_unpaid` / `coaching_ended` / `product_refunded` when a client may see this routine but
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

def _sync_error(key, code, message, **extra):
    """One refused row of a batch. The batch itself still succeeds."""
    return {'client_uuid': str(key) if key else None, 'status': 'error',
            'code': code, 'error': str(message), **extra}


def _sync_number(raw, *, low, high, default=None):
    """A number inside its bounds, or `None` when it is neither. `False` for a value
    that is present and wrong, so the caller can tell "not sent" from "not allowed"."""
    if raw is None or raw == '':
        return default
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return False
    if not low <= value <= high:
        return False
    return value


def _duplicate_touch(user, row, existing):
    """`(progress, routine, day)` for a set a re-sent batch already wrote — so its
    recomputation runs again (SIDE_BUGS S-120).

    `set-logs/sync/` has no outer transaction: each set commits on its own, and the
    recomputation runs once at the end. A request that died in between (a timeout, a
    dropped connection, a worker restart) left those sets written and never counted,
    and the retry — the thing the app does next — answered them `duplicate` with
    nothing to recompute, so `completed_sets`, the day's `RoutineProgress` and any
    closed outcome stayed stale until another set of the exercise arrived. The
    recomputation is idempotent, so running it for a set already counted costs time and
    changes nothing.

    Scoped like the first write: the stored set's own session names the routine and
    the day; without one, the row's `routine_id` (under the same rule step 4 of
    `_sync_one_set` applies) and `day`.
    """
    from .models import Routine

    progress = existing.user_exercise_progress
    if progress is None:
        return None
    session = existing.workout_session
    routine = getattr(session, 'routine', None)
    day = getattr(session, 'day', None)
    routine_id = _int_or_none(row.get('routine_id')) if isinstance(row, dict) else None
    if routine is None and routine_id is not None:
        routine = (Routine.objects.filter(pk=routine_id)
                   .filter(Q(assigned_to=user) | Q(created_by=user)).distinct().first())
    if isinstance(row, dict):
        day = _int_or_none(row.get('day')) or day
    return progress, routine, day


@functools.lru_cache(maxsize=1)
def _single_post_rest_time_field():
    """The `rest_time` field of `POST set-logs/` (`ExerciseSetLogSerializer`), built once."""
    return ExerciseSetLogSerializer().fields['rest_time']


def _sync_rest_time(raw):
    """A sync row's `rest_time`, read by the SAME field the single `POST set-logs/`
    reads it with (BACKEND_BUGS B-109), so the two routes store and refuse exactly the
    same values. None when not sent; False when the single POST would refuse it."""
    if raw is None or raw == '':
        return None
    try:
        return _single_post_rest_time_field().run_validation(raw)
    except serializers.ValidationError:
        return False


def _sync_answer(key, state, log, exercise_id):
    """One `created` or `duplicate` row of a sync answer: what the server HOLDS for
    this set (BACKEND_BUGS B-110) — its stored `weight` and `reps`, and the record
    verdict it was answered with (`ExerciseSetLog.personal_record`), with the best that
    record beat (`previous_best`, B-112). A `created` row's verdict is filled in by
    `sync` once the batch is judged; a `duplicate` answers the stored pair, so a batch
    re-sent after its answer was lost learns the record it had and exactly what it
    beat. Both are READ, never judged again here: recomputing `previous_best` at replay
    time answered a stored verdict beside a fresh number, and an earlier set corrected
    or deleted in between made them disagree (the review of 2026-09-24)."""
    record = bool(getattr(log, 'personal_record', False))
    previous = None
    if record and state == 'duplicate':
        previous = getattr(log, 'previous_best', None)
    return {'client_uuid': str(key), 'status': state, 'id': log.pk,
            'set_number': log.set_number, 'exercise_id': exercise_id,
            'weight': None if log.weight is None else float(log.weight),
            'reps': log.reps, 'personal_record': record, 'previous_best': previous}


def _sync_key_refusal(key, user, found):
    """The owner test for a set found by its retry key: None when it is this caller's
    (or the caller is an admin), otherwise the `idempotency_conflict` row — which says
    nothing about the set. ONE test for both places `_sync_one_set` finds a key: step 1
    (the batch's keys read up front) and the race branch (a twin that committed after
    them), so the two can never answer the same key differently."""
    owner = (found.user_exercise_progress.user_id
             if found.user_exercise_progress_id else None)
    if owner != user.id and not getattr(user, 'is_admin', False):
        return _sync_error(key, 'idempotency_conflict',
                           _('This id belongs to another record.'))
    return None


def _sync_one_set(request, row, key, known, today):
    """Write one set of an offline batch. Returns `(answer, touched, log)`.

    `answer` is always a row for the response — `created`, `duplicate` or an `error`
    with its `code`. `touched` is `(progress, routine, day)` when a progress row needs
    the recomputation afterwards, and `log` the new set when there is one.

    Every rule the single POST applies, applied here: the retry key decides first (a
    re-sent batch writes nothing), the exercise has to be one this caller may read,
    reps are required unless the coach's are asked for, the date is bounded by
    `MAX_BACKLOG_DAYS`, and the coaching refusal is the same 403 code — reported on the
    row instead of thrown, because one frozen routine must not lose the other sets.
    """
    from django.core.exceptions import ValidationError as DjangoValidationError

    from .models import Routine, RoutineExercise, UserExerciseProgress
    from .permissions import visible_exercises_for

    user = request.user
    if not isinstance(row, dict):
        return _sync_error(None, 'bad_row', _('Each set must be an object.')), None, None
    if key is None:
        return _sync_error(None, 'client_uuid_required',
                           _('Every set needs its own `client_uuid`, so a re-sent '
                             'batch is not logged twice.')), None, None

    # 1. The retry key. A set already written under this id is answered with that row
    #    and nothing is written — the same contract as a retried single POST.
    existing = known.get(key)
    if existing is not None:
        refused = _sync_key_refusal(key, user, existing)
        if refused is not None:
            return refused, None, None
        # What the server HOLDS for it (B-110): the stored numbers and the verdict the
        # first answer gave, which the phone lost with that answer.
        return (_sync_answer(key, 'duplicate', existing,
                             existing.user_exercise_progress.exercise_id
                             if existing.user_exercise_progress_id else None),
                _duplicate_touch(user, row, existing), None)

    # 2. The date. Bounded exactly as `bulk-create` bounds it. Sent, it is kept; not
    #    sent, a set that names its session is dated on the session's START day
    #    (SIDE_BUGS S-289, `routine/models.py set_day_without_date`), any other today.
    #    The session itself is checked in step 4 (`session_not_found`).
    if row.get('date'):
        parsed = _date_or_none(row.get('date'))
    else:
        from .models import set_day_without_date

        named = (WorkoutSession.objects.filter(
                     pk=_int_or_none(row.get('workout_session_id')), user=user).first()
                 if row.get('workout_session_id') is not None else None)
        parsed = set_day_without_date(named, today)
    if parsed is None:
        return _sync_error(key, 'bad_date',
                           _('date must be in YYYY-MM-DD format.')), None, None
    if parsed > today:
        return _sync_error(key, 'date_in_future',
                           _('Cannot log a workout in the future.')), None, None
    if (today - parsed).days > MAX_BACKLOG_DAYS:
        return _sync_error(key, 'date_too_old',
                           _('Cannot log a workout more than %(d)d days in the past.')
                           % {'d': MAX_BACKLOG_DAYS}), None, None

    # 3. The exercise, under the one visibility rule. An exercise this caller may not
    #    read is answered exactly like one that does not exist.
    exercise_id = _int_or_none(row.get('exercise_id'))
    if exercise_id is None:
        return _sync_error(key, 'exercise_required',
                           _('exercise_id is required.')), None, None
    exercise = visible_exercises_for(user).filter(pk=exercise_id).first()
    if exercise is None:
        return _sync_error(key, 'exercise_not_found',
                           _('Exercise not found.')), None, None

    # 4. The session and the routine, when the phone knows them. A session that is not
    #    this caller's is not named as somebody else's — it is simply not found.
    session = None
    if row.get('workout_session_id') is not None:
        session = WorkoutSession.objects.filter(
            pk=_int_or_none(row.get('workout_session_id')), user=user).first()
        if session is None:
            return _sync_error(key, 'session_not_found',
                               _('Workout session not found.')), None, None
    routine = getattr(session, 'routine', None)
    day = getattr(session, 'day', None)
    routine_id = _int_or_none(row.get('routine_id'))
    if routine_id is not None:
        routine = Routine.objects.filter(pk=routine_id).filter(
            Q(assigned_to=user) | Q(created_by=user)).distinct().first()
        if routine is None and not getattr(user, 'is_admin', False):
            return _sync_error(key, 'routine_not_found',
                               _('Routine not found.')), None, None
        routine = routine or Routine.objects.filter(pk=routine_id).first()
    day = _int_or_none(row.get('day')) or day

    # 5. May this person train this, on that date?
    try:
        if routine is not None:
            _require_can_train(user, routine, parsed)
        else:
            _require_can_train_exercise(user, exercise_id, parsed)
    except APIException as refused:
        codes = refused.get_codes()
        detail = refused.detail if isinstance(refused.detail, dict) else {}
        return _sync_error(key, codes if isinstance(codes, str) else 'not_allowed',
                           detail.get('detail', _('You cannot train this right now.')),
                           trainer_id=detail.get('trainer_id')), None, None

    # 6. What was lifted. `reps` is required (the owner, 2026-09-20) unless the row
    #    says `as_prescribed`, and then the coach's number for that day is used.
    prescription = None
    if routine is not None and day is not None:
        prescription = (RoutineExercise.objects
                        .filter(routine=routine, day=day, exercise_id=exercise_id)
                        .order_by('order', 'id').first())
    reps = _sync_number(row.get('reps'), low=1, high=MAX_REPS_PER_SET)
    if reps is False:
        return _sync_error(key, 'reps_out_of_range',
                           _('reps must be between 1 and %(max)d.')
                           % {'max': MAX_REPS_PER_SET}), None, None
    if reps is None:
        if not _truthy(row.get('as_prescribed')):
            return _sync_error(key, 'reps_required',
                               _("How many reps? Send `as_prescribed: true` to use "
                                 "your coach's number.")), None, None
        if prescription is None or prescription.reps is None:
            return _sync_error(key, 'no_prescribed_reps',
                               _('Your coach did not set reps for this exercise; '
                                 'enter what you did.')), None, None
        reps = prescription.reps
    weight = _sync_number(row.get('weight'), low=0, high=MAX_WEIGHT_KG, default=None)
    if weight is False:
        return _sync_error(key, 'weight_out_of_range',
                           _('Weight must be between 0 and %(max)d kg.')
                           % {'max': MAX_WEIGHT_KG}), None, None
    if weight is None and prescription is not None and prescription.weight is not None \
            and _truthy(row.get('as_prescribed')):
        weight = prescription.weight
    set_number = _sync_number(row.get('set_number'), low=1, high=MAX_SET_NUMBER)
    if set_number is False:
        return _sync_error(key, 'set_number_out_of_range',
                           _('set_number must be between 1 and %(max)d.')
                           % {'max': MAX_SET_NUMBER}), None, None
    rpe = _sync_number(row.get('rpe'), low=1, high=10)
    if rpe is False:
        return _sync_error(key, 'rpe_out_of_range',
                           _('RPE must be between 1 and 10')), None, None
    # The rest before this set, kept exactly as the single POST keeps it (B-109): it
    # was dropped here, so a phone that trained offline had no rest on any set.
    rest_time = _sync_rest_time(row.get('rest_time'))
    if rest_time is False:
        return _sync_error(key, 'rest_time_out_of_range',
                           _('rest_time must be a whole number of seconds.')), None, None

    # 7. The progress row the phone could not know the id of. This is the whole point
    #    of the endpoint: `(user, exercise, date)` is its key, so the server can
    #    resolve it from what the phone DOES know.
    prescribed = (prescription.sets or 0) if prescription is not None else 0
    progress, created_progress = UserExerciseProgress.objects.get_or_create(
        user=user, exercise=exercise, date=parsed,
        defaults={'target_sets': prescribed})
    # target_sets is the COACH'S prescription and nothing the caller sent, the same
    # rule `bulk-create` states: taken from the request, `completed >= target` would
    # be true by construction and the day would complete itself.
    if not created_progress and prescribed and progress.target_sets != prescribed:
        progress.target_sets = prescribed
        progress.save(update_fields=['target_sets'])

    try:
        # Savepoint: this write is one row of a batch that keeps going. Without one,
        # an IntegrityError aborts the enclosing transaction and every later set of
        # the request fails with TransactionManagementError instead of being answered.
        with transaction.atomic():
            log = ExerciseSetLog.objects.create(
                user_exercise_progress=progress, workout_session=session,
                set_number=int(set_number) if set_number else None,
                weight=weight, reps=int(reps), date=parsed,
                notes=(row.get('notes') or ''), rpe=int(rpe) if rpe else None,
                rest_time=rest_time, client_uuid=key)
    except DjangoValidationError as invalid:
        return _sync_error(key, 'invalid_set',
                           (invalid.messages[0] if getattr(invalid, 'messages', None)
                            else _('Could not record this set.'))), None, None
    except IntegrityError:
        # The same batch arriving twice at once: the unique index on `client_uuid` let
        # one through, and the other answers with its row — under step 1's owner test,
        # word for word. `known` was read before that row committed, so this is the
        # first time this request sees whose it is, and a key someone else holds is
        # `idempotency_conflict` here as it is there: never their weight, reps or
        # record (the review of 2026-09-24).
        twin = (ExerciseSetLog.objects.select_related('user_exercise_progress')
                .filter(client_uuid=key).first())
        if twin is None:
            raise
        refused = _sync_key_refusal(key, user, twin)
        if refused is not None:
            return refused, None, None
        return (_sync_answer(key, 'duplicate', twin, exercise_id),
                (progress, routine, day), None)

    # The verdict and `previous_best` are filled in by `sync` once the batch is judged.
    return _sync_answer(key, 'created', log, exercise_id), (progress, routine, day), log


class ExerciseSetLogViewSet(viewsets.ModelViewSet):
    """
    ViewSet for logging individual exercise sets.

    POST may carry `client_uuid` (the app's id for the set): the first POST with it
    answers 201, a retry answers 200 with the same row, and an id already used by
    someone else's set answers 409 `idempotency_conflict`. `date` is writable, so a set
    logged offline keeps its day. Omitted, it is today — or, for a set that names its
    `workout_session`, that session's START day (SIDE_BUGS S-289): a workout begun at
    23:40 is one workout on its start day, its 00:10 sets included. A sent date is
    always kept. `set-logs/sync/` dates its rows the same way.

    A client's new set answers 403 `coaching_frozen` / `coaching_unpaid` / `coaching_ended` / `product_refunded` (with
    `trainer_id`) while the coaching its routine came from is unpaid or over, unless the
    set is dated inside a paid month (training done offline while paid still syncs).

    A set that beats everything this person has logged for that exercise answers
    `personal_record: true` and sends them the `personal_record` push (the owner,
    2026-09-20). The first set of an exercise never counts as a record. With it the
    answer carries `previous_best: {weight, reps, date}` — the best this set beat, the
    heaviest set logged before it (among equal weights the most reps, then the latest),
    so the record card can say «+x kg» against a record set weeks ago (BACKEND_BUGS
    B-112); null when the set is not a record. Both are judged only on the POST that
    creates the set: a replay (200), a PATCH and a list row answer null for both.

    `rest_time` (seconds of rest before the set) is stored as sent, a whole number, 0
    or more: a negative answers 400 `validation_error` with
    `field_errors.rest_time[0].code` `rest_time_out_of_range`, on a POST and a PATCH
    (B-122). `set-logs/sync/` reads it with this route's own field (B-109), so a sync
    row refuses the same values with the same code.

    POST, PATCH and DELETE belong to the PERSON WHOSE SET IT IS (BACKEND_DELTA item 15),
    and to an admin; a trainer may read a client's sets and no longer create, edit or
    delete them — they did not lift them. A trainer's POST naming a client's progress
    row or session answers 403 `set_not_yours` (SIDE_BUGS S-114) — and so, since the
    owner's decision of 2026-09-24, does their PATCH or DELETE of a client's set, which
    answered the generic `permission_denied` (`IsSetLogCreatorOrTrainerOrAdmin.code`). A
    second row for the same progress row, set number and date answers 400
    `set_log_exists`. The coaching
    refusal on a create is judged for the set's owner, never for the coach. `reps` is
    1-1000 (400 on `reps`), the bound every logging route applies (S-115). Anyone
    else gets 404, because the list is scoped. Only
    `weight`, `reps`, `notes` and `rpe` may change: moving a set to another progress
    row, session, set number or date answers 400 `set_immutable` on that field. Both
    are refused with 403 `coaching_frozen` / `coaching_unpaid` / `coaching_ended` / `product_refunded` while
    the coaching the set's routine came from is unpaid or over, judged on the set's own
    date. A delete recomputes everything the set was counted in — the exercise's
    completed sets, the day's `RoutineProgress`, and the `DayOutcome` of a day that was
    already closed, which can turn a complete day short and break a streak. A day that
    completed ITSELF from its sets (outcome `via` `auto`, or `session` on top of it)
    moves back from `completed` when they are deleted; a day the person closed
    (`completed`, `confirmed_unlogged`, `bulk_complete`, or `routine-progress/`, which
    writes no outcome) stays completed, and only its outcome turns short (SIDE_BUGS
    S-110).
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
        # WHOSE set this is, and who may write it (SIDE_BUGS S-114). The serializer lets
        # an approved trainer NAME a client's progress row or session — that is the
        # read rule — and a trainer then logged sets the client never lifted: filling
        # in short days, completing them, repairing a streak, raising the adherence the
        # coach is judged by, and pushing `personal_record` to the client for it. The
        # same reason a trainer may not edit or delete a client's set (they did not
        # lift it) refuses the create: 403 `set_not_yours`. An admin keeps the write,
        # as for edits. Whether a coach should ever log for a client is the owner's
        # call; until then this fails closed.
        actor = self.request.user
        owner_id = (progress.user_id if progress is not None
                    else session.user_id if session is not None else actor.id)
        is_admin = actor.is_staff or getattr(actor, 'is_admin', False)
        if owner_id != actor.id and not is_admin:
            raise PermissionDenied(
                _("Only the person who trained can log a set. A coach reads a "
                  "client's sets and does not write them."), code='set_not_yours')
        # The coaching refusal is a rule about the CLIENT and their coach, so it is
        # asked about the set's owner (the caller, once the check above has run) — it
        # was asked about the trainer, who is never refused, so a frozen coaching could
        # still be logged into. An admin is asked about as themselves, as
        # `_refuse_if_frozen` asks.
        if session is not None:
            _require_can_train(actor, session.routine, session.start_time)
        elif progress is not None:
            _require_can_train_exercise(actor, progress.exercise_id,
                                        data.get('date') or timezone.localdate())
        try:
            serializer.save()
        except IntegrityError as e:
            if 'client_uuid' in str(e):
                raise       # a retry racing its first attempt: `create` answers it
            if 'unique constraint' in str(e).lower():
                # 400 `set_log_exists`, through the handler's `service_code` (the owner,
                # 2026-09-24): a `detail`-only ValidationError answered the bare
                # `validation_error` with no field to show it on.
                refusal = serializers.ValidationError({'detail': _('A set log for this progress, set number, and date already exists.')})
                refusal.service_code = 'set_log_exists'
                raise refusal
            raise
        _celebrate_personal_record(serializer.instance)

    def perform_update(self, serializer):
        """Correcting what was lifted is training, so it is refused exactly like
        logging it: on the set's OWN date, through the routine it belongs to.

        Judged before the write, and the date is the stored one — the serializer fixes
        `date` once written, so an edit cannot move itself into a paid month.
        """
        self._refuse_if_frozen(serializer.instance)
        serializer.save()
        # The recomputation is the post_save receiver's, as it is on create: a
        # corrected weight changes the volume the day counted, not how many sets it
        # holds, and the receiver writes both from the set logs.

    def perform_destroy(self, instance):
        """Deleting a set is a training write too, and everything it was counted in is
        recomputed — by the `post_delete` receiver in `routine/models.py`, which is the
        one place that knows how (`_recalc_locked` -> `refresh_routine_progress` ->
        `refresh_day_quietly`). Nothing is duplicated here."""
        self._refuse_if_frozen(instance)
        instance.delete()

    def _refuse_if_frozen(self, instance):
        """403 `coaching_frozen` / `coaching_unpaid` / `coaching_ended` / `product_refunded` when the coaching
        behind this set is unpaid or over.

        Asked about the CALLER, as `perform_create` asks: the refusal is a rule about a
        client and their coach, so an admin clearing up a bad row is not caught by a
        client's unpaid month. The session names the routine when there is one; without
        a session the exercise decides, and the set's own date is when it happened.
        """
        actor = self.request.user
        session = instance.workout_session
        if session is not None:
            _require_can_train(actor, session.routine, session.start_time)
            return
        progress = instance.user_exercise_progress
        if progress is not None:
            _require_can_train_exercise(actor, progress.exercise_id,
                                        instance.date or progress.date)

    @action(detail=False, methods=['get'], url_path='my-progress')
    def my_progress(self, request):
        """GET /api/routine/set-logs/my-progress/ — the caller's own set logs, or their
        totals per exercise.

        Clients only (403 `clients_only` otherwise — the code every client-only route
        answers; it was the generic `permission_denied` until the owner's decision of
        2026-09-24). Query params: `group_by=exercise` for one row per
        exercise (`exercise`, `total_volume`, `sets_completed`, `average_weight`,
        `average_reps`); `routine_id` to keep to one routine.

        **Paginated** — `{count, next, previous, results}`, 25 a page, `?page_size=` up
        to 100 (SIDE_BUGS S-032). It answered a bare JSON array holding every set the
        person had ever logged: a year of training is one unbounded response, built by
        serialising every row into memory first. Both shapes page the same way.

        Order: the sets newest first (`date` descending, then the most recently written);
        `group_by=exercise` rows by exercise name.
        """
        user = request.user
        if not user.is_authenticated or not user.is_client:
            return Response({'error': _('Only clients can view their own progress.'),
                             'code': 'clients_only'}, status=403)
        group_by = request.query_params.get('group_by')
        routine_id = _int_or_none(request.query_params.get('routine_id'))
        qs = self.get_queryset().filter(user_exercise_progress__user=user)
        if routine_id:
            qs = qs.filter(workout_session__routine_id=routine_id)
        if group_by == 'exercise':
            # Aggregate per exercise. Ordered by the grouping key, which is unique per
            # row here, so the page boundaries are stable.
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
            page = self.paginate_queryset(data)
            rows = page if page is not None else data
            # Rename keys for frontend clarity
            result = [
                {
                    'exercise': d['user_exercise_progress__exercise__name'],
                    'total_volume': d['total_volume'] or 0,
                    'sets_completed': d['sets_completed'],
                    'average_weight': round(d['average_weight'] or 0, 2),
                    'average_reps': round(d['average_reps'] or 0, 2),
                }
                for d in rows
            ]
            if page is not None:
                return self.get_paginated_response(result)
            return Response(result)
        # Default: the client's set logs (optionally filtered by routine), a page at a
        # time, NEWEST FIRST — `-date`, then `-id` so the order is total and LIMIT/OFFSET
        # neither repeats nor hides a row (SIDE_BUGS S-121). It kept the model's
        # ('set_number', 'id'), so page 1 was every "set 1" the person ever logged,
        # oldest first, and the recent sets were on the last page.
        qs = qs.order_by('-date', '-id')
        page = self.paginate_queryset(qs)
        if page is not None:
            return self.get_paginated_response(self.get_serializer(page, many=True).data)
        return Response(self.get_serializer(qs, many=True).data)

    @action(detail=False, methods=['post'], url_path='bulk-create')
    def bulk_create(self, request):
        """POST /api/routine/set-logs/bulk-create/ — log a day's sets in one request.

        Body: `routine_id`, `day`, `date`, `sets`, `weight`, and `reps` — required (the
        owner, 2026-09-20), unless `as_prescribed: true` is sent instead, which uses the
        coach's reps for each exercise of that day (and their weight when none is sent).
        400 `reps_required`; an exercise whose coach set no reps is reported in `errors`
        with `no_prescribed_reps`.
        403 `coaching_frozen` / `coaching_unpaid` / `coaching_ended` / `product_refunded` (with `trainer_id`): the coaching this
        routine came from is unpaid or over — readable, not trainable (the owner).

        Every refusal names its own code (the owner, 2026-09-24; those marked new
        answered `bad_request`, `permission_denied` or `not_found`):

        | Status | `code` | When |
        |---|---|---|
        | 400 | `routine_day_date_required` (new) | `routine_id`, `day` or `date` missing |
        | 400 | `bad_number` (new) | `weight` is not a number |
        | 400 | `validation_error` | `sets` / `reps` not a whole number (`field_errors` names it) |
        | 400 | `reps_required` | above |
        | 400 | `sets_out_of_range` (new) | `sets` above 50 |
        | 400 | `weight_out_of_range` (new) | `weight` outside 0-1000 kg |
        | 400 | `reps_out_of_range` (new) | `reps` outside 1-1000 |
        | 400 | `bad_date` (new) | `date` is not YYYY-MM-DD |
        | 400 | `date_in_future` (new) | `date` after today |
        | 400 | `date_too_old` (new) | `date` more than 365 days back |
        | 400 | `day_out_of_range` / `day_has_no_exercises` (new) | the day is not one of the routine's, or holds nothing |
        | 403 | `routine_not_yours` (new) | the routine is neither yours nor assigned to you |
        | 404 | `routine_not_found` (new) | `routine_id` names no routine |

        The codes of `sync/`'s rows for the same reasons. Each `errors[]` entry of a 200
        is `{exercise, error, code}` (+ `set_number`): `no_prescribed_reps`,
        `set_log_exists`, `set_write_failed`.

        Each `results[]` row is `{exercise, set_number, created, id, personal_record,
        previous_best}`: `personal_record` is true on the heaviest new set of an
        exercise when it beat everything logged before it (the push follows), and
        `previous_best` is then `{weight, reps, date}` of the best it beat, null
        otherwise (BACKEND_BUGS B-112, as `POST set-logs/` answers it).
        """
        user = request.user
        routine_id = _int_or_none(request.data.get('routine_id'))
        day = _int_or_none(request.data.get('day'))
        date = request.data.get('date')
        if not routine_id or not day or not date:
            return Response({'error': _('routine_id, day, and date are required.'),
                             'code': 'routine_day_date_required'}, status=400)

        # --- Real-world input validation -------------------------------------
        # Previously unvalidated: negative weight (-500 kg), 100000 reps, dates in
        # 2030 or 1900, and an uncapped `sets` (500 sets => 502 rows, ~11s request).
        as_prescribed = _truthy(request.data.get('as_prescribed'))
        try:
            sets = int_param(request.data, 'sets', default=1, minimum=1, maximum=100)
            weight = float(request.data.get('weight', 0))
            reps = int_param(request.data, 'reps', default=None, minimum=1, maximum=1000)
        except (TypeError, ValueError):
            # `int_param` answers a non-number `sets` / `reps` itself, as a field error;
            # what reaches here is a `weight` that is not a number.
            return Response({'error': _('sets, weight and reps must be numeric.'),
                             'code': 'bad_number', 'field': 'weight'},
                            status=status.HTTP_400_BAD_REQUEST)
        if reps is None and not as_prescribed:
            # Ten was filled in silently before, and every "reps missed" figure reads
            # these rows (the owner, 2026-09-20).
            return Response({'error': _("How many reps? Send `as_prescribed: true` to use "
                                        "your coach's number."),
                             'code': 'reps_required', 'field': 'reps'},
                            status=status.HTTP_400_BAD_REQUEST)

        if not (1 <= sets <= MAX_SETS_PER_REQUEST):
            return Response(
                {'error': _('sets must be between 1 and %(max)d.') % {'max': MAX_SETS_PER_REQUEST},
                 'code': 'sets_out_of_range', 'max': MAX_SETS_PER_REQUEST},
                status=status.HTTP_400_BAD_REQUEST)
        if not (0 <= weight <= MAX_WEIGHT_KG):
            return Response(
                {'error': _('Weight must be between 0 and %(max)d kg.') % {'max': MAX_WEIGHT_KG},
                 'code': 'weight_out_of_range'},
                status=status.HTTP_400_BAD_REQUEST)
        if reps is not None and not (1 <= reps <= MAX_REPS_PER_SET):
            return Response(
                {'error': _('reps must be between 1 and %(max)d.') % {'max': MAX_REPS_PER_SET},
                 'code': 'reps_out_of_range'},
                status=status.HTTP_400_BAD_REQUEST)

        refused = _backlog_date_refused(date)
        if refused is not None:
            return refused
        date = _date_or_none(date)
        from .models import Routine, RoutineExercise, UserExerciseProgress, ExerciseSetLog
        try:
            routine = Routine.objects.get(id=routine_id)
        except Routine.DoesNotExist:
            return _routine_not_found()

        # AUTHORIZATION: the caller must be entitled to this routine. Without this,
        # any authenticated user could pass an arbitrary routine_id and both create
        # progress rows against a routine never assigned to them AND read back every
        # exercise name of another trainer's private routine from the response.
        if not (routine.assigned_to.filter(id=user.id).exists()
                or routine.created_by_id == user.id
                or user.is_admin):
            return _routine_not_yours()

        _require_can_train(user, routine, date)

        exercises = RoutineExercise.objects.filter(routine=routine, day=day)
        if not exercises.exists():
            # Previously a nonexistent day returned 200 {"count": 0} — a silent
            # no-op. update_progress() validates the day, so this is now consistent.
            return _day_refused(routine, day)
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
                # "As prescribed": the coach's reps for THIS exercise, and their
                # weight when the app sent none. Decided BEFORE anything is written:
                # skipping afterwards left a progress row claiming sets with no set log
                # behind it, which every adherence figure then counted as work done.
                set_reps = reps if reps is not None else rex.reps
                if set_reps is None:
                    errors.append({'exercise': rex.exercise.name,
                                   'error': _('Your coach did not set reps for this '
                                              'exercise; enter what you did.'),
                                   'code': 'no_prescribed_reps'})
                    continue
                set_weight = weight
                if as_prescribed and not request.data.get('weight') and rex.weight is not None:
                    set_weight = rex.weight

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
                            # Update, not get: re-submitting a set with a corrected
                            # weight/reps previously returned success while silently
                            # discarding the correction, so users could never fix a mistake.
                            #
                            # The OLDEST row for this set number, not `update_or_create`
                            # (SIDE_BUGS S-071): `(progress, set_number, date)` carries no
                            # unique constraint — only `client_uuid` is unique — so a set
                            # re-sent without the app's id writes a second row, and then
                            # `update_or_create` raised MultipleObjectsReturned, which is
                            # not an IntegrityError: every later "log the whole day" for
                            # that day answered 500. The duplicate is left as it is; the
                            # counts already collapse set numbers (`_recalc_locked`).
                            log = (ExerciseSetLog.objects.select_for_update()
                                   .filter(user_exercise_progress=progress,
                                           set_number=set_num, date=date)
                                   .order_by('id').first())
                            created = log is None
                            if created:
                                log = ExerciseSetLog.objects.create(
                                    user_exercise_progress=progress, set_number=set_num,
                                    date=date, weight=set_weight, reps=set_reps)
                            else:
                                log.weight, log.reps = set_weight, set_reps
                                log.save(update_fields=['weight', 'reps'])
                        results.append({'exercise': rex.exercise.name, 'set_number': set_num,
                                        'created': created, 'id': log.id})
                    except IntegrityError as e:
                        # Each row error carries a `code`, as `bulk-complete`'s do (S-042;
                        # the owner, 2026-09-24): these had a sentence only, and the
                        # duplicate one was English whatever the client spoke.
                        if 'unique constraint' in str(e).lower():
                            errors.append({'exercise': rex.exercise.name, 'set_number': set_num,
                                           'error': _('A set log for this progress, set number, and date already exists.'),
                                           'code': 'set_log_exists'})
                        else:
                            logger.exception("bulk set-log failed for %s set %s", rex.exercise_id, set_num)
                            errors.append({'exercise': rex.exercise.name, 'set_number': set_num,
                                           'error': _('Could not record this set.'),
                                           'code': 'set_write_failed'})

        # Run the suppressed recomputation ONCE per affected progress row — outside
        # the suspension block, otherwise it would be muted along with the rest. Scoped
        # to the routine and day this request names: unscoped, an exercise shared with
        # another routine decided that routine's day too (B-011 fixed the same confusion
        # for "log a whole day").
        for prog in touched_progress:
            recalc_progress_for(prog, routine_scope=routine, day_scope=day)

        # The class docstring promises a record is celebrated whenever one is beaten,
        # and this is the route the app uses to log a whole day: only the single-set
        # POST used to check, so a client who logged the day at once beat their best in
        # silence. The heaviest new set of each exercise is the only one that can claim
        # it (`strength.is_personal_record`), so that is what is asked about.
        for log in _heaviest_per_exercise(results):
            _celebrate_personal_record(log)
            for row in results:
                if row['id'] == log.pk:
                    row['personal_record'] = bool(getattr(log, '_is_personal_record', False))
                    # The best it beat, as `POST set-logs/` and `sync/` answer (B-112).
                    row['previous_best'] = getattr(log, '_previous_best', None)
        for row in results:
            row.setdefault('personal_record', False)
            row.setdefault('previous_best', None)

        return Response({'results': results, 'errors': errors, 'count': len(results)})

    @action(detail=False, methods=['post'], url_path='sync')
    def sync(self, request):
        """POST /api/routine/set-logs/sync/ — a phone that was offline hands over the
        sets it recorded (BACKEND_DELTA item 14).

        The single POST needs a server `UserExerciseProgress` id, which a phone with no
        signal cannot have. Here the phone sends what it DOES know and the server
        resolves or creates the progress row itself, from `(you, exercise, date)`.

        Body: `{"sets": [ ... ]}`, at most 200 entries (400 `batch_too_large`, which
        carries `max`), or the bare list. Each entry:

            client_uuid         required — the app's own id for the set, the retry key
            exercise_id         required
            date                YYYY-MM-DD; kept as sent. Omitted: the START day of
                                the row's `workout_session_id` session (a workout
                                begun at 23:40 is that day's, S-289), else today
            set_number          1-100, optional
            weight              kg, 0-1000, optional
            reps                1-1000 — required, unless `as_prescribed: true`
            as_prescribed       use the coach's reps (and weight) for that routine day
            routine_id, day     which day this belongs to; also found from the session
            workout_session_id  the session it was logged in, when there was one
            notes, rpe          as on a single set (rpe 1-10)
            rest_time           seconds of rest before this set, a whole number, 0
                                or more — kept exactly as the single `POST
                                set-logs/` keeps it, read by that route's own field,
                                so both accept and refuse the same values
                                (BACKEND_BUGS B-109; it was dropped until 2026-09-24;
                                a negative is refused since B-122)

        IDEMPOTENT per `client_uuid`, exactly as the single POST is: re-sending the
        same batch writes nothing and answers the same ids. PARTIAL SUCCESS: one bad
        row never fails the batch. Every entry answers `status`:

            created     `{client_uuid, status, id, set_number, exercise_id, weight,
                         reps, personal_record, previous_best}` — `weight` and `reps`
                         as STORED (the coach's numbers when `as_prescribed` filled
                         them in); `previous_best` below
            duplicate   the same keys, for the row the first send wrote, with what
                        the server HOLDS for it (BACKEND_BUGS B-110): the stored
                        `weight` and `reps` — which may differ from the re-sent
                        copy — and the `personal_record` verdict the first answer
                        gave with the `previous_best` it gave beside it (both kept on
                        the set when it was judged, never recomputed: an earlier set
                        corrected or deleted since does not change what this record
                        beat; a key sent twice in one batch answers its twin's
                        verdict). Nothing is written and nothing is
                        celebrated again; what the set is counted in is recomputed
                        again, so a send that died before its recount is repaired by
                        the retry (SIDE_BUGS S-120)
            error       `{client_uuid, status, code, error}` — `bad_row`,
                        `client_uuid_required`, `idempotency_conflict` (the id is on
                        someone else's set — also when their send and this one raced:
                        nothing of theirs is answered), `bad_date`,
                        `date_in_future`, `date_too_old`, `exercise_required`,
                        `exercise_not_found`, `session_not_found`, `routine_not_found`,
                        `reps_required`, `reps_out_of_range`, `no_prescribed_reps`,
                        `weight_out_of_range`, `set_number_out_of_range`,
                        `rpe_out_of_range`, `rest_time_out_of_range` (a `rest_time`
                        the single POST refuses: not a whole number of seconds, or below 0),
                        `invalid_set`, or the coaching refusal
                        `coaching_frozen` / `coaching_unpaid` / `coaching_ended` /
                        `product_refunded` (with
                        `trainer_id`) — per row, because one frozen coaching must not
                        throw away the sets that belong to another trainer's routine.
                        `bad_date` also answers a date that does not exist
                        (`2026-02-30`): it failed the whole batch with a 500 until
                        2026-09-24.

        The whole-request answer is `{results, created, duplicates, errors, count}`.
        400 `sets_required` when the body carries no list at all. The response's
        `personal_record` is true on a set that beat everything logged before it, and
        the `personal_record` push follows, as on every other logging route. Only the
        heaviest new set of each exercise in the batch can claim it. When it is true,
        `previous_best` is `{weight, reps, date}` of the best it beat — the heaviest set
        written before it, among equal weights the most reps, then the latest; a
        lighter set earlier in the same batch counts (`strength.record_beaten`,
        BACKEND_BUGS B-112) — and null otherwise.

        Identical on every build; sets are the client's own, so there is no `user_id`.
        """
        from .models import suspend_progress_recalc, recalc_progress_for

        payload = request.data
        rows = payload if isinstance(payload, list) else (
            payload.get('sets') if hasattr(payload, 'get') else None)
        if not isinstance(rows, list):
            return Response({'error': _('Send `sets`: the list of sets to sync.'),
                             'code': 'sets_required', 'field': 'sets'},
                            status=status.HTTP_400_BAD_REQUEST)
        if len(rows) > MAX_SYNC_SETS:
            return Response({'error': _('A sync carries at most %(max)d sets.')
                             % {'max': MAX_SYNC_SETS},
                             'code': 'batch_too_large', 'max': MAX_SYNC_SETS},
                            status=status.HTTP_400_BAD_REQUEST)

        import uuid as _uuid

        today = timezone.localdate()
        # Every retry key of the batch read ONCE. A re-sent batch is the common case
        # (the write landed, the answer was lost), and it must not cost a query a row
        # to discover that it has nothing to do.
        keys = {}
        for index, row in enumerate(rows):
            raw = row.get('client_uuid') if isinstance(row, dict) else None
            try:
                keys[index] = (None if raw in (None, '') else
                               _uuid.UUID(int=raw) if isinstance(raw, int)
                               and not isinstance(raw, bool) else _uuid.UUID(str(raw)))
            except (ValueError, TypeError, AttributeError):
                keys[index] = None          # malformed: answered `client_uuid_required`
        # The session and its routine come along: a `duplicate` is recomputed too, scoped
        # to where it was logged (`_duplicate_touch`), and that must not cost a query a
        # row either.
        known = {log.client_uuid: log for log in ExerciseSetLog.objects
                 .filter(client_uuid__in=[k for k in keys.values() if k is not None])
                 .select_related('user_exercise_progress', 'workout_session__routine')}

        results, fresh = [], []
        # `(progress_pk, routine_pk, day)`: the recomputation runs ONCE per progress
        # row, not once per set — that is what `suspend_progress_recalc` is for, and
        # the scope is what stops one exercise deciding another routine's day (B-011).
        touched: dict = {}
        # Each set commits on its own, so the challenges it moves are recounted ONCE
        # for the whole sync when this block ends, not once per set (the review, M4).
        from social.challenge_progress import batched_recounts

        with batched_recounts():
            with suspend_progress_recalc():
                for index, row in enumerate(rows):
                    answer, touch, log = _sync_one_set(request, row, keys.get(index),
                                                       known, today)
                    results.append(answer)
                    if log is not None:
                        fresh.append(answer)
                        # So a second row of the same batch, or a re-send that arrives
                        # before the commit, sees it as written rather than writing again.
                        known[log.client_uuid] = log
                    if touch is not None:
                        progress, routine, day = touch
                        touched[(progress.pk, getattr(routine, 'pk', None), day)] = touch

            # Outside the suspension, or it would be muted along with the rest.
            for progress, routine, day in touched.values():
                recalc_progress_for(progress, routine_scope=routine, day_scope=day)

        # The heaviest new set of each exercise is the only one that can be a record
        # (`strength.is_personal_record`), the same as `bulk-create`. The verdict, and
        # the best a record beat (`previous_best`, B-112), go on the set's `created` row
        # AND on any `duplicate` row of the same set in this batch (a key sent twice):
        # one set, one verdict, in one answer (B-110).
        verdicts = {}
        for log in _heaviest_per_exercise(fresh):
            _celebrate_personal_record(log)
            verdicts[log.pk] = (bool(getattr(log, '_is_personal_record', False)),
                                getattr(log, '_previous_best', None))
        written_now = {answer['id'] for answer in fresh}
        for answer in results:
            if answer.get('id') in written_now and answer['status'] != 'error':
                answer['personal_record'], answer['previous_best'] = \
                    verdicts.get(answer['id'], (False, None))

        counts = {'created': 0, 'duplicate': 0, 'error': 0}
        for answer in results:
            counts[answer['status']] = counts.get(answer['status'], 0) + 1
        return Response({'results': results, 'count': len(results),
                         'created': counts['created'],
                         'duplicates': counts['duplicate'],
                         'errors': counts['error']})


def _prescription_payload(entry):
    """One row of a day's prescription, as `?expand=sets` answers it (B-106), from an
    entry of `_reviewed_plan`."""
    return {'id': entry['id'], 'sets': entry['sets'], 'reps': entry['reps'],
            'weight': None if entry['weight'] is None else float(entry['weight']),
            'rest_time': entry['rest_time'], 'order': entry['order']}


def _reviewed_plan(session, logs):
    """The day's plan a session is reviewed against, in the day's order: one entry per
    row, `{id, exercise_id, exercise, sets, reps, weight, rest_time, order, notes}`.

    **A closed day is read from its `DayOutcome`** — the plan frozen when the day closed
    (`outcomes.plan_from_detail`), matched by `(user, routine, day, date of the
    session's start)`, the key finishing a session writes it under. The coach edits
    `RoutineExercise` in place and an edit must not rewrite last week: read live, a
    raise to 5 × 6 at 90 kg the next morning made the review say "8 at 82.5 against 6
    at 90" and contradict the day's own outcome, and a row deleted since vanished from
    the plan (the review of 2026-09-24). `sets`, `reps`, `weight` and `order` are the
    snapshot's (`sets` as the day counted it: at least one); `rest_time` and `notes`,
    which the snapshot does not hold, are the live row's while it exists, and null and
    "" for a row deleted since. A row whose exercise has left the catalogue altogether
    has nothing left to name and is left out. **A day still open** — no outcome yet, or
    one written before the snapshot carried rows — is read live.

    Cost: ONE query — the live rows, with the outcome's snapshot as a subquery column.
    One more only when the day holds no live row any more (the outcome is then read on
    its own), and one more for the names of exercises the coach has deleted since that
    nobody logged in this session (the logged ones come with the sets). Never a query
    per set or per exercise.
    """
    from django.db.models import JSONField, Subquery

    from .models import DayOutcome, Exercise
    from .outcomes import plan_from_detail

    if not (session.routine_id and session.day):
        return []
    outcome = DayOutcome.objects.filter(
        user_id=session.user_id, routine_id=session.routine_id, day=session.day,
        date=timezone.localdate(session.start_time))
    live = list(RoutineExercise.objects
                .filter(routine_id=session.routine_id, day=session.day)
                .select_related('exercise')
                .annotate(closed_plan=Subquery(outcome.values('detail')[:1],
                                               output_field=JSONField()))
                .order_by('order', 'id'))
    detail = (live[0].closed_plan if live
              else outcome.values_list('detail', flat=True).first())
    frozen = plan_from_detail(detail)
    if not frozen:
        return [{'id': row.pk, 'exercise_id': row.exercise_id, 'exercise': row.exercise,
                 'sets': row.sets, 'reps': row.reps, 'weight': row.weight,
                 'rest_time': row.rest_time, 'order': row.order, 'notes': row.notes}
                for row in live]

    now_row = {row.pk: row for row in live}
    exercises = {row.exercise_id: row.exercise for row in live}
    for log in logs:
        progress = log.user_exercise_progress
        if progress is not None:
            exercises.setdefault(progress.exercise_id, progress.exercise)
    unnamed = {entry['exercise_id'] for entry in frozen} - set(exercises)
    if unnamed:
        exercises.update({exercise.pk: exercise
                          for exercise in Exercise.objects.filter(pk__in=unnamed)})
    plan = []
    for entry in frozen:
        exercise = exercises.get(entry['exercise_id'])
        if exercise is None:
            continue
        row = now_row.get(entry['routine_exercise_id'])
        plan.append({
            'id': entry['routine_exercise_id'], 'exercise_id': entry['exercise_id'],
            'exercise': exercise, 'sets': entry['sets'], 'reps': entry['reps'],
            'weight': entry['weight'],
            'rest_time': row.rest_time if row is not None else None,
            'order': (entry['order'] if entry['order'] is not None
                      else getattr(row, 'order', None)),
            'notes': row.notes if row is not None else '',
        })
    return plan


def _session_review(session, reader):
    """`{sets, planned, routine_name}` for `GET workout-sessions/{id}/?expand=sets`
    (BACKEND_BUGS B-106): what was done, set by set, beside what the day prescribed —
    the plan the day CLOSED with once it is closed, the live one while it is open
    (`_reviewed_plan`).

    The sets, their progress rows and exercises come from the prefetch the detail
    already made; the day's plan is ONE more query (see `_reviewed_plan` for the two
    rare cases that cost one more each). Nothing here is asked per set or per exercise.
    A set's prescription is the plan's first row for its exercise by `(order, id)` —
    the row `set-logs/` reads for `as_prescribed` — so the review and the logging agree
    about what "planned" was.
    """
    from .services import routine_name_for

    def exercise_of(log):
        progress = log.user_exercise_progress
        return progress.exercise if progress is not None else None

    logs = sorted(session.set_logs.all(), key=lambda log: log.pk)
    plan = _reviewed_plan(session, logs)
    prescription_of, position = {}, {}
    for index, entry in enumerate(plan):
        prescription_of.setdefault(entry['exercise_id'], entry)
        position.setdefault(entry['exercise_id'], (0, index))

    first_logged = {}
    for log in logs:
        exercise = exercise_of(log)
        first_logged.setdefault(getattr(exercise, 'pk', None), log.pk)

    def place(log):
        exercise_id = getattr(exercise_of(log), 'pk', None)
        return (position.get(exercise_id, (1, first_logged[exercise_id])),
                log.set_number is None, log.set_number or 0, log.pk)

    sets = []
    for log in sorted(logs, key=place):
        exercise = exercise_of(log)
        planned = prescription_of.get(getattr(exercise, 'pk', None))
        sets.append({
            'id': log.pk, 'set_number': log.set_number,
            'weight': None if log.weight is None else float(log.weight),
            'reps': log.reps, 'rest_time': log.rest_time, 'rpe': log.rpe,
            'notes': log.notes, 'date': log.date.isoformat() if log.date else None,
            'personal_record': bool(log.personal_record),
            # What the record beat, as stored with the verdict (`routine/0029`), and
            # the app's own key for the set (B-123): a phone that lost the answer to a
            # record recovers the «+x kg» and matches its set by its key from here.
            'previous_best': log.previous_best if log.personal_record else None,
            'client_uuid': str(log.client_uuid) if log.client_uuid else None,
            'exercise': (None if exercise is None
                         else {'id': exercise.pk, 'name': exercise.name}),
            'prescription': None if planned is None else _prescription_payload(planned),
        })
    return {
        'sets': sets,
        'planned': [{**_prescription_payload(entry),
                     'exercise': {'id': entry['exercise_id'],
                                  'name': entry['exercise'].name},
                     'notes': entry['notes']}
                    for entry in plan],
        'routine_name': (routine_name_for(session.routine, reader)
                         if session.routine_id else None),
    }


class WorkoutSessionViewSet(viewsets.ModelViewSet):
    """
    API endpoint for managing WorkoutSession objects.
    A client opens sessions on routines assigned to them; a trainer may open one for an
    approved client (`user` in the body). The owner PATCHes their session.

    **PATCH, PUT and DELETE belong to the person who trained, and to an admin**
    (SIDE_BUGS S-067). A trainer reads a client's sessions and answers 403
    `session_not_yours` on every write to one (the owner, 2026-09-24: it was the generic
    `permission_denied`) — the same rule as a client's sets, which a session holds: deleting
    it deletes every set logged in it, and finishing it closes the client's day. (A
    trainer's PATCH that also fails validation answers that 400 first, as it always
    has — `effort_not_yours` on `effort_rating`; nothing is written either way.)
    DELETE removes the session and the sets logged in it, and recomputes everything
    those sets were counted in, exactly as deleting each set would; it is refused with
    403 `coaching_frozen` / `coaching_unpaid` / `coaching_ended` / `product_refunded` while the coaching is
    unpaid or over, judged at the session's start.

    **`status` only moves forward** (SIDE_BUGS S-122): `active` may become `completed`
    or `abandoned`; `completed` and `abandoned` are final, and a PATCH that changes
    either answers 400 `session_status_final` on `status` (re-sending the status the
    row already holds is a no-op, so a PUT of the whole object still works). Reopening
    a finished workout re-ran everything finishing it does — the client and the coach
    were told twice — and finishing an abandoned one invented a twelve-hour workout.

    While the coaching a routine came from is unpaid (frozen) or over, the routine stays
    readable and opening or editing a session on it answers 403 `coaching_frozen` /
    `coaching_unpaid` / `coaching_ended` / `product_refunded` with `trainer_id` (the owner, 2026-09-19). A
    session begun while
    paid can still be finished, and an offline one dated inside a paid month still syncs.

    `effort_rating` (1 easy — 5 all I had, null while unanswered) may be sent on create,
    on the PATCH that completes the session, and on any later PATCH of the session
    detail — a client often rates a workout the next morning. ONLY the person who
    trained may set it (400 `effort_not_yours` on the field otherwise, including for
    their trainer and for an admin), and it must be inside 1-5 (400
    `effort_out_of_range`; the database carries the same bound as a check constraint).
    It feeds `progression` on `analytics/strength/` and on the coach's client screen.

    **The records of a finished workout** (BACKEND_DELTA item 12; the completion
    screen's record card). The answer that FINISHES a session — the PATCH or PUT that
    moves `status` to `completed`, or the POST that creates one already completed (an
    offline sync) — carries `personal_records`; no other answer does (not a re-sent
    finish of a finished session, not a replayed POST, not a read). Each entry is one
    exercise that set a personal record in THIS session (sets logged with
    `workout_session` = this session), in the order they set it; `[]` when none did::

        "personal_records": [{"exercise": {"id": 3, "name": "Bench press"},
                              "set_log_id": 91, "weight": 82.5, "reps": 8,
                              "date": "2026-09-19",
                              "previous_best": {"weight": 80.0, "reps": 8,
                                                "date": "2026-09-12"} | null,
                              "gain_kg": 2.5 | null}]

    Nothing is judged at the finish: a set is a record exactly when its own logging
    answer said `personal_record: true`, and `previous_best` is what that answer said
    it beat (`routine/strength.py session_records`). Per exercise the entry is the
    session's heaviest record set, and `previous_best` is the best that stood before the
    session's first record of it, so two records in one workout read as one gain over
    last time. `gain_kg` is `weight` less `previous_best.weight`, null when the best is
    unknown (a record kept before 2026-09-24). The key is absent if the records could
    not be read; the workout is finished either way. Identical on every build.

    **A workout that is still running** is `active` AND begun within the last twelve
    hours (`routine/sessions.py SESSION_MAX_SECONDS`). Past that it was forgotten, not
    left running: `?status=active` does not list it, `home/` does not offer to continue
    it, and the nightly `routine.abandon_forgotten_sessions` writes `abandoned` on the
    row so the column agrees. Nothing happens to the sets logged inside it — they stay,
    and stay counted (see that task's docstring). `?status=abandoned` lists the ones
    the sweep has already reached.

    The list is paginated: `{count, next, previous, results}`, `?page_size=` up to 100,
    newest first: by `start_time`, then by `id`, so the order is total and a row never
    repeats or goes missing between pages (BACKEND_BUGS B-067).

    **One read for the coach's review** (BACKEND_BUGS B-106): `GET
    workout-sessions/{id}/?expand=sets` adds the session's sets inline, each beside the
    day's prescription — see `retrieve`. Without `expand` the answer is unchanged, and
    the list is never expanded.

    Input: API requests with session data (POST), session updates (PATCH)
    Output: JSON responses with session data
    """
    #: What `?expand=` may name on the detail (B-106).
    EXPANSIONS = ('sets',)
    queryset = WorkoutSession.objects.select_related(
        'user', 'routine', 'routine__created_by'
    ).prefetch_related(
        'set_logs',
        'set_logs__user_exercise_progress__exercise'
    )
    serializer_class = WorkoutSessionSerializer
    # Explicit, like every other list in this app: the project default paginates too,
    # but this one answers a bad `?page=` with 404 where the rest answer 400 naming the
    # parameter, so a client could not write one handler for a paging error (S-032).
    pagination_class = StandardResultsSetPagination
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
        # Newest first, and a TOTAL order: the list is paged by page number, and two
        # sessions begun in the same second must not swap between pages (BACKEND_BUGS
        # B-067). Said here rather than left to `Meta.ordering`, which is what it rested
        # on; `workoutsess_recent_idx (user, -start_time, -id)` serves exactly this.
        ordered = self.queryset.order_by('-start_time', '-id')
        if allowed_ids is None:  # admin
            base = ordered
        else:
            base = ordered.filter(user_id__in=allowed_ids)
        # "Is a workout running" has one answer in this platform, and it is not
        # `status='active'` on its own (SIDE_BUGS S-022): a row the client walked away
        # from reads `active` until the nightly sweep reaches it. Asked for the running
        # ones, answer the running ones. Only on the list — a detail GET carrying a
        # stale `?status=` must not turn into a 404.
        if self.action == 'list' and self.request.query_params.get('status') == 'active':
            from .sessions import running
            return running(base)
        return base

    def retrieve(self, request, *args, **kwargs):
        """GET one session: the fields of the list row, unchanged.

        **`?expand=sets`** (BACKEND_BUGS B-106) — the trainer's review in one read. The
        answer gains three keys and changes none:

            sets          every set logged in this session, grouped by exercise in the
                          order the day prescribes them (exercises the day does not
                          prescribe after, in the order they were first logged), and by
                          `set_number` within one. Each:
                          `{id, set_number, weight, reps, rest_time, rpe, notes, date,
                            personal_record, previous_best, client_uuid,
                            exercise: {id, name}, prescription}` —
                          what was DONE, as stored. `personal_record` is the verdict the
                          client was answered with when the set was logged (kept on the
                          set; false on sets logged before 2026-09-24). `previous_best`
                          is `{weight, reps, date}` of the best that record beat, as
                          stored with the verdict and never recomputed — exactly what
                          the logging answer said — and null when the set is not a
                          record (and on records kept before the column existed).
                          `client_uuid` is the app's own key for the set, the one it
                          sent when logging it, or null for a set logged without one
                          (BACKEND_BUGS B-123). `exercise` is
                          null for a legacy set with no progress row.
                          `prescription` is the day's row for that exercise — `{id,
                          sets, reps, weight, rest_time, order}`, the first by `order`
                          when the day holds it twice, the row logging reads — or null
                          when the day does not prescribe it (or the session names no
                          routine day).
            planned       the whole day's prescription, in order, the exercises nobody
                          touched included: `{id, exercise: {id, name}, sets, reps,
                          weight, rest_time, order, notes}`. Empty without a day.
            routine_name  the routine's name in the reader's language.

        **Which plan.** Once the day is CLOSED (it has a day outcome: finishing the
        session writes one), `planned` and every `prescription` are the plan the day
        closed with — the same frozen plan the day's outcome counts against — so the
        coach editing the routine afterwards does not rewrite what this workout was
        asked to be (the review of 2026-09-24). `sets`, `reps`, `weight` and `order`
        are as they were then (`sets` as the day counted it: at least one), and a row
        the coach has deleted since is still listed, with its old `id`; `rest_time` and
        `notes` are the row's current ones, and null and "" for a deleted row. A day
        still OPEN is answered against the routine as it is now.

        Exercise names are in the request's language. The cost does not grow with the
        sets or the exercises: the sets, their progress rows and exercises are read in
        the three queries the detail already made, and the day's plan in one more —
        plus one when the day holds no row any more, and one when a row deleted since
        names an exercise nobody logged in this session.

        Who may read it is who may read the session: the person who trained, an admin,
        and a trainer with an APPROVED relation to them — the one predicate
        (`accessible_user_ids` / `can_access_user_data`); anyone else gets 404
        `not_found`, as for the plain detail. The `assigned_trainer` pointer grants
        nothing.

        `expand` is a comma-separated list; a name other than `sets` answers 400
        `bad_expand` with `field: "expand"`. An empty `expand` is no expansion. Identical
        on every build.
        """
        raw = request.query_params.get('expand') or ''
        wanted = {name.strip() for name in raw.split(',') if name.strip()}
        unknown = sorted(wanted - set(self.EXPANSIONS))
        if unknown:
            return Response(
                {'error': _('Cannot expand %(names)s. What can be expanded: %(known)s.')
                 % {'names': ', '.join(unknown), 'known': ', '.join(self.EXPANSIONS)},
                 'code': 'bad_expand', 'field': 'expand'},
                status=status.HTTP_400_BAD_REQUEST)
        instance = self.get_object()
        data = self.get_serializer(instance).data
        if 'sets' in wanted:
            data = {**data, **_session_review(instance, request.user)}
        return Response(data)

    def create(self, request, *args, **kwargs):
        """POST: open a session. Body: `routine`, `day`, optional `client_uuid` (the
        app's id: a retry answers 200 with the first row; someone else's id answers 409
        `idempotency_conflict`) and optional `start_time` for a workout logged offline
        (at most 30 days back: `start_time_in_future`, `start_time_too_old`); `end_time`
        is then checked against it. Field codes (400): `routine_not_yours` on `routine`
        for a routine neither yours nor assigned to you (the owner, 2026-09-24; it was
        the generic `invalid`), `routine_inactive` on `routine`,
        `day_out_of_range` / `day_has_no_exercises` on `day` — here, and on a PATCH that
        changes the routine or the day (a status-only PATCH is not checked).

        A trainer opening a session for someone (`user` in the body) who is not their
        approved client answers 403 `not_your_client` (the owner, 2026-09-24; it was the
        generic `permission_denied`)."""
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
                    raise PermissionDenied(_('You may only create sessions for your approved clients.'),
                                           code='not_your_client')
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
        # Only the person who trained, or an admin, writes a session (SIDE_BUGS S-067).
        # Refused HERE rather than in the permission class so that a trainer's edit
        # still answers the field codes it always has (`effort_not_yours`) first —
        # either way nothing of theirs is written.
        from .permissions import IsSessionOwnerOrTrainerOrAdmin, is_session_writer
        if not is_session_writer(self.request.user, instance):
            # The code and sentence the DELETE answers from the permission class: one
            # meaning, one code (the owner, 2026-09-24).
            raise PermissionDenied(IsSessionOwnerOrTrainerOrAdmin.message,
                                   code=IsSessionOwnerOrTrainerOrAdmin.code)
        _require_can_train(self.request.user, instance.routine, instance.start_time)
        serializer.save()

    def perform_destroy(self, instance):
        """Deleting a workout deletes the sets logged in it (the foreign key cascades),
        so it is refused exactly as deleting those sets is: only the owner or an admin
        reaches here (`IsSessionOwnerOrTrainerOrAdmin`), and a frozen or ended coaching
        refuses it, judged at the session's start (SIDE_BUGS S-067). Asked about the
        CALLER, as `ExerciseSetLogViewSet._refuse_if_frozen` asks: an admin clearing up a
        bad row is not caught by a client's unpaid month.

        Each deleted set is recomputed by the `post_delete` receiver in
        `routine/models.py`, which runs for a cascade too; nothing is duplicated here."""
        _require_can_train(self.request.user, instance.routine, instance.start_time)
        instance.delete()

    def update(self, request, *args, **kwargs):
        """PATCH or PUT: the client marks the session completed, and finishing it
        notifies and records the day. `partial_update` delegates here, so a PUT that
        finishes a session is treated exactly like a PATCH — it was not. The answer that
        finishes it carries `personal_records` (see the class docstring); only that one.
        """
        # Was it already finished BEFORE this request? The branch below keyed on
        # "the payload says completed", not on a transition, so a double-tapped finish
        # button congratulated the client twice, told the trainer twice, and queued the
        # milestone walk twice.
        was_completed = self.get_object().status == 'completed'
        response = super().update(request, *args, **kwargs)
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
        # The ids both sides' pages open with (Road to 10 S3.1): the trainer's review is
        # `workout-sessions/{session_id}/?expand=sets` on the client's card.
        opens = {'session_id': instance.pk, 'client_id': client.pk,
                 'routine_id': instance.routine_id}
        # Each row also names its sentence (`variant`) and params, so the inbox reads it
        # in the reader's language and a push in the saved one (Road to 10, notification
        # language P1, `notifications/domain/routine_events.py`).
        mine = {'routine': routine_name_for(instance.routine, client)}
        send_notification(
            user=client,
            notif_type="session_completed",
            message=in_recipients_language(client, lambda: _(
                "Well done! You finished a workout on %(routine)s.") % mine),
            related_object=instance, data=opens,
            context={'variant': 'client', **mine},
        )
        # Notify the trainer this client actually has an APPROVED relation with.
        # Reading `assigned_trainer` would tell a dismissed trainer what their former
        # client trained today, which is the client's data, not theirs.
        from users.models import TrainerClientRelation
        for trainer in (t.trainer for t in TrainerClientRelation.objects
                        .filter(client=instance.user, status='approved')
                        .select_related('trainer')):
            # The client's name, not their username, in the trainer's language.
            theirs = {'name': f"{client.first_name or ''} {client.last_name or ''}".strip()
                              or client.username,
                      'routine': routine_name_for(instance.routine, trainer)}
            send_notification(
                user=trainer,
                notif_type="session_completed",
                message=in_recipients_language(trainer, lambda: _(
                    "%(name)s finished a workout on %(routine)s.") % theirs),
                related_object=instance, data=opens,
                context={'variant': 'trainer', **theirs},
            )

        # Milestones. Deferred to on_commit so a rolled-back completion cannot
        # congratulate anyone, and pushed to the worker because computing a streak
        # walks backwards day by day through the activity log.
        from django.db import transaction

        _user_id = instance.user_id
        transaction.on_commit(lambda: _queue_milestones(_user_id))

        # The day this workout belongs to is closed: keep what it planned against what
        # was done (the owner, 2026-09-20). Dated by the session's own start, so a
        # workout synced late is recorded on the day it happened.
        if instance.routine_id and instance.day:
            from .outcomes import record_day_quietly
            record_day_quietly(instance.user, instance.routine, instance.day,
                               timezone.localdate(instance.start_time), via='session',
                               session=instance)

        # The completion screen's record card (BACKEND_DELTA item 12): the records set
        # in this session, as their logging answers gave them. On THIS answer only — the
        # one that finished the workout. A failure costs the card, never the finish: it
        # is logged and the key is left out, which the app reads as "no card".
        from .strength import session_records
        try:
            response.data['personal_records'] = session_records(instance)
        except Exception:       # noqa: BLE001 — the workout is finished whatever happens
            logger.warning("could not read the records of session %s", instance.pk,
                           exc_info=True)


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


def _user_not_accessible():
    """403 `user_not_accessible`: a `user_id` the caller may not read — not themselves,
    not an approved client of theirs, and they are not an admin. One code for the six
    analytics actions, which answered the generic `permission_denied` (the owner,
    2026-09-24)."""
    return Response({'error': _('You do not have access to this user\'s data.'),
                     'code': 'user_not_accessible'},
                    status=status.HTTP_403_FORBIDDEN)


def _user_not_found():
    """404 `user_not_found`: a `user_id` the caller may read names nobody (only an admin
    can get here — anyone else is answered `user_not_accessible` first). The code
    `social/` and `moderation/` answer for a missing account."""
    return Response({'error': _('User not found'), 'code': 'user_not_found'},
                    status=status.HTTP_404_NOT_FOUND)


class AnalyticsViewSet(viewsets.ViewSet):
    """
    Analytics endpoints for user training data.
    Provides summary stats: total volume, PRs, workout frequency.
    Input: GET params period=week|month, user_id (optional, trainer/admin only)
    Output: JSON summary of analytics

    Refusals, the same on every action that takes them (every refusal names its own
    code — the owner, 2026-09-24; those marked new answered `bad_request`,
    `permission_denied` or `not_found`):

    | Status | `code` | When |
    |---|---|---|
    | 400 | `bad_period` (new on `summary` and `trends`) | `period` is not one the action knows |
    | 400 | `bad_exercise_id` | `strength`: `exercise_id` not a whole number |
    | 403 | `user_not_accessible` (new) | `user_id` is not you, not an approved client of yours, and you are not an admin |
    | 404 | `user_not_found` (new) | `user_id` names nobody (an admin's question) |
    | 404 | `not_found` | `strength`: `exercise_id` names no exercise you may read — the same answer as a missing one |
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

        `volume_change_percent` compares this window's volume with the window of the
        same length just before it, to one decimal. It is NULL when that earlier window
        holds no volume — a first week, month or year has nothing to be compared with,
        and is not a +100% (BACKEND_BUGS B-068; it answered the constant 100.0 there).
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
                {'error': _('period must be one of: week, month, year.'), 'code': 'bad_period'},
                status=status.HTTP_400_BAD_REQUEST)
        user_id = request.query_params.get('user_id')
        user = request.user
        
        if user_id:
            # AUTHORIZATION: a caller-supplied user_id must be self, an approved
            # client (trainer), or admin. Without this any authenticated account
            # could read any other user's training data.
            from .permissions import can_access_user_data
            if not can_access_user_data(request.user, user_id):
                return _user_not_accessible()
            try:
                from users.models import CustomUser
                user = CustomUser.objects.get(pk=user_id)
            except CustomUser.DoesNotExist:
                return _user_not_found()

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
        
        # A change FROM NOTHING is not a percentage. It was the constant 100.0 whenever
        # the previous window was empty, so a first month read exactly like a real
        # doubling and the app could only hide the chip on 100.0, the genuine one
        # included (BACKEND_BUGS B-068). Null says "nothing to compare with".
        volume_change = None
        if prev_volume > 0:
            volume_change = round(((total_volume - prev_volume) / prev_volume) * 100, 1)

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
                return _user_not_accessible()
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
        """GET /api/routine/analytics/streaks/?user_id= — the training-day streak.

        A day counts only when nothing was missed (the owner, 2026-09-20): closing a day
        with sets still unlogged, or skipping it, BREAKS the streak, and `last_break`
        points at the day that broke it (`date`, `routine_id`, `day`, `missed_sets`,
        `reason`: `sets_missed` | `skipped`). Days closed before this was recorded fall
        back to their status, and `approximate` says so.

        Answers `{user_id, current_streak, max_streak, last_training_date, last_break,
        approximate}`.
        """
        from .outcomes import last_break, streak_from, training_dates
        from users.models import CustomUser
        user_id = request.query_params.get('user_id')
        user = request.user
        if user_id:
            # AUTHORIZATION: a caller-supplied user_id must be self, an approved
            # client (trainer), or admin. Without this any authenticated account
            # could read any other user's training data.
            from .permissions import can_access_user_data
            if not can_access_user_data(request.user, user_id):
                return _user_not_accessible()
            try:
                user = CustomUser.objects.get(pk=user_id)
            except CustomUser.DoesNotExist:
                return _user_not_found()

        # Streaks are computed over DISTINCT calendar training days, using the
        # real `date` field. It previously used `updated_at` (auto_now), so editing
        # an old record moved that workout to today.
        # Days closed before outcomes were recorded have no row to judge: their status
        # is all there is, and a day that was closed short back then still reads as
        # completed. Those days keep counting, and the answer says it is approximate.
        # `training_dates` is the one definition; `adherence` answers the same streak.
        training_days, approximate = training_dates(user)
        current_streak, longest_streak = streak_from(training_days)
        ordered = sorted(training_days)
        return Response({
            'user_id': user.id,
            'current_streak': current_streak,
            'max_streak': longest_streak,
            'last_training_date': ordered[-1] if ordered else None,
            'last_break': last_break(user),
            'approximate': approximate,
        })

    @action(detail=False, methods=['get'])
    def adherence(self, request):
        """GET /api/routine/analytics/adherence/?period=week|month|year&user_id= — how
        much of the plan was actually done (the owner, 2026-09-20).

        Self, an approved client of yours, or admin (403 `user_not_accessible`
        otherwise, 404 `user_not_found` for no such user). Answers `{from, to,
        days_closed, perfect_days, short_days, skipped_days,
        sets: {planned, logged, missed, done_percent}, reps: {planned, logged, missed},
        volume_kg: {planned, logged}, perfect_streak: {current, longest}, last_break,
        approximate, by_day: [...]}`.

        `reps` and `volume_kg` are null where the coach prescribed none, and reps are
        left out of the totals for a day logged a whole day at a time (no set is
        recorded, so its reps are unknown) — `approximate` is true when that happened or
        when a day was filled in afterwards. Identical on every build.

        A day of the free first workout (Road to 10 S2.4, `/api/routine/trial-workout/`)
        is in none of these numbers and not in `by_day`: it is the person's history
        (`workout-sessions/`), not training against a plan — for them or their coach.
        """
        from datetime import timedelta

        from .outcomes import adherence
        from .serializers import DayOutcomeSummarySerializer
        from users.models import CustomUser

        user = request.user
        user_id = request.query_params.get('user_id')
        if user_id:
            from .permissions import can_access_user_data
            if not can_access_user_data(request.user, user_id):
                return _user_not_accessible()
            user = CustomUser.objects.filter(pk=user_id).first()
            if user is None:
                return _user_not_found()

        period = (request.query_params.get('period') or 'week').lower()
        spans = {'week': 7, 'month': 30, 'year': 365}
        if period not in spans:
            return Response({'error': _('period must be one of: %(options)s')
                             % {'options': ', '.join(spans)}, 'code': 'bad_period'},
                            status=status.HTTP_400_BAD_REQUEST)
        today = timezone.localdate()
        start = today - timedelta(days=spans[period] - 1)

        from .models import DayOutcome
        body = adherence(user, start, today, today=today)
        body['user_id'] = user.id
        body['period'] = period
        # Without `detail`: a year of days would otherwise carry every prescribed
        # exercise of every day in one response. The per-day rows are on
        # `routine-progress/`, which answers `outcome` per day.
        # `.defer('detail')`, not "serialise then pop": a year of days would otherwise
        # read every prescribed exercise of every day out of the database and render it
        # only to throw it away. The per-day rows are on `routine-progress/`. And a
        # serializer that does not NAME the column, or rendering it loads it back one
        # row at a time (SIDE_BUGS S-072).
        # The days the totals above count, and no others: the free first workout is
        # not one of them (Road to 10 S2.4, `outcomes._counted`).
        from .outcomes import _counted
        body['by_day'] = DayOutcomeSummarySerializer(
            _counted(DayOutcome.objects.filter(user=user, date__gte=start, date__lte=today))
            .defer('detail').order_by('date', 'id'), many=True).data
        return Response(body)

    @action(detail=False, methods=['get'])
    def strength(self, request):
        """GET /api/routine/analytics/strength/?exercise_id=&user_id= — how strong this
        person is getting, and where that is heading (the owner, 2026-09-20).

        Self, an approved client of yours, or admin (403 `user_not_accessible`
        otherwise, 404 `user_not_found` for no such user). Read from sets already
        logged; nothing here is collected separately.

        Without `exercise_id`: `{exercises: [...], top_movers, plateaus, rising_count,
        tracked_count, headline, adherence_percent}` — one entry per exercise trained in
        the last 8 weeks, each with `direction` (`rising` | `steady` | `easing`),
        `change_percent`, `best`, `latest`, `sessions`, `top_set` and its `projection`.

        With `exercise_id`: the same for one exercise plus `points` (one entry per
        training day: `estimated_1rm`, `best_weight`, `best_reps`, `volume_kg`, `sets`,
        `top_set`), `share` — the headline a card can draw — and `progression`.

        `top_set` is «أفضل مجموعة» (the owner, F3, 2026-09-24): the set with the highest
        weight × reps, among equals the heavier weight; only sets with both a weight and
        reps compete, a set longer than 12 reps included. On a point it is that day's
        `{weight, reps, volume_kg}` (null when no set of the day has both). On an overview
        row, and at the top of the one-exercise answer, it is the best such set of the
        8-week window with its `date` (among equal sets the most recent day), or null.
        It is not `best` (the heaviest set) and not `best_weight`/`best_reps` (the set
        that decides the one-rep max), whose meanings are unchanged. An `exercise_id`
        that is SENT is always read as one (BACKEND_BUGS B-073): not a whole number is
        400 `bad_exercise_id` with `field`; a whole number that names no exercise this
        caller may read — 0 and negatives included, and another trainer's private one —
        is 404 `not_found`. Only an absent or empty `exercise_id` answers the overview.

        `progression` is what to change next: `{code, effort: {typical,
        rated_sessions}, adherence_percent, direction, sessions, latest, next, needs}`.
        `code` is one of `add_weight`, `add_reps`, `hold`, `deload`,
        `not_enough_data` — the closed set, and the rule behind each one is written out
        in `routine/progression.py`. `next` carries the one number the suggestion means
        (`{weight_kg}` or `{reps}`) and is null for `hold` and `not_enough_data`;
        `needs` says what is still missing and is null otherwise. It reads the
        `effort_rating` the client gave their recent sessions; without any rating the
        answer is `not_enough_data`. No health claim, and identical on every build.

        `projection` is `{available: false, reason: "insufficient_data", needs: {...}}`
        until there are 3 training days of that exercise spread over 2 weeks. When it is
        available it is a RANGE — `from_estimated_1rm`, `low`, `high`, `horizon_days`
        (14), `confidence`, `per_week_kg`, `assumes_sets_done_percent` — never a single
        number and never a promise. It says what the training says: no calories, no
        weight, no muscle, no medical claim. Identical on every build.
        """
        from datetime import timedelta

        from . import strength as strength_module
        from .outcomes import adherence
        from users.models import CustomUser

        user = request.user
        user_id = request.query_params.get('user_id')
        if user_id:
            from .permissions import can_access_user_data
            if not can_access_user_data(request.user, user_id):
                return _user_not_accessible()
            user = CustomUser.objects.filter(pk=user_id).first()
            if user is None:
                return _user_not_found()

        today = timezone.localdate()
        # What the projection assumes: the share of PLANNED sets actually done over the
        # last four weeks (routine/outcomes.py). It conditions the answer instead of
        # decorating it — this is the part the person controls.
        done_percent = adherence(user, today - timedelta(days=27), today,
                                 today=today)['sets']['done_percent']

        # PRESENT is what decides, not truthy (BACKEND_BUGS B-073): `?exercise_id=0` was
        # falsy and answered the whole overview as if no id had been sent. An id that is
        # not a whole number is 400; a whole number that names no exercise you may read —
        # 0 and negatives included — is the 404 every missing id answers.
        raw_exercise_id = request.query_params.get('exercise_id')
        exercise_id = None
        if raw_exercise_id not in (None, ''):
            exercise_id = _int_or_none(raw_exercise_id)
            if exercise_id is None:
                return Response(
                    {'error': _('exercise_id must be a whole number.'),
                     'code': 'bad_exercise_id', 'field': 'exercise_id'},
                    status=status.HTTP_400_BAD_REQUEST)
        if exercise_id is not None:
            # The same rule as every other endpoint that takes an exercise id
            # (`routine.permissions.visible_exercises_for`): another trainer's private
            # exercise is answered exactly like an id that does not exist, so no name
            # can be read by enumeration. One addition, because this endpoint answers
            # about a PERSON and not about the catalogue: an exercise this person has
            # actually trained is theirs to read whatever became of the routine it came
            # from — they lifted it, the name is not news to them, and the id cannot be
            # guessed into a name because the history has to exist.
            from .models import Exercise, UserExerciseProgress
            from .permissions import visible_exercises_for
            exercise = visible_exercises_for(request.user).filter(pk=exercise_id).first()
            if exercise is None and UserExerciseProgress.objects.filter(
                    user=user, exercise_id=exercise_id).exists():
                exercise = Exercise.objects.filter(pk=exercise_id).first()
            if exercise is None:
                # Named, not left to the envelope (the owner, 2026-09-24): "no such
                # exercise, or not one you may read" is exactly `not_found`.
                return Response({'error': _('Exercise not found.'), 'code': 'not_found'},
                                status=404)
            series, top_set = strength_module.series_and_top_set(user, exercise_id, until=today)
            body = strength_module.trend(user, exercise_id, until=today, series=series)
            body['exercise_name'] = exercise.name
            body['top_set'] = top_set
            body['projection'] = strength_module.projection(
                user, exercise_id, until=today, series=series, adherence_percent=done_percent)
            body['share'] = strength_module.headline(body)
            # What to change next, from the effort ratings this person gave their last
            # sessions read together with the trend and how much of the plan was done
            # (BACKEND_DELTA item 11). A code and its numbers; never a sentence.
            from . import progression as progression_module
            body['progression'] = progression_module.for_exercise(
                user, exercise_id, trend_summary=body, adherence_percent=done_percent,
                until=today)
            body['user_id'] = user.id
            body['adherence_percent'] = done_percent
            return Response(body)

        body = strength_module.overview(user, until=today, adherence_percent=done_percent)
        body['headline'] = strength_module.headline(
            body['top_movers'][0] if body['top_movers']
            else (body['exercises'][0] if body['exercises'] else None))
        body['user_id'] = user.id
        body['adherence_percent'] = done_percent
        return Response(body)

    @action(detail=False, methods=['get'])
    def trends(self, request):
        """
        Get training volume and completion trends over time (per day/week/month).
        Input: user_id (optional), period=day|week|month
        Output: `{volume_trend: [{period, total_volume}], completion_trend: [{period,
        completed, total}]}`. `period` is a DATE (`YYYY-MM-DD`, the first day of the
        week or month) in both lists, and both are bucketed by the day the training
        happened. `completion_trend` was bucketed by when its row was last written and
        answered a datetime (SIDE_BUGS S-123).
        """
        from .models import ExerciseSetLog, RoutineProgress
        from django.db.models.functions import TruncDay, TruncWeek, TruncMonth
        user_id = request.query_params.get('user_id') or request.user.id
        # AUTHORIZATION: same rule as the other analytics actions — a supplied
        # user_id must be self, an approved client, or admin.
        from .permissions import can_access_user_data
        if not can_access_user_data(request.user, user_id):
            return _user_not_accessible()
        period = request.query_params.get('period', 'day')
        if period not in ('day', 'week', 'month'):
            return Response(
                {'error': _('period must be one of: day, week, month.'), 'code': 'bad_period'},
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
        # Completion trend, bucketed by the TRAINING date (SIDE_BUGS S-123). It used
        # `updated_at`, which is `auto_now`: a day's completion landed on whatever day its
        # row was last touched — a later sync, a recalculation — which is exactly why the
        # model says `updated_at` is useless as a training date.
        rps = RoutineProgress.objects.filter(user_id=user_id)
        if period == 'week':
            rps = rps.annotate(period=TruncWeek('date'))
        elif period == 'month':
            rps = rps.annotate(period=TruncMonth('date'))
        else:
            rps = rps.annotate(period=TruncDay('date'))
        completion_trend = rps.values('period').annotate(
            completed=Count('id', filter=Q(status='completed')),
            total=Count('id')
        ).order_by('period')
        return Response({'volume_trend': list(volume_trend), 'completion_trend': list(completion_trend)})

    @action(detail=False, methods=['get'])
    def admin_dashboard(self, request):
        """
        Admin/trainer dashboard: summary stats for the caller's clients (a trainer's
        approved clients; every client for an admin; never the caller — a trainer was a
        row of their own list, S-172). 403 `not_a_trainer` for anyone else (the owner,
        2026-09-24; it was the generic `permission_denied`).

        Output: `{dashboard: [{client_id, username, total_volume, completion_rate,
        max_streak}], count, next, previous}` — PAGINATED over clients, by id, 25 a page,
        `?page=` / `?page_size=` up to 100 (400 naming the parameter when either is
        bad). `dashboard` is the key it has always been; `count`, `next` and `previous`
        are new. For an admin the list was every client on the platform in one
        response, and the aggregates were computed for all of them (SIDE_BUGS S-124).
        """
        from .models import RoutineProgress, ExerciseSetLog
        from users.models import CustomUser
        from django.db.models import Q, Count, Sum, F, Max
        from django.utils import timezone
        from datetime import timedelta
        
        if not (request.user.is_trainer or request.user.is_admin):
            return Response({'error': _('Permission denied.'), 'code': 'not_a_trainer'},
                            status=403)

        # The CLIENTS: a trainer's approved ones, every one for an admin. Never the
        # caller: `accessible_user_ids` always holds the caller's own id (it answers
        # "whose training data may I read"), so a trainer was a row of their own client
        # list (the fix run's S-172). And an admin who is also a trainer got None from
        # it, read as "nobody", and saw an empty list.
        ids = accessible_user_ids(request.user)
        clients = CustomUser.objects.filter(user_type='client')
        if ids is not None:
            clients = clients.filter(id__in=ids)

        # Nothing is prefetched: every figure below is an aggregate with its own query.
        # `exercise_progress__set_logs` was prefetched here and never read (~36k
        # progress rows and ~110k set logs for twenty clients), and so was
        # `routine_progress` after it — for an admin, every RoutineProgress row on the
        # platform loaded to build a list that never looks at one (SIDE_BUGS S-124).
        # A page of clients, by id, and the aggregates for that page only.
        paginator = StandardResultsSetPagination()
        page = paginator.paginate_queryset(clients.order_by('id').only('id', 'username'),
                                           request, view=self)
        clients = list(page) if page is not None else list(clients.only('id', 'username'))
        client_ids = [client.id for client in clients]
        
        # Calculate date range for volume (last 30 days)
        thirty_days_ago = timezone.localdate() - timedelta(days=30)
        
        # Bulk aggregate volume data for all clients
        volume_data = ExerciseSetLog.objects.filter(
            user_exercise_progress__user_id__in=client_ids,
            date__gte=thirty_days_ago
        ).values('user_exercise_progress__user').annotate(
            total_volume=Sum(F('weight') * F('reps'))
        )
        
        # Create volume lookup dictionary
        volume_lookup = {item['user_exercise_progress__user']: item['total_volume'] for item in volume_data}
        
        # Bulk aggregate completion data for all clients
        completion_data = RoutineProgress.objects.filter(
            user_id__in=client_ids
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
        # And a day closed with sets still unlogged is not a training day at all (the
        # owner, 2026-09-20), so those dates are dropped here too — the console must not
        # contradict the client's own streak.
        from .models import DayOutcome
        from .outcomes import STREAK_WINDOW_DAYS
        from django.db.models import Count as _Count
        # The same window the client's own streak uses: a console page must not read
        # every outcome and every progress row this platform has ever written.
        streak_window = timezone.localdate() - timedelta(days=STREAK_WINDOW_DAYS)
        # And without the free first workout, as the client's own streak is (Road to 10
        # S2.4, `outcomes._counted`): neither its outcomes nor its progress rows.
        from .outcomes import _counted
        broken = set()
        for row in (_counted(DayOutcome.objects.filter(user_id__in=client_ids,
                                                       date__gte=streak_window))
                    .values('user_id', 'date')
                    .annotate(complete=_Count('id', filter=Q(complete=True)))
                    .filter(complete=0)):
            broken.add((row['user_id'], row['date']))

        streak_lookup = {}
        streak_rows = [row for row in (_counted(RoutineProgress.objects
                       .filter(user_id__in=client_ids, status='completed',
                               date__gte=streak_window))
                       .values_list('user_id', 'date')
                       .order_by('user_id', 'date')
                       .distinct()) if row not in broken]
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
        
        if page is not None:
            # The project envelope, with the rows under the key they have always had.
            return Response({'dashboard': dashboard, 'count': paginator.page.paginator.count,
                             'next': paginator.get_next_link(),
                             'previous': paginator.get_previous_link()})
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


class RoutineTemplateViewSet(ExerciseMediaMixin, viewsets.ModelViewSet):
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

    **The platform's starter templates** (Road to 10 S2.10, the owner's D16): public
    templates the PLATFORM wrote (`by_platform: true`, `created_by` null), one per goal
    (`build_muscle`, `strength`, `fat_loss`, `rehab`) and place (`home`, `gym`), named in
    Arabic and English. Every trainer lists them in `public_templates/` and copies one
    into their own library with `{id}/copy/` («ابدأ من قالب جاهز»). Written onto a new
    server by `manage.py bootstrap_platform` (`routine/data/platform_templates.py`).

    List filters (here and on `public_templates/`, `my_templates/`): `?goal=`,
    `?is_public=`, `?created_by=`, `?search=`, and (additive, S2.10) `?place=home|gym`
    and `?by_platform=true|false`. A value that names nothing simply matches nothing.

    **A trainer's own template cannot be made public** (S2.10, the owner's D16: the public
    templates are the platform's alone, until the owner opens it): `POST` with
    `is_public: true`, or a `PATCH`/`PUT` that turns it on, answers 400
    `template_cannot_be_public` with `field: "is_public"`, and nothing is written. Sending
    `false`, leaving it out, or re-sending the value the template already holds is
    accepted. Nothing here differs by build.
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

        # Road to 10 S2.10: where it is trained, and who wrote it.
        place = (self.request.query_params.get('place') or '').strip().lower()
        if place:
            qs = qs.filter(place=place)
        by_platform = self.request.query_params.get('by_platform')
        if by_platform is not None:
            qs = qs.filter(created_by__isnull=(by_platform.strip().lower() == 'true'))
        
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

        # `used_by_count` (B-108) counted in this query, as a subquery — an aggregate
        # here would add a GROUP BY and drop the model's ordering.
        from .serializers import annotate_used_by_count

        return annotate_used_by_count(qs.distinct())

    def perform_create(self, serializer):
        """Override to assign the template to the creator."""
        serializer.save(created_by=self.request.user)

    @staticmethod
    def _turns_public(request, instance=None) -> bool:
        """Whether this write would make a template public that is not (S2.10). A value
        that is not a boolean is left to the serializer, which answers it."""
        raw = request.data.get('is_public') if hasattr(request.data, 'get') else None
        if raw is None:
            return False
        try:
            wanted = serializers.BooleanField().to_internal_value(raw)
        except serializers.ValidationError:
            return False
        return bool(wanted) and not (instance is not None and instance.is_public)

    @staticmethod
    def _public_refused():
        return Response({'error': _('Public templates are written by the platform. Keep '
                                    'yours private; trainers can copy the platform\'s '
                                    'starter templates.'),
                         'code': 'template_cannot_be_public', 'field': 'is_public'},
                        status=status.HTTP_400_BAD_REQUEST)

    def create(self, request, *args, **kwargs):
        """`POST /api/routine/templates/` — 201 with the template. 400
        `template_cannot_be_public` (+ `field`) for `is_public: true` (S2.10)."""
        if self._turns_public(request):
            return self._public_refused()
        return super().create(request, *args, **kwargs)

    def update(self, request, *args, **kwargs):
        """`PATCH`/`PUT /api/routine/templates/{id}/` — 400 `template_cannot_be_public`
        (+ `field`) when it would turn `is_public` on (S2.10)."""
        if self._turns_public(request, self.get_object()):
            return self._public_refused()
        return super().update(request, *args, **kwargs)

    def destroy(self, request, *args, **kwargs):
        """`DELETE /api/routine/templates/{id}/` — 204, or 409 `template_listed_for_sale`
        while a routine product of yours is in review or published on it (plan step 10):
        retire the product first (`POST /api/v1/trainer/products/{id}/retire/`).
        Deleting one behind a draft or a retired product is allowed; the draft then needs
        another template. Buyers' copies are theirs and are never touched. 404
        `not_found`: no such template, or not one you can see; 403 for someone else's."""
        from .product_models import ProductStatus, RoutineProduct

        template = self.get_object()
        if RoutineProduct.objects.filter(
                source_template=template,
                status__in=(ProductStatus.IN_REVIEW, ProductStatus.PUBLISHED)).exists():
            return Response({'error': _('This template is behind a program in review or on '
                                        'sale. Retire the program first.'),
                             'code': 'template_listed_for_sale'},
                            status=status.HTTP_409_CONFLICT)
        return super().destroy(request, *args, **kwargs)

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

        Codes — every refusal names its own since the owner's decision of 2026-09-24;
        the ones marked new answered only the status default (`bad_request`,
        `permission_denied`, `not_found`), which is what a client that does not know
        them still reads them as:

        | Status | `code` | When |
        |---|---|---|
        | 400 | `client_id_required` (new) | assigning, and `client_id` missing or not a whole number |
        | 400 | `bad_date` (new) | `start_date` / `end_date` not a real YYYY-MM-DD date |
        | 400 | `end_before_start` (new) | `end_date` before `start_date` |
        | 400 | `bad_days` (new) | `days` not a whole number |
        | 400 | `days_out_of_range` (new) | `days` outside 1-365; carries `max` |
        | 400 | `bad_customization` | with `key` |
        | 400 | `invalid_client_ids` | the list is not 1-50 different ids |
        | 400 | `template_empty` | no exercises |
        | 400 | `client_ids_need_assign` | `client_ids` with `assign: false` |
        | 400 | `generate_too_large` | clients x days above `max_routine_days`, 1000 |
        | 403 | `not_your_client` | not your approved client — `client_id` (new there), and with `client_ids` for the list form |
        | 404 | `client_not_found` (new) | `client_id` names no client |
        | 404 | `not_found` | no such template, or not one you can see |

        Identical on every build; no price and no purchase is involved.
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
                return Response({'detail': _('client_id required'),
                                 'code': 'client_id_required'}, status=400)
            # A client, not whoever has that id: a trainer's own id passed
            # `can_access_user_data` (it admits the caller) and wrote a routine assigned
            # to the trainer.
            client = CustomUser.objects.filter(pk=client_id, user_type='client').first()
            if client is None:
                return Response({'detail': _('Client not found'),
                                 'code': 'client_not_found'}, status=404)
            # Only a trainer, and only for an approved client of theirs. This read
            # `client.assigned_trainer_id`, the denormalised display pointer that
            # routine/permissions.py opens by forbidding as an access test — it was the
            # pointer that once granted access without consent.
            if not (request.user.is_trainer and can_access_user_data(request.user, client.pk)):
                # The code the list form already answers for the same refusal.
                return Response({'detail': _('Permission denied. You can only assign routines to your own clients.'),
                                 'code': 'not_your_client'}, status=403)
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
        # `_date_or_none`, not `parse_date`: that one RAISES for "2026-13-45" (shaped
        # like a date, not one), which is the 500 this block was written to end.
        d1 = _date_or_none(start_date) if start_date else None
        d2 = _date_or_none(end_date) if end_date else None
        if (start_date and not d1) or (end_date and not d2):
            return Response({'error': _('start_date and end_date must be YYYY-MM-DD.'),
                             'code': 'bad_date'},
                            status=status.HTTP_400_BAD_REQUEST)
        if d1 and d2 and d2 < d1:
            return Response({'error': _('end_date is before start_date.'),
                             'code': 'end_before_start'},
                            status=status.HTTP_400_BAD_REQUEST)
        routine_days = request.data.get('days')
        if routine_days not in (None, ''):
            try:
                routine_days = int(routine_days)
            except (TypeError, ValueError):
                return Response({'error': _('days must be a whole number.'),
                                 'code': 'bad_days'},
                                status=status.HTTP_400_BAD_REQUEST)
        elif d1 and d2:
            routine_days = (d2 - d1).days + 1
        else:
            routine_days = template.days
        if not 1 <= int(routine_days) <= MAX_GENERATED_DAYS:
            return Response({'error': _('days must be between 1 and %(max)d.')
                             % {'max': MAX_GENERATED_DAYS},
                             'code': 'days_out_of_range', 'max': MAX_GENERATED_DAYS},
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

        Your own template (to make a variant), or another trainer's PUBLIC one. 400
        `template_not_public` for someone else's private template (the owner,
        2026-09-24; it was the bare `bad_request` — only an admin can see one to ask).
        404 `not_found`: no such template, or not one you can see. 201 with the copy:
        private, in your library, both languages of the name prefixed "Copy of".

        Copying your own template answered 400 "Cannot copy your own template", so a
        trainer could not make a variant of their own program to reuse.

        A copy of one of the PLATFORM's starter templates (Road to 10 S2.10,
        `by_platform: true`) keeps the starter's own name in both languages, without
        "Copy of": it is the trainer's first program, not a variant of their own. Every
        copy carries the source's `place` (additive) and is the caller's own
        (`by_platform: false`).

        The copy's `goal` is one of the four codes (`ProductGoal`), like every template
        (wave-1 review R-5: it copied the source's text unchecked, so a console's
        "Hypertrophy" spread). A template stored before the closed list is copied with
        the code its text reads as (`routine.products.goal_code`, the catalogue's
        reading); one whose text reads as none answers 400 `validation_error` with
        `field_errors.goal` and copies nothing — its author sets a goal first.
        """
        from django.conf import settings
        from django.utils.translation import override

        from .products import goal_code

        template = self.get_object()
        if not template.is_public and template.created_by_id != request.user.id:
            return Response({'detail': _('Can only copy public templates'),
                             'code': 'template_not_public'}, status=400)

        names = {}
        for language, _label in settings.LANGUAGES:
            with override(language):
                original = getattr(template, f'name_{language}', None) or template.name
                names[f'name_{language}'] = (
                    original if template.by_platform
                    else _('Copy of %(name)s') % {'name': original})[:255]
        with transaction.atomic():
            new_template = RoutineTemplate.objects.create(
                **names,
                **{f'description_{language}': getattr(template, f'description_{language}', '')
                   or '' for language, _label in settings.LANGUAGES},
                # The code, never the source's raw text; text that reads as no code is
                # kept so the model's choices refuse it (400, rolled back).
                goal=goal_code(template.goal) or template.goal,
                days=template.days,
                weeks=template.weeks,
                place=template.place,
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
        Only for trainers: anyone else answers 403 `not_a_trainer` (the owner,
        2026-09-24; it was the generic `permission_denied`).
        """
        if not request.user.is_trainer:
            return Response({'detail': _('Only trainers can access this endpoint'),
                             'code': 'not_a_trainer'}, status=403)
        
        templates = self.get_queryset().filter(created_by=request.user)
        page = self.paginate_queryset(templates)
        if page is not None:
            return self.get_paginated_response(self.get_serializer(page, many=True).data)
        return Response(self.get_serializer(templates, many=True).data)

    @action(detail=False, methods=['get'])
    def public_templates(self, request):
        """`GET /api/routine/templates/public_templates/` — every public template, newest
        first, paginated (`{count, next, previous, results}`). Any signed-in account.

        Since Road to 10 S2.10 these are the PLATFORM's starter templates (a trainer's
        own cannot be made public): `by_platform: true`, `created_by: null`, `goal` one of
        the four, `place` `home` or `gym`, `days`, `weeks`, `session_count` and every row
        in `exercises`. `?by_platform=true` lists those alone; `?goal=` and `?place=`
        narrow them. Copy one with `POST templates/{id}/copy/`. Nothing differs by build.
        """
        # Every public template on the platform, from every trainer. This is the one
        # that grows without bound as the platform grows, and it returned the whole
        # table as a bare array with no envelope and no page links.
        templates = self.get_queryset().filter(is_public=True)
        page = self.paginate_queryset(templates)
        if page is not None:
            return self.get_paginated_response(self.get_serializer(page, many=True).data)
        return Response(self.get_serializer(templates, many=True).data)


class RoutineTemplateExerciseListSerializer(serializers.ModelSerializer):
    """What `template-exercises/` answers: the row, with its exercise both as an id and
    expanded under `exercise_details`.

    Named for its job (SIDE_BUGS S-029). It was called `RoutineTemplateExerciseSerializer`
    — the name of a DIFFERENT class in `routine/serializers.py`, which nests the whole
    exercise under `exercise` and takes `exercise_id` on write, and which this module
    also imported. Two classes, one name, two shapes: the router used this one, the
    nested `exercises` of a template used that one, and an edit meant for either could
    land in the wrong file without anything failing.
    """
    exercise_details = ExerciseSerializer(source='exercise', read_only=True)

    class Meta:
        model = RoutineTemplateExercise
        fields = ['id', 'template', 'exercise', 'exercise_details', 'sets', 'reps', 'rest_time', 'day', 'order']


class RoutineTemplateExerciseViewSet(ExerciseMediaMixin, viewsets.ReadOnlyModelViewSet):
    """
    API endpoint for viewing RoutineTemplateExercise objects with pagination.
    ReadOnly since trainers usually manage these via the template or custom actions.
    """
    queryset = RoutineTemplateExercise.objects.select_related('exercise').prefetch_related('exercise__media')
    serializer_class = RoutineTemplateExerciseListSerializer
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


class UserExerciseProgressFilter(django_filters.FilterSet):
    """`?exercise=<id>&date=YYYY-MM-DD` — `exercise` as a NUMBER, never a lookup of the
    exercise row (SIDE_BUGS S-164, the S-119 oracle; see `UserExerciseProgressViewSet`)."""
    exercise = _WholeNumberFilter(field_name='exercise_id')
    date = django_filters.DateFilter(field_name='date')

    class Meta:
        model = UserExerciseProgress
        fields = ['exercise', 'date']


class UserExerciseProgressViewSet(ExerciseMediaMixin, viewsets.ModelViewSet):
    """
    API endpoint for managing UserExerciseProgress objects (per-user, per-exercise progress).
    Allows trainers, admins, and clients to create, view, and update their own progress records.
    A client's create answers 403 `coaching_frozen` / `coaching_unpaid` / `coaching_ended` / `product_refunded` when every routine
    of theirs holding the exercise comes from a coaching that is unpaid or over.

    `POST` body: `exercise` (id), optional `date` (default today), `skipped`. 400
    `validation_error` with, on the field: `does_not_exist` on `exercise` for an
    exercise the caller may not read and has never trained — the same answer as an id
    that does not exist (SIDE_BUGS S-056); `date_in_future` / `date_too_old` on `date`
    outside the training window (SIDE_BUGS S-112). 400 `progress_exists` with the
    existing row's `id` for a second row on the same exercise and day.

    **List filters.** `?exercise=<id>` and `?date=YYYY-MM-DD` narrow the rows the caller
    may read. `exercise` is a plain whole number (SIDE_BUGS S-164): it was checked
    against EVERY exercise, so a missing id answered 400 "Select a valid choice" while
    another trainer's private exercise answered an empty 200, and exercise ids could be
    probed. Both are an empty page now; a value that is not a whole number is 400
    `validation_error` on `exercise`.

    `?latest_for=<id>,<id>,...&before=YYYY-MM-DD` (BACKEND_BUGS B-181; «آخر مرة» beside
    each exercise of a day, in ONE call instead of one per exercise): the newest TRAINED
    row (`skipped` false, `completed_sets` > 0) of each named exercise dated before
    `before` (any date when it is not sent) — one row per exercise for a client; for a
    trainer, one per exercise per client they may read. At most 50 ids. 400
    `bad_exercise_id` (+ `field`: `latest_for`) for an id that is not a whole number
    from 1, 400 `too_many_exercise_ids` (+ `max`), 400 `bad_date` (+ `field`: `before`).
    An id that names nothing, or nothing the caller may read, simply has no row.

    Every list and detail row carries `top_set` (B-181): that day's «أفضل مجموعة» by the
    owner's rule (the highest weight x reps, among equals the heavier weight;
    `routine/strength.py`) as `{weight, reps, volume_kg}`, or null when no set of that day
    has both a weight and reps — so «آخر مرة» needs no `daily-summary/` call per date.
    """
    queryset = UserExerciseProgress.objects.all()
    serializer_class = UserExerciseProgressSerializer
    pagination_class = StandardResultsSetPagination
    filter_backends = [DjangoFilterBackend]
    filterset_class = UserExerciseProgressFilter

    #: How many exercises one `?latest_for=` may name: a day, not the library.
    LATEST_FOR_MAX = 50

    def list(self, request, *args, **kwargs):
        raw = request.query_params.get('latest_for')
        if raw is None:
            return super().list(request, *args, **kwargs)
        ids = []
        for part in raw.split(','):
            number = _int_or_none(part.strip())
            if number is None or number < 1 or str(number) != part.strip():
                return Response({'error': _('latest_for must be exercise ids separated by '
                                            'commas.'),
                                 'code': 'bad_exercise_id', 'field': 'latest_for'},
                                status=status.HTTP_400_BAD_REQUEST)
            ids.append(number)
        if len(set(ids)) > self.LATEST_FOR_MAX:
            return Response({'error': _('Ask for at most %(max)d exercises at a time.')
                             % {'max': self.LATEST_FOR_MAX},
                             'code': 'too_many_exercise_ids', 'max': self.LATEST_FOR_MAX},
                            status=status.HTTP_400_BAD_REQUEST)
        trained = Q(exercise_id__in=set(ids), skipped=False, completed_sets__gt=0)
        before = request.query_params.get('before')
        if before is not None:
            day = _date_or_none(before)
            if day is None:
                return Response({'error': _('date must be in YYYY-MM-DD format.'),
                                 'code': 'bad_date', 'field': 'before'},
                                status=status.HTTP_400_BAD_REQUEST)
            trained &= Q(date__lt=day)
        queryset = self.filter_queryset(self.get_queryset())
        # The newest row per (person, exercise), chosen in a narrow subquery — only the
        # key columns are DISTINCT ON'd — and read back through the scoped queryset, so
        # the page keeps its ordinary order and joins.
        newest = (queryset.filter(trained)
                  .order_by('user_id', 'exercise_id', '-date', '-id')
                  .distinct('user_id', 'exercise_id').values('pk'))
        queryset = queryset.filter(pk__in=newest)
        page = self.paginate_queryset(queryset)
        if page is not None:
            return self.get_paginated_response(self.get_serializer(page, many=True).data)
        return Response(self.get_serializer(queryset, many=True).data)

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
        # The detail serializer nests the whole exercise, and this queryset had
        # neither the join nor the prefetch: every row cost a query for its exercise
        # and another for that exercise's media. Twenty-five rows was fifty queries
        # for one page.
        base = self.queryset.select_related('exercise').prefetch_related('exercise__media')
        if self.action in ('list', 'retrieve'):
            base = base.prefetch_related('set_logs')        # each row's `top_set`
        if user.is_admin:
            return base.all()
        elif user.is_trainer:
            ids = accessible_user_ids(user)
            return base if ids is None else base.filter(user_id__in=ids)
        else:
            return base.filter(user=user)

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

        `?date=YYYY-MM-DD` (today by default); anything else answers 400 `bad_date` (the
        owner, 2026-09-24; it was the bare `bad_request`).
        """
        date_str = request.query_params.get('date')
        if not date_str:
            date_str = timezone.localdate().isoformat()

        try:
            # Validate date format
            target_date = datetime.strptime(date_str, '%Y-%m-%d').date()
        except ValueError:
            return Response({'error': _('Invalid date format. Use YYYY-MM-DD.'),
                             'code': 'bad_date'}, status=400)
            
        queryset = list(self.get_queryset().filter(date=target_date)
                        .select_related('exercise')
                        .prefetch_related('set_logs', 'exercise__media'))

        # Use the specific detailed serializer
        from .media_context import prepare
        from .serializers import UserDailySummarySerializer
        # With the request, so `exercise.image` is absolute as it is everywhere else
        # (BACKEND_BUGS B-032): without it the same exercise came back as a relative
        # path here and a full URL from `routines/`.
        # And with the media context, so the day's exercises resolve whose
        # demonstration the client sees in ONE query rather than one per exercise —
        # this route builds its serializer by hand, so `ExerciseMediaMixin` on the
        # viewset never sees it.
        context = {'request': request, **prepare(request, queryset)}
        serializer = UserDailySummarySerializer(queryset, many=True, context=context)

        return Response({
            'date': date_str,
            # `len`, not `queryset.count()`: the rows are already in memory, and the
            # extra COUNT(*) was a second trip for a number we are holding.
            'total_exercises': len(queryset),
            'exercises': serializer.data
        })

    @action(detail=False, methods=['post'], url_path='bulk-complete', permission_classes=[permissions.IsAuthenticated])
    def bulk_complete(self, request):
        """
        Mark all exercises for a given routine and day as done for the current user.
        Input: routine_id, day, date, completed_sets, target_sets

        Answers `{results, errors, count}`. Each `errors[]` entry is
        `{exercise, error, code}` — `code` is `progress_exists` or
        `progress_write_failed`; `error` is translated and for showing only.

        403 `coaching_frozen` / `coaching_unpaid` / `coaching_ended` / `product_refunded` (with `trainer_id`): the coaching this
        routine came from is unpaid or over — readable, not trainable (the owner).

        The other refusals are `bulk-create`'s, code for code (the owner, 2026-09-24;
        they answered `bad_request`, `permission_denied` or `not_found`): 400
        `routine_day_date_required`, `bad_date`, `date_in_future`, `date_too_old`,
        `day_out_of_range`, `day_has_no_exercises`; 400 `validation_error` for a
        `completed_sets` that is not a whole number; 403 `routine_not_yours`; 404
        `routine_not_found`.
        """
        user = request.user
        routine_id = _int_or_none(request.data.get('routine_id'))
        day = _int_or_none(request.data.get('day'))
        date = request.data.get('date')
        completed_sets = int_param(request.data, 'completed_sets', default=1, minimum=0,
                                   maximum=MAX_SETS_PER_REQUEST)
        skipped = request.data.get('skipped', False)
        if not routine_id or not day or not date:
            return Response({'error': _('routine_id, day, and date are required.'),
                             'code': 'routine_day_date_required'}, status=400)

        refused = _backlog_date_refused(date)
        if refused is not None:
            return refused
        date = _date_or_none(date)

        from .models import Routine, RoutineExercise, UserExerciseProgress
        try:
            routine = Routine.objects.get(id=routine_id)
        except Routine.DoesNotExist:
            return _routine_not_found()

        # AUTHORIZATION: the same check bulk-create already makes, and for the same two
        # reasons. Without it any caller could write progress against a routine never
        # assigned to them, AND read every exercise name of another trainer's private
        # routine straight out of the `results` array.
        if not (routine.assigned_to.filter(id=user.id).exists()
                or routine.created_by_id == user.id
                or user.is_admin):
            return _routine_not_yours()

        _require_can_train(user, routine, date)

        exercises = RoutineExercise.objects.filter(routine=routine, day=day)
        if not exercises.exists():
            return _day_refused(routine, day)
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
                            # The claim itself, kept apart from the logged sets so the
                            # next set logged that day cannot erase it (S-175).
                            'claimed_sets': completed_sets,
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
                # Each row error carries a `code` (SIDE_BUGS S-042): this list had
                # sentences only, and the duplicate one was English whatever the
                # client spoke. `progress_exists` is the code `POST
                # user-exercise-progress/` already answers for the same collision.
                if 'unique constraint' in str(e).lower():
                    errors.append({'exercise': rex.exercise.name,
                                   'error': _('A progress record for this user, exercise, '
                                              'and date already exists.'),
                                   'code': 'progress_exists'})
                else:
                    logger.exception("bulk progress write failed for %s", rex.exercise_id)
                    errors.append({'exercise': rex.exercise.name,
                                   'error': _('Could not record progress for this exercise.'),
                                   'code': 'progress_write_failed'})
        if written:
            # One pass decides the whole day from every exercise's progress.
            refresh_routine_progress(written[-1], routine_scope=routine, day_scope=day)
            # Logging a whole day closes it, so what it planned against what was done is
            # kept (the owner, 2026-09-20). This path records no set logs, so the day's
            # reps are not known and the week is shown as approximate.
            # A whole day logged at once is a quick log by definition (B-157).
            from .outcomes import record_day_quietly
            record_day_quietly(user, routine, day, date, via='bulk_complete', quick_log=True)
        return Response({'results': results, 'errors': errors, 'count': len(results)})


def _exercise_payload(request, exercise):
    """One exercise, as `ExerciseSerializer` renders it everywhere else.

    Two routes here built an exercise dict by hand from four columns, so anything
    added to an exercise afterwards simply was not in their answer — `media_slots`,
    `media_source` and `media_trainer_id` (the trainer media library) were already
    missing, and the app read two different shapes for one object depending on which
    route replied. `media_context.prepare` goes into the context for the same reason
    the viewsets mix it in: it resolves whose demonstration this viewer sees without a
    query per exercise.
    """
    from .media_context import prepare
    from .serializers import ExerciseSerializer

    context = {'request': request, **prepare(request, exercise)}
    return ExerciseSerializer(exercise, context=context).data


# WHERE an exercise's `video`/`photo` media may point. The list itself is
# `settings_base.py` `EXERCISE_MEDIA_ALLOWED_HOSTS`.
def _media_host_allowed(url) -> bool:
    """Whether `url` points at a host this platform is willing to send clients to.

    Any http(s) URL was accepted, so a coach could aim every one of their clients'
    players at a host nobody here controls — a tracker, a paywall, a site that starts
    serving something else next month — and the app would load it because the server
    said it was the exercise's demonstration. The allow-list is exact hostnames, an
    entry beginning with a dot matches that domain and its subdomains, and an entry
    `host/prefix` matches that host only under `/prefix/` — the project's bucket on the
    host every GCS bucket shares (S-081).
    """
    from django.conf import settings
    from urllib.parse import unquote, urlparse

    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    if parsed.scheme not in ('http', 'https'):
        return False
    host = (parsed.hostname or '').lower()
    if not host:
        return False
    for allowed in getattr(settings, 'EXERCISE_MEDIA_ALLOWED_HOSTS', ()) or ():
        allowed = str(allowed).strip().lower().rstrip('.')
        if not allowed:
            continue
        if '/' in allowed:
            # `host/prefix`: one bucket on a host every bucket shares (S-081). A client
            # normalises `..`, `%2e%2e` and `\` before it fetches, so a path that would
            # step out of the prefix is refused rather than compared.
            allowed_host, _slash, prefix = allowed.partition('/')
            path = unquote(parsed.path or '')
            segments = path.replace('\\', '/').split('/')
            if host == allowed_host and path.startswith(f"/{prefix.strip('/')}/") \
                    and '..' not in segments and '.' not in segments:
                return True
            continue
        if allowed.startswith('.'):
            if host == allowed[1:] or host.endswith(allowed):
                return True
        elif host == allowed:
            return True
    return False


# ---- Resolving an exercise for a media write: one rule, written once ---------------
# `ExerciseImageUploadView` and `ExerciseAddMediaView` each wrote the "may see" and
# "may change" rules out for themselves, and the image view's copy had already drifted:
# it resolved ids with `Exercise.objects.get`, so another trainer's private exercise
# answered 403 while a missing id answered 404 (SIDE_BUGS S-125).

def _visible_exercise(request, exercise_id):
    """The exercise if this caller may read it at all, else None (-> 404).

    `routine.permissions.visible_exercises_for`, the rule the exercise list uses.
    `.first()` rather than `.get()`: the client branch of the rule joins through
    routine_exercises, and a row reachable by two routes must not raise
    MultipleObjectsReturned at the caller.
    """
    return visible_exercises_for(request.user).filter(id=exercise_id).first()


def _exercise_not_found():
    """The one answer for "no such exercise" and for "not yours to see".

    A fresh dict per call, not a constant: `normalize_error_envelope` fills `detail`
    and `code` into `response.data` in place, so a shared dict would be edited by the
    first 404 this process serves. `not_found` named here rather than left to the
    envelope (the owner, 2026-09-24): its only meaning is "no such exercise, or not
    yours to see".
    """
    return Response({'error': _('Exercise not found.'), 'code': 'not_found'},
                    status=status.HTTP_404_NOT_FOUND)


def _may_modify_exercise(request, exercise):
    """Writes are owner-only: `created_by`, or an admin.

    Global exercises belong to the shared catalogue: admin-only. The old guard was
    `not is_global and created_by != user`, which short-circuits to False for global
    exercises, leaving them writable by ANY user. Keyed on ownership rather than
    `is_global`, because the "created_by => not global" invariant lives in
    `Exercise.clean()`, which `.create()` never runs.
    """
    is_admin = getattr(request.user, 'is_admin', False) or request.user.is_staff
    if exercise.created_by_id:
        return (exercise.created_by_id == request.user.id) or is_admin
    # No owner => shared global catalog => admin only.
    return is_admin


def _not_yours_to_modify():
    """403 `permission_denied`, only ever for an exercise the caller can already see.
    Translated (SIDE_BUGS S-042): the four copies of this sentence were English."""
    return Response({'error': _('You do not have permission to modify this exercise.'),
                     'code': 'permission_denied'},
                    status=status.HTTP_403_FORBIDDEN)


class ExerciseImageUploadView(APIView):
    """
    `/api/routine/exercises/{exercise_id}/image/` — an exercise's main demonstration
    image. `POST` is multipart/form-data with the file under `image`; `DELETE` takes no
    body. Signed in.

    **Who.** The id is resolved through `routine.permissions.visible_exercises_for`
    FIRST, the rule the exercise list and `add-media/` use: an exercise the caller may
    not see answers `404 code="not_found"`, `{"error"/"detail": "Exercise not found."}`
    — byte-identical to an id that does not exist. It answered 403 for another trainer's
    private exercise, which told a caller which ids were real (SIDE_BUGS S-125). Then
    ownership: the exercise's `created_by`, or an admin; the shared catalogue is
    admin-only. Anyone else who can SEE it answers 403 `permission_denied`.

    | Method | Answers |
    |---|---|
    | `POST` | `200` `{message, exercise}` · `400` `image_required` (no `image`) · `400` refused upload · `403` `permission_denied` · `404` `not_found` · `503` `upload_busy` (every image-decode slot is taken: nothing stored; retry after `Retry-After` seconds, also `retry_after_seconds` in the body) · `500` `image_upload_failed` |
    | `DELETE` | `200` `{message}` · `404` `no_image` `{message, error}` when the exercise has no image · `403` `permission_denied` · `404` `not_found` · `500` `image_removal_failed` |

    `image_required` and `no_image` are the owner's decision of 2026-09-24 (every
    refusal names its own code): they answered `bad_request` and `not_found`, and
    `no_image` could not be told from "no such exercise" at all. `no_image` also
    carries `error` beside the `message` it always had.

    `exercise` is the full `ExerciseSerializer` shape. A refused upload carries the
    upload guard's own `code` — `file_too_large`, `invalid_mime_type`,
    `unreadable_image`, `unsupported_format`, `image_dimensions_too_large`,
    `image_too_many_pixels`, and the guard's other refusals; `invalid_image` when it
    names none (SIDE_BUGS S-118). A 500 never carries the exception text.

    Nothing here varies by build (`X-App-Channel`): it sells nothing and moves no money.
    """
    permission_classes = [IsAuthenticated]
    parser_classes = [MultiPartParser, FormParser]

    def post(self, request, exercise_id):
        """Upload an image for a specific exercise"""
        try:
            # Invisible -> 404, exactly like a missing id; only then ownership.
            exercise = _visible_exercise(request, exercise_id)
            if exercise is None:
                return _exercise_not_found()
            if not _may_modify_exercise(request, exercise):
                return _not_yours_to_modify()

            # Get the uploaded file
            image_file = request.FILES.get('image')
            
            if not image_file:
                return Response(
                    {'error': _('No image file provided. Please include a file with key "image"'),
                     'code': 'image_required'},
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
                return _refused_upload(e)

            # Store under the DETECTED format's extension, never the client filename.
            exercise.image.save(f"exercise.{img_ext}", safe_file, save=False)
            exercise.save()

            # The serializer, not a hand-rolled dict. This listed four fields by hand,
            # so every field added to an exercise since was invisible here — the new
            # `media_slots`, `media_source`, `media_trainer_id` and `media` among them —
            # and the app got a different shape for the same object depending on which
            # route answered. `image` is absolute, as it is everywhere else.
            return Response({
                'message': _('Exercise image uploaded successfully'),
                'exercise': _exercise_payload(request, exercise),
            }, status=status.HTTP_200_OK)

        except PASSTHROUGH_EXCEPTIONS:
            # Control-flow exceptions carry their own correct status (404/403/401/400).
            # The broad handler below turned every one of them into a 500, which is the
            # wrong contract for the client and hides real faults in the error budget.
            raise
        except Exception:
            # The exception TEXT used to be the response body. That is a stack trace's
            # worth of internals — file paths, storage bucket names, SQL — handed to
            # whoever posted the file. Logged with the traceback, answered with a code.
            logger.exception("exercise image upload failed for %s", exercise_id)
            return Response(
                {'error': _('Could not save that image. Please try again.'),
                 'code': 'image_upload_failed'},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )

    def delete(self, request, exercise_id):
        """Remove exercise image"""
        try:
            # Same order as POST: invisible -> 404, then ownership.
            exercise = _visible_exercise(request, exercise_id)
            if exercise is None:
                return _exercise_not_found()
            if not _may_modify_exercise(request, exercise):
                return _not_yours_to_modify()

            if exercise.image:
                # See users/views.py: the post-commit receiver owns file removal.
                exercise.image = None
                exercise.save()
                
                return Response({
                    'message': _('Exercise image removed successfully')
                }, status=status.HTTP_200_OK)
            else:
                # `no_image`: the exercise is there and has no picture, which is not the
                # `not_found` of a missing exercise (the owner, 2026-09-24).
                return Response({
                    'message': _('No image to remove'),
                    'error': _('No image to remove'),
                    'code': 'no_image',
                }, status=status.HTTP_404_NOT_FOUND)
                
        except PASSTHROUGH_EXCEPTIONS:
            # Control-flow exceptions carry their own correct status (404/403/401/400).
            # The broad handler below turned every one of them into a 500, which is the
            # wrong contract for the client and hides real faults in the error budget.
            raise
        except Exception:
            # Same rule as `post`: never the exception text (S-006).
            logger.exception("exercise image removal failed for %s", exercise_id)
            return Response(
                {'error': _('Could not remove that image. Please try again.'),
                 'code': 'image_removal_failed'},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )


class ExerciseAddMediaView(APIView):
    """
    `/api/routine/exercises/{exercise_id}/add-media/` — an exercise's media
    (video URLs, photo URLs, text form-tips). Signed in; no body on GET.

    **Who sees what.** Every method resolves `exercise_id` through
    `routine.permissions.visible_exercises_for` FIRST, the same rule the exercise
    list and detail use: admin sees every exercise, a trainer sees their own plus the
    shared catalogue, a client sees the catalogue, their approved trainers'
    exercises, and anything a routine assigned to them uses. An exercise outside that
    set answers `404 code="not_found"`, `{"error"/"detail": "Exercise not found"}` —
    byte-identical to an id that does not exist, so ids cannot be probed for
    existence. `GET` had no visibility rule at all and served any trainer's private
    media to any signed-in caller.

    **Who may write.** Reading is enough to see media; changing it is not. After the
    404 gate, `POST` and `DELETE` additionally require ownership: the exercise's
    `created_by`, or an admin. The shared catalogue (`created_by IS NULL`) is
    admin-only — a client or trainer reading it gets 200 on GET and `403
    code="permission_denied"`, "You do not have permission to modify this exercise.",
    on a write (translated, SIDE_BUGS S-042). A 403 is only ever returned for a row the caller can already see.
    Keyed on `created_by_id`, not `is_global`: the "created_by => not global"
    invariant lives in `Exercise.clean()`, which `.create()` never runs.

    | Method | Answers |
    |---|---|
    | `GET` | `200` `{exercise_id, exercise_name, media_count, media_items[], exercise{}}` · `404` `not_found` |
    | `POST` | `201` created · `207` some items rejected (`errors[]`, `errors_detail[]`, `partial_success`) · `400` `media_items_required` · `403` `permission_denied` (not the owner) · `404` `not_found` |
    | `DELETE` | `200` all deleted · `207` some ids missed (`errors[]`, `errors_detail[]`, `partial_success`) · `400` `media_ids_required` · `403` `permission_denied` (not the owner) · `404` `not_found` |

    `media_items_required`, `media_ids_required` and the DELETE's `errors_detail[]` are
    the owner's decision of 2026-09-24 (every refusal names its own code): the two 400s
    answered `bad_request`, and a partly refused DELETE carried sentences only.

    `GET`'s `exercise` is the full `ExerciseSerializer` shape — the same object the
    exercise list and detail answer, `media_slots` included. `exercise_id` and
    `exercise_name` are kept beside it. A photo the platform STORED
    (`create-with-image/`) is served in `media_items[].content` as a URL signed when it
    is read; it expires like every media URL (SIDE_BUGS S-070). `message` on a 201/200
    is translated and for showing only.

    **Caching.** These GETs, the exercise list and the detail are cached per user for
    five minutes; every `POST` and `DELETE` here invalidates them, so a coach sees their
    own change at once (SIDE_BUGS S-138).

    Nothing here varies by build (`X-App-Channel`): it sells nothing and moves no money.

    POST body — `media_type` and `content` are required per item. `video` and `photo`
    content must be an `http(s)` URL **on an allowed host**
    (`settings.EXERCISE_MEDIA_ALLOWED_HOSTS`: the platform's own media hosts, plus
    whatever the deployment adds). An item that fails a check is reported and the rest
    are still created (207). `errors[]` is one TRANSLATED sentence per rejected item —
    Arabic for an Arabic client, the item number included (S-039) — and is for showing,
    never for branching; the `code` to branch on is in
    `errors_detail[] = [{index, code, message}]`:
    `media_fields_required`, `media_type_invalid`, `media_url_invalid`,
    `media_host_not_allowed`, `media_write_failed`.
    {
        "media_items": [
            {
                "media_type": "video",
                "content": "https://storage.googleapis.com/yalla-gym/demo.mp4",
                "title": "Exercise Tutorial",
                "description": "Step-by-step guide",
                "order": 1
            },
            {
                "media_type": "photo",
                "content": "https://storage.googleapis.com/yalla-gym/form.jpg",
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

    DELETE body — `{"media_ids": [1, 2]}`; ids belonging to another exercise are
    reported in `errors[]` (translated too), never deleted. The code to branch on is in
    `errors_detail[] = [{media_id, code, message}]`, as on the POST:
    `media_not_on_exercise` (no such item on this exercise) or `media_delete_failed`.
    """
    permission_classes = [IsAuthenticated]

    # The rules themselves are module-level, shared with `ExerciseImageUploadView`
    # (SIDE_BUGS S-125): see `_visible_exercise` and `_may_modify_exercise`.
    _not_found = staticmethod(_exercise_not_found)
    _visible_exercise = staticmethod(_visible_exercise)
    _may_modify = staticmethod(_may_modify_exercise)

    def post(self, request, exercise_id):
        """Add media items to an existing exercise. Owner or admin; see the class docstring."""
        try:
            # Resolve through the visibility rule first: an exercise this caller cannot
            # see is answered as missing, not as forbidden, so a 403 never confirms that
            # someone else's exercise exists.
            exercise = self._visible_exercise(request, exercise_id)
            if exercise is None:
                return self._not_found()

            # Seeing it is not editing it. Global exercises belong to the shared
            # catalog: admin-only. The old guard was
            # `not is_global and created_by != user`, which short-circuits to False for
            # global exercises, leaving them writable by ANY user.
            if not self._may_modify(request, exercise):
                return _not_yours_to_modify()

            # Get media items from request
            media_items = request.data.get('media_items', [])
            
            if not media_items:
                return Response(
                    {'error': _('No media items provided. Please include "media_items" array'),
                     'code': 'media_items_required'},
                    status=status.HTTP_400_BAD_REQUEST
                )

            # Validate and create media items
            created_media = []
            errors = []
            #: The same refusals, with the `code` the app is supposed to branch on.
            #: The codes arrived beside the sentences rather than replacing them,
            #: because the contract is additive.
            errors_detail = []

            def refuse(index, code, message):
                # The whole sentence goes through gettext, the number included
                # (SIDE_BUGS S-039). Only the reason was translated, so an Arabic
                # client read `Item 2: ` in English in front of its own Arabic, and
                # "Item" is the one word in the line a person actually has to parse.
                errors.append(_('Item %(number)d: %(reason)s')
                              % {'number': index + 1, 'reason': message})
                errors_detail.append({'index': index, 'code': code,
                                      'message': str(message)})

            for i, item in enumerate(media_items):
                try:
                    # Validate required fields
                    media_type = item.get('media_type')
                    content = item.get('content')
                    
                    if not media_type or not content:
                        refuse(i, 'media_fields_required',
                               _('media_type and content are required.'))
                        continue
                    
                    # Validate media type
                    valid_types = ['video', 'photo', 'text']
                    if media_type not in valid_types:
                        refuse(i, 'media_type_invalid',
                               _('media_type must be one of: %(types)s.')
                               % {'types': ', '.join(valid_types)})
                        continue
                    
                    # Validate content (basic URL validation for video/photo)
                    if media_type in ['video', 'photo']:
                        if not str(content).startswith(('http://', 'https://')):
                            refuse(i, 'media_url_invalid',
                                   _('content must be an http(s) URL for %(kind)s.')
                                   % {'kind': media_type})
                            continue
                        # WHOSE host. Any http(s) URL was accepted, so one coach could
                        # point every client's player at a third party nobody here
                        # controls (S-017). The allow-list is a setting; the project's
                        # own media hosts are in it and anything else is a deliberate
                        # decision, not a coach's.
                        if not _media_host_allowed(content):
                            refuse(i, 'media_host_not_allowed',
                                   _('That link is not on a host this app plays media '
                                     'from. Upload the file instead.'))
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
                    
                except Exception:
                    # Never the exception text (S-006).
                    logger.exception("adding media item %s to exercise %s failed",
                                     i + 1, exercise_id)
                    refuse(i, 'media_write_failed',
                           _('Could not add that media. Please try again.'))
            
            # Return response
            response_data = {
                'message': _('Successfully added %(count)d media items to exercise "%(name)s"')
                % {'count': len(created_media), 'name': exercise.name},
                'exercise_id': exercise.id,
                'exercise_name': exercise.name,
                'created_media': created_media,
                'total_media_count': exercise.media.count()
            }
            
            if errors:
                response_data['errors'] = errors
                response_data['errors_detail'] = errors_detail
                response_data['partial_success'] = True
                return Response(response_data, status=status.HTTP_207_MULTI_STATUS)
            else:
                return Response(response_data, status=status.HTTP_201_CREATED)

        except PASSTHROUGH_EXCEPTIONS:
            # Control-flow exceptions carry their own correct status (404/403/401/400).
            # The broad handler below turned every one of them into a 500, which is the
            # wrong contract for the client and hides real faults in the error budget.
            raise
        except Exception:
            # Same rule as the image upload: never the exception text (S-006).
            logger.exception("adding exercise media failed for %s", exercise_id)
            return Response(
                {'error': _('Could not add that media. Please try again.'),
                 'code': 'media_write_failed'},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )

    def get(self, request, exercise_id):
        """Get all media for an exercise this caller may see; 404 otherwise.

        This resolved `Exercise.objects.get(id=exercise_id)` with no visibility check,
        so any signed-in account could read any trainer's private exercise media —
        photos, video URLs and coaching tips — by walking ids.
        """
        try:
            exercise = self._visible_exercise(request, exercise_id)
            if exercise is None:
                return self._not_found()

            # Get all media for this exercise
            media_items = exercise.media.all().order_by('order', 'created_at')
            
            media_data = []
            for media in media_items:
                media_data.append({
                    'id': media.id,
                    'media_type': media.media_type,
                    # A stored photo is signed now, from its name (S-070).
                    'content': media.content_url(request),
                    'title': media.title,
                    'description': media.description,
                    'order': media.order,
                    'created_at': media.created_at
                })
            
            return Response({
                'exercise_id': exercise.id,
                'exercise_name': exercise.name,
                'media_count': len(media_data),
                'media_items': media_data,
                # The exercise itself, from the serializer rather than from two
                # columns copied by hand: this route answered `exercise_name` and
                # nothing else, so the trainer media library's `media_slots` — the
                # field this endpoint is about — was invisible from it (S-031). The
                # two legacy keys stay; the contract is additive.
                'exercise': _exercise_payload(request, exercise),
            }, status=status.HTTP_200_OK)

        except PASSTHROUGH_EXCEPTIONS:
            # Control-flow exceptions carry their own correct status (404/403/401/400).
            # The broad handler below turned every one of them into a 500, which is the
            # wrong contract for the client and hides real faults in the error budget.
            raise
        except Exception:
            # Same rule as the image upload: never the exception text (S-006).
            logger.exception("reading exercise media failed for %s", exercise_id)
            return Response(
                {'error': _('Could not read that exercise\'s media. Please try again.'),
                 'code': 'media_read_failed'},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )

    def delete(self, request, exercise_id):
        """Delete specific media items from an exercise. Owner or admin; see the class docstring."""
        try:
            # Same order as POST: invisible -> 404 (never a 403 that confirms the row
            # exists), then ownership.
            exercise = self._visible_exercise(request, exercise_id)
            if exercise is None:
                return self._not_found()

            # Seeing it is not editing it. Global exercises belong to the shared
            # catalog: admin-only. The old guard was
            # `not is_global and created_by != user`, which short-circuits to False for
            # global exercises, leaving them writable by ANY user.
            if not self._may_modify(request, exercise):
                return _not_yours_to_modify()

            # Get media IDs to delete
            media_ids = request.data.get('media_ids', [])
            
            if not media_ids:
                return Response(
                    {'error': _('No media_ids provided. Please include "media_ids" array'),
                     'code': 'media_ids_required'},
                    status=status.HTTP_400_BAD_REQUEST
                )

            # Delete media items
            deleted_count = 0
            errors = []
            #: The same refusals with a `code`, as the POST has them (the owner,
            #: 2026-09-24). Beside the sentences, not instead: the contract is additive.
            errors_detail = []

            def refuse(media_id, code, message):
                errors.append(message)
                errors_detail.append({'media_id': media_id, 'code': code,
                                      'message': str(message)})

            for media_id in media_ids:
                try:
                    media_item = ExerciseMedia.objects.get(id=media_id, exercise=exercise)
                    media_item.delete()
                    deleted_count += 1
                except ExerciseMedia.DoesNotExist:
                    # Translated, like the POST's (SIDE_BUGS S-039): this is the other
                    # 207 on this route and its `errors[]` were English too.
                    refuse(media_id, 'media_not_on_exercise',
                           _('Media item %(id)s is not on this exercise.') % {'id': media_id})
                except Exception:
                    # Never the exception text (S-006): the id is what the caller sent.
                    logger.exception("deleting exercise media %s failed", media_id)
                    refuse(media_id, 'media_delete_failed',
                           _('Media item %(id)s could not be removed.') % {'id': media_id})
            
            response_data = {
                'message': _('Successfully deleted %(count)d media items from exercise "%(name)s"')
                % {'count': deleted_count, 'name': exercise.name},
                'exercise_id': exercise.id,
                'deleted_count': deleted_count,
                'remaining_media_count': exercise.media.count()
            }
            
            if errors:
                response_data['errors'] = errors
                response_data['errors_detail'] = errors_detail
                response_data['partial_success'] = True
                return Response(response_data, status=status.HTTP_207_MULTI_STATUS)
            else:
                return Response(response_data, status=status.HTTP_200_OK)

        except PASSTHROUGH_EXCEPTIONS:
            # Control-flow exceptions carry their own correct status (404/403/401/400).
            # The broad handler below turned every one of them into a 500, which is the
            # wrong contract for the client and hides real faults in the error budget.
            raise
        except Exception:
            # Same rule as the image upload: never the exception text (S-006).
            logger.exception("deleting exercise media failed for %s", exercise_id)
            return Response(
                {'error': _('Could not remove that media. Please try again.'),
                 'code': 'media_delete_failed'},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )


def _client_strength(client, limit=5):
    """The client's trend, as the coach needs it: which exercises are rising, which are
    flat, and the headline for the one that moved most.

    Carries the same `assumes_sets_done_percent` the client sees, so the coach reads the
    projection under the condition it was made: without it every projection on this
    screen said it assumed nothing."""
    from datetime import timedelta

    from . import strength as strength_module
    from .outcomes import adherence

    today = timezone.localdate()
    done_percent = adherence(client, today - timedelta(days=27), today,
                             today=today)['sets']['done_percent']
    overview = strength_module.overview(client, limit=limit, adherence_percent=done_percent)
    # What to change next, per exercise (BACKEND_DELTA item 11). The coach gets the
    # same codes and the same numbers the client sees on `analytics/strength/`, so the
    # two screens cannot disagree about what the training says. One extra query for
    # every exercise together, not one each — this is the roster's neighbour screen.
    from . import progression as progression_module
    suggestions = progression_module.suggest_many(
        client, overview['exercises'], adherence_percent=done_percent)
    for row in overview['exercises']:
        row['progression'] = suggestions.get(row['exercise_id'])
    return {
        'headline': strength_module.headline(
            overview['top_movers'][0] if overview['top_movers']
            else (overview['exercises'][0] if overview['exercises'] else None)),
        'rising_count': overview['rising_count'],
        'tracked_count': overview['tracked_count'],
        'top_movers': overview['top_movers'],
        'plateaus': overview['plateaus'],
        'progression': [suggestions[row['exercise_id']]
                        for row in overview['exercises']
                        if row['exercise_id'] in suggestions],
        'assumes_sets_done_percent': done_percent,
    }


def _week_adherence(client):
    """The last week of one client's plan-vs-done, for the trainer's screens."""
    from datetime import timedelta

    from .outcomes import adherence

    today = timezone.localdate()
    week = adherence(client, today - timedelta(days=6), today, today=today)
    return {
        'planned_sets': week['sets']['planned'],
        'logged_sets': week['sets']['logged'],
        'missed_sets': week['sets']['missed'],
        'set_completion_percent': week['sets']['done_percent'],
        'perfect_days': week['perfect_days'],
        'short_days': week['short_days'],
        'skipped_days': week['skipped_days'],
        'approximate': week['approximate'],
    }


class TrainerClientProgressViewSet(viewsets.ViewSet):
    permission_classes = [IsTrainerOrAdmin]

    # Digits only. `[^/.]+` matched anything, so /client-progress/abc/ reached the
    # ORM as a filter on an integer column and raised ValueError — a 500 on a URL any
    # crawler can construct.
    @action(detail=False, methods=['get'], url_path='(?P<client_id>[0-9]+)')
    def client_progress(self, request, client_id=None):
        """GET /api/routine/trainer/client-progress/{client_id}/ — one client's month,
        as the coach reads it. Trainers (their own approved clients) and admins; 403
        `not_your_client` for a client who is not an approved client of yours, 403
        `permission_denied` for a caller who is neither a trainer nor an admin, 404
        `not_found` for no such client (the owner, 2026-09-24: the first answered
        `permission_denied`, and the 404 named no code).

        `{client: {...}, client_info: {...}, weekly_stats: {...}, strength: {...},
        recent_activity: [...]}`.

        `client` and `client_info` are the SAME object — `{id, name, joined, goal}` —
        under two names. The docstring promised `client` and the code has always
        answered `client_info`, so the app was built from a shape the server does not
        send; the contract is additive (CLAUDE.md), so the promised name arrives beside
        the shipped one rather than replacing it. Read either; `client` is the one to
        build on.

        `weekly_stats` is the last 7 days: `sessions_count`, `total_volume`, and this
        client's week against the plan — `planned_sets`, `logged_sets`, `missed_sets`,
        `set_completion_percent`, `perfect_days`, `short_days`, `skipped_days`,
        `approximate` (the same numbers as `analytics/adherence/`).
        `strength` is `{headline, rising_count, tracked_count, top_movers, plateaus,
        progression, assumes_sets_done_percent}`, the same data as `analytics/strength/`
        for this client, cut to the five exercises that moved most (see that docstring
        for the shape of a `headline`, a `projection` and a `progression`). Every row
        of `top_movers` and `plateaus` also carries its own `progression`, and
        `strength.progression` is the same answers as a flat list, most-moved first.

        `recent_activity` is the client's last `RECENT_ACTIVITY_DAYS` days of completed
        sessions,
        each `{id, date, duration, volume, routine_name, exercises, effort_rating}`.
        `effort_rating` is 1 (easy) to 5 (all I had), as the client rated that session,
        or null while unanswered.

        `coaching_refusal` (additive, BACKEND_BUGS B-201, the manager's ruling of
        2026-11-24): null while the coaching may be used, else `coaching_frozen` or
        `coaching_unpaid` (`users.coaching.refusals_for`, the rule every trainer read of
        a client's personal data applies). While it is set, `client.goal` (the client's
        own goals) is null. The training record — `weekly_stats`, `strength`,
        `recent_activity` — stays: it is what the trainer prepares the next weeks from.
        An admin reads in full (null).

        Identical on every build; no price and no claim.
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
                         {"error": _("You can only view progress for your approved clients."),
                          "code": "not_your_client"},
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
                     # `routine` joined here: every row below names its routine, and
                     # reading it off an unloaded session cost one query per session
                     # (SIDE_BUGS S-126).
                     queryset=WorkoutSession.objects.filter(
                        status='completed',
                        end_time__gte=timezone.now() - timedelta(days=RECENT_ACTIVITY_DAYS),
                     ).select_related('routine').order_by('-end_time').prefetch_related(
                         Prefetch(
                             'set_logs',
                             queryset=ExerciseSetLog.objects.select_related(
                                 'user_exercise_progress__exercise'
                             ).order_by('set_number')
                         )
                     )
                 ),
                 # `routine_progress` was prefetched here too — every row of the
                 # client's whole history — and nothing below reads it (S-126).
             ).get(id=client_id, user_type='client')
             
             # 3. Efficient In-Memory Calculation (No new DB queries)
             from .services import routine_name_for
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
                     # In the coach's own language, like every other routine name
                     # this app shows (`routine_name_for`); the column is the
                     # default-language one (S-126). A session always has a routine.
                     'routine_name': routine_name_for(session.routine, user),
                     'exercises': list(exercises_done),
                     # How hard the client said it was, or null (BACKEND_DELTA item 11).
                     # Volume says what was moved; this says what it cost them.
                     'effort_rating': session.effort_rating,
                 })

             # One object, two names. The docstring has always promised `client` and
             # this view has always answered `client_info`, so an app built from the
             # contract read a key that was never sent. Additive: the promised name is
             # added, the shipped one stays (CLAUDE.md — never remove a key the app
             # may already read).
             from users.coaching import coaching_refusals

             refusal = coaching_refusals(user, [client.pk]).get(client.pk)
             client_summary = {
                 'id': client.id,
                 'name': client.full_name or client.username,
                 'joined': client.date_joined,
                 # The client's own goals: held back while the coaching may not be
                 # used (B-201), as `trainer/clients/` and the file hold them back.
                 'goal': (None if refusal is not None
                          else getattr(client, 'client_goals', 'Not set'))
             }
             return Response({
                 'coaching_refusal': refusal,
                 'client': client_summary,
                 'client_info': client_summary,
                 'strength': _client_strength(client),
                 'weekly_stats': {
                     'sessions_count': len(week_sessions),
                     'total_volume': week_volume,
                     # How much of what this trainer asked for was actually done (the
                     # owner, 2026-09-20): sessions and volume say the client trained,
                     # not whether they finished the plan.
                     **{k: v for k, v in _week_adherence(client).items()},
                 },
                 'recent_activity': formatted_sessions
             })

        except CustomUser.DoesNotExist:
            # The URL's own object: "no such client" is exactly `not_found`.
            return Response({
                'error': _('Client not found'), 'code': 'not_found'},
                status=status.HTTP_404_NOT_FOUND
            )
    def attention(self, request):
        """GET /api/routine/trainer/client-progress/attention/ — the clients who need a
        look, most urgent first (the owner, 2026-09-20).

        Trainers and admins (403 `not_a_trainer` otherwise — the owner, 2026-09-24; it was
        the generic `permission_denied`). One batch of queries for the whole roster.

        `{trainer_id, count, clients: [{client_id, client_name, score, reasons,
        last_trained, this_week, usual_set_completion_percent}]}`. Each reason carries
        its numbers: `frozen`, `ended`, `unpaid`, `never_started` (assigned a routine
        and never trained: no finished session, no closed day), `quiet` (+`days`, null
        for a client who never trained and has nothing assigned),
        `slipping` (+`this_week`, `usual` — measured against that client's own four-week
        average, so a steady 70% is not an alarm), `missing_sets` (+`missed`,
        `planned`). A client with nothing to report is left out. Identical on every
        build; no price and no claim. The free first workout (Road to 10 S2.4) is not
        training here: a client who did only that reads `never_started`, and it is in
        none of `this_week`'s numbers.
        """
        from .outcomes import attention as needs_attention
        from users.models import CustomUser

        if not (request.user.is_trainer or request.user.is_admin):
            return Response({'error': _('This endpoint is only for trainers'),
                             'code': 'not_a_trainer'},
                            status=status.HTTP_403_FORBIDDEN)
        # `accessible_user_ids` answers None for an admin, meaning "no restriction";
        # `ids or []` read that as "nobody" and showed an admin an empty roster.
        ids = accessible_user_ids(request.user)
        clients = CustomUser.objects.filter(user_type='client')
        if ids is not None:
            clients = clients.filter(id__in=ids)
        clients = list(clients.only('id', 'first_name', 'last_name', 'username')[:200])
        names = {client.pk: (f"{client.first_name or ''} {client.last_name or ''}".strip()
                             or client.username) for client in clients}
        rows = needs_attention(request.user, [client.pk for client in clients])
        for row in rows:
            row['client_name'] = names.get(row['client_id'])
        return Response({'trainer_id': request.user.id, 'count': len(rows), 'clients': rows})

    @action(detail=False, methods=['get'], url_path='recent')
    def recent_progress(self, request):
        """GET /api/routine/trainer/client-progress/recent/ — the roster page.
        Trainers only (403 `not_a_trainer` otherwise — the owner, 2026-09-24; it was the
        generic `permission_denied`).

        `{trainer_id, trainer_name, client_count, recent_activity: [...]}`. Each entry
        carries `unread_count` (S2.3: messages from that client the trainer has not
        read, as the chat list counts them), the client, their last session (with its
        `id`, S2.3), this week's exercises and, since
        2026-09-20, `week_plan` — that client's week against the coach's plan
        (`sets_planned`, `sets_logged`, `sets_missed`, `days_closed`, `perfect_days`,
        `short_days`) — and `trend`, how many of their exercises are `rising`, `easing`
        or `steady` over the last 8 weeks. Both are absent for a client with no plan and
        no logged set; the full numbers are on `trainer/client-progress/{id}/`.

        Each entry also carries `coaching_refusal` (additive, BACKEND_BUGS B-201): null
        while that client's coaching may be used, else `coaching_frozen`,
        `coaching_unpaid` or `coaching_ended` — the key `trainer/clients/` and the
        client's file carry, so the roster needs no second request to show a paused
        client. The row holds no personal record to withhold (names, picture, training).

        One batch of queries for the whole roster (it was ~509 on a full page).
        Identical on every build.
        """
        user = request.user
        if not user.is_trainer:
            return Response(
                {'error': _('Only trainers can access this endpoint'), 'code': 'not_a_trainer'},
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
        # Each row's unread count (Road to 10 S2.3), the chat list's own annotation, for
        # the whole roster in one query.
        from chat.serializers import conversation_queryset

        unread = dict(conversation_queryset(user)
                      .filter(client_id__in=[client.pk for client in clients])
                      .values_list('client_id', 'unread_count'))
        # With the request: `client_info.profile_picture` is absolute here as it is
        # everywhere else (the same shape bug as BACKEND_BUGS B-032, on the picture).
        from users.coaching import refusals_for

        refusals = refusals_for(user.pk, [client.pk for client in clients])
        recent_activity = [
            {**RecentActivitySerializer(
                client, context={"activity": activity, "request": request,
                                 "unread": unread}).data,
             'coaching_refusal': refusals.get(client.pk)}
            for client in clients
        ]
        
        return Response({
            'trainer_id': user.id,
            'trainer_name': user.username,
            'client_count': len(recent_activity),
            'recent_activity': recent_activity
        })