"""
Analytics Serializers

Serializers for analytics models to handle API data conversion.
"""

from rest_framework import serializers
from .models import (
    UserActivity, PerformanceMetric, UserSession,
    UserGoal, AnalyticsDashboard
)


class UserActivitySerializer(serializers.ModelSerializer):
    """Serializer for user activity tracking"""
    
    class Meta:
        model = UserActivity
        fields = [
            'id', 'user', 'activity_type', 'timestamp',
            'content_type', 'object_id', 'metadata',
            'session_id', 'ip_address', 'user_agent'
        ]
        read_only_fields = [
            'id', 'user', 'timestamp', 'ip_address', 'user_agent'
        ]


class PerformanceMetricSerializer(serializers.ModelSerializer):
    """Serializer for performance metrics"""
    
    class Meta:
        model = PerformanceMetric
        fields = [
            'id', 'user', 'metric_type', 'value', 'unit',
            'recorded_at', 'notes', 'metadata'
        ]
        read_only_fields = ['id', 'user', 'recorded_at']


class UserSessionSerializer(serializers.ModelSerializer):
    """Serializer for user sessions"""
    
    duration_minutes = serializers.SerializerMethodField()
    is_active = serializers.ReadOnlyField()
    
    class Meta:
        model = UserSession
        fields = [
            'id', 'user', 'session_id', 'started_at', 'ended_at',
            'ip_address', 'user_agent', 'device_type', 'browser', 'os',
            'page_views', 'api_calls', 'duration_seconds', 'duration_minutes',
            'country', 'region', 'city', 'is_active'
        ]
        read_only_fields = ['id', 'user', 'started_at']
    
    def get_duration_minutes(self, obj):
        """Convert duration to minutes"""
        if obj.duration_seconds:
            return round(obj.duration_seconds / 60, 2)
        return None


class UserGoalSerializer(serializers.ModelSerializer):
    """Serializer for user goals"""
    
    days_remaining = serializers.SerializerMethodField()
    
    class Meta:
        model = UserGoal
        fields = [
            'id', 'user', 'goal_type', 'title', 'description',
            'target_value', 'current_value', 'unit', 'created_at',
            'target_date', 'completed_at', 'status', 'progress_percentage',
            'metadata', 'days_remaining'
        ]
        read_only_fields = [
            'id', 'user', 'created_at', 'completed_at', 'progress_percentage'
        ]
    
    def get_days_remaining(self, obj):
        """Calculate days remaining to target date"""
        if obj.target_date and obj.status == 'active':
            from django.utils import timezone
            today = timezone.localdate()
            delta = obj.target_date - today
            return delta.days if delta.days > 0 else 0
        return None


class AnalyticsDashboardSerializer(serializers.ModelSerializer):
    """Serializer for analytics dashboard data"""
    
    class Meta:
        model = AnalyticsDashboard
        fields = [
            'id', 'dashboard_type', 'user', 'data', 'computed_at',
            'period_start', 'period_end'
        ]
        read_only_fields = ['id', 'computed_at'] 