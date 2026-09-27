"""The operator console for app releases (registered on `/dj-admin/` in
`admin_dashboard/admin.py`).

One page: every build of every channel, newest first. An operator adds a release, uploads
the APK or names where it is hosted, and ticks "published" when the build is really
available; from then on the release is a record (`releases/models.py`) and only its
notes, its store page, its minimum supported build and the published tick can change.

**Who may do it.** What every phone is told to install: operators only — a superuser or
an `admin` account (`admin_dashboard.operators.is_console_operator`) — and a bare staff
account gets 403 on every page. Looking needs nothing more; adding a release needs the
add permission and publishing or editing one the change permission, like the other
decisions in this console. A release that was never published may be deleted (with its
file); a published one never is — untick "published" to withdraw it.

Console strings are plain English, like every other page of the console.
"""
from django.contrib import admin
from django.utils.html import format_html

from admin_dashboard.operators import is_console_operator
from training_platform.media_storage import media_url

from .models import AppRelease

ADD_PERMISSION = "releases.add_apprelease"
CHANGE_PERMISSION = "releases.change_apprelease"
DELETE_PERMISSION = "releases.delete_apprelease"

#: What a published release shows but no longer lets anyone edit (the model refuses
#: it too; this keeps the form from offering it).
FROZEN_FIELDS = ("channel", "version_name", "build_number", "apk_file", "download_url",
                 "sha256", "size_bytes")


class AppReleaseAdmin(admin.ModelAdmin):
    list_select_related = ("published_by",)
    list_display = ("channel", "version_name", "build_number", "min_supported_build",
                    "is_published", "published_at", "published_by", "file_source",
                    "created_at")
    list_filter = ("channel", "is_published")
    ordering = ("channel", "-build_number", "-id")
    actions = None
    readonly_fields = ("published_at", "published_by", "created_at", "updated_at",
                       "download_link")
    fieldsets = (
        (None, {"fields": ("channel", "version_name", "build_number",
                           "min_supported_build", "is_published")}),
        ("Release notes", {"fields": ("release_notes_ar", "release_notes_en")}),
        ("Direct APK: the file (upload it, or name where it is hosted)", {
            "fields": ("apk_file", "download_url", "sha256", "size_bytes",
                       "download_link")}),
        ("Google Play / App Store build", {"fields": ("store_url",)}),
        ("Record", {"fields": ("published_at", "published_by", "created_at",
                               "updated_at")}),
    )

    # --- who ------------------------------------------------------------------
    def has_module_permission(self, request):
        return is_console_operator(request.user)

    def has_view_permission(self, request, obj=None):
        return is_console_operator(request.user)

    def has_add_permission(self, request):
        return is_console_operator(request.user) and request.user.has_perm(ADD_PERMISSION)

    def has_change_permission(self, request, obj=None):
        return (is_console_operator(request.user)
                and request.user.has_perm(CHANGE_PERMISSION))

    def has_delete_permission(self, request, obj=None):
        return (obj is not None and obj.published_at is None
                and is_console_operator(request.user)
                and request.user.has_perm(DELETE_PERMISSION))

    # --- the form -------------------------------------------------------------
    def get_readonly_fields(self, request, obj=None):
        fields = list(self.readonly_fields)
        if obj is not None and obj.published_at is not None:
            fields += [f for f in FROZEN_FIELDS if f not in fields]
        return fields

    def save_model(self, request, obj, form, change):
        obj.save(published_by=request.user)

    # --- what the list and the page show --------------------------------------
    @admin.display(description="File")
    def file_source(self, obj):
        if obj.apk_file:
            return "uploaded"
        if obj.download_url:
            return "hosted"
        return "-"

    @admin.display(description="Download (as the app is given it)")
    def download_link(self, obj):
        url = (media_url(obj.apk_file) if obj.apk_file else obj.download_url) if obj else ""
        if not url:
            return "-"
        return format_html('<a href="{}" rel="noopener">{}</a>', url, url)
