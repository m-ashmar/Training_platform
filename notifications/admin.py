from django.contrib import admin
from django.utils.html import format_html, mark_safe
from django.db.models import Count
from django.db.models.functions import TruncDay
from django.template.response import TemplateResponse
from django.urls import path
from .models import Notification, UserNotificationPreference

@admin.register(UserNotificationPreference)
class UserNotificationPreferenceAdmin(admin.ModelAdmin):
    list_display = ('user', 'event_type', 'is_enabled', 'channels_summary')
    list_filter = ('event_type', 'is_enabled')
    search_fields = ('user__username', 'event_type')
    
    def channels_summary(self, obj):
        return str(obj.channels)

@admin.register(Notification)
class NotificationAdmin(admin.ModelAdmin):
    list_display = ('recipient', 'actor', 'event_type', 'status_summary', 'created_at', 'event_id_short')
    list_filter = ('event_type', 'created_at', 'is_read')
    search_fields = ('recipient__username', 'actor__username', 'event_id', 'deduplication_key')
    readonly_fields = ('created_at', 'updated_at', 'metadata_pretty', 'status_pretty', 'event_id')
    ordering = ('-created_at',)
    date_hierarchy = 'created_at'
    
    change_list_template = 'admin/notifications/notification/change_list.html'

    def get_queryset(self, request):
        return super().get_queryset(request).select_related('recipient', 'actor')

    def event_id_short(self, obj):
        return str(obj.event_id)[:8] if obj.event_id else '-'
    event_id_short.short_description = 'Event ID'

    def status_summary(self, obj):
        fcm = obj.status.get('fcm', {})
        if not fcm:
            return "-"
        status = fcm.get('status', 'unknown')
        success = fcm.get('success_count', 0)
        failure = fcm.get('failure_count', 0)
        color = 'green' if status == 'sent' else 'red'
        # format_html escapes the interpolated values; mark_safe on an f-string did not,
        # and `status` comes from the FCM response rather than from us.
        return format_html(
            "<span style='color:{}'>{}</span> (S:{} / F:{})", color, status, success, failure
        )

    def metadata_pretty(self, obj):
        import json
        # Notification metadata carries user-influenced content, so it must be escaped
        # before it lands in the admin page.
        return format_html("<pre>{}</pre>", json.dumps(obj.metadata, indent=2))

    def status_pretty(self, obj):
        import json
        return format_html("<pre>{}</pre>", json.dumps(obj.status, indent=2))

    def changelist_view(self, request, extra_context=None):
        # Analytics Data
        response = super().changelist_view(request, extra_context=extra_context)
        
        # Only inject if we are rendering the changelist (not redirection)
        if hasattr(response, 'context_data'):
            # Aggregate data by day
            daily_stats = (
                Notification.objects
                .annotate(date=TruncDay('created_at'))
                .values('date')
                .annotate(count=Count('id'))
                .order_by('-date')[:7]
            )
            
            # Aggregate by Event Type
            type_stats = (
                Notification.objects
                .values('event_type')
                .annotate(count=Count('id'))
                .order_by('-count')[:5]
            )
            
            response.context_data['analytics'] = {
                'daily': list(daily_stats)[::-1], # Reverse for chart
                'types': list(type_stats)
            }
            
        return response

from .models import NotificationFailure

@admin.register(NotificationFailure)
class NotificationFailureAdmin(admin.ModelAdmin):
    list_display = ('event_type', 'created_at', 'retry_count', 'is_resolved')
    list_filter = ('event_type', 'is_resolved', 'created_at')
    search_fields = ('event_type', 'error_message')
    readonly_fields = ('created_at', 'event_payload_pretty', 'stack_trace')
    ordering = ('-created_at',)
    
    def event_payload_pretty(self, obj):
        import json
        # DLQ payloads originate from failed events — escape before rendering.
        return format_html("<pre>{}</pre>", json.dumps(obj.event_payload, indent=2))
