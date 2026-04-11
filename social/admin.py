"""
Social App Admin — Admin interface for social models with translation support.
"""

from django.contrib import admin
from modeltranslation.admin import TranslationAdmin
from .models import (
    UserFollow, Post, PostLike, Comment, CommentLike,
    Challenge, ChallengeParticipation, Achievement, UserAchievement,
)


@admin.register(Challenge)
class ChallengeAdmin(TranslationAdmin):
    """Admin for Challenge with multilingual title/description."""
    list_display = ['title', 'challenge_type', 'status', 'start_date', 'end_date', 'participants_count']
    list_filter = ['challenge_type', 'status']
    search_fields = ['title', 'description']
    readonly_fields = ['created_at']


@admin.register(Achievement)
class SocialAchievementAdmin(TranslationAdmin):
    """Admin for social Achievement with multilingual name/description."""
    list_display = ['name', 'category', 'points', 'is_rare', 'is_secret']
    list_filter = ['category', 'is_rare', 'is_secret']
    search_fields = ['name', 'description']


@admin.register(Post)
class PostAdmin(admin.ModelAdmin):
    list_display = ['author', 'post_type', 'title', 'created_at']
    list_filter = ['post_type', 'visibility']
    search_fields = ['title', 'content']
    readonly_fields = ['created_at', 'updated_at']


@admin.register(UserFollow)
class UserFollowAdmin(admin.ModelAdmin):
    list_display = ['follower', 'following', 'created_at']
    readonly_fields = ['created_at']


@admin.register(ChallengeParticipation)
class ChallengeParticipationAdmin(admin.ModelAdmin):
    list_display = ['user', 'challenge', 'current_value', 'progress_percentage']
    list_filter = ['challenge']


@admin.register(UserAchievement)
class UserAchievementAdmin(admin.ModelAdmin):
    list_display = ['user', 'achievement', 'earned_at']
    readonly_fields = ['earned_at']
