"""
Analytics Views

ViewSets for analytics API endpoints.
"""

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


class UserActivityViewSet(viewsets.ModelViewSet):
    """
    ViewSet for user activity tracking
    """
    serializer_class = UserActivitySerializer
    permission_classes = [permissions.IsAuthenticated]
    
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
        """Get recent activities for the user"""
        days = int(request.query_params.get('days', 7))
        since_date = timezone.now() - timedelta(days=days)
        
        activities = self.get_queryset().filter(
            timestamp__gte=since_date
        ).order_by('-timestamp')[:50]
        
        serializer = self.get_serializer(activities, many=True)
        return Response(serializer.data)
    
    @action(detail=False, methods=['get'])
    def activity_summary(self, request):
        """Get activity summary by type"""
        days = int(request.query_params.get('days', 30))
        since_date = timezone.now() - timedelta(days=days)
        
        summary = self.get_queryset().filter(
            timestamp__gte=since_date
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
        days = int(request.query_params.get('days', 30))
        since_date = timezone.now() - timedelta(days=days)
        
        queryset = self.get_queryset().filter(
            recorded_at__gte=since_date
        )
        
        if metric_type:
            queryset = queryset.filter(metric_type=metric_type)
        
        trends = queryset.values('recorded_at__date').annotate(
            avg_value=Avg('value'),
            count=Count('id')
        ).order_by('recorded_at__date')
        
        return Response(trends)
    
    @action(detail=False, methods=['get'])
    def current_metrics(self, request):
        """Get current metric values"""
        metrics = self.get_queryset().values('metric_type').annotate(
            latest_value=Avg('value'),
            latest_date=Avg('recorded_at')
        )
        
        return Response(metrics)


class UserSessionViewSet(viewsets.ModelViewSet):
    """
    ViewSet for user sessions
    """
    serializer_class = UserSessionSerializer
    permission_classes = [permissions.IsAuthenticated]
    
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
    ViewSet for user goals
    """
    serializer_class = UserGoalSerializer
    permission_classes = [permissions.IsAuthenticated]
    
    def get_queryset(self):
        """Filter goals by user"""
        user = self.request.user
        if user.is_staff:
            return UserGoal.objects.all()
        return UserGoal.objects.filter(user=user)
    
    def perform_create(self, serializer):
        """Automatically set user"""
        serializer.save(user=self.request.user)
    
    @action(detail=True, methods=['post'])
    def update_progress(self, request, pk=None):
        """Update goal progress"""
        goal = self.get_object()
        new_value = request.data.get('new_value')
        
        if new_value is not None:
            goal.update_progress(float(new_value))
            serializer = self.get_serializer(goal)
            return Response(serializer.data)
        
        return Response(
            {'error': 'new_value required'}, 
            status=status.HTTP_400_BAD_REQUEST
        )
    
    @action(detail=False, methods=['get'])
    def active_goals(self, request):
        """Get active goals for the user"""
        active_goals = self.get_queryset().filter(status='active')
        serializer = self.get_serializer(active_goals, many=True)
        return Response(serializer.data)


class AnalyticsDashboardViewSet(viewsets.ModelViewSet):
    """
    ViewSet for analytics dashboard data
    """
    serializer_class = AnalyticsDashboardSerializer
    permission_classes = [permissions.IsAuthenticated]
    
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
        days = int(request.query_params.get('days', 30))
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
        
        days = int(request.query_params.get('days', 30))
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