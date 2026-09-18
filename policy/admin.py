from django.contrib import admin

from .models import FLAG_FIELDS, CapabilityChangeLog, ChannelCapabilities


class ChannelCapabilitiesAdmin(admin.ModelAdmin):
    """Edits go through `save(changed_by=...)`, so the change log names the operator.

    No bulk delete: `QuerySet.delete()` skips the model's `delete()` and with it the log.
    """
    list_display = ("channel", "min_version", *FLAG_FIELDS, "updated_at")
    list_filter = ("channel",)
    ordering = ("channel", "min_version")
    readonly_fields = ("updated_at",)

    def save_model(self, request, obj, form, change):
        obj.save(changed_by=request.user)

    def delete_model(self, request, obj):
        obj.delete(changed_by=request.user)

    def get_actions(self, request):
        actions = super().get_actions(request)
        actions.pop("delete_selected", None)
        return actions


class CapabilityChangeLogAdmin(admin.ModelAdmin):
    list_display = ("created_at", "channel", "min_version", "action", "changed_by")
    list_filter = ("channel", "action")
    readonly_fields = ("channel", "min_version", "action", "changed_by",
                       "old_values", "new_values", "created_at")

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


admin.site.register(ChannelCapabilities, ChannelCapabilitiesAdmin)
admin.site.register(CapabilityChangeLog, CapabilityChangeLogAdmin)
