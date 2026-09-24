"""
Analytics Views

ViewSets for analytics API endpoints.
"""

from django.utils.translation import gettext as _
from training_platform.numbers import is_finite
from rest_framework.throttling import UserRateThrottle
from rest_framework import viewsets, permissions, status
from rest_framework.decorators import action
from rest_framework.response import Response
from django.db.models import Count, Avg, Sum
from django.utils import timezone
from datetime import timedelta

from .models import (
    UserActivity, PerformanceMetric, UserSession,
    UserGoal, AnalyticsDashboard
)
from .serializers import (
    UserActivitySerializer, PerformanceMetricSerializer,
    UserSessionSerializer, UserGoalSerializer, AnalyticsDashboardSerializer
)
from training_platform.query_params import int_param


class AnalyticsWriteThrottle(UserRateThrottle):
    """Bounds how fast one account can write analytics rows.

    `DEFAULT_THROTTLE_RATES` defined only `charging`, and no analytics view declared a
    scope, so an authenticated client could fill these tables at line speed. Reads are
    not throttled here; only writes are.
    """

    scope = "analytics_write"

    def allow_request(self, request, view):
        if request.method in ("GET", "HEAD", "OPTIONS"):
            return True
        return super().allow_request(request, view)


class UserActivityViewSet(viewsets.ModelViewSet):
    """
    ViewSet for user activity tracking.

    An activity log is a LOG: rows are appended, never edited or removed through the API.
    PATCH turned a row into another type and DELETE erased it, on the table the
    achievement engine counts.
    """
    serializer_class = UserActivitySerializer
    permission_classes = [permissions.IsAuthenticated]
    throttle_classes = [AnalyticsWriteThrottle]
    http_method_names = ['get', 'post', 'head', 'options']
    
    def get_queryset(self):
        """Filter activities by user"""
        user = self.request.user
        if user.is_staff:
            return UserActivity.objects.all()
        return UserActivity.objects.filter(user=user)
    
    def perform_create(self, serializer):
        """Automatically set user and IP address"""
        serializer.save(
            user=self.request.user,
            ip_address=self.request.META.get('REMOTE_ADDR'),
            user_agent=self.request.META.get('HTTP_USER_AGENT', '')
        )
    
    @action(detail=False, methods=['get'])
    def recent_activities(self, request):
        """GET — the CALLER's activity log, newest first, paginated
        (`{count, next, previous, results}`, `?page_size=` up to 100).

        `?days=` (1-365, default 7) bounds how far back. `?activity_type=` narrows it to
        one type or a comma-separated list of them (`routine_completed,meal_completed`);
        a name that is not an activity type is 400 `bad_activity_type` with `field`
        (BACKEND_BUGS B-071). Each row is `UserActivitySerializer`: a finished workout
        carries `workout` (day, duration, volume — B-072), and since 2026-09-24 a meal
        is ONE `meal_completed` row carrying the meal's id, name and calories in
        `metadata`, and a logged set writes no row of its own (the workout it belongs to
        is the row). Rows written before that are kept and shown as they were written.
        """
        days = int_param(request.query_params, 'days', default=7, minimum=1, maximum=365)
        since_date = timezone.now() - timedelta(days=days)

        # The hard [:50] slice hid every older row with nothing in the response to say
        # so — the same defect the wallet ledger had. Pagination replaces it, so the
        # client can page back and knows how many rows exist.
        # The CALLER's activities. `get_queryset()` is the whole table for a staff
        # account, and this action is documented "for the user" — so an admin opening
        # their own recent activity was shown the platform's.
        activities = UserActivity.objects.filter(
            user=request.user, timestamp__gte=since_date
        ).order_by('-timestamp', '-id')
        asked = request.query_params.get('activity_type')
        if asked not in (None, ''):
            known = {key for key, _label in UserActivity.ACTIVITY_TYPES}
            wanted = {name.strip() for name in asked.split(',') if name.strip()}
            if not wanted or not wanted <= known:
                return Response(
                    {'error': _('activity_type must be one or more of: %(types)s.')
                        % {'types': ', '.join(sorted(known))},
                     'code': 'bad_activity_type', 'field': 'activity_type'},
                    status=status.HTTP_400_BAD_REQUEST)
            activities = activities.filter(activity_type__in=wanted)
        page = self.paginate_queryset(activities)
        if page is not None:
            return self.get_paginated_response(self.get_serializer(page, many=True).data)
        
        serializer = self.get_serializer(activities, many=True)
        return Response(serializer.data)
    
    @action(detail=False, methods=['get'])
    def activity_summary(self, request):
        """Get activity summary by type"""
        days = int_param(request.query_params, 'days', default=30, minimum=1, maximum=365)
        since_date = timezone.now() - timedelta(days=days)
        
        summary = UserActivity.objects.filter(
            user=request.user, timestamp__gte=since_date
        ).values('activity_type').annotate(
            count=Count('id')
        ).order_by('-count')
        
        return Response(summary)


