"""
Comprehensive Admin Dashboard for Training Platform

This module provides a powerful admin interface for managing all aspects of the training platform:
- User Management (Clients, Trainers, Admins)
- Routine & Exercise Management
- Diet & Nutrition Management
- Subscription & Payment Management
- Analytics & Reporting
- Social Features Management
- System Configuration
"""

from django.contrib import admin
from django.contrib.admin import AdminSite
from django.utils.html import format_html
from django.urls import path, reverse
from django.shortcuts import render, redirect
from django.http import JsonResponse
from django.contrib import messages
from django.db.models import Count, Sum, Avg, Q
from django.utils import timezone
from django.utils.safestring import mark_safe
from django.template.response import TemplateResponse
from django.contrib.admin.views.decorators import staff_member_required
from django.utils.decorators import method_decorator
from django.views.decorators.csrf import csrf_exempt
from django.http import HttpResponse
import json
from datetime import datetime, timedelta
import logging

# Import all models
from users.models import CustomUser, TrainerClientRelation, DeviceToken
from routine.models import (
    Exercise, Routine, RoutineExercise, UserExerciseProgress, 
    ExerciseSetLog, RoutineProgress, WorkoutSession, RoutineTemplate,
    RoutineTemplateExercise, ExerciseMedia
)
from diet.models import (
    FoodItem, FoodCategory, UserFoodPreference, DietPlan, 
    Meal, MealComponent, DailyAdvice
)
from subscription.models import (
    SubscriptionPlan, Subscription, Payment, 
    SubscriptionFeature, SubscriptionUsage
)
from social.models import (
    Post, Comment, UserFollow, Challenge, ChallengeParticipation,
    Achievement, UserAchievement, Leaderboard
)
from analytics.models import (
    UserActivity, PerformanceMetric, UserSession, UserGoal,
    AnalyticsDashboard
)

logger = logging.getLogger(__name__)

# ============================================================================
# CUSTOM ADMIN SITE CONFIGURATION
# ============================================================================

def _log_admin_action(request, obj, message):
    """Record a bulk change in Django's admin log.

    Actions that used `queryset.update()` wrote nothing to LogEntry, so a bulk
    privilege change (e.g. verifying every trainer) left no record of who did it.
    """
    from django.contrib.admin.models import CHANGE, LogEntry
    from django.contrib.contenttypes.models import ContentType
    try:
        LogEntry.objects.log_action(
            user_id=request.user.pk,
            content_type_id=ContentType.objects.get_for_model(obj.__class__).pk,
            object_id=obj.pk, object_repr=str(obj),
            action_flag=CHANGE, change_message=message,
        )
    except Exception:  # auditing must never break the action itself
        logger.warning("Could not write an admin LogEntry for %s", obj, exc_info=True)


def _bulk_apply(request, queryset, message, **fields):
    """Apply field changes per row via save(), logging each one.

    `queryset.update()` skips save(), every signal and every validator — so
    activating users bypassed the state rules on CustomUser.save(), and flipping
    Exercise visibility never bumped the cache version, leaving clients on stale data.
    """
    count = 0
    for obj in queryset:
        for k, v in fields.items():
            setattr(obj, k, v)
        obj.save(update_fields=list(fields) if obj.pk else None)
        _log_admin_action(request, obj, message)
        count += 1
    return count


