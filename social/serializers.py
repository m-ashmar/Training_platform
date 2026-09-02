"""
Social Features Serializers

Serializers for social models to handle API data conversion.
"""

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
import logging

logger = logging.getLogger(__name__)


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

    def get_followers_count(self, obj):
        return obj.followers.count()

    def get_following_count(self, obj):
        return obj.following.count()

    def get_posts_count(self, obj):
        return getattr(obj, 'posts', None).count() if hasattr(obj, 'posts') else 0

    def get_is_following(self, obj):
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
    likes_count = serializers.ReadOnlyField()
    comments_count = serializers.ReadOnlyField()
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
    likes_count = serializers.ReadOnlyField()
    is_liked = serializers.SerializerMethodField()
    
    class Meta:
        model = Comment
        fields = [
            'id', 'post', 'author', 'content', 'parent_comment',
            'created_at', 'updated_at', 'likes_count', 'is_liked'
        ]
        read_only_fields = ['id', 'author', 'created_at', 'updated_at']
    
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