from django.core.validators import MinValueValidator
from django.db import transaction
from rest_framework import serializers
from django.conf import settings
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from django.contrib.auth import get_user_model
from .models import (
    Routine, Exercise, RoutineExercise,ExerciseMedia,
    UserExerciseProgress, RoutineProgress, ExerciseSetLog, WorkoutSession,
    RoutineTemplate, RoutineTemplateExercise, DayOutcome
)
from .models import (DAY_NAME_MAX_LENGTH, SUPERSET_GROUP_MAX, load_value_problem,
                     merge_day_names)
from .sessions import SESSION_MAX_SECONDS
import logging
from django.db.models import Sum, F
from users.models import CustomUser

logger = logging.getLogger(__name__)


class ExerciseMediaSerializer(serializers.ModelSerializer):
    """Serializer for Exercise Media (e.g., videos, photos, text).

    `content` is what the app loads: a photo the platform stored (`create-with-image/`)
    is a URL signed at the moment it is served, and it expires like every media URL —
    read it again rather than keeping it. The row keeps the storage name; it used to
    keep a signed URL, which died an hour after the upload (SIDE_BUGS S-070). A URL a
    coach gave, and a text tip, are served as they were written.
    """
    content = serializers.SerializerMethodField()

    class Meta:
        model = ExerciseMedia
        fields = ['id', 'media_type', 'content']

    def get_content(self, obj):
        return obj.content_url(self.context.get('request'))


class ExerciseMediaSlotsMixin:
    """`media_slots` and `media_source`: WHOSE demonstration this viewer is shown.

    The rule and the reasons are in `mediakit/resolution.py`; this only serves its
    answer. In order: the author of the routine this exercise was read out of, else a
    trainer the viewer has an approved relation with, else the shared catalogue.

    Why not `media`. `media` on an exercise has been the legacy `ExerciseMedia` list —
    `[{id, media_type, content}]` — since before the slot system existed, and every
    build in the field reads it. The contract is additive or nothing (CLAUDE.md), so
    the slots arrive beside it under their own name and the legacy list is untouched.
    `media_slots` is the same shape the rest of the platform serves as `media`:
    `{"<slot>": {url_1x, url_2x, blur, focal, ...}}` for a still,
    `{video_url, poster_url, state, ...}` for a video, and an empty slot is absent.

    `media_source` is `routine_author`, `my_trainer`, `catalogue`, or null when there
    is no media anywhere; `media_trainer_id` names the coach (null for the
    catalogue). A trainer reading their own library gets `my_trainer` with their own
    id — they are the trainer they are connected to.

    A page is resolved in one go by `routine.media_context`. Without it this still
    answers correctly, one query per exercise.
    """
    def _resolved_media(self, obj):
        from mediakit.resolution import ExerciseMediaCache, empty

        from .media_context import CACHE_KEY, ROW_AUTHOR_KEY

        if getattr(obj, 'pk', None) is None:
            return empty()
        cache = self.context.get(CACHE_KEY)
        if cache is None:
            request = self.context.get('request')
            cache = ExerciseMediaCache(getattr(request, 'user', None), request)
            self.context[CACHE_KEY] = cache
        return cache.get(obj.pk, self.context.get(ROW_AUTHOR_KEY))

    def get_media_slots(self, obj):
        return self._resolved_media(obj)['media']

    def get_media_source(self, obj):
        return self._resolved_media(obj)['media_source']

    def get_media_trainer_id(self, obj):
        return self._resolved_media(obj)['media_trainer_id']


class ExerciseAuthoringFieldsMixin(serializers.Serializer):
    """Whose exercise it is, and what it is done with (BACKEND_BUGS B-102, B-107).

    `is_mine`        true when the READER wrote this exercise (their own library), false
                     for the shared catalogue and for anybody else's — the same exercise
                     reads true to its trainer and false to that trainer's client.
    `created_by_id`  the trainer who wrote it, or null for the shared catalogue.
    `equipment`      a code: `barbell`, `dumbbell`, `cable`, `machine`, `bodyweight`,
                     or null when not said (every exercise written before the field).
                     Writable; any other code answers 400 `invalid_choice` on the field.
    `equipment_display`  its label in the reader's language («دمبل»), or null.

    Read from the row itself (`created_by_id`, and the request's user): no query.
    """
    is_mine = serializers.SerializerMethodField()
    created_by_id = serializers.IntegerField(read_only=True)
    equipment_display = serializers.CharField(source='get_equipment_display',
                                              read_only=True, allow_null=True)

    def get_is_mine(self, obj):
        request = self.context.get('request')
        user_id = getattr(getattr(request, 'user', None), 'pk', None)
        return user_id is not None and obj.created_by_id == user_id

    def validate_equipment(self, value):
        """An empty string is "not said": stored as null, like every exercise written
        before the field."""
        return value or None


class ExerciseSerializer(ExerciseAuthoringFieldsMixin, ExerciseMediaSlotsMixin,
                         serializers.ModelSerializer):
    """
    Serializer for Exercises, with muscle group targeting.
    Input: name, description, target_muscle
    Output: Serialized Exercise data for API responses

    `media` is the legacy `ExerciseMedia` list. `media_slots`, `media_source` and
    `media_trainer_id` are the per-trainer library — see `ExerciseMediaSlotsMixin`.

    **Names (B-107).** `name` means what it always meant: the reader's language, the
    other language when that one is empty; writing `name` writes the reader's language
    (`Accept-Language`). `name_ar` and `name_en` are the two names themselves, readable
    and writable — TrainerNewExercise sends both. A create needs a name in at least one
    of `name`, `name_ar`, `name_en` (400 `required` on `name` otherwise, as before);
    when `name` and the reader's own `name_<lang>` are both sent, `name_<lang>` wins.

    `is_mine`, `created_by_id`, `equipment`, `equipment_display`: see
    `ExerciseAuthoringFieldsMixin`.
    """
    media = ExerciseMediaSerializer(many=True, read_only=True)
    media_slots = serializers.SerializerMethodField()
    media_source = serializers.SerializerMethodField()
    media_trainer_id = serializers.SerializerMethodField()
    thumbnail = serializers.SerializerMethodField()
    difficulty_level = serializers.CharField(read_only=True)
    # The stored value is the client's key; the label is what a person reads, and it
    # goes through the translation catalogue. Serving only the raw value meant an
    # Arabic client displayed "Front Quads" and "beginner" in English or wrote its own
    # second copy of the vocabulary.
    target_muscle_display = serializers.CharField(
        source='get_target_muscle_display', read_only=True)
    difficulty_level_display = serializers.CharField(
        source='get_difficulty_level_display', read_only=True)

    class Meta:
        model = Exercise
        fields = ['id', 'name', 'name_ar', 'name_en', 'description', 'target_muscle',
                  'target_muscle_display', 'difficulty_level', 'difficulty_level_display',
                  'equipment', 'equipment_display', 'image', 'thumbnail',
                  'media', 'media_slots', 'media_source', 'media_trainer_id',
                  'is_mine', 'created_by_id']
        # A name may arrive as `name_ar` / `name_en` alone; `validate` still refuses a
        # create with no name at all.
        extra_kwargs = {'name': {'required': False}}

    def validate(self, attrs):
        # modeltranslation's own: always a language the model has a column for, the
        # one its `name` descriptor and constructor write.
        from modeltranslation.utils import get_language

        attrs = super().validate(attrs)
        if self.instance is None and not any(
                str(attrs.get(f) or '').strip() for f in ('name', 'name_ar', 'name_en')):
            raise serializers.ValidationError({'name': [serializers.ErrorDetail(
                self.fields['name'].error_messages['required'], code='required')]})
        # `name` writes the reader's language. Sent beside that language's own column,
        # the explicit column wins, whatever order the two are applied in.
        own = f"name_{get_language()}"
        if 'name' in attrs and own in attrs:
            attrs.pop('name')
        # A create sent only as `name_ar` / `name_en` still fills the untranslated
        # `name` column — the one `.values('exercise__name')` reads (the analyzers, the
        # set-log summaries) — with the default language's name, else the other one.
        # The reader's own column is named explicitly, so modeltranslation's
        # constructor cannot copy that fallback into it (an English name would
        # otherwise land in `name_ar` for an Arabic reader).
        if self.instance is None and not str(attrs.get('name') or '').strip():
            attrs.setdefault(own, None)
            attrs['name'] = next(v for v in (attrs.get('name_en'), attrs.get('name_ar'))
                                 if str(v or '').strip())
        return attrs

    # `get_image` stood here and never ran: `image` is a plain model field in
    # `Meta.fields`, not a method field (BACKEND_BUGS B-036). DRF's ImageField already
    # answers an absolute URL when the request is in the context.

    def validate_image(self, value):
        """Sniff the content, cap the pixels, re-encode, and name the file after the
        format that was actually found.

        This is the serializer behind `POST`/`PATCH`/`PUT /api/routine/exercises/`, and
        it was the last write path that stored an upload without the check — the
        dedicated `exercises/{id}/image/` endpoint and `exercises/create-with-image/`
        both call `process_uploaded_image` already. DRF's `ImageField` only asks Pillow
        whether the bytes open: a GIF header with a PHP payload after it opens, and the
        file was then stored under the CLIENT's filename, so a `.php` landed in media.
        There is no pixel cap on that path either, and the stored bytes keep their EXIF
        — a client's GPS coordinates travelling with an exercise photo.
        """
        if not value:
            return value
        from django.core.exceptions import ValidationError as DjangoValidationError
        from training_platform.file_security import process_uploaded_image
        import os as _os
        try:
            safe_file, ext = process_uploaded_image(value, max_bytes=5 * 1024 * 1024)
        except DjangoValidationError as e:
            raise serializers.ValidationError(
                e.messages[0] if getattr(e, 'messages', None) else str(e))
        # The DETECTED format decides the extension; only the stem of the client's name
        # survives, so `shell.php.jpg` cannot come back out of storage as `shell.php`.
        base = _os.path.splitext(getattr(value, 'name', 'exercise'))[0][:60] or 'exercise'
        safe_file.name = f"{base}.{ext}"
        return safe_file

    def get_thumbnail(self, obj):
        """Get first image/video thumbnail from media"""
        # optimization: iterate if prefetched
        all_media = obj.media.all()
        for media in all_media:
             if media.media_type in ['photo', 'video']:
                 # Signed when served, the same URL `media[].content` carries (S-070).
                 return media.content_url(self.context.get('request'))
        return None


class ExerciseCreateWithImageSerializer(ExerciseAuthoringFieldsMixin, ExerciseMediaSlotsMixin,
                                        serializers.ModelSerializer):
    """
    Serializer for creating exercises with optional image upload.
    Input: name, description, target_muscle, image (optional)
    Output: Serialized Exercise data for API responses

    Also answers `name_ar`, `name_en`, `equipment`, `equipment_display`, `is_mine` and
    `created_by_id`, exactly as `ExerciseSerializer` does (B-102, B-107).
    """
    media = ExerciseMediaSerializer(many=True, read_only=True)
    media_slots = serializers.SerializerMethodField()
    media_source = serializers.SerializerMethodField()
    media_trainer_id = serializers.SerializerMethodField()
    image = serializers.SerializerMethodField()

    class Meta:
        model = Exercise
        fields = ['id', 'name', 'name_ar', 'name_en', 'description', 'target_muscle',
                  'equipment', 'equipment_display', 'image', 'media',
                  'media_slots', 'media_source', 'media_trainer_id',
                  'is_mine', 'created_by_id']

    def get_image(self, obj):
        request = self.context.get('request')
        if obj.image:
            url = obj.image.url
            if request is not None:
                return request.build_absolute_uri(url)
            return url
        return None

    def validate_image(self, value):
        # Content-based validation. `content_type` is a client-supplied header, so the
        # previous check let a renamed PHP/HTML payload through this serializer path
        # even though the dedicated upload endpoint rejected it.
        if not value:
            return value
        from django.core.exceptions import ValidationError as DjangoValidationError
        from training_platform.file_security import process_uploaded_image
        import os as _os
        try:
            safe_file, ext = process_uploaded_image(value, max_bytes=5 * 1024 * 1024)
        except DjangoValidationError as e:
            raise serializers.ValidationError(e.messages[0] if getattr(e, 'messages', None) else str(e))
        base = _os.path.splitext(getattr(value, 'name', 'exercise'))[0][:60] or 'exercise'
        safe_file.name = f"{base}.{ext}"
        return safe_file


def _absolute_picture(user, request):
    """A profile picture as an ABSOLUTE url, the way every other endpoint answers it.

    Two serializers here returned `profile_picture.url` raw, so the same field came
    back as `/media/...` from the trainer's roster and as `https://.../media/...` from
    `users/serializers.py`, `chat/serializers.py` and `users/trainer_directory.py` —
    the app got two shapes for one field (BACKEND_BUGS B-032 was this exact bug on
    `exercise.image`). Relative is still returned when no request is in context, which
    is what `request.build_absolute_uri` cannot do anything about.
    """
    picture = getattr(user, 'profile_picture', None)
    if not picture:
        return None
    url = picture.url
    if request is not None and url.startswith('/'):
        return request.build_absolute_uri(url)
    return url


