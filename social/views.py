"""
Social Features API Views

This module provides REST API endpoints for social networking features
including following, posts, comments, likes, challenges, and achievements.
"""

from rest_framework import viewsets, status, permissions
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.exceptions import APIException
from rest_framework.filters import SearchFilter, OrderingFilter
from django_filters.rest_framework import DjangoFilterBackend
from django.db import transaction
from django.db.models import Q, Exists, F, OuterRef
from django.utils import timezone
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy as _lazy
from django.contrib.contenttypes.models import ContentType
from django.core.cache import cache

from .models import (
    UserFollow, Post, PostLike, Comment, CommentLike, Challenge,
    ChallengeParticipation,
)
from .permissions import IsOwnerOrReadOnly, IsFollowParticipant
# Where each participant of a challenge stands: ONE rule, read by every surface
# (BACKEND_BUGS B-086). The module imports no model at import time.
from .standing import (STANDING_ORDER, WRITE_BATCH, active_participants_count, in_standing,
                       place)
from .serializers import (
    UserFollowSerializer, PostSerializer, CommentSerializer,
    ChallengeSerializer, AchievementSerializer, UserAchievementSerializer,
    NotificationSerializer, PublicUserProfileSerializer
)

from users.models import CustomUser
# Who may be shown to other people: ONE predicate, read by every surface below (the
# owner's decision 8, 2026-09-24). A suspended or erased account is not active, and
# leaves the boards, the feed, the threads, the follow lists and search while it is.
from users.visibility import hidden_q, public_people, public_q
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


