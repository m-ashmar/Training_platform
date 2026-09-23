"""
Social Features Serializers

Serializers for social models to handle API data conversion.
"""

from django.db.models import F, Q
from django.utils.translation import gettext_lazy as _
from rest_framework import serializers
from .models import (
    UserFollow, Post, PostLike, Comment, CommentLike,
    Challenge, ChallengeParticipation,
)
# The badges `/api/social/achievements/` serves are the `achievements` app's — the ONE
# badge system this platform awards (`achievements/engine.py`). `social.Achievement` and
# `social.UserAchievement` are tables nothing live writes (SIDE_BUGS S-127).
from achievements.models import Achievement, UserAchievement
# NOTE: `Notification` is deliberately NOT imported from social.models here.
# NotificationSerializer binds to the canonical notifications.Notification
# (imported further down). The old social.models import was shadowed by that
# later import — a rename/reorder would have silently rebound the serializer to
# the deprecated table.
from users.models import CustomUser
# Validates content + caps dimensions + re-encodes (strips EXIF/GPS).
# Plain ImageField only checks that Pillow can parse the file.
from training_platform.file_security import SecureImageField
# One query, both directions (moderation/services.py). It imports no model at module
# level, which is why it can be imported from here at all.
from moderation.services import blocked_ids
import logging

logger = logging.getLogger(__name__)


def _hidden_from(serializer):
    """The accounts this request's viewer may not see — resolved ONCE per response.

    `blocked_ids` is a single query whatever the size of the page, but a serializer
    method runs per ROW, so calling it there would be one query per row. DRF reuses one
    child serializer instance for every row of a list, so the cache below lasts exactly
    as long as the response and no longer.

    This is the fallback path. A viewset that pages these objects annotates the counts
    in its own query instead (`social/views.py`), and then no row reaches here.
    """
    cached = getattr(serializer, '_blocked_ids_cache', None)
    if cached is None:
        request = serializer.context.get('request')
        cached = serializer._blocked_ids_cache = blocked_ids(
            getattr(request, 'user', None))
    return cached


class UserMinimalSerializer(serializers.ModelSerializer):
    """Minimal user serializer for social features"""
    
    class Meta:
        model = CustomUser
        # `email` removed: this serializer is embedded as the author/creator of
        # every post, comment and challenge, so exposing it leaked the email of
        # any user whose content appeared in a feed.
        fields = ['id', 'username', 'user_type', 'profile_picture']


def member_since(user):
    """The month someone joined, as the first day of it (`"2026-05-01"`).

    A MONTH, on purpose: the profile says «منذ أيار» and Find «انضم في آب», and the day
    and hour an account was opened is more than a stranger needs (BACKEND_BUGS B-077).
    """
    joined = getattr(user, 'date_joined', None)
    if joined is None:
        return None
    from django.utils import timezone

    day = timezone.localtime(joined).date() if timezone.is_aware(joined) else joined.date()
    return day.replace(day=1).isoformat()


def badge_card(badge, request=None):
    """A badge as a profile or a share card draws it — nothing about who holds it.

    `id` is the ACHIEVEMENT's id — the one `/api/achievements/` lists and
    `/api/achievements/{id}/feature/` takes — never an earned row's.
    """
    icon = None
    if badge.icon:
        icon = (request.build_absolute_uri(badge.icon.url) if request is not None
                else badge.icon.url)
    return {'id': badge.id, 'key': badge.key, 'name': badge.name,
            'category': badge.category, 'points': badge.points, 'icon_url': icon,
            'badge_color': badge.badge_color, 'is_rare': badge.is_rare}


def featured_badge_rows(earned, request=None):
    """The rows of a profile's «أبرز إنجازاته»: the badges its owner chose to feature,
    each `badge_card` plus when they earned it."""
    return [{**badge_card(record.achievement, request), 'earned_at': record.earned_at}
            for record in earned]