class AuthorableExerciseField(serializers.PrimaryKeyRelatedField):
    """An exercise the CALLER may write into a routine or a template (BACKEND_BUGS
    B-139): a trainer's own or the shared catalogue's; an admin's, any.

    The rule is `routine.permissions.visible_exercises_for` as it reads for an author,
    and an id outside it — another trainer's private exercise — is refused exactly like
    an id that does not exist: 400 `does_not_exist` on the field, the same sentence. The
    routine path refused it with a sentence of its own and the template path with an
    English one, and either told a trainer which of another trainer's ids were real (the
    S-056 defect, on the write routes).

    Without a request in the context there is no caller to decide for: the serializer
    was built by trusted code, and the field reads every exercise as it always did.
    """

    def __init__(self, **kwargs):
        kwargs.setdefault('queryset', Exercise.objects.all())
        super().__init__(**kwargs)

    def get_queryset(self):
        from .permissions import visible_exercises_for

        request = self.context.get('request')
        user = getattr(request, 'user', None)
        if request is None or user is None:
            return Exercise.objects.all()
        if getattr(user, 'is_trainer', False):
            return visible_exercises_for(user)          # their own + the catalogue
        if getattr(user, 'is_admin', False) or getattr(user, 'is_staff', False):
            return Exercise.objects.all()
        # Nobody else writes routines or templates (their permissions refuse first);
        # never a client's wider READING rule.
        return Exercise.objects.none()


class RoutineExerciseSerializer(serializers.ModelSerializer):
    """
    Serializer for routine exercises.
    Uses nested ExerciseSerializer for read operations to provide full details.
    Uses PrimaryKeyRelatedField for write operations.

    **Supersets and load modes (BACKEND_BUGS B-104)**, readable and writable:

    `superset_group`  1-20 or null. Rows of the same routine and day sharing a number are
                      one superset (the design's bracket), done back to back in `order`;
                      the same number on another day is another superset. 400
                      `superset_group_out_of_range` outside 1-20.
    `load_mode`       `kg` (the default), `percent` (of the client's one-rep max) or
                      `rpe`. Anything else: 400 `invalid_choice`.
    `load_value`      null in `kg`; the percent (1-100) in `percent`; the RPE (1-10, in
                      steps of 0.5) in `rpe`. 400 `load_value_required` (percent or rpe
                      without it), `load_value_out_of_range`, `load_value_not_allowed`
                      (a value beside kg). Switching a row to `kg` without sending
                      `load_value` clears it.
    `weight`          unchanged: the kilograms, or null. In `percent` it is the percentage
                      as the trainer resolved it against the client's record when writing
                      it (TrainerScheme's «= 66 kg»); in `rpe` an optional starting load.
                      The server never re-resolves it.

    Every code above is on its field (`field_errors.<field>[0].code`).

    **The row's refusals (BACKEND_BUGS B-139)**, each on its field:

    `exercise_id`  `does_not_exist` — no such exercise, OR one the caller may not put in
                   a routine (another trainer's private exercise). The two answer the
                   same sentence, so the answer tells nobody which ids exist
                   (`AuthorableExerciseField`).
    `exercise`     `exercise_retired` — an exercise taken out of use (the code the
                   template path answers for the same fault).
    `routine`      `not_routine_owner` — a routine another trainer wrote (the code
                   `routines/{id}/copy_week/` answers for the same fault).
    `day`          `day_out_of_range` — a day past the routine's `days`, on a create or
                   an edit (the code `update_progress` and `bulk-complete` answer).
    """
    exercise = ExerciseSerializer(read_only=True)
    exercise_id = AuthorableExerciseField(source='exercise', write_only=True)
    routine = serializers.PrimaryKeyRelatedField(queryset=Routine.objects.all())
    # Declared, so the refusal names its own code rather than `min_value` / `max_value`.
    superset_group = serializers.IntegerField(required=False, allow_null=True)

    class Meta:
        model = RoutineExercise
        # `weight`: the prescribed working weight in kg, null for none. It was on the model
        # and reachable by no endpoint (BACKEND_BUGS B-012).
        fields = ['id', 'routine', 'exercise', 'exercise_id', 'sets', 'reps', 'rest_time', 'day', 'order', 'notes', 'weight',
                  'superset_group', 'load_mode', 'load_value',
                  'created_at', 'updated_at']
        read_only_fields = ['created_at', 'updated_at']

    def validate_superset_group(self, value):
        if value is not None and not 1 <= value <= SUPERSET_GROUP_MAX:
            raise serializers.ValidationError(
                _("A superset number is from 1 to %(n)s.") % {"n": SUPERSET_GROUP_MAX},
                code="superset_group_out_of_range")
        return value

    def _validate_load(self, attrs):
        """The mode and its value, judged together, against what the row already holds
        for whichever of the two this write does not send."""
        if attrs.get('load_mode') == 'kg' and 'load_value' not in attrs:
            attrs['load_value'] = None
        mode = attrs.get('load_mode', getattr(self.instance, 'load_mode', None) or 'kg')
        value = attrs.get('load_value', getattr(self.instance, 'load_value', None))
        problem = load_value_problem(mode, value)
        if problem is not None:
            raise serializers.ValidationError(
                {'load_value': [serializers.ErrorDetail(problem[1], code=problem[0])]})
        return attrs

    def to_representation(self, instance):
        """Name the coach who prescribed this row, for the length of this row.

        Line (a) of the media rule — "the coach who prescribed it is the one
        demonstrating it" — is a fact about the ROUTINE, and the nested
        `ExerciseSerializer` only ever sees the exercise. The author is put in the
        shared context around this one row and taken out again: serialization is
        sequential and depth-first, so the nested serializer reads the value for the
        row it is inside, and a later row (or a later routine, in a list of them)
        cannot inherit it. The author map is built once per page by
        `routine.media_context`, so this costs no query.
        """
        from .media_context import AUTHORS_KEY, ROW_AUTHOR_KEY

        authors = self.context.get(AUTHORS_KEY) or {}
        if instance.routine_id in authors:
            author = authors[instance.routine_id]
        elif instance.routine_id:
            # Not prepared (a write response, or a view without the mixin). One query,
            # and `in authors` above is what stops a routine with no author — the map
            # holds the None — from paying for it on every row.
            author = (Routine.objects.filter(pk=instance.routine_id)
                      .values_list('created_by_id', flat=True).first())
        else:
            author = None
        previous = self.context.get(ROW_AUTHOR_KEY)
        self.context[ROW_AUTHOR_KEY] = author
        try:
            return super().to_representation(instance)
        finally:
            self.context[ROW_AUTHOR_KEY] = previous

    def validate_weight(self, value):
        if value is not None and not 0 <= value <= 1000:
            raise serializers.ValidationError(_("Weight must be between 0 and 1000 kg."),
                                              code="weight_out_of_range")
        return value

    def validate(self, attrs):
        user = self.context['request'].user
        routine = attrs.get('routine')
        exercise = attrs.get('exercise')

        if user.is_trainer:
            # 1. Validation: Trainer owns the routine
            # Check if routine exists (for updates) or is being set
            if routine and routine.created_by_id != user.id:
                raise serializers.ValidationError({"routine": [serializers.ErrorDetail(
                    _("You can only add exercises to routines you created."),
                    code='not_routine_owner')]})

            # 2. The exercise is the trainer's own or the catalogue's: `exercise_id`
            # (`AuthorableExerciseField`) has already answered anything else exactly
            # like an id that does not exist. It used to be refused HERE with its own
            # sentence, which told a trainer that another trainer's id was real.
            if exercise is not None and not exercise.is_active:
                raise serializers.ValidationError({"exercise": [serializers.ErrorDetail(
                    _("That exercise has been retired and cannot be added."),
                    code='exercise_retired')]})

        # Checked here rather than on the model: `days` belongs to the routine, so a
        # trainer shortening a routine would otherwise invalidate rows already written
        # against the old length. Asking at the point the day is chosen keeps the
        # answer true when it is given.
        day = attrs.get('day', getattr(self.instance, 'day', None))
        routine = routine or getattr(self.instance, 'routine', None)
        if day is not None and routine is not None and day > routine.days:
            raise serializers.ValidationError({"day": [serializers.ErrorDetail(
                _("Day %(day)s exceeds the routine's %(days)s days.")
                % {"day": day, "days": routine.days}, code='day_out_of_range')]})

        return self._validate_load(attrs)

    def create(self, validated_data):
        """Override create to sync the M2M exercises field on the routine."""
        routine_exercise = super().create(validated_data)
        
        # Ensure the exercise is added to the routine's ManyToMany field
        routine = routine_exercise.routine
        exercise = routine_exercise.exercise
        routine.exercises.add(exercise)
        
        return routine_exercise


from training_platform.utils.serializers import TranslatedJSONFieldMixin


def clean_day_names(value):
    """A `day_names` write as `{"<day>": name or None}`, None meaning "remove that day's
    name" (BACKEND_BUGS B-104). Names are trimmed; an empty one is a removal.

    Refused on the field, each with its own code: `day_names_invalid` for anything but
    an object whose keys are day numbers written plainly ("3", never "03" or "٣") and
    whose values are text or null; `too_long` for a name over `DAY_NAME_MAX_LENGTH`
    once trimmed. Whether each day is inside the routine is `RoutineSerializer.validate`'s
    question, because `days` may arrive in the same write.
    """
    def invalid():
        return serializers.ValidationError(
            _("Day names must be an object of day number to name."), code='day_names_invalid')

    if not isinstance(value, dict):
        raise invalid()
    cleaned = {}
    for key, name in value.items():
        if not (isinstance(key, str) and key.isascii() and key.isdigit()
                and key == str(int(key))):
            raise invalid()
        if name is None:
            cleaned[key] = None
            continue
        if not isinstance(name, str):
            raise invalid()
        name = name.strip()
        if len(name) > DAY_NAME_MAX_LENGTH:
            raise serializers.ValidationError(
                _("Keep this to %(n)s characters.") % {"n": DAY_NAME_MAX_LENGTH},
                code='too_long')
        cleaned[key] = name or None
    return cleaned


class RoutineAssigneesMixin(serializers.Serializer):
    """Who a routine is for, as far as the CALLER may know (BACKEND_BUGS B-001).

    A routine is written by a coach for ONE client (owner, 2026-09-19): clients never see
    each other. The trainer who wrote it (or an admin) sees its assignee; another trainer
    sees only their own approved clients among them; a client sees only themselves. The
    keys stay (`assigned_to`, `assigned_usernames`, `client_count`); their contents follow
    the caller.

    `assigned_ids` (BACKEND_BUGS B-152, additive): the same people as user ids, under the
    same rule. `assigned_to` is display text — `str(user)`, a username and a translated
    user-type label — and stays exactly that; match a client to a routine on
    `assigned_ids`.
    """
    assigned_to = serializers.SerializerMethodField()
    assigned_usernames = serializers.SerializerMethodField()
    assigned_ids = serializers.SerializerMethodField()
    client_count = serializers.SerializerMethodField()

    def _visible_assignees(self, obj):
        request = self.context.get('request')
        user = getattr(request, 'user', None)
        people = list(obj.assigned_to.all())
        if user is None or getattr(user, 'is_admin', False) or obj.created_by_id == user.id:
            return people
        if getattr(user, 'is_trainer', False):
            if not hasattr(self, '_accessible_ids'):
                from .permissions import accessible_user_ids
                self._accessible_ids = accessible_user_ids(user)
            ids = self._accessible_ids
            return people if ids is None else [p for p in people if p.id in ids]
        return [p for p in people if p.id == user.id]

    def get_assigned_to(self, obj):
        return [str(p) for p in self._visible_assignees(obj)]

    def get_assigned_usernames(self, obj):
        """Retrieve assigned user names."""
        return [p.username for p in self._visible_assignees(obj)]

    def get_assigned_ids(self, obj):
        """The assignees the caller may know about, as user ids (B-152)."""
        return [p.pk for p in self._visible_assignees(obj)]

    def get_client_count(self, obj):
        """Clients this routine is assigned to that the caller may know about."""
        return sum(1 for p in self._visible_assignees(obj) if p.user_type == 'client')

