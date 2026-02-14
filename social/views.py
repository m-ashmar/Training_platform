"""
Social Features API Views

This module provides REST API endpoints for social networking features
including following, posts, comments, likes, challenges, and achievements.
"""

from rest_framework import viewsets, status, permissions
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.filters import SearchFilter, OrderingFilter
from django_filters.rest_framework import DjangoFilterBackend
from django.db.models import Q
from django.utils import timezone
from django.contrib.contenttypes.models import ContentType
from django.core.cache import cache

from .models import (
    UserFollow, Post, PostLike, Comment, CommentLike, Challenge,
    ChallengeParticipation, Achievement, UserAchievement, Notification
)
from .serializers import (
    UserFollowSerializer, PostSerializer, CommentSerializer,
    ChallengeSerializer, AchievementSerializer, UserAchievementSerializer,
    NotificationSerializer, PublicUserProfileSerializer
)

from users.models import CustomUser
from .tasks import dispatch_notification

# Use IsAuthenticated from rest_framework.permissions
IsAuthenticated = permissions.IsAuthenticated


class UserFollowViewSet(viewsets.ModelViewSet):
    """
    API endpoints for user following system
    
    Permissions:
    - Users can follow/unfollow others
    - Users can view their followers/following
    """
    serializer_class = UserFollowSerializer
    permission_classes = [IsAuthenticated]
    
    def get_queryset(self):
        return UserFollow.objects.filter(
            Q(follower=self.request.user) | Q(following=self.request.user)
        )
    
    def perform_create(self, serializer):
        serializer.save(follower=self.request.user)
    
    @action(detail=False, methods=['post'])
    def follow_user(self, request):
        """
        Follow a user
        
        POST /api/social/follows/follow_user/
        {
            "user_id": 123
        }
        """
        user_id = request.data.get('user_id')
        try:
            user_to_follow = CustomUser.objects.get(id=user_id)
            
            if user_to_follow == request.user:
                return Response(
                    {'error': 'Cannot follow yourself'}, 
                    status=status.HTTP_400_BAD_REQUEST
                )
            
            follow, created = UserFollow.objects.get_or_create(
                follower=request.user,
                following=user_to_follow
            )
            
            if created:
                # Create notification
                Notification.objects.create(
                    recipient=user_to_follow,
                    sender=request.user,
                    notification_type='follow',
                    title='New Follower',
                    message=f'{request.user.username} started following you'
                )
                
                # Send Push Notification
                # Emit Domain Event
                from notifications.domain.dispatcher import emit_event
                from notifications.domain.events import UserFollowedEvent
                
                emit_event(UserFollowedEvent(
                    actor_id=request.user.id,
                    target_user_id=user_to_follow.id
                ))
                
                return Response(
                    {'message': 'Successfully followed user'}, 
                    status=status.HTTP_201_CREATED
                )
            else:
                return Response(
                    {'message': 'Already following this user'}, 
                    status=status.HTTP_200_OK
                )
                
        except CustomUser.DoesNotExist:
            return Response(
                {'error': 'User not found'}, 
                status=status.HTTP_404_NOT_FOUND
            )
    
    @action(detail=False, methods=['post'])
    def unfollow_user(self, request):
        """
        Unfollow a user
        
        POST /api/social/follows/unfollow_user/
        {
            "user_id": 123
        }
        """
        user_id = request.data.get('user_id')
        try:
            user_to_unfollow = CustomUser.objects.get(id=user_id)
            
            UserFollow.objects.filter(
                follower=request.user,
                following=user_to_unfollow
            ).delete()
            
            return Response(
                {'message': 'Successfully unfollowed user'}, 
                status=status.HTTP_200_OK
            )
            
        except CustomUser.DoesNotExist:
            return Response(
                {'error': 'User not found'}, 
                status=status.HTTP_404_NOT_FOUND
            )
    
    @action(detail=False, methods=['get'])
    def followers(self, request):
        """
        Get user's followers
        
        GET /api/social/follows/followers/
        """
        followers = UserFollow.objects.filter(
            following=request.user
        ).select_related('follower')
        
        data = [{
            'id': follow.follower.id,
            'username': follow.follower.username,
            'email': follow.follower.email,
            'user_type': follow.follower.user_type,
            'profile_picture': follow.follower.profile_picture.url if follow.follower.profile_picture else None,
            'followed_at': follow.created_at
        } for follow in followers]
        
        return Response({
            'followers': data,
            'count': len(data)
        })
    
    @action(detail=False, methods=['get'])
    def following(self, request):
        """
        Get users that the current user is following
        
        GET /api/social/follows/following/
        """
        following = UserFollow.objects.filter(
            follower=request.user
        ).select_related('following')
        
        data = [{
            'id': follow.following.id,
            'username': follow.following.username,
            'email': follow.following.email,
            'user_type': follow.following.user_type,
            'profile_picture': follow.following.profile_picture.url if follow.following.profile_picture else None,
            'followed_at': follow.created_at
        } for follow in following]
        
        return Response({
            'following': data,
            'count': len(data)
        })


