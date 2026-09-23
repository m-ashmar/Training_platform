"""
serializers.py — DRF serializers for AI assistant REST endpoints.
"""

from rest_framework import serializers
from .models import ChatSession, ChatMessage, AITrainingData


class ChatMessageSerializer(serializers.ModelSerializer):
    class Meta:
        model = ChatMessage
        # `feedback` (`positive` | `negative` | null): the 👍/👎 given on this reply, so
        # the app can show it after a reload. Additive (SIDE_BUGS S-061).
        fields = ['id', 'role', 'content', 'tool_calls', 'tokens_used', 'created_at',
                  'feedback']
        read_only_fields = fields


class ChatSessionSerializer(serializers.ModelSerializer):
    message_count = serializers.IntegerField(source='total_messages', read_only=True)

    class Meta:
        model = ChatSession
        fields = [
            'session_id', 'title', 'message_count',
            'is_active', 'created_at', 'updated_at',
        ]
        read_only_fields = fields


class ChatSessionDetailSerializer(serializers.ModelSerializer):
    # We removed the fully nested `messages` to enforce scalable API design constraint (pagination)
    
    class Meta:
        model = ChatSession
        fields = [
            'session_id', 'title', 'total_messages', 'total_tokens_used',
            'is_active', 'created_at', 'updated_at',
        ]
        read_only_fields = fields


class FeedbackSerializer(serializers.Serializer):
    message_id = serializers.IntegerField()
    feedback = serializers.ChoiceField(choices=['positive', 'negative'])