class RoutineSerializer(RoutineAssigneesMixin, TranslatedJSONFieldMixin, serializers.ModelSerializer):
    """
    Enhanced Serializer for Routines with improved assignment validation.
    
    Features:
    - Validates that only trainers can create routines
    - Ensures assignment only to approved clients
    - Comprehensive error handling and logging
    - Support for assignment validation
    
    TODO: Add routine template support
    TODO: Implement routine cloning functionality
    TODO: Add routine sharing between trainers

    `created_by_id` (BACKEND_BUGS B-111, read-only): the author's user id. `created_by`
    keeps its display text. The coaching freeze (`users/coaching.training_refusal`)
    holds exactly the routines whose `created_by_id` is the frozen coaching's trainer.

    `day_names` (BACKEND_BUGS B-104): `{"<day>": "<name>"}` — the day as a string, only
    named days present, a name at most 40 characters. A write MERGES by day: a day sent
    with a name sets it, a day sent with null or "" removes its name, a day not sent
    keeps its name (so two phones naming two days never erase each other's). Refusals
    on `day_names`: 400 `day_names_invalid` (not an object of day number to text),
    `day_out_of_range` (a day outside 1..`days`), `too_long`. Shortening `days` drops
    the names of the days the routine no longer has.
    """
    routine_exercises = RoutineExerciseSerializer(many=True, read_only=True)
    created_by = serializers.StringRelatedField(read_only=True)
    created_by_id = serializers.IntegerField(read_only=True)
    day_names = serializers.JSONField(required=False)
    estimated_duration_minutes = serializers.SerializerMethodField()
    target_muscles = serializers.SerializerMethodField()
    product = serializers.SerializerMethodField()

    translated_fields = ['name', 'description']

    class Meta:
        model = Routine
        fields = [
            'id', 'name', 'description', 'difficulty_level', 'is_active', 'days', 'start_date', 'end_date',
            'created_at', 'updated_at', 'routine_exercises', 'assigned_to', 'assigned_usernames',
            'created_by', 'client_count', 'estimated_duration_minutes', 'target_muscles',
            'source_template', 'product', 'created_by_id', 'day_names', 'assigned_ids',
        ]
        # `source_template`: the template this private copy was generated from, or null.
        # A copy, not a link — set by `templates/{id}/generate/`, never by a write here.
        read_only_fields = ['created_at', 'updated_at', 'created_by', 'source_template']

    def get_product(self, obj):
        """The routine product this routine is the reader's bought copy of (plan step
        10), or null for every other routine: `{id, title, trainer: {id, name}}`.
        Additive. `created_by` of a bought copy is the buyer (the copy is theirs, not a
        coaching); the program's author is `product.trainer`. Costs nothing for a
        routine that was not bought (`purchase_id` is on the row)."""
        if getattr(obj, 'purchase_id', None) is None:
            return None
        from django.utils.translation import get_language

        product = obj.purchase.product
        trainer = product.trainer
        return {'id': product.pk, 'title': product.title_in(get_language() or 'ar'),
                'trainer': {'id': trainer.pk,
                            'name': trainer.full_name or trainer.username}}

    def validate_day_names(self, value):
        """The names as `{"<day>": name or None}`, None meaning "remove". Whether each
        day is inside the routine is `validate`'s question: `days` may arrive with it."""
        return clean_day_names(value)

    def get_estimated_duration_minutes(self, obj):
        """
        Calculate estimated duration based on sets, reps (4s/rep), and rest times.
        Formula: sum(sets * reps * 4s) + sum((sets - 1) * rest_time)
        """
        total_seconds = 0
        # Use all() to leverage prefetch_related
        exercises = obj.routine_exercises.all()
        for ex in exercises:
            sets = ex.sets or 3
            reps = ex.reps or 10
            rest = ex.rest_time or 60
            
            # Work time (approx 4s per rep)
            work_time = sets * reps * 4
            # Rest time
            rest_time = (sets - 1) * rest if sets > 1 else 0
            
            total_seconds += work_time + rest_time
            
        # Add transition time (e.g., 2 mins between exercises)
        if len(exercises) > 1:
            total_seconds += (len(exercises) - 1) * 120
            
        return round(total_seconds / 60)

    def get_target_muscles(self, obj):
        """Get list of unique target muscles in this routine."""
        # Avoid select_related here as it conflicts with prefetch_related
        # iterate over all() instead which should be prefetched
        return list(set(
            ex.exercise.target_muscle 
            for ex in obj.routine_exercises.all()
        ))

    def create(self, validated_data):
        days = validated_data.get('days') or Routine._meta.get_field('days').default
        if 'day_names' in validated_data:
            validated_data['day_names'] = merge_day_names(
                {}, validated_data['day_names'], days)
        return super().create(validated_data)

    def update(self, instance, validated_data):
        # An edited name or description is what every language shows until it is
        # translated again: the stored translation used to hide the edit (see
        # `Routine.forget_translations`).
        edited = [f for f in ('name', 'description') if f in validated_data]
        if edited:
            validated_data['translations'] = Routine.forget_translations(
                instance.translations, *edited)
        # `day_names` merges by day under a lock on the routine, from the names as they
        # are NOW — not as this request read them — so two phones naming two days never
        # erase each other's. Every update takes the lock, because `save()` writes the
        # whole row: a rename that saved the names it had read would put back a name
        # another phone had just removed. Days past the (new) last day lose their name.
        with transaction.atomic():
            current = (Routine.objects.select_for_update().filter(pk=instance.pk)
                       .values_list('day_names', flat=True).first())
            days = validated_data.get('days', instance.days)
            validated_data['day_names'] = merge_day_names(
                current, validated_data.get('day_names'), days)
            return super().update(instance, validated_data)

    def validate(self, attrs):
        """
        Enhanced validation for routine creation and assignment.
        
        Validates:
        - Only trainers and admins can create routines
        - Assignment only to approved clients
        - Proper error messages and logging
        """
        request = self.context.get('request')
        if request and request.user:
            # Only trainers and admins can create routines
            if not request.user.is_trainer and not request.user.is_admin:
                logger.warning(f"User {request.user.id} attempted to create routine without permission")
                raise serializers.ValidationError(_("Only trainers and admins can create routines"))
            
            # Enhanced assignment validation for trainers
            if request.user.is_trainer:
                assigned_to = self.initial_data.get('assigned_to', [])
                if assigned_to:
                    self._validate_client_assignments(request.user, assigned_to)

        # Every named day must be a day of the routine — the routine as this write
        # leaves it, since `days` may change in the same request.
        names = attrs.get('day_names')
        if names:
            days = attrs.get('days', getattr(self.instance, 'days', None))
            if days is None:
                days = Routine._meta.get_field('days').default
            for key in names:
                if not 1 <= int(key) <= days:
                    raise serializers.ValidationError({'day_names': [serializers.ErrorDetail(
                        _("Day %(day)s is outside the routine's %(days)s days.")
                        % {"day": key, "days": days}, code='day_out_of_range')]})

        return attrs

    def _validate_client_assignments(self, trainer, assigned_to):
        """
        Validate that all assigned clients are approved for this trainer.
        
        Args:
            trainer: The trainer user
            assigned_to: List of client IDs or usernames to assign
            
        Raises:
            ValidationError: If any client is not approved
        """
        from users.models import CustomUser, TrainerClientRelation
        
        # Handle different input formats
        if isinstance(assigned_to, str):
            import json
            try:
                assigned_to = json.loads(assigned_to)
            except Exception:
                assigned_to = [assigned_to]
        
        if not isinstance(assigned_to, list):
            assigned_to = [assigned_to]
        
        unapproved_clients = []
        
        for client_identifier in assigned_to:
            try:
                # Try to get client by ID first, then by username
                if isinstance(client_identifier, int) or str(client_identifier).isdigit():
                    client = CustomUser.objects.get(id=client_identifier, user_type='client')
                else:
                    client = CustomUser.objects.get(username=client_identifier, user_type='client')
                
                # Check if trainer-client relation is approved
                if not TrainerClientRelation.objects.filter(
                    trainer=trainer, 
                    client=client, 
                    status='approved'
                ).exists():
                    unapproved_clients.append(client.username)
                    
            except CustomUser.DoesNotExist:
                raise serializers.ValidationError(
                    f"Client with identifier '{client_identifier}' does not exist."
                )
        
        if unapproved_clients:
            logger.warning(
                f"Trainer {trainer.id} attempted to assign routine to unapproved clients: {unapproved_clients}"
            )
            raise serializers.ValidationError(
                f"You can only assign routines to your approved clients. "
                f"Unapproved clients: {', '.join(unapproved_clients)}"
            )


def fixed_once_written(attrs, instance, fields, code, message):
    """Refuse an update that MOVES a row to another key, and allow one that repeats it.

    Every training row is identified by what it is about — whose exercise, which
    routine, which day, which date — and the authorisation that let it be written was
    decided from exactly those values on create. An update that changes one of them is
    a create the server never authorised: the same row, now describing work on another
    routine or another day. Re-sending the value a row already holds is what a PUT of
    the whole object does, and it changes nothing, so it is allowed.

    Raises 400 `<code>` on the field that was moved.
    """
    if instance is None:
        return attrs
    for field in fields:
        if field not in attrs:
            continue
        current = getattr(instance, field, None)
        if attrs[field] != current:
            raise serializers.ValidationError(
                {field: [serializers.ErrorDetail(message, code=code)]})
        attrs.pop(field)
    return attrs


def training_date_refusal(day, today=None):
    """`(code, message)` when `day` is outside the window a training write accepts.

    The window `set-logs/bulk-create/`, `set-logs/sync/` and
    `user-exercise-progress/bulk-complete/` already held a day to: not in the future,
    and not more than `MAX_BACKLOG_DAYS` back, with the same two codes. The plain
    `POST user-exercise-progress/` and `POST set-logs/` had no bound at all, so a day
    could be recorded ahead of time — it entered `training_dates`, and the streak read a
    run ending in the future as current (SIDE_BUGS S-112). None when the day is fine.
    """
    if day is None:
        return None
    # `routine.views` imports this module, so its constant is read at call time.
    from .views import MAX_BACKLOG_DAYS

    today = today or timezone.localdate()
    if day > today:
        return 'date_in_future', _('Cannot log a workout in the future.')
    if (today - day).days > MAX_BACKLOG_DAYS:
        return 'date_too_old', (_('Cannot log a workout more than %(d)d days in the past.')
                                % {'d': MAX_BACKLOG_DAYS})
    return None


class ReadableExerciseField(serializers.PrimaryKeyRelatedField):
    """An exercise the CALLER may read — or has already trained.

    `POST user-exercise-progress/` took any exercise id, and the list and detail then
    nest the whole exercise (`UserExerciseProgressDetailSerializer`): name,
    description, image, media. So anyone could walk ids and read every trainer's private
    library, signed video links included (SIDE_BUGS S-056). The rule is
    `routine.permissions.visible_exercises_for`, and an id outside it is refused
    exactly like one that does not exist — the same 400 `does_not_exist` on `exercise`,
    so the answer tells nobody which ids are real.

    One addition, the one `analytics/strength/` makes: an exercise the caller already
    has progress on is theirs to log again whatever became of the routine it came from.
    They lifted it, the name is not news to them, and an id cannot be guessed into a
    name because the history has to exist. The row being edited keeps its own exercise,
    so a PUT that repeats it is still the no-op `fixed_once_written` promises.

    Without a request in the context there is no caller to decide for: the serializer
    was built by trusted code, not by an API write, and the field reads every exercise
    as it always did.
    """

    def get_queryset(self):
        from django.db.models import Q

        from .permissions import visible_exercises_for

        request = self.context.get('request')
        user = getattr(request, 'user', None)
        if request is None or user is None:
            return Exercise.objects.all()
        readable = Q(pk__in=visible_exercises_for(user).values('pk'))
        if getattr(user, 'is_authenticated', False):
            readable |= Q(pk__in=UserExerciseProgress.objects.filter(user=user)
                          .values('exercise_id'))
        instance = getattr(self.parent, 'instance', None)
        if isinstance(instance, UserExerciseProgress):
            readable |= Q(pk=instance.exercise_id)
        return Exercise.objects.filter(readable)


class UserExerciseProgressSerializer(serializers.ModelSerializer):
    """Serializer for tracking user progress on individual exercises.

    **On create** (an API write, with the request in the context): `exercise` must be
    one the caller may read or has already trained — anything else answers 400
    `does_not_exist` on `exercise`, exactly like an id that does not exist
    (`ReadableExerciseField`, SIDE_BUGS S-056) — and `date` must be inside the training
    window: 400 `date_in_future` or `date_too_old` on `date`, the codes
    `set-logs/sync/` already answers (`training_date_refusal`, SIDE_BUGS S-112).

    `exercise` and `date` are FIXED once the row is written: a PATCH that changes
    either answers 400 `progress_immutable` on that field, and re-sending the value the
    row already has is a no-op (so a PUT of the whole object still works).

    Read-only after create rather than re-running `perform_create`'s authorisation on
    every update, because it is the smaller rule and the stronger one: the row is
    keyed `(user, exercise, date)`, so moving either column is not an edit of this row
    but a write of a different one — and the day outcomes, the streak and the
    achievements are all recomputed from the key. Re-checking the coaching refusal
    would have allowed the move whenever the client happened to be paid up, which is
    most of the time, and a client rewriting their own history was never about payment.

    `completed_sets` and `target_sets` are DERIVED and read-only, the same rule
    `RoutineProgressSerializer` states for its own three: a client cannot author their
    own compliance. `completed_sets` is recomputed from the set logs
    (`routine/models.py` `_recalc_locked`) and `target_sets` is the COACH'S
    prescription, taken from `RoutineExercise.sets` by the routes that write training
    (`set-logs/sync/`, `set-logs/bulk-create/`, `user-exercise-progress/bulk-complete/`).
    Writable, a value sent here was either overwritten by the next recomputation or —
    with no set log behind it — a claim that completed a day that was never trained.
    Send the sets; the numbers follow.
    """
    exercise = ReadableExerciseField()
    user = serializers.PrimaryKeyRelatedField(read_only=True)

    def validate_date(self, value):
        """Inside the training window, on create. An update cannot move the date at all
        (`progress_immutable`), so re-sending an old row's own date stays a no-op."""
        if self.instance is None and self.context.get('request') is not None:
            refusal = training_date_refusal(value)
            if refusal is not None:
                code, message = refusal
                raise serializers.ValidationError(message, code=code)
        return value

    def validate(self, attrs):
        attrs = super().validate(attrs)
        return fixed_once_written(
            attrs, self.instance, ('exercise', 'date'), 'progress_immutable',
            _("A progress record cannot be moved to another exercise or date. "
              "Record the work on its own day."))

    class Meta:
        model = UserExerciseProgress
        fields = ['id', 'user', 'exercise', 'date', 'completed_sets', 'target_sets', 'skipped']
        #: DERIVED, not declared — see the class docstring. `completed_sets` comes from
        #: the set logs and `target_sets` from the coach's prescription; both were
        #: writable here while `RoutineProgressSerializer` had already locked exactly
        #: this pair of numbers for exactly this reason.
        read_only_fields = ['completed_sets', 'target_sets']