class PostViewSet(viewsets.ModelViewSet):
    """
    API endpoints for social posts
    """
    serializer_class = PostSerializer
    permission_classes = [IsAuthenticated]
    filter_backends = [DjangoFilterBackend, SearchFilter, OrderingFilter]
    filterset_fields = ['author', 'post_type', 'visibility']
    search_fields = ['title', 'content']
    ordering_fields = ['created_at', 'updated_at', 'likes_count', 'comments_count']
    ordering = ['-created_at']
    
    def get_queryset(self):
        user = self.request.user
        
        # Get posts that user can view based on visibility
        queryset = Post.objects.filter(
            Q(visibility='public') |
            Q(author=user) |  # Own posts
            Q(visibility='followers', author__followers__follower=user)  # Followed users
        ).distinct()
        
        # Handle author filtering
        author_id = self.request.query_params.get('author')
        if author_id:
            try:
                author_id = int(author_id)
                queryset = queryset.filter(author_id=author_id)
            except (ValueError, TypeError):
                # If author_id is not a valid integer, return empty queryset
                queryset = Post.objects.none()
        
        # Optimize: Prefetch related data to avoid N+1
        from django.db.models import Exists, OuterRef
        
        # Annotate whether user liked the post
        if user.is_authenticated:
            queryset = queryset.annotate(
                is_liked_anno=Exists(PostLike.objects.filter(post=OuterRef('pk'), user=user))
            )

        return queryset.select_related('author')
    
    def perform_create(self, serializer):
        serializer.save(author=self.request.user)
    
    @action(detail=True, methods=['post'])
    def like(self, request, pk=None):
        """
        Like/unlike a post
        
        POST /api/social/posts/{id}/like/
        """
        post = self.get_object()
        
        like, created = PostLike.objects.get_or_create(
            user=request.user,
            post=post
        )
        
        if created:
            # Update post likes count
            post.likes_count = post.likes.count()
            post.save()
            
            # Create notification
            if post.author != request.user:
                Notification.objects.create(
                    recipient=post.author,
                    sender=request.user,
                    notification_type='like',
                    title='Post Liked',
                    message=f'{request.user.username} liked your post',
                    related_object=post
                )
                
                # Send Push Notification
                # Emit Domain Event
                from notifications.domain.dispatcher import emit_event
                from notifications.domain.events import PostLikedEvent
                
                emit_event(PostLikedEvent(
                    actor_id=request.user.id,
                    target_post_id=post.id,
                    post_author_id=post.author.id
                ))
            
            return Response({'message': 'Post liked'})
        else:
            # Unlike the post
            like.delete()
            post.likes_count = post.likes.count()
            post.save()
            
            return Response({'message': 'Post unliked'})
    
    @action(detail=False, methods=['get'])
    def feed(self, request):
        """
        Get user's social feed
        
        GET /api/social/posts/feed/?page=1&limit=10
        """
        page = int(request.query_params.get('page', 1))
        limit = min(int(request.query_params.get('limit', 10)), 50)
        offset = (page - 1) * limit
        
        posts = self.get_queryset()[offset:offset + limit]
        serializer = self.get_serializer(posts, many=True)
        
        return Response({
            'posts': serializer.data,
            'page': page,
            'limit': limit,
            'has_more': len(posts) == limit
        })


