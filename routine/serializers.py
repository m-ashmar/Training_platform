from rest_framework import serializers
from django.conf import settings
from django.utils.translation import gettext_lazy as _
from django.contrib.auth import get_user_model
from .models import (
    Routine, Exercise, RoutineExercise,ExerciseMedia,
    UserExerciseProgress, RoutineProgress, ExerciseSetLog, WorkoutSession,
    RoutineTemplate, RoutineTemplateExercise
)
import logging
from django.db.models import Sum, F
from users.models import CustomUser

logger = logging.getLogger(__name__)


class ExerciseMediaSerializer(serializers.ModelSerializer):
    """Serializer for Exercise Media (e.g., videos, photos, text)."""

    class Meta:
        model = ExerciseMedia
        fields = ['id', 'media_type', 'content']


class ExerciseSerializer(serializers.ModelSerializer):
    """
    Serializer for Exercises, with muscle group targeting.
    Input: name, description, target_muscle
    Output: Serialized Exercise data for API responses
    """
    media = ExerciseMediaSerializer(many=True, read_only=True)
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
        fields = ['id', 'name', 'description', 'target_muscle', 'target_muscle_display',
                  'difficulty_level', 'difficulty_level_display', 'image', 'thumbnail', 'media']

    def get_image(self, obj):
        request = self.context.get('request')
        if obj.image:
            url = obj.image.url
            if request is not None:
                return request.build_absolute_uri(url)
            return url
        return None

    def get_thumbnail(self, obj):
        """Get first image/video thumbnail from media"""
        # optimization: iterate if prefetched
        all_media = obj.media.all()
        for media in all_media:
             if media.media_type in ['photo', 'video']:
                 return media.content
        return None


class ExerciseCreateWithImageSerializer(serializers.ModelSerializer):
    """
    Serializer for creating exercises with optional image upload.
    Input: name, description, target_muscle, image (optional)
    Output: Serialized Exercise data for API responses
    """
    media = ExerciseMediaSerializer(many=True, read_only=True)
    image = serializers.SerializerMethodField()

    class Meta:
        model = Exercise
        fields = ['id', 'name', 'description', 'target_muscle', 'image', 'media']

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


class RoutineExerciseSerializer(serializers.ModelSerializer):
    """
    Serializer for routine exercises.
    Uses nested ExerciseSerializer for read operations to provide full details.
    Uses PrimaryKeyRelatedField for write operations.
    """
    exercise = ExerciseSerializer(read_only=True)
    exercise_id = serializers.PrimaryKeyRelatedField(
        queryset=Exercise.objects.all(), 
        source='exercise', 
        write_only=True
    )
    routine = serializers.PrimaryKeyRelatedField(queryset=Routine.objects.all())

    class Meta:
        model = RoutineExercise
        fields = ['id', 'routine', 'exercise', 'exercise_id', 'sets', 'reps', 'rest_time', 'day', 'order', 'notes', 'created_at', 'updated_at']
        read_only_fields = ['created_at', 'updated_at']

    def validate(self, attrs):
        user = self.context['request'].user
        routine = attrs.get('routine')
        exercise = attrs.get('exercise')

        if user.is_trainer:
            # 1. Validation: Trainer owns the routine
            # Check if routine exists (for updates) or is being set
            if routine and routine.created_by != user:
                raise serializers.ValidationError({
                    "routine": _("You can only add exercises to routines you created.")
                })
            
            # 2. Validation: Exercise is accessible (Global or Own)
            if exercise:
                is_own = exercise.created_by == user
                is_global = exercise.created_by is None
                if not (is_own or is_global):
                    raise serializers.ValidationError({
                        "exercise": _("You can only assign your own exercises or global exercises.")
                    })
                if not exercise.is_active:
                    raise serializers.ValidationError({
                        "exercise": _("That exercise has been retired and cannot be added.")
                    })

        # Checked here rather than on the model: `days` belongs to the routine, so a
        # trainer shortening a routine would otherwise invalidate rows already written
        # against the old length. Asking at the point the day is chosen keeps the
        # answer true when it is given.
        day = attrs.get('day', getattr(self.instance, 'day', None))
        routine = routine or getattr(self.instance, 'routine', None)
        if day is not None and routine is not None and day > routine.days:
            raise serializers.ValidationError({
                "day": _("Day %(day)s exceeds the routine's %(days)s days.")
                       % {"day": day, "days": routine.days}
            })

        return attrs

    def create(self, validated_data):
        """Override create to sync the M2M exercises field on the routine."""
        routine_exercise = super().create(validated_data)
        
        # Ensure the exercise is added to the routine's ManyToMany field
        routine = routine_exercise.routine
        exercise = routine_exercise.exercise
        routine.exercises.add(exercise)
        
        return routine_exercise


