from rest_framework import serializers
from django.conf import settings
from django.contrib.auth import get_user_model
from .models import (
    Routine, Exercise, RoutineExercise,ExerciseMedia,
    UserExerciseProgress, RoutineProgress, ExerciseSetLog
)


class ExerciseMediaSerializer(serializers.ModelSerializer):
    """Serializer for Exercise Media (e.g., videos, photos, text)."""

    class Meta:
        model = ExerciseMedia
        fields = ['id', 'media_type', 'content']


class ExerciseSerializer(serializers.ModelSerializer):
    """Serializer for Exercises, with nested media."""
    media = ExerciseMediaSerializer(many=True, read_only=True)

    class Meta:
        model = Exercise
        fields = ['id', 'name', 'description', 'media']


class RoutineExerciseSerializer(serializers.ModelSerializer):
    """Serializer for Routine Exercises."""
    exercise = ExerciseSerializer(read_only=True)

    class Meta:
        model = RoutineExercise
        fields = ['id', 'exercise', 'sets', 'repetitions', 'rest_time', 'day', 'order']


class RoutineSerializer(serializers.ModelSerializer):
    """Serializer for Routines, including exercises and assigned users."""
    routine_exercises = RoutineExerciseSerializer(many=True, read_only=True)
    assigned_to = serializers.StringRelatedField(many=True, read_only=True)
    assigned_usernames = serializers.SerializerMethodField()

    class Meta:
        model = Routine
        fields = [
            'id', 'name', 'description', 'is_active', 'days', 'start_date', 'end_date',
            'created_at', 'updated_at', 'routine_exercises', 'assigned_to', 'assigned_usernames'
        ]

    def get_assigned_usernames(self, obj):
        """Retrieve assigned user names."""
        return [user.username for user in obj.assigned_to.all()]


class UserExerciseProgressSerializer(serializers.ModelSerializer):
    """Serializer for tracking user progress on individual exercises."""
    exercise = ExerciseSerializer(read_only=True)

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
    """Serializer for exercise set logs."""
    user_exercise_progress = UserExerciseProgressSerializer(read_only=True)

    class Meta:
        model = ExerciseSetLog
        fields = ['id', 'user_exercise_progress', 'set_number', 'weight', 'date']