def featured_badges_queryset(viewer):
    """The earned rows a viewer is shown on someone's profile: featured, of a badge still
    in the catalogue, and not a SECRET badge unless the profile is the viewer's own —
    the catalogue lists a secret badge only to someone who has earned it, and a profile
    must not be the way around that."""
    from achievements.models import UserAchievement as Earned

    shown = Q(achievement__is_secret=False)
    if viewer is not None and getattr(viewer, 'is_authenticated', False):
        shown |= Q(user=viewer)
    return (Earned.objects.filter(is_featured=True, achievement__is_active=True)
            .filter(shown).select_related('achievement').order_by('-earned_at', '-id'))


class PublicUserProfileSerializer(serializers.ModelSerializer):
    """Public-facing user profile serializer with non-sensitive fields and social counts.

    Since 2026-09-24 (BACKEND_BUGS B-077), additively:

    * `featured_achievements` — at most three rows, the badges the person chose to
      feature (`featured_badge_rows`): `{id, key, name, category, points, icon_url,
      badge_color, is_rare, earned_at}`, `id` being the achievement's. A secret badge
      is left out for everyone but its owner.
    * `community_points` — the points of every badge they have earned: the number the
      community-points leaderboard (`GET /api/achievements/leaderboard/`) ranks by.
    * `member_since` — the first day of the month they joined, `"YYYY-MM-01"`.

    The profile of an account the viewer has blocked, or that has blocked them, is not
    served at all (`PublicUserProfileViewSet`), so none of this reaches them.
    """
    followers_count = serializers.SerializerMethodField()
    following_count = serializers.SerializerMethodField()
    posts_count = serializers.SerializerMethodField()
    is_following = serializers.SerializerMethodField()
    profile_picture_url = serializers.SerializerMethodField()
    featured_achievements = serializers.SerializerMethodField()
    community_points = serializers.SerializerMethodField()
    member_since = serializers.SerializerMethodField()

    class Meta:
        model = CustomUser
        # NOTE: `email` is deliberately NOT exposed. This endpoint is readable by
        # any authenticated user for any active user, so including email allowed
        # harvesting every account's address by walking ids.
        fields = [
            'id', 'username', 'user_type', 'first_name', 'last_name',
            'profile_picture_url', 'followers_count', 'following_count', 'posts_count',
            'is_following', 'featured_achievements', 'community_points', 'member_since',
        ]
        read_only_fields = fields

    # `PublicUserProfileViewSet` prefetches the featured rows and annotates the points
    # for the whole page; the fallbacks below are one query per row, outside it.

    def get_featured_achievements(self, obj):
        request = self.context.get('request')
        earned = getattr(obj, 'featured_badges', None)
        if earned is None:
            earned = featured_badges_queryset(getattr(request, 'user', None)).filter(
                user=obj)[:3]
        return featured_badge_rows(list(earned)[:3], request)

    def get_community_points(self, obj):
        annotated = getattr(obj, 'community_points_anno', None)
        if annotated is not None:
            return annotated
        from django.db.models import Sum
        from achievements.models import UserAchievement as Earned

        return Earned.objects.filter(user=obj).aggregate(
            total=Sum('achievement__points'))['total'] or 0

    def get_member_since(self, obj):
        return member_since(obj)

    # The three counts agree with the rows this viewer can actually fetch. They were
    # unfiltered, so a profile said "12 followers" to someone whose follower list
    # showed 11 — and the missing one was the account they had blocked, which is
    # exactly the person a block is supposed to stop being told about.
    #
    # `PublicUserProfileViewSet` annotates all three in the page's own query; the
    # fallbacks below keep the serializer correct anywhere else, at one query per row.

    def get_followers_count(self, obj):
        annotated = getattr(obj, 'followers_count_anno', None)
        if annotated is not None:
            return annotated
        return obj.followers.exclude(
            follower_id__in=_hidden_from(self)).count()

    def get_following_count(self, obj):
        annotated = getattr(obj, 'following_count_anno', None)
        if annotated is not None:
            return annotated
        return obj.following.exclude(
            following_id__in=_hidden_from(self)).count()

    def get_posts_count(self, obj):
        annotated = getattr(obj, 'posts_count_anno', None)
        if annotated is not None:
            return annotated
        if not hasattr(obj, 'posts'):
            return 0
        request = self.context.get('request')
        user = getattr(request, 'user', None)
        if user is None or not getattr(user, 'is_authenticated', False):
            # `visible_posts_for` takes a real user (it asks who follows whom); for
            # anyone else the visible set is the public, unhidden posts.
            return obj.posts.filter(visibility='public', is_hidden=False).count()
        # `visible_posts_for` is the ONE definition of what a viewer may read — the
        # same one the post list, the feed and search use — so the number cannot drift
        # from the list it describes. Imported here because `social.views` imports this
        # module.
        from .views import visible_posts_for

        return visible_posts_for(user).filter(author_id=obj.pk).count()

    def get_is_following(self, obj):
        # Prefers the EXISTS annotation the viewset adds for the whole page; the
        # `.exists()` below is one query per row and only runs outside it.
        annotated = getattr(obj, 'is_following_anno', None)
        if annotated is not None:
            return annotated
        request = self.context.get('request')
        if not request or not request.user.is_authenticated:
            return False
        return obj.followers.filter(follower=request.user).exists()

    def get_profile_picture_url(self, obj):
        request = self.context.get('request')
        if obj.profile_picture:
            url = obj.profile_picture.url
            if request is not None:
                return request.build_absolute_uri(url)
            return url
        return None