class CommentViewSet(viewsets.ModelViewSet):
    """
    API endpoints for post comments
    """
    serializer_class = CommentSerializer
    permission_classes = [IsAuthenticated]
    filter_backends = [DjangoFilterBackend, SearchFilter, OrderingFilter]
    filterset_fields = ['post', 'author', 'parent_comment']
    search_fields = ['content']
    ordering_fields = ['created_at', 'updated_at', 'likes_count']
    ordering = ['-created_at']
    
    def get_queryset(self):
        user = self.request.user
        
        # Base queryset with visibility permissions
        queryset = Comment.objects.filter(
            post__in=Post.objects.filter(
                Q(visibility='public') |
                Q(author=user) |
                Q(visibility='followers', author__followers__follower=user)
            )
        ).distinct()
        
        # Handle post filtering
        post_id = self.request.query_params.get('post')
        if post_id:
            try:
                post_id = int(post_id)
                queryset = queryset.filter(post_id=post_id)
            except (ValueError, TypeError):
                # If post_id is not a valid integer, return empty queryset
                queryset = Comment.objects.none()
        
        return queryset
    
    def perform_create(self, serializer):
        comment = serializer.save(author=self.request.user)
        
        # Update post comments count
        post = comment.post
        post.comments_count = post.comments.count()
        post.save()
        
        # Create notification
        if post.author != self.request.user:
            Notification.objects.create(
                recipient=post.author,
                sender=self.request.user,
                notification_type='comment',
                title='New Comment',
                message=f'{self.request.user.username} commented on your post',
                related_object=comment
            )
            
            # Send Push Notification
            # Emit Domain Event
            from notifications.domain.dispatcher import emit_event
            from notifications.domain.events import CommentCreatedEvent
            
            emit_event(CommentCreatedEvent(
                actor_id=self.request.user.id,
                target_post_id=post.id,
                comment_id=comment.id,
                post_author_id=post.author.id,
                comment_text=comment.content
            ))
    
    @action(detail=True, methods=['post'])
    def like(self, request, pk=None):
        """
        Like/unlike a comment
        
        POST /api/social/comments/{id}/like/
        """
        comment = self.get_object()
        
        like, created = CommentLike.objects.get_or_create(
            user=request.user,
            comment=comment
        )
        
        if created:
            comment.likes_count = comment.likes.count()
            comment.save()
            return Response({'message': 'Comment liked'})
        else:
            like.delete()
            comment.likes_count = comment.likes.count()
            comment.save()
            return Response({'message': 'Comment unliked'})


