"""
Analytics Models for Training Platform

This module provides comprehensive analytics tracking for user behavior,
performance metrics, and platform insights.
"""

from django.db import models
from django.contrib.contenttypes.models import ContentType
from django.contrib.contenttypes.fields import GenericForeignKey
from django.utils import timezone
from users.models import CustomUser


class UserActivity(models.Model):
    """
    Track all user activities across the platform
    """
    ACTIVITY_TYPES = [
        ('login', 'User Login'),
        ('logout', 'User Logout'),
        ('profile_update', 'Profile Update'),
        ('routine_created', 'Routine Created'),
        ('routine_completed', 'Routine Completed'),
        ('diet_plan_generated', 'Diet Plan Generated'),
        ('meal_completed', 'Meal Completed'),
        ('exercise_completed', 'Exercise Completed'),
        ('subscription_created', 'Subscription Created'),
        ('payment_completed', 'Payment Completed'),
        ('file_uploaded', 'File Uploaded'),
        ('api_request', 'API Request'),
        ('page_view', 'Page View'),
        ('feature_used', 'Feature Used'),
        ('error_occurred', 'Error Occurred'),
    ]
    
    user = models.ForeignKey(
        CustomUser, 
        on_delete=models.CASCADE, 
        related_name='activities',
        db_index=True
    )
    activity_type = models.CharField(
        max_length=50, 
        choices=ACTIVITY_TYPES,
        db_index=True
    )
    timestamp = models.DateTimeField(auto_now_add=True, db_index=True)
    
    # Generic relation to track activity on any model
    content_type = models.ForeignKey(
        ContentType, 
        on_delete=models.CASCADE, 
        null=True, 
        blank=True
    )
    object_id = models.PositiveIntegerField(null=True, blank=True)
    content_object = GenericForeignKey('content_type', 'object_id')
    
    # Additional context data
    metadata = models.JSONField(default=dict, blank=True)
    session_id = models.CharField(max_length=100, blank=True, db_index=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.TextField(blank=True)
    
    class Meta:
        ordering = ['-timestamp']
        indexes = [
            models.Index(fields=['user', 'activity_type', 'timestamp']),
            models.Index(fields=['activity_type', 'timestamp']),
            models.Index(fields=['session_id', 'timestamp']),
        ]
        verbose_name = "User Activity"
        verbose_name_plural = "User Activities"
    
    def __str__(self):
        return f"{self.user.username} - {self.get_activity_type_display()} at {self.timestamp}"


class PerformanceMetric(models.Model):
    """
    Track user performance metrics over time
    """
    METRIC_TYPES = [
        ('weight', 'Weight'),
        ('body_fat', 'Body Fat Percentage'),
        ('muscle_mass', 'Muscle Mass'),
        ('workout_duration', 'Workout Duration'),
        ('calories_burned', 'Calories Burned'),
        ('sets_completed', 'Sets Completed'),
        ('reps_completed', 'Reps Completed'),
        ('distance_run', 'Distance Run'),
        ('diet_adherence', 'Diet Adherence'),
        ('meal_completion', 'Meal Completion Rate'),
        ('goal_achievement', 'Goal Achievement'),
        ('streak_count', 'Streak Count'),
    ]
    
    user = models.ForeignKey(
        CustomUser, 
        on_delete=models.CASCADE, 
        related_name='performance_metrics',
        db_index=True
    )
    metric_type = models.CharField(
        max_length=50, 
        choices=METRIC_TYPES,
        db_index=True
    )
    value = models.FloatField()
    unit = models.CharField(max_length=20, blank=True)
    recorded_at = models.DateTimeField(auto_now_add=True, db_index=True)
    
    # Optional relation to specific activities
    related_activity = models.ForeignKey(
        UserActivity, 
        on_delete=models.SET_NULL, 
        null=True, 
        blank=True
    )
    
    # Additional context
    notes = models.TextField(blank=True)
    metadata = models.JSONField(default=dict, blank=True)
    
    class Meta:
        ordering = ['-recorded_at']
        indexes = [
            models.Index(fields=['user', 'metric_type', 'recorded_at']),
            models.Index(fields=['metric_type', 'recorded_at']),
        ]
        unique_together = ['user', 'metric_type', 'recorded_at']
        verbose_name = "Performance Metric"
        verbose_name_plural = "Performance Metrics"
    
    def __str__(self):
        return f"{self.user.username} - {self.metric_type}: {self.value} {self.unit}"


class PlatformMetric(models.Model):
    """
    Track platform-wide metrics and KPIs
    """
    METRIC_CATEGORIES = [
        ('users', 'User Metrics'),
        ('subscriptions', 'Subscription Metrics'),
        ('content', 'Content Metrics'),
        ('performance', 'Performance Metrics'),
        ('revenue', 'Revenue Metrics'),
        ('engagement', 'Engagement Metrics'),
    ]
    
    metric_name = models.CharField(max_length=100, db_index=True)
    category = models.CharField(
        max_length=50, 
        choices=METRIC_CATEGORIES,
        db_index=True
    )
    value = models.FloatField()
    unit = models.CharField(max_length=20, blank=True)
    recorded_at = models.DateTimeField(auto_now_add=True, db_index=True)
    
    # Time period this metric covers
    period_start = models.DateTimeField(null=True, blank=True)
    period_end = models.DateTimeField(null=True, blank=True)
    
    # Additional context
    metadata = models.JSONField(default=dict, blank=True)
    
    class Meta:
        ordering = ['-recorded_at']
        indexes = [
            models.Index(fields=['metric_name', 'recorded_at']),
            models.Index(fields=['category', 'recorded_at']),
        ]
        verbose_name = "Platform Metric"
        verbose_name_plural = "Platform Metrics"
    
    def __str__(self):
        return f"{self.metric_name}: {self.value} {self.unit} ({self.recorded_at})"


class UserSession(models.Model):
    """
    Track user sessions for analytics
    """
    user = models.ForeignKey(
        CustomUser, 
        on_delete=models.CASCADE, 
        related_name='sessions',
        null=True,  # Allow anonymous sessions
        blank=True,
        db_index=True
    )
    session_id = models.CharField(max_length=100, unique=True, db_index=True)
    started_at = models.DateTimeField(auto_now_add=True, db_index=True)
    ended_at = models.DateTimeField(null=True, blank=True, db_index=True)
    
    # Session context
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.TextField(blank=True)
    device_type = models.CharField(max_length=50, blank=True)
    browser = models.CharField(max_length=100, blank=True)
    os = models.CharField(max_length=100, blank=True)
    
    # Session metrics
    page_views = models.PositiveIntegerField(default=0)
    api_calls = models.PositiveIntegerField(default=0)
    duration_seconds = models.PositiveIntegerField(null=True, blank=True)
    
    # Geographic data (if available)
    country = models.CharField(max_length=100, blank=True)
    region = models.CharField(max_length=100, blank=True)
    city = models.CharField(max_length=100, blank=True)
    
    class Meta:
        ordering = ['-started_at']
        indexes = [
            models.Index(fields=['user', 'started_at']),
            models.Index(fields=['session_id']),
            models.Index(fields=['started_at', 'ended_at']),
        ]
        verbose_name = "User Session"
        verbose_name_plural = "User Sessions"
    
    def __str__(self):
        username = self.user.username if self.user else 'Anonymous'
        return f"{username} - Session {self.session_id[:8]}... ({self.started_at})"
    
    @property
    def is_active(self):
        """Check if session is currently active"""
        return self.ended_at is None
    
    def end_session(self):
        """End the session and calculate duration"""
        if not self.ended_at:
            self.ended_at = timezone.now()
            self.duration_seconds = int((self.ended_at - self.started_at).total_seconds())
            self.save()


class FeatureUsage(models.Model):
    """
    Track usage of specific platform features
    """
    feature_name = models.CharField(max_length=100, db_index=True)
    user = models.ForeignKey(
        CustomUser, 
        on_delete=models.CASCADE, 
        related_name='feature_usage',
        db_index=True
    )
    used_at = models.DateTimeField(auto_now_add=True, db_index=True)
    
    # Usage context
    session = models.ForeignKey(
        UserSession, 
        on_delete=models.SET_NULL, 
        null=True, 
        blank=True
    )
    metadata = models.JSONField(default=dict, blank=True)
    
    class Meta:
        ordering = ['-used_at']
        indexes = [
            models.Index(fields=['feature_name', 'used_at']),
            models.Index(fields=['user', 'feature_name', 'used_at']),
        ]
        verbose_name = "Feature Usage"
        verbose_name_plural = "Feature Usage"
    
    def __str__(self):
        return f"{self.user.username} used {self.feature_name} at {self.used_at}"


class ErrorLog(models.Model):
    """
    Track application errors for analysis
    """
    ERROR_LEVELS = [
        ('debug', 'Debug'),
        ('info', 'Info'),
        ('warning', 'Warning'),
        ('error', 'Error'),
        ('critical', 'Critical'),
    ]
    
    level = models.CharField(max_length=20, choices=ERROR_LEVELS, db_index=True)
    message = models.TextField()
    occurred_at = models.DateTimeField(auto_now_add=True, db_index=True)
    
    # User context (if available)
    user = models.ForeignKey(
        CustomUser, 
        on_delete=models.SET_NULL, 
        null=True, 
        blank=True,
        db_index=True
    )
    session = models.ForeignKey(
        UserSession, 
        on_delete=models.SET_NULL, 
        null=True, 
        blank=True
    )
    
    # Technical details
    exception_type = models.CharField(max_length=200, blank=True)
    traceback = models.TextField(blank=True)
    request_path = models.CharField(max_length=500, blank=True)
    request_method = models.CharField(max_length=10, blank=True)
    
    # Additional context
    metadata = models.JSONField(default=dict, blank=True)
    
    class Meta:
        ordering = ['-occurred_at']
        indexes = [
            models.Index(fields=['level', 'occurred_at']),
            models.Index(fields=['user', 'occurred_at']),
            models.Index(fields=['exception_type', 'occurred_at']),
        ]
        verbose_name = "Error Log"
        verbose_name_plural = "Error Logs"
    
    def __str__(self):
        return f"{self.level.upper()}: {self.message[:50]}... ({self.occurred_at})"


class UserGoal(models.Model):
    """
    Track user goals and achievement progress
    """
    GOAL_TYPES = [
        ('weight_loss', 'Weight Loss'),
        ('weight_gain', 'Weight Gain'),
        ('muscle_gain', 'Muscle Gain'),
        ('strength', 'Strength Improvement'),
        ('endurance', 'Endurance Improvement'),
        ('diet_adherence', 'Diet Adherence'),
        ('workout_frequency', 'Workout Frequency'),
        ('custom', 'Custom Goal'),
    ]
    
    STATUS_CHOICES = [
        ('active', 'Active'),
        ('completed', 'Completed'),
        ('paused', 'Paused'),
        ('abandoned', 'Abandoned'),
    ]
    
    user = models.ForeignKey(
        CustomUser, 
        on_delete=models.CASCADE, 
        related_name='goals',
        db_index=True
    )
    goal_type = models.CharField(max_length=50, choices=GOAL_TYPES, db_index=True)
    title = models.CharField(max_length=200)
    description = models.TextField(blank=True)
    
    # Goal parameters
    target_value = models.FloatField()
    current_value = models.FloatField(default=0)
    unit = models.CharField(max_length=20, blank=True)
    
    # Timeline
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    target_date = models.DateField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    
    # Status and progress
    status = models.CharField(
        max_length=20, 
        choices=STATUS_CHOICES, 
        default='active',
        db_index=True
    )
    progress_percentage = models.FloatField(default=0)
    
    # Additional context
    metadata = models.JSONField(default=dict, blank=True)
    
    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['user', 'status', 'created_at']),
            models.Index(fields=['goal_type', 'status']),
        ]
        verbose_name = "User Goal"
        verbose_name_plural = "User Goals"
    
    def __str__(self):
        return f"{self.user.username} - {self.title} ({self.progress_percentage}%)"
    
    def update_progress(self, new_value):
        """Update goal progress"""
        self.current_value = new_value
        if self.target_value > 0:
            self.progress_percentage = min(100, (new_value / self.target_value) * 100)
        
        # Check if goal is completed
        if self.progress_percentage >= 100 and self.status == 'active':
            self.status = 'completed'
            self.completed_at = timezone.now()
        
        self.save()