class PerformanceMetricViewSet(viewsets.ModelViewSet):
    """
    ViewSet for performance metrics
    """
    serializer_class = PerformanceMetricSerializer
    permission_classes = [permissions.IsAuthenticated]
    throttle_classes = [AnalyticsWriteThrottle]
    
    def get_queryset(self):
        """Filter metrics by user"""
        user = self.request.user
        if user.is_staff:
            return PerformanceMetric.objects.all()
        return PerformanceMetric.objects.filter(user=user)
    
    def perform_create(self, serializer):
        """Automatically set user"""
        serializer.save(user=self.request.user)
    
    @action(detail=False, methods=['get'])
    def metric_trends(self, request):
        """Get metric trends over time"""
        metric_type = request.query_params.get('metric_type')
        days = int_param(request.query_params, 'days', default=30, minimum=1, maximum=365)
        since_date = timezone.now() - timedelta(days=days)
        
        # The caller's own metrics, bucketed by the caller's own calendar day.
        # `recorded_at__date` resolves against TIME_ZONE (Asia/Damascus) for everyone,
        # while streaks, daily advice and the quota use the user's timezone — so a
        # weigh-in at 22:30 Damascus landed on a different day for a user in Auckland
        # than their streak said. `training_platform/user_time.py` exists for this.
        from django.db.models.functions import TruncDate
        from zoneinfo import ZoneInfo

        try:
            user_tz = ZoneInfo(request.user.preferred_timezone or "UTC")
        except Exception:
            user_tz = timezone.get_current_timezone()

        queryset = PerformanceMetric.objects.filter(
            user=request.user, recorded_at__gte=since_date
        )
        
        if metric_type:
            queryset = queryset.filter(metric_type=metric_type)
        
        trends = (queryset
                  .annotate(day=TruncDate('recorded_at', tzinfo=user_tz))
                  .values('day')
                  .annotate(avg_value=Avg('value'), count=Count('id'))
                  .order_by('day'))
        
        return Response([
            {'recorded_at__date': row['day'], 'avg_value': row['avg_value'],
             'count': row['count']}
            for row in trends
        ])
    
    @action(detail=False, methods=['get'])
    def current_metrics(self, request):
        """The most recently recorded value for each metric type.

        Was `latest_date=Avg('recorded_at')`. Postgres has no avg(timestamptz), so
        this endpoint answered 500 for every role on every request; SQLite would have
        averaged the timestamp and returned a number that meant nothing. Both
        annotations were also averages despite being named `latest_*`, so even where
        it ran it did not report what it claimed.

        DISTINCT ON gives one row per metric_type, the newest, in a single query.
        """
        # The CALLER's metrics. For a staff account `get_queryset()` is the whole table,
        # so DISTINCT ON returned the newest weight of ANY user as the admin's own, while
        # `user_overview` on the same dashboard correctly showed theirs.
        rows = (
            PerformanceMetric.objects.filter(user=request.user)
            .order_by('metric_type', '-recorded_at', '-id')
            .distinct('metric_type')
            .values('metric_type', 'value', 'recorded_at')
        )
        return Response([
            {
                'metric_type': r['metric_type'],
                'latest_value': r['value'],
                'latest_date': r['recorded_at'],
            }
            for r in rows
        ])


class UserSessionViewSet(viewsets.ModelViewSet):
    """
    ViewSet for user sessions
    """
    serializer_class = UserSessionSerializer
    permission_classes = [permissions.IsAuthenticated]
    throttle_classes = [AnalyticsWriteThrottle]
    
    def get_queryset(self):
        """Filter sessions by user"""
        user = self.request.user
        if user.is_staff:
            return UserSession.objects.all()
        return UserSession.objects.filter(user=user)
    
    def perform_create(self, serializer):
        """Automatically set user and session info"""
        serializer.save(
            user=self.request.user,
            ip_address=self.request.META.get('REMOTE_ADDR'),
            user_agent=self.request.META.get('HTTP_USER_AGENT', '')
        )
    
    @action(detail=False, methods=['post'])
    def end_session(self, request):
        """End current user session"""
        session_id = request.data.get('session_id')
        if session_id:
            try:
                session = self.get_queryset().get(session_id=session_id)
                session.end_session()
                serializer = self.get_serializer(session)
                return Response(serializer.data)
            except UserSession.DoesNotExist:
                return Response(
                    {'error': 'Session not found'}, 
                    status=status.HTTP_404_NOT_FOUND
                )
        return Response(
            {'error': 'session_id required'}, 
            status=status.HTTP_400_BAD_REQUEST
        )


