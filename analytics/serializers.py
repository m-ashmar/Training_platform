"""
Analytics Serializers

Serializers for analytics models to handle API data conversion.
"""

from rest_framework import serializers

import json

from django.utils.translation import gettext_lazy as _

from training_platform.numbers import FiniteFloatField
from .models import (
    UserActivity, PerformanceMetric, UserSession,
    UserGoal, AnalyticsDashboard
)


#: The most a `metadata` blob may hold. It is an unvalidated JSONField on four tables that
#: a client writes, with no throttle, on a 512 MB box: a single 200 KB blob was a 201.
MAX_METADATA_BYTES = 8 * 1024


def _bounded_metadata(value):
    if value in (None, ""):
        return value
    try:
        size = len(json.dumps(value, default=str))
    except (TypeError, ValueError):
        raise serializers.ValidationError(_("metadata must be JSON."))
    if size > MAX_METADATA_BYTES:
        raise serializers.ValidationError(
            _("metadata is too large (%(size)d bytes; the limit is %(limit)d).")
            % {"size": size, "limit": MAX_METADATA_BYTES})
    return value


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

    def validate_activity_type(self, value):
        from analytics.activity_types import CLIENT_LOGGABLE

        if value not in CLIENT_LOGGABLE:
            raise serializers.ValidationError(
                _("%(value)s is recorded by the server and cannot be logged by the app.")
                % {"value": value})
        return value

    def validate_metadata(self, value):
        return _bounded_metadata(value)


class PerformanceMetricSerializer(serializers.ModelSerializer):
    """Serializer for performance metrics"""

    #: A real number. DRF's FloatField accepts "NaN", "Infinity" and "1e400", Postgres
    #: stores them, and the row COMMITS before the response is rendered — so the write
    #: succeeded and only the reply failed, and every later read of that row was a 500.
    #: Three POSTs from one client took the metrics endpoints down for every staff
    #: account too, because the staff queryset is unscoped. See training_platform/numbers.
    value = FiniteFloatField()

    class Meta:
        model = PerformanceMetric
        fields = [
            'id', 'user', 'metric_type', 'value', 'unit',
            'recorded_at', 'notes', 'metadata'
        ]
        read_only_fields = ['id', 'user', 'recorded_at']

    #: The range a real measurement of each kind can fall in. `value` was any float, and
    #: weight-loss achievements read these rows. The bounds only refuse the impossible —
    #: a 3,000 kg person, a body-fat of 900% — not the unusual.
    BOUNDS = {
        "weight": (20.0, 400.0),
        "body_fat": (1.0, 75.0),
        "muscle_mass": (5.0, 250.0),
        "workout_duration": (0.0, 1440.0),
        "calories_burned": (0.0, 20000.0),
        "sets_completed": (0.0, 1000.0),
        "reps_completed": (0.0, 20000.0),
        "distance_run": (0.0, 500.0),
        "diet_adherence": (0.0, 100.0),
        "meal_completion": (0.0, 100.0),
        "goal_achievement": (0.0, 100.0),
        "streak_count": (0.0, 10000.0),
    }

    def validate(self, attrs):
        metric = attrs.get("metric_type", getattr(self.instance, "metric_type", None))
        value = attrs.get("value", getattr(self.instance, "value", None))
        low, high = self.BOUNDS.get(metric, (0.0, 1e6))
        if value is not None and not (low <= value <= high):
            raise serializers.ValidationError({
                "value": [_("%(metric)s must be between %(low)s and %(high)s.")
                          % {"metric": metric, "low": low, "high": high}]})
        return attrs

    def validate_metadata(self, value):
        return _bounded_metadata(value)


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
        #: `ended_at` and `duration_seconds` are computed by `end_session()` from the
        #: server's own clock. Writable, a client posted a 30-day session and a billion
        #: page views, and the owner's engagement numbers were whatever it typed.
        read_only_fields = ['id', 'user', 'started_at', 'ended_at', 'duration_seconds']

    def _counter(self, value, name):
        if value is not None and not (0 <= value <= 100000):
            raise serializers.ValidationError(
                _("%(name)s must be between 0 and 100000.") % {"name": name})
        return value

    def validate_page_views(self, value):
        return self._counter(value, "page_views")

    def validate_api_calls(self, value):
        return self._counter(value, "api_calls")
    
    def get_duration_minutes(self, obj):
        """Convert duration to minutes"""
        if obj.duration_seconds:
            return round(obj.duration_seconds / 60, 2)
        return None


class UserGoalSerializer(serializers.ModelSerializer):
    """Serializer for user goals"""
    
    days_remaining = serializers.SerializerMethodField()
    # Same reason as PerformanceMetric.value: `update_progress` writes these and a
    # non-finite value poisons every later read of the goal list.
    target_value = FiniteFloatField()
    
    class Meta:
        model = UserGoal
        fields = [
            'id', 'user', 'goal_type', 'title', 'description',
            'target_value', 'current_value', 'unit', 'created_at',
            'target_date', 'completed_at', 'status', 'progress_percentage',
            'metadata', 'days_remaining'
        ]
        #: `current_value` is read-only here: progress moves through `update_progress`,
        #: which keeps `progress_percentage` and completion in step. PATCHing
        #: `current_value` directly left "3 kg of 10" displayed as 100%.
        read_only_fields = [
            'id', 'user', 'created_at', 'completed_at', 'progress_percentage',
            'current_value',
        ]

    def validate_target_value(self, value):
        # A zero or negative target was accepted and then silently never moved the bar.
        if value is None or value <= 0:
            raise serializers.ValidationError(_("target_value must be greater than zero."))
        return value

    def validate_status(self, value):
        # A client may pause or abandon a goal, and resume it. It may not declare it
        # COMPLETED — that fires the goal achievement check — and it may not reopen one
        # that is: completion is a fact `update_progress` establishes.
        current = getattr(self.instance, "status", None)
        if value == "completed" and current != "completed":
            raise serializers.ValidationError(
                _("A goal is completed by reaching its target, not by setting its status."))
        if current == "completed" and value != "completed":
            raise serializers.ValidationError(_("A completed goal cannot be reopened."))
        return value

    def validate_metadata(self, value):
        return _bounded_metadata(value)
    
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
        #: `user` is READ-ONLY. It was writable and `AnalyticsDashboardViewSet` is the
        #: one viewset in this module with no `perform_create` — the other four all pin
        #: `user=self.request.user` — so any authenticated client could POST a dashboard
        #: row owned by somebody else, with a `data` blob of their choosing. `computed_at`
        #: is `auto_now` and `Meta.ordering` is `['-computed_at']`, so the planted row
        #: sorted FIRST on the victim's own progress screen.
        read_only_fields = ['id', 'user', 'computed_at'] 