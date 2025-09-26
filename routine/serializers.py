from rest_framework import serializers
from django.conf import settings
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
        if value:
            if value.size > 5 * 1024 * 1024:  # 5MB limit for exercise images
                raise serializers.ValidationError('Exercise image size must be under 5MB.')
            valid_types = ['image/jpeg', 'image/png', 'image/webp']
            if hasattr(value, 'content_type') and value.content_type not in valid_types:
                raise serializers.ValidationError('Only JPEG, PNG, and WebP images are allowed.')
        return value


class RoutineExerciseSerializer(serializers.ModelSerializer):
    """Serializer for Routine Exercises."""
    exercise = serializers.PrimaryKeyRelatedField(queryset=Exercise.objects.all())
    routine = serializers.PrimaryKeyRelatedField(queryset=Routine.objects.all())

    class Meta:
        model = RoutineExercise
        fields = '__all__'  # Expose all fields for now; restrict as needed
        read_only_fields = ['id', 'created_at', 'updated_at']
    # TODO: Add custom validation if needed


class RoutineSerializer(serializers.ModelSerializer):
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

    class Meta:
        model = Routine
        fields = [
            'id', 'name', 'description', 'is_active', 'days', 'start_date', 'end_date',
            'created_at', 'updated_at', 'routine_exercises', 'assigned_to', 'assigned_usernames',
            'created_by', 'client_count'
        ]
        read_only_fields = ['created_at', 'updated_at', 'created_by']

    def get_assigned_usernames(self, obj):
        """Retrieve assigned user names."""
        return [user.username for user in obj.assigned_to.all()]

    def get_client_count(self, obj):
        """Get count of clients assigned to this routine."""
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
                raise serializers.ValidationError("Only trainers and admins can create routines")
            
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


class RoutineProgressSerializer(serializers.ModelSerializer):
    """Serializer for tracking progress of routines for each user."""
    user = serializers.StringRelatedField(read_only=True)  # Display user's name
    routine = RoutineSerializer(read_only=True)

    class Meta:
        model = RoutineProgress
        fields = ['id', 'user', 'routine', 'day', 'status', 'updated_at']


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

    class Meta:
        model = ExerciseSetLog
        fields = ['id', 'user_exercise_progress', 'workout_session', 'set_number', 'weight', 'reps', 'volume', 'date', 'notes', 'rest_time', 'rpe']
        # TODO: Add more fields as needed for analytics/reporting

    def get_volume(self, obj):
        """Calculate volume for this set (weight × reps)"""
        weight = obj.weight or 0
        reps = obj.reps or 0
        return weight * reps


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
        return obj.assigned_to.filter(user_type='client').count()

    def get_completion_rate(self, obj):
        """
        Calculate completion rate for this routine.
        
        TODO: Implement actual completion rate calculation
        TODO: Add progress tracking and analytics
        """
        # Placeholder for completion rate calculation
        return 0.0

    def validate(self, attrs):
        """
        Validate that only trainers can use this serializer.
        
        TODO: Add more comprehensive validation rules
        TODO: Implement routine template validation
        """
        request = self.context.get('request')
        if request and not request.user.is_trainer:
            logger.warning(f"Non-trainer user {request.user.id} attempted to use TrainerRoutineSerializer")
            raise serializers.ValidationError("This serializer is only for trainers")
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
        """Calculate TDEE for the client."""
        if obj.height and obj.weight and obj.age:
            value = obj.calculate_daily_calories('Maintain')
            return value if value is not None else 0.0
        return 0.0

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
            raise serializers.ValidationError("This serializer is only for client profiles")
        return attrs


class WorkoutSessionSerializer(serializers.ModelSerializer):
    class Meta:
        model = WorkoutSession
        fields = '__all__'  # Expose all fields for now; restrict as needed
        read_only_fields = ['id', 'start_time', 'end_time']
    # TODO: Add custom validation if needed


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
    exercises_completed = serializers.SerializerMethodField()
    
    class Meta:
        model = WorkoutSession
        fields = [
            'id', 'routine_name', 'routine_id', 'start_time', 'end_time',
            'status', 'duration_minutes', 'total_volume', 'exercises_completed'
        ]
    
    def get_duration_minutes(self, obj):
        if obj.start_time and obj.end_time:
            duration = obj.end_time - obj.start_time
            return round(duration.total_seconds() / 60, 1)
        return None
    
    def get_total_volume(self, obj):
        # Calculate total volume from set logs in this session
        set_logs = ExerciseSetLog.objects.filter(workout_session=obj)
        total_volume = sum(
            (log.weight * log.reps) for log in set_logs 
            if log.weight and log.reps
        )
        return total_volume
    
    def get_exercises_completed(self, obj):
        # Get unique exercises completed in this session
        set_logs = ExerciseSetLog.objects.filter(workout_session=obj)
        exercises = set_logs.values(
            'user_exercise_progress__exercise__name',
            'user_exercise_progress__exercise__id'
        ).distinct()
        
        exercise_details = []
        for exercise in exercises:
            exercise_logs = set_logs.filter(
                user_exercise_progress__exercise__id=exercise['user_exercise_progress__exercise__id']
            )
            
            sets_data = []
            for log in exercise_logs:
                sets_data.append({
                    'set_number': log.set_number,
                    'weight': log.weight,
                    'reps': log.reps,
                    'volume': log.weight * log.reps if log.weight and log.reps else 0,
                    'rpe': log.rpe,
                    'notes': log.notes
                })
            
            exercise_details.append({
                'exercise_name': exercise['user_exercise_progress__exercise__name'],
                'exercise_id': exercise['user_exercise_progress__exercise__id'],
                'sets_completed': len(exercise_logs),
                'sets_data': sets_data,
                'total_volume': sum(set_data['volume'] for set_data in sets_data)
            })
        
        return exercise_details


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
        thirty_days_ago = timezone.now().date() - timedelta(days=30)
        
        recent_progress = RoutineProgress.objects.filter(
            user=client,
            updated_at__date__gte=thirty_days_ago
        )
        
        total_days = recent_progress.count()
        completed_days = recent_progress.filter(status='Completed').count()
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
        week_ago = timezone.now().date() - timedelta(days=7)
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
        month_ago = timezone.now().date() - timedelta(days=30)
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
        
        week_ago = timezone.now().date() - timedelta(days=7)
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
        completed_days = week_progress.filter(status='Completed').count()
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
        
        week_ago = timezone.now().date() - timedelta(days=7)
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