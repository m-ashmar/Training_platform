from django.contrib import admin
from .models import (
    Routine, RoutineExercise, Exercise,
    ExerciseMedia, UserExerciseProgress, ExerciseSetLog, RoutineProgress, WorkoutSession,
    RoutineTemplate, RoutineTemplateExercise
)
from django.db.models import Sum
from django.contrib import messages
from modeltranslation.admin import TranslationAdmin
import matplotlib
matplotlib.use('Agg')

class ExerciseMediaInline(admin.TabularInline):
    model = ExerciseMedia
    extra = 1
    fields = ['media_type', 'content']  # Show media fields


# Exercise Admin

# Routine Exercise Inline
class RoutineExerciseInline(admin.TabularInline):
    model = RoutineExercise
    extra = 1
    fields = ['exercise', 'sets', 'reps', 'rest_time', 'day', 'order']


@admin.register(Exercise)
class ExerciseAdmin(TranslationAdmin):
    list_display = ('name', 'target_muscle', 'description', 'created_by', 'has_image')
    inlines = [ExerciseMediaInline]
    search_fields = ('name', 'target_muscle', 'description')
    list_filter = ('target_muscle', 'difficulty_level', 'is_global', 'is_active')
    readonly_fields = ('created_at', 'updated_at')
    fieldsets = (
        (None, {
            'fields': ('name', 'description', 'image')
        }),
        ('Classification', {
            'fields': ('target_muscle', 'difficulty_level')
        }),
        ('Access Control', {
            'fields': ('created_by', 'is_global', 'is_active')
        }),
        ('Timestamps', {
            'fields': ('created_at', 'updated_at'),
            'classes': ('collapse',)
        }),
    )

    def has_image(self, obj):
        return bool(obj.image)
    has_image.boolean = True
    has_image.short_description = 'Has Image'

    def get_media_display(self, obj):
        media = obj.media.all()
        return ", ".join([m.media_type.capitalize() for m in media]) if media else "No media"
    get_media_display.short_description = "Media"


# Register ExerciseSetLog
@admin.register(Routine)
class RoutineAdmin(admin.ModelAdmin):
    list_display = ('name', 'created_by', 'is_active', 'start_date', 'end_date', 'get_assigned_clients')
    list_filter = ('is_active', 'created_by')
    search_fields = ('name', 'created_by__username', 'assigned_to__username')
    filter_horizontal = ('assigned_to',)
    readonly_fields = ('created_at', 'updated_at')

    def get_assigned_clients(self, obj):
        return ", ".join([c.username for c in obj.assigned_to.all()])
    get_assigned_clients.short_description = 'Assigned Clients'


# Inline for ExerciseSetLog under UserExerciseProgress
class ExerciseSetLogInline(admin.TabularInline):
    model = ExerciseSetLog
    extra = 0
    fields = ['set_number', 'weight', 'reps', 'rest_time', 'date']
    readonly_fields = ['set_number', 'weight', 'reps', 'rest_time', 'date']

# Register RoutineProgress
@admin.register(RoutineProgress)
class RoutineProgressAdmin(admin.ModelAdmin):
    list_display = ['user', 'routine', 'day', 'status', 'exercises_completed', 'total_exercises', 'completion_percentage', 'updated_at']
    list_filter = ('routine', 'status', 'updated_at')
    search_fields = ('user__username', 'routine__name')
    readonly_fields = ['completion_percentage']

    # Simple bar chart for completion rate (placeholder)
    def changelist_view(self, request, extra_context=None):
        try:
            import matplotlib.pyplot as plt
            from io import BytesIO
            import base64
            qs = self.get_queryset(request)
            data = qs.values_list('status', flat=True)
            completed = sum(1 for s in data if s == 'Completed')
            in_progress = sum(1 for s in data if s == 'In Progress')
            not_started = sum(1 for s in data if s == 'Not Started')
            skipped = sum(1 for s in data if s == 'Skipped')
            labels = ['Completed', 'In Progress', 'Not Started', 'Skipped']
            values = [completed, in_progress, not_started, skipped]
            fig, ax = plt.subplots()
            ax.bar(labels, values)
            ax.set_ylabel('Count')
            ax.set_title('Routine Progress Status')
            buf = BytesIO()
            plt.savefig(buf, format='png')
            plt.close(fig)
            buf.seek(0)
            image_base64 = base64.b64encode(buf.read()).decode('utf-8')
            extra_context = extra_context or {}
            extra_context['chart'] = image_base64
        except Exception:
            pass
        return super().changelist_view(request, extra_context=extra_context)

    def completion_percentage(self, obj):
        if obj.total_exercises == 0:
            return 0
        return round(100 * obj.exercises_completed / obj.total_exercises, 2)
    completion_percentage.short_description = 'Completion %'


# Exercise Media Inline - allows adding media (photo, video, text) for each exercise


# Routine Admin
# User Exercise Progress Admin
@admin.register(UserExerciseProgress)
class UserExerciseProgressAdmin(admin.ModelAdmin):
    list_display = ('user', 'exercise', 'date', 'completed_sets', 'target_sets', 'skipped')
    list_filter = ('exercise', 'user', 'skipped')
    search_fields = ('user__username', 'exercise__name')
    inlines = [ExerciseSetLogInline]

    def save_model(self, request, obj, form, change):
        try:
            super().save_model(request, obj, form, change)
        except Exception as e:
            if 'unique constraint' in str(e).lower():
                self.message_user(request, 'A progress record for this user, exercise, and date already exists.', level=messages.ERROR)
            else:
                self.message_user(request, f'Error: {e}', level=messages.ERROR)

# Register WorkoutSession in admin
@admin.register(WorkoutSession)
class WorkoutSessionAdmin(admin.ModelAdmin):
    list_display = ('user', 'routine', 'start_time', 'end_time', 'status')
    list_filter = ('status', 'routine', 'user')
    search_fields = ('user__username', 'routine__name')

# Routine Template Inline
class RoutineTemplateExerciseInline(admin.TabularInline):
    model = RoutineTemplateExercise
    extra = 1

@admin.register(RoutineTemplate)
class RoutineTemplateAdmin(TranslationAdmin):
    list_display = ('name', 'goal', 'created_by', 'is_public', 'created_at')
    search_fields = ('name', 'goal', 'description')
    list_filter = ('goal', 'is_public')
    inlines = [RoutineTemplateExerciseInline]
    # TODO: Add filter by tags, usage metrics, etc.

@admin.register(RoutineTemplateExercise)
class RoutineTemplateExerciseAdmin(admin.ModelAdmin):
    list_display = ('template', 'exercise', 'sets', 'reps', 'rest_time', 'order')
    search_fields = ('template__name', 'exercise__name')

@admin.register(ExerciseSetLog)
class ExerciseSetLogAdmin(admin.ModelAdmin):
    list_display = ('user_exercise_progress', 'set_number', 'weight', 'reps', 'rest_time', 'date')
    list_filter = ('user_exercise_progress__user', 'date')
    search_fields = ('user_exercise_progress__user__username', 'user_exercise_progress__exercise__name')

    def save_model(self, request, obj, form, change):
        try:
            super().save_model(request, obj, form, change)
        except Exception as e:
            if 'unique constraint' in str(e).lower():
                self.message_user(request, 'A set log for this progress, set number, and date already exists.', level=messages.ERROR)
            else:
                self.message_user(request, f'Error: {e}', level=messages.ERROR)