class PublicUserSearchSerializer(serializers.ModelSerializer):
    """One row of `GET /api/social/users/public-profile/search/` (BACKEND_BUGS B-081).

    A SUBSET of the public profile — every field here is also on
    `PublicUserProfileSerializer`, read the same way — so a search can never tell anyone
    more about a person than opening their profile would.
    """
    followers_count = serializers.SerializerMethodField()
    is_following = serializers.SerializerMethodField()
    profile_picture_url = serializers.SerializerMethodField()
    member_since = serializers.SerializerMethodField()

    class Meta:
        model = CustomUser
        fields = ['id', 'username', 'user_type', 'first_name', 'last_name',
                  'profile_picture_url', 'followers_count', 'is_following',
                  'member_since']
        read_only_fields = fields

    get_followers_count = PublicUserProfileSerializer.get_followers_count
    get_is_following = PublicUserProfileSerializer.get_is_following
    get_profile_picture_url = PublicUserProfileSerializer.get_profile_picture_url
    get_member_since = PublicUserProfileSerializer.get_member_since


class UserFollowSerializer(serializers.ModelSerializer):
    """Serializer for user following relationships"""
    
    follower = UserMinimalSerializer(read_only=True)
    following = UserMinimalSerializer(read_only=True)
    # Both sides were read-only, so `following` could never be supplied and every POST
    # to /api/social/follows/ died with a NOT NULL violation (500). This write-only
    # companion field accepts the target id while keeping the nested read output.
    # Active accounts only, the rule `follow_user/` applies (BACKEND_BUGS B-079): an
    # inactive (suspended or erased) account answers the same `does_not_exist` field
    # error a missing id does.
    following_id = serializers.PrimaryKeyRelatedField(
        queryset=CustomUser.objects.filter(is_active=True), source='following',
        write_only=True
    )

    class Meta:
        model = UserFollow
        fields = ['id', 'follower', 'following', 'following_id', 'created_at']
        read_only_fields = ['id', 'created_at']

    def validate_following_id(self, value):
        request = self.context.get('request')
        if request and request.user.is_authenticated and value.pk == request.user.pk:
            raise serializers.ValidationError("You cannot follow yourself.")
        return value


def _share_types():
    """(ContentType of a workout session, ContentType of a badge) — the two things a post
    can share. `get_for_model` is cached per process."""
    from django.contrib.contenttypes.models import ContentType
    from routine.models import WorkoutSession

    return (ContentType.objects.get_for_model(WorkoutSession),
            ContentType.objects.get_for_model(Achievement))