class UserGoalViewSet(viewsets.ModelViewSet):
    """
    The caller's goals (every goal for a staff account). Paginated lists.

    `POST /api/analytics/goals/` — body `goal_type`, `title`, `target_value` (> 0), and
    optionally `start_value`, `unit`, `target_date`, `description`, `metadata`. With a
    start the goal is stored at it: `current_value = start_value`, 0% (BACKEND_BUGS
    B-069). 400 `validation_error` with field codes: `start_reaches_target` on
    `start_value`; `target_value` must be greater than zero. The goal's shape, and how a
    `workout_frequency` goal is counted, are on `UserGoalSerializer`.

    **A `workout_frequency` goal's target is calculated** (the owner's decision 5,
    2026-09-24): from the person's program this week, else their profile's training
    days a week (`analytics/goals.py`). `target_value` may be left out of its POST.
    Refusals, on POST and on PATCH/PUT, each `400 {"error", "detail", "code", ...}`:

      `goal_target_from_plan` — a `target_value` that differs from the calculated one;
          the body carries `expected_target` (a number) and `target_source`
          (`"program"` | `"profile"`) — the app can offer that number instead;
      `goal_target_unavailable` — a new weekly goal (or a sent target) for someone
          with no program asking for work this week and no training days on their
          profile: the system has no number to own. The app asks the person to set
          their training days a week (profile) and tries again.

    A PATCH that changes nothing about the target (a title, `status`) is accepted
    whatever the plan says. Nothing differs by build.
    """
    serializer_class = UserGoalSerializer
    permission_classes = [permissions.IsAuthenticated]
    throttle_classes = [AnalyticsWriteThrottle]
    
    def get_queryset(self):
        """Filter goals by user"""
        user = self.request.user
        if user.is_staff:
            return UserGoal.objects.all()
        return UserGoal.objects.filter(user=user)
    
    def perform_create(self, serializer):
        """Automatically set user"""
        serializer.save(user=self.request.user)

    def create(self, request, *args, **kwargs):
        from .goals import GoalTargetRefused

        try:
            return super().create(request, *args, **kwargs)
        except GoalTargetRefused as refusal:
            return Response({**refusal.extra, 'error': refusal.message,
                             'detail': refusal.message, 'code': refusal.code},
                            status=status.HTTP_400_BAD_REQUEST)

    def update(self, request, *args, **kwargs):
        from .goals import GoalTargetRefused

        try:
            return super().update(request, *args, **kwargs)
        except GoalTargetRefused as refusal:
            return Response({**refusal.extra, 'error': refusal.message,
                             'detail': refusal.message, 'code': refusal.code},
                            status=status.HTTP_400_BAD_REQUEST)
    
    @action(detail=True, methods=['post'])
    def update_progress(self, request, pk=None):
        """POST `{new_value}` — a new reading; answers the goal.

        Progress is the share of the way from the goal's start to its target, in either
        direction (BACKEND_BUGS B-066): 82 -> 75 kg at 80 is 28.6%, a lift 72.5 -> 100 at
        82.5 is 36.4%. It is bounded to 0-100, a reading that moved the wrong way is 0,
        and the goal becomes `completed` (with `completed_at`) only when the reading
        REACHES the target in its direction — at or below it for a goal aiming down, at
        or above it for one aiming up. A goal with no `start_value` is measured from 0
        upward, as before; a `weight_loss` goal always aims down, and without a start
        its first reading becomes the start.

        400: `missing_new_value`, `bad_new_value` (not a finite number), and
        `goal_counted_from_workouts` for a `workout_frequency` goal — the server counts
        that one from the week's workouts, so there is no number to send.
        """
        from .goals import COUNTED_TYPES

        goal = self.get_object()
        if goal.goal_type in COUNTED_TYPES:
            return Response(
                {'error': _('This goal is counted from your workouts; there is no '
                            'number to record.'),
                 'code': 'goal_counted_from_workouts'},
                status=status.HTTP_400_BAD_REQUEST)
        new_value = request.data.get('new_value')
        
        if new_value is None:
            return Response(
                {'error': _('new_value is required'), 'code': 'missing_new_value'},
                status=status.HTTP_400_BAD_REQUEST
            )

        # `float(new_value)` was unguarded, so 'abc' was a 500 — and 'nan' and '1e400'
        # were WRITTEN and then poisoned every later read of the goal list, because the
        # row commits before the response is rendered and `json.dumps` refuses the value
        # from then on.
        if not is_finite(new_value) or isinstance(new_value, bool):
            return Response(
                {'error': _('new_value must be a finite number'),
                 'code': 'bad_new_value'},
                status=status.HTTP_400_BAD_REQUEST
            )

        goal.update_progress(float(new_value))
        serializer = self.get_serializer(goal)
        return Response(serializer.data)
    
    @action(detail=False, methods=['get'])
    def active_goals(self, request):
        """Active goals for the user, paginated.

        Returned every matching row as a bare array with no envelope and no limit, so
        the payload grew with the user's history and nothing told the client there was
        more to fetch.
        """
        active_goals = self.get_queryset().filter(status='active')
        page = self.paginate_queryset(active_goals)
        if page is not None:
            return self.get_paginated_response(self.get_serializer(page, many=True).data)
        return Response(self.get_serializer(active_goals, many=True).data)