class UserExerciseProgressDetailSerializer(UserExerciseProgressSerializer):
    """
    Detailed serializer for UserExerciseProgress.
    Includes full exercise details instead of just ID.
    Used for read operations (GET).
    """
    exercise = ExerciseSerializer(read_only=True)
    top_set = serializers.SerializerMethodField()

    class Meta(UserExerciseProgressSerializer.Meta):
        fields = UserExerciseProgressSerializer.Meta.fields + ['top_set']

    def get_top_set(self, obj):
        """That day's «أفضل مجموعة» (BACKEND_BUGS B-181): the set with the highest weight
        x reps, among equals the heavier weight — the owner's rule, and the same key and
        shape as `routine/strength.py` answers on `analytics/strength/`. `{weight, reps,
        volume_kg}`, or null when no set of the day has both a weight and reps. Reads
        the prefetched `set_logs` (the viewset prefetches them for list and detail)."""
        from .strength import _set_volume_key, _top_set_payload

        best = None
        for log in obj.set_logs.all():
            if not log.weight or not log.reps or log.weight <= 0 or log.reps <= 0:
                continue
            if best is None or (_set_volume_key(log.weight, log.reps)
                                > _set_volume_key(*best)):
                best = (log.weight, log.reps)
        return _top_set_payload(*best) if best is not None else None





class DayOutcomeSerializer(serializers.ModelSerializer):
    """What one training day planned against what was done (routine/outcomes.py).

    Read-only everywhere: these rows are written when a day closes, never by a client.

    `quick_log` (BACKEND_BUGS B-157, additive): the day was logged after the fact in one
    go («سجّل دفعة واحدة») — `update_progress` with `quick_log: true`, or
    `bulk-complete` — rather than live. It is the coach's quick-log badge; `via` still
    says what closed the day.
    """
    missed_reps = serializers.SerializerMethodField()

    class Meta:
        model = DayOutcome
        fields = [
            'id', 'routine', 'day', 'date', 'via', 'complete', 'confirmed_unlogged',
            'planned_sets', 'logged_sets', 'missed_sets',
            'planned_reps', 'logged_reps', 'missed_reps', 'reps_known',
            'planned_volume_kg', 'logged_volume_kg',
            'exercises_planned', 'exercises_full', 'exercises_partial',
            'exercises_untouched', 'exercises_skipped',
            'prescription_exact', 'detail', 'quick_log',
        ]
        read_only_fields = fields

    def get_missed_reps(self, obj):
        """Null when the coach prescribed no reps, or when part of the day was logged a
        whole day at a time and its reps are unknown."""
        if obj.planned_reps is None or not obj.reps_known:
            return None
        return max(obj.planned_reps - (obj.logged_reps or 0), 0)


class RoutineProgressSerializer(serializers.ModelSerializer):
    """
    Enhanced serializer with rich progress data.
    Provides completion percentage, exercise summary, and next action suggestions.

    `routine`, `day` and `date` are FIXED once the row is written: a PATCH that changes
    any of them answers 400 `progress_immutable` on that field, and re-sending the
    value the row already holds is a no-op. The row IS that key — `perform_create`
    checks the routine is the caller's and that the coaching may be trained on, and
    `perform_update` re-checked `instance.routine`, so a PATCH could carry the row onto
    a routine and a date nobody had authorised and rewrite a finished day's history.
    Everything else here is derived from the set logs and already read-only.
    """
    user = serializers.StringRelatedField(read_only=True)
    routine_name = serializers.CharField(source='routine.name', read_only=True)
    routine_id = serializers.IntegerField(source='routine.id', read_only=True)
    # `routine` had no writable representation at all, so every POST left routine_id
    # NULL and died with an IntegrityError (500). `date` likewise: without it the row
    # cannot be placed on a day, which is what the streak calculation keys off.
    routine = serializers.PrimaryKeyRelatedField(
        queryset=Routine.objects.all(), write_only=True
    )
    completion_percentage = serializers.SerializerMethodField()
    exercises_summary = serializers.SerializerMethodField()
    next_suggested_action = serializers.SerializerMethodField()
    #: What this day planned against what was done, when the day has been closed
    #: (the owner, 2026-09-20). Annotated by the viewset in ONE query for the whole
    #: page; null on a day still open, or for a caller that did not annotate.
    outcome = serializers.SerializerMethodField()

    class Meta:
        model = RoutineProgress
        fields = [
            'id', 'user', 'routine', 'routine_id', 'routine_name',
            'day', 'date', 'status', 'exercises_completed', 'total_exercises',
            'completion_percentage', 'exercises_summary', 
            'next_suggested_action', 'outcome', 'updated_at'
        ]
        #: DERIVED, not declared. These three were writable, and `perform_create` only
        #: checked that the routine was assigned to the caller — never that the numbers
        #: corresponded to work done. A client could PATCH `exercises_completed: 99`
        #: against a two-exercise day and their trainer's screen read 4950%, or POST a
        #: `completed` day with no set logs behind it and manufacture a streak from two
        #: HTTP calls. The signal in routine/models.py computes all three from the set
        #: logs; nothing else may.
        read_only_fields = ['status', 'exercises_completed', 'total_exercises']

    def validate(self, attrs):
        attrs = super().validate(attrs)
        return fixed_once_written(
            attrs, self.instance, ('routine', 'day', 'date'), 'progress_immutable',
            _("A day's progress cannot be moved to another routine, day or date."))

    def get_outcome(self, obj):
        planned = getattr(obj, 'outcome_planned_sets', None)
        if planned is None:
            return None
        return {
            'complete': getattr(obj, 'outcome_complete', None),
            'confirmed_unlogged': getattr(obj, 'outcome_confirmed_unlogged', None),
            'planned_sets': planned,
            'logged_sets': getattr(obj, 'outcome_logged_sets', 0),
            'missed_sets': getattr(obj, 'outcome_missed_sets', 0),
            # Additive (BACKEND_BUGS B-157): the coach's quick-log badge, and `via`,
            # which the viewset's docstring already promised and this never carried.
            'quick_log': getattr(obj, 'outcome_quick_log', None),
            'via': getattr(obj, 'outcome_via', None),
        }

    def get_completion_percentage(self, obj):
        if obj.total_exercises == 0:
            return 0.0
        # Clamped. The inputs are derived now, but a percentage is a percentage: a
        # stale or hand-repaired row must not render as 4950% on a trainer's screen.
        return round(max(0.0, min(100.0, (obj.exercises_completed / obj.total_exercises) * 100)), 1)
    
    def get_exercises_summary(self, obj):
        """Get summary of exercises for this day with optimized queries."""
        from .models import RoutineExercise, UserExerciseProgress
        
        # N+1 FIX: this method ran two queries PER ROW (26 queries for 20 rows).
        # Results are memoised on the serializer instance and keyed by the values
        # they actually depend on, so a page of rows sharing a routine/day/date
        # costs one pair of queries instead of one pair each.
        cache = getattr(self, '_exsummary_cache', None)
        if cache is None:
            cache = self._exsummary_cache = {'rex': {}, 'prog': {}}

        rex_key = (obj.routine_id, obj.day)
        if rex_key not in cache['rex']:
            cache['rex'][rex_key] = list(
                RoutineExercise.objects.filter(routine_id=obj.routine_id, day=obj.day)
                .select_related('exercise')
            )
        routine_exercises = cache['rex'][rex_key]

        exercise_ids = [re.exercise_id for re in routine_exercises]
        # Use the real training date. This previously derived the date from
        # `updated_at` (auto_now), which pointed at the wrong day as soon as a row
        # was edited — the RoutineProgress.date field is now authoritative.
        progress_date = obj.date

        # Key the progress cache WITHOUT the date and bucket by date in memory:
        # a page of RoutineProgress rows for one routine/day spans many dates, so
        # keying per-date still cost one query per row.
        prog_key = (obj.user_id, rex_key)
        if prog_key not in cache['prog']:
            by_date = {}
            if exercise_ids:
                for p in UserExerciseProgress.objects.filter(
                    user_id=obj.user_id,
                    exercise_id__in=exercise_ids
                ).order_by('exercise_id', '-updated_at'):
                    bucket = by_date.setdefault(p.date, {})
                    if p.exercise_id not in bucket:
                        bucket[p.exercise_id] = p
            cache['prog'][prog_key] = by_date
        progress_map = cache['prog'][prog_key].get(progress_date, {})
        
        summary = []
        for re in routine_exercises:
            # Check if completed using cached progress
            is_completed = False
            progress = progress_map.get(re.exercise_id)
            
            if progress and not progress.skipped:
                if progress.target_sets > 0:
                    is_completed = progress.completed_sets >= progress.target_sets
                else:
                    is_completed = progress.completed_sets > 0

            summary.append({
                'exercise_id': re.exercise.id,
                'exercise_name': re.exercise.name,
                'target_muscle': re.exercise.target_muscle,
                'target_sets': re.sets,
                'target_reps': re.reps,
                'completed': is_completed
            })
            
        return summary
    
    def get_next_suggested_action(self, obj):
        """Suggest what user should do next, in the request's language.

        These were English sentences on every row, for an Arabic-first app (SIDE_BUGS
        S-113). Worded so no plural form depends on the count."""
        if obj.status == 'completed':
            return str(_("Great job! You've finished this day's workout. Rest up for "
                         "the next one."))
        elif obj.status == 'in_progress':
            remaining = max(obj.total_exercises - obj.exercises_completed, 0)
            return _("Keep pushing! Exercises left to complete Day %(day)s: "
                     "%(remaining)s.") % {'day': obj.day, 'remaining': remaining}
        else:
            return _("Ready to start? Begin your Day %(day)s workout now.") % {
                'day': obj.day}


class UserRoutineSerializer(serializers.ModelSerializer):
    """Serializer for User Routines with progress tracking."""
    users = serializers.PrimaryKeyRelatedField(many=True, queryset=get_user_model().objects.all())
    routine_template = RoutineSerializer(read_only=True)
    progress = RoutineProgressSerializer(many=True, read_only=True)

    class Meta:
        model = Routine
        fields = ['id', 'users', 'routine_template', 'start_date', 'end_date', 'progress']


class DayOutcomeSummarySerializer(DayOutcomeSerializer):
    """A `DayOutcomeSerializer` row without `detail` — for `analytics/adherence/`
    `by_day`, whose queryset DEFERS that column (SIDE_BUGS S-072).

    Serialising a deferred field loads it, one query per row: the adherence view
    deferred `detail`, rendered it through the full serializer anyway and then popped
    it, so a year of days cost one extra query per day to read the very column the
    deferral was there to skip."""

    class Meta(DayOutcomeSerializer.Meta):
        fields = [name for name in DayOutcomeSerializer.Meta.fields if name != 'detail']
        read_only_fields = fields


class RestTimeNotNegative(MinValueValidator):
    """`MinValueValidator` answering the code both logging routes name for a rest they
    refuse, `rest_time_out_of_range` (B-109, B-122), instead of Django's `min_value`."""
    code = 'rest_time_out_of_range'


