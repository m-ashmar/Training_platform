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
    ChallengeParticipation,
)
from .permissions import IsOwnerOrReadOnly, IsFollowParticipant
# Where each participant of a challenge stands: ONE rule, read by every surface
# (BACKEND_BUGS B-086). The module imports no model at import time.
from .standing import STANDING_ORDER, WRITE_BATCH, in_standing, place
from .serializers import (
    UserFollowSerializer, PostSerializer, CommentSerializer,
    ChallengeSerializer, AchievementSerializer, UserAchievementSerializer,
    NotificationSerializer, PublicUserProfileSerializer
)

from users.models import CustomUser
# Blocking is enforced HERE, on the read, not by deleting anything: a block hides two
# people from each other and must be undoable without losing either one's history.
# `blocked_ids` is one query and returns both directions (moderation/services.py).
from moderation.services import blocked_ids
from .tasks import dispatch_notification
import logging

logger = logging.getLogger(__name__)

# Use IsAuthenticated from rest_framework.permissions
IsAuthenticated = permissions.IsAuthenticated


def refuse_a_blocked_follow(follower, target):
    """Raise 403 `user_blocked` when either of the two has blocked the other.

    Both follow paths create a row AND, on `follow_user`, push "X started following
    you" to the target's lock screen. Neither asked about blocks, so the person someone
    had blocked could still follow them and reach them with a push — the inbox hides
    the row, but FCM had already delivered it — and the follow they never accepted
    came back into view the moment the block was lifted. The answer is the same in
    both directions and says nothing about which side blocked whom
    (`moderation/exceptions.py UserIsBlocked`).
    """
    from moderation.exceptions import UserIsBlocked
    from moderation.services import is_blocked

    if is_blocked(follower, target):
        raise UserIsBlocked()


class UserFollowViewSet(viewsets.ModelViewSet):
    """
    API endpoints for user following system

    Permissions:
    - Users can follow/unfollow others
    - Users can view their followers/following

    `POST /api/social/follows/` body `{"following_id": <user id>}` and
    `POST follow_user/` body `{"user_id": <user id>}` both answer 403 `user_blocked`
    when either person has blocked the other — the same code and wording whichever
    side made the block. Unfollowing is never refused. Nothing differs by build.
    Neither follows an account that is not active: `follows/` answers the
    `validation_error` a missing `following_id` does, `follow_user/` 404
    `user_not_found` (see each action for its codes).
    """
    serializer_class = UserFollowSerializer
    permission_classes = [IsFollowParticipant]

    def get_queryset(self):
        # Both columns are excluded: the row names two people, and a block has to
        # remove it whichever side the blocked person is on.
        hidden = blocked_ids(self.request.user)
        return UserFollow.objects.filter(
            Q(follower=self.request.user) | Q(following=self.request.user)
        ).exclude(Q(follower_id__in=hidden) | Q(following_id__in=hidden))

    def perform_create(self, serializer):
        refuse_a_blocked_follow(self.request.user,
                                serializer.validated_data.get('following'))
        serializer.save(follower=self.request.user)

    @staticmethod
    def _target_id(request):
        """The `user_id` of the body as an int, or None when it is not one.

        It went straight into `CustomUser.objects.get(id=...)`, so `"abc"` raised
        ValueError there — a 500 — and an absent id was a 404 (BACKEND_BUGS B-079). A
        JSON array body has no `.get` at all.
        """
        data = request.data if isinstance(request.data, dict) else {}
        raw = data.get('user_id')
        if isinstance(raw, bool):
            return None
        if isinstance(raw, int):
            candidate = raw
        elif isinstance(raw, str) and raw.strip().isascii() and raw.strip().isdigit():
            # ASCII digits only: "²" and "①" pass `isdigit()` and `int()` refuses them —
            # a 500 where the contract says `invalid_user_id` (the review, M9).
            candidate = int(raw.strip())
        else:
            return None
        # A Postgres integer: anything larger is no user, and would be a 500 as a
        # parameter.
        return candidate if 0 < candidate <= 2_147_483_647 else None

    @staticmethod
    def _bad_target():
        return Response({'error': _('user_id must be the id of a user.'),
                         'code': 'invalid_user_id'},
                        status=status.HTTP_400_BAD_REQUEST)

    @staticmethod
    def _no_such_user():
        return Response({'error': _('User not found'), 'code': 'user_not_found'},
                        status=status.HTTP_404_NOT_FOUND)

    @action(detail=False, methods=['post'])
    def follow_user(self, request):
        """
        Follow a user

        POST /api/social/follows/follow_user/
        {
            "user_id": 123
        }

        Auth required; body `{"user_id": <int>}` (a digit string is accepted too).
        Nothing differs by build. Answers:

          201 `{"message"}` — followed (the person is told by a push);
          200 `{"message"}` — you already followed them;
          400 `invalid_user_id` — `user_id` absent, not a positive whole number, or a
              JSON body that is not an object (was a 500 for `"abc"`, a 404 when absent);
          400 `cannot_follow_yourself` — the id is your own;
          403 `user_blocked` — either of you has blocked the other;
          404 `user_not_found` — no such account, OR an account that is not active
              (suspended or erased): the same answer, so it says nothing about which.
              Following an inactive account used to succeed (BACKEND_BUGS B-079).
        """
        user_id = self._target_id(request)
        if user_id is None:
            return self._bad_target()
        try:
            user_to_follow = CustomUser.objects.get(id=user_id, is_active=True)

            if user_to_follow == request.user:
                return Response(
                    {'error': _('Cannot follow yourself'),
                     'code': 'cannot_follow_yourself'},
                    status=status.HTTP_400_BAD_REQUEST
                )

            refuse_a_blocked_follow(request.user, user_to_follow)

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
            return self._no_such_user()

    @action(detail=False, methods=['post'])
    def unfollow_user(self, request):
        """
        Unfollow a user

        POST /api/social/follows/unfollow_user/
        {
            "user_id": 123
        }

        Auth required; body `{"user_id": <int>}`. Nothing differs by build. Never
        refused for a block, and it works on an account that is no longer active, so a
        follow of a suspended or erased account can always be undone. Answers:

          200 `{"message"}` — you do not follow them (whether or not you did before);
          400 `invalid_user_id` — as on `follow_user/` (was a 500 for `"abc"`);
          404 `user_not_found` — no account has that id.
        """
        user_id = self._target_id(request)
        if user_id is None:
            return self._bad_target()
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
            return self._no_such_user()
    
    @action(detail=False, methods=['get'])
    def followers(self, request):
        """
        Get user's followers
        
        GET /api/social/follows/followers/
        """
        followers = UserFollow.objects.filter(
            following=request.user
        ).exclude(follower_id__in=blocked_ids(request.user)).select_related('follower')
        
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
        ).exclude(following_id__in=blocked_ids(request.user)).select_related('following')
        
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