def load_shares(posts, request=None) -> dict:
    """{post_id: share} for a page of posts, in at most TWO queries whatever its size.

    A share is what the card draws and nothing else (BACKEND_BUGS B-080):

        {"kind": "workout",
         "workout": {"title": "<routine name>", "volume_kg": 6240.0, "minutes": 52}}
        {"kind": "achievement",
         "achievement": {"id", "key", "name", "category", "points", "icon_url",
                         "badge_color", "is_rare"}}

    `volume_kg` is Σ weight × reps of the session's sets that have both, the figure the
    session's own detail answers as `total_volume`; `minutes` its length, rounded, null
    when it has no end. A share whose session or badge no longer exists is null.
    """
    from django.db.models import ExpressionWrapper, FloatField, Sum
    from routine.models import WorkoutSession

    session_type, badge_type = _share_types()
    wanted = {session_type.pk: set(), badge_type.pk: set()}
    for post in posts:
        if post.content_type_id in wanted and post.object_id:
            wanted[post.content_type_id].add(post.object_id)

    sessions = {}
    if wanted[session_type.pk]:
        volume = Sum(ExpressionWrapper(F('set_logs__weight') * F('set_logs__reps'),
                                       output_field=FloatField()),
                     filter=Q(set_logs__weight__gt=0, set_logs__reps__gt=0))
        for session in (WorkoutSession.objects.filter(pk__in=wanted[session_type.pk])
                        .select_related('routine').annotate(volume=volume)):
            minutes = None
            if session.start_time and session.end_time:
                minutes = int(round((session.end_time - session.start_time)
                                    .total_seconds() / 60))
            sessions[session.pk] = {
                'kind': 'workout',
                'workout': {'title': session.routine.name,
                            'volume_kg': round(session.volume or 0.0, 1),
                            'minutes': minutes},
            }
    badges = {}
    if wanted[badge_type.pk]:
        for badge in Achievement.objects.filter(pk__in=wanted[badge_type.pk]):
            badges[badge.pk] = {'kind': 'achievement',
                                'achievement': badge_card(badge, request)}

    shares = {}
    for post in posts:
        found = None
        if post.content_type_id == session_type.pk:
            found = sessions.get(post.object_id)
        elif post.content_type_id == badge_type.pk:
            found = badges.get(post.object_id)
        shares[post.pk] = found
    return shares


class PostListSerializer(serializers.ListSerializer):
    """A page of posts: their shares are loaded for the whole page before any row is
    drawn, so a feed of 50 shared workouts costs two queries, not fifty."""

    def to_representation(self, data):
        from django.db import models as db_models

        items = list(data.all() if isinstance(data, db_models.manager.BaseManager)
                     else data)
        self.child._shares = load_shares(items, self.context.get('request'))
        try:
            return [self.child.to_representation(item) for item in items]
        finally:
            self.child._shares = None