class AnalyticsDashboardViewSet(viewsets.ModelViewSet):
    """
    ViewSet for analytics dashboard data
    """
    serializer_class = AnalyticsDashboardSerializer
    permission_classes = [permissions.IsAuthenticated]
    throttle_classes = [AnalyticsWriteThrottle]

    def perform_create(self, serializer):
        """The row belongs to whoever asked for it.

        This was the only viewset in the module without this method — the other four all
        pin the user — and the serializer left `user` writable, so a client could create
        a dashboard owned by another account and have it render at the top of their
        screen.
        """
        serializer.save(user=self.request.user)

    def perform_update(self, serializer):
        serializer.save(user=self.request.user)
    
    def get_queryset(self):
        """Filter dashboards by user and type"""
        user = self.request.user
        dashboard_type = self.request.query_params.get('dashboard_type', 'user')
        
        if user.is_staff:
            return AnalyticsDashboard.objects.filter(
                dashboard_type=dashboard_type
            )
        
        return AnalyticsDashboard.objects.filter(
            user=user,
            dashboard_type=dashboard_type
        )
    
    @action(detail=False, methods=['get'])
    def user_overview(self, request):
        """Get user overview analytics"""
        user = request.user
        days = int_param(request.query_params, 'days', default=30, minimum=1, maximum=365)
        since_date = timezone.now() - timedelta(days=days)
        
        # Activity count
        activity_count = UserActivity.objects.filter(
            user=user,
            timestamp__gte=since_date
        ).count()
        
        # Performance metrics
        metrics = PerformanceMetric.objects.filter(
            user=user,
            recorded_at__gte=since_date
        ).values('metric_type').annotate(
            avg_value=Avg('value'),
            count=Count('id')
        )
        
        # Active goals
        active_goals = UserGoal.objects.filter(
            user=user,
            status='active'
        ).count()
        
        # Session duration
        sessions = UserSession.objects.filter(
            user=user,
            started_at__gte=since_date
        ).aggregate(
            total_duration=Sum('duration_seconds'),
            session_count=Count('id')
        )
        
        overview = {
            'activity_count': activity_count,
            'metrics': list(metrics),
            'active_goals': active_goals,
            'total_session_duration': sessions['total_duration'] or 0,
            'session_count': sessions['session_count'] or 0,
            'period_days': days
        }
        
        return Response(overview)
    
    @action(detail=False, methods=['get'])
    def platform_stats(self, request):
        """Get platform-wide statistics (admin only)"""
        if not request.user.is_staff:
            return Response(
                {'error': 'Admin access required'}, 
                status=status.HTTP_403_FORBIDDEN
            )
        
        days = int_param(request.query_params, 'days', default=30, minimum=1, maximum=365)
        since_date = timezone.now() - timedelta(days=days)
        
        # User activity
        total_activities = UserActivity.objects.filter(
            timestamp__gte=since_date
        ).count()
        
        # Active users
        active_users = UserActivity.objects.filter(
            timestamp__gte=since_date
        ).values('user').distinct().count()
        
        # Performance metrics
        total_metrics = PerformanceMetric.objects.filter(
            recorded_at__gte=since_date
        ).count()
        
        # Active goals
        active_goals = UserGoal.objects.filter(
            status='active'
        ).count()
        
        platform_stats = {
            'total_activities': total_activities,
            'active_users': active_users,
            'total_metrics': total_metrics,
            'active_goals': active_goals,
            'period_days': days
        }
        
        return Response(platform_stats) 