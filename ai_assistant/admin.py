"""
admin.py — AI Assistant Admin Configuration

Provides browsable, filterable admin views for debugging production issues
and monitoring AI usage, training data quality, and cost.
"""

from django.contrib import admin
from .models import (
    ChatSession, ChatMessage, AITrainingData,
    UserBehaviorEvent, UserInsight, UsageCost,
)


class ChatMessageInline(admin.TabularInline):
    model = ChatMessage
    readonly_fields = ['role', 'content', 'tool_calls', 'tokens_used', 'created_at']
    fields = ['role', 'content', 'tool_calls', 'tokens_used', 'created_at']
    extra = 0
    ordering = ['created_at']
    show_change_link = False

    def has_add_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(ChatSession)
class ChatSessionAdmin(admin.ModelAdmin):
    list_display = [
        'user', 'title', 'total_messages', 'total_tokens_used',
        'estimated_cost_usd', 'is_active', 'created_at',
    ]
    list_filter = ['is_active', 'created_at']
    search_fields = ['user__username', 'user__email', 'title']
    readonly_fields = [
        'session_id', 'total_tokens_used', 'total_messages',
        'estimated_cost_usd', 'created_at', 'updated_at',
    ]
    inlines = [ChatMessageInline]
    date_hierarchy = 'created_at'


@admin.register(AITrainingData)
class AITrainingDataAdmin(admin.ModelAdmin):
    list_display = [
        'user', 'user_feedback', 'response_tokens',
        'response_latency_ms', 'created_at',
    ]
    list_filter = ['user_feedback', 'created_at']
    search_fields = ['user__username', 'user_message', 'ai_response']
    readonly_fields = [
        'user', 'session', 'user_context_snapshot', 'user_message',
        'tools_called', 'tool_results', 'ai_response',
        'response_tokens', 'response_latency_ms', 'created_at',
    ]
    date_hierarchy = 'created_at'

    def has_add_permission(self, request):
        return False


@admin.register(UserBehaviorEvent)
class UserBehaviorEventAdmin(admin.ModelAdmin):
    list_display = ['user', 'event_type', 'created_at']
    list_filter = ['event_type', 'created_at']
    search_fields = ['user__username']
    readonly_fields = ['user', 'event_type', 'event_data', 'created_at']
    date_hierarchy = 'created_at'

    def has_add_permission(self, request):
        return False


@admin.register(UserInsight)
class UserInsightAdmin(admin.ModelAdmin):
    list_display = ['user', 'insight_type', 'confidence', 'expires_at', 'created_at']
    list_filter = ['insight_type', 'created_at']
    search_fields = ['user__username']
    readonly_fields = ['user', 'insight_type', 'content', 'confidence', 'expires_at', 'created_at']


@admin.register(UsageCost)
class UsageCostAdmin(admin.ModelAdmin):
    list_display = ['user', 'date', 'total_messages', 'total_tokens', 'estimated_cost_usd']
    list_filter = ['date']
    search_fields = ['user__username']
    ordering = ['-date', '-estimated_cost_usd']
    readonly_fields = ['user', 'date', 'total_messages', 'total_tokens', 'estimated_cost_usd']
    date_hierarchy = 'date'
