"""The operator console for the legal pages and the deletion-request queue (registered
on `/dj-admin/` in `admin_dashboard/admin.py`).

**Legal documents.** A draft is edited here and published with the "published" tick;
from then on it is the version the public page shows and it is read-only — the next
wording is a new version, started with the "Start a new draft from this version" action
(it copies the text). Drafts may be deleted; published versions never are.

**Account-deletion requests.** The queue, oldest first. The operator confirms, by writing
to the address, that the requester owns it, then runs "Erase the matching account, then
close as erased" (Road to 10 R4, 2026-11-23): the one erasure (`CustomUser.retire()`),
and the request closed in the same transaction. Before it, nothing here could delete an
account and the console had no page that could, so a web request could not be finished
without a shell. A request for no active account, an operator account, a trainer still
coaching, or an erasure refused stays open with the reason; the money the account still
holds is named, for support to return. The other three actions only close a request,
stamping who and when. The page shows whether an active account uses the
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


def accounts_named_by(address):
    """The ACTIVE accounts a deletion request's address names, by the project's one
    lookup rule (`users/utils.py find_by_email`, BACKEND_BUGS B-033): the address
    lower-cased, then as typed, each matched EXACTLY. Never `iexact`: Postgres folds the
    dotless ı to I and the long ſ to S where Python's `lower()` does not, so a request
    its owner confirmed for `samer@gmaıl.com` reached the real `samer@gmail.com` (the
    integrator's review of R4, 2026-11-23).

    A list, not one account: when the two spellings name two different accounts (a row
    stored with its case, and an operator-made account that kept its own), both are
    returned and the caller must pick neither. Empty when none does."""
    from users.models import CustomUser
    from users.utils import find_by_email

    found = find_by_email(address, is_active=True)
    if found is None:
        return []
    typed = (address or "").strip()
    # `__in` is exact equality: the same rule, asked for the account it did not return.
    rivals = (CustomUser.objects.filter(email__in={typed, typed.lower()}, is_active=True)
              .exclude(pk=found.pk).order_by("pk"))
    return [found, *rivals]


def _log_or_fail(request, obj, message):
    """A console log entry that RAISES when it cannot be written: inside the erasure's
    transaction, so an erasure is never committed without its record.
    `log_admin_action` swallows a failure, which is right for a closing stamp and wrong
    here."""
    from django.contrib.admin.models import CHANGE, LogEntry
    from django.contrib.contenttypes.models import ContentType

    LogEntry.objects.log_action(
        user_id=request.user.pk,
        content_type_id=ContentType.objects.get_for_model(obj.__class__).pk,
        object_id=obj.pk, object_repr=str(obj), action_flag=CHANGE,
        change_message=message)


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
    actions = ["erase_and_close", "mark_erased", "mark_no_account", "mark_declined"]
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
        if not obj.email:
            return "-"
        # The rule the erase action uses (`accounts_named_by`); `iexact` showed the real
        # account beside a lookalike address.
        people = accounts_named_by(obj.email)
        if not people:
            return "none"
        if len(people) > 1:
            return "two accounts: " + ", ".join(f"#{person.pk}" for person in people)
        url = reverse(f"{self.admin_site.name}:users_customuser_change",
                      args=[people[0].pk])
        return format_html('<a href="{}">account #{}</a>', url, people[0].pk)

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

    def _money_note(self, money):
        """The money a deleted account leaves, in the operator's words: what support now
        returns to the requester (the privacy policy: "contact us first to get it back")."""
        parts = [f"its wallet holds {row['amount']} {row['currency']}"
                 for row in money["wallet"]]
        parts += [f"it still holds {row['amount']} {row['currency']} of the platform's "
                  f"cash (not settled)" for row in money["agent_cash"]]
        return "; ".join(parts)

    @admin.action(description="Erase the matching account, then close as erased",
                  permissions=["change"])
    def erase_and_close(self, request, queryset):
        """The web path's last step, in the console (Road to 10 R4, 2026-11-23). The
        queue could only be CLOSED: the erasure it pointed to had no console page, so an
        operator could not finish a request without a shell. After confirming ownership
        by writing to the address, this erases the active account that uses it, through
        the one erasure (`CustomUser.retire()`, the registry), and closes the request.

        The account is found by the project's one lookup rule (`accounts_named_by`:
        `find_by_email`, never `iexact`); the page stores the address lower-cased, so the
        account erased is the one whose address is EXACTLY that, and an operator-made
        account that differs only in case is another person's and is never touched.

        Row by row, each in its own transaction, its two log entries inside it. A row
        stays OPEN, with the reason in plain words, when: no active account uses the
        address (close it "no account"); two active accounts could be meant (never
        guessed between);
        the account is staff or a superuser (an operator account is never erased from
        this queue); the account is a trainer who still coaches someone (the owner's
        rule — the trainer ends each coaching first); or the erasure was refused (a held
        payment or a verification document could not be removed — try again later).
        Money the account still holds is named in the message, for support to return.
        """
        from django.db import transaction

        from training_platform.privacy.registry import ErasureRefused
        from training_platform.privacy.views import money_left

        erased = 0
        for row in queryset.filter(status=DeletionRequestStatus.OPEN).order_by("id"):
            label = f"Request #{row.pk} ({row.email})"
            people = accounts_named_by(row.email) if row.email else []
            if not people:
                self.message_user(request, f"{label}: no active account uses this address. "
                                  "Close it with 'no account'.", messages.WARNING)
                continue
            if len(people) > 1:
                self.message_user(
                    request, f"{label}: two active accounts could be meant ("
                    + ", ".join(f"#{person.pk} {person.email}" for person in people)
                    + "). Nothing was erased and the request stays open; confirm with the "
                    "requester which one, and erase it from its own page.", messages.ERROR)
                continue
            person = people[0]
            if person.is_staff or person.is_superuser:
                self.message_user(request, f"{label}: account #{person.pk} is an operator "
                                  "account and is not erased from this queue.",
                                  messages.ERROR)
                continue
            try:
                with transaction.atomic():
                    person.retire(reason=f"web deletion request {row.pk}")
                    row.status = DeletionRequestStatus.ERASED
                    row.handled_at = timezone.now()
                    row.handled_by = request.user
                    row.save(update_fields=["status", "handled_at", "handled_by"])
                    # Inside the transaction, and raising: an erasure is never committed
                    # without its record (the integrator's review, 2026-11-23).
                    _log_or_fail(request, person, f"erased: web deletion request {row.pk}")
                    _log_or_fail(request, row, "status -> erased (account erased)")
            except ErasureRefused as exc:
                if exc.code == "trainer_has_active_clients":
                    reason = (f"the trainer still coaches {exc.extra.get('active_clients', 0)} "
                              "client(s); they must end each coaching first")
                else:
                    reason = ("a held payment or a verification document could not be "
                              "removed; nothing was changed, try again later")
                self.message_user(request, f"{label}: account #{person.pk} was NOT erased: "
                                  f"{reason}. The request stays open.", messages.ERROR)
                continue
            erased += 1
            # Read after the erasure: a held month has been returned into the wallet.
            note = self._money_note(money_left(person))
            if note:
                self.message_user(request, f"{label}: account #{person.pk} erased, but "
                                  f"{note}. Return it to {row.email}.", messages.WARNING)
        self.message_user(request, f"{erased} account(s) erased and request(s) closed.",
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