class ExerciseSetLogSerializer(serializers.ModelSerializer):
    """
    Serializer for exercise set logs.
    Input: user_exercise_progress (PK), workout_session (PK), set_number, weight, reps, date
    Output: Serialized ExerciseSetLog data for API responses with calculated volume

    `reps` is required on a new set (the owner, 2026-09-20): an omitted rep count used
    to be stored as ten, so "reps missed this week" could never be trusted. The app's
    "as prescribed" button sends `as_prescribed: true` instead of a number, and the
    server fills in the coach's reps for that exercise on that routine day — and the
    prescribed weight too, when the coach set one and the app sent none. The prescription
    is found through the set's session; without one, send `routine` and `day` (write-only,
    used only to look it up). Field codes on `reps`: `reps_required`, `no_prescribed_reps`.

    On a PATCH only what was LIFTED may change — `weight`, `reps`, `notes`, `rpe`.
    `user_exercise_progress`, `workout_session`, `set_number` and `date` are fixed once
    written: changing one answers 400 `set_immutable` on that field, and re-sending the
    value the row already holds is a no-op. `as_prescribed` is a create-time
    convenience; an edit already carries the reps it is correcting.

    **The progress row decides the day.** `date` is a copy of
    `user_exercise_progress.date` and nothing else: send it and it must match (400
    `set_date_mismatch` on `date`), omit it and the set is TODAY's — and must match the
    row just the same, so an undated set cannot be backdated onto an older row
    rather than today. `routine/models.py ExerciseSetLog` says why the two must agree.
    A new set's day must also be inside the training window: 400 `date_in_future` or
    `date_too_old` on `date` (`training_date_refusal`, SIDE_BUGS S-112).
    That is about a NEW set: an edit keeps the date the set was stored with, so a set
    from any earlier day can be corrected (SIDE_BUGS S-068).

    `reps` is 1-1000 on every route (S-115). `rest_time` is a whole number of seconds,
    0 or more: a negative answers 400 `validation_error` with
    `field_errors.rest_time[0].code` `rest_time_out_of_range` on a POST and a PATCH, and
    the same code on a `set-logs/sync/` row (B-122). A TRAINER may not create a set on a
    client's progress row or session: 403 `set_not_yours` (S-114, see
    `ExerciseSetLogViewSet.perform_create`).

    Read-only on the answer that creates a set: `personal_record` (true / false) and,
    when it is true, `previous_best` `{weight, reps, date}` — the best it beat
    (BACKEND_BUGS B-112). Both are null on a replay, a PATCH and a list read.
    """
    user_exercise_progress = serializers.PrimaryKeyRelatedField(queryset=UserExerciseProgress.objects.all(), required=True)
    volume = serializers.SerializerMethodField()
    one_rep_max_estimate = serializers.SerializerMethodField()
    #: True when this set beat everything logged before it for that exercise, so the app
    #: can celebrate at once; the push follows (the owner, 2026-09-20).
    personal_record = serializers.SerializerMethodField()
    #: `{weight, reps, date}` of the best this set beat, beside `personal_record: true`
    #: on the answer that created it (BACKEND_BUGS B-112); null otherwise.
    previous_best = serializers.SerializerMethodField()
    #: "I did what the coach asked" — write-only, never stored.
    as_prescribed = serializers.BooleanField(write_only=True, required=False, default=False)
    #: Which routine the prescription for `as_prescribed` comes from — a plain id,
    #: resolved in `_prescription` under the same rule that decides whether it may be
    #: read. A related field would have answered "no such routine" for an id that does
    #: not exist and `no_prescribed_reps` for one that does but is not the caller's,
    #: which tells a caller by enumeration which routine ids are real. Both now answer
    #: `no_prescribed_reps`: no prescription this caller may read, for whatever reason.
    routine = serializers.IntegerField(write_only=True, required=False, min_value=1)
    day = serializers.IntegerField(write_only=True, required=False, min_value=1)
    #: Seconds of rest before this set, 0 or more (BACKEND_BUGS B-122). The column is a
    #: signed integer, so `-5` was stored by both routes and reached the coach's review
    #: and every rest figure. A negative is refused ON THE FIELD, with its own code,
    #: because `set-logs/sync/` reads a row's rest through this very field
    #: (`routine/views.py _sync_rest_time`, B-109): one field, so both routes refuse the
    #: same values. No upper bound beyond the column's own: a rest is measured, not
    #: prescribed, and nothing in the product caps a measured one.
    rest_time = serializers.IntegerField(
        required=False, allow_null=True, max_value=2147483647,
        validators=[RestTimeNotNegative(
            0, message=_("rest_time must be a whole number of seconds, 0 or more."))])

    def validate_user_exercise_progress(self, value):
        """
        A set log may only be attached to a progress row the caller owns (or, for a
        trainer/admin, one belonging to an approved client).

        Without this, any authenticated user could POST a set log referencing ANOTHER
        user's progress id and inject fabricated training data into their history —
        proven to move a victim's reported week_volume from 0 to 25000.
        """
        request = self.context.get('request')
        if request is None or not request.user.is_authenticated:
            raise serializers.ValidationError(_("Authentication required."))
        from routine.permissions import can_access_user_data
        if not can_access_user_data(request.user, value.user_id):
            raise serializers.ValidationError(
                _("You cannot log sets against another user's progress.")
            )
        return value

    #: The same bounds `bulk-create` enforces (routine/views.py). They were applied on
    #: that route only, so the plain `POST /set-logs/` route — same table, same
    #: aggregates — accepted `weight=-500`, which reported a volume of -5,000 and a
    #: one-rep max of -666.7, and turned a client's weekly volume negative.
    MAX_WEIGHT_KG = 1000
    MAX_REPS = 1000

    def validate_weight(self, value):
        if value is None:
            return value
        if value < 0 or value > self.MAX_WEIGHT_KG:
            raise serializers.ValidationError(
                _("Weight must be between 0 and %(max)d kg.") % {"max": self.MAX_WEIGHT_KG})
        return value

    def validate_reps(self, value):
        """1 to MAX_REPS, the bound `bulk-create` and `sync` apply (SIDE_BUGS S-115).
        Zero was accepted here, and a set of no repetitions counted toward the
        exercise's completed sets and the day's logged sets, so a day could complete
        itself on sets where nothing was lifted — against the owner's rule that reps
        are required (2026-09-20)."""
        if value is None:
            return value
        if value < 1 or value > self.MAX_REPS:
            raise serializers.ValidationError(
                _("reps must be between 1 and %(max)d.") % {"max": self.MAX_REPS})
        return value

    def validate_workout_session(self, value):
        """The session has to be one the caller may write to.

        `user_exercise_progress` was guarded and this field was not — a bare
        `PrimaryKeyRelatedField` over every session in the table. Session ids are
        sequential, and every reader (`WorkoutSessionDetailSerializer`, the trainer's
        client-progress screen, the roster's recent activity) aggregates a session's
        `set_logs` without re-checking whose progress each log belongs to. So a stranger
        could post onto another client's session and the trainer watched that client's
        volume go from 500 kg to 100,500 kg, with an exercise they never performed
        appearing in their activity list.
        """
        if value is None:
            return value
        request = self.context.get('request')
        if request is None or not request.user.is_authenticated:
            raise serializers.ValidationError(_("Authentication required."))
        from routine.permissions import can_access_user_data
        if not can_access_user_data(request.user, value.user_id):
            raise serializers.ValidationError(
                _("You cannot log sets against another user's session.")
            )
        return value

    def validate(self, attrs):
        """A set log belongs to ONE person: the session and the progress must agree."""
        attrs = super().validate(attrs)
        # Write-only helpers: they find the coach's prescription and are never stored.
        as_prescribed = attrs.pop('as_prescribed', False)
        routine = attrs.pop('routine', None)
        day = attrs.pop('day', None)
        if self.instance is not None:
            attrs.pop('client_uuid', None)      # the retry key is fixed once written
            # What the set IS — whose exercise, which session, which set of which day —
            # is fixed with it. Only what was LIFTED may be corrected (weight, reps,
            # notes, rpe). Otherwise an edit is a move: the same row re-pointed at
            # another progress row or another date, which is how a client would carry
            # today's work onto a day that was short and repair a broken streak.
            attrs = fixed_once_written(
                attrs, self.instance,
                ('user_exercise_progress', 'workout_session', 'set_number', 'date'),
                'set_immutable',
                _("A logged set cannot be moved to another exercise, session, set "
                  "number or date. Delete it and log it where it belongs."))
        progress = attrs.get('user_exercise_progress') or getattr(
            self.instance, 'user_exercise_progress', None)
        session = attrs.get('workout_session') or getattr(
            self.instance, 'workout_session', None)
        if progress is not None and session is not None and (
                progress.user_id != session.user_id):
            raise serializers.ValidationError(
                _("That session and that progress record belong to different users.")
            )

        # ONE DAY PER SET. `UserExerciseProgress` is keyed `(user, exercise, date)`, so
        # the progress row IS the training day and this column only repeats it. They
        # were independent, and two readers believed different ones: `outcomes.day_work`
        # counts a set by THIS date, `refresh_routine_progress` measures the day by the
        # progress row's — so a mismatched date put one workout on two days and let a
        # short day be completed by aiming a set at it.
        #
        # An OMITTED date means TODAY, and must then match the row like any other: it is
        # not filled in from whatever day the row happens to carry. Inheriting the row's
        # day silently backdates the set, and the coaching refusal is judged on that day
        # — so a frozen client could log today's training by aiming an undated set at a
        # progress row from a month they had paid for
        # (`tests/test_coaching_freeze.py::test_an_undated_set_is_judged_on_the_day_it_is_stored`,
        # the hole that test exists to keep shut). Catching up yesterday's workout is
        # still possible: send the date, against yesterday's row.
        #
        # ON CREATE ONLY (SIDE_BUGS S-068). On an update `date` is fixed once written
        # (`fixed_once_written` above refuses a changed one and drops a re-sent one), so
        # an omitted date there means "leave it", not "today": filling in today made
        # every correction of a set from any earlier day fail with `set_date_mismatch`.
        # The model's `clean()` still holds the stored row to its progress row's day.
        owner_date = getattr(progress, 'date', None)
        if self.instance is None and progress is not None and owner_date is not None:
            if attrs.get('date') is None:
                attrs['date'] = timezone.localdate()
            elif attrs['date'] != owner_date:
                raise serializers.ValidationError({'date': [serializers.ErrorDetail(
                    _("A set is logged on the day of the exercise it belongs to "
                      "(%(day)s). Log it against that day's progress record.")
                    % {'day': owner_date}, code='set_date_mismatch')]})

        # Inside the training window, like every route that writes training (SIDE_BUGS
        # S-112): a progress row dated ahead must not become a way to log tomorrow's
        # sets today, nor a year-old row a way round the backlog bound.
        if self.instance is None and self.context.get('request') is not None:
            refusal = training_date_refusal(attrs.get('date'))
            if refusal is not None:
                raise serializers.ValidationError({'date': [serializers.ErrorDetail(
                    refusal[1], code=refusal[0])]})

        if self.instance is None and attrs.get('reps') is None:
            if not as_prescribed:
                # Stored as ten before, silently. Every "reps missed" figure reads these
                # rows, so a guess is worse than a refusal (the owner, 2026-09-20).
                raise serializers.ValidationError({'reps': [serializers.ErrorDetail(
                    _("How many reps? Send `as_prescribed: true` to use your coach's "
                      "number."), code='reps_required')]})
            prescription = self._prescription(progress, session, routine, day)
            if prescription is None or prescription.reps is None:
                raise serializers.ValidationError({'reps': [serializers.ErrorDetail(
                    _("Your coach did not set reps for this exercise; enter what you did."),
                    code='no_prescribed_reps')]})
            attrs['reps'] = prescription.reps
            if attrs.get('weight') is None and prescription.weight is not None:
                attrs['weight'] = prescription.weight
        return attrs

    def _prescription(self, progress, session, routine, day):
        """The coach's row for this exercise on this routine day, or None.

        The session names the routine and the day; a set logged without one has to say
        which day it belongs to, because `UserExerciseProgress` records neither. A
        `routine` in the body is only honoured when the caller may train it — the field
        is a plain id, and without this check "as prescribed" read any trainer's
        prescribed reps and weight by enumeration. An id that does not exist and one
        that is not the caller's are the same answer here: no prescription.
        """
        if progress is None:
            return None
        if routine is not None:
            routine = Routine.objects.filter(pk=routine).first()
            if routine is None or not self._may_read_plan(routine):
                return None
        routine = routine or getattr(session, 'routine', None)
        day = day or getattr(session, 'day', None)
        if routine is None or day is None:
            return None
        return (RoutineExercise.objects
                .filter(routine=routine, day=day, exercise_id=progress.exercise_id)
                .order_by('order', 'id').first())

    def _may_read_plan(self, routine):
        """Assigned to the caller, written by them, or an admin — the same rule
        `set-logs/bulk-create/` applies to the routine it is given."""
        request = self.context.get('request')
        user = getattr(request, 'user', None)
        if user is None or not user.is_authenticated:
            return False
        return bool(getattr(user, 'is_admin', False)
                    or routine.created_by_id == user.id
                    or routine.assigned_to.filter(pk=user.pk).exists())

    class Meta:
        model = ExerciseSetLog
        fields = [
            'id', 'user_exercise_progress', 'workout_session', 'set_number', 
            'weight', 'reps', 'volume', 'one_rep_max_estimate', 'personal_record',
            'previous_best',
            'date', 'notes', 'rest_time', 'rpe',
            # The app's id for the set; a retried POST returns the first row (B-003).
            'client_uuid',
            # Write-only: "as prescribed", and where to find the prescription.
            'as_prescribed', 'routine', 'day',
        ]
        # The unique index decides, not a validator (see WorkoutSessionSerializer).
        extra_kwargs = {'client_uuid': {'validators': []}}
        # TODO: Add more fields as needed for analytics/reporting

    def get_personal_record(self, obj):
        """True or false for the set just written, null when reading a list: asking per
        row would be a query per set."""
        return getattr(obj, '_is_personal_record', None)

    def get_previous_best(self, obj):
        """`{weight, reps, date}` of the best the set just written beat, when it is a
        personal record (`strength.record_beaten`, the same judgement as
        `personal_record`); null otherwise, and on every read that judged nothing."""
        return getattr(obj, '_previous_best', None)

    def get_volume(self, obj):
        """Calculate volume for this set (weight × reps)"""
        weight = obj.weight or 0
        reps = obj.reps or 0
        return weight * reps

    def get_one_rep_max_estimate(self, obj):
        """Brzycki's estimate for this set, in kilograms.

        `routine.strength.one_rep_max` is the one implementation: the same number the
        trend, the projection and the record all read. Copied out here, the two drifted
        the moment either was touched.
        """
        from .strength import one_rep_max

        return one_rep_max(obj.weight, obj.reps)




class UserDailySummarySerializer(serializers.ModelSerializer):
    """
    Detailed summary of a user's progress on a specific exercise for a given day.
    Includes full exercise details and all set logs.
    """
    exercise = ExerciseSerializer(read_only=True)
    set_logs = ExerciseSetLogSerializer(many=True, read_only=True)
    total_volume = serializers.SerializerMethodField()
    avg_intensity = serializers.SerializerMethodField()
    status = serializers.SerializerMethodField()

    class Meta:
        model = UserExerciseProgress
        fields = [
            'id', 'exercise', 'date', 'status', 
            'completed_sets', 'target_sets', 'skipped',
            'total_volume', 'avg_intensity',
            'set_logs', 'notes'
        ]

    def get_total_volume(self, obj):
        return sum(log.weight * log.reps for log in obj.set_logs.all() if log.weight and log.reps)

    def get_avg_intensity(self, obj):
        rpes = [log.rpe for log in obj.set_logs.all() if log.rpe]
        if not rpes:
            return None
        return round(sum(rpes) / len(rpes), 1)

    def get_status(self, obj):
        if obj.skipped:
            return 'skipped'
        elif obj.completed_sets >= obj.target_sets and obj.target_sets > 0:
            return 'completed'
        elif obj.completed_sets > 0:
            return 'in_progress'
        return 'not_started'