class AlreadyFollowing(APIException):
    """400 `already_following` — `POST follows/` for a follow the caller already has."""

    status_code = status.HTTP_400_BAD_REQUEST
    default_code = 'already_following'
    default_detail = _lazy('You already follow this person.')


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

    **A suspended account is not listed** (the owner's decision 8, 2026-09-24):
    `GET follows/`, `followers/` and `following/` leave out every follow whose other
    person is not publicly visible (`users/visibility.py` — suspended or erased), and
    the counts on a public profile agree. The follow rows are kept: reinstating the
    account lists them again. `unfollow_user/` still undoes a follow of such an
    account; `DELETE follows/{id}/` answers 404 `not_found` for one, as for any row the
    caller cannot see.

    Every refusal names its `code` (the owner's decision 9, 2026-09-24):
    `DELETE`/`PATCH follows/{id}/` of a follow the caller did not make is 403
    `not_your_follow` (was `permission_denied`); `POST follows/` with your own id is
    400 `validation_error` with the field code `cannot_follow_yourself` on
    `following_id`, and a follow you already have is 400 `already_following`.
    """
    serializer_class = UserFollowSerializer
    permission_classes = [IsFollowParticipant]

    def get_queryset(self):
        # Both columns are excluded: the row names two people, and a block has to
        # remove it whichever side the blocked person is on.
        hidden = blocked_ids(self.request.user)
        return UserFollow.objects.filter(
            Q(follower=self.request.user) | Q(following=self.request.user)
        ).exclude(Q(follower_id__in=hidden) | Q(following_id__in=hidden)).filter(
            # Both sides, for the same reason: the caller is one of them, and the other
            # is not shown while their account is suspended (decision 8).
            public_q('follower'), public_q('following'))

    def perform_create(self, serializer):
        target = serializer.validated_data.get('following')
        refuse_a_blocked_follow(self.request.user, target)
        if UserFollow.objects.filter(follower=self.request.user, following=target).exists():
            # The unique pair was a 500 (IntegrityError) on a repeated follow.
            raise AlreadyFollowing()
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

        Auth required; no body; nothing differs by build. `{"followers": [{id,
        username, user_type, profile_picture, followed_at}], "count"}` — without anyone
        blocked either way, and without an account that is suspended or erased (the
        owner's decision 8, 2026-09-24): the follow is kept and listed again when the
        account is reinstated.
        """
        followers = UserFollow.objects.filter(
            following=request.user
        ).exclude(follower_id__in=blocked_ids(request.user)).filter(
            public_q('follower')).select_related('follower')
        
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

        The same shape and the same rule as `followers/`, under `"following"`: no one
        blocked either way, no suspended or erased account (decision 8).
        """
        following = UserFollow.objects.filter(
            follower=request.user
        ).exclude(following_id__in=blocked_ids(request.user)).filter(
            public_q('following')).select_related('following')
        
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


class TotalOrderingFilter(OrderingFilter):
    """`OrderingFilter` whose order always ends in the primary key (SIDE_BUGS S-218).

    A view's `ordering` and a caller's `?ordering=likes_count` REPLACE the model's
    `Meta.ordering` (`-created_at, -id`), so the post list sorted on one non-unique
    column and LIMIT/OFFSET (or a cursor) repeated and skipped rows sharing a value —
    ten posts with 0 likes, or a fan-out batch sharing a timestamp. `-id` is appended
    unless the order already names the key; the paginator reads this same order.
    """

    def get_ordering(self, request, queryset, view):
        ordering = super().get_ordering(request, queryset, view)
        if not ordering:
            return ordering
        ordering = list(ordering)
        if not any(str(term).lstrip('-') in ('id', 'pk') for term in ordering):
            ordering.append('-id')
        return ordering


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


def visible_posts_for(user, *, hidden=None):
    """Every post `user` is allowed to see.

    `hidden`: `blocked_ids(user)` when the caller has already read it, so a view that
    needs the set itself does not read it twice (SIDE_BUGS S-219).

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

    And a post whose author is not publicly visible — the account is suspended or
    erased (`users/visibility.py`, the owner's decision 8, 2026-09-24) — is in nobody's
    result: not the feed, not search, not `?author=`, not its detail route, and it
    cannot be liked or commented on. The post is kept; reinstating the account brings
    it back. The caller's own posts are unaffected: a signed-in caller is active.
    """
    return Post.objects.filter(
        (
            Q(visibility='public')
            | Q(visibility='followers', author__followers__follower=user)
        ) & Q(is_hidden=False)
        | Q(author=user)
    ).exclude(author_id__in=blocked_ids(user) if hidden is None else hidden).filter(
        public_q('author')).distinct()


class PostViewSet(viewsets.ModelViewSet):
    """
    API endpoints for social posts

    Any member, a TRAINER included (Road to 10 S3.8, the owner's D11): no action here
    refuses by role, and a trainer reads, posts, edits, deletes and likes as a client
    does.

    What a caller may read is `visible_posts_for` — which, since the owner's decision 8
    (2026-09-24), leaves out every post of a suspended or erased account.
    `PATCH`/`DELETE` of someone else's post is 403 `not_owner` (was
    `permission_denied`, the owner's decision 9, 2026-09-24); a post the caller cannot
    read is 404 `not_found`.

    The banned-terms filter (spec B1, 2026-09-27): a `POST`/`PUT`/`PATCH` whose `title`
    or `content` holds a term on the operators' list answers 400 `content_not_allowed`
    — the top-level `code`, and `field_errors.<field>[0].code` for each such field —
    and nothing is written. Only the fields the request sends are checked, after every
    other validation. The matched term is never named. Same on every build
    (`moderation/ugc_filter.py`).

    A POST/PUT/PATCH carrying an `image` answers 503 `upload_busy` while the server is
    already decoding as many images as its memory holds (`training_platform/
    image_budget.py`): nothing was saved; send it again after `Retry-After` seconds
    (`retry_after_seconds` in the body).
    """
    serializer_class = PostSerializer
    permission_classes = [IsOwnerOrReadOnly]
    #: POSTs to a detail route that act ON a post rather than editing it. Without this
    #: `IsOwnerOrReadOnly` refused everyone but the author and the like button returned
    #: 403 for every user on every post they did not write.
    interaction_actions = ('like',)
    # A total order whatever `?ordering=` asks for (SIDE_BUGS S-218).
    filter_backends = [DjangoFilterBackend, SearchFilter, TotalOrderingFilter]
    filterset_fields = ['author', 'post_type', 'visibility']
    search_fields = ['title', 'content']
    ordering_fields = ['created_at', 'updated_at', 'likes_count', 'comments_count']
    ordering = ['-created_at', '-id']
    
    def get_queryset(self):
        user = self.request.user
        
        # Get posts that user can view based on visibility AND moderation. The block
        # set is read once, for this and the likes correction below (S-219).
        hidden = blocked_ids(user)
        queryset = visible_posts_for(user, hidden=hidden)
        
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
        # A comment by a suspended account is unreadable too (decision 8).
        unreadable = Q(is_hidden=True) | hidden_q('author')
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


from rest_framework.exceptions import ErrorDetail, ValidationError as DRFValidationError, NotFound
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

    Any member, a TRAINER included (Road to 10 S3.8, the owner's D11): no action here
    refuses by role.

    A comment is listed only when its post is one the caller may read
    (`visible_posts_for`), it is not hidden, its author is not blocked either way, and
    its author's account is publicly visible — a suspended or erased account's comments
    leave every thread, and every post's `comments_count` agrees (the owner's decision
    8, 2026-09-24). Replying to such a comment is refused like replying to a hidden one.

    Codes (the owner's decision 9, 2026-09-24): `POST` on a post the caller cannot read
    is 404 `not_found`; `PATCH`/`DELETE` of someone else's comment is 403 `not_owner`
    (was `permission_denied`); moving a comment is 400 `validation_error` with the
    field code `comment_cannot_move`, a reply outside its thread the field code
    `reply_not_in_thread` on `parent_comment`.

    The banned-terms filter (spec B1, 2026-09-27): a `POST`/`PUT`/`PATCH` whose
    `content` holds a term on the operators' list answers 400 `content_not_allowed`
    (top-level `code`, and `field_errors.content[0].code`); nothing is written, no
    counter moves and no push is sent. Same on every build (`moderation/ugc_filter.py`).
    """
    serializer_class = CommentSerializer
    permission_classes = [IsOwnerOrReadOnly]
    interaction_actions = ('like',)
    pagination_class = CommentCursorPagination
    # `?ordering=likes_count` keeps the `-id` tiebreak too (SIDE_BUGS S-218).
    filter_backends = [DjangoFilterBackend, SearchFilter, TotalOrderingFilter]
    filterset_fields = ['post', 'author', 'parent_comment']
    search_fields = ['content']
    ordering_fields = ['created_at', 'updated_at', 'likes_count']
    # What the cursor actually pages by (OrderingFilter's ordering wins over the
    # paginator's): `-id` breaks ties (B-016).
    ordering = ['-created_at', '-id']
    
    def get_queryset(self):
        user = self.request.user
        # Who this caller blocked or was blocked by: read ONCE, for the filter and the
        # likes correction below (it was read twice, one query each — SIDE_BUGS S-219).
        hidden = blocked_ids(user)

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
            post__in=visible_posts_for(user, hidden=hidden),
            is_hidden=False,
        ).filter(
            # Not while its author's account is suspended (decision 8).
            public_q('author'),
        ).exclude(
            # Its OWN block filter, not only the one inherited through the post. The
            # post clause hides a blocked person's posts; it does nothing about a
            # blocked person's comments on YOUR post, which is where the harassment
            # this feature exists for actually lands.
            author_id__in=hidden,
        ).distinct()

        # Same correction as the post list, for the same reason: `Comment.likes_count`
        # is denormalised and counts a blocked person's like. One subquery for the
        # page, not one query per comment.
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
            raise NotFound(_('No such post.'), code='not_found')

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

    Auth required. Nothing differs by build. Any member, a TRAINER included (Road to 10
    S3.8, the owner's D11): a trainer creates, edits, closes, joins, logs and leaves a
    challenge and reads its board as a client does; no action refuses by role. A
    challenge row (list, detail, create,
    update) is `ChallengeSerializer`; since 2026-09-24 it also carries `metric` —
    `manual` | `volume_kg` | `sets` | `workouts` | `training_days` | `exercise_reps` |
    `streak_days` (the last two since B-161, below), writable, default `manual` — and
    `auto_counted` (read-only, `metric != "manual"`): when true the
    server counts the participants' progress from the training they log
    (`social/challenge_progress.py`), `update_progress` refuses a hand-typed number
    (`progress_auto_counted`), and the app shows «ابدأ تمرين اليوم» instead of the
    log sheet (BACKEND_BUGS B-075).

    **The metrics** (the CreateChallenge artboard; the owner, 2026-09-28, BACKEND_BUGS
    B-161). Two more values of `metric`, additive: `exercise_reps` — the reps of ONE
    exercise, named by `exercise` (an exercise id: a catalogue one, or a trainer
    creator's own in their `my_clients` challenge; writable, nullable, REQUIRED for
    this metric), served with
    `exercise_name` (read-only, the caller's language); a day above 500 reps of it
    flags the participant for review — and `streak_days` — the CURRENT run of
    consecutive days with at least one counted set: today, not over yet, breaks
    nothing, nor does the window's last day once the window is over (the run stands as
    it stood at the end instant); any other day without one breaks it, and the hourly
    phase task recounts it so a broken run reads 0 within the hour. The artboard's «عدد التمارين» is
    `workouts` (workouts finished, `status: "completed"`, with the F20 rules below);
    «مجموع الأوزان» is `volume_kg`, «عدد المجموعات» `sets`, «أيام التمرين»
    `training_days`. Create/update refusals (400 `validation_error`, the field code in
    `field_errors.exercise`): `exercise_required` — `metric` `exercise_reps` with no
    exercise; `exercise_not_found` — an id that does not exist, is inactive or is
    another trainer's own, or a private exercise (`created_by` set: the creator's own,
    a client's coach's) in any challenge but its author's `my_clients` one — a PATCH
    of `audience` back to `community` on such a challenge included, the code then
    still in `field_errors.exercise`. `exercise` sent with any other metric is stored
    null.

    **Who may join** (the CreateChallenge artboard's «من يستطيع الانضمام»; the owner,
    2026-09-28, BACKEND_BUGS B-162). `audience` — `community` (default: everyone) |
    `my_clients`, writable on create and update. Only a creator who is a TRAINER may
    set `my_clients`: anyone else gets 400 `validation_error` with the field code
    `audience_trainers_only` in `field_errors.audience`. A `my_clients` challenge is
    shown only to its creator, staff, the creator's clients (an approved relation with
    them — a frozen coaching still counts) and anyone already in it (a client whose
    coaching ends mid-challenge stays in the standing and keeps seeing it): for
    everyone else it is not in the list, and its detail, `leaderboard/`, `leave/` and
    `update_progress/` answer 404 as for a challenge that does not exist; `join/`
    answers 403 `challenge_clients_only`. The Challenge Winner badge still needs
    `winner_min_participants` active participants. Nothing differs by build.

    `user_progress` — `{current_value, progress_percentage, rank, review_state,
    review_reason}` of the caller, or null when they have not joined. `rank` is
    counted live by the one rule every standing surface reads (`social/standing.py`,
    BACKEND_BUGS B-086): 1 + the number of participants strictly ahead — more
    progress, then an earlier join, then a lower participation id — so two people on
    the same number hold different places, the earlier joiner first. Null for someone
    who dropped out or was disqualified.

    **A suspended account holds no place** (the owner's decision 8, 2026-09-24): a
    participant whose account is not publicly visible (`users/visibility.py` —
    suspended or erased) is treated like one who dropped out or was disqualified — not
    on the board, not counted ahead of anyone, so everyone behind moves up in `rank`,
    the `leaderboard/` rows and the home card alike — and a challenge created by such
    an account is still listed, with `creator: null`. Reinstating the account restores
    both.

    `PATCH`/`PUT`/`DELETE` of a challenge the caller did not create is 403 `not_owner`
    (was `permission_denied`, the owner's decision 9, 2026-09-24).

    Anti-cheat (owner, 2026-09-24, F20), all read-only on the row:
      `ranked` — bool: `auto_counted` AND the creator is staff or a VERIFIED trainer
          (`trainer_is_verified`; anyone may sign up as a trainer). Only a
          ranked challenge can award the Challenge Winner badge (or anything a first
          place earns); a manual (self-reported) challenge is never ranked.
      `counting` — null for a manual challenge, else the credible-work rules the count
          applies (`social/challenge_progress.py COUNTING`): `{"max_reps_per_set",
          "max_weight_kg_per_set", "min_sets_per_day", "max_sets_per_day",
          "max_volume_kg_per_day", "min_sets_per_workout", "min_workout_minutes",
          "max_workouts_per_day", "late_hours"}`.
      `review_hours` — 72 (equal to `counting.late_hours`): how long after the end
          the standing stays open for review, and keeps counting its window's sets that
          sync late, before the winner is final.
      `winner_final` — bool: the challenge ended at least `review_hours` ago and its
          winner is settled.
      `winner_min_participants` — int, 5 (the owner's decision of 2026-09-24,
          `social/standing.py MIN_PARTICIPANTS_FOR_WINNER`): a challenge needs at least
          this many active participants at the settlement for its first place to win
          the Challenge Winner badge (or anything a first place earns). Fewer, and it
          settles with no winner (`winner_final` still becomes true).
      `active_participants` — int, live: participants in the standing (not dropped,
          not disqualified, the account visible) whose counted number is above zero —
          a joiner who never trained does not count. Compare it with
          `winner_min_participants` to say "a winner needs N more people".
      `user_progress.review_state` — `"clear"` | `"flagged"` | `"disqualified"`, and
      `user_progress.review_reason` — null | `"day_over_threshold"` (a day of the
          window above a review threshold, set automatically) | `"operator"`. Only the
          person themselves sees them. Flagged: listed and placed normally, but a
          flagged first place wins nothing until an operator clears it. Disqualified:
          no place anywhere, `join` and `leave` answer 403 `challenge_disqualified`.

    PUT/PATCH (the creator, or staff) — what decides a standing is fixed where
    changing it would let a result be picked after the fact (F20). Both answer 400
    with the top-level `code` and the field error beside it (`field_errors`):
      `challenge_over` — `metric`, `start_date`, `end_date`, `target_value` or
          `exercise` of a challenge that is over (ended or closed). Nothing recounts a
          finished challenge, so switching a manual one to an auto-counted metric after
          the end would have ranked its self-reported numbers.
      `end_date_in_past` — an `end_date` earlier than now. Ending early is a close
          (`status: "completed"`), which moves the end to the moment of the close, so
          the review window runs from when it really ended.
    Changing `metric`, `exercise` or the dates of a RUNNING challenge still recounts
    everyone.

    The banned-terms filter (spec B1, 2026-09-27): a POST/PUT/PATCH whose `title`,
    `description`, `rules`, `reward_description` or `unit` holds a term on the
    operators' list answers 400 `content_not_allowed` (top-level `code`, and
    `field_errors.<field>[0].code` for each such field); nothing is written. Checked
    after the two refusals above. Same on every build (`moderation/ugc_filter.py`).

    A POST/PUT/PATCH carrying an `image` answers 503 `upload_busy` while the server is
    already decoding as many images as its memory holds (`training_platform/
    image_budget.py`): nothing was saved; send it again after `Retry-After` seconds
    (`retry_after_seconds` in the body).
    """
    #: What a finished challenge keeps: `challenge_over` if a PATCH changes one.
    FIXED_ONCE_OVER = ('metric', 'start_date', 'end_date', 'target_value', 'exercise')

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
            .select_related('creator', 'exercise')
            .only(
                'id', 'title', 'description', 'challenge_type',
                'target_value', 'unit', 'start_date', 'end_date',
                'max_participants', 'participants_count', 'status',
                'created_at', 'rules', 'reward_description', 'image', 'metric',
                'winner_checked_at', 'audience',
                # `exercise_name` (B-161), read with the row.
                'exercise', 'exercise__id', 'exercise__name',
                'creator__id', 'creator__username', 'creator__email',
                'creator__user_type', 'creator__profile_picture', 'creator__is_staff',
                # `ranked` reads it (F20): deferred, it cost one query per row.
                'creator__trainer_is_verified',
                # Whether the creator may be shown (decision 8), read with the row.
                'creator__is_active',
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
            # `active_participants` (the owner's minimum for a winner, 2026-09-24): one
            # correlated COUNT inside this query, never a query per row.
            .annotate(active_participants_anno=active_participants_count())
        )
        user = getattr(self, 'request', None).user if getattr(self, 'request', None) else None
        # A `my_clients` challenge is hidden from everyone but its creator's clients
        # (the owner, 2026-09-28, B-162): not listed, and 404 on every route that names
        # it — except `join`, which answers 403 `challenge_clients_only` itself.
        if getattr(self, 'action', None) != 'join':
            qs = qs.filter(Challenge.visible_to_q(user))
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
                # The caller's own review (F20); nobody else's is ever served.
                review_state_anno=Subquery(part_qs.values('review_state')[:1]),
                review_reason_anno=Subquery(part_qs.values('review_reason')[:1]),
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

        Joined by the version EVERY reader's list carries (`social/challenge_cache.py
        EVERYONE`, SIDE_BUGS S-189 / S-194): a hidden, edited, closed or deleted
        challenge, or anyone's join or leave, leaves every cached list at once.
        """
        from . import challenge_cache

        mine = challenge_cache.version(
            f"challenges_ver:{cls._challenges_cache_user_part(user)}")
        return f"{mine}.{challenge_cache.version(challenge_cache.EVERYONE)}"

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
        from .challenge_cache import bump

        bump(version_key)

    def perform_create(self, serializer):
        serializer.save(creator=self.request.user)
        self._invalidate_challenges_cache(self.request)

    def perform_update(self, serializer):
        """Save; a creator closing the challenge early ends it NOW.

        The Challenge Winner badge counts a first place only once the challenge is over
        (`achievements/engine.py challenge_wins`, SIDE_BUGS S-044) and, since the
        owner's F20 (2026-09-24), only once its standing has been open for review for
        `Challenge.REVIEW_HOURS` after the end: the hourly phase task settles the
        winner then (`social/tasks.py settle_winners`). A creator closing a challenge
        early (`status: "completed"`) moves its `end_date` to the moment of the close,
        so the review window runs from when it really ended.
        """
        before = serializer.instance
        self._refuse_changes_that_pick_a_result(before, serializer.validated_data)
        was_over = before.lifecycle_status == 'completed'
        measured = (before.metric, before.start_date, before.end_date, before.exercise_id)
        target = before.target_value
        challenge = serializer.save()
        now = timezone.now()
        if (not was_over and challenge.status == 'completed'
                and challenge.end_date > now):
            Challenge.objects.filter(pk=challenge.pk).update(end_date=now)
            challenge.end_date = now
        if challenge.target_value != target and not was_over:
            # Completion follows the target as it follows the number (the review, M6).
            from .challenge_progress import reapply_target

            reapply_target(challenge)
        # What the challenge counts, or over which days, changed while it runs: every
        # participant's number is recounted (one grouped query) and the standing
        # re-ranked, rather than waiting for each of them to log again (B-075).
        if (challenge.auto_counted and challenge.is_active
                and (challenge.metric, challenge.start_date, challenge.end_date,
                     challenge.exercise_id) != measured):
            from .challenge_progress import recount

            with transaction.atomic():
                recount(challenge)
                self._update_challenge_rankings(challenge)
        # The save reached every cached list already; the close's `.update()` of
        # `end_date` above goes around it (SIDE_BUGS S-194).
        from .challenge_cache import bump_everyone

        bump_everyone()

    def _refuse_changes_that_pick_a_result(self, before, data):
        """`challenge_over` / `end_date_in_past`; see the class docstring."""
        from rest_framework.exceptions import ErrorDetail

        def refuse(field, message, code):
            refusal = DRFValidationError({field: [ErrorDetail(message, code=code)]})
            refusal.service_code = code
            raise refusal

        if before.lifecycle_status in Challenge.TERMINAL_STATUSES:
            for field in self.FIXED_ONCE_OVER:
                if field in data and data[field] != getattr(before, field):
                    refuse(field, _('This challenge is over: what it measured and its '
                                    'dates can no longer change.'), 'challenge_over')
        end = data.get('end_date')
        if end is not None and end != before.end_date and end < timezone.now():
            refuse('end_date', _('The end of a challenge cannot be in the past. To end '
                                 'it now, close it.'), 'end_date_in_past')

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
          `403` `{"error", "code": "challenge_disqualified"}` — an operator
                disqualified you from this challenge (owner, 2026-09-24, F20); checked
                before anything else
          `403` `{"error", "code": "challenge_clients_only"}` — an `audience:
                "my_clients"` challenge and you are not its creator's client (no
                approved relation with them; a frozen coaching still counts) — the
                owner, 2026-09-28, BACKEND_BUGS B-162. Checked right after the above
          `404` — no such challenge, or one an operator hid
        """
        challenge = self.get_object()

        if self._is_disqualified(request.user, challenge):
            return self._disqualified_response()

        # Its creator's clients only (B-162). Before "not running": a challenge someone
        # may not see tells them nothing about its dates.
        if not challenge.visible_to(request.user):
            return Response(
                {'error': _("This challenge is open to its coach's clients only."),
                 'code': 'challenge_clients_only'},
                status=status.HTTP_403_FORBIDDEN)

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

    @staticmethod
    def _is_disqualified(user, challenge):
        return ChallengeParticipation.objects.filter(
            user=user, challenge=challenge,
            review_state=ChallengeParticipation.REVIEW_DISQUALIFIED).exists()

    @staticmethod
    def _disqualified_response():
        return Response(
            {'error': _('You were disqualified from this challenge.'),
             'code': 'challenge_disqualified'},
            status=status.HTTP_403_FORBIDDEN)

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
            # `participants_count` moved for every reader, not only the joiner (S-194).
            from .challenge_cache import bump_everyone

            bump_everyone()
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
          `403` `{"error", "code": "challenge_disqualified"}` — an operator
                disqualified you (F20): the participation is kept, or leaving and
                joining again would wipe the decision
          `404` — no such challenge

        Nothing differs by build.
        """
        challenge = self.get_object()

        if self._is_disqualified(request.user, challenge):
            return self._disqualified_response()

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
        # `participants_count` moved for every reader (S-194).
        from .challenge_cache import bump_everyone

        bump_everyone()
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
        dropped out (someone who dropped holds no place and is never listed). `me` alone
        also carries the caller's review (owner, 2026-09-24, F20), which nobody else
        sees: `"review_state": "clear" | "flagged" | "disqualified"` and
        `"review_reason": null | "day_over_threshold" | "operator"`. A DISQUALIFIED
        participant is left out of the rows and out of every place (places run 1..N
        over the rest); their `me` row is still served, with `"rank": null`. A flagged
        participant is listed and placed like anyone else.

        `ranked` (bool) — whether a first place here can win anything (the challenge
        row's `ranked`).

        An account the caller has blocked, or that has blocked them, is left out of
        `leaderboard` AFTER the places are counted: everyone else keeps their true
        `rank` and the list may be shorter than `limit`.

        A suspended or erased account is different (the owner's decision 8,
        2026-09-24): it holds NO place, like one that dropped out, so it is not listed
        and the places close up behind it (1, 2, 3 — not 1, 2, 4). A block is one
        viewer's choice and must not renumber the board for everyone; a suspension is
        the platform's, and the public board excludes the account for everyone.

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
            # Not on this page, or holding no place (disqualified): still their row.
            from .standing import participating, place_or_none

            mine = (participating(ChallengeParticipation.objects.filter(
                        challenge=challenge, user=request.user))
                    .select_related('user').annotate(place=place_or_none()).first())

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
        me = None
        if mine is not None:
            me = row(mine)
            me['review_state'] = mine.review_state
            me['review_reason'] = mine.review_reason
        return Response({
            'challenge': challenge.title,
            'auto_counted': challenge.auto_counted,
            'ranked': challenge.ranked,
            'leaderboard': [row(p) for p in participants if p.user_id not in hidden],
            'me': me,
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
            # Its own code (the owner's decision 9, 2026-09-24): `cursor_paginated`,
            # at the top and on the field, where it answered `validation_error` /
            # `invalid`.
            refusal = DRFValidationError({
                'page': [ErrorDetail(_(
                    "This endpoint is cursor-paginated. Follow the `next` link "
                    "instead of passing `page`."
                ), code='cursor_paginated')]
            })
            refusal.service_code = 'cursor_paginated'
            raise refusal
        return super().paginate_queryset(queryset, request, view)


class NotificationViewSet(viewsets.ReadOnlyModelViewSet):
    """
    API endpoints for notifications.
    
    Paginated via cursor-based pagination.
    
    GET /api/social/notifications/?page_size=20
    GET /api/social/notifications/?cursor=<token>&page_size=20

    The inbox of every role: a trainer's is read and marked read the same way, and
    holds the kinds `GET /api/notifications/preferences/event_types/` lists for the
    role (`role_event_types`).

    `?page=` is 400 `cursor_paginated` (the owner's decision 9, 2026-09-24; it was
    `validation_error`): follow `next`. An unknown or foreign id is 404 `not_found`.
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
        ).filter(
            # A suspended actor's name, photo and words leave the list too, as they left
            # the thread (the owner's decision 8; the review, D6). System notices have
            # no actor and stay; reinstating the account brings the rows back.
            Q(actor__isnull=True) | public_q('actor')
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
    has blocked the caller, is not served: 404 `not_found` on the detail route and on
    `by_username/`. The counts agree with the rows the caller can fetch (blocks, hidden
    posts, and — the owner's decision 8, 2026-09-24 — followers, follows and posts of
    suspended or erased accounts, which are not counted).
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
        """Every account this viewer may see: publicly visible (`users/visibility.py`,
        the account is active), and neither side blocking."""
        return public_people().exclude(id__in=self._hidden())

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
            # Not a suspended or erased follower either: the count agrees with
            # `followers/` (decision 8).
            followers_count_anno=subquery_count(
                UserFollow.objects.filter(following_id=OuterRef('pk')).exclude(
                    follower_id__in=hidden).filter(public_q('follower')), 'following_id'),
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
                    following_id__in=hidden).filter(public_q('following')),
                'follower_id'),
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
        400 `username_required` without `username` (was the `bad_request` default);
        404 `not_found` when no visible account has it — none, a suspended or erased
        one, or one blocked either way: the same answer the detail route gives (the
        owner's decision 9, 2026-09-24, names both codes explicitly). Otherwise the
        profile, as the detail route answers it.
        """
        username = request.query_params.get('username')
        if not username:
            return Response({'error': _('username is required'), 'code': 'username_required'},
                            status=status.HTTP_400_BAD_REQUEST)
        try:
            # Through `get_queryset()`, not `CustomUser.objects`: this route bypassed
            # the viewset's queryset entirely, so a blocked person stayed reachable by
            # name even once their profile page was gone.
            user = self.get_queryset().get(username=username)
            serializer = self.get_serializer(user)
            return Response(serializer.data)
        except CustomUser.DoesNotExist:
            return Response({'error': _('User not found'), 'code': 'not_found'},
                            status=status.HTTP_404_NOT_FOUND)

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

        Listed: publicly visible (active — never a suspended or erased account, the
        owner's decision 8), verified members (`client` and `trainer` accounts) other
        than the caller, and never anyone the caller has blocked or who has blocked the
        caller — a subset of the people whose profile the caller could open; staff,
        top-up agents and signups that never verified are not listed (the owner's
        decision 7, 2026-09-24, keeps this). Order: an exact username first, then usernames that start
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