from training_platform.utils.serializers import TranslatedJSONFieldMixin

class RoutineSerializer(TranslatedJSONFieldMixin, serializers.ModelSerializer):
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
    """
    routine_exercises = RoutineExerciseSerializer(many=True, read_only=True)
    assigned_to = serializers.StringRelatedField(many=True, read_only=True)
    assigned_usernames = serializers.SerializerMethodField()
    created_by = serializers.StringRelatedField(read_only=True)
    client_count = serializers.SerializerMethodField()
    estimated_duration_minutes = serializers.SerializerMethodField()
    target_muscles = serializers.SerializerMethodField()

    translated_fields = ['name', 'description']

    class Meta:
        model = Routine
        fields = [
            'id', 'name', 'description', 'difficulty_level', 'is_active', 'days', 'start_date', 'end_date',
            'created_at', 'updated_at', 'routine_exercises', 'assigned_to', 'assigned_usernames',
            'created_by', 'client_count', 'estimated_duration_minutes', 'target_muscles'
        ]
        read_only_fields = ['created_at', 'updated_at', 'created_by']

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

    def get_assigned_usernames(self, obj):
        """Retrieve assigned user names."""
        return [user.username for user in obj.assigned_to.all()]

    def get_client_count(self, obj):
        """Get count of clients assigned to this routine."""
        # Use annotated value if available
        if hasattr(obj, 'client_count'):
            return obj.client_count
        return obj.assigned_to.filter(user_type='client').count()

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


class UserExerciseProgressSerializer(serializers.ModelSerializer):
    """Serializer for tracking user progress on individual exercises."""
    exercise = serializers.PrimaryKeyRelatedField(queryset=Exercise.objects.all())
    user = serializers.PrimaryKeyRelatedField(read_only=True)

    class Meta:
        model = UserExerciseProgress
        fields = ['id', 'user', 'exercise', 'date', 'completed_sets', 'target_sets', 'skipped']


class UserExerciseProgressDetailSerializer(UserExerciseProgressSerializer):
    """
    Detailed serializer for UserExerciseProgress.
    Includes full exercise details instead of just ID.
    Used for read operations (GET).
    """
    exercise = ExerciseSerializer(read_only=True)

    class Meta(UserExerciseProgressSerializer.Meta):
        fields = UserExerciseProgressSerializer.Meta.fields





class RoutineProgressSerializer(serializers.ModelSerializer):
    """
    Enhanced serializer with rich progress data.
    Provides completion percentage, exercise summary, and next action suggestions.
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
    
    class Meta:
        model = RoutineProgress
        fields = [
            'id', 'user', 'routine', 'routine_id', 'routine_name',
            'day', 'date', 'status', 'exercises_completed', 'total_exercises',
            'completion_percentage', 'exercises_summary', 
            'next_suggested_action', 'updated_at'
        ]
    
    def get_completion_percentage(self, obj):
        if obj.total_exercises == 0:
            return 0.0
        return round((obj.exercises_completed / obj.total_exercises) * 100, 1)
    
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
        """Suggest what user should do next."""
        if obj.status == 'completed':
            return "Great job! You've finished this day's workout. Rest up for the next one."
        elif obj.status == 'in_progress':
            remaining = obj.total_exercises - obj.exercises_completed
            return f"Keep pushing! You have {remaining} exercises remaining to complete Day {obj.day}."
        else:
            return f"Ready to start? Begin your Day {obj.day} workout now."


class UserRoutineSerializer(serializers.ModelSerializer):
    """Serializer for User Routines with progress tracking."""
    users = serializers.PrimaryKeyRelatedField(many=True, queryset=get_user_model().objects.all())
    routine_template = RoutineSerializer(read_only=True)
    progress = RoutineProgressSerializer(many=True, read_only=True)

    class Meta:
        model = Routine
        fields = ['id', 'users', 'routine_template', 'start_date', 'end_date', 'progress']