class TrainingPlatformAdminSite(AdminSite):
    """Custom admin site for the training platform"""
    site_header = "Training Platform Administration"
    site_title = "Training Platform Admin"
    index_title = "Welcome to Training Platform Administration"
    
    def get_app_list(self, request, app_label=None, *args, **kwargs):
        """Customize the app list to group related models.

        Accepts optional ``app_label`` and varargs for compatibility with
        different Django versions and call sites.
        """
        # Call parent implementation with compatibility for different Django versions
        try:
            app_list = super().get_app_list(request, app_label, *args, **kwargs)  # type: ignore[call-arg]
        except TypeError:
            app_list = super().get_app_list(request)
            if app_label is not None:
                app_list = [app for app in app_list if app.get('app_label') == app_label]

        # Reorganize apps for better UX
        for app in app_list:
            if app['app_label'] == 'users':
                app['name'] = '👥 User Management'
            elif app['app_label'] == 'routine':
                app['name'] = '🏋️ Training Management'
            elif app['app_label'] == 'diet':
                app['name'] = '🥗 Nutrition Management'
            elif app['app_label'] == 'subscription':
                app['name'] = '💳 Subscription & Payments'
            elif app['app_label'] == 'social':
                app['name'] = '📱 Social Features'
            elif app['app_label'] == 'analytics':
                app['name'] = '📊 Analytics & Reports'
            elif app['app_label'] == 'challenges':
                app['name'] = '🏆 Challenges & Achievements'
        
        return app_list

# Create custom admin site instance
admin_site = TrainingPlatformAdminSite(name='training_admin')

# ============================================================================
# USER MANAGEMENT
# ============================================================================

class CustomUserAdmin(admin.ModelAdmin):
    """Enhanced user management with comprehensive features"""
    
    # Without this each row re-queried assigned_trainer — ~100 extra
    # queries per changelist page.
    list_select_related = ('assigned_trainer',)
    list_display = (
        'username', 'email', 'user_type', 'is_active', 'is_staff',
        'assigned_trainer', 'trainer_status', 'client_count', 'date_joined'
    )
    list_editable = ('user_type', 'is_active', 'is_staff')
    list_filter = (
        'user_type', 'is_active', 'is_staff', 'trainer_is_verified', 
        'trainer_is_available', 'date_joined', 'gender', 'activity_level'
    )
    search_fields = ('username', 'email', 'phone_number', 'first_name', 'last_name')
    ordering = ('-date_joined',)
    
    # Inline for related data
    inlines = []
    
    # Actions
    actions = [
        'activate_users', 'deactivate_users', 'make_trainers_verified',
        'reset_passwords', 'export_user_data', 'bulk_assign_trainers'
    ]
    
    # Fieldsets for organized editing
    fieldsets = (
        ('Basic Information', {
            # `password` is deliberately ABSENT. As a plain ModelAdmin field it rendered
            # as a text input and wrote whatever was typed straight to the column,
            # unhashed — locking the account out and storing the value in clear. Use
            # the 'Send password reset' action instead.
            'fields': ('username', 'email', 'phone_number')
        }),
        ('Personal Information', {
            'fields': (
                'first_name', 'last_name', 'profile_picture', 'height', 
                'weight', 'age', 'gender', 'activity_level', 'specific_injury'
            )
        }),
        ('User Type & Permissions', {
            'fields': ('user_type', 'is_active', 'is_staff', 'is_superuser')
        }),
        ('Trainer Information', {
            'fields': (
                'trainer_bio', 'trainer_specializations', 'trainer_certifications', 
                'trainer_experience_years', 'trainer_hourly_rate', 
                'trainer_is_verified', 'trainer_is_available'
            ),
            'classes': ('collapse',)
        }),
        ('Client Information', {
            'fields': ('assigned_trainer', 'client_goals', 'client_preferences'),
            'classes': ('collapse',)
        }),
        ('System Information', {
            'fields': ('date_joined', 'last_login', 'created_at', 'updated_at'),
            'classes': ('collapse',)
        }),
    )
    
    readonly_fields = ('date_joined', 'last_login', 'created_at', 'updated_at', 'client_count')
    
    def trainer_status(self, obj):
        """Display trainer verification status"""
        if obj.user_type == 'trainer':
            if obj.trainer_is_verified:
                return format_html('<span style="color: green;">✓ Verified</span>')
            else:
                return format_html('<span style="color: orange;">⚠ Pending</span>')
        return "-"
    trainer_status.short_description = "Trainer Status"
    
    def client_count(self, obj):
        """Show number of clients for trainers"""
        if obj.user_type == 'trainer':
            return obj.clients.count()
        return "-"
    client_count.short_description = "Clients"
    
    # Custom actions
    def activate_users(self, request, queryset):
        updated = _bulk_apply(request, queryset, "Activated via bulk admin action", is_active=True)
        self.message_user(request, f"{updated} record(s) updated.")
    activate_users.short_description = "Activate selected users"
    
    def deactivate_users(self, request, queryset):
        updated = _bulk_apply(request, queryset, "Deactivated via bulk admin action", is_active=False)
        self.message_user(request, f"{updated} record(s) updated.")
    deactivate_users.short_description = "Deactivate selected users"
    
    def make_trainers_verified(self, request, queryset):
        updated = _bulk_apply(request, queryset, "Trainer verified via bulk admin action", trainer_is_verified=True)
        self.message_user(request, f"{updated} record(s) updated.")
    make_trainers_verified.short_description = "Verify selected trainers"
    
    def reset_passwords(self, request, queryset):
        """Invalidate the current password and force a reset.

        This used to set every selected account to the literal string "testpass123"
        and print it back in the confirmation banner — a known shared credential on
        real accounts. Now it makes the existing password unusable and records an
        admin log entry per user; each user must go through the normal reset flow.
        """
        from django.contrib.admin.models import CHANGE, LogEntry
        from django.contrib.contenttypes.models import ContentType

        ct = ContentType.objects.get_for_model(queryset.model)
        count = 0
        for user in queryset:
            user.set_unusable_password()
            user.save(update_fields=['password'])
            LogEntry.objects.log_action(
                user_id=request.user.pk, content_type_id=ct.pk, object_id=user.pk,
                object_repr=str(user), action_flag=CHANGE,
                change_message='Password invalidated by admin; reset required.',
            )
            count += 1
        self.message_user(
            request,
            f"Invalidated the password for {count} user(s). They must use "
            f"'forgot password' to set a new one — no shared password is issued.",
        )
    reset_passwords.short_description = "Invalidate passwords (force reset)"
    
    def export_user_data(self, request, queryset):
        """Export user data to CSV"""
        import csv
        from django.http import HttpResponse
        
        response = HttpResponse(content_type='text/csv')
        response['Content-Disposition'] = 'attachment; filename="users_export.csv"'
        
        # This streams PII. Record who exported how many rows — previously an export
        # left no trace at all, so a compromised staff session could dump the user
        # table a page at a time invisibly.
        logger.warning(
            "PII export: admin=%s exported %s user record(s)",
            getattr(request.user, 'username', '?'), queryset.count(),
        )

        writer = csv.writer(response)
        writer.writerow(['Username', 'Email', 'User Type', 'Is Active', 'Date Joined'])
        
        for user in queryset:
            writer.writerow([user.username, user.email, user.user_type, user.is_active, user.date_joined])
        
        return response
    export_user_data.short_description = "Export selected users to CSV"
    
    def get_queryset(self, request):
        """Optimize queryset with select_related"""
        return super().get_queryset(request).select_related('assigned_trainer')

