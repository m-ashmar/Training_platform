"""
Social Features Serializers

Serializers for social models to handle API data conversion.
"""

from django.db.models import Q
from django.utils.translation import gettext_lazy as _
from rest_framework import serializers
from .models import (
    UserFollow, Post, PostLike, Comment, CommentLike,
    Challenge, ChallengeParticipation, Achievement,
    UserAchievement
)
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


class PublicUserProfileSerializer(serializers.ModelSerializer):
    """Public-facing user profile serializer with non-sensitive fields and social counts."""
    followers_count = serializers.SerializerMethodField()
    following_count = serializers.SerializerMethodField()
    posts_count = serializers.SerializerMethodField()
    is_following = serializers.SerializerMethodField()
    profile_picture_url = serializers.SerializerMethodField()

    class Meta:
        model = CustomUser
        # NOTE: `email` is deliberately NOT exposed. This endpoint is readable by
        # any authenticated user for any active user, so including email allowed
        # harvesting every account's address by walking ids.
        fields = [
            'id', 'username', 'user_type', 'first_name', 'last_name',
            'profile_picture_url', 'followers_count', 'following_count', 'posts_count',
            'is_following'
        ]
        read_only_fields = fields

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

class UserFollowSerializer(serializers.ModelSerializer):
    """Serializer for user following relationships"""
    
    follower = UserMinimalSerializer(read_only=True)
    following = UserMinimalSerializer(read_only=True)
    # Both sides were read-only, so `following` could never be supplied and every POST
    # to /api/social/follows/ died with a NOT NULL violation (500). This write-only
    # companion field accepts the target id while keeping the nested read output.
    following_id = serializers.PrimaryKeyRelatedField(
        queryset=CustomUser.objects.all(), source='following', write_only=True
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


class PostSerializer(serializers.ModelSerializer):
    """Serializer for social posts"""

    image = SecureImageField(required=False, allow_null=True, max_bytes=5 * 1024 * 1024)
    author = UserMinimalSerializer(read_only=True)
    # Not `ReadOnlyField`: the stored column counts every like and comment there is,
    # and a blocked person's are not rows this viewer can fetch. See
    # `get_likes_count`.
    likes_count = serializers.SerializerMethodField()
    comments_count = serializers.SerializerMethodField()
    is_liked = serializers.SerializerMethodField()

    class Meta:
        model = Post
        fields = [
            'id', 'author', 'post_type', 'title', 'content',
            'image', 'visibility', 'created_at', 'updated_at',
            'likes_count', 'comments_count', 'is_liked', 'views_count',
            'shares_count'
        ]
        read_only_fields = ['id', 'author', 'created_at', 'updated_at']
    
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
    
    class Meta:
        model = Challenge
        fields = [
            'id', 'creator', 'title', 'description', 'challenge_type',
            'target_value', 'unit', 'start_date', 'end_date',
            'max_participants', 'participants_count', 'status',
            'created_at', 'rules', 'reward_description', 'image',
            'is_joined', 'user_progress', 'is_active'
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
            try:
                participation = ChallengeParticipation.objects.get(
                    user=request.user,
                    challenge=obj
                )
                return {
                    'current_value': participation.current_value,
                    'progress_percentage': participation.progress_percentage,
                    'rank': participation.rank
                }
            except ChallengeParticipation.DoesNotExist:
                # Optional side effect: swallowing this silently is what made the
                # surrounding failures invisible in logs. Control flow is unchanged.
                logger.debug('suppressed non-fatal error', exc_info=True)
        return None


class AchievementSerializer(serializers.ModelSerializer):
    """Serializer for achievements"""
    
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