class ExerciseSetLogSerializer(serializers.ModelSerializer):
    """
    Serializer for exercise set logs.
    Input: user_exercise_progress (PK), workout_session (PK), set_number, weight, reps, date
    Output: Serialized ExerciseSetLog data for API responses with calculated volume
    """
    user_exercise_progress = serializers.PrimaryKeyRelatedField(queryset=UserExerciseProgress.objects.all(), required=True)
    volume = serializers.SerializerMethodField()
    one_rep_max_estimate = serializers.SerializerMethodField()

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

    class Meta:
        model = ExerciseSetLog
        fields = [
            'id', 'user_exercise_progress', 'workout_session', 'set_number', 
            'weight', 'reps', 'volume', 'one_rep_max_estimate', 
            'date', 'notes', 'rest_time', 'rpe'
        ]
        # TODO: Add more fields as needed for analytics/reporting

    def get_volume(self, obj):
        """Calculate volume for this set (weight × reps)"""
        weight = obj.weight or 0
        reps = obj.reps or 0
        return weight * reps

    def get_one_rep_max_estimate(self, obj):
        """
        Estimate 1RM using Brzycki Formula: weight * (36 / (37 - reps))
        Only valid for reps <= 10 roughly, but we return for all.
        """
        if not obj.weight or not obj.reps or obj.reps == 0:
            return 0.0
        # If reps are very high, formula breaks down, but useful as metric
        return round(obj.weight * (36 / (37 - obj.reps)), 1)




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


class TrainerRoutineSerializer(serializers.ModelSerializer):
    """
    Enhanced Serializer for trainer-specific routine operations.
    
    Features:
    - Trainer-specific fields and validation
    - Client count and assignment information
    - Enhanced error handling
    
    TODO: Add routine analytics and performance metrics
    TODO: Implement routine difficulty progression tracking
    TODO: Add routine completion statistics
    """
    routine_exercises = RoutineExerciseSerializer(many=True, read_only=True)
    assigned_to = serializers.StringRelatedField(many=True, read_only=True)
    assigned_usernames = serializers.SerializerMethodField()
    client_count = serializers.SerializerMethodField()
    completion_rate = serializers.SerializerMethodField()
    
    class Meta:
        model = Routine
        fields = [
            'id', 'name', 'description', 'is_active', 'days', 'start_date', 'end_date',
            'created_at', 'updated_at', 'routine_exercises', 'assigned_to', 'assigned_usernames',
            'client_count', 'completion_rate'
        ]
        read_only_fields = ['created_at', 'updated_at']

    def get_assigned_usernames(self, obj):
        """Retrieve assigned user names."""
        return [user.username for user in obj.assigned_to.all()]

    def get_client_count(self, obj):
        """Get count of clients assigned to this routine."""
        if hasattr(obj, 'client_count'):
            return obj.client_count
        return obj.assigned_to.filter(user_type='client').count()

    def get_completion_rate(self, obj):
        """
        Calculate completion rate for this routine.
        Returns the percentage of days marked as 'Completed' across all assigned users.
        """
        from .models import RoutineProgress
        
        # Get all progress entries for this routine
        # Use all() to leverage prefetch_related
        progress_entries = obj.progress.all()
        
        # Calculate manually in python to avoid DB hit
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
    
    class Meta:
        model = get_user_model()
        fields = [
            'id', 'username', 'email', 'full_name', 'profile_picture',
            'height', 'weight', 'age', 'gender', 'specific_injury',
            'activity_level', 'client_goals', 'client_preferences',
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

    class Meta:
        model = WorkoutSession
        fields = ['id', 'user', 'routine', 'start_time', 'end_time', 'status', 'duration']
        read_only_fields = ['id', 'user', 'start_time', 'duration']
    
    def get_duration(self, obj):
        if obj.start_time and obj.end_time:
            # Return duration in seconds
            return int((obj.end_time - obj.start_time).total_seconds())
        elif obj.start_time:
            # If session is still active, return duration so far in seconds
            from django.utils import timezone
            return int((timezone.now() - obj.start_time).total_seconds())
        return None


class RoutineTemplateExerciseSerializer(serializers.ModelSerializer):
    """
    Serializer for RoutineTemplateExercise (exercise in a template).
    Input: exercise (PK), sets, reps, rest_time, day, order
    Output: Nested exercise details for listing
    """
    exercise = ExerciseSerializer(read_only=True)
    exercise_id = serializers.PrimaryKeyRelatedField(queryset=Exercise.objects.all(), source='exercise', write_only=True)
    
    class Meta:
        model = RoutineTemplateExercise
        fields = ['id', 'exercise', 'exercise_id', 'sets', 'reps', 'rest_time', 'day', 'order']


class RoutineTemplateSerializer(serializers.ModelSerializer):
    """
    Serializer for RoutineTemplate.
    Input: name, description, goal, days, is_public, exercises (list of RoutineTemplateExercise)
    Output: Nested template with exercises
    """
    exercises = RoutineTemplateExerciseSerializer(source='routinetemplateexercise_set', many=True)
    created_by = serializers.StringRelatedField(read_only=True)
    
    class Meta:
        model = RoutineTemplate
        fields = ['id', 'name', 'description', 'goal', 'days', 'is_public', 'created_by', 'created_at', 'exercises']
    def create(self, validated_data):
        # Pop out nested exercises
        exercises_data = validated_data.pop('routinetemplateexercise_set', [])
        request = self.context.get('request')
        created_by = request.user if request else None
        template = RoutineTemplate.objects.create( **validated_data)
        for ex in exercises_data:
            RoutineTemplateExercise.objects.create(template=template, **ex)
        return template
    def update(self, instance, validated_data):
        # Update template and nested exercises
        exercises_data = validated_data.pop('routinetemplateexercise_set', None)
        for attr, value in validated_data.items():
            setattr(instance, attr, value)
        instance.save()
        if exercises_data is not None:
            instance.routinetemplateexercise_set.all().delete()
            for ex in exercises_data:
                RoutineTemplateExercise.objects.create(template=instance, **ex)
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
            'muscles_worked', 'exercises_completed'
        ]
    
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


