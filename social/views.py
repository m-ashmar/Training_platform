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
from django.db import transaction
from django.db.models import Q, Exists, F, OuterRef
from django.utils import timezone
from django.utils.translation import gettext as _
from django.contrib.contenttypes.models import ContentType
from django.core.cache import cache

from .models import (
    UserFollow, Post, PostLike, Comment, CommentLike, Challenge,
    ChallengeParticipation, Achievement, UserAchievement
)
from .permissions import IsOwnerOrReadOnly, IsFollowParticipant
from .serializers import (
    UserFollowSerializer, PostSerializer, CommentSerializer,
    ChallengeSerializer, AchievementSerializer, UserAchievementSerializer,
    NotificationSerializer, PublicUserProfileSerializer
)

from users.models import CustomUser
from .tasks import dispatch_notification
import logging

logger = logging.getLogger(__name__)

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
    permission_classes = [IsFollowParticipant]
    
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
                    {'error': _('Cannot follow yourself')}, 
                    status=status.HTTP_400_BAD_REQUEST
                )
            
            follow, created = UserFollow.objects.get_or_create(
                follower=request.user,
                following=user_to_follow
            )
            
            if created:
                # Send Push Notification
                # Emit Domain Event
                from notifications.domain.dispatcher import emit_event
                from notifications.domain.events import UserFollowedEvent
                
                emit_event(UserFollowedEvent(
                    actor_id=request.user.id,
                    target_user_id=user_to_follow.id
                ))
                
                return Response(
                    {'message': _('Successfully followed user')}, 
                    status=status.HTTP_201_CREATED
                )
            else:
                return Response(
                    {'message': _('Already following this user')}, 
                    status=status.HTTP_200_OK
                )
                
        except CustomUser.DoesNotExist:
            return Response(
                {'error': _('User not found')}, 
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
                {'message': _('Successfully unfollowed user')}, 
                status=status.HTTP_200_OK
            )
            
        except CustomUser.DoesNotExist:
            return Response(
                {'error': _('User not found')}, 
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
        
        # No `email`. `UserMinimalSerializer` deliberately dropped it (see
        # social/serializers.py) and these two hand-rolled dicts were never updated, so
        # following someone and reading your own list harvested their address.
        data = [{
            'id': follow.follower.id,
            'username': follow.follower.username,
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
        
        # No `email`. `UserMinimalSerializer` deliberately dropped it (see
        # social/serializers.py) and these two hand-rolled dicts were never updated, so
        # following someone and reading your own list harvested their address.
        data = [{
            'id': follow.following.id,
            'username': follow.following.username,
            'user_type': follow.following.user_type,
            'profile_picture': follow.following.profile_picture.url if follow.following.profile_picture else None,
            'followed_at': follow.created_at
        } for follow in following]
        
        return Response({
            'following': data,
            'count': len(data)
        })


def visible_posts_for(user):
    """Every post `user` is allowed to see.

    ONE definition, because there were two and they disagreed. `Post.can_be_viewed_by`
    (social/models.py) encodes the rule including `is_hidden` and has never had a single
    caller; the querysets in this module encode it without `is_hidden`, and they are what
    actually runs. So moderation did nothing: an admin hid an abusive post and it stayed
    in the feed, in search, and in the detail endpoint for everyone — and every comment
    already on a hidden post stayed world-readable, because the comment queryset filtered
    the post's VISIBILITY and not its hidden flag.

    An author still sees their own hidden post, so hiding is not a silent deletion.
    """
    return Post.objects.filter(
        (
            Q(visibility='public')
            | Q(visibility='followers', author__followers__follower=user)
        ) & Q(is_hidden=False)
        | Q(author=user)
    ).distinct()


class PostViewSet(viewsets.ModelViewSet):
    """
    API endpoints for social posts
    """
    serializer_class = PostSerializer
    permission_classes = [IsOwnerOrReadOnly]
    #: POSTs to a detail route that act ON a post rather than editing it. Without this
    #: `IsOwnerOrReadOnly` refused everyone but the author and the like button returned
    #: 403 for every user on every post they did not write.
    interaction_actions = ('like',)
    filter_backends = [DjangoFilterBackend, SearchFilter, OrderingFilter]
    filterset_fields = ['author', 'post_type', 'visibility']
    search_fields = ['title', 'content']
    ordering_fields = ['created_at', 'updated_at', 'likes_count', 'comments_count']
    ordering = ['-created_at']
    
    def get_queryset(self):
        user = self.request.user
        
        # Get posts that user can view based on visibility AND moderation.
        queryset = visible_posts_for(user)
        
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
    
    def perform_destroy(self, instance):
        """Take the post out of the feeds that carry it, not only out of the table.

        Nothing ever removed an id from a follower's ZSET, so a deleted post stayed there
        for the key's 24-hour life and the reader dropped it after the SQL visibility
        filter — which used to end the feed at that point.
        """
        from .feed_cache import remove_post_from_feeds

        post_id = instance.pk
        follower_ids = list(
            UserFollow.objects.filter(following=instance.author)
            .values_list('follower_id', flat=True))
        super().perform_destroy(instance)
        try:
            remove_post_from_feeds(post_id, follower_ids)
        except Exception:
            # The feed reader tolerates a stale id; this is housekeeping, not correctness.
            logger.debug("could not evict post %s from feeds", post_id, exc_info=True)

    def perform_create(self, serializer):
        post = serializer.save(author=self.request.user)
        # ⚡️ Cache Implementation Phase 3: Push to Fan-Out Queue
        try:
            from .tasks import fan_out_post_root
            fan_out_post_root.delay(post.author.id, post.id, post.created_at.timestamp())
        except Exception:
            # Optional side effect: swallowing this silently is what made the
            # surrounding failures invisible in logs. Control flow is unchanged.
            logger.debug('suppressed non-fatal error', exc_info=True)
    
    @action(detail=True, methods=['post'])
    def like(self, request, pk=None):
        """
        Like/Unlike post
        
        POST /api/social/posts/{id}/like/
        """
        post = self.get_object()
        
        like, created = PostLike.objects.get_or_create(
            user=request.user,
            post=post
        )
        
        from django.db.models import F
        
        if created:
            # Atomic update to avoid race conditions and extra query
            Post.objects.filter(pk=post.pk).update(likes_count=F('likes_count') + 1)
            
            # Logic moved to event dispatcher
            
            # Send Push Notification
            # Emit Domain Event
            from notifications.domain.dispatcher import emit_event
            from notifications.domain.events import PostLikedEvent
            
            # Only emit event if not self-like
            if post.author_id != request.user.id:
                emit_event(PostLikedEvent(
                    actor_id=request.user.id,
                    target_post_id=post.id,
                    post_author_id=post.author_id
                ))
            
            return Response({'message': _('Post liked')})
        else:
            # Unlike the post
            like.delete()
            
            # Atomic decrement, FLOORED at zero. `F('likes_count') - 1` against the
            # row's own `CHECK (likes_count >= 0)` was a 500 whenever the counter and
            # the like rows had drifted apart — which the whole-row `save()` in
            # `perform_create` guaranteed — and the 500 left them drifted.
            Post.objects.filter(pk=post.pk, likes_count__gt=0).update(
                likes_count=F('likes_count') - 1)
            
            return Response({'message': _('Post unliked')})
    
    @action(detail=False, methods=['get'])
    def feed(self, request):
        """
        Get user's social feed utilizing Hybrid ZSET Fan-Out merging.
        
        GET /api/social/posts/feed/?page=1&limit=10
        """
        page = int_param(request.query_params, 'page', default=1, minimum=1)
        limit = int_param(request.query_params, 'limit', default=10, minimum=1, maximum=50)
        offset = (page - 1) * limit
        user_id = request.user.id
        
        import logging
        logger = logging.getLogger(__name__)
        from .feed_cache import get_user_feed
        
        try:
            # 1. Ask Redis for native hybrid global/personal feeds
            post_ids = get_user_feed(user_id, offset, limit)
            
            if not post_ids:
                # An empty ZSET is NOT proof of an empty feed — it is also what a failed
                # or lagging fan-out looks like. Returning [] here turned a worker outage
                # into a permanently blank feed with no error anywhere. Fall through to
                # SQL, which is the same degraded path used when Redis is unreachable.
                raise RuntimeError('empty feed cache — falling back to SQL')
                
            # 2. Leverage get_queryset matching to respect N+1 guards and auth restrictions
            posts_query = self.get_queryset().filter(id__in=post_ids)
            posts_dict = {p.id: p for p in posts_query}
            
            # 3. Order the SQL response precisely mirroring the ZSET temporal sorting
            ordered_posts = [posts_dict[int(i)] for i in post_ids if int(i) in posts_dict]
            
            # `has_more` is about the ZSET, not about what survived the visibility
            # filter. It read `len(ordered_posts) == limit`, and nothing ever ZREMs a
            # post id — deleted, erased and now-invisible posts stay in every follower's
            # sorted set for the key's 24h life — so ONE hole ended the feed: the author
            # deleted a single post, the page came back four rows instead of five,
            # `has_more` went False, and the seven posts behind it were unreachable.
            # Unfollowing someone blanked the feed entirely for the same reason.
            serializer = self.get_serializer(ordered_posts, many=True)
            return Response({
                'posts': serializer.data,
                'page': page,
                'limit': limit,
                'has_more': len(post_ids) == limit
            })
            
        except Exception as e:
            # GRACEFUL FALLBACK if Redis is unavailable or corrupted
            logger.warning("Feed served from SQL fallback: %s", e)
            posts = self.get_queryset()[offset:offset + limit]
            serializer = self.get_serializer(posts, many=True)
            
            return Response({
                'posts': serializer.data,
                'page': page,
                'limit': limit,
                'has_more': len(posts) == limit
            })


from rest_framework.exceptions import ValidationError as DRFValidationError, NotFound
from rest_framework.pagination import CursorPagination

class CommentCursorPagination(CursorPagination):
    """
    Cursor-based pagination for social comments to prevent page drift.
    """
    page_size = 20
    page_size_query_param = 'page_size'
    max_page_size = 50
    ordering = '-created_at'


class CommentViewSet(viewsets.ModelViewSet):
    """
    API endpoints for post comments
    """
    serializer_class = CommentSerializer
    permission_classes = [IsOwnerOrReadOnly]
    interaction_actions = ('like',)
    pagination_class = CommentCursorPagination
    filter_backends = [DjangoFilterBackend, SearchFilter, OrderingFilter]
    filterset_fields = ['post', 'author', 'parent_comment']
    search_fields = ['content']
    ordering_fields = ['created_at', 'updated_at', 'likes_count']
    ordering = ['-created_at']
    
    def get_queryset(self):
        user = self.request.user
        
        # Base queryset with visibility permissions
        # select_related('author','post') is required: CommentSerializer nests the
        # author, so without it the endpoint issued ~1.2 queries per comment
        # (measured 12 queries at 5 rows, 42 at 30 — unbounded with page size).
        queryset = Comment.objects.select_related('author', 'post').annotate(
            # EXISTS subquery instead of a per-row .exists() in the serializer.
            is_liked_annotated=Exists(
                CommentLike.objects.filter(comment=OuterRef('pk'), user=user)
            )
        ).filter(
            # Same rule as the post list, from the same function, so a hidden post's
            # thread cannot be read through the comment endpoint.
            post__in=visible_posts_for(user),
            is_hidden=False,
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
        # The post has to be one this user may READ. The read queryset was scoped by
        # visibility and the write path was not, so a stranger could comment into a
        # private diary post, a followers-only post and a hidden post — 201 every time,
        # bumping the counter and firing a push notification to the author quoting their
        # text. 201-vs-400 was also a post-existence oracle over the whole id space.
        target = serializer.validated_data.get('post')
        if target is not None and not visible_posts_for(self.request.user).filter(
                pk=target.pk).exists():
            raise NotFound(_('No such post.'))

        comment = serializer.save(author=self.request.user)

        # Update post comments count. `F()` on the row, not a full `save()` of an
        # in-memory copy: the whole-row write clobbered likes_count, views_count and
        # shares_count with whatever this request had read, and 30 concurrent likes
        # collapsed to 1.
        Post.objects.filter(pk=comment.post_id).update(
            comments_count=F('comments_count') + 1)
        post = comment.post
        
        # Create notification
        if post.author != self.request.user:
            pass # Logic moved to event dispatcher
            
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
        
        # `update_fields`, not a whole-row `save()` of an in-memory copy: the full save
        # writes every column back from what this request happened to read, so two
        # concurrent likes on one comment lose each other's work. Counted from the rows
        # rather than incremented, so a drifted counter self-heals on the next tap.
        if created:
            Comment.objects.filter(pk=comment.pk).update(
                likes_count=CommentLike.objects.filter(comment=comment).count())
            return Response({'message': _('Comment liked')})
        else:
            like.delete()
            Comment.objects.filter(pk=comment.pk).update(
                likes_count=CommentLike.objects.filter(comment=comment).count())
            return Response({'message': _('Comment unliked')})


from routine.views import StandardResultsSetPagination

class ChallengeViewSet(viewsets.ModelViewSet):
    """
    API endpoints for community challenges
    """
    serializer_class = ChallengeSerializer
    permission_classes = [IsOwnerOrReadOnly]
    interaction_actions = ('join', 'leave', 'update_progress')
    pagination_class = StandardResultsSetPagination
    
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
    
    CHALLENGES_CACHE_TIMEOUT = 120

    def list(self, request, *args, **kwargs):
        """Short-lived per-user cache of the challenges list.

        The key covers the QUERY STRING as well as the user. It did not, despite the
        docstring claiming it did, so the first request of a 120-second window was
        replayed for every later one: ask for page 1 and page 2 is unreachable, ask
        for page 2 first and page 1 is unreachable. Whichever arrived first won.
        """
        try:
            key = self._challenges_cache_key(request.user, request.query_params)
            cached = cache.get(key)
            if cached is not None:
                return Response(cached)
            response = super().list(request, *args, **kwargs)
            if response.status_code == 200 and isinstance(response.data, (list, dict)):
                cache.set(key, response.data, timeout=self.CHALLENGES_CACHE_TIMEOUT)
            return response
        except Exception:
            return super().list(request, *args, **kwargs)

    @staticmethod
    def _challenges_cache_user_part(user):
        return f"user:{user.id}" if user and user.is_authenticated else "anon"

    @classmethod
    def _challenges_cache_version(cls, user):
        """Monotonic per-user version, bumped to invalidate every variant at once.

        One user now has one cache entry PER query string, so a single cache.delete()
        can no longer reach them all — it only ever knew one key. Folding a version
        number into the key means a write orphans every variant in one step, and the
        orphans fall out on their own when their short TTL expires.
        """
        version_key = f"challenges_ver:{cls._challenges_cache_user_part(user)}"
        version = cache.get(version_key)
        if version is None:
            version = 1
            cache.set(version_key, version, timeout=None)
        return version

    @classmethod
    def _challenges_cache_key(cls, user, query_params=None):
        """Cache key for one user's challenges list under one exact query string."""
        import hashlib
        from django.utils.translation import get_language
        from training_platform.i18n import CACHE_VERSION

        lang = get_language() or 'en'
        # Sorted so ?a=1&b=2 and ?b=2&a=1 share an entry; hashed so a caller cannot
        # grow the key space with arbitrarily long query strings.
        items = sorted((query_params or {}).items())
        qs_digest = hashlib.sha256(repr(items).encode('utf-8')).hexdigest()[:16]
        return (
            f"challenges_list:{cls._challenges_cache_user_part(user)}:{lang}:"
            f"{CACHE_VERSION}:v{cls._challenges_cache_version(user)}:{qs_digest}"
        )

    def _invalidate_challenges_cache(self, request):
        """Invalidate every cached variant of this user's challenges list."""
        try:
            version_key = f"challenges_ver:{self._challenges_cache_user_part(request.user)}"
            try:
                cache.incr(version_key)
            except ValueError:
                # No counter yet: seeding it at 2 skips whatever v1 entries exist.
                cache.set(version_key, 2, timeout=None)
        except Exception:
            # Optional side effect: swallowing this silently is what made the
            # surrounding failures invisible in logs. Control flow is unchanged.
            logger.debug('suppressed non-fatal error', exc_info=True)

    def perform_create(self, serializer):
        serializer.save(creator=self.request.user)
        self._invalidate_challenges_cache(self.request)
    
    @action(detail=True, methods=['post'])
    def join(self, request, pk=None):
        """
        Join a challenge

        POST /api/social/challenges/{id}/join/
        """
        challenge = self.get_object()

        # A challenge you can still take part in. `join` checked capacity only, while
        # `update_progress` checked `is_active` — so a client could join one that ended
        # ten days ago, or a cancelled one, and then be unable to log anything against it.
        # `Challenge.is_active` is status AND the date window, which is the whole rule.
        if not challenge.is_active:
            return Response(
                {'error': _('This challenge is not running.'), 'code': 'challenge_closed'},
                status=status.HTTP_400_BAD_REQUEST)

        # Capacity, under the challenge's own row lock. `participants_count` was read
        # and compared outside any lock, so eight real threads all passed a
        # `max_participants` of one and all eight joined.
        with transaction.atomic():
            locked = Challenge.objects.select_for_update().filter(pk=challenge.pk).first()
            if locked and locked.max_participants and (
                    ChallengeParticipation.objects.filter(challenge=locked).count()
                    >= locked.max_participants):
                return Response(
                    {'error': _('Challenge is full')},
                    status=status.HTTP_400_BAD_REQUEST)
            return self._join_locked(request, locked or challenge)

    def _join_locked(self, request, challenge):
        """The join itself, with the challenge row already locked by the caller."""
        participation, created = ChallengeParticipation.objects.get_or_create(
            user=request.user,
            challenge=challenge
        )

        if created:
            # Same reason as the comment counter: a whole-row save clobbers every other
            # column with this request's stale copy.
            Challenge.objects.filter(pk=challenge.pk).update(
                participants_count=ChallengeParticipation.objects.filter(
                    challenge=challenge).count())
            self._invalidate_challenges_cache(request)
            return Response(
                {'message': _('Successfully joined challenge')},
                status=status.HTTP_201_CREATED
            )
        else:
            return Response(
                {'message': _('Already participating in this challenge')},
                status=status.HTTP_200_OK
            )
    
    @action(detail=True, methods=['get'])
    def leaderboard(self, request, pk=None):
        """
        Get challenge leaderboard
        
        GET /api/social/challenges/{id}/leaderboard/
        """
        challenge = self.get_object()
        
        # A total order, and the user joined. `-current_value` alone is not an order:
        # a hundred participants tied on 4.0 meant whichever row Postgres happened to
        # return got rank 1 — and rank 1 is what the Challenge Winner and Champion
        # badges key on. `-current_value, joined_at, id` breaks the tie by who got there
        # first. `select_related` because `p.user.username` was a query per row.
        participants = ChallengeParticipation.objects.filter(
            challenge=challenge
        ).select_related('user').order_by('-current_value', 'joined_at', 'id')[:10]

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
                {'error': _('You are not participating in this challenge. Join first!')}, 
                status=status.HTTP_400_BAD_REQUEST
            )
        
        # Check if challenge is active
        if not challenge.is_active:
            return Response(
                {'error': _('This challenge is not currently active')}, 
                status=status.HTTP_400_BAD_REQUEST
            )
        
        current_value = request.data.get('current_value')
        notes = request.data.get('notes', '')
        
        if current_value is None:
            return Response(
                {'error': _('current_value is required')}, 
                status=status.HTTP_400_BAD_REQUEST
            )
        
        try:
            current_value = float(current_value)
        except (ValueError, TypeError):
            return Response(
                {'error': _('current_value must be a valid number')}, 
                status=status.HTTP_400_BAD_REQUEST
            )

        # A real amount of progress. `float()` alone accepted -500 and 1e12: the first
        # gave a progress percentage of -500 (the `min(100.0, ...)` had no lower bound)
        # and the second completed the challenge instantly. `challenge_wins` in the
        # achievements engine keys on `status='completed'` and `rank=1`, so this fed a
        # badge.
        if current_value != current_value or current_value in (
                float('inf'), float('-inf')):
            return Response(
                {'error': _('current_value must be a valid number')},
                status=status.HTTP_400_BAD_REQUEST)
        if current_value < 0:
            return Response(
                {'error': _('current_value cannot be negative'),
                 'code': 'negative_progress'},
                status=status.HTTP_400_BAD_REQUEST)
        ceiling = float(challenge.target_value or 0) * 100 or 1e9
        if current_value > ceiling:
            return Response(
                {'error': _('current_value is implausibly large for this challenge'),
                 'code': 'progress_out_of_range'},
                status=status.HTTP_400_BAD_REQUEST)
        
        # Update participation progress
        old_value = participation.current_value
        participation.current_value = current_value
        
        # Calculate progress percentage
        if challenge.target_value and challenge.target_value > 0:
            participation.progress_percentage = max(0.0, min(
                100.0, (current_value / challenge.target_value) * 100))
        else:
            participation.progress_percentage = 0.0

        # Completion FOLLOWS the number, in both directions. The branch only ever set
        # `completed`, so a participant who logged 100 and then corrected it to 0 stayed
        # "completed" with zero progress — and stayed eligible for the badge.
        if challenge.target_value:
            reached = current_value >= challenge.target_value
            if reached and participation.status != 'completed':
                participation.status = 'completed'
                participation.completed_at = timezone.now()
            elif not reached and participation.status == 'completed':
                participation.status = 'active'
                participation.completed_at = None
        
        participation.save()
        
        # Update rankings for all participants
        self._update_challenge_rankings(challenge)
        
        # Create notification for significant progress
        if current_value > old_value:
            progress_increase = current_value - old_value
            if progress_increase > 0:
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
            'message': _('Progress updated successfully'),
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


from notifications.models import Notification
from rest_framework.pagination import CursorPagination
from training_platform.query_params import int_param


class NotificationCursorPagination(CursorPagination):
    """
    Cursor-based pagination for notifications.
    
    Cursor pagination is preferred over offset/limit for notifications because:
    1. No page drift when new notifications arrive between page loads.
    2. O(1) keyset seek via the indexed created_at column.
    3. Stable ordering guaranteed by the cursor token.
    
    Query params:
        - page_size: items per page (default 20, max 50)
        - cursor: opaque cursor token from previous response
    """
    page_size = 20
    page_size_query_param = 'page_size'
    max_page_size = 50
    ordering = '-created_at'

    def paginate_queryset(self, queryset, request, view=None):
        # `?page=` belongs to the offset paginator every other list endpoint uses.
        # Cursor pagination ignored it, so a client written against the platform's
        # documented shape paged forever through the same twenty rows and never saw
        # the other hundred. Say so rather than answering page one again.
        if 'page' in request.query_params:
            raise DRFValidationError({
                'page': _(
                    "This endpoint is cursor-paginated. Follow the `next` link "
                    "instead of passing `page`."
                )
            })
        return super().paginate_queryset(queryset, request, view)


class NotificationViewSet(viewsets.ReadOnlyModelViewSet):
    """
    API endpoints for notifications.
    
    Paginated via cursor-based pagination.
    
    GET /api/social/notifications/?page_size=20
    GET /api/social/notifications/?cursor=<token>&page_size=20
    """
    serializer_class = NotificationSerializer
    permission_classes = [IsAuthenticated]
    pagination_class = NotificationCursorPagination
    
    def get_queryset(self):
        # `recipient` is serialised on every row and was not joined, so a page of 20
        # cost 21 queries: one for the notifications and one identical user select per
        # row, for the same user the queryset already filters on.
        qs = Notification.objects.select_related('actor', 'recipient').filter(
            recipient=self.request.user
        ).order_by('-created_at')
        
        # Optional filter by notification type
        notification_type = self.request.query_params.get('type')
        if notification_type:
            qs = qs.filter(event_type=notification_type)
        
        # Optional filter by read status
        is_read = self.request.query_params.get('is_read')
        if is_read is not None:
            qs = qs.filter(is_read=is_read.lower() in ('true', '1', 'yes'))
        
        return qs
    
    @action(detail=True, methods=['post'])
    def mark_read(self, request, pk=None):
        """
        Mark notification as read
        
        POST /api/social/notifications/{id}/mark_read/
        """
        notification = self.get_object()
        if not notification.is_read:
            notification.is_read = True
            notification.save(update_fields=['is_read'])
        
        return Response({'message': _('Notification marked as read')})
    
    @action(detail=False, methods=['post'])
    def mark_all_read(self, request):
        """
        Mark all notifications as read
        
        POST /api/social/notifications/mark_all_read/
        """
        self.get_queryset().filter(is_read=False).update(
            is_read=True
        )
        
        return Response({'message': _('All notifications marked as read')})
    
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
            return Response({'error': _('username is required')}, status=status.HTTP_400_BAD_REQUEST)
        try:
            user = CustomUser.objects.get(username=username, is_active=True)
            serializer = self.get_serializer(user)
            return Response(serializer.data)
        except CustomUser.DoesNotExist:
            return Response({'error': _('User not found')}, status=status.HTTP_404_NOT_FOUND)