class ChallengeViewSet(viewsets.ModelViewSet):
    """
    API endpoints for community challenges
    """
    serializer_class = ChallengeSerializer
    permission_classes = [IsAuthenticated]
    
    def get_queryset(self):
        # Optimize to avoid N+1 queries:
        # - select_related('creator') to load creator in one query
        # - annotate current user's participation data using EXISTS/Subquery
        #   so the serializer does not issue queries per challenge
        from django.db.models import Exists, OuterRef, Subquery
        qs = (
            Challenge.objects.all()
            .select_related('creator')
            .only(
                'id', 'title', 'description', 'challenge_type',
                'target_value', 'unit', 'start_date', 'end_date',
                'max_participants', 'participants_count', 'status',
                'created_at', 'rules', 'reward_description', 'image',
                'creator__id', 'creator__username', 'creator__email',
                'creator__user_type', 'creator__profile_picture',
            )
            .order_by('-created_at')
        )
        user = getattr(self, 'request', None).user if getattr(self, 'request', None) else None
        if user and user.is_authenticated:
            part_qs = ChallengeParticipation.objects.filter(user=user, challenge=OuterRef('pk'))
            qs = qs.annotate(
                is_joined_anno=Exists(part_qs),
                cur_value_anno=Subquery(part_qs.values('current_value')[:1]),
                prog_pct_anno=Subquery(part_qs.values('progress_percentage')[:1]),
                rank_anno=Subquery(part_qs.values('rank')[:1]),
            )
        return qs
    
    def list(self, request, *args, **kwargs):
        """
        Per-user short-lived cache for the challenges list to reduce repeated DB work.
        Cached for 30 seconds keyed by user and querystring.
        """
        try:
            user_part = f"user:{request.user.id}" if request.user and request.user.is_authenticated else "anon"
            # Ignore querystring noise to maximize cache hits for mobile polling
            key = f"challenges_list:{user_part}"
            cached = cache.get(key)
            if cached is not None:
                return Response(cached)
            response = super().list(request, *args, **kwargs)
            if response.status_code == 200 and isinstance(response.data, (list, dict)):
                cache.set(key, response.data, timeout=120)
            return response
        except Exception:
            return super().list(request, *args, **kwargs)
    
    def perform_create(self, serializer):
        serializer.save(creator=self.request.user)
    
    @action(detail=True, methods=['post'])
    def join(self, request, pk=None):
        """
        Join a challenge
        
        POST /api/social/challenges/{id}/join/
        """
        challenge = self.get_object()
        
        if challenge.max_participants and challenge.participants_count >= challenge.max_participants:
            return Response(
                {'error': 'Challenge is full'}, 
                status=status.HTTP_400_BAD_REQUEST
            )
        
        participation, created = ChallengeParticipation.objects.get_or_create(
            user=request.user,
            challenge=challenge
        )
        
        if created:
            challenge.participants_count = challenge.participations.count()
            challenge.save()
            
            return Response(
                {'message': 'Successfully joined challenge'}, 
                status=status.HTTP_201_CREATED
            )
        else:
            return Response(
                {'message': 'Already participating in this challenge'}, 
                status=status.HTTP_200_OK
            )
    
    @action(detail=True, methods=['get'])
    def leaderboard(self, request, pk=None):
        """
        Get challenge leaderboard
        
        GET /api/social/challenges/{id}/leaderboard/
        """
        challenge = self.get_object()
        
        participants = ChallengeParticipation.objects.filter(
            challenge=challenge
        ).order_by('-current_value')[:10]
        
        leaderboard = [{
            'rank': idx + 1,
            'user': {
                'id': p.user.id,
                'username': p.user.username
            },
            'current_value': p.current_value,
            'progress_percentage': p.progress_percentage
        } for idx, p in enumerate(participants)]
        
        return Response({
            'challenge': challenge.title,
            'leaderboard': leaderboard
        })

    @action(detail=True, methods=['post'])
    def update_progress(self, request, pk=None):
        """
        Update user's progress in a challenge
        
        POST /api/social/challenges/{id}/update_progress/
        {
            "current_value": 150.5,
            "notes": "Completed today's workout"
        }
        """
        challenge = self.get_object()
        user = request.user
        
        # Check if user is participating in this challenge
        try:
            participation = ChallengeParticipation.objects.get(
                user=user,
                challenge=challenge
            )
        except ChallengeParticipation.DoesNotExist:
            return Response(
                {'error': 'You are not participating in this challenge. Join first!'}, 
                status=status.HTTP_400_BAD_REQUEST
            )
        
        # Check if challenge is active
        if not challenge.is_active:
            return Response(
                {'error': 'This challenge is not currently active'}, 
                status=status.HTTP_400_BAD_REQUEST
            )
        
        current_value = request.data.get('current_value')
        notes = request.data.get('notes', '')
        
        if current_value is None:
            return Response(
                {'error': 'current_value is required'}, 
                status=status.HTTP_400_BAD_REQUEST
            )
        
        try:
            current_value = float(current_value)
        except (ValueError, TypeError):
            return Response(
                {'error': 'current_value must be a valid number'}, 
                status=status.HTTP_400_BAD_REQUEST
            )
        
        # Update participation progress
        old_value = participation.current_value
        participation.current_value = current_value
        
        # Calculate progress percentage
        if challenge.target_value and challenge.target_value > 0:
            participation.progress_percentage = min(100.0, (current_value / challenge.target_value) * 100)
        else:
            participation.progress_percentage = 0.0
        
        # Check if challenge is completed
        if (challenge.target_value and
                current_value >= challenge.target_value and
                participation.status != 'completed'):
            participation.status = 'completed'
            participation.completed_at = timezone.now()
        
        participation.save()
        
        # Update rankings for all participants
        self._update_challenge_rankings(challenge)
        
        # Create notification for significant progress
        if current_value > old_value:
            progress_increase = current_value - old_value
            if progress_increase > 0:
                Notification.objects.create(
                    recipient=user,
                    notification_type='challenge_progress',
                    title=f'Challenge Progress Update',
                    message=f'Great job! You made progress in "{challenge.title}" - {progress_increase:.1f} {challenge.unit}',
                    content_type=ContentType.objects.get_for_model(Challenge),
                    object_id=challenge.id
                )
                
                # Send Push Notification
                # Emit Domain Event
                from notifications.domain.dispatcher import emit_event
                from notifications.domain.events import ChallengeProgressEvent
                
                emit_event(ChallengeProgressEvent(
                    user_id=user.id,
                    challenge_id=challenge.id,
                    challenge_title=challenge.title,
                    progress=progress_increase,
                    unit=challenge.unit
                ))
        
        return Response({
            'message': 'Progress updated successfully',
            'participation': {
                'current_value': participation.current_value,
                'progress_percentage': participation.progress_percentage,
                'status': participation.status,
                'rank': participation.rank,
                'target_value': challenge.target_value,
                'unit': challenge.unit
            }
        })
    
    def _update_challenge_rankings(self, challenge):
        """Update rankings for all participants in a challenge"""
        participants = ChallengeParticipation.objects.filter(
            challenge=challenge
        ).order_by('-current_value')
        
        for rank, participation in enumerate(participants, 1):
            participation.rank = rank
            participation.save()


