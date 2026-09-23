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
from training_platform.query_params import int_param


class AchievementViewSet(viewsets.ReadOnlyModelViewSet):
    """
    API endpoints for achievements.
    
    GET /api/achievements/              - List all available achievements
    GET /api/achievements/{id}/         - Get single achievement details
    GET /api/achievements/my/           - Get user's earned achievements with stats
    GET /api/achievements/progress/     - Get progress towards unearned achievements
    GET /api/achievements/leaderboard/  - Get top achievers
    POST /api/achievements/check/       - Manually trigger achievement check
    POST /api/achievements/{id}/feature/ - Feature/unfeature an earned badge

    Auth required; nothing differs by build. `{id}` is always the ACHIEVEMENT's id.
    Catalogue rows (list and detail) carry `is_featured` for the caller, and `my/` rows
    carry `achievement_id` beside their own `id` (2026-09-24, BACKEND_BUGS B-078).
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
    
    def _progress_context(self, request):
        """The two maps that turn this endpoint's per-row queries into per-request ones."""
        if not request.user.is_authenticated:
            return {}
        return {
            'earned_map': {
                ua.achievement_id: ua
                for ua in UserAchievement.objects.filter(user=request.user)
            },
            'metric_cache': {},
        }

    def list(self, request, *args, **kwargs):
        """
        List all available achievements with user's progress.
        
        Returns achievements grouped by category with progress data.

        `{"achievements": [...], "by_category": {category: [...]},
        "total_available": int}`; a row is `AchievementWithProgressSerializer`:
        `id, key, name, description, category, category_display, criteria, points,
        icon_url, badge_color, is_rare, is_secret, created_at, progress, is_earned,
        earned_at, is_featured` — `is_featured` whether the caller features it (false
        when not earned; B-078). A secret badge is listed only to someone who earned it.
        """
        queryset = self.get_queryset()
        # Built once and handed to the serializer, which used to issue an EXISTS, a
        # SELECT and a metric COUNT per achievement.
        serializer = self.get_serializer(
            queryset, many=True,
            context={**self.get_serializer_context(), **self._progress_context(request)},
        )

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

        A row of `achievements` is `UserAchievementDetailSerializer`: `id` is the EARNED
        row's id, `achievement_id` the badge's — the id the catalogue lists and
        `feature/` takes (B-078) — then `name, description, category,
        category_display, points, badge_color, is_rare, icon_url, earned_at,
        is_featured`.
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

        `limit` 1..50 (default 10). Ties on points are ordered by user id, so a place
        does not change between two requests over the same data. An account the caller
        has blocked, or that has blocked the caller, is left out of `leaderboard`
        AFTER the ranks are counted: the other rows keep their true `rank` and the
        list can be shorter than `limit`. Nothing differs by build.
        """
        limit = int_param(request.query_params, 'limit', default=10, minimum=1, maximum=50)

        from django.db.models import Sum as DjSum
        from moderation.services import blocked_ids
        from users.models import CustomUser

        # ONE query for the board. It was an aggregate and then a
        # `CustomUser.objects.get` per row — up to 50 more queries a request — ordered
        # by `-total_points` alone, which is not an order: two people tied on points
        # swapped places between requests. The user's id breaks the tie (SIDE_BUGS
        # S-088) — `user_id`, not `user`: ordering by the relation sorts by
        # `CustomUser.Meta.ordering`, which is not the id.
        leaders = (UserAchievement.objects
                   .values('user', 'user__username', 'user__profile_picture')
                   .annotate(total_points=DjSum('achievement__points'),
                             total_achievements=Count('id'))
                   .order_by('-total_points', 'user_id')[:limit])

        # Blocked people are dropped AFTER the rank is counted, the rule the challenge
        # leaderboard follows (`social/views.py`): ranks stay true and the reader sees
        # a gap, rather than someone promoted into a place they did not earn. They were
        # not dropped at all, so a blocked account's name and photo still reached the
        # person who had blocked it.
        hidden = blocked_ids(request.user)
        storage = CustomUser._meta.get_field('profile_picture').storage
        leaderboard = []
        for idx, entry in enumerate(leaders, 1):
            if entry['user'] in hidden:
                continue
            picture = entry['user__profile_picture']
            leaderboard.append({
                'rank': idx,
                'user_id': entry['user'],
                'username': entry['user__username'],
                'total_points': entry['total_points'] or 0,
                'total_achievements': entry['total_achievements'],
                'profile_picture_url': (
                    request.build_absolute_uri(storage.url(picture)) if picture else None
                ),
            })

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

        `{id}` is the ACHIEVEMENT's id (`achievement_id` on a `my/` row). No body. A
        toggle: `200 {"is_featured", "message"}`. `404 {"error", "code":
        "achievement_not_earned"}` — you have not earned it (or no such badge);
        `400 {"error", "code": "featured_limit_reached"}` — three are featured already.
        Both answered only their status default before (2026-09-24). The featured
        badges are what another member sees on your profile
        (`GET /api/social/users/public-profile/{id}/` `featured_achievements`).
        """
        user = request.user
        
        try:
            user_achievement = UserAchievement.objects.get(
                user=user,
                achievement_id=pk
            )
        except (UserAchievement.DoesNotExist, ValueError, TypeError):
            # A non-numeric `{id}` reached the lookup and was a 500.
            return Response(
                {'error': _('Achievement not earned'), 'code': 'achievement_not_earned'},
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
                    {'error': _('Maximum 3 featured achievements allowed'),
                     'code': 'featured_limit_reached'},
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