class TrainerRoutineSerializer(RoutineAssigneesMixin, serializers.ModelSerializer):
    """
    Enhanced Serializer for trainer-specific routine operations.
    
    Features:
    - Trainer-specific fields and validation
    - Client count and assignment information
    - Enhanced error handling
    
    TODO: Add routine analytics and performance metrics
    TODO: Implement routine difficulty progression tracking
    TODO: Add routine completion statistics

    What a trainer's `GET routines/` and `GET routines/{id}/` answer. `created_by_id`
    (B-111) and `day_names` (B-104) mean exactly what they mean on `RoutineSerializer`,
    which is what every write answers; both are read-only here.
    """
    routine_exercises = RoutineExerciseSerializer(many=True, read_only=True)
    completion_rate = serializers.SerializerMethodField()
    created_by_id = serializers.IntegerField(read_only=True)
    day_names = serializers.JSONField(read_only=True)

    class Meta:
        model = Routine
        fields = [
            'id', 'name', 'description', 'is_active', 'days', 'start_date', 'end_date',
            'created_at', 'updated_at', 'routine_exercises', 'assigned_to', 'assigned_usernames',
            'client_count', 'completion_rate', 'source_template', 'created_by_id', 'day_names',
            'assigned_ids',
        ]
        read_only_fields = ['created_at', 'updated_at', 'source_template']

    def get_completion_rate(self, obj):
        """
        Calculate completion rate for this routine.
        Returns the percentage of days marked as 'Completed' across all assigned users.
        """
        # Counted in the query when the viewset annotated it. Counting in Python
        # "to avoid a DB hit" meant fetching every progress row of every client to
        # produce one percentage.
        total = getattr(obj, 'progress_total', None)
        if total is not None:
            done = getattr(obj, 'progress_done', 0) or 0
            return round((done / total) * 100, 1) if total else 0.0

        from .models import RoutineProgress

        progress_entries = obj.progress.all()
        total_entries = len(progress_entries)
        completed_entries = sum(1 for p in progress_entries if p.status == 'completed')
        
        if total_entries == 0:
            return 0.0
            
        return round((completed_entries / total_entries) * 100, 1)

    def validate(self, attrs):
        """
        Validate that only trainers can use this serializer.
        
        TODO: Add more comprehensive validation rules
        TODO: Implement routine template validation
        """
        request = self.context.get('request')
        if request and not request.user.is_trainer:
            logger.warning(f"Non-trainer user {request.user.id} attempted to use TrainerRoutineSerializer")
            raise serializers.ValidationError(_("This serializer is only for trainers"))
        return attrs


class ClientProfileViewSerializer(serializers.ModelSerializer):
    """
    Enhanced Serializer for viewing client profiles by trainers.
    
    Features:
    - Personal data: weight, height, age, gender, activity_level
    - Calculated metrics: BMI, BMR, TDEE
    - Goals and preferences
    - Training history
    
    TODO: Add progress tracking and analytics
    TODO: Implement goal achievement tracking
    TODO: Add performance metrics and trends
    """
    bmi = serializers.SerializerMethodField()
    bmr = serializers.SerializerMethodField()
    tdee = serializers.SerializerMethodField()
    full_name = serializers.SerializerMethodField()
    training_history = serializers.SerializerMethodField()
    # The onboarding answers the client gave for their trainer (design v2). The date of
    # birth is NOT here: a trainer gets `age`, never the date.
    injury_areas = serializers.ListField(child=serializers.CharField(), read_only=True)
    
    class Meta:
        model = get_user_model()
        fields = [
            'id', 'username', 'email', 'full_name', 'profile_picture',
            'height', 'weight', 'age', 'gender', 'specific_injury',
            'activity_level', 'client_goals', 'client_preferences',
            'training_goal', 'training_place', 'training_days_per_week', 'injury_areas',
            'bmi', 'bmr', 'tdee', 'training_history',
            'date_joined', 'last_login'
        ]
        read_only_fields = ['id', 'username', 'email', 'date_joined', 'last_login']

    def get_bmi(self, obj):
        """Calculate BMI for the client."""
        value = obj.calculate_bmi() if obj.height and obj.weight else None
        return value if value is not None else 0.0

    def get_bmr(self, obj):
        """Calculate BMR for the client."""
        value = obj.calculate_bmr() if obj.height and obj.weight and obj.age else None
        return value if value is not None else 0.0

    def get_tdee(self, obj):
        """Calculate TDEE for the client, or 0.0 when their profile cannot support it.

        `calculate_daily_calories` raises on an incomplete profile and on an activity
        level outside its table, and this method is called once per row of a list. One
        client stored as 'moderate' instead of 'Moderate' therefore returned 500 for
        the whole of `/api/auth/trainer/client-profile/` — 260 clients unreachable
        because of one. A profile this serializer cannot compute is a zero for that
        row, logged, not an error for everyone else's.
        """
        if not (obj.height and obj.weight and obj.age):
            return 0.0
        try:
            value = obj.calculate_daily_calories('Maintain')
        except ValueError:
            logger.warning("TDEE unavailable for user %s", obj.pk, exc_info=True)
            return 0.0
        return value if value is not None else 0.0

    def get_full_name(self, obj):
        """Get client's full name."""
        return obj.full_name

    def get_training_history(self, obj):
        """
        Get training history for the client.
        
        TODO: Implement comprehensive training history
        TODO: Add progress tracking and analytics
        TODO: Include routine completion statistics
        """
        # Placeholder for training history
        return {
            'total_routines': 0,
            'completed_routines': 0,
            'current_routines': 0,
            'last_activity': None
        }

    def validate(self, attrs):
        """
        Validate that the object is a client.
        
        TODO: Add more comprehensive validation
        TODO: Implement data privacy checks
        """
        if self.instance and not self.instance.is_client:
            raise serializers.ValidationError(_("This serializer is only for client profiles"))
        return attrs