class PostSerializer(serializers.ModelSerializer):
    """Serializer for social posts

    **The share card** (2026-09-24, BACKEND_BUGS B-080). A post can carry the finished
    workout or the badge it shares — the card Write attaches and the Feed draws.

    Write (create or PATCH, both optional, at most one of them non-null):
      `share_workout_session` — the id of one of YOUR workout sessions, finished
          (`status: "completed"`); null clears the share.
      `share_achievement` — the id of a badge (`/api/achievements/` id) YOU have
          earned; null clears it.
    When a share is set and the request names no `post_type`, it becomes `workout` /
    `achievement`. Refusals are `400 validation_error` with the field's code in
    `field_errors`: `share_not_yours` (no such session of yours — another person's is
    answered the same), `share_workout_unfinished`, `share_badge_not_earned` (or no such
    badge), `share_one_thing` (both sent).

    Read: `share` — null, or `{"kind": "workout", "workout": {title, volume_kg,
    minutes}}` or `{"kind": "achievement", "achievement": {id, key, name, category,
    points, icon_url, badge_color, is_rare}}` (`load_shares`). Only what the card draws:
    no session id, no sets, nothing about the routine beyond its name.
    """

    image = SecureImageField(required=False, allow_null=True, max_bytes=5 * 1024 * 1024)
    author = UserMinimalSerializer(read_only=True)
    # Not `ReadOnlyField`: the stored column counts every like and comment there is,
    # and a blocked person's are not rows this viewer can fetch. See
    # `get_likes_count`.
    likes_count = serializers.SerializerMethodField()
    comments_count = serializers.SerializerMethodField()
    is_liked = serializers.SerializerMethodField()
    share = serializers.SerializerMethodField()
    share_workout_session = serializers.IntegerField(
        write_only=True, required=False, allow_null=True, min_value=1)
    share_achievement = serializers.IntegerField(
        write_only=True, required=False, allow_null=True, min_value=1)

    class Meta:
        model = Post
        fields = [
            'id', 'author', 'post_type', 'title', 'content',
            'image', 'visibility', 'created_at', 'updated_at',
            'likes_count', 'comments_count', 'is_liked', 'views_count',
            'shares_count', 'share', 'share_workout_session', 'share_achievement',
        ]
        read_only_fields = ['id', 'author', 'created_at', 'updated_at']
        list_serializer_class = PostListSerializer

    def validate(self, attrs):
        """Turn a requested share into the post's generic relation, and check it is the
        poster's own to share (B-080)."""
        attrs = super().validate(attrs)
        wants_session = 'share_workout_session' in attrs
        wants_badge = 'share_achievement' in attrs
        session_id = attrs.pop('share_workout_session', None)
        badge_id = attrs.pop('share_achievement', None)
        if not (wants_session or wants_badge):
            return attrs
        if session_id is not None and badge_id is not None:
            raise serializers.ValidationError(
                {'share_achievement': _('A post shares one thing: a workout or a badge.')},
                code='share_one_thing')

        request = self.context.get('request')
        poster = getattr(request, 'user', None)
        session_type, badge_type = _share_types()
        if session_id is not None:
            from routine.models import WorkoutSession

            session = (WorkoutSession.objects.filter(pk=session_id, user=poster)
                       .only('id', 'status').first()) if poster is not None else None
            if session is None:
                raise serializers.ValidationError(
                    {'share_workout_session': _('You can only share a workout of your own.')},
                    code='share_not_yours')
            if session.status != 'completed':
                raise serializers.ValidationError(
                    {'share_workout_session': _('Only a finished workout can be shared.')},
                    code='share_workout_unfinished')
            attrs['content_type'], attrs['object_id'] = session_type, session.pk
            kind = 'workout'
        elif badge_id is not None:
            earned = poster is not None and UserAchievement.objects.filter(
                user=poster, achievement_id=badge_id,
                achievement__is_active=True).exists()
            if not earned:
                raise serializers.ValidationError(
                    {'share_achievement': _('You can only share a badge you have earned.')},
                    code='share_badge_not_earned')
            attrs['content_type'], attrs['object_id'] = badge_type, badge_id
            kind = 'achievement'
        else:
            # Explicit nulls: take the share off the post.
            attrs['content_type'], attrs['object_id'] = None, None
            kind = None
        if kind is not None and 'post_type' not in attrs and self.instance is None:
            attrs['post_type'] = kind
        return attrs

    def get_share(self, obj):
        shares = getattr(self, '_shares', None)
        if shares is not None and obj.pk in shares:
            return shares[obj.pk]
        if not obj.content_type_id or not obj.object_id:
            return None
        return load_shares([obj], self.context.get('request')).get(obj.pk)
    
    def get_likes_count(self, obj):
        """The stored counter, less the likes from accounts this viewer cannot see.

        `likes_count` is denormalised and maintained with `F()` (social/views.py), so
        it counts everyone. After a block it therefore exceeded the rows the viewer
        could reach — a post showing "3 likes" whose like list has two entries tells
        the viewer the blocked account is still there.

        `PostViewSet` annotates `hidden_likes` for the whole page in its own query; the
        fallback costs one query per row and only runs outside that viewset.
        """
        return max(0, (obj.likes_count or 0) - self._hidden_likes(obj))

    def get_comments_count(self, obj):
        """The stored counter, less every comment on this post the viewer cannot open.

        Two kinds are unreadable and both used to be counted here. A comment by an
        account this viewer has blocked, which `CommentViewSet` excludes; and a comment
        a moderator hid (`is_hidden`), which `CommentViewSet` also excludes and which
        nothing ever subtracts from the denormalised column — so a post whose only
        reply had been hidden read "1 comment" and opened on an empty thread. The
        number here is the number of rows `GET /api/social/comments/?post={id}` will
        hand this viewer.
        """
        return max(0, (obj.comments_count or 0) - self._hidden_comments(obj))

    def _hidden_likes(self, obj):
        annotated = getattr(obj, 'hidden_likes', None)
        if annotated is not None:
            return annotated
        hidden = _hidden_from(self)
        if not hidden:
            return 0
        return PostLike.objects.filter(post=obj, user_id__in=hidden).count()

    def _hidden_comments(self, obj):
        """Comments on this post this viewer cannot open: hidden, or by a blocked author.

        `PostViewSet` annotates this for the whole page in its own query and is the
        only place `PostSerializer` is used with a queryset, so the fallback below runs
        for a single instance — the 201 body of a create, whose counter is zero and
        which therefore costs nothing.
        """
        annotated = getattr(obj, 'hidden_comments', None)
        if annotated is not None:
            return annotated
        if not (obj.comments_count or 0):
            return 0
        unreadable = Q(is_hidden=True)
        hidden = _hidden_from(self)
        if hidden:
            unreadable |= Q(author_id__in=hidden)
        return Comment.objects.filter(post=obj).filter(unreadable).count()

    def get_is_liked(self, obj):
        """Check if current user has liked this post"""
        # Prefer annotated field to avoid N+1 queries
        if hasattr(obj, 'is_liked_anno'):
            return obj.is_liked_anno

        request = self.context.get('request')
        if request and request.user.is_authenticated:
            return PostLike.objects.filter(
                user=request.user,
                post=obj
            ).exists()
        return False


