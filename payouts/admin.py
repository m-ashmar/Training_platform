"""The operator console for trainer withdrawals (registered on `/dj-admin/` in
`admin_dashboard/admin.py`). Version one is operator-executed (spec section 8): the
console shows the queue, the operator sends through ShamCash and records the
transaction id.

Two pages:

* **Withdrawal queue** — the open withdrawals (requested, approved, sent), oldest
  request first.
* **Withdrawals** — every withdrawal in any state, with its events.

**Nothing about the money is edited as a field.** Each decision goes through
`payouts/services.py`, under the withdrawal's row lock, and is idempotent; each writes
an immutable `WithdrawalEvent` naming the operator, and a line in the admin log:

* approve — `requested` → `approved` (also a list action);
* sent — `approved` → `sent`, with the ShamCash transaction id it went out with
  (required for a ShamCash withdrawal; one transaction pays one withdrawal);
* confirm — `sent` → `confirmed`: it arrived; the money leaves payout clearing (also a
  list action);
* fail — from any open state → `failed`, with a reason the trainer is shown; the money
  goes back to the trainer's wallet. After `sent`, fail only a transfer that did NOT
  arrive (it bounced, or was never received) — the trainer is paid back in full.

**Who may do it.** Operators only — a superuser or an `admin` account — and a bare staff
account gets 403 (as `trainer_verification/admin.py`): the page shows a person's
payout address. A decision also needs the change permission on withdrawals, like
`TransferReviewAdmin`. Nothing is added or deleted here.

Console strings are plain English, like every other page of the console; the words the
trainer reads (the pushes, the API) go through gettext.
"""
from django import forms
from django.contrib import admin, messages
from django.utils import timezone
from django.utils.html import format_html_join
from django.utils.safestring import mark_safe

from training_platform.admin_utils import log_admin_action

from . import services
from .models import OPEN_STATES, MethodKind, Withdrawal, WithdrawalQueueEntry

CHANGE_PERMISSION = "payouts.change_withdrawal"


def _is_operator(user) -> bool:
    """An actual admin, not merely a staff account."""
    return bool(user and user.is_active
                and (user.is_superuser or getattr(user, "is_admin", False)))


DECISIONS = [
    ("", "— no decision —"),
    ("approve", "Approve"),
    ("sent", "Mark sent (record the ShamCash transaction id)"),
    ("confirm", "Confirm it arrived"),
    ("fail", "Fail — the money goes back to the trainer's wallet"),
]


class WithdrawalDecisionForm(forms.ModelForm):
    """Nothing on the withdrawal is typed here: only the decision and what it rests
    on."""
    decision = forms.ChoiceField(choices=DECISIONS, required=False)
    transfer_id = forms.CharField(
        required=False, max_length=64, label="ShamCash transaction id",
        help_text="For 'Mark sent': the id of the transfer it went out with. One "
                  "transfer pays one withdrawal.")
    note = forms.CharField(
        required=False, max_length=services.MAX_NOTE, widget=forms.Textarea(attrs={"rows": 3}),
        help_text="For 'Fail': the reason, which the trainer is shown. Otherwise an "
                  "optional note kept on the event.")

    class Meta:
        model = Withdrawal
        fields = []