class WorkoutSessionSerializer(serializers.ModelSerializer):
    duration = serializers.SerializerMethodField()

    #: The longest a single session may be recorded as. An ALIAS of the one rule in
    #: `routine/sessions.py`: the number that bounds a recorded workout and the number
    #: past which an open one is called forgotten are the same number, and they were
    #: written out three times in three files (SIDE_BUGS S-022). The attribute stays
    #: because the validators and `routine/views.py` read it off the class.
    MAX_SESSION_SECONDS = SESSION_MAX_SECONDS

    #: How far back an offline workout may be dated, and the clock skew tolerated ahead.
    MAX_BACKDATE_DAYS = 30
    FUTURE_SKEW_SECONDS = 5 * 60

    #: A session in one of these is over, and its `status` no longer changes (S-122).
    FINAL_STATUSES = ('completed', 'abandoned')

    #: How hard it felt, as the person who trained judges it (BACKEND_DELTA item 11).
    EFFORT_MIN, EFFORT_MAX = 1, 5
    #: Declared, so the BOUND has one code. Built from the model field it carried the
    #: validators' `max_value` / `min_value`, and the app branches on `code`: one rule
    #: must not answer with two of them depending on which side it was broken.
    effort_rating = serializers.IntegerField(required=False, allow_null=True)

    class Meta:
        model = WorkoutSession
        # `start_time` and `client_uuid` are written on create only (see `validate`).
        fields = ['id', 'user', 'routine', 'day', 'start_time', 'end_time', 'status',
                  'duration', 'client_uuid', 'effort_rating']
        read_only_fields = ['id', 'user', 'duration']
        # No uniqueness VALIDATOR on the retry key: a retry racing its first attempt
        # would be told 400 "already exists" for a write that landed. The unique index
        # decides, and `_idempotent_create` answers its IntegrityError with the row.
        extra_kwargs = {'start_time': {'required': False},
                        'client_uuid': {'validators': []}}

    def validate_effort_rating(self, value):
        """1 to 5, and only from the person who trained (BACKEND_DELTA item 11).

        Codes: `effort_out_of_range`, `effort_not_yours`. A trainer opening or
        correcting a client's session may not answer for them — the whole point of the
        number is that it is the client's own sense of how hard it was, and every
        progression suggestion built on it would otherwise be the coach arguing with
        their own guess. An admin is refused for the same reason.
        """
        if value is None:
            return value
        if not self.EFFORT_MIN <= value <= self.EFFORT_MAX:
            raise serializers.ValidationError(
                _("How hard it felt is a number from %(low)d to %(high)d.")
                % {"low": self.EFFORT_MIN, "high": self.EFFORT_MAX},
                code="effort_out_of_range")
        request = self.context.get('request')
        actor = getattr(request, 'user', None)
        # On create the owner is decided in the view (`perform_create`): the caller,
        # unless a trainer named a client in the body. On update it is the row's.
        owner_id = self.instance.user_id if self.instance is not None else None
        if owner_id is None and request is not None and hasattr(request.data, 'get'):
            named = request.data.get('user')
            try:
                owner_id = None if named in (None, '') else int(named)
            except (TypeError, ValueError):
                owner_id = None     # a malformed `user` is the view's 400, not ours
        if actor is None or not actor.is_authenticated:
            raise serializers.ValidationError(_("Authentication required."),
                                              code="effort_not_yours")
        if owner_id is not None and owner_id != actor.id:
            raise serializers.ValidationError(
                _("Only the person who trained can say how hard it felt."),
                code="effort_not_yours")
        return value

    def validate_start_time(self, value):
        """An offline workout says when it began. Codes: `start_time_in_future`,
        `start_time_too_old`."""
        from django.utils import timezone

        if self.instance is not None:
            return value            # ignored on update (see `validate`)
        now = timezone.now()
        if (value - now).total_seconds() > self.FUTURE_SKEW_SECONDS:
            raise serializers.ValidationError(_("A workout cannot start in the future."),
                                              code="start_time_in_future")
        if (now - value).days > self.MAX_BACKDATE_DAYS:
            raise serializers.ValidationError(
                _("A workout can be logged up to %(days)d days late.")
                % {"days": self.MAX_BACKDATE_DAYS}, code="start_time_too_old")
        return value

    def validate(self, attrs):
        from django.utils import timezone

        attrs = super().validate(attrs)
        if self.instance is not None:
            # Fixed once written: the start is the record, and the id is the retry key.
            attrs.pop('start_time', None)
            attrs.pop('client_uuid', None)
            # `status` only moves forward (SIDE_BUGS S-122): `active` -> `completed` or
            # `abandoned`, and both of those are final. It was freely writable, so a
            # completed session toggled to `active` and back ran everything finishing
            # it does a second time (the client and the coach were told twice, the
            # milestones queued twice), and an abandoned one "finished" days later was
            # stamped a twelve-hour workout the nightly sweep had deliberately left
            # without an end. Re-sending the status the row holds changes nothing.
            wanted = attrs.get('status')
            if wanted is not None and wanted != self.instance.status \
                    and self.instance.status in self.FINAL_STATUSES:
                raise serializers.ValidationError({'status': [serializers.ErrorDetail(
                    _("A finished or abandoned workout cannot be reopened or finished "
                      "again."), code='session_status_final')]})
            # Moving a session to another day or routine is checked like opening one:
            # PATCH {"day": 9} stored a day whose sets move no progress. A status-only
            # PATCH is not checked, so a session begun before a routine was retired can
            # still be finished.
            routine = attrs.get('routine', self.instance.routine)
            day = attrs.get('day', self.instance.day)
            if (routine is not None and getattr(routine, 'pk', None) != self.instance.routine_id) \
                    or ('day' in attrs and day != self.instance.day):
                self._check_routine_day(routine, day)
            return attrs
        # A session is for a day the routine actually has (BACKEND_BUGS B-023): any day
        # was accepted, and so was a retired routine, whose sets never move its progress.
        self._check_routine_day(attrs.get('routine'), attrs.get('day'))
        end = attrs.get('end_time')
        if end is not None:
            start = attrs.get('start_time') or timezone.now()
            if end < start:
                raise serializers.ValidationError(
                    {'end_time': [_("A workout cannot end before it started.")]})
            if (end - start).total_seconds() > self.MAX_SESSION_SECONDS:
                raise serializers.ValidationError({'end_time': [
                    _("A workout cannot last longer than %(hours)d hours.")
                    % {"hours": self.MAX_SESSION_SECONDS // 3600}]})
        return attrs

    @staticmethod
    def _check_routine_day(routine, day):
        """`routine_inactive` on `routine`; `day_out_of_range` / `day_has_no_exercises`
        on `day` (BACKEND_BUGS B-023)."""
        if routine is None:
            return
        if not routine.is_active:
            raise serializers.ValidationError({'routine': [serializers.ErrorDetail(
                _("This routine has been retired."), code='routine_inactive')]})
        if day is None:
            return
        if not 1 <= day <= (routine.days or 0):
            raise serializers.ValidationError({'day': [serializers.ErrorDetail(
                _("Day %(day)s is not part of this routine.") % {'day': day},
                code='day_out_of_range')]})
        if not routine.routine_exercises.filter(day=day).exists():
            raise serializers.ValidationError({'day': [serializers.ErrorDetail(
                _("Day %(day)s has no exercises.") % {'day': day},
                code='day_has_no_exercises')]})

    def validate_end_time(self, value):
        """A workout ends after it starts, and lasts a plausible length of time.

        `start_time` is `auto_now_add` so only one side of the interval was trusted, and
        nothing compared them: an end three days BEFORE the start was accepted and
        reported a duration of -259,199 seconds, and one 400 days after gave 34,560,000.
        The seven-day activity chart sums these, so a single call turned a bar negative
        or into 9,600 hours, and `intensity_score` divides volume by it.
        """
        if value is None:
            return value
        start = getattr(self.instance, 'start_time', None)
        if start is None:
            return value
        if value < start:
            raise serializers.ValidationError(
                _("A workout cannot end before it started."))
        if (value - start).total_seconds() > self.MAX_SESSION_SECONDS:
            raise serializers.ValidationError(
                _("A workout cannot last longer than %(hours)d hours.")
                % {"hours": self.MAX_SESSION_SECONDS // 3600})
        return value

    def validate_routine(self, routine):
        """A session belongs to a routine you were given or wrote.

        `routine` was a writable field over every Routine, and object permissions are
        never consulted on create, so any authenticated user could open a session
        against any trainer's routine — and completing it notified that trainer's
        client.
        """
        request = self.context.get('request')
        user = getattr(request, 'user', None)
        if user is None or not user.is_authenticated:
            raise serializers.ValidationError(_("Authentication required."))
        if user.is_staff or getattr(user, 'is_admin', False):
            return routine
        if routine.created_by_id == user.id or routine.assigned_to.filter(pk=user.pk).exists():
            return routine
        # A field code of its own, the one the routine routes answer for the same reason
        # (the owner, 2026-09-24): it was the generic `invalid`, and English.
        raise serializers.ValidationError(_("That routine is not assigned to you."),
                                          code='routine_not_yours')
    
    def get_duration(self, obj):
        """Seconds: from start to end once the session has an end; the time so far
        while it is RUNNING (`active` and begun inside `SESSION_MAX_SECONDS`, the one
        rule in `routine/sessions.py`); otherwise null.

        Every session without an end answered "now minus start", so an abandoned or a
        forgotten workout reported lasting days, a number that grew for ever — while
        `routine.abandon_forgotten_sessions` promises such a session has no duration
        (SIDE_BUGS S-116)."""
        from django.utils import timezone

        from .sessions import cutoff

        if obj.start_time and obj.end_time:
            return int((obj.end_time - obj.start_time).total_seconds())
        if obj.start_time and obj.status == 'active' and obj.start_time >= cutoff():
            return int((timezone.now() - obj.start_time).total_seconds())
        return None


class RoutineTemplateExerciseSerializer(serializers.ModelSerializer):
    """
    Serializer for RoutineTemplateExercise (exercise in a template).
    Input: exercise (PK), sets, reps, rest_time, day, order
    Output: Nested exercise details for listing
    """
    exercise = ExerciseSerializer(read_only=True)
    #: Your own exercises, or the global ones; never another trainer's private one.
    #: The template path once took any id, so a trainer could put a competitor's private
    #: exercise into a template and push it to a client, exposing its name, description
    #: and media. Then it refused one with an English sentence of its own, which told
    #: the trainer the id was real: now `does_not_exist`, exactly like a missing id
    #: (BACKEND_BUGS B-139, `AuthorableExerciseField`).
    exercise_id = AuthorableExerciseField(source='exercise', write_only=True)

    class Meta:
        model = RoutineTemplateExercise
        fields = ['id', 'exercise', 'exercise_id', 'sets', 'reps', 'rest_time', 'day', 'order']


#: Where `annotate_used_by_count` puts the count on each template row.
USED_BY_ANNOTATION = '_used_by_count'


def _used_by_counts():
    """Distinct clients on the routines generated from the outer template: one number,
    as a subquery, so a page of templates costs one query however long it is."""
    from django.db.models import Count, OuterRef

    through = Routine.assigned_to.through
    return (through.objects
            .filter(routine__source_template=OuterRef('pk'),
                    customuser__user_type='client')
            .order_by()
            .values('routine__source_template')
            .annotate(n=Count('customuser_id', distinct=True))
            .values('n'))


def annotate_used_by_count(queryset):
    """`queryset` of templates with `used_by_count` counted in the same query (B-108)."""
    from django.db.models import IntegerField, Subquery
    from django.db.models.functions import Coalesce

    return queryset.annotate(**{USED_BY_ANNOTATION: Coalesce(
        Subquery(_used_by_counts(), output_field=IntegerField()), 0)})


class RoutineTemplateSerializer(serializers.ModelSerializer):
    """
    Serializer for RoutineTemplate.
    Input: name, description, goal, days, is_public, exercises (list of RoutineTemplateExercise)
    Output: Nested template with exercises

    `used_by_count` (BACKEND_BUGS B-108, read-only): how many distinct clients are on a
    routine generated from this template (`templates/{id}/generate/`, or any routine
    whose `source_template` is it) — TrainerRoutines' «6 عملاء استخدموه». A client
    counts once however many copies they hold; a copy nobody is on counts nobody.
    Only the template's author and an admin read the number; everyone else reading a
    public template gets null — how a trainer's program sells is their business.
    """
    exercises = RoutineTemplateExerciseSerializer(source='routinetemplateexercise_set', many=True)
    created_by = serializers.SerializerMethodField()
    #: BACKEND_BUGS B-100 (additive). `weeks` is writable: how many weeks the program
    #: runs, 1-52, or null while the trainer has not said — `days` is its week, and the
    #: rows cannot say how often that week repeats. `session_count` is read-only:
    #: `days` x `weeks`, null while `weeks` is (never a guess). The product form's line
    #: «4 أسابيع · 3 أيام · 12 جلسة» is `weeks` · `days` · `session_count`.
    session_count = serializers.IntegerField(read_only=True, allow_null=True)

    used_by_count = serializers.SerializerMethodField()

    def get_used_by_count(self, obj):
        request = self.context.get('request')
        user = getattr(request, 'user', None)
        if getattr(user, 'pk', None) is None or not (
                obj.created_by_id == user.pk or getattr(user, 'is_admin', False)):
            return None
        counted = getattr(obj, USED_BY_ANNOTATION, None)
        if counted is not None:
            return counted
        # A template the viewset did not annotate (a create's or a copy's answer).
        from django.db.models import Count

        return (Routine.assigned_to.through.objects
                .filter(routine__source_template=obj, customuser__user_type='client')
                .aggregate(n=Count('customuser_id', distinct=True))['n'])

    def get_created_by(self, obj):
        """Who wrote it, as before (`str(user)`) — null once that account is suspended or
        erased: a public template must not keep naming someone who left every other
        public surface (the owner's decision 8; the review, D9)."""
        from users.visibility import is_public

        author = obj.created_by
        return str(author) if author is not None and is_public(author) else None
    
    class Meta:
        model = RoutineTemplate
        fields = ['id', 'name', 'description', 'goal', 'days', 'weeks', 'session_count',
                  'is_public', 'created_by', 'created_at', 'exercises', 'used_by_count']

    def validate(self, attrs):
        """Every row must be one a client's routine can hold.

        Refused under `exercises` (field_errors.exercises[0].code):
          template_day_out_of_range  a row's day outside 1..days. It was saved, then
                                     silently left out of every routine generated.
          template_duplicate_row     the same exercise twice at the same day and order.
                                     Generating it broke the routine's unique key: 500.
          exercise_retired           an exercise taken out of use; the routine path
                                     already refused it.
        """
        days = attrs.get('days', getattr(self.instance, 'days', None))
        if days is None:
            days = RoutineTemplate._meta.get_field('days').default
        rows = attrs.get('routinetemplateexercise_set')
        if rows is None:
            if self.instance is not None and 'days' in attrs:
                beyond = self.instance.routinetemplateexercise_set.filter(day__gt=days).exists()
                if beyond or days < 1:
                    raise serializers.ValidationError({'exercises': [serializers.ErrorDetail(
                        _("Some exercises are on a day after the template's last day. "
                          "Move them first."), code='template_day_out_of_range')]})
            return attrs
        seen = set()
        for index, row in enumerate(rows, start=1):
            day = row.get('day', 1)
            if not 1 <= day <= days:
                raise serializers.ValidationError({'exercises': [serializers.ErrorDetail(
                    _("Exercise %(n)s is on day %(day)s; the template has %(days)s days.")
                    % {'n': index, 'day': day, 'days': days},
                    code='template_day_out_of_range')]})
            exercise = row.get('exercise')
            key = (getattr(exercise, 'pk', None), day, row.get('order', 1))
            if key in seen:
                raise serializers.ValidationError({'exercises': [serializers.ErrorDetail(
                    _("Exercise %(n)s repeats another at the same day and position.")
                    % {'n': index}, code='template_duplicate_row')]})
            seen.add(key)
            if exercise is not None and not exercise.is_active:
                raise serializers.ValidationError({'exercises': [serializers.ErrorDetail(
                    _("That exercise has been retired and cannot be added."),
                    code='exercise_retired')]})
        return attrs

    @transaction.atomic
    def create(self, validated_data):
        exercises_data = validated_data.pop('routinetemplateexercise_set', [])
        template = RoutineTemplate.objects.create(**validated_data)
        RoutineTemplateExercise.objects.bulk_create([
            RoutineTemplateExercise(template=template, **ex) for ex in exercises_data])
        return template

    @transaction.atomic
    def update(self, instance, validated_data):
        """Atomic. The old exercises are deleted before the new ones are written, so a
        failure on the third of twelve left the trainer's template holding two, with
        no way to recover the rest."""
        exercises_data = validated_data.pop('routinetemplateexercise_set', None)
        for attr, value in validated_data.items():
            setattr(instance, attr, value)
        instance.save()
        if exercises_data is not None:
            instance.routinetemplateexercise_set.all().delete()
            RoutineTemplateExercise.objects.bulk_create([
                RoutineTemplateExercise(template=instance, **ex) for ex in exercises_data])
        return instance


class DetailedExerciseSetSerializer(serializers.ModelSerializer):
    """Detailed serializer for exercise set logs with exercise information."""
    exercise_name = serializers.CharField(source='user_exercise_progress.exercise.name', read_only=True)
    exercise_id = serializers.IntegerField(source='user_exercise_progress.exercise.id', read_only=True)
    target_muscle = serializers.CharField(source='user_exercise_progress.exercise.target_muscle', read_only=True)
    volume = serializers.SerializerMethodField()
    
    class Meta:
        model = ExerciseSetLog
        fields = [
            'id', 'exercise_name', 'exercise_id', 'target_muscle',
            'set_number', 'weight', 'reps', 'volume', 'date',
            'rest_time', 'rpe', 'notes'
        ]
    
    def get_volume(self, obj):
        if obj.weight and obj.reps:
            return obj.weight * obj.reps
        return 0


class WorkoutSessionDetailSerializer(serializers.ModelSerializer):
    """Detailed serializer for workout sessions with exercise information."""
    routine_name = serializers.CharField(source='routine.name', read_only=True)
    routine_id = serializers.IntegerField(source='routine.id', read_only=True)
    duration_minutes = serializers.SerializerMethodField()
    total_volume = serializers.SerializerMethodField()
    intensity_score = serializers.SerializerMethodField()
    muscles_worked = serializers.SerializerMethodField()
    exercises_completed = serializers.SerializerMethodField()
    
    class Meta:
        model = WorkoutSession
        fields = [
            'id', 'routine_name', 'routine_id', 'start_time', 'end_time',
            'status', 'duration_minutes', 'total_volume', 'intensity_score',
            'muscles_worked', 'exercises_completed',
            # How hard it felt, 1-5, or null while unanswered (BACKEND_DELTA item 11).
            # Read-only here: it is written on the session itself, by whoever trained.
            'effort_rating',
        ]
        read_only_fields = fields
    
    def _get_set_logs(self, obj):
        """Cache set logs to avoid duplicate queries across methods."""
        if not hasattr(self, '_set_logs_cache'):
            self._set_logs_cache = {}
        
        if obj.id not in self._set_logs_cache:
            # Use prefetched data if available, otherwise query with optimization
            if hasattr(obj, '_prefetched_objects_cache') and 'set_logs' in obj._prefetched_objects_cache:
                self._set_logs_cache[obj.id] = list(obj.set_logs.all())
            else:
                self._set_logs_cache[obj.id] = list(
                    ExerciseSetLog.objects.filter(workout_session=obj)
                    .select_related('user_exercise_progress__exercise')
                )
        return self._set_logs_cache[obj.id]
    
    def get_duration_minutes(self, obj):
        if obj.start_time and obj.end_time:
            duration = obj.end_time - obj.start_time
            return round(duration.total_seconds() / 60, 1)
        return None
    
    def get_total_volume(self, obj):
        # Use cached set logs
        set_logs = self._get_set_logs(obj)
        total_volume = sum(
            (log.weight * log.reps) for log in set_logs 
            if log.weight and log.reps
        )
        return total_volume
    
    def get_exercises_completed(self, obj):
        """Get exercises completed in this session with optimized queries."""
        set_logs = self._get_set_logs(obj)
        
        # Group logs by exercise
        exercise_map = {}
        for log in set_logs:
            if not log.user_exercise_progress or not log.user_exercise_progress.exercise:
                continue
            exercise = log.user_exercise_progress.exercise
            if exercise.id not in exercise_map:
                exercise_map[exercise.id] = {
                    'exercise_name': exercise.name,
                    'exercise_id': exercise.id,
                    'sets_data': [],
                    'total_volume': 0
                }
            
            volume = (log.weight * log.reps) if log.weight and log.reps else 0
            exercise_map[exercise.id]['sets_data'].append({
                'set_number': log.set_number,
                'weight': log.weight,
                'reps': log.reps,
                'volume': volume,
                'rpe': log.rpe,
                'notes': log.notes
            })
            exercise_map[exercise.id]['total_volume'] += volume
        
        # Build result with sets_completed count
        exercise_details = []
        for data in exercise_map.values():
            data['sets_completed'] = len(data['sets_data'])
            exercise_details.append(data)
        
        return exercise_details

    def get_intensity_score(self, obj):
        """Calculate intensity (Volume / Duration in minutes)."""
        duration = self.get_duration_minutes(obj)
        volume = self.get_total_volume(obj)
        if duration and duration > 0 and volume:
            return round(volume / duration, 1)
        return 0.0

    def get_muscles_worked(self, obj):
        """Return list of muscles targeted in this session."""
        set_logs = self._get_set_logs(obj)
        return list(set(
            log.user_exercise_progress.exercise.target_muscle 
            for log in set_logs
            if log.user_exercise_progress and log.user_exercise_progress.exercise
        ))


# `DetailedClientProgressSerializer` was here and is gone (SIDE_BUGS S-037). It was
# imported by `routine/views.py` and used by nothing: dead code that read like the
# live path, so the next person reading `trainer/client-progress/{id}/` would have
# found `get_client_info` here and believed it was what the endpoint answers. It is
# not, and wiring it in would have been a breaking change for a worse shape -- the
# endpoint answers `{client, client_info, weekly_stats, strength, recent_activity}`
# built from ONE prefetched query, while this serializer keyed off a `RoutineProgress`
# row and re-queried every session and set log per client.


class RecentActivitySerializer(serializers.ModelSerializer):
    """One client's recent training, for the trainer's roster screen.

    Every field used to be computed with its own queries against `obj`: the last
    session, its set logs, a distinct count, this week's sessions iterated with their
    set logs, two RoutineProgress counts, and the week's exercise logs. Serialising a
    roster of fifty cost ~259 queries and a hundred ~509, growing strictly with the
    number of clients — on a deployment whose connection budget is 15 per machine
    against a 100-connection Postgres, which is why `fly.toml` caps concurrency there.

    The queries are the same queries; what changed is that they are asked ONCE for the
    whole roster and handed in through `context["activity"]`, keyed by user id. Build it
    with `RecentActivitySerializer.collect(users)`. Without that context the serializer
    still works for a single user — it collects for that one user — so nothing that
    serialises one client had to change.

    Pass `context["request"]` too: `client_info.profile_picture` is then absolute, the
    way every other endpoint answers that field.
    """
    client_info = serializers.SerializerMethodField()
    last_session = serializers.SerializerMethodField()
    this_week = serializers.SerializerMethodField()
    recent_exercises = serializers.SerializerMethodField()

    class Meta:
        model = CustomUser
        fields = [
            'client_info', 'last_session', 'this_week', 'recent_exercises'
        ]

    # ---- batch collection ---------------------------------------------------

    @staticmethod
    def collect(users):
        """Everything the fields below need, for every user, in a fixed query count."""
        from django.db.models import Count, Max, Q
        from django.utils import timezone
        from datetime import timedelta

        ids = [u.pk for u in users]
        collected = {
            uid: {"last_session": None, "last_volume": 0, "last_exercises": 0,
                  "week_volume": 0, "week_sessions": 0,
                  "week_total_days": 0, "week_completed_days": 0, "exercises": {},
                  "week_plan": {}}
            for uid in ids
        }
        if not ids:
            return collected

        # SIX, not seven. Combined with the `__gte` below this window is inclusive on both
        # ends, so `days=7` counted EIGHT calendar dates: the trainer's roster and the
        # detailed progress screen both reported 8 sessions for a week in which the
        # client trained 7 times, while `AnalyticsViewSet.summary` — which uses
        # `days_in_period - 1` — reported 7 for the same data.
        week_ago = timezone.localdate() - timedelta(days=6)

        # The most recent completed session per user: one query for the ids, one for the
        # rows, rather than a `.first()` per user.
        latest_ids = [
            row["latest"] for row in (
                WorkoutSession.objects
                .filter(user_id__in=ids, status='completed')
                .values('user_id')
                .annotate(latest=Max('id'))
            )
        ]
        for session in (WorkoutSession.objects
                        .filter(id__in=latest_ids)
                        .select_related('routine')):
            collected[session.user_id]["last_session"] = session

        # Volume and distinct exercise count of those sessions, aggregated in the query.
        for row in (ExerciseSetLog.objects
                    .filter(workout_session_id__in=latest_ids)
                    .values('workout_session__user_id')
                    .annotate(volume=Sum(F('weight') * F('reps')),
                              exercises=Count('user_exercise_progress__exercise',
                                              distinct=True))):
            entry = collected[row['workout_session__user_id']]
            entry["last_volume"] = row['volume'] or 0
            entry["last_exercises"] = row['exercises']

        # This week: sessions per user, and the volume of their set logs. Counted
        # distinctly because the join to set_logs multiplies the session rows.
        for row in (WorkoutSession.objects
                    .filter(user_id__in=ids, status='completed', end_time__date__gte=week_ago)
                    .values('user_id')
                    .annotate(sessions=Count('id', distinct=True),
                              volume=Sum(F('set_logs__weight') * F('set_logs__reps')))):
            collected[row['user_id']]["week_sessions"] = row['sessions']
            collected[row['user_id']]["week_volume"] = row['volume'] or 0

        # This week's routine progress, both counts in one pass.
        for row in (RoutineProgress.objects
                    .filter(user_id__in=ids, updated_at__date__gte=week_ago)
                    .values('user_id')
                    .annotate(total=Count('id'),
                              completed=Count('id', filter=Q(status='completed')))):
            collected[row['user_id']]["week_total_days"] = row['total']
            collected[row['user_id']]["week_completed_days"] = row['completed']

        # This week against the plan: sets planned, logged and missed, and the days
        # with nothing missing (the owner, 2026-09-20). One query for every client on
        # the page, like everything else collected here.
        from .outcomes import week_totals
        for user_id, totals in week_totals(ids, week_ago, timezone.localdate()).items():
            collected[user_id]["week_plan"] = totals

        # Which way each client's lifting is going, from their own set logs
        # (routine/strength.py). The roster shows an arrow; the full trend is on the
        # client's own screen.
        from .strength import MAX_REPS_FOR_ESTIMATE, TREND_WEEKS
        from .models import ExerciseSetLog as _SetLog
        from django.db.models import ExpressionWrapper, FloatField, Max as _Max
        trend_window = timezone.localdate() - timedelta(weeks=TREND_WEEKS)
        # The client's own FIRST and LAST training day of each exercise in the window,
        # not two fixed halves: someone who started three weeks ago has no "earlier
        # half", and their progress would have read as nothing at all.
        #
        # The best estimate of each (client, exercise, DAY) is aggregated in Postgres:
        # streaming eight weeks of every roster client's individual sets into Python was
        # tens of thousands of rows for one page. Brzycki is the same formula
        # `strength.one_rep_max` uses, and the same rep ceiling — a long set says more
        # about endurance than about a one-rep max, and it must not swing a roster arrow.
        estimate_expr = ExpressionWrapper(
            F('weight') * 36.0 / (37.0 - F('reps')), output_field=FloatField())
        ends: dict = {}
        for row in (_SetLog.objects
                    .filter(user_exercise_progress__user_id__in=ids, date__gte=trend_window,
                            weight__gt=0, reps__gt=0, reps__lte=MAX_REPS_FOR_ESTIMATE)
                    .annotate(estimate=estimate_expr)
                    .values('user_exercise_progress__user_id',
                            'user_exercise_progress__exercise_id', 'date')
                    .annotate(best=_Max('estimate'))
                    .order_by()):
            user_id = row['user_exercise_progress__user_id']
            exercise_id = row['user_exercise_progress__exercise_id']
            date, estimate = row['date'], round(row['best'], 1)
            entry = ends.setdefault((user_id, exercise_id),
                                    {'first_date': date, 'first': 0.0,
                                     'last_date': date, 'last': 0.0})
            if date < entry['first_date']:
                entry.update(first_date=date, first=estimate)
            elif date == entry['first_date']:
                entry['first'] = max(entry['first'], estimate)
            if date > entry['last_date']:
                entry.update(last_date=date, last=estimate)
            elif date == entry['last_date']:
                entry['last'] = max(entry['last'], estimate)
        for (user_id, _exercise_id), entry in ends.items():
            if entry['first_date'] == entry['last_date'] or not entry['first']:
                continue                    # one training day says nothing about a direction
            counts = collected[user_id].setdefault(
                "trend", {'rising': 0, 'easing': 0, 'steady': 0})
            change = 100 * (entry['last'] - entry['first']) / entry['first']
            counts['rising' if change > 2 else 'easing' if change < -2 else 'steady'] += 1

        # This week's exercise logs, for the per-exercise summary. Ordered the way the
        # per-user version ordered them, so the "top 5" are the same five.
        for log in (ExerciseSetLog.objects
                    .filter(user_exercise_progress__user_id__in=ids, date__gte=week_ago)
                    .select_related('user_exercise_progress__exercise')
                    .order_by('user_exercise_progress__exercise__name', '-date')):
            progress = log.user_exercise_progress
            entry = collected[progress.user_id]["exercises"]
            exercise = progress.exercise
            summary = entry.setdefault(exercise.name, {
                'exercise_name': exercise.name,
                'exercise_id': exercise.id,
                'last_performed': log.date,
                'sets': 0, 'reps': 0, 'weight': 0, 'volume': 0,
            })
            summary['sets'] += 1
            summary['reps'] += log.reps or 0
            summary['weight'] = max(summary['weight'], log.weight or 0)
            summary['volume'] += (log.weight * log.reps) if log.weight and log.reps else 0

        return collected

    def _activity(self, obj):
        collected = self.context.get("activity")
        if collected is None or obj.pk not in collected:
            collected = self.collect([obj])
        return collected[obj.pk]

    # ---- fields -------------------------------------------------------------

    def get_client_info(self, obj):
        return {
            'id': obj.id,
            'name': obj.username,
            'full_name': obj.full_name,
            'profile_picture': _absolute_picture(obj, self.context.get('request'))
        }

    def get_last_session(self, obj):
        """`{date, routine_name, exercises_completed, total_volume, duration,
        duration_seconds}`, or null before the first completed session.

        `duration_seconds` (additive, BACKEND_BUGS B-095) is the session's length in
        exactly the shape `trainer/client-progress/{id}/` `recent_activity[].duration`
        sends: seconds as a decimal string (`"2820.0"`), null when the session has no
        start or end. Read this one.

        `duration` is LEGACY DISPLAY TEXT, kept for builds that read it: the minutes as a
        sentence in the reader's language (`"47.0 minutes"`, `"47.0 دقيقة"`), null for a
        session without both times or of zero length. Never parse it.
        """
        from django.utils.translation import gettext

        entry = self._activity(obj)
        last_session = entry["last_session"]
        if not last_session:
            return None

        duration = seconds = None
        if last_session.start_time and last_session.end_time:
            elapsed = last_session.end_time - last_session.start_time
            duration = round(elapsed.total_seconds() / 60, 1)
            # `str(timedelta.total_seconds())`: what DRF's encoder writes for the
            # `WorkoutSession.duration` timedelta on client-progress, character for
            # character.
            seconds = str(elapsed.total_seconds())

        return {
            # The Damascus day (TIME_ZONE), as routine/outcomes judges idle days: the
            # stored UTC day moved a session after local midnight to yesterday (B-135).
            'date': timezone.localdate(last_session.end_time) if last_session.end_time else None,
            'routine_name': last_session.routine.name if last_session.routine else None,
            'exercises_completed': entry["last_exercises"],
            'total_volume': entry["last_volume"],
            # User-facing text, so through the catalogue (it was English to everyone).
            'duration': (gettext('%(minutes)s minutes') % {'minutes': duration}
                         if duration else None),
            'duration_seconds': seconds,
        }

    def get_this_week(self, obj):
        entry = self._activity(obj)
        total_days = entry["week_total_days"]
        completed_days = entry["week_completed_days"]
        completion_rate = round((completed_days / total_days * 100) if total_days > 0 else 0, 1)
        plan = entry["week_plan"]
        return {
            'sessions': entry["week_sessions"],
            'total_volume': entry["week_volume"],
            'completion_rate': completion_rate,
            # What the coach asked for against what was done. `completion_rate` counts
            # DAYS by status and cannot see a day closed with sets missing; these can.
            'planned_sets': plan.get('planned_sets', 0),
            'logged_sets': plan.get('logged_sets', 0),
            'missed_sets': plan.get('missed_sets', 0),
            'set_completion_percent': plan.get('set_completion_percent'),
            'perfect_days': plan.get('perfect_days', 0),
            # Which way their lifting is going over the last eight weeks: how many
            # exercises are rising, easing or flat (routine/strength.py).
            'trend': entry.get("trend") or {'rising': 0, 'easing': 0, 'steady': 0},
        }

    def get_recent_exercises(self, obj):
        return list(self._activity(obj)["exercises"].values())[:5]