class DetailedClientProgressSerializer(serializers.ModelSerializer):
    """Enhanced serializer for detailed client progress with session and exercise information."""
    client_info = serializers.SerializerMethodField()
    recent_sessions = serializers.SerializerMethodField()
    progress_summary = serializers.SerializerMethodField()
    
    class Meta:
        model = RoutineProgress
        fields = [
            'id', 'client_info', 'recent_sessions', 'progress_summary'
        ]
    
    def get_client_info(self, obj):
        client = obj.user
        # Get last workout date
        last_workout = WorkoutSession.objects.filter(
            user=client,
            status='completed'
        ).order_by('-end_time').first()
        
        # Get total workouts
        total_workouts = WorkoutSession.objects.filter(
            user=client,
            status='completed'
        ).count()
        
        # Calculate completion rate (last 30 days)
        from django.utils import timezone
        from datetime import timedelta
        thirty_days_ago = timezone.localdate() - timedelta(days=30)
        
        recent_progress = RoutineProgress.objects.filter(
            user=client,
            updated_at__date__gte=thirty_days_ago
        )
        
        total_days = recent_progress.count()
        completed_days = recent_progress.filter(status='completed').count()
        completion_rate = round((completed_days / total_days * 100) if total_days > 0 else 0, 1)
        
        return {
            'id': client.id,
            'name': client.username,
            'full_name': client.full_name,
            'profile_picture': client.profile_picture.url if client.profile_picture else None,
            'last_workout': last_workout.end_time.date() if last_workout and last_workout.end_time else None,
            'total_workouts': total_workouts,
            'completion_rate': completion_rate
        }
    
    def get_recent_sessions(self, obj):
        client = obj.user
        # Get recent completed sessions (last 10)
        recent_sessions = WorkoutSession.objects.filter(
            user=client,
            status='completed'
        ).order_by('-end_time')[:10]
        
        return WorkoutSessionDetailSerializer(recent_sessions, many=True).data
    
    def get_progress_summary(self, obj):
        client = obj.user
        from django.utils import timezone
        from datetime import timedelta
        
        # This week (last 7 days)
        week_ago = timezone.localdate() - timedelta(days=7)
        week_sessions = WorkoutSession.objects.filter(
            user=client,
            status='completed',
            end_time__date__gte=week_ago
        )
        
        week_volume = sum(
            session.set_logs.aggregate(
                total=Sum(F('weight') * F('reps'))
            )['total'] or 0
            for session in week_sessions
        )
        
        # This month (last 30 days)
        month_ago = timezone.localdate() - timedelta(days=30)
        month_sessions = WorkoutSession.objects.filter(
            user=client,
            status='completed',
            end_time__date__gte=month_ago
        )
        
        month_volume = sum(
            session.set_logs.aggregate(
                total=Sum(F('weight') * F('reps'))
            )['total'] or 0
            for session in month_sessions
        )
        
        return {
            'this_week': {
                'sessions': week_sessions.count(),
                'total_volume': week_volume,
                'exercises_completed': ExerciseSetLog.objects.filter(
                    workout_session__in=week_sessions
                ).values('user_exercise_progress__exercise').distinct().count()
            },
            'this_month': {
                'sessions': month_sessions.count(),
                'total_volume': month_volume,
                'exercises_completed': ExerciseSetLog.objects.filter(
                    workout_session__in=month_sessions
                ).values('user_exercise_progress__exercise').distinct().count()
            }
        }


