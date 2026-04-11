"""
Achievement Admin - Admin interface for managing achievements.
"""

from django.contrib import admin
from modeltranslation.admin import TranslationAdmin
from .models import Achievement, UserAchievement, AchievementProgress


@admin.register(Achievement)
class AchievementAdmin(TranslationAdmin):
    """Admin interface for Achievement model."""
    
    list_display = [
        'name', 'key', 'category', 'points', 
        'is_rare', 'is_secret', 'is_active', 'users_earned'
    ]
    list_filter = ['category', 'is_rare', 'is_secret', 'is_active']
    search_fields = ['name', 'key', 'description']
    ordering = ['category', '-points', 'name']
    readonly_fields = ['created_at', 'updated_at']
    
    fieldsets = (
        ('Basic Info', {
            'fields': ('key', 'name', 'description', 'category')
        }),
        ('Criteria', {
            'fields': ('criteria', 'points'),
            'description': 'JSON format: {"type": "workout_count", "target": 10, "condition": "gte"}'
        }),
        ('Visual', {
            'fields': ('icon', 'badge_color')
        }),
        ('Flags', {
            'fields': ('is_rare', 'is_secret', 'is_active')
        }),
        ('Metadata', {
            'fields': ('created_at', 'updated_at'),
            'classes': ('collapse',)
        }),
    )
    
    def users_earned(self, obj):
        """Count of users who earned this achievement."""
        return obj.user_achievements.count()
    users_earned.short_description = 'Users Earned'


@admin.register(UserAchievement)
class UserAchievementAdmin(admin.ModelAdmin):
    """Admin interface for UserAchievement model."""
    
    list_display = [
        'user', 'achievement', 'earned_at', 'is_featured', 'points'
    ]
    list_filter = ['achievement__category', 'is_featured', 'earned_at']
    search_fields = ['user__username', 'user__email', 'achievement__name']
    ordering = ['-earned_at']
    raw_id_fields = ['user', 'achievement']
    readonly_fields = ['earned_at', 'progress_data']
    
    def points(self, obj):
        return obj.achievement.points
    points.short_description = 'Points'
    
    actions = ['award_to_all_users']
    
    def award_to_all_users(self, request, queryset):
        """Bulk award selected achievements to all active users."""
        from users.models import CustomUser
        from django.contrib import messages
        
        achievements = queryset.values_list('achievement', flat=True).distinct()
        
        if not achievements:
            messages.warning(request, 'No achievements selected.')
            return
        
        # This is a placeholder - implement as needed
        messages.info(request, f'Bulk award not yet implemented.')
    award_to_all_users.short_description = 'Award to all users'


@admin.register(AchievementProgress)
class AchievementProgressAdmin(admin.ModelAdmin):
    """Admin interface for AchievementProgress model."""
    
    list_display = [
        'user', 'achievement', 'current_value', 
        'target_value', 'progress_percentage', 'last_updated'
    ]
    list_filter = ['achievement__category', 'last_updated']
    search_fields = ['user__username', 'achievement__name']
    ordering = ['-progress_percentage']
    raw_id_fields = ['user', 'achievement']
    readonly_fields = ['last_updated']
