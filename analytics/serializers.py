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
    """One row of the activity log.

    `workout` (BACKEND_BUGS B-072) is set on a `routine_completed` row and null on every
    other: `{session_id, routine_id, day, duration_minutes, volume_kg, sets}`, read from
    the session when the row is shown (`analytics/feed.py`), so rows written before it
    existed carry it too. It is null as well when that session has been deleted. `day` is
    the routine day (null when the session named none), `duration_minutes` null while
    the session has no end. A page costs one extra query, whatever it holds.

    `metadata` of a `meal_completed` row written since 2026-09-24 holds `meal_id`,
    `name`, `name_ar`, `meal_type`, `date` and `calories` (kcal eaten, one decimal) —
    one row per meal (B-071). Older rows are one per FOOD and hold `component_id` and
    `food_id` instead; they are kept as they were written.
    """

    workout = serializers.SerializerMethodField()

    class Meta:
        model = UserActivity
        fields = [
            'id', 'user', 'activity_type', 'timestamp',
            'content_type', 'object_id', 'metadata',
            'session_id', 'ip_address', 'user_agent', 'workout',
        ]
        read_only_fields = [
            'id', 'user', 'timestamp', 'ip_address', 'user_agent'
        ]

    def get_workout(self, obj):
        from .activity_types import ROUTINE_COMPLETED
        from .feed import workout_details, workout_for

        if obj.activity_type != ROUTINE_COMPLETED:
            return None
        cache = self.context.setdefault('_workouts', {'asked': set(), 'details': {}}) \
            if isinstance(self.context, dict) else {'asked': set(), 'details': {}}
        if obj.pk not in cache['asked']:
            # The first workout row of a page asks for every workout row on it at once.
            page = getattr(self.root, 'instance', None)
            rows = [obj]
            if isinstance(page, (list, tuple)) or getattr(page, '_result_cache', None):
                rows += [row for row in page
                         if getattr(row, 'activity_type', None) == ROUTINE_COMPLETED]
            cache['details'].update(workout_details(rows))
            cache['asked'].update(row.pk for row in rows)
        return workout_for(obj, cache['details'])

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
    """A goal, and how far along it is.

    **Where it starts** (BACKEND_BUGS B-066, B-069). `start_value` may be sent on
    create: the goal is then stored AT its start in the same write (`current_value =
    start_value`, `progress_percentage` 0). Without it, a finite number in
    `metadata.start_value` (where the app kept the start before this field existed) is
    taken instead. A start that already reaches the target — equal to it, or for a
    `weight_loss` goal at or below it — is 400 `validation_error` with the field code
    `start_reaches_target` on `start_value` (a `metadata.start_value` like that is
    ignored instead). After creation it is read-only: a PATCH that sends it changes
    nothing.
    Progress is the share of the way from the start to the target in either direction —
    `update_progress` on the view says how, and when a goal completes.

    **`workout_frequency` is counted, not typed** (B-070): `current_value` is the number
    of workouts (`WorkoutSession`s `completed`) begun THIS week, Saturday to Friday,
    `progress_percentage` is that against `target_value` capped at 100, and `week_start`
    / `week_end` (dates) say which week. It never becomes `completed`; next week starts
    from 0. `week_start` and `week_end` are null on every other type. The rule is in
    `analytics/goals.py`.

    **And its target is calculated** (the owner's decision 5, 2026-09-24):
    `target_value` is what the person's plan asks for THIS week — their program's
    training days this week, else the training days a week on their profile — and
    `target_source` says which: `"program"`, `"profile"`, or `"saved"` (neither is
    available now; the target it was last calculated at). `target_source` is null on
    every other type, whose target the person sets. On create `target_value` may be
    left out for a `workout_frequency` goal (it is filled in); one that differs from the
    calculated number is refused — 400 `goal_target_from_plan` with `expected_target`
    (a number) and `target_source` — and with no program and no stated training days
    the goal cannot be created: 400 `goal_target_unavailable`. A PATCH that sends a
    different `target_value` is refused the same way. A list costs four extra queries
    however many rows it holds (the count, and the targets: routines, their days,
    profiles).
    """

    days_remaining = serializers.SerializerMethodField()
    # Same reason as PerformanceMetric.value: `update_progress` writes these and a
    # non-finite value poisons every later read of the goal list.
    #: Not required at the FIELD: a `workout_frequency` goal takes it from the plan
    #: (decision 5). `validate` still requires it for every other type, with the same
    #: `required` field error as before.
    target_value = FiniteFloatField(required=False)
    start_value = FiniteFloatField(required=False, allow_null=True)
    week_start = serializers.SerializerMethodField()
    week_end = serializers.SerializerMethodField()
    target_source = serializers.SerializerMethodField()

    class Meta:
        model = UserGoal
        fields = [
            'id', 'user', 'goal_type', 'title', 'description',
            'target_value', 'start_value', 'current_value', 'unit', 'created_at',
            'target_date', 'completed_at', 'status', 'progress_percentage',
            'metadata', 'days_remaining', 'week_start', 'week_end', 'target_source',
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

    def validate(self, attrs):
        """The start is fixed at creation, and stored with the goal in the one write.

        A `workout_frequency` goal's target is the system's (decision 5): see
        `_calculated_target`.
        """
        from .goals import COUNTED_TYPES, start_from_metadata

        goal_type = attrs.get('goal_type', getattr(self.instance, 'goal_type', None))
        if goal_type in COUNTED_TYPES:
            attrs = self._calculated_target(attrs)
        elif (self.instance is None or not self.partial) and \
                attrs.get('target_value') is None:
            # A create, or a full PUT: the field was required before, and still is.
            raise serializers.ValidationError({'target_value': [serializers.ErrorDetail(
                self.fields['target_value'].error_messages['required'], code='required')]})

        if self.instance is not None:
            # Read-only after creation: moving the start would silently rescale every
            # reading already shown against it.
            attrs.pop('start_value', None)
            return attrs
        target = attrs.get('target_value')
        start = attrs.get('start_value')
        # The model's own rule decides what "already there" means for this goal type.
        probe = UserGoal(goal_type=attrs.get('goal_type'), target_value=target)
        if start is None:
            start = start_from_metadata(attrs.get('metadata'), target)
            if start is not None and target is not None:
                probe.start_value = start
                if probe.reached(start):
                    start = None        # the app's note, not a request: ignored
        elif target is not None:
            probe.start_value = start
            if probe.reached(start):
                raise serializers.ValidationError({'start_value': [serializers.ErrorDetail(
                    _("The goal would be reached at its start: the start must be "
                      "short of the target."),
                    code='start_reaches_target')]})
        attrs['start_value'] = start
        if start is not None:
            # `current_value` is read-only to the client; the server sets it here so
            # that a new goal is AT its start, not at 0, without a second request.
            attrs['current_value'] = start
            attrs['progress_percentage'] = 0.0
        return attrs

    def _calculated_target(self, attrs):
        """The target of a `workout_frequency` goal is what the plan asks for this week.

        Raises `GoalTargetRefused` (answered by `UserGoalViewSet`) for a sent target
        that differs — `goal_target_from_plan`, with the number expected — and, when
        there is no program and no stated training days, for a new goal or a sent
        target — `goal_target_unavailable`. Otherwise the calculated number is stored.
        """
        from .goals import GoalTargetRefused, weekly_target

        request = self.context.get('request')
        owner_id = (self.instance.user_id if self.instance is not None
                    else getattr(getattr(request, 'user', None), 'pk', None))
        target, source = weekly_target(owner_id)
        sent = attrs.get('target_value')
        if target is None:
            if self.instance is None or sent is not None:
                raise GoalTargetRefused(
                    'goal_target_unavailable',
                    str(_('A weekly workout goal follows your plan. Set how many days a '
                          'week you train in your profile, and the goal will follow it.')))
            attrs.pop('target_value', None)            # the saved target stands
            return attrs
        if sent is not None and float(sent) != float(target):
            raise GoalTargetRefused(
                'goal_target_from_plan',
                str(_('A weekly workout goal follows your plan: %(target)s workouts this '
                      'week.') % {'target': target}),
                expected_target=target, target_source=source)
        attrs['target_value'] = float(target)
        return attrs

    def _weekly_figures(self, goal):
        """(this week's workouts, target, source) for the goal's owner. The same queries
        for a whole page: the first row asks for every owner of a counted goal on the
        page at once."""
        from .goals import COUNTED_TYPES, weekly_targets, workouts_this_week

        cache = self.context.setdefault('_weekly_goal_figures', {}) \
            if isinstance(self.context, dict) else {}
        if goal.user_id not in cache:
            owners = {goal.user_id}
            page = getattr(self.root, 'instance', None)
            if isinstance(page, (list, tuple)) or getattr(page, '_result_cache', None):
                owners |= {row.user_id for row in page
                           if getattr(row, 'goal_type', None) in COUNTED_TYPES}
            owners -= set(cache)
            counts = workouts_this_week(owners)
            targets = weekly_targets(owners)
            for owner in owners:
                cache[owner] = (counts.get(owner, 0),) + targets.get(owner, (None, None))
        return cache[goal.user_id]

    def to_representation(self, instance):
        data = super().to_representation(instance)
        from .goals import COUNTED_TYPES, FROM_SAVED, weekly_progress

        if instance.goal_type in COUNTED_TYPES:
            count, target, source = self._weekly_figures(instance)
            if target is None:
                target, source = instance.target_value, FROM_SAVED
            data['target_value'] = float(target)
            data['target_source'] = source
            data['current_value'] = float(count)
            data['progress_percentage'] = weekly_progress(count, target)
        return data

    def get_target_source(self, obj):
        # Filled for a counted goal by `to_representation`; the person's own otherwise.
        return None

    def _week(self, obj, index):
        from .goals import COUNTED_TYPES, current_week

        if obj.goal_type not in COUNTED_TYPES:
            return None
        return current_week()[index].isoformat()

    def get_week_start(self, obj):
        return self._week(obj, 0)

    def get_week_end(self, obj):
        return self._week(obj, 1)

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