class AchievementViewSet(viewsets.ReadOnlyModelViewSet):
    """
    API endpoints for achievements (read-only)
    """
    serializer_class = AchievementSerializer
    permission_classes = [IsAuthenticated]
    
    def get_queryset(self):
        return Achievement.objects.filter(is_active=True)
    
    @action(detail=False, methods=['get'])
    def user_achievements(self, request):
        """
        Get user's earned achievements
        
        GET /api/social/achievements/user_achievements/
        """
        user_achievements = UserAchievement.objects.filter(
            user=request.user
        ).select_related('achievement')
        
        data = [{
            'achievement': {
                'id': ua.achievement.id,
                'name': ua.achievement.name,
                'description': ua.achievement.description,
                'category': ua.achievement.category,
                'points': ua.achievement.points,
                'icon': ua.achievement.icon.url if ua.achievement.icon else None,
                'badge_color': ua.achievement.badge_color,
                'is_rare': ua.achievement.is_rare
            },
            'earned_at': ua.earned_at,
            'progress_data': ua.progress_data
        } for ua in user_achievements]
        
        return Response({
            'achievements': data,
            'total_points': sum(ua.achievement.points for ua in user_achievements)
        })


class NotificationViewSet(viewsets.ReadOnlyModelViewSet):
    """
    API endpoints for notifications
    """
    serializer_class = NotificationSerializer
    permission_classes = [IsAuthenticated]
    
    def get_queryset(self):
        return Notification.objects.select_related('actor').filter(
            recipient=self.request.user
        ).order_by('-created_at')
    
    @action(detail=True, methods=['post'])
    def mark_read(self, request, pk=None):
        """
        Mark notification as read
        
        POST /api/social/notifications/{id}/mark_read/
        """
        notification = self.get_object()
        notification.mark_as_read()
        
        return Response({'message': 'Notification marked as read'})
    
    @action(detail=False, methods=['post'])
    def mark_all_read(self, request):
        """
        Mark all notifications as read
        
        POST /api/social/notifications/mark_all_read/
        """
        self.get_queryset().filter(is_read=False).update(
            is_read=True,
            read_at=timezone.now()
        )
        
        return Response({'message': 'All notifications marked as read'})
    
    @action(detail=False, methods=['get'])
    def unread_count(self, request):
        """
        Get unread notification count
        
        GET /api/social/notifications/unread_count/
        """
        count = self.get_queryset().filter(is_read=False).count()
        return Response({'unread_count': count}) 


class PublicUserProfileViewSet(viewsets.ReadOnlyModelViewSet):
    """Read-only public user profiles with non-sensitive fields and social counts."""
    serializer_class = PublicUserProfileSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        # All active users are potentially viewable; serializer limits fields
        return CustomUser.objects.filter(is_active=True)

    @action(detail=False, methods=['get'])
    def by_username(self, request):
        """Fetch public profile by username: /api/social/users/public-profile/by_username/?username=..."""
        username = request.query_params.get('username')
        if not username:
            return Response({'error': 'username is required'}, status=status.HTTP_400_BAD_REQUEST)
        try:
            user = CustomUser.objects.get(username=username, is_active=True)
            serializer = self.get_serializer(user)
            return Response(serializer.data)
        except CustomUser.DoesNotExist:
            return Response({'error': 'User not found'}, status=status.HTTP_404_NOT_FOUND)