class RecentActivitySerializer(serializers.ModelSerializer):
    """Serializer for recent activity summary."""
    client_info = serializers.SerializerMethodField()
    last_session = serializers.SerializerMethodField()
    this_week = serializers.SerializerMethodField()
    recent_exercises = serializers.SerializerMethodField()
    
    class Meta:
        model = CustomUser
        fields = [
            'client_info', 'last_session', 'this_week', 'recent_exercises'
        ]
    
    def get_client_info(self, obj):
        return {
            'id': obj.id,
            'name': obj.username,
            'full_name': obj.full_name,
            'profile_picture': obj.profile_picture.url if obj.profile_picture else None
        }
    
    def get_last_session(self, obj):
        last_session = WorkoutSession.objects.filter(
            user=obj,
            status='completed'
        ).order_by('-end_time').first()
        
        if not last_session:
            return None
        
        duration = None
        if last_session.start_time and last_session.end_time:
            duration = round((last_session.end_time - last_session.start_time).total_seconds() / 60, 1)
        
        total_volume = sum(
            (log.weight * log.reps) for log in last_session.set_logs.all()
            if log.weight and log.reps
        )
        
        exercises_count = last_session.set_logs.values(
            'user_exercise_progress__exercise'
        ).distinct().count()
        
        return {
            'date': last_session.end_time.date() if last_session.end_time else None,
            'routine_name': last_session.routine.name,
            'exercises_completed': exercises_count,
            'total_volume': total_volume,
            'duration': f"{duration} minutes" if duration else None
        }
    
    def get_this_week(self, obj):
        from django.utils import timezone
        from datetime import timedelta
        
        week_ago = timezone.localdate() - timedelta(days=7)
        week_sessions = WorkoutSession.objects.filter(
            user=obj,
            status='completed',
            end_time__date__gte=week_ago
        )
        
        week_volume = sum(
            sum((log.weight * log.reps) for log in session.set_logs.all() if log.weight and log.reps)
            for session in week_sessions
        )
        
        # Calculate completion rate for this week
        week_progress = RoutineProgress.objects.filter(
            user=obj,
            updated_at__date__gte=week_ago
        )
        total_days = week_progress.count()
        completed_days = week_progress.filter(status='completed').count()
        completion_rate = round((completed_days / total_days * 100) if total_days > 0 else 0, 1)
        
        return {
            'sessions': week_sessions.count(),
            'total_volume': week_volume,
            'completion_rate': completion_rate
        }
    
    def get_recent_exercises(self, obj):
        # Get recent exercises with their latest performance
        from django.utils import timezone
        from datetime import timedelta
        
        week_ago = timezone.localdate() - timedelta(days=7)
        recent_logs = ExerciseSetLog.objects.filter(
            user_exercise_progress__user=obj,
            date__gte=week_ago
        ).select_related(
            'user_exercise_progress__exercise'
        ).order_by(
            'user_exercise_progress__exercise__name',
            '-date'
        )
        
        exercise_summaries = {}
        for log in recent_logs:
            exercise_name = log.user_exercise_progress.exercise.name
            exercise_id = log.user_exercise_progress.exercise.id
            
            if exercise_name not in exercise_summaries:
                exercise_summaries[exercise_name] = {
                    'exercise_name': exercise_name,
                    'exercise_id': exercise_id,
                    'last_performed': log.date,
                    'sets': 0,
                    'reps': 0,
                    'weight': 0,
                    'volume': 0
                }
            
            summary = exercise_summaries[exercise_name]
            summary['sets'] += 1
            summary['reps'] += log.reps or 0
            summary['weight'] = max(summary['weight'], log.weight or 0)
            summary['volume'] += (log.weight * log.reps) if log.weight and log.reps else 0
        
        return list(exercise_summaries.values())[:5]  # Return top 5 recent exercises