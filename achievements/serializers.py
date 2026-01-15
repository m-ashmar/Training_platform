"""
Achievement Serializers - Rich data serialization for APIs.
"""

from rest_framework import serializers
from .models import Achievement, UserAchievement, AchievementProgress


class AchievementSerializer(serializers.ModelSerializer):
    """Serializer for achievement definitions."""
    
    category_display = serializers.CharField(
        source='get_category_display', 
        read_only=True
    )
    icon_url = serializers.SerializerMethodField()
    
    class Meta:
        model = Achievement
        fields = [
            'id', 'key', 'name', 'description', 'category', 'category_display',
            'criteria', 'points', 'icon_url', 'badge_color',
            'is_rare', 'is_secret', 'created_at'
        ]
        read_only_fields = ['id', 'created_at']
    
    def get_icon_url(self, obj):
        if obj.icon:
            request = self.context.get('request')
            if request:
                return request.build_absolute_uri(obj.icon.url)
            return obj.icon.url
        return None


class AchievementWithProgressSerializer(AchievementSerializer):
    """Achievement with user's progress data."""
    
    progress = serializers.SerializerMethodField()
    is_earned = serializers.SerializerMethodField()
    earned_at = serializers.SerializerMethodField()
    
    class Meta(AchievementSerializer.Meta):
        fields = AchievementSerializer.Meta.fields + [
            'progress', 'is_earned', 'earned_at'
        ]
    
    def get_progress(self, obj):
        request = self.context.get('request')
        if not request or not request.user.is_authenticated:
            return None
        
        from .engine import AchievementEngine
        progress_data = AchievementEngine.get_user_progress(request.user, obj)
        return {
            'current_value': progress_data['current_value'],
            'target_value': progress_data['target_value'],
            'progress_percentage': progress_data['progress_percentage'],
            'remaining': progress_data['remaining'],
        }
    
    def get_is_earned(self, obj):
        request = self.context.get('request')
        if not request or not request.user.is_authenticated:
            return False
        return UserAchievement.objects.filter(
            user=request.user, 
            achievement=obj
        ).exists()
    
    def get_earned_at(self, obj):
        request = self.context.get('request')
        if not request or not request.user.is_authenticated:
            return None
        try:
            ua = UserAchievement.objects.get(user=request.user, achievement=obj)
            return ua.earned_at
        except UserAchievement.DoesNotExist:
            return None


class UserAchievementSerializer(serializers.ModelSerializer):
    """Serializer for user's earned achievements."""
    
    achievement = AchievementSerializer(read_only=True)
    
    class Meta:
        model = UserAchievement
        fields = [
            'id', 'achievement', 'earned_at', 'progress_data', 'is_featured'
        ]
        read_only_fields = ['id', 'achievement', 'earned_at', 'progress_data']


class UserAchievementDetailSerializer(serializers.ModelSerializer):
    """Detailed serializer for a single earned achievement."""
    
    name = serializers.CharField(source='achievement.name', read_only=True)
    description = serializers.CharField(source='achievement.description', read_only=True)
    category = serializers.CharField(source='achievement.category', read_only=True)
    category_display = serializers.CharField(
        source='achievement.get_category_display', 
        read_only=True
    )
    points = serializers.IntegerField(source='achievement.points', read_only=True)
    badge_color = serializers.CharField(source='achievement.badge_color', read_only=True)
    is_rare = serializers.BooleanField(source='achievement.is_rare', read_only=True)
    icon_url = serializers.SerializerMethodField()
    
    class Meta:
        model = UserAchievement
        fields = [
            'id', 'name', 'description', 'category', 'category_display',
            'points', 'badge_color', 'is_rare', 'icon_url',
            'earned_at', 'is_featured'
        ]
    
    def get_icon_url(self, obj):
        if obj.achievement.icon:
            request = self.context.get('request')
            if request:
                return request.build_absolute_uri(obj.achievement.icon.url)
            return obj.achievement.icon.url
        return None


class AchievementProgressSerializer(serializers.Serializer):
    """Serializer for progress towards an unearned achievement."""
    
    achievement = AchievementSerializer(read_only=True)
    current_value = serializers.FloatField()
    target_value = serializers.FloatField()
    progress_percentage = serializers.FloatField()
    remaining = serializers.FloatField()


class LeaderboardEntrySerializer(serializers.Serializer):
    """Serializer for leaderboard entries."""
    
    rank = serializers.IntegerField()
    user_id = serializers.IntegerField()
    username = serializers.CharField()
    total_points = serializers.IntegerField()
    total_achievements = serializers.IntegerField()
    profile_picture_url = serializers.CharField(allow_null=True)


class UserAchievementStatsSerializer(serializers.Serializer):
    """Serializer for user's achievement statistics."""
    
    total_points = serializers.IntegerField()
    total_achievements = serializers.IntegerField()
    rank = serializers.IntegerField()
    categories = serializers.DictField(child=serializers.IntegerField())
    recent = serializers.ListField(child=serializers.DictField())