class _WithdrawalAdmin(admin.ModelAdmin):
    form = WithdrawalDecisionForm
    list_select_related = ("trainer", "destination_agent")
    list_display = ("number", "trainer_name", "trainer_verified", "amount", "currency",
                    "state", "destination", "requested_at", "waiting")
    list_filter = ("state", "currency", "destination_kind")
    search_fields = ("number", "trainer__email", "trainer__username",
                     "shamcash_transaction_id")
    readonly_fields = ("number", "trainer", "trainer_verified", "amount", "currency",
                       "state", "destination", "shamcash_transaction_id", "method",
                       "hold_reference", "return_reference", "payout_reference",
                       "failure_reason", "requested_at", "approved_at", "sent_at",
                       "confirmed_at", "failed_at", "cancelled_at", "events_timeline")
    actions = ["approve_selected", "confirm_selected"]

    # --- who ------------------------------------------------------------------
    def has_module_permission(self, request):
        return _is_operator(request.user)

    def has_view_permission(self, request, obj=None):
        return _is_operator(request.user)

    def has_change_permission(self, request, obj=None):
        return _is_operator(request.user) and request.user.has_perm(CHANGE_PERMISSION)

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    # --- columns --------------------------------------------------------------
    @admin.display(description="Trainer", ordering="trainer__email")
    def trainer_name(self, obj):
        trainer = obj.trainer
        name = f"{trainer.first_name} {trainer.last_name}".strip()
        return f"{name} <{trainer.email}>" if name else trainer.email

    @admin.display(description="Verified", boolean=True)
    def trainer_verified(self, obj):
        return bool(obj.trainer.trainer_is_verified)

    @admin.display(description="Destination")
    def destination(self, obj):
        if obj.destination_kind == MethodKind.SHAMCASH:
            return (f"ShamCash {services.grouped(obj.destination_address)} "
                    f"({obj.destination_holder})")
        agent = obj.destination_agent
        return f"Cash at agent #{agent.pk} {agent.display_name}" if agent else "Cash"

    @admin.display(description="Events")
    def events_timeline(self, obj):
        """Every transition, oldest first: who, when, from, to, and the evidence. A
        read-only field rather than an inline: nothing here is ever edited."""
        rows = obj.events.select_related("actor").order_by("created_at", "id")
        return format_html_join(
            mark_safe("<br>"), "{} &middot; {} &rarr; {} &middot; {} {} &middot; {}",
            ((e.created_at.strftime("%Y-%m-%d %H:%M"), e.from_state or "-", e.to_state,
              e.actor_role, getattr(e.actor, "email", "") or "", e.evidence)
             for e in rows)) or "-"

    @admin.display(description="Waiting")
    def waiting(self, obj):
        if obj.state not in OPEN_STATES:
            return "-"
        hours = int((timezone.now() - obj.requested_at).total_seconds() // 3600)
        return f"{hours // 24}d {hours % 24}h"

    # --- decisions ------------------------------------------------------------
    def _run(self, request, obj, decide, verb):
        try:
            done = decide()
        except services.TransitionRefused as exc:
            self.message_user(request, f"{obj.number}: {exc}", messages.ERROR)
            return False
        log_admin_action(request, done, f"Withdrawal {verb} ({done.state})")
        self.message_user(request, f"{done.number}: {verb}.", messages.SUCCESS)
        return True

    def save_model(self, request, obj, form, change):
        """Nothing is saved as a field; a chosen decision goes through the service."""
        decision = form.cleaned_data.get("decision")
        note = form.cleaned_data.get("note", "")
        user = request.user
        if decision == "approve":
            self._run(request, obj, lambda: services.approve(obj.pk, user, note), "approved")
        elif decision == "sent":
            txid = form.cleaned_data.get("transfer_id", "")
            self._run(request, obj, lambda: services.mark_sent(obj.pk, user, txid, note),
                      "marked sent")
        elif decision == "confirm":
            self._run(request, obj, lambda: services.confirm(obj.pk, user, note),
                      "confirmed")
        elif decision == "fail":
            self._run(request, obj, lambda: services.fail(obj.pk, user, note), "failed")

    @admin.action(description="Approve the selected withdrawals", permissions=["change"])
    def approve_selected(self, request, queryset):
        for obj in queryset:
            self._run(request, obj, lambda o=obj: services.approve(o.pk, request.user),
                      "approved")

    @admin.action(description="Confirm the selected withdrawals arrived",
                  permissions=["change"])
    def confirm_selected(self, request, queryset):
        for obj in queryset:
            self._run(request, obj, lambda o=obj: services.confirm(o.pk, request.user),
                      "confirmed")


class WithdrawalAdmin(_WithdrawalAdmin):
    """Every withdrawal, any state, newest first."""
    ordering = ("-requested_at", "-id")


class WithdrawalQueueAdmin(_WithdrawalAdmin):
    """The open withdrawals, oldest request first."""
    list_filter = ("state", "currency")

    def get_queryset(self, request):
        return (super().get_queryset(request)
                .filter(state__in=OPEN_STATES).order_by("requested_at", "id"))


# The default site too, as `trainer_verification/admin.py` does; the console registers
# the same classes on `/dj-admin/`.
admin.site.register(WithdrawalQueueEntry, WithdrawalQueueAdmin)
admin.site.register(Withdrawal, WithdrawalAdmin)
