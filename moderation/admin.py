"""The operator console: the reports queue, read-only views of blocks and consent, and
the banned-terms list (`BannedTermAdmin`, editable behind its own permissions).

Registered onto the console in `admin_dashboard/admin.py` (the site at `/dj-admin/`),
the way every other page there is. The changelist gives the queue its filters, its
search and its paging for free; what this module adds is the three things a queue needs
and a changelist does not have: a link that OPENS the reported thing, two actions that
close a report, and a rule about who may do it.

**Who may do it.** `AdminSite.admin_view` only asks for `is_active and is_staff`, and
this project has staff accounts that are not operators (the console index test exists
because one of them could read PII). Complaints name people and quote what they wrote,
and closing one is a decision about another person's account, so every permission below
is narrowed to an actual admin. A bare staff account gets 403 on the page and cannot
run either action.

Console strings are plain English, like every other page here: there is no
`LocaleMiddleware` in the stack and the console renders in English whatever the
operator's app language is. The user-facing strings all live in `moderation/views.py`
and `moderation/models.py`, and those do go through gettext.
"""
from django.contrib import admin, messages
from django.utils import timezone
from django.utils.html import format_html

from training_platform.admin_utils import log_admin_action

from .models import AIConsent, BannedTerm, Block, Report, ReportStatus
from .services import open_report_count
from .targets import console_link


def _is_operator(user) -> bool:
    """An actual admin, not merely a staff account."""
    return bool(user and user.is_active
                and (user.is_superuser or getattr(user, "is_admin", False)))


class OperatorOnlyMixin:
    """Every door into a page, closed to anyone who is not an operator."""

    def has_module_permission(self, request):
        return _is_operator(request.user)

    def has_view_permission(self, request, obj=None):
        return _is_operator(request.user)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return _is_operator(request.user)

    def has_delete_permission(self, request, obj=None):
        # A complaint is the platform's record that it acted. Dismissing one is a
        # decision with a name and a time on it; deleting one is not.
        return False


@admin.register(Report)
class ReportAdmin(OperatorOnlyMixin, admin.ModelAdmin):
    """The moderation queue."""

    # Without these, every row re-queried the reporter and the operator: ~2 extra
    # queries per row on a 100-row changelist.
    list_select_related = ("reporter", "handled_by")
    list_display = ("created_at", "status", "reason", "target", "reporter",
                    "handled_by", "handled_at")
    list_filter = ("status", "reason", "target_type")
    search_fields = ("reporter__email", "reporter__username", "note",
                     "resolution_note")
    date_hierarchy = "created_at"
    # What the operator writes, and nothing else. `status` is moved by the two actions
    # or here; the rest is the reporter's account of what happened and is evidence.
    readonly_fields = ("reporter", "target_type", "target_id", "target", "reason",
                       "note", "handled_by", "handled_at", "created_at")
    fields = ("created_at", "reporter", "target_type", "target_id", "target", "reason",
              "note", "status", "resolution_note", "handled_by", "handled_at")
    actions = ["dismiss_reports", "mark_action_taken"]

    @admin.display(description="Target")
    def target(self, obj):
        """The reported thing, as a link into its own console page.

        Plain text when the model has no page, and when the row is gone: `target_id`
        is not a foreign key precisely so a deleted post cannot take the complaint
        about it with it, which means a dangling id is a normal state here, not a bug.
        """
        url = console_link(obj.target_type, obj.target_id, self.admin_site.name)
        label = f"{obj.get_target_type_display()} #{obj.target_id}"
        if not url:
            return label
        return format_html('<a href="{}">{}</a>', url, label)

    def changelist_view(self, request, extra_context=None):
        """Put the number of waiting complaints in the page title — the queue badge."""
        waiting = open_report_count()
        extra_context = {**(extra_context or {}),
                         "title": f"Reports — {waiting} waiting"}
        return super().changelist_view(request, extra_context)

    def save_model(self, request, obj, form, change):
        """Stamp who closed it and when, whenever the change form closes one.

        The two bulk actions do the same thing; this is the path an operator takes when
        they want to write a real resolution note instead of the default one. Without
        it, a report edited by hand would carry an outcome and no accountability.
        """
        if obj.status in (ReportStatus.ACTIONED, ReportStatus.DISMISSED):
            if obj.handled_at is None:
                obj.handled_at = timezone.now()
            if obj.handled_by is None:
                obj.handled_by = request.user
        else:
            # Re-opened: the previous decision no longer stands, so neither does the
            # name on it.
            obj.handled_by = None
            obj.handled_at = None
        super().save_model(request, obj, form, change)
        log_admin_action(request, obj, f"status -> {obj.status}")

    def _close(self, request, queryset, new_status, default_note):
        """Row by row through `save()`, with an admin-log entry each.

        Never a bulk queryset write: it skips `save()`, every signal and the admin
        log, so a bulk close would leave no record of who closed what — the exact
        thing `handled_by` exists to record. `training_platform/admin_utils.py` says
        the same, and a gate test greps every `admin.py` for the shortcut.
        """
        now = timezone.now()
        closed = 0
        for report in queryset:
            report.status = new_status
            report.handled_by = request.user
            report.handled_at = now
            # An operator who already wrote a reason keeps it; the default is only for
            # the rows that have none.
            if not report.resolution_note:
                report.resolution_note = default_note
            report.save()
            log_admin_action(request, report, f"status -> {new_status}")
            closed += 1
        self.message_user(request, f"{closed} report(s) closed as {new_status}.",
                          messages.SUCCESS)

    @admin.action(description="Dismiss: nothing to answer here", permissions=["change"])
    def dismiss_reports(self, request, queryset):
        self._close(request, queryset, ReportStatus.DISMISSED,
                    "Dismissed in the console.")

    @admin.action(description="Action taken: the target was dealt with", permissions=["change"])
    def mark_action_taken(self, request, queryset):
        self._close(request, queryset, ReportStatus.ACTIONED,
                    "Action taken in the console.")