class CommentSerializer(serializers.ModelSerializer):
    """Serializer for post comments"""
    
    author = UserMinimalSerializer(read_only=True)
    # Same reason as `PostSerializer.likes_count`: the stored counter counts a blocked
    # person's like, and the viewer cannot see it.
    likes_count = serializers.SerializerMethodField()
    is_liked = serializers.SerializerMethodField()

    class Meta:
        model = Comment
        fields = [
            'id', 'post', 'author', 'content', 'parent_comment',
            'created_at', 'updated_at', 'likes_count', 'is_liked'
        ]
        read_only_fields = ['id', 'author', 'created_at', 'updated_at']

    def validate(self, attrs):
        """Where a comment lives is fixed at creation, and a reply lives in its parent's
        thread.

        `post` and `parent_comment` were writable on update and only `perform_create`
        checked visibility, so the author of any comment could PATCH it into another
        user's private post (and leave both posts' comment counters wrong). A reply
        could also name a parent comment from a different, hidden post.
        """
        attrs = super().validate(attrs)
        if self.instance is not None:
            for name in ('post', 'parent_comment'):
                if name in attrs and attrs[name] != getattr(self.instance, name):
                    raise serializers.ValidationError(
                        {name: _('A comment cannot be moved.')})
            return attrs
        parent = attrs.get('parent_comment')
        post = attrs.get('post')
        if parent is not None and (post is None or parent.post_id != post.pk
                                   or parent.is_hidden):
            raise serializers.ValidationError(
                {'parent_comment': _('Reply to a comment on the same post.')})
        return attrs

    def get_likes_count(self, obj):
        """The stored counter, less the likes from accounts this viewer cannot see.

        `CommentViewSet` annotates `hidden_likes` for the whole page; the fallback is
        one query per row and only runs outside that viewset.
        """
        hidden_likes = getattr(obj, 'hidden_likes', None)
        if hidden_likes is None:
            hidden = _hidden_from(self)
            hidden_likes = (CommentLike.objects.filter(
                comment=obj, user_id__in=hidden).count() if hidden else 0)
        return max(0, (obj.likes_count or 0) - hidden_likes)

    def get_is_liked(self, obj):
        """Whether the current user has liked this comment.

        Prefers the `is_liked_annotated` EXISTS annotation added by
        CommentViewSet.get_queryset — running the .exists() below per row was one
        query per comment (the endpoint measured 42 queries for 30 comments).
        The fallback keeps the serializer correct outside that viewset.
        """
        annotated = getattr(obj, 'is_liked_annotated', None)
        if annotated is not None:
            return annotated
        request = self.context.get('request')
        if request and request.user.is_authenticated:
            return CommentLike.objects.filter(
                user=request.user,
                comment=obj
            ).exists()
        return False


