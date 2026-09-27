from django import forms
from django.contrib import admin
from django.utils.translation import gettext_lazy as _
from .models import (
    Routine, RoutineExercise, Exercise,
    ExerciseMedia, UserExerciseProgress, ExerciseSetLog, RoutineProgress, WorkoutSession,
    RoutineTemplate, RoutineTemplateExercise
)
from django.db.models import Sum
from django.contrib import messages
from modeltranslation.admin import TranslationAdmin
import matplotlib
import logging

logger = logging.getLogger(__name__)
matplotlib.use('Agg')

class ExerciseMediaInline(admin.TabularInline):
    model = ExerciseMedia
    extra = 1
    fields = ['media_type', 'content']  # Show media fields


# Exercise Admin

class StarterCatalogueFilter(admin.SimpleListFilter):
    """The exercises `manage.py bootstrap_platform` wrote from the starter catalogue
    (`routine/data/exercise_catalogue.py`), for the owner to review: they carry
    `catalogue_key`, and nothing a person made does."""
    title = "Starter catalogue"
    parameter_name = "starter"

    def lookups(self, request, model_admin):
        return (("yes", "From the starter catalogue"), ("no", "Made by people"))

    def queryset(self, request, queryset):
        if self.value() == "yes":
            return queryset.filter(catalogue_key__isnull=False)
        if self.value() == "no":
            return queryset.filter(catalogue_key__isnull=True)
        return queryset


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
    list_filter = ('target_muscle', 'difficulty_level', 'is_global', 'is_active',
                   StarterCatalogueFilter)
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


class OneClientRoutineForm(forms.ModelForm):
    """A routine is one client's private program (the owner, 2026-09-19). The admin's
    two-pane picker let staff put several clients on one routine, sharing its rows,
    and with no settlement of any coaching month.

    Only a client being ADDED is checked: removing clients, renaming or retiring a
    routine (a legacy shared one included) always saves."""

    class Meta:
        model = Routine
        fields = '__all__'

    def clean_assigned_to(self):
        from .assignment import trained_by_someone_else

        chosen = self.cleaned_data.get('assigned_to')
        if chosen is None:
            return chosen
        current = (set(self.instance.assigned_to.values_list('pk', flat=True))
                   if self.instance.pk else set())
        added = [c for c in chosen if c.pk not in current]
        if not added:
            return chosen
        if len(chosen) > 1:
            raise forms.ValidationError(
                _("A routine belongs to one client. Copy it for each other client."),
                code='routine_belongs_to_another_client')
        if self.instance.pk and trained_by_someone_else(self.instance, added[0],
                                                        check_assignees=False):
            raise forms.ValidationError(
                _("This routine already belongs to another client. "
                  "Create a copy for this client instead."),
                code='routine_belongs_to_another_client')
        return chosen


class OneClientRoutineAdminMixin:
    """The console's side of `routine/assignment.py`, for both admin sites: one client
    per routine; an edited name or description clears its stale translation; and a
    client added here settles the coaching's first month, as the API's assignment does —
    a first program assigned by staff left the trainer unpaid."""
    form = OneClientRoutineForm

    def save_model(self, request, obj, form, change):
        edited = [f for f in ('name', 'description') if f in form.changed_data]
        if change and edited:
            obj.translations = Routine.forget_translations(obj.translations, *edited)
        super().save_model(request, obj, form, change)

    def save_related(self, request, form, formsets, change):
        from .assignment import settle_first_month

        routine = form.instance
        before = set(routine.assigned_to.values_list('pk', flat=True))
        super().save_related(request, form, formsets, change)
        for client in routine.assigned_to.exclude(pk__in=before):
            settle_first_month(routine.created_by, client)


# Register ExerciseSetLog
@admin.register(Routine)
class RoutineAdmin(OneClientRoutineAdminMixin, admin.ModelAdmin):
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
            completed = sum(1 for s in data if s == 'completed')
            in_progress = sum(1 for s in data if s == 'in_progress')
            not_started = sum(1 for s in data if s == 'not_started')
            skipped = sum(1 for s in data if s == 'skipped')
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
            # Optional side effect: swallowing this silently is what made the
            # surrounding failures invisible in logs. Control flow is unchanged.
            logger.debug('suppressed non-fatal error', exc_info=True)
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