class AnalyticsDashboard(models.Model):
    """
    Pre-computed analytics for dashboard display
    """
    DASHBOARD_TYPES = [
        ('user', 'User Dashboard'),
        ('trainer', 'Trainer Dashboard'),
        ('admin', 'Admin Dashboard'),
        ('platform', 'Platform Overview'),
    ]
    
    dashboard_type = models.CharField(
        max_length=20, 
        choices=DASHBOARD_TYPES,
        db_index=True
    )
    user = models.ForeignKey(
        CustomUser, 
        on_delete=models.CASCADE, 
        null=True, 
        blank=True,
        db_index=True
    )
    
    # Dashboard data
    data = models.JSONField(default=dict)
    computed_at = models.DateTimeField(auto_now=True, db_index=True)
    
    # Time period this dashboard covers
    period_start = models.DateTimeField()
    period_end = models.DateTimeField()
    
    class Meta:
        ordering = ['-computed_at']
        indexes = [
            models.Index(fields=['dashboard_type', 'user', 'computed_at']),
            models.Index(fields=['user', 'computed_at']),
        ]
        unique_together = ['dashboard_type', 'user', 'period_start', 'period_end']
        verbose_name = "Analytics Dashboard"
        verbose_name_plural = "Analytics Dashboards"
    
    def __str__(self):
        user_info = f" for {self.user.username}" if self.user else ""
        return f"{self.get_dashboard_type_display()}{user_info} ({self.computed_at})" 