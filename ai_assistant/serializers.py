"""
serializers.py — DRF serializers for AI assistant REST endpoints.
"""

from rest_framework import serializers

from training_platform.disclaimers import ai_coach_disclaimer

from .models import ChatSession, ChatMessage, AITrainingData
from .store_safe import store_safe_reply


class ChatMessageSerializer(serializers.ModelSerializer):
    """One message of a chat with the AI coach.

    `medical_disclaimer` (spec §9 B4, additive): on an assistant message, the sentence
    the app shows under every AI coach reply, in the reader's language; null on the
    person's own messages.

    Differs by build: a reply whose id is in the context's `store_safe_ids` — on a
    build that may not sell, a reply to a payment question (`ChatMessageListView`) —
    is shown as the store-safe reply, whole. Every other reply is shown as written.
    """
    medical_disclaimer = serializers.SerializerMethodField()

    class Meta:
        model = ChatMessage
        # `feedback` (`positive` | `negative` | null): the 👍/👎 given on this reply, so
        # the app can show it after a reload. Additive (SIDE_BUGS S-061).
        fields = ['id', 'role', 'content', 'tool_calls', 'tokens_used', 'created_at',
                  'feedback', 'medical_disclaimer']
        read_only_fields = ['id', 'role', 'content', 'tool_calls', 'tokens_used',
                            'created_at', 'feedback']

    def get_medical_disclaimer(self, obj):
        return ai_coach_disclaimer() if obj.role == 'assistant' else None

    def to_representation(self, obj):
        data = super().to_representation(obj)
        if obj.id in self.context.get('store_safe_ids', ()):
            data['content'] = store_safe_reply()
        return data


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
    # `null` clears a rating (the owner, F12, 2026-09-24: 👍 then 👎 then none). The key
    # is still required: a body that forgets it is a 400, never a silent clear.
    feedback = serializers.ChoiceField(choices=['positive', 'negative'], allow_null=True)


class AITrainingConsentSerializer(serializers.Serializer):
    """`{granted, set_at}` — the person's own answer to "help us improve".

    `set_at` is null until they have answered once, which is how the app tells "never
    asked" from "said no". Read from the user row (`CustomUser.ai_training_consent`,
    `ai_training_consent_set_at`).
    """

    granted = serializers.BooleanField(source='ai_training_consent', read_only=True)
    set_at = serializers.DateTimeField(source='ai_training_consent_set_at', read_only=True)


class _JSONBoolean(serializers.BooleanField):
    """`true` or `false` as JSON, and nothing else.

    DRF's BooleanField also takes `"yes"`, `"on"`, `1` and friends. A consent is
    switched on by the person's own tap, which the app sends as a JSON boolean; a
    string or a number is a client bug, and a client bug must not be able to opt
    anybody in. Refused with DRF's own "Must be a valid boolean." (`invalid`).
    """

    def to_internal_value(self, data):
        if isinstance(data, bool):
            return data
        self.fail('invalid', input=data)


class AITrainingConsentWriteSerializer(serializers.Serializer):
    """`{granted: true|false}` — required; there is no default to fall back on."""

    granted = _JSONBoolean()