class ChallengeSerializer(serializers.ModelSerializer):
    """Serializer for community challenges"""

    image = SecureImageField(required=False, allow_null=True, max_bytes=5 * 1024 * 1024)
    creator = UserMinimalSerializer(read_only=True)
    participants_count = serializers.ReadOnlyField()
    is_joined = serializers.SerializerMethodField()
    user_progress = serializers.SerializerMethodField()
    is_active = serializers.ReadOnlyField()
    #: The server counts this challenge's progress from logged training (`metric` is
    #: not `manual`), so the app offers «ابدأ تمرين اليوم» and no manual log
    #: (BACKEND_BUGS B-075). `metric` itself is writable, default `manual`.
    auto_counted = serializers.ReadOnlyField()

    class Meta:
        model = Challenge
        fields = [
            'id', 'creator', 'title', 'description', 'challenge_type',
            'target_value', 'unit', 'start_date', 'end_date',
            'max_participants', 'participants_count', 'status',
            'created_at', 'rules', 'reward_description', 'image',
            'is_joined', 'user_progress', 'is_active', 'metric', 'auto_counted',
        ]
        read_only_fields = ['id', 'creator', 'created_at', 'participants_count']

    def validate_status(self, value):
        """Only a CLOSE is a decision. 'upcoming' and 'active' follow from the dates, so
        accepting them from a client produced a write that read back as something else."""
        if value not in Challenge.TERMINAL_STATUSES:
            raise serializers.ValidationError(
                _('A challenge starts and ends by its dates; only closing it is a choice.'))
        return value

    def to_representation(self, instance):
        data = super().to_representation(instance)
        # The stored value only records a close; the phase comes from the dates.
        data['status'] = instance.lifecycle_status
        return data

    def get_is_joined(self, obj):
        """Check if current user has joined this challenge"""
        # Prefer annotated field to avoid N+1 queries
        annotated = getattr(obj, 'is_joined_anno', None)
        if annotated is not None:
            return bool(annotated)
        request = self.context.get('request')
        if request and request.user.is_authenticated:
            return ChallengeParticipation.objects.filter(
                user=request.user,
                challenge=obj
            ).exists()
        return False
    
    def get_user_progress(self, obj):
        """Get current user's progress in this challenge"""
        # Prefer annotated fields if present
        if hasattr(obj, 'cur_value_anno') or hasattr(obj, 'prog_pct_anno') or hasattr(obj, 'rank_anno'):
            if getattr(obj, 'cur_value_anno', None) is None and getattr(obj, 'prog_pct_anno', None) is None and getattr(obj, 'rank_anno', None) is None:
                return None
            return {
                'current_value': getattr(obj, 'cur_value_anno', None) or 0.0,
                'progress_percentage': getattr(obj, 'prog_pct_anno', None) or 0.0,
                'rank': getattr(obj, 'rank_anno', None),
            }
        request = self.context.get('request')
        if request and request.user.is_authenticated:
            # `rank` by the one rule every standing surface reads, counted live, as the
            # annotated path above does (`social/standing.py`, BACKEND_BUGS B-086).
            from .standing import place_or_none

            try:
                participation = ChallengeParticipation.objects.annotate(
                    place=place_or_none()).get(user=request.user, challenge=obj)
                return {
                    'current_value': participation.current_value,
                    'progress_percentage': participation.progress_percentage,
                    'rank': participation.place
                }
            except ChallengeParticipation.DoesNotExist:
                # Optional side effect: swallowing this silently is what made the
                # surrounding failures invisible in logs. Control flow is unchanged.
                logger.debug('suppressed non-fatal error', exc_info=True)
        return None