# ============================================================================
# TRAINING MANAGEMENT
# ============================================================================

class ExerciseAdmin(admin.ModelAdmin):
    """Comprehensive exercise management"""
    
    # Without this each row re-queried created_by — ~100 extra
    # queries per changelist page.
    list_select_related = ('created_by',)
    list_display = ('name', 'target_muscle', 'difficulty_level', 'created_by', 'is_global', 'is_active', 'media_count')
    list_filter = ('target_muscle', 'difficulty_level', 'is_global', 'is_active', 'created_by')
    search_fields = ('name', 'description', 'target_muscle')
    list_editable = ('is_active', 'is_global')
    
    fieldsets = (
        ('Basic Information', {
            'fields': ('name', 'description', 'image')
        }),
        ('Classification', {
            'fields': ('target_muscle', 'difficulty_level')
        }),
        ('Access Control', {
            'fields': ('created_by', 'is_global', 'is_active')
        }),
        ('System Information', {
            'fields': ('created_at', 'updated_at'),
            'classes': ('collapse',)
        }),
    )
    
    readonly_fields = ('created_at', 'updated_at', 'created_by')
    inlines = []
    
    actions = ['make_global', 'make_private', 'activate_exercises', 'deactivate_exercises']
    
    def media_count(self, obj):
        return obj.media.count()
    media_count.short_description = "Media"
    
    def make_global(self, request, queryset):
        updated = _bulk_apply(request, queryset, "Made global via bulk admin action", is_global=True)
        self.message_user(request, f"{updated} record(s) updated.")
    make_global.short_description = "Make exercises global"
    
    def make_private(self, request, queryset):
        updated = _bulk_apply(request, queryset, "Made private via bulk admin action", is_global=False)
        self.message_user(request, f"{updated} record(s) updated.")
    make_private.short_description = "Make exercises private"

