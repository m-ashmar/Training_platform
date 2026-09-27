"""The operator console for the legal pages and the deletion-request queue (registered
on `/dj-admin/` in `admin_dashboard/admin.py`).

**Legal documents.** A draft is edited here and published with the "published" tick;
from then on it is the version the public page shows and it is read-only — the next
wording is a new version, started with the "Start a new draft from this version" action
(it copies the text). Drafts may be deleted; published versions never are.

**Account-deletion requests.** The queue, oldest first. Nothing here deletes an account:
the operator confirms, by writing to the address, that the requester owns it, erases the
account through the existing erasure, and then closes the request with one of the three
actions, which stamp who and when. The page shows whether an active account uses the
address — for the operator only; the public page never says. Nobody adds or deletes a
request here: the page writes them, and retention removes them 180 days after they are
handled (`training_platform/privacy/sources.py`).

**Who may do it.** Operators only — a superuser or an `admin` account
(`admin_dashboard.operators.is_console_operator`); a bare staff account gets 403.
Changing anything needs the change permission, as elsewhere in this console.

Console strings are plain English, like every other page of the console.
"""
from django.contrib import admin, messages
from django.urls import reverse
from django.utils import timezone
from django.utils.html import format_html

from admin_dashboard.operators import is_console_operator
from training_platform.admin_utils import log_admin_action

from .models import AccountDeletionRequest, DeletionRequestStatus, LegalDocument


def _may_change(request, codename):
    return (is_console_operator(request.user)
            and request.user.has_perm(f"legal.{codename}"))


class LegalDocumentAdmin(admin.ModelAdmin):
    list_select_related = ("published_by",)
    list_display = ("kind", "language", "version", "title", "is_published",
                    "published_at", "published_by", "updated_at")
    list_filter = ("kind", "language", "is_published")
    ordering = ("kind", "language", "-version", "-id")
    actions = ["start_new_draft"]
    fields = ("kind", "language", "version", "title", "body", "is_published",
              "published_at", "published_by", "public_page", "created_at", "updated_at")

    def has_module_permission(self, request):
        return is_console_operator(request.user)

    def has_view_permission(self, request, obj=None):
        return is_console_operator(request.user)

    def has_add_permission(self, request):
        return _may_change(request, "add_legaldocument")

    def has_change_permission(self, request, obj=None):
        return _may_change(request, "change_legaldocument")

    def has_delete_permission(self, request, obj=None):
        return (obj is not None and not obj.is_published
                and _may_change(request, "delete_legaldocument"))

    def get_actions(self, request):
        actions = super().get_actions(request)
        actions.pop("delete_selected", None)
        return actions

    def get_readonly_fields(self, request, obj=None):
        fields = ["version", "published_at", "published_by", "public_page", "created_at",
                  "updated_at"]
        if obj is not None:
            # A draft keeps its place in the numbering; a published version keeps
            # everything.
            fields += ["kind", "language"]
            if obj.is_published:
                fields += ["title", "body", "is_published"]
        return fields

    def save_model(self, request, obj, form, change):
        obj.save(published_by=request.user)

    @admin.display(description="Public page")
    def public_page(self, obj):
        if obj is None or not obj.pk:
            return "-"
        url = f"{reverse('legal:' + obj.kind)}?lang={obj.language}"
        return format_html('<a href="{}" target="_blank" rel="noopener">{}</a>', url, url)

    @admin.action(description="Start a new draft from this version",
                  permissions=["change"])
    def start_new_draft(self, request, queryset):
        made = 0
        for document in queryset:
            draft = document.new_draft()
            log_admin_action(request, draft, f"new draft from version {document.version}")
            made += 1
        self.message_user(request, f"{made} draft(s) started.", messages.SUCCESS)


class AccountDeletionRequestAdmin(admin.ModelAdmin):
    list_select_related = ("handled_by",)
    list_display = ("created_at", "email", "status", "matching_account", "handled_at",
                    "handled_by")
    list_filter = ("status",)
    search_fields = ("email",)
    ordering = ("created_at", "id")
    date_hierarchy = "created_at"
    actions = ["mark_erased", "mark_no_account", "mark_declined"]
    # The requester's words are evidence and stay as sent; the status moves by action.
    readonly_fields = ("email", "reason", "status", "matching_account", "handled_at",
                       "handled_by", "created_at")
    fields = ("created_at", "email", "reason", "matching_account", "status",
              "operator_note", "handled_at", "handled_by")

    def has_module_permission(self, request):
        return is_console_operator(request.user)

    def has_view_permission(self, request, obj=None):
        return is_console_operator(request.user)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return _may_change(request, "change_accountdeletionrequest")

    def has_delete_permission(self, request, obj=None):
        return False

    def changelist_view(self, request, extra_context=None):
        waiting = AccountDeletionRequest.objects.filter(
            status=DeletionRequestStatus.OPEN).count()
        extra_context = {**(extra_context or {}),
                         "title": f"Account deletion requests — {waiting} waiting"}
        return super().changelist_view(request, extra_context)

    @admin.display(description="Active account with this address")
    def matching_account(self, obj):
        from users.models import CustomUser

        if not obj.email:
            return "-"
        user = (CustomUser.objects.filter(email__iexact=obj.email, is_active=True)
                .only("pk", "email").first())
        if user is None:
            return "none"
        url = reverse(f"{self.admin_site.name}:users_customuser_change", args=[user.pk])
        return format_html('<a href="{}">account #{}</a>', url, user.pk)

    def _close(self, request, queryset, status, label):
        """Row by row through `save()`, logged — never a bulk update (see
        `training_platform/admin_utils.py`)."""
        now = timezone.now()
        closed = 0
        for row in queryset.filter(status=DeletionRequestStatus.OPEN):
            row.status = status
            row.handled_at = now
            row.handled_by = request.user
            row.save(update_fields=["status", "handled_at", "handled_by"])
            log_admin_action(request, row, f"status -> {status}")
            closed += 1
        self.message_user(request, f"{closed} request(s) closed: {label}.",
                          messages.SUCCESS)

    @admin.action(description="Close: the account was erased", permissions=["change"])
    def mark_erased(self, request, queryset):
        self._close(request, queryset, DeletionRequestStatus.ERASED, "account erased")

    @admin.action(description="Close: no account uses this address", permissions=["change"])
    def mark_no_account(self, request, queryset):
        self._close(request, queryset, DeletionRequestStatus.NO_ACCOUNT, "no account")

    @admin.action(description="Close: declined (ownership not confirmed, or spam)",
                  permissions=["change"])
    def mark_declined(self, request, queryset):
        self._close(request, queryset, DeletionRequestStatus.DECLINED, "declined")