@admin.register(Block)
class BlockAdmin(OperatorOnlyMixin, admin.ModelAdmin):
    """Who has hidden whom. Read-only: a block belongs to the person who made it.

    Here so an operator reading a complaint can see whether the two have already
    blocked each other — not so anyone can block on their behalf, or undo it for them.
    """

    list_select_related = ("blocker", "blocked")
    list_display = ("created_at", "blocker", "blocked")
    search_fields = ("blocker__email", "blocker__username",
                     "blocked__email", "blocked__username")
    date_hierarchy = "created_at"
    readonly_fields = ("blocker", "blocked", "created_at")

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(AIConsent)
class AIConsentAdmin(OperatorOnlyMixin, admin.ModelAdmin):
    """The record of who allowed the AI coach to read their data, and under which
    policy version. Read-only everywhere: consent that an operator can grant is not
    consent.
    """

    list_select_related = ("user",)
    list_display = ("user", "granted", "policy_version", "granted_at", "revoked_at")
    list_filter = ("policy_version",)
    search_fields = ("user__email", "user__username")
    readonly_fields = ("user", "granted_at", "revoked_at", "policy_version",
                       "created_at", "updated_at")

    @admin.display(boolean=True, description="Granted")
    def granted(self, obj):
        return obj.is_active

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(BannedTerm)
class BannedTermAdmin(admin.ModelAdmin):
    """The banned-terms list (spec B1; `moderation/ugc_filter.py` says how it is
    matched and where).

    **Who may do it.** Operators only, like the rest of this module: a bare staff
    account gets 403. Reading the list is enough to be an operator; ADDING a term needs
    the add permission, CHANGING one the change permission, DELETING one the delete
    permission (`moderation.<action>_bannedterm`) — a superuser has all three. Adding a
    word to this list decides what every account may write, so it is a permission of
    its own, like deciding a verification or a withdrawal.

    Every save and delete takes effect at the next write anywhere (the model's signals
    move the cache's version); Django's admin log records who changed what. The two
    actions go row by row through `save()`, never a bulk update, so each change is
    logged and moves the version.

    The seeded starter list arrives with "needs review" set: filter on it, and untick it
    (or use "Mark as reviewed") once the owner has read a term.
    """

    list_display = ("term", "normalized", "whole_word", "language", "category",
                    "is_active", "needs_review", "updated_at")
    list_filter = ("needs_review", "is_active", "category", "language", "whole_word")
    search_fields = ("term", "normalized", "notes")
    ordering = ("normalized", "id")
    readonly_fields = ("normalized", "created_at", "updated_at")
    fields = ("term", "whole_word", "language", "category", "notes", "is_active",
              "needs_review", "normalized", "created_at", "updated_at")
    actions = ["mark_reviewed", "switch_off"]

    def _may(self, request, action):
        return (_is_operator(request.user)
                and request.user.has_perm(f"moderation.{action}_bannedterm"))

    def has_module_permission(self, request):
        return _is_operator(request.user)

    def has_view_permission(self, request, obj=None):
        return _is_operator(request.user)

    def has_add_permission(self, request):
        return self._may(request, "add")

    def has_change_permission(self, request, obj=None):
        return self._may(request, "change")

    def has_delete_permission(self, request, obj=None):
        return self._may(request, "delete")

    def _each(self, request, queryset, message, **fields):
        changed = 0
        for term in queryset:
            for name, value in fields.items():
                setattr(term, name, value)
            term.save()
            log_admin_action(request, term, message)
            changed += 1
        self.message_user(request, f"{changed} term(s): {message}.", messages.SUCCESS)

    @admin.action(description="Mark as reviewed (keep as it is)", permissions=["change"])
    def mark_reviewed(self, request, queryset):
        self._each(request, queryset, "reviewed", needs_review=False)

    @admin.action(description="Switch off (keep listed, match nowhere)",
                  permissions=["change"])
    def switch_off(self, request, queryset):
        self._each(request, queryset, "switched off", is_active=False)
