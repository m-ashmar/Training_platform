"""
Achievement Views - Rich API endpoints for achievement system.
"""

from rest_framework import viewsets, status, permissions
from django.utils.translation import gettext as _
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.views import APIView
from django.db.models import Sum, Count

from .models import Achievement, UserAchievement, AchievementProgress
from .serializers import (
    AchievementSerializer, 
    AchievementWithProgressSerializer,
    UserAchievementSerializer,
    UserAchievementDetailSerializer,
)
from .engine import AchievementEngine


class AchievementViewSet(viewsets.ReadOnlyModelViewSet):
    """
    API endpoints for achievements.
    
    GET /api/achievements/              - List all available achievements
    GET /api/achievements/{id}/         - Get single achievement details
    GET /api/achievements/my/           - Get user's earned achievements with stats
    GET /api/achievements/progress/     - Get progress towards unearned achievements
    GET /api/achievements/leaderboard/  - Get top achievers
    POST /api/achievements/check/       - Manually trigger achievement check
    """
    permission_classes = [permissions.IsAuthenticated]
    
    def get_queryset(self):
        """Return active achievements, hiding secrets for non-earned."""
        queryset = Achievement.objects.filter(is_active=True)
        
        # Hide secret achievements the user hasn't earned
        if self.request.user.is_authenticated:
            earned_ids = UserAchievement.objects.filter(
                user=self.request.user
            ).values_list('achievement_id', flat=True)
            
            # Show non-secret OR earned secret achievements
            queryset = queryset.filter(
                is_secret=False
            ) | queryset.filter(
                id__in=earned_ids
            )
        else:
            queryset = queryset.filter(is_secret=False)
        
        return queryset.distinct()
    
    def get_serializer_class(self):
        if self.action == 'list':
            return AchievementWithProgressSerializer
        return AchievementWithProgressSerializer
    
    def list(self, request, *args, **kwargs):
        """
        List all available achievements with user's progress.
        
        Returns achievements grouped by category with progress data.
        """
        queryset = self.get_queryset()
        serializer = self.get_serializer(queryset, many=True)
        
        # Group by category
        categories = {}
        for item in serializer.data:
            cat = item['category']
            if cat not in categories:
                categories[cat] = []
            categories[cat].append(item)
        
        return Response({
            'achievements': serializer.data,
            'by_category': categories,
            'total_available': queryset.count()
        })
    
    @action(detail=False, methods=['get'])
    def my(self, request):
        """
        Get user's earned achievements with comprehensive stats.
        
        GET /api/achievements/my/
        
        Returns:
        - List of earned achievements
        - Total points
        - Rank among all users
        - Category breakdown
        - Recent achievements
        """
        user = request.user
        
        # Get earned achievements
        earned = UserAchievement.objects.filter(
            user=user
        ).select_related('achievement').order_by('-earned_at')
        
        # Get stats
        stats = AchievementEngine.get_user_stats(user)
        
        # Serialize achievements
        serializer = UserAchievementDetailSerializer(
            earned, 
            many=True, 
            context={'request': request}
        )
        
        return Response({
            'achievements': serializer.data,
            'stats': stats
        })
    
    @action(detail=False, methods=['get'])
    def progress(self, request):
        """
        Get user's progress towards unearned achievements.
        
        GET /api/achievements/progress/
        
        Returns progress data for achievements not yet earned,
        sorted by closest to completion.
        """
        user = request.user
        
        # Get achievements not yet earned
        earned_ids = UserAchievement.objects.filter(
            user=user
        ).values_list('achievement_id', flat=True)
        
        unearned = Achievement.objects.filter(
            is_active=True,
            is_secret=False  # Don't show progress for secrets
        ).exclude(id__in=earned_ids)
        
        # Calculate progress for each
        progress_list = []
        for achievement in unearned:
            progress = AchievementEngine.get_user_progress(user, achievement)
            progress_list.append({
                'achievement': AchievementSerializer(
                    achievement, 
                    context={'request': request}
                ).data,
                'current_value': progress['current_value'],
                'target_value': progress['target_value'],
                'progress_percentage': progress['progress_percentage'],
                'remaining': progress['remaining'],
            })
        
        # Sort by closest to completion
        progress_list.sort(key=lambda x: -x['progress_percentage'])
        
        return Response({
            'progress': progress_list,
            'total_in_progress': len(progress_list)
        })
    
    @action(detail=False, methods=['get'])
    def leaderboard(self, request):
        """
        Get achievement leaderboard.
        
        GET /api/achievements/leaderboard/?limit=10
        
        Returns top achievers ranked by total points.
        """
        limit = min(int(request.query_params.get('limit', 10)), 50)
        
        # Aggregate points per user
        from django.db.models import Sum as DjSum
        from users.models import CustomUser
        
        leaders = UserAchievement.objects.values('user').annotate(
            total_points=DjSum('achievement__points'),
            total_achievements=Count('id')
        ).order_by('-total_points')[:limit]
        
        # Enrich with user data
        leaderboard = []
        for idx, entry in enumerate(leaders, 1):
            try:
                user = CustomUser.objects.get(id=entry['user'])
                leaderboard.append({
                    'rank': idx,
                    'user_id': user.id,
                    'username': user.username,
                    'total_points': entry['total_points'] or 0,
                    'total_achievements': entry['total_achievements'],
                    'profile_picture_url': (
                        request.build_absolute_uri(user.profile_picture.url)
                        if user.profile_picture else None
                    )
                })
            except CustomUser.DoesNotExist:
                continue
        
        # Find current user's rank
        user_rank = None
        user_stats = None
        if request.user.is_authenticated:
            stats = AchievementEngine.get_user_stats(request.user)
            user_rank = stats['rank']
            user_stats = stats
        
        return Response({
            'leaderboard': leaderboard,
            'user_rank': user_rank,
            'user_stats': user_stats
        })
    
    @action(detail=False, methods=['post'])
    def check(self, request):
        """
        Manually trigger achievement check for current user.
        
        POST /api/achievements/check/
        
        Useful for retroactive achievement awarding.
        """
        user = request.user
        
        awarded = AchievementEngine.bulk_check_for_user(user)
        
        return Response({
            'message': _('Achievement check complete.'),
            'newly_awarded': awarded
        })
    
    @action(detail=True, methods=['post'])
    def feature(self, request, pk=None):
        """
        Feature/unfeature an achievement on user's profile.
        
        POST /api/achievements/{id}/feature/
        
        Users can feature up to 3 achievements.
        """
        user = request.user
        
        try:
            user_achievement = UserAchievement.objects.get(
                user=user,
                achievement_id=pk
            )
        except UserAchievement.DoesNotExist:
            return Response(
                {'error': 'Achievement not earned'},
                status=status.HTTP_404_NOT_FOUND
            )
        
        # Check max featured (3)
        if not user_achievement.is_featured:
            featured_count = UserAchievement.objects.filter(
                user=user,
                is_featured=True
            ).count()
            
            if featured_count >= 3:
                return Response(
                    {'error': 'Maximum 3 featured achievements allowed'},
                    status=status.HTTP_400_BAD_REQUEST
                )
        
        # Toggle featured status
        user_achievement.is_featured = not user_achievement.is_featured
        user_achievement.save()
        
        return Response({
            'is_featured': user_achievement.is_featured,
            'message': _('Achievement featured') if user_achievement.is_featured else _('Achievement unfeatured')
        })


class AchievementCategoriesView(APIView):
    """
    Get all achievement categories with counts.
    
    GET /api/achievements/categories/
    """
    permission_classes = [permissions.IsAuthenticated]
    
    def get(self, request):
        categories = []
        
        for key, label in Achievement.ACHIEVEMENT_CATEGORIES:
            total = Achievement.objects.filter(
                category=key, 
                is_active=True,
                is_secret=False
            ).count()
            
            earned = UserAchievement.objects.filter(
                user=request.user,
                achievement__category=key
            ).count()
            
            categories.append({
                'key': key,
                'label': label,
                'total': total,
                'earned': earned,
                'progress_percentage': (earned / total * 100) if total > 0 else 0
            })
        
        return Response({'categories': categories})
