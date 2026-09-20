from django.contrib import admin
from django.utils.html import format_html

from .models import (
    FLAG_FIELDS, CapabilityChangeLog, ChannelCapabilities, ThemeChangeLog,
    ThemeOverride,
)


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


class ThemeOverrideAdmin(admin.ModelAdmin):
    """The skin an operator publishes. Same handling as capabilities above: every edit
    goes through `save(changed_by=...)` so the log names who did it, and bulk delete is
    removed because `QuerySet.delete()` skips the model's `delete()` and its log.

    The two guards live in the model's `clean()`, so a token name the design system
    does not declare, or a colour pair that would make text unreadable, is reported
    on THIS form — the operator never gets to publish it and find out from a reviewer.
    """
    list_display = ("name", "channels_list", "version_range", "is_enabled",
                    "priority", "starts_at", "ends_at", "token_count", "updated_at")
    list_filter = ("is_enabled",)
    ordering = ("-priority", "-id")
    readonly_fields = ("updated_at", "what_it_overrides")
    fields = ("name", "is_enabled", "priority", "channels", "min_version",
              "max_version", "starts_at", "ends_at", "tokens", "what_it_overrides",
              "updated_at")

    @admin.display(description="Channels")
    def channels_list(self, obj):
        return ", ".join(obj.channels or [])

    @admin.display(description="Versions")
    def version_range(self, obj):
        return f"{obj.min_version} – {obj.max_version or '∞'}"

    @admin.display(description="Tokens")
    def token_count(self, obj):
        return len(obj.tokens or {})

    @admin.display(description="Overrides")
    def what_it_overrides(self, obj):
        """The token names this row moves, so an operator can read the row without
        parsing JSON. Names only — the values are in the field above."""
        from .theme_tokens import ALLOWED_TOKENS

        names = sorted(obj.tokens or {})
        if not names:
            return "—"
        return format_html(
            "<br>".join("{}" for _name in names),
            *[f"{name} ({ALLOWED_TOKENS.get(name, 'unknown')})" for name in names])

    def save_model(self, request, obj, form, change):
        obj.save(changed_by=request.user)

    def delete_model(self, request, obj):
        obj.delete(changed_by=request.user)

    def get_actions(self, request):
        actions = super().get_actions(request)
        actions.pop("delete_selected", None)
        return actions


class ThemeChangeLogAdmin(admin.ModelAdmin):
    list_display = ("created_at", "name", "theme_id_at_the_time", "action", "changed_by")
    list_filter = ("action",)
    readonly_fields = ("theme_id_at_the_time", "name", "action", "changed_by",
                       "old_values", "new_values", "created_at")

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


admin.site.register(ChannelCapabilities, ChannelCapabilitiesAdmin)
admin.site.register(CapabilityChangeLog, CapabilityChangeLogAdmin)
admin.site.register(ThemeOverride, ThemeOverrideAdmin)
admin.site.register(ThemeChangeLog, ThemeChangeLogAdmin)
