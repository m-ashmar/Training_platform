from django.contrib import admin
from .models import (
    Routine, RoutineExercise, Exercise,
    ExerciseMedia, UserExerciseProgress, ExerciseSetLog, RoutineProgress
)
from django.db.models import Sum
class ExerciseMediaInline(admin.TabularInline):
    model = ExerciseMedia
    extra = 1
    fields = ['media_type', 'content']  # Show media fields


# Exercise Admin

# Routine Exercise Inline
class RoutineExerciseInline(admin.TabularInline):
    model = RoutineExercise
    extra = 1
    fields = ['exercise', 'sets', 'repetitions', 'rest_time', 'day', 'order']


@admin.register(Exercise)
class ExerciseAdmin(admin.ModelAdmin):
    list_display = ('name', 'description', 'get_media_display')
    inlines = [ExerciseMediaInline]

    def get_media_display(self, obj):
        media = obj.media.all()
        return ", ".join([m.media_type.capitalize() for m in media]) if media else "No media"
    get_media_display.short_description = "Media"


# Register ExerciseSetLog
@admin.register(Routine)
class RoutineAdmin(admin.ModelAdmin):
    list_display = ('name', 'days', 'created_by', 'is_active' , 'get_assigned_users','created_at', 'scheduled_date')
    inlines = [RoutineExerciseInline]  # Inline for adding exercises
    filter_horizontal = ('assigned_to', 'exercises')  # Allow multi-select for users and exercises
    exclude = ('exercises',)
    search_fields = ('name', 'created_by__username')  # Search by routine name or creator
    list_filter = ('is_active', 'days')  # Filter by active status and number of days



@admin.register(ExerciseSetLog)
class ExerciseSetLogAdmin(admin.ModelAdmin):
    list_display = ('user_exercise_progress', 'set_number', 'weight', 'date')
    list_filter = ('user_exercise_progress__user', 'date')  # Filter by user and date
    search_fields = ('user_exercise_progress__user__username', 'user_exercise_progress__exercise__name')


# Register RoutineProgress
@admin.register(RoutineProgress)
class RoutineProgressAdmin(admin.ModelAdmin):
    list_display = ['user', 'routine', 'day', 'status', 'updated_at']
    list_filter = ('routine', 'status', 'updated_at')  # Filter by routine, status, and update time
    search_fields = ('user__username', 'routine__name')


# Exercise Media Inline - allows adding media (photo, video, text) for each exercise


# Routine Admin
# User Exercise Progress Admin
@admin.register(UserExerciseProgress)
class UserExerciseProgressAdmin(admin.ModelAdmin):
    list_display = ('user', 'exercise', 'date', 'completed_sets', 'target_sets', 'skipped')
    list_filter = ('exercise', 'user', 'skipped')  # Allow filtering by exercise, user, and skipped status
    search_fields = ('user__username', 'exercise__name')  # Search by username or exercise name