def subquery_count(rows, group_by):
    """A per-row COUNT over `rows`, as ONE correlated subquery for the whole page.

    Every count a viewer is shown has to agree with the rows that viewer can fetch,
    and the rows they cannot fetch are a set (`blocked_ids`) that is the same for the
    whole response. Evaluating that as a serializer method would be one query per row
    of the page; as a subquery annotation it is part of the page's own query, whatever
    the page size — which is the whole point of `blocked_ids` returning a set.

    `Coalesce(..., 0)` because a subquery with no matching rows is NULL, not 0.
    """
    from django.db.models import Count, IntegerField, Subquery
    from django.db.models.functions import Coalesce

    return Coalesce(
        Subquery(
            rows.order_by().values(group_by)
            .annotate(n=Count('pk', distinct=True)).values('n')[:1],
            output_field=IntegerField()),
        0)


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

    Blocking is applied here too, and both ways: a post written by anyone this person
    has blocked, or who has blocked them, is not in the result. One `.exclude` on the
    one definition, so the post list, the feed, search, the detail route, the comment
    queryset and the comment-create guard all inherit it — the same reason `is_hidden`
    lives here rather than in five querysets that disagree.
    """
    return Post.objects.filter(
        (
            Q(visibility='public')
            | Q(visibility='followers', author__followers__follower=user)
        ) & Q(is_hidden=False)
        | Q(author=user)
    ).exclude(author_id__in=blocked_ids(user)).distinct()


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

        # `likes_count` and `comments_count` are denormalised columns that count
        # everyone, so after a block they promised rows the viewer's own like and
        # comment lists no longer return. The serializer subtracts these two; they are
        # annotated here so the correction costs the page one query, not one per post.
        hidden = blocked_ids(user)
        if hidden:
            queryset = queryset.annotate(
                hidden_likes=subquery_count(
                    PostLike.objects.filter(post_id=OuterRef('pk'),
                                            user_id__in=hidden), 'post_id'),
            )

        # The comment badge has a second way of over-reporting, and it does not need a
        # block to happen: `CommentViewSet` serves `is_hidden=False` only, and nothing
        # decrements `comments_count` when a moderator hides one. A post whose only
        # reply was hidden still read "1 comment" and opened on an empty thread, for
        # every viewer — this is the correction blocks alone never covered.
        #
        # ONE subquery for both exclusions rather than a second annotation beside
        # `hidden_likes`: a blocked author's hidden comment is a single unreadable row
        # and must be subtracted once, and two counts added together would subtract it
        # twice. Annotated unconditionally, because moderation does not depend on who
        # is looking — and it is still part of the page's own query, not a query per
        # post.
        unreadable = Q(is_hidden=True)
        if hidden:
            unreadable |= Q(author_id__in=hidden)
        queryset = queryset.annotate(
            hidden_comments=subquery_count(
                Comment.objects.filter(post_id=OuterRef('pk')).filter(unreadable),
                'post_id'),
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
    # `-id` breaks ties: rows written in the same instant came back in whatever order
    # the database chose on each query, so a page boundary between them repeated one and
    # dropped another (BACKEND_BUGS B-016).
    ordering = ('-created_at', '-id')


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
    # What the cursor actually pages by (OrderingFilter's ordering wins over the
    # paginator's): `-id` breaks ties (B-016).
    ordering = ['-created_at', '-id']
    
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
        ).exclude(
            # Its OWN block filter, not only the one inherited through the post. The
            # post clause hides a blocked person's posts; it does nothing about a
            # blocked person's comments on YOUR post, which is where the harassment
            # this feature exists for actually lands.
            author_id__in=blocked_ids(user),
        ).distinct()

        # Same correction as the post list, for the same reason: `Comment.likes_count`
        # is denormalised and counts a blocked person's like. One subquery for the
        # page, not one query per comment.
        hidden = blocked_ids(user)
        if hidden:
            queryset = queryset.annotate(
                hidden_likes=subquery_count(
                    CommentLike.objects.filter(comment_id=OuterRef('pk'),
                                               user_id__in=hidden), 'comment_id'),
            )

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
    
    def perform_destroy(self, instance):
        """Delete the comment and take it off its post's `comments_count`.

        `perform_create` adds one to the counter and nothing ever took one off, so a
        deleted comment stayed on the badge for good — the post read "1 comment" and
        opened on an empty thread. S-033 corrected the badge for hidden and blocked
        rows, which are still in the table; a deleted row is not, so it has to leave
        the counter here.

        A reply is deleted with its parent (`parent_comment` cascades), so the counter
        drops by every Comment row the delete removed, not by one. `F()` on the row, as
        the create does, so two deletes on one post cannot lose each other's work; and
        floored at zero, because the column is `CHECK (comments_count >= 0)` and a
        counter that had already drifted must not turn a delete into a 500.
        """
        from django.db.models import IntegerField, Value
        from django.db.models.functions import Greatest

        post_id = instance.post_id
        with transaction.atomic():
            _total, per_model = instance.delete()
            removed = per_model.get(Comment._meta.label, 0)
            if removed:
                Post.objects.filter(pk=post_id).update(
                    comments_count=Greatest(F('comments_count') - Value(removed),
                                            Value(0), output_field=IntegerField()))

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

    Auth required. Nothing differs by build. A challenge row (list, detail, create,
    update) is `ChallengeSerializer`; since 2026-09-24 it also carries `metric` —
    `manual` | `volume_kg` | `sets` | `workouts` | `training_days`, writable, default
    `manual` — and `auto_counted` (read-only, `metric != "manual"`): when true the
    server counts the participants' progress from the training they log
    (`social/challenge_progress.py`), `update_progress` refuses a hand-typed number
    (`progress_auto_counted`), and the app shows «ابدأ تمرين اليوم» instead of the
    log sheet (BACKEND_BUGS B-075).

    `user_progress` — `{current_value, progress_percentage, rank}` of the caller, or
    null when they have not joined. `rank` is counted live by the one rule every
    standing surface reads (`social/standing.py`, BACKEND_BUGS B-086): 1 + the number
    of participants strictly ahead — more progress, then an earlier join, then a lower
    participation id — so two people on the same number hold different places, the
    earlier joiner first. Null for someone who dropped out.
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
            # Not one an operator hid (SIDE_BUGS S-065) — for everyone, its creator
            # included, the way a hidden post is served to nobody.
            Challenge.objects.filter(is_hidden=False)
            .select_related('creator')
            .only(
                'id', 'title', 'description', 'challenge_type',
                'target_value', 'unit', 'start_date', 'end_date',
                'max_participants', 'participants_count', 'status',
                'created_at', 'rules', 'reward_description', 'image', 'metric',
                'creator__id', 'creator__username', 'creator__email',
                'creator__user_type', 'creator__profile_picture',
            )
            # `-created_at, -id`, never `-created_at` alone. A single non-unique
            # sort column is not a total order, so LIMIT/OFFSET paging repeats
            # and hides rows whenever two records share the value — which a
            # fan-out batch guarantees. Every model here declares the tiebreaker
            # in `Meta.ordering`; a view-level `.order_by()` OVERRIDES it, and
            # the sweep that fixed the models never reached these call sites.
            # Reproduced: 50 notifications sharing a timestamp served `b-1` on
            # page 1 and again on page 2, and `b-6` on neither.
            .order_by('-created_at', '-id')
        )
        user = getattr(self, 'request', None).user if getattr(self, 'request', None) else None
        if user and user.is_authenticated:
            part_qs = ChallengeParticipation.objects.filter(user=user, challenge=OuterRef('pk'))
            qs = qs.annotate(
                is_joined_anno=Exists(part_qs),
                cur_value_anno=Subquery(part_qs.values('current_value')[:1]),
                prog_pct_anno=Subquery(part_qs.values('progress_percentage')[:1]),
                # Counted live by THE rule (`social/standing.py`), not read from the
                # stored column: the column is only as fresh as its last write, and the
                # Feed card read 2 while the board and the home card said 3 (B-086).
                rank_anno=Subquery(in_standing(part_qs).annotate(place=place())
                                   .values('place')[:1]),
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
        self._bump_challenges_cache_version(
            f"challenges_ver:{self._challenges_cache_user_part(request.user)}")

    @classmethod
    def invalidate_challenges_cache_for(cls, user_id):
        """The same, for a person who is not the one asking — the one whose progress a
        recount of their logged training just moved (`social/challenge_progress.py`)."""
        cls._bump_challenges_cache_version(f"challenges_ver:user:{user_id}")

    @staticmethod
    def _bump_challenges_cache_version(version_key):
        try:
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

    def perform_update(self, serializer):
        """Save, and when this edit is what closed the challenge, check its winner.

        The Challenge Winner badge counts a first place only once the challenge is over
        (`achievements/engine.py challenge_wins`, SIDE_BUGS S-044). The hourly phase
        task checks the winners of the challenges the clock closes; a creator closing
        one early (`status: "completed"`, or an end date moved into the past) closes it
        here, so the check is queued here too — on commit, after the close is visible.
        """
        before = serializer.instance
        was_over = before.lifecycle_status == 'completed'
        measured = (before.metric, before.start_date, before.end_date)
        target = before.target_value
        challenge = serializer.save()
        if challenge.target_value != target and not was_over:
            # Completion follows the target as it follows the number (the review, M6).
            from .challenge_progress import reapply_target

            reapply_target(challenge)
        if not was_over and challenge.lifecycle_status == 'completed':
            from .tasks import award_challenge_winners

            transaction.on_commit(lambda: award_challenge_winners([challenge.pk]))
        # What the challenge counts, or over which days, changed while it runs: every
        # participant's number is recounted (one grouped query) and the standing
        # re-ranked, rather than waiting for each of them to log again (B-075).
        if (challenge.auto_counted and challenge.is_active
                and (challenge.metric, challenge.start_date, challenge.end_date) != measured):
            from .challenge_progress import recount

            with transaction.atomic():
                recount(challenge)
                self._update_challenge_rankings(challenge)

    @action(detail=True, methods=['post'])
    def join(self, request, pk=None):
        """
        Join a challenge

        POST /api/social/challenges/{id}/join/

        Body: none. Nothing differs by build. Answers:
          `201` `{"message"}` — joined. On an `auto_counted` challenge the training
                you already logged inside its window is counted at once.
          `200` `{"message"}` — you were already in it
          `400` `{"error", "code": "challenge_closed"}` — not running: not started
                yet, over, or cancelled
          `400` `{"error", "code": "challenge_full"}` — `max_participants` reached (it
                answered the bare `bad_request` default before, BACKEND_BUGS B-076)
          `404` — no such challenge, or one an operator hid
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
                    {'error': _('Challenge is full'), 'code': 'challenge_full'},
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
            # What they already trained inside the window counts from the start (B-075).
            if challenge.auto_counted:
                from .challenge_progress import recount

                recount(challenge, [request.user.id])
            # Rank the newcomer now. Ranks used to be written only by
            # `update_progress`, so anyone who joined and logged nothing kept
            # `rank=NULL` — last place is a place, and the app reads the column.
            self._update_challenge_rankings(challenge)
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
    
    @action(detail=True, methods=['post'])
    def leave(self, request, pk=None):
        """
        Leave a challenge you joined

        POST /api/social/challenges/{id}/leave/

        Body: none. The participation of the CALLING user is the one removed, so there
        is nothing to name.

        `interaction_actions` has advertised this action since the permission class was
        written, and the viewset never defined it: the route did not exist, so joining
        a challenge was permanent and `participants_count` only ever grew. Leaving
        deletes the participation row, recounts `participants_count` from the rows
        (never `- 1` on a column that has drifted), and closes the gap the departure
        leaves in the ranking.

        Answers:
          `200` `{"message", "code": "left_challenge"}`
          `400` `{"error", "code": "not_participating"}` — you had not joined
          `400` `{"error", "code": "challenge_closed"}` — the challenge is over or was
                cancelled. A finished challenge is a record: its board, and the
                Challenge Winner badge that keys on `rank=1`, do not change afterwards.
          `404` — no such challenge

        Nothing differs by build.
        """
        challenge = self.get_object()

        # Same rule and the same `code` as `join`. Reused deliberately: it means the
        # same thing to the reader ("this challenge is not running"), and a shipped
        # code is never renamed.
        if challenge.lifecycle_status in Challenge.TERMINAL_STATUSES:
            return Response(
                {'error': _('This challenge is not running.'), 'code': 'challenge_closed'},
                status=status.HTTP_400_BAD_REQUEST)

        with transaction.atomic():
            # The same row `join` locks, so a join and a leave racing each other cannot
            # both recount from the same stale snapshot.
            locked = Challenge.objects.select_for_update().filter(
                pk=challenge.pk).first() or challenge
            deleted, _rows = ChallengeParticipation.objects.filter(
                challenge=locked, user=request.user).delete()
            if not deleted:
                return Response(
                    {'error': _('You are not participating in this challenge.'),
                     'code': 'not_participating'},
                    status=status.HTTP_400_BAD_REQUEST)
            Challenge.objects.filter(pk=locked.pk).update(
                participants_count=ChallengeParticipation.objects.filter(
                    challenge=locked).count())
            self._update_challenge_rankings(locked)

        self._invalidate_challenges_cache(request)
        return Response({'message': _('You have left this challenge.'),
                         'code': 'left_challenge'})

    @action(detail=True, methods=['get'])
    def leaderboard(self, request, pk=None):
        """
        Get challenge leaderboard

        GET /api/social/challenges/{id}/leaderboard/?limit=10

        Auth required; no body; nothing differs by build. `limit` 1..50, default 10
        (outside that range it is clamped; not a number: `400 validation_error`).
        Answers `200`:

            {"challenge": "<title>",
             "auto_counted": true,
             "leaderboard": [{"rank": 1,
                              "user": {"id", "username", "first_name", "last_name",
                                       "user_type", "profile_picture"},
                              "current_value": 48900.0,
                              "progress_percentage": 97.8}, …],
             "me": {… the same shape, for the caller …} | null}

        `rank` is THE place (`social/standing.py`): 1 + the participants strictly
        ahead — more progress, then an earlier join, then a lower participation id —
        the same number `user_progress.rank` on the challenge and the home card's
        `challenge_rank` answer (BACKEND_BUGS B-086). Rows are listed in that order.
        `user.user_type` is what draws the trainer's bronze ring; `profile_picture`
        is an absolute URL or null (B-074).

        `me` is the caller's own row whether or not it made the list — the app pins it
        below the board when it did not — and null when the caller has not joined or
        dropped out (someone who dropped holds no place and is never listed).

        An account the caller has blocked, or that has blocked them, is left out of
        `leaderboard` AFTER the places are counted: everyone else keeps their true
        `rank` and the list may be shorter than `limit`.

        `404` — no such challenge, or one an operator hid.
        """
        challenge = self.get_object()
        limit = int_param(request.query_params, 'limit', default=10, minimum=1, maximum=50)

        # The places come from `place()`, the one rule every surface reads; the rows are
        # listed in `STANDING_ORDER`, the order it counts in. `select_related` because
        # the user's names and picture were a query per row.
        standing = (in_standing(ChallengeParticipation.objects.filter(challenge=challenge))
                    .select_related('user').annotate(place=place()))
        participants = list(standing.order_by(*STANDING_ORDER)[:limit])
        mine = next((p for p in participants if p.user_id == request.user.id), None)
        if mine is None:
            mine = standing.filter(user=request.user).first()

        storage = CustomUser._meta.get_field('profile_picture').storage

        def row(p):
            person = p.user
            picture = person.profile_picture.name if person.profile_picture else None
            return {
                'rank': p.place,
                'user': {
                    'id': person.id,
                    'username': person.username,
                    'first_name': person.first_name,
                    'last_name': person.last_name,
                    'user_type': person.user_type,
                    'profile_picture': (request.build_absolute_uri(storage.url(picture))
                                        if picture else None),
                },
                'current_value': p.current_value,
                'progress_percentage': p.progress_percentage,
            }

        # Blocked people are dropped from the board AFTER the rank is computed, so the
        # ranks stay true — the reader sees 1, 2, 4 with a gap rather than someone else
        # promoted into a place they did not earn. Excluding them from the query would
        # have renumbered the whole board differently for every viewer, and the
        # Champion badge keys on rank 1.
        hidden = blocked_ids(request.user)
        return Response({
            'challenge': challenge.title,
            'auto_counted': challenge.auto_counted,
            'leaderboard': [row(p) for p in participants if p.user_id not in hidden],
            'me': row(mine) if mine is not None else None,
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

        The manual log, for a challenge the server cannot measure (`auto_counted`
        false). Auth required; body `{"current_value": <number>, "notes"?}` — the
        caller's total so far, not an increment. Nothing differs by build. Answers:

          `200` `{"message", "participation": {current_value, progress_percentage,
                status, rank, target_value, unit}}` — `rank` is the caller's place by
                the one rule (`social/standing.py`), after this log
          `400` `{"error", "code"}`, `code` one of:
                `not_participating` — you have not joined (the code `leave` answers)
                `challenge_closed` — not running: not started yet, over, or cancelled
                    (the code `join` and `leave` answer)
                `progress_auto_counted` — the server counts this challenge from your
                    logged training; there is nothing to type in
                `progress_required` — no `current_value`
                `invalid_progress` — `current_value` is not a finite number
                `negative_progress` — below zero
                `progress_out_of_range` — more than a hundred times the target
          `404` — no such challenge, or one an operator hid

        The first five codes are new (2026-09-24, BACKEND_BUGS B-076): those refusals
        answered only the `bad_request` default before. The checks run in that order.
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
                {'error': _('You are not participating in this challenge. Join first!'),
                 'code': 'not_participating'},
                status=status.HTTP_400_BAD_REQUEST
            )

        # Check if challenge is active
        if not challenge.is_active:
            return Response(
                {'error': _('This challenge is not currently active'),
                 'code': 'challenge_closed'},
                status=status.HTTP_400_BAD_REQUEST
            )

        # Counted from logged training (B-075): a hand-typed number would be overwritten
        # by the next set anyway, and taking it would let anyone type their way up.
        if challenge.auto_counted:
            return Response(
                {'error': _('This challenge is counted from the training you log, so '
                            'there is nothing to enter by hand.'),
                 'code': 'progress_auto_counted'},
                status=status.HTTP_400_BAD_REQUEST)

        data = request.data if isinstance(request.data, dict) else {}
        current_value = data.get('current_value')
        notes = data.get('notes', '')

        if current_value is None:
            return Response(
                {'error': _('current_value is required'), 'code': 'progress_required'},
                status=status.HTTP_400_BAD_REQUEST
            )

        try:
            current_value = float(current_value)
        except (ValueError, TypeError):
            return Response(
                {'error': _('current_value must be a valid number'),
                 'code': 'invalid_progress'},
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
                {'error': _('current_value must be a valid number'),
                 'code': 'invalid_progress'},
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
        
        # Update participation progress: the value, its percentage and the completion,
        # by the ONE implementation the counted progress uses too
        # (`challenge_progress.apply_value`). Completion FOLLOWS the number, in both
        # directions: the branch here only ever set `completed`, so a participant who
        # logged 100 and then corrected it to 0 stayed "completed" with zero progress —
        # and stayed eligible for the badge.
        from .challenge_progress import apply_value

        old_value = participation.current_value
        apply_value(participation, challenge, current_value)

        # The row and the whole standing move together, under the CHALLENGE's lock.
        # Two people logging progress at the same moment each computed the board from
        # their own snapshot and wrote it, so the later writer could publish an order
        # that never existed. Serialising on the one row every participant shares makes
        # the recompute a single-writer operation; `join` already locks the same row
        # for its capacity check, so the lock order is unchanged.
        with transaction.atomic():
            Challenge.objects.select_for_update().filter(pk=challenge.pk).first()
            participation.save()
            places = self._update_challenge_rankings(challenge)
        # The caller's own place, from the pass that just ran: `participation` is a
        # stale copy the moment `bulk_update` writes, and the response quotes `rank`.
        participation.rank = places.get(participation.pk, participation.rank)

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
    
    #: The order the standing is listed in — `social/standing.py STANDING_ORDER`, named
    #: here as well because this is where it lived before it moved there.
    RANKING_ORDER = STANDING_ORDER
    RANKING_BATCH = WRITE_BATCH

    def _update_challenge_rankings(self, challenge):
        """Write `rank` for EVERY participant, in one pass, and return {id: rank}.

        What was here wrote the column only from `update_progress`, ordered by
        `-current_value` alone, with a full-row `save()` per participant. Three
        consequences, and a badge on the end of them:

        * someone who joined and never logged was never in the loop's effect at all —
          their `rank` stayed NULL for the life of the challenge;
        * `-current_value` is not a total order, so two tied participants were handed
          places in whatever sequence Postgres returned them, and swapped between runs;
        * `achievements/engine.py` `challenge_wins` counts participations with
          `status='completed'` AND `rank=1`, so the "Challenge Winner" badge was
          awarded off that ordering.

        It is `social/standing.py write_places` now: the column is written from
        `place()`, the ONE rule the leaderboard, `user_progress.rank` and the home
        card also read (BACKEND_BUGS B-086), so the four cannot disagree. Every
        participant still in the standing is placed, including the ones at zero; a
        dropped one is NULL (`NOT_IN_STANDING`, SIDE_BUGS S-045). One SELECT and one
        bulk UPDATE of the rows whose place moved. **A challenge that is over is not
        re-ranked**: the standing is a record from that moment, and a late `leave`
        must not move the Challenge Winner badge.
        """
        from home.views import bump_stamp

        from .standing import write_places

        # The people whose place moved have the old one cached too (the review, M5).
        movers: set = set()
        places = write_places(challenge, moved_users=movers)
        for other in sorted(movers):
            type(self).invalidate_challenges_cache_for(other)
            bump_stamp(other)
        return places


class AchievementViewSet(viewsets.ReadOnlyModelViewSet):
    """The badges, and the ones the caller has earned — read from the `achievements` app.

    GET /api/social/achievements/                     every active badge (paginated)
    GET /api/social/achievements/{id}/                one badge
    GET /api/social/achievements/user_achievements/   the caller's earned badges:
        {"achievements": [{"achievement": {id, name, description, category, points,
                                           icon, badge_color, is_rare},
                           "earned_at", "progress_data"}],
         "total_points": int}

    Auth required; no body, no special headers; nothing differs by build. No `code`
    beyond the platform's generic ones (401, 404 for an unknown or secret id).

    These three routes used to read `social.Achievement` / `social.UserAchievement`,
    which nothing live writes — the badge engine is `achievements/engine.py` and it
    awards `achievements.UserAchievement` — so `user_achievements/` answered an empty
    list and 0 points to everyone, for ever (SIDE_BUGS S-127). They now read the same tables
    `GET /api/achievements/my/` does, in the same response shape as before; the ids are
    therefore the `achievements` app's ids, the ones `/api/achievements/` uses.

    A secret badge is listed only to someone who has earned it, the rule
    `/api/achievements/` applies: the list used to hand every secret badge and its
    criteria to anyone who asked.
    """
    serializer_class = AchievementSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        from achievements.models import Achievement as Badge
        from achievements.models import UserAchievement as Earned

        earned = Earned.objects.filter(user=self.request.user).values('achievement_id')
        return (Badge.objects.filter(is_active=True)
                .filter(Q(is_secret=False) | Q(pk__in=earned)))

    @action(detail=False, methods=['get'])
    def user_achievements(self, request):
        """
        Get user's earned achievements

        GET /api/social/achievements/user_achievements/
        """
        from achievements.models import UserAchievement as Earned

        user_achievements = list(Earned.objects.filter(
            user=request.user
        ).select_related('achievement').order_by('-earned_at', '-id'))

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
    # `-id` breaks ties: rows written in the same instant came back in whatever order
    # the database chose on each query, so a page boundary between them repeated one and
    # dropped another (BACKEND_BUGS B-016).
    ordering = ('-created_at', '-id')

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
        # `-created_at, -id`: the model declares that tiebreaker in `Meta.ordering` and
        # a view-level `.order_by()` overrides it. Without the id, a fan-out batch that
        # shares a timestamp made cursor pages repeat one row and skip another.
        # A blocked person's like, comment or follow must not keep arriving as a
        # notification — that is the one place a block is most obviously broken.
        # `actor` is nullable (system notices have none) and `__in` never matches
        # NULL, so those are untouched.
        qs = Notification.objects.select_related('actor', 'recipient').filter(
            recipient=self.request.user
        ).exclude(
            actor_id__in=blocked_ids(self.request.user)
        ).order_by('-created_at', '-id')
        
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
    """Read-only public user profiles with non-sensitive fields and social counts.

    GET /api/social/users/public-profile/{id}/
    GET /api/social/users/public-profile/by_username/?username=<exact username>
    GET /api/social/users/public-profile/search/?q=<text>      (paginated, below)

    Auth required; no body; nothing differs by build. A profile row is
    `PublicUserProfileSerializer`: `id, username, user_type, first_name, last_name,
    profile_picture_url, followers_count, following_count, posts_count, is_following`
    and, since 2026-09-24 (BACKEND_BUGS B-077), `featured_achievements` (at most three:
    `{id, key, name, category, points, icon_url, badge_color, is_rare, earned_at}`, `id`
    the achievement's; a secret badge only on your own profile), `community_points`
    (the points of every badge earned — what the community-points leaderboard ranks)
    and `member_since` (`"YYYY-MM-01"`, the month joined).

    An inactive account (suspended or erased), and anyone the caller has blocked or who
    has blocked the caller, is not served: 404 `not_found` on the detail route, and
    `by_username/` answers 404 `{"error": "User not found"}`. The counts agree with the
    rows the caller can fetch (blocks, hidden posts).
    """
    serializer_class = PublicUserProfileSerializer
    permission_classes = [IsAuthenticated]

    #: The shortest `q` that `search/` answers. Two, because an Arabic given name can be
    #: two letters and a username prefix of one letter is the whole directory.
    SEARCH_MIN_LENGTH = 2
    #: Longer than any username (150) or full name (30 + 1 + 30) can match.
    SEARCH_MAX_LENGTH = 150
    #: Who a search lists: members of the community. Staff and top-up agents keep a
    #: public profile, but a stranger has no reason to go looking for one by name.
    SEARCH_USER_TYPES = ('client', 'trainer')

    def _hidden(self):
        """`blocked_ids` for this request, asked once (one query, both directions)."""
        if not hasattr(self, '_hidden_ids'):
            self._hidden_ids = blocked_ids(self.request.user)
        return self._hidden_ids

    def _visible_people(self):
        """Every account this viewer may see: active, and neither side blocking."""
        return CustomUser.objects.filter(is_active=True).exclude(id__in=self._hidden())

    def _with_follow_figures(self, queryset):
        """`is_following` and `followers_count`, for the whole page in its own query."""
        user = self.request.user
        hidden = self._hidden()
        return queryset.annotate(
            # `is_following` ran an `.exists()` per row, which made the page's cost
            # grow with the page and hid whether the counts below cost anything.
            is_following_anno=Exists(
                UserFollow.objects.filter(following_id=OuterRef('pk'),
                                          follower=user)),
            followers_count_anno=subquery_count(
                UserFollow.objects.filter(following_id=OuterRef('pk')).exclude(
                    follower_id__in=hidden), 'following_id'),
        )

    def get_queryset(self):
        # All active users are potentially viewable; serializer limits fields.
        # Except the ones this person has blocked and the ones who have blocked them:
        # a profile that still opens is a block that did nothing.
        from django.db.models import IntegerField, Prefetch, Subquery, Sum
        from django.db.models.functions import Coalesce
        from achievements.models import UserAchievement as Earned
        from .serializers import featured_badges_queryset

        user = self.request.user
        hidden = self._hidden()
        queryset = self._with_follow_figures(self._visible_people())

        # The three counts, annotated for the whole page rather than counted per row.
        # Unfiltered they contradicted the lists they summarise: a profile claiming 12
        # followers next to a follower list of 11 tells the viewer the account they
        # blocked is still following — which is the one thing the block had to stop.
        return queryset.annotate(
            following_count_anno=subquery_count(
                UserFollow.objects.filter(follower_id=OuterRef('pk')).exclude(
                    following_id__in=hidden), 'follower_id'),
            # Through the one definition of what this viewer may read, so the number
            # cannot drift from `GET /api/social/posts/?author=<id>`.
            posts_count_anno=subquery_count(
                visible_posts_for(user).filter(author_id=OuterRef('pk')), 'author_id'),
            # The community points (B-077): the sum the achievements leaderboard ranks.
            community_points_anno=Coalesce(
                Subquery(Earned.objects.filter(user_id=OuterRef('pk')).order_by()
                         .values('user_id').annotate(total=Sum('achievement__points'))
                         .values('total')[:1], output_field=IntegerField()),
                0, output_field=IntegerField()),
        ).prefetch_related(
            # The featured badges of every profile on the page, in ONE query.
            Prefetch('earned_achievements', queryset=featured_badges_queryset(user),
                     to_attr='featured_badges'),
        )

    @action(detail=False, methods=['get'])
    def by_username(self, request):
        """Fetch public profile by username: /api/social/users/public-profile/by_username/?username=...

        The EXACT username, case-sensitive — use `search/` to find people.
        400 `{"error"}` without `username`; 404 `{"error"}` when no visible account has
        it. Otherwise the profile, as the detail route answers it.
        """
        username = request.query_params.get('username')
        if not username:
            return Response({'error': _('username is required')}, status=status.HTTP_400_BAD_REQUEST)
        try:
            # Through `get_queryset()`, not `CustomUser.objects`: this route bypassed
            # the viewset's queryset entirely, so a blocked person stayed reachable by
            # name even once their profile page was gone.
            user = self.get_queryset().get(username=username)
            serializer = self.get_serializer(user)
            return Response(serializer.data)
        except CustomUser.DoesNotExist:
            return Response({'error': _('User not found')}, status=status.HTTP_404_NOT_FOUND)

    @action(detail=False, methods=['get'])
    def search(self, request):
        """Find people: GET /api/social/users/public-profile/search/?q=<text>&page=<n>

        The Find screen's people search (BACKEND_BUGS B-081) — `by_username/` only ever
        matched a whole username, case-sensitively. Auth required; no body; nothing
        differs by build.

        `q` — at least `SEARCH_MIN_LENGTH` (2) characters after trimming spaces and a
        leading `@`, at most 150. It matches, case-insensitively, the START of the
        username, of the first name, of the last name, or of "first last" (so «ليث الن»
        finds ليث النجار). `%` and `_` are ordinary characters.

        Listed: active, verified members (`client` and `trainer` accounts) other than
        the caller, and never anyone the caller has blocked or who has blocked the
        caller — a subset of the people whose profile the caller could open; staff,
        top-up agents and signups that never verified are not listed. Order: an exact username first, then usernames that start
        with `q`, then name matches; each group by username, then id.

        Paginated, the project's one shape: `{count, next, previous, results}`, 25 a
        page (`page_size` up to 100). A row is a SUBSET of the public profile —
        `{id, username, user_type, first_name, last_name, profile_picture_url,
        followers_count, is_following, member_since}` — so a search tells nobody more
        than the profile would.

        `400 {"error", "code": "query_too_short", "min_length": 2}` — `q` absent or
        shorter than that. A bad `page` / `page_size` is the paginator's 400.
        """
        from django.db.models import Case, IntegerField, Value, When
        from django.db.models.functions import Concat
        from .serializers import PublicUserSearchSerializer

        raw = request.query_params.get('q') or ''
        query = raw.strip().lstrip('@').strip()[:self.SEARCH_MAX_LENGTH]
        if len(query) < self.SEARCH_MIN_LENGTH:
            return Response(
                {'error': _('Type at least %(count)d characters to search.')
                 % {'count': self.SEARCH_MIN_LENGTH},
                 'code': 'query_too_short', 'min_length': self.SEARCH_MIN_LENGTH},
                status=status.HTTP_400_BAD_REQUEST)

        people = (self._visible_people()
                  .filter(email_verified=True, user_type__in=self.SEARCH_USER_TYPES)
                  .exclude(pk=request.user.pk)
                  .annotate(search_name=Concat('first_name', Value(' '), 'last_name'))
                  .filter(Q(username__istartswith=query)
                          | Q(first_name__istartswith=query)
                          | Q(last_name__istartswith=query)
                          | Q(search_name__istartswith=query))
                  .annotate(match_rank=Case(
                      When(username__iexact=query, then=Value(0)),
                      When(username__istartswith=query, then=Value(1)),
                      default=Value(2), output_field=IntegerField()))
                  .order_by('match_rank', 'username', 'id'))
        page = self.paginate_queryset(self._with_follow_figures(people))
        serializer = PublicUserSearchSerializer(page, many=True,
                                                context=self.get_serializer_context())
        return self.get_paginated_response(serializer.data)
