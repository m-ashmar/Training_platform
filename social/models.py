"""
Social Features Models for Training Platform

This module provides social networking capabilities including user following,
content sharing, community interactions, and social feeds.
"""

from django.db import models
from django.contrib.contenttypes.models import ContentType
from django.contrib.contenttypes.fields import GenericForeignKey
from django.utils import timezone
from users.models import CustomUser


class UserFollow(models.Model):
    """
    Track user following relationships
    """
    follower = models.ForeignKey(
        CustomUser,
        on_delete=models.CASCADE,
        related_name='following',
        db_index=True
    )
    following = models.ForeignKey(
        CustomUser,
        on_delete=models.CASCADE,
        related_name='followers',
        db_index=True
    )
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    
    # Follow preferences
    notify_activity = models.BooleanField(default=True)
    show_in_feed = models.BooleanField(default=True)
    
    class Meta:
        unique_together = ['follower', 'following']
        indexes = [
            models.Index(fields=['follower', 'created_at']),
            models.Index(fields=['following', 'created_at']),
        ]
        verbose_name = "User Follow"
        verbose_name_plural = "User Follows"
    
    def __str__(self):
        return f"{self.follower.username} follows {self.following.username}"
    
    def clean(self):
        """Prevent users from following themselves"""
        from django.core.exceptions import ValidationError
        if self.follower == self.following:
            raise ValidationError("Users cannot follow themselves")


class Post(models.Model):
    """
    User posts for social feed
    """
    POST_TYPES = [
        ('text', 'Text Post'),
        ('workout', 'Workout Share'),
        ('achievement', 'Achievement'),
        ('progress', 'Progress Update'),
        ('meal', 'Meal Share'),
        ('motivation', 'Motivation'),
        ('question', 'Question'),
        ('tip', 'Fitness Tip'),
    ]
    
    VISIBILITY_CHOICES = [
        ('public', 'Public'),
        ('followers', 'Followers Only'),
        ('private', 'Private'),
    ]
    
    author = models.ForeignKey(
        CustomUser,
        on_delete=models.CASCADE,
        related_name='posts',
        db_index=True
    )
    post_type = models.CharField(
        max_length=20,
        choices=POST_TYPES,
        default='text',
        db_index=True
    )
    title = models.CharField(max_length=200, blank=True)
    content = models.TextField()
    visibility = models.CharField(
        max_length=20,
        choices=VISIBILITY_CHOICES,
        default='public',
        db_index=True
    )
    
    # Media attachments
    image = models.ImageField(
        upload_to='posts/',
        blank=True,
        null=True
    )
    
    # Related content (generic foreign key)
    content_type = models.ForeignKey(
        ContentType,
        on_delete=models.CASCADE,
        null=True,
        blank=True
    )
    object_id = models.PositiveIntegerField(null=True, blank=True)
    related_object = GenericForeignKey('content_type', 'object_id')
    
    # Engagement metrics
    likes_count = models.PositiveIntegerField(default=0)
    comments_count = models.PositiveIntegerField(default=0)
    shares_count = models.PositiveIntegerField(default=0)
    views_count = models.PositiveIntegerField(default=0)
    
    # Timestamps
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    # Moderation
    is_flagged = models.BooleanField(default=False)
    is_hidden = models.BooleanField(default=False)
    
    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['author', 'visibility', 'created_at']),
            models.Index(fields=['post_type', 'visibility', 'created_at']),
            models.Index(fields=['visibility', 'created_at']),
        ]
        verbose_name = "Post"
        verbose_name_plural = "Posts"
    
    def __str__(self):
        return f"{self.author.username} - {self.get_post_type_display()}"
    
    def can_be_viewed_by(self, user):
        """Check if user can view this post"""
        if self.is_hidden:
            return self.author == user
        
        if self.visibility == 'public':
            return True
        elif self.visibility == 'private':
            return self.author == user
        elif self.visibility == 'followers':
            if self.author == user:
                return True
            return UserFollow.objects.filter(
                follower=user,
                following=self.author
            ).exists()
        
        return False