class RoutineAdmin(admin.ModelAdmin):
    """Routine management with client assignment"""
    
    # Without this each row re-queried created_by — ~100 extra
    # queries per changelist page.
    list_select_related = ('created_by',)
    list_display = ('name', 'created_by', 'difficulty_level', 'is_active', 'client_count', 'exercise_count', 'created_at')
    list_filter = ('is_active', 'created_by', 'difficulty_level', 'start_date')
    search_fields = ('name', 'created_by__username', 'assigned_to__username')
    filter_horizontal = ('assigned_to', 'exercises')
    
    fieldsets = (
        ('Basic Information', {
            # 'goal' is not a field on Routine — it raised FieldError and 500'd
            # the add page. Django's admin checks do not cover custom AdminSites.
            'fields': ('name', 'description')
        }),
        ('Configuration', {
            'fields': ('difficulty_level', 'estimated_duration', 'start_date', 'end_date')
        }),
        ('Assignment', {
            'fields': ('created_by', 'assigned_to', 'exercises')
        }),
        ('Status', {
            'fields': ('is_active',)
        }),
        ('System Information', {
            'fields': ('created_at', 'updated_at'),
            'classes': ('collapse',)
        }),
    )
    
    readonly_fields = ('created_at', 'updated_at', 'created_by')
    
    actions = ['activate_routines', 'deactivate_routines', 'duplicate_routines']
    
    def client_count(self, obj):
        return obj.assigned_to.count()
    client_count.short_description = "Clients"
    
    def exercise_count(self, obj):
        return obj.exercises.count()
    exercise_count.short_description = "Exercises"
    
    def duplicate_routines(self, request, queryset):
        for routine in queryset:
            new_routine = Routine.objects.create(
                name=f"{routine.name} (Copy)",
                description=routine.description,
                difficulty_level=routine.difficulty_level,
                estimated_duration=routine.estimated_duration,
                created_by=routine.created_by,
                is_active=False
            )
            new_routine.exercises.set(routine.exercises.all())
        self.message_user(request, f"{queryset.count()} routines duplicated successfully.")
    duplicate_routines.short_description = "Duplicate selected routines"

class RoutineProgressAdmin(admin.ModelAdmin):
    """Track routine completion progress"""
    
    # Without this each row re-queried routine, user — ~100 extra
    # queries per changelist page.
    list_select_related = ('routine', 'user')
    list_display = ('user', 'routine', 'day', 'status', 'completion_percentage', 'updated_at')
    list_filter = ('status', 'routine', 'user', 'updated_at')
    search_fields = ('user__username', 'routine__name')
    readonly_fields = ('completion_percentage', 'updated_at')
    
    def completion_percentage(self, obj):
        if obj.total_exercises == 0:
            return 0
        return round(100 * obj.exercises_completed / obj.total_exercises, 2)
    completion_percentage.short_description = "Completion %"

class WorkoutSessionAdmin(admin.ModelAdmin):
    """Workout session tracking"""
    
    # Without this each row re-queried routine, user — ~100 extra
    # queries per changelist page.
    list_select_related = ('routine', 'user')
    list_display = ('user', 'routine', 'start_time', 'end_time', 'status', 'duration')
    list_filter = ('status', 'routine', 'user', 'start_time')
    search_fields = ('user__username', 'routine__name')
    readonly_fields = ('start_time', 'duration')
    
    def duration(self, obj):
        if obj.end_time and obj.start_time:
            return obj.end_time - obj.start_time
        return "-"
    duration.short_description = "Duration"