class AchievementSerializer(serializers.ModelSerializer):
    """A badge of the `achievements` app, in the shape `/api/social/achievements/`
    has always answered.

    Bound to `achievements.Achievement` since SIDE_BUGS S-127: it was bound to
    `social.Achievement`, a catalogue no engine awards, so the social routes listed
    badges nobody could earn and `user_achievements/` was always empty. The field list
    is unchanged — both models carry exactly these columns.
    """
    
    class Meta:
        model = Achievement
        fields = [
            'id', 'name', 'description', 'category', 'criteria',
            'points', 'icon', 'badge_color', 'is_rare', 'is_secret',
            'created_at'
        ]
        read_only_fields = ['id', 'created_at']


class UserAchievementSerializer(serializers.ModelSerializer):
    """Serializer for user achievements"""
    
    achievement = AchievementSerializer(read_only=True)
    user = UserMinimalSerializer(read_only=True)
    
    class Meta:
        model = UserAchievement
        fields = [
            'id', 'user', 'achievement', 'earned_at', 'progress_data'
        ]
        read_only_fields = ['id', 'user', 'earned_at']


from notifications.models import Notification

class NotificationSerializer(serializers.ModelSerializer):
    """
    Serializer for notifications (New System).

    Resolves title/message at read-time from the event template
    using the active language context. Falls back to metadata keys
    for backward-compatible notifications that stored title/body.
    """

    sender = UserMinimalSerializer(source='actor', read_only=True)
    recipient = UserMinimalSerializer(read_only=True)
    notification_type = serializers.CharField(source='event_type', read_only=True)
    title = serializers.SerializerMethodField()
    message = serializers.SerializerMethodField()
    data = serializers.SerializerMethodField()

    class Meta:
        model = Notification
        fields = [
            'id', 'recipient', 'sender', 'notification_type',
            'title', 'message', 'data', 'is_read', 'created_at', 'event_id'
        ]
        read_only_fields = fields

    def _resolve_template(self, obj):
        """Lazily resolve and cache the template render for this notification."""
        cache_attr = '_resolved_template'
        if hasattr(obj, cache_attr):
            return getattr(obj, cache_attr)

        # Try template resolution first (new architecture)
        try:
            from notifications.channels.fcm import _resolve_event_template
            from notifications.template_resolver import NotificationTemplateResolver

            template = _resolve_event_template(obj.event_type)
            if template:
                context = obj.metadata.get('context', {})
                title, body = NotificationTemplateResolver.render(
                    event_type=obj.event_type,
                    template=template,
                    context=context,
                    recipient_id=obj.recipient_id,
                )
                result = (title, body)
                setattr(obj, cache_attr, result)
                return result
        except Exception:
            # Optional side effect: swallowing this silently is what made the
            # surrounding failures invisible in logs. Control flow is unchanged.
            logger.debug('suppressed non-fatal error', exc_info=True)

        # Fallback: legacy notifications that stored title/body in metadata
        title = obj.metadata.get('title', '')
        body = obj.metadata.get('body', '')
        result = (title, body)
        setattr(obj, cache_attr, result)
        return result

    def get_title(self, obj):
        title, _ = self._resolve_template(obj)
        return title

    def get_message(self, obj):
        _, body = self._resolve_template(obj)
        return body

    def get_data(self, obj):
        return obj.metadata.get('data', {}) 