class PostLike(models.Model):
    """
    Track post likes
    """
    user = models.ForeignKey(
        CustomUser,
        on_delete=models.CASCADE,
        related_name='post_likes',
        db_index=True
    )
    post = models.ForeignKey(
        Post,
        on_delete=models.CASCADE,
        related_name='likes',
        db_index=True
    )
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    
    class Meta:
        unique_together = ['user', 'post']
        indexes = [
            models.Index(fields=['post', 'created_at']),
            models.Index(fields=['user', 'created_at']),
        ]
        verbose_name = "Post Like"
        verbose_name_plural = "Post Likes"
    
    def __str__(self):
        return f"{self.user.username} likes post by {self.post.author.username}"


class Comment(models.Model):
    """
    Comments on posts
    """
    author = models.ForeignKey(
        CustomUser,
        on_delete=models.CASCADE,
        related_name='comments',
        db_index=True
    )
    post = models.ForeignKey(
        Post,
        on_delete=models.CASCADE,
        related_name='comments',
        db_index=True
    )
    content = models.TextField()
    
    # Reply to another comment (nested comments)
    parent_comment = models.ForeignKey(
        'self',
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name='replies'
    )
    
    # Engagement
    likes_count = models.PositiveIntegerField(default=0)
    
    # Timestamps
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    # Moderation
    is_flagged = models.BooleanField(default=False)
    is_hidden = models.BooleanField(default=False)
    
    class Meta:
        ordering = ['created_at']
        indexes = [
            models.Index(fields=['post', 'created_at']),
            models.Index(fields=['author', 'created_at']),
            models.Index(fields=['parent_comment', 'created_at']),
        ]
        verbose_name = "Comment"
        verbose_name_plural = "Comments"
    
    def __str__(self):
        return f"Comment by {self.author.username} on {self.post.id}"


class CommentLike(models.Model):
    """
    Track comment likes
    """
    user = models.ForeignKey(
        CustomUser,
        on_delete=models.CASCADE,
        related_name='comment_likes',
        db_index=True
    )
    comment = models.ForeignKey(
        Comment,
        on_delete=models.CASCADE,
        related_name='likes',
        db_index=True
    )
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    
    class Meta:
        unique_together = ['user', 'comment']
        verbose_name = "Comment Like"
        verbose_name_plural = "Comment Likes"
    
    def __str__(self):
        return f"{self.user.username} likes comment by {self.comment.author.username}"


class Challenge(models.Model):
    """
    Community challenges
    """
    CHALLENGE_TYPES = [
        ('workout', 'Workout Challenge'),
        ('diet', 'Diet Challenge'),
        ('weight_loss', 'Weight Loss Challenge'),
        ('endurance', 'Endurance Challenge'),
        ('strength', 'Strength Challenge'),
        ('habit', 'Habit Challenge'),
        ('custom', 'Custom Challenge'),
    ]
    
    STATUS_CHOICES = [
        ('upcoming', 'Upcoming'),
        ('active', 'Active'),
        ('completed', 'Completed'),
        ('cancelled', 'Cancelled'),
    ]
    
    title = models.CharField(max_length=200)
    description = models.TextField()
    challenge_type = models.CharField(
        max_length=20,
        choices=CHALLENGE_TYPES,
        db_index=True
    )
    
    # Challenge details
    creator = models.ForeignKey(
        CustomUser,
        on_delete=models.CASCADE,
        related_name='created_challenges',
        db_index=True
    )
    start_date = models.DateTimeField(db_index=True)
    end_date = models.DateTimeField(db_index=True)
    status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        default='upcoming',
        db_index=True
    )
    
    # Challenge parameters
    target_value = models.FloatField(null=True, blank=True)
    unit = models.CharField(max_length=20, blank=True)
    rules = models.TextField(blank=True)
    
    # Participation
    max_participants = models.PositiveIntegerField(null=True, blank=True)
    participants_count = models.PositiveIntegerField(default=0)
    
    # Rewards
    reward_description = models.TextField(blank=True)
    
    # Media
    image = models.ImageField(
        upload_to='challenges/',
        blank=True,
        null=True
    )
    
    # Timestamps
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['status', 'start_date']),
            models.Index(fields=['challenge_type', 'status']),
            models.Index(fields=['creator', 'status']),
        ]
        verbose_name = "Challenge"
        verbose_name_plural = "Challenges"
    
    def __str__(self):
        return f"{self.title} ({self.get_status_display()})"
    
    @property
    def is_active(self):
        """Check if challenge is currently active"""
        now = timezone.now()
        return (self.status == 'active' and 
                self.start_date <= now <= self.end_date)