# ============================================================================
# NUTRITION MANAGEMENT
# ============================================================================

class FoodItemAdmin(admin.ModelAdmin):
    """Food item management with nutritional data"""
    
    # Without this each row re-queried category — ~100 extra
    # queries per changelist page.
    list_select_related = ('category',)
    list_display = ('name', 'category', 'calories', 'protein', 'carbs', 'fat', 'serving_size')
    list_filter = ('category', 'calories')
    search_fields = ('name', 'category__name')
    list_editable = ('category',)
    
    fieldsets = (
        ('Basic Information', {
            'fields': ('api_id', 'name', 'image_url')
        }),
        ('Nutritional Information', {
            'fields': ('calories', 'protein', 'carbs', 'fat')
        }),
        ('Serving Information', {
            'fields': ('serving_size', 'serving_size_grams')
        }),
        ('Classification', {
            'fields': ('category',)
        }),
    )
    
    actions = ['recalculate_nutrition', 'import_from_edamam']

class DietPlanAdmin(admin.ModelAdmin):
    """Diet plan management"""
    
    # Without this each row re-queried user — ~100 extra
    # queries per changelist page.
    list_select_related = ('user',)
    list_display = ('user', 'goal', 'daily_calories', 'start_date', 'end_date', 'is_active', 'meal_count')
    list_filter = ('goal', 'is_active', 'start_date', 'end_date')
    search_fields = ('user__username', 'goal')
    
    def meal_count(self, obj):
        return obj.meals.count()
    meal_count.short_description = "Meals"

class MealAdmin(admin.ModelAdmin):
    """Meal management"""
    
    # Without this each row re-queried diet_plan — ~100 extra
    # queries per changelist page.
    list_select_related = ('diet_plan',)
    list_display = ('diet_plan', 'meal_type', 'date', 'template', 'is_ai_generated', 'scheduled_time')
    list_filter = ('meal_type', 'template', 'is_ai_generated', 'date')
    search_fields = ('diet_plan__user__username', 'description')

# ============================================================================
# SUBSCRIPTION & PAYMENT MANAGEMENT
# ============================================================================

class SubscriptionPlanAdmin(admin.ModelAdmin):
    """Subscription plan management"""
    
    list_display = ('name', 'plan_type', 'price', 'duration_days', 'is_active', 'feature_count')
    list_filter = ('plan_type', 'is_active', 'has_diet_access', 'has_routine_access')
    search_fields = ('name',)
    list_editable = ('is_active', 'price')
    
    fieldsets = (
        ('Basic Information', {
            'fields': ('name', 'plan_type', 'description', 'price', 'duration_days', 'is_active')
        }),
        ('Features', {
            'fields': (
                'has_diet_access', 'has_routine_access', 'has_challenges_access',
                'has_ai_advice', 'has_priority_support', 'max_meals_per_day', 'max_routines'
            )
        }),
        ('System Information', {
            'fields': ('created_at', 'updated_at'),
            'classes': ('collapse',)
        })
    )
    
    readonly_fields = ('created_at', 'updated_at')
    
    actions = ['activate_plans', 'deactivate_plans']
    
    def feature_count(self, obj):
        features = []
        if obj.has_diet_access: features.append('Diet')
        if obj.has_routine_access: features.append('Routine')
        if obj.has_challenges_access: features.append('Challenges')
        if obj.has_ai_advice: features.append('AI Advice')
        return len(features)
    feature_count.short_description = "Features"

class SubscriptionAdmin(admin.ModelAdmin):
    """User subscription management"""
    
    # Without this each row re-queried plan, user — ~100 extra
    # queries per changelist page.
    list_select_related = ('plan', 'user')
    list_display = ('user', 'plan', 'status', 'start_date', 'end_date', 'is_active', 'auto_renew')
    list_filter = ('plan', 'status', 'auto_renew', 'start_date')
    search_fields = ('user__email', 'user__username')
    readonly_fields = ('start_date', 'end_date', 'created_at', 'updated_at')
    
    actions = ['activate_subscriptions', 'cancel_subscriptions', 'extend_subscriptions']

