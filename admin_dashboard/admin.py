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
from subscription.admin import (
    activate_subscriptions, cancel_subscriptions, extend_subscriptions,
)
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
from routine.admin import OneClientRoutineAdminMixin
from routine.models import (
    Exercise, Routine, RoutineExercise, UserExerciseProgress, 
    ExerciseSetLog, RoutineProgress, WorkoutSession, RoutineTemplate,
    RoutineTemplateExercise, ExerciseMedia
)
from diet.models import (
    FoodItem, FoodCategory, UserFoodPreference, DietPlan,
    Meal, MealComponent, DailyAdvice, Recipe, RecipeIngredient,
    UserFoodCategoryPreference, UserFoodWeight, MealSwap, MealEvent,
)
# The diet app's own admin classes. This site had a second, simpler FoodItemAdmin that
# could not edit meal slots, household units or the review flag, and it did not carry
# Recipe at all — so the recipe library, which is the engine's culinary knowledge, was
# unreachable from the admin the owner actually uses. One definition, registered on
# both sites, rather than two that drift.
from diet.admin import (
    ImprovedFoodItemAdmin as DietFoodItemAdmin,
    RecipeAdmin as DietRecipeAdmin,
    ImprovedUserFoodCategoryPreferenceAdmin as DietFoodChoiceAdmin,
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

# Shared with users/admin.py, which is the class actually registered for users.
from training_platform.admin_utils import bulk_apply as _bulk_apply  # noqa: E402
from training_platform.admin_utils import log_admin_action as _log_admin_action  # noqa: E402


def _set_field_action(name, description, message, **fields):
    """An admin action that sets fields one row at a time through `save()`, logged.

    Fourteen actions were declared on this site by NAME — `hide_posts`, `cancel_subscriptions`,
    `activate_routines` and the rest — with no method behind them. `ModelAdmin.get_action`
    returns None for a name it cannot resolve and `_get_base_actions` drops it silently, and
    the admin system checks do not run on a custom AdminSite, so the dropdown simply lacked
    them: there was no bulk post moderation at all, with no error anywhere.
    """
    def action(modeladmin, request, queryset):
        count = _bulk_apply(request, queryset, message, **fields)
        modeladmin.message_user(request, f"{description}: {count} updated.")

    action.__name__ = name
    action.short_description = description
    return action


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

# No user admin here. This module held a `CustomUserAdmin` — with per-row saves, an
# admin log, a PII-logging export and a password reset — that no site registered:
# both sites register `users.admin.CustomUserAdmin` below. Two definitions of the same
# admin, with the correct behaviour in the one that never ran, is how the live one
# kept its `queryset.update()` actions.

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
    
    actions = ['make_global', 'make_private',
               _set_field_action('activate_exercises', 'Activate selected exercises',
                                 'Activated via bulk admin action', is_active=True),
               _set_field_action('deactivate_exercises', 'Deactivate selected exercises',
                                 'Deactivated via bulk admin action', is_active=False)]
    
    def media_count(self, obj):
        # Annotated in get_queryset: `.count()` here was one query per row.
        return getattr(obj, '_media_count', 0)
    media_count.short_description = "Media"
    
    def make_global(self, request, queryset):
        updated = _bulk_apply(request, queryset, "Made global via bulk admin action", is_global=True)
        self.message_user(request, f"{updated} record(s) updated.")
    make_global.short_description = "Make exercises global"
    
    def make_private(self, request, queryset):
        updated = _bulk_apply(request, queryset, "Made private via bulk admin action", is_global=False)
        self.message_user(request, f"{updated} record(s) updated.")
    make_private.short_description = "Make exercises private"

    def get_queryset(self, request):
        # Per-row `.count()` columns cost one query per row — ~125 queries for a
        # 30-row routine page. `list_select_related` fixes FKs, not reverse counts.
        return super().get_queryset(request).annotate(_media_count=Count('media', distinct=True))

class RoutineAdmin(OneClientRoutineAdminMixin, admin.ModelAdmin):
    """Routine management with client assignment. One client per routine, and a client
    added here settles the coaching's first month (routine/assignment.py)."""
    
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
    
    actions = [_set_field_action('activate_routines', 'Activate selected routines',
                                 'Activated via bulk admin action', is_active=True),
               _set_field_action('deactivate_routines', 'Deactivate selected routines',
                                 'Deactivated via bulk admin action', is_active=False),
               'duplicate_routines']
    
    def client_count(self, obj):
        # Annotated in get_queryset: `.count()` here was one query per row.
        return getattr(obj, '_client_count', 0)
    client_count.short_description = "Clients"
    
    def exercise_count(self, obj):
        # Annotated in get_queryset: `.count()` here was one query per row.
        return getattr(obj, '_exercise_count', 0)
    exercise_count.short_description = "Exercises"
    
    def duplicate_routines(self, request, queryset):
        # The full program (days, rows, translations), unassigned and inactive. This
        # copied only the vestigial `exercises` list: a routine with no days at all.
        from routine.assignment import copy_routine
        for routine in queryset:
            copy_routine(routine, name=f"{routine.name} (Copy)"[:255], is_active=False)
        self.message_user(request, f"{queryset.count()} routines duplicated successfully.")
    duplicate_routines.short_description = "Duplicate selected routines"

    def get_queryset(self, request):
        # Per-row `.count()` columns cost one query per row — ~125 queries for a
        # 30-row routine page. `list_select_related` fixes FKs, not reverse counts.
        return super().get_queryset(request).annotate(_client_count=Count('assigned_to', distinct=True), _exercise_count=Count('exercises', distinct=True))

class RoutineProgressAdmin(admin.ModelAdmin):
    """Track routine completion progress"""
    
    # Without this each row re-queried routine, user — ~100 extra
    # queries per changelist page.
    list_select_related = ('routine', 'user')
    list_display = ('user', 'routine', 'day', 'status', 'completion_percentage', 'updated_at')
    # No raw `user` filter: a FK filter renders one sidebar link per row of the user
    # table — 510 links and 70 KB of HTML for a changelist holding zero rows. Search
    # by username instead.
    list_filter = ('status', 'updated_at')
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
    list_filter = ('status', 'start_time')
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
    
    actions = [_set_field_action('activate_plans', 'Activate selected plans',
                                 'Activated via bulk admin action', is_active=True),
               _set_field_action('deactivate_plans', 'Deactivate selected plans',
                                 'Deactivated via bulk admin action', is_active=False)]
    
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
    
    # The same logged, through-the-model actions as the default admin site.
    actions = [activate_subscriptions, cancel_subscriptions, extend_subscriptions]

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
    
    actions = [_set_field_action('hide_posts', 'Hide selected posts',
                                 'Hidden by moderation', is_hidden=True),
               _set_field_action('unhide_posts', 'Unhide selected posts',
                                 'Unhidden by moderation', is_hidden=False),
               _set_field_action('flag_posts', 'Flag selected posts',
                                 'Flagged by moderation', is_flagged=True),
               _set_field_action('unflag_posts', 'Unflag selected posts',
                                 'Unflagged by moderation', is_flagged=False)]

class ChallengeAdmin(admin.ModelAdmin):
    """Challenge management"""
    
    # Without this each row re-queried creator — ~100 extra
    # queries per changelist page.
    list_select_related = ('creator',)
    list_display = ('title', 'creator', 'challenge_type', 'status', 'start_date', 'end_date', 'participant_count')
    list_filter = ('challenge_type', 'status', 'start_date', 'end_date')
    search_fields = ('title', 'creator__username')
    
    def participant_count(self, obj):
        # Annotated in get_queryset: `.count()` here was one query per row.
        return getattr(obj, '_participant_count', 0)
    participant_count.short_description = "Participants"

    def get_queryset(self, request):
        # Per-row `.count()` columns cost one query per row — ~125 queries for a
        # 30-row routine page. `list_select_related` fixes FKs, not reverse counts.
        return super().get_queryset(request).annotate(_participant_count=Count('participations', distinct=True))

class AchievementAdmin(admin.ModelAdmin):
    """Achievement management"""
    
    list_display = ('name', 'category', 'points', 'is_active', 'user_count')
    list_filter = ('category', 'is_active')
    search_fields = ('name', 'description')
    
    def user_count(self, obj):
        # Annotated in get_queryset: `.count()` here was one query per row.
        return getattr(obj, '_user_count', 0)
    user_count.short_description = "Users Earned"

    def get_queryset(self, request):
        # Per-row `.count()` columns cost one query per row — ~125 queries for a
        # 30-row routine page. `list_select_related` fixes FKs, not reverse counts.
        return super().get_queryset(request).annotate(_user_count=Count('user_achievements', distinct=True))

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
        """Main dashboard view with platform overview.

        SUPERUSERS ONLY. Replacing `AdminSite.index` also replaced the one place the
        admin filters what it shows through `has_module_permission` and
        `has_view_permission`; `admin_view` only checks `is_active and is_staff`. So an
        account with `is_staff` and no permissions at all — refused on every changelist —
        was shown the ten newest users with their email addresses and everyone's recent
        activity on the landing page. `is_staff` is one checkbox on the user admin.
        Anyone else gets Django's own index, which lists only what they may see.
        """
        if not request.user.is_superuser:
            return type(self.admin_site).index(self.admin_site, request)

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
        active_challenges = Challenge.objects.filter(Challenge.running_q())[:5]
        
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
# The users app's own admin, which hashes a changed password AND keeps
# `assigned_trainer` in step with the approved relation. This site had its own simpler
# class that wrote the pointer with no relation, no consent and no charge.
from users.admin import CustomUserAdmin as UsersCustomUserAdmin

admin_site.register(CustomUser, UsersCustomUserAdmin)
# Read-only: this row carries consent, the agreed price and the escrow reference.
# Registered bare, any staff account could set status='approved' with no client
# consent and no money held, or point `hold_reference` at nothing.
@admin.register(TrainerClientRelation, site=admin_site)
class TrainerClientRelationAdmin(admin.ModelAdmin):
    list_display = ('trainer', 'client', 'status', 'initiated_by', 'agreed_rate',
                    'hold_reference', 'created_at')
    list_filter = ('status', 'initiated_by')
    search_fields = ('trainer__email', 'client__email')
    readonly_fields = ('trainer', 'client', 'status', 'initiated_by', 'agreed_rate',
                       'hold_reference', 'created_at', 'updated_at')

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False
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

admin_site.register(FoodItem, DietFoodItemAdmin)
admin_site.register(FoodCategory)
admin_site.register(UserFoodPreference)
# The recipe library: 105 dishes whose gram amounts a person has to check against a
# real plate, and the one screen where that job can be done.
admin_site.register(Recipe, DietRecipeAdmin)
admin_site.register(UserFoodCategoryPreference, DietFoodChoiceAdmin)
admin_site.register(DietPlan, DietPlanAdmin)
admin_site.register(Meal, MealAdmin)
admin_site.register(MealComponent)
admin_site.register(MealSwap)
admin_site.register(MealEvent)
admin_site.register(UserFoodWeight)
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

# What each build may show and sell, and the append-only record of every change to it.
from policy.admin import CapabilityChangeLogAdmin, ChannelCapabilitiesAdmin  # noqa: E402
from policy.models import CapabilityChangeLog, ChannelCapabilities  # noqa: E402
admin_site.register(ChannelCapabilities, ChannelCapabilitiesAdmin)
admin_site.register(CapabilityChangeLog, CapabilityChangeLogAdmin)

# Media kit: every picture the app draws, by slot. `Media slots > kits/` lists each
# trainer's completeness.
from mediakit.admin import MediaAssetAdmin  # noqa: E402
from mediakit.models import MediaAsset  # noqa: E402
admin_site.register(MediaAsset, MediaAssetAdmin)

# Safety: the reports queue (filter by status and reason, open the target, dismiss or
# record that action was taken), plus read-only views of who has blocked whom and who
# has allowed the AI coach to read their data. Operator-only — see the permission
# narrowing in moderation/admin.py, which is what keeps a bare `is_staff` account out.
from moderation.admin import AIConsentAdmin, BlockAdmin, ReportAdmin  # noqa: E402
from moderation.models import AIConsent, Block, Report  # noqa: E402
admin_site.register(Report, ReportAdmin)
admin_site.register(Block, BlockAdmin)
admin_site.register(AIConsent, AIConsentAdmin)

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
