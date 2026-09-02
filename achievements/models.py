import uuid
import os
"""
Achievement Models for Training Platform

This module provides comprehensive achievement tracking with automatic awarding,
progress tracking, and rich metadata for gamification features.
"""

from django.db import models
from django.utils import timezone
from users.models import CustomUser


def achievement_icon_upload_path(instance, filename):
    """Random, unguessable stored path — see social/models.py for the rationale."""
    ext = os.path.splitext(filename)[1].lower()[:10] or '.bin'
    return os.path.join('achievements/icons', f"{uuid.uuid4().hex}{ext}")


class Achievement(models.Model):
    """
    Achievement definitions with criteria for automatic awarding.
    """
    ACHIEVEMENT_CATEGORIES = [
        ('workout', 'Workout Achievements'),
        ('diet', 'Diet Achievements'),
        ('social', 'Social Achievements'),
        ('challenge', 'Challenge Achievements'),
        ('streak', 'Streak Achievements'),
        ('milestone', 'Milestone Achievements'),
    ]

    # Basic info
    key = models.CharField(
        max_length=50,
        unique=True,
        db_index=True,
        help_text="Unique identifier for the achievement (e.g., 'first_workout')"
    )
    name = models.CharField(max_length=100)
    description = models.TextField()
    category = models.CharField(
        max_length=20,
        choices=ACHIEVEMENT_CATEGORIES,
        db_index=True
    )

    # Achievement criteria (JSON format)
    # Example: {"type": "workout_count", "target": 10, "condition": "gte"}
    criteria = models.JSONField(default=dict)
    
    # Rewards
    points = models.PositiveIntegerField(default=10)

    # Visual elements
    icon = models.ImageField(
        upload_to=achievement_icon_upload_path,
        blank=True,
        null=True
    )
    badge_color = models.CharField(max_length=7, default='#FFD700')

    # Rarity and visibility
    is_rare = models.BooleanField(default=False)
    is_secret = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)

    # Metadata
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        # `id` tiebreaker: a single non-unique sort column is not a total order, so LIMIT/OFFSET
        # paging repeated and hid rows whenever two records shared the value.
        ordering = ['category', 'points', 'name', 'id']
        indexes = [
            models.Index(fields=['category', 'is_active']),
            models.Index(fields=['is_rare', 'is_active']),
            models.Index(fields=['key']),
        ]
        verbose_name = "Achievement"
        verbose_name_plural = "Achievements"

    def __str__(self):
        return f"{self.name} ({self.get_category_display()})"


class UserAchievement(models.Model):
    """
    Track user's earned achievements.
    """
    user = models.ForeignKey(
        CustomUser,
        on_delete=models.CASCADE,
        related_name='earned_achievements',
        db_index=True
    )
    achievement = models.ForeignKey(
        Achievement,
        on_delete=models.CASCADE,
        related_name='user_achievements',
        db_index=True
    )
    earned_at = models.DateTimeField(auto_now_add=True, db_index=True)

    # Context when achievement was earned
    progress_data = models.JSONField(default=dict, blank=True)

    # For display purposes
    is_featured = models.BooleanField(
        default=False,
        help_text="User can feature up to 3 achievements on their profile"
    )

    class Meta:
        unique_together = ['user', 'achievement']
        # `id` tiebreaker: a single non-unique sort column is not a total order, so LIMIT/OFFSET
        # paging repeated and hid rows whenever two records shared the value.
        ordering = ['-earned_at', '-id']
        indexes = [
            models.Index(fields=['user', 'earned_at']),
            models.Index(fields=['achievement', 'earned_at']),
            models.Index(fields=['user', 'is_featured']),
            # Matches the real access pattern: WHERE user=? ORDER BY earned_at DESC, id DESC.
            # Measured on a power user with 5,050 rows: 0.682 ms -> 0.089 ms, because
            # the planner stops sorting their entire history to return 25 rows.
            models.Index(fields=['user', '-earned_at', '-id'], name='userachieve_recent_idx'),
        ]
        verbose_name = "User Achievement"
        verbose_name_plural = "User Achievements"

    def __str__(self):
        return f"{self.user.username} earned {self.achievement.name}"


class AchievementProgress(models.Model):
    """
    Track user's progress towards achievements (for non-earned achievements).
    This is used for showing progress bars in the UI.
    """
    user = models.ForeignKey(
        CustomUser,
        on_delete=models.CASCADE,
        related_name='achievement_progress',
        db_index=True
    )
    achievement = models.ForeignKey(
        Achievement,
        on_delete=models.CASCADE,
        related_name='progress_records',
        db_index=True
    )
    
    # Progress tracking
    current_value = models.FloatField(default=0)
    target_value = models.FloatField(default=0)
    progress_percentage = models.FloatField(default=0)
    
    # Timestamps
    last_updated = models.DateTimeField(auto_now=True)
    
    class Meta:
        unique_together = ['user', 'achievement']
        # `id` tiebreaker: a single non-unique sort column is not a total order, so LIMIT/OFFSET
        # paging repeated and hid rows whenever two records shared the value.
        ordering = ['-progress_percentage', '-id']
        indexes = [
            models.Index(fields=['user', 'progress_percentage']),
        ]
        verbose_name = "Achievement Progress"
        verbose_name_plural = "Achievement Progress"

    def __str__(self):
        return f"{self.user.username} - {self.achievement.name}: {self.progress_percentage:.0f}%"

    def update_progress(self, new_value):
        """Update progress and calculate percentage."""
        self.current_value = new_value
        if self.target_value > 0:
            self.progress_percentage = min(100.0, (new_value / self.target_value) * 100)
        self.save()