class PaymentAdmin(admin.ModelAdmin):
    """Payment tracking"""
    
    # Without this each row re-queried subscription — ~100 extra
    # queries per changelist page.
    list_select_related = ('subscription',)
    list_display = ('subscription', 'amount', 'payment_method', 'status', 'created_at')
    list_filter = ('status', 'payment_method', 'created_at')
    search_fields = ('subscription__user__email', 'transaction_id')
    readonly_fields = ('created_at', 'updated_at')

# ============================================================================
# SOCIAL FEATURES MANAGEMENT
# ============================================================================

class PostAdmin(admin.ModelAdmin):
    """Social post management"""
    
    # Without this each row re-queried author — ~100 extra
    # queries per changelist page.
    list_select_related = ('author',)
    list_display = ('author', 'post_type', 'title', 'visibility', 'is_hidden', 'likes_count', 'created_at')
    list_filter = ('post_type', 'visibility', 'is_flagged', 'is_hidden', 'created_at')
    search_fields = ('author__username', 'title', 'content')
    list_editable = ('visibility', 'is_hidden')
    
    actions = ['hide_posts', 'unhide_posts', 'flag_posts', 'unflag_posts']

class ChallengeAdmin(admin.ModelAdmin):
    """Challenge management"""
    
    # Without this each row re-queried creator — ~100 extra
    # queries per changelist page.
    list_select_related = ('creator',)
    list_display = ('title', 'creator', 'challenge_type', 'status', 'start_date', 'end_date', 'participant_count')
    list_filter = ('challenge_type', 'status', 'start_date', 'end_date')
    search_fields = ('title', 'creator__username')
    
    def participant_count(self, obj):
        return obj.participations.count()
    participant_count.short_description = "Participants"

class AchievementAdmin(admin.ModelAdmin):
    """Achievement management"""
    
    list_display = ('name', 'category', 'points', 'is_active', 'user_count')
    list_filter = ('category', 'is_active')
    search_fields = ('name', 'description')
    
    def user_count(self, obj):
        return obj.user_achievements.count()
    user_count.short_description = "Users Earned"

# ============================================================================
# ANALYTICS & REPORTING
# ============================================================================

class UserActivityAdmin(admin.ModelAdmin):
    """User activity tracking"""
    
    # Without this each row re-queried user — ~100 extra
    # queries per changelist page.
    list_select_related = ('user',)
    list_display = ('user', 'activity_type', 'timestamp', 'ip_address')
    list_filter = ('activity_type', 'timestamp')
    search_fields = ('user__username', 'activity_type')
    readonly_fields = ('timestamp',)

class PerformanceMetricAdmin(admin.ModelAdmin):
    """Performance metrics tracking"""
    
    # Without this each row re-queried user — ~100 extra
    # queries per changelist page.
    list_select_related = ('user',)
    list_display = ('user', 'metric_type', 'value', 'unit', 'recorded_at')
    list_filter = ('metric_type', 'recorded_at')
    search_fields = ('user__username', 'metric_type')
    readonly_fields = ('recorded_at',)