class ChallengeParticipation(models.Model):
    """
    Track user participation in challenges
    """
    STATUS_CHOICES = [
        ('joined', 'Joined'),
        ('active', 'Active'),
        ('completed', 'Completed'),
        ('dropped', 'Dropped Out'),
    ]
    
    user = models.ForeignKey(
        CustomUser,
        on_delete=models.CASCADE,
        related_name='challenge_participations',
        db_index=True
    )
    challenge = models.ForeignKey(
        Challenge,
        on_delete=models.CASCADE,
        related_name='participations',
        db_index=True
    )
    status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        default='joined',
        db_index=True
    )
    
    # Progress tracking
    current_value = models.FloatField(default=0)
    progress_percentage = models.FloatField(default=0)
    
    # Timestamps
    joined_at = models.DateTimeField(auto_now_add=True, db_index=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    
    # Ranking
    rank = models.PositiveIntegerField(null=True, blank=True)
    
    class Meta:
        unique_together = ['user', 'challenge']
        ordering = ['-joined_at']
        indexes = [
            models.Index(fields=['challenge', 'status', 'current_value']),
            models.Index(fields=['user', 'status']),
        ]
        verbose_name = "Challenge Participation"
        verbose_name_plural = "Challenge Participations"
    
    def __str__(self):
        return f"{self.user.username} in {self.challenge.title}"


class Leaderboard(models.Model):
    """
    Track leaderboards for various metrics
    """
    LEADERBOARD_TYPES = [
        ('workout_streaks', 'Workout Streaks'),
        ('calories_burned', 'Calories Burned'),
        ('weight_loss', 'Weight Loss'),
        ('muscle_gain', 'Muscle Gain'),
        ('challenge_wins', 'Challenge Wins'),
        ('posts_likes', 'Most Liked Posts'),
        ('followers', 'Most Followers'),
        ('achievements', 'Most Achievements'),
    ]
    
    PERIOD_TYPES = [
        ('daily', 'Daily'),
        ('weekly', 'Weekly'),
        ('monthly', 'Monthly'),
        ('yearly', 'Yearly'),
        ('all_time', 'All Time'),
    ]
    
    leaderboard_type = models.CharField(
        max_length=30,
        choices=LEADERBOARD_TYPES,
        db_index=True
    )
    period_type = models.CharField(
        max_length=20,
        choices=PERIOD_TYPES,
        db_index=True
    )
    
    # Time period
    period_start = models.DateTimeField(db_index=True)
    period_end = models.DateTimeField(db_index=True)
    
    # Leaderboard data (JSON with rankings)
    rankings = models.JSONField(default=list)
    
    # Metadata
    total_participants = models.PositiveIntegerField(default=0)
    last_updated = models.DateTimeField(auto_now=True, db_index=True)
    
    class Meta:
        unique_together = ['leaderboard_type', 'period_type', 'period_start']
        ordering = ['-period_start']
        indexes = [
            models.Index(fields=['leaderboard_type', 'period_type']),
            models.Index(fields=['period_start', 'period_end']),
        ]
        verbose_name = "Leaderboard"
        verbose_name_plural = "Leaderboards"
    
    def __str__(self):
        return f"{self.get_leaderboard_type_display()} - {self.get_period_type_display()}"


class Achievement(models.Model):
    """
    User achievements and badges
    """
    ACHIEVEMENT_CATEGORIES = [
        ('workout', 'Workout Achievements'),
        ('diet', 'Diet Achievements'),
        ('social', 'Social Achievements'),
        ('challenge', 'Challenge Achievements'),
        ('streak', 'Streak Achievements'),
        ('milestone', 'Milestone Achievements'),
    ]
    
    name = models.CharField(max_length=100, unique=True)
    description = models.TextField()
    category = models.CharField(
        max_length=20,
        choices=ACHIEVEMENT_CATEGORIES,
        db_index=True
    )
    
    # Achievement criteria
    criteria = models.JSONField(default=dict)
    points = models.PositiveIntegerField(default=10)
    
    # Media
    icon = models.ImageField(
        upload_to='achievements/',
        blank=True,
        null=True
    )
    badge_color = models.CharField(max_length=7, default='#FFD700')  # Gold default
    
    # Rarity
    is_rare = models.BooleanField(default=False)
    is_secret = models.BooleanField(default=False)
    
    # Status
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    
    class Meta:
        ordering = ['category', 'name']
        indexes = [
            models.Index(fields=['category', 'is_active']),
            models.Index(fields=['is_rare', 'is_active']),
        ]
        verbose_name = "Achievement"
        verbose_name_plural = "Achievements"
    
    def __str__(self):
        return f"{self.name} ({self.get_category_display()})"


class UserAchievement(models.Model):
    """
    Track user achievements
    """
    user = models.ForeignKey(
        CustomUser,
        on_delete=models.CASCADE,
        related_name='achievements',
        db_index=True
    )
    achievement = models.ForeignKey(
        Achievement,
        on_delete=models.CASCADE,
        related_name='user_achievements',
        db_index=True
    )
    earned_at = models.DateTimeField(auto_now_add=True, db_index=True)
    
    # Achievement context
    progress_data = models.JSONField(default=dict, blank=True)
    
    class Meta:
        unique_together = ['user', 'achievement']
        ordering = ['-earned_at']
        indexes = [
            models.Index(fields=['user', 'earned_at']),
            models.Index(fields=['achievement', 'earned_at']),
        ]
        verbose_name = "User Achievement"
        verbose_name_plural = "User Achievements"
    
    def __str__(self):
        return f"{self.user.username} earned {self.achievement.name}"


class Notification(models.Model):
    """
    Social notifications
    """
    NOTIFICATION_TYPES = [
        ('follow', 'New Follower'),
        ('like', 'Post Liked'),
        ('comment', 'New Comment'),
        ('mention', 'Mentioned in Post'),
        ('challenge_invite', 'Challenge Invitation'),
        ('achievement', 'Achievement Earned'),
        ('leaderboard', 'Leaderboard Update'),
        ('system', 'System Notification'),
    ]
    
    recipient = models.ForeignKey(
        CustomUser,
        on_delete=models.CASCADE,
        related_name='social_notifications',
        db_index=True
    )
    sender = models.ForeignKey(
        CustomUser,
        on_delete=models.CASCADE,
        related_name='sent_social_notifications',
        null=True,
        blank=True,
        db_index=True
    )
    
    notification_type = models.CharField(
        max_length=20,
        choices=NOTIFICATION_TYPES,
        db_index=True
    )
    title = models.CharField(max_length=200)
    message = models.TextField()
    
    # Related object (generic foreign key)
    content_type = models.ForeignKey(
        ContentType,
        on_delete=models.CASCADE,
        null=True,
        blank=True
    )
    object_id = models.PositiveIntegerField(null=True, blank=True)
    related_object = GenericForeignKey('content_type', 'object_id')
    
    # Status
    is_read = models.BooleanField(default=False, db_index=True)
    is_sent = models.BooleanField(default=False, db_index=True)
    
    # Timestamps
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    read_at = models.DateTimeField(null=True, blank=True)
    
    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['recipient', 'is_read', 'created_at']),
            models.Index(fields=['notification_type', 'created_at']),
            models.Index(fields=['sender', 'created_at']),
        ]
        verbose_name = "Notification"
        verbose_name_plural = "Notifications"
    
    def __str__(self):
        return f"Notification for {self.recipient.username}: {self.title}"
    
    def mark_as_read(self):
        """Mark notification as read"""
        if not self.is_read:
            self.is_read = True
            self.read_at = timezone.now()
            self.save() 