class AdminDashboardView:
    """Custom admin dashboard with analytics and quick actions"""
    
    def __init__(self, admin_site):
        self.admin_site = admin_site
    
    def index(self, request):
        """Main dashboard view with platform overview"""
        
        # Get key metrics
        total_users = CustomUser.objects.count()
        active_users = CustomUser.objects.filter(is_active=True).count()
        total_trainers = CustomUser.objects.filter(user_type='trainer').count()
        total_clients = CustomUser.objects.filter(user_type='client').count()
        
        # Recent activity
        recent_users = CustomUser.objects.order_by('-date_joined')[:10]
        recent_activities = UserActivity.objects.select_related('user').order_by('-timestamp')[:10]
        
        # Platform statistics
        total_routines = Routine.objects.count()
        total_exercises = Exercise.objects.count()
        total_diet_plans = DietPlan.objects.count()
        active_subscriptions = Subscription.objects.filter(status='active').count()
        
        # Recent posts and challenges
        recent_posts = Post.objects.select_related('author').order_by('-created_at')[:5]
        active_challenges = Challenge.objects.filter(status='active')[:5]
        
        context = {
            'title': 'Training Platform Dashboard',
            'total_users': total_users,
            'active_users': active_users,
            'total_trainers': total_trainers,
            'total_clients': total_clients,
            'total_routines': total_routines,
            'total_exercises': total_exercises,
            'total_diet_plans': total_diet_plans,
            'active_subscriptions': active_subscriptions,
            'recent_users': recent_users,
            'recent_activities': recent_activities,
            'recent_posts': recent_posts,
            'active_challenges': active_challenges,
        }
        
        return TemplateResponse(request, 'admin/dashboard/index.html', context)

# Register the dashboard view
dashboard_view = AdminDashboardView(admin_site)
admin_site.index = dashboard_view.index

# ============================================================================
# REGISTER ALL MODELS
# ============================================================================

# Register all models with the custom admin site
admin_site.register(CustomUser, CustomUserAdmin)
admin_site.register(TrainerClientRelation)
admin_site.register(DeviceToken)

admin_site.register(Exercise, ExerciseAdmin)
admin_site.register(Routine, RoutineAdmin)
admin_site.register(RoutineExercise)
admin_site.register(UserExerciseProgress)
admin_site.register(ExerciseSetLog)
admin_site.register(RoutineProgress, RoutineProgressAdmin)
admin_site.register(WorkoutSession, WorkoutSessionAdmin)
admin_site.register(RoutineTemplate)
admin_site.register(RoutineTemplateExercise)
admin_site.register(ExerciseMedia)

admin_site.register(FoodItem, FoodItemAdmin)
admin_site.register(FoodCategory)
admin_site.register(UserFoodPreference)
admin_site.register(DietPlan, DietPlanAdmin)
admin_site.register(Meal, MealAdmin)
admin_site.register(MealComponent)
admin_site.register(DailyAdvice)

admin_site.register(SubscriptionPlan, SubscriptionPlanAdmin)
admin_site.register(Subscription, SubscriptionAdmin)
admin_site.register(Payment, PaymentAdmin)
admin_site.register(SubscriptionFeature)
admin_site.register(SubscriptionUsage)

admin_site.register(Post, PostAdmin)
admin_site.register(Comment)
admin_site.register(UserFollow)
admin_site.register(Challenge, ChallengeAdmin)
admin_site.register(ChallengeParticipation)
admin_site.register(Achievement, AchievementAdmin)
admin_site.register(UserAchievement)
admin_site.register(Leaderboard)
# Notifications: register the CANONICAL notifications.Notification store.
# The legacy social.Notification table is no longer written to, so registering it
# here would show admins a permanently empty table and imply notifications broke.
try:
    from notifications.models import Notification as CanonicalNotification
    from notifications.admin import NotificationAdmin as CanonicalNotificationAdmin
    admin_site.register(CanonicalNotification, CanonicalNotificationAdmin)
except Exception:  # pragma: no cover — admin wiring must never break boot
    # Optional side effect: swallowing this silently is what made the
    # surrounding failures invisible in logs. Control flow is unchanged.
    logger.debug('suppressed non-fatal error', exc_info=True)

admin_site.register(UserActivity, UserActivityAdmin)
admin_site.register(PerformanceMetric, PerformanceMetricAdmin)
admin_site.register(UserSession)
admin_site.register(UserGoal)
admin_site.register(AnalyticsDashboard)

# ============================================================================
# URL CONFIGURATION
# ============================================================================

def get_admin_urls():
    """Get admin URLs for the custom admin site"""
    return [
        path('admin/', admin_site.urls),
    ]
