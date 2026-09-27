import uuid
from decimal import Decimal

from django import forms
from django.contrib import admin, messages
from django.utils import timezone
from django.utils.html import format_html, format_html_join
from django.utils.safestring import mark_safe
from django.db.models import Count, DecimalField, Q, Sum, Value
from django.db.models.functions import Coalesce
from .models import (
    Wallet,
    Transaction,
    AgentProfile,
    AgentAPIKey,
    AgentReceivable,
    AgentSettlement,
    ExchangeRate,
    WalletAuditLog,
    IdempotencyKey,
)


class ApiKeyIdFilter(admin.SimpleListFilter):
    title = "API Key"
    parameter_name = "api_key_id"

    def lookups(self, request, model_admin):
        keys = AgentAPIKey.objects.values_list("key_id", flat=True).order_by("key_id").distinct()
        return [(k, k) for k in keys if k]

    def queryset(self, request, queryset):
        value = self.value()
        if value:
            return queryset.filter(metadata__api_key_id=value)
        return queryset


class LedgerReadOnlyAdmin(admin.ModelAdmin):
    """Money is read here and changed only through the code that records it.

    A ModelAdmin makes every concrete field editable by default, and these four classes
    declared only `list_display` and friends — so `Wallet.balance` was a plain form
    input. Setting it to 999999 was a 302 and a committed row, with zero `Transaction`
    rows and zero `WalletAuditLog` entries: money created outside `move_funds_atomic`,
    invisible to any reconciliation against the ledger, and absent from the
    tamper-evident chain because there was nothing to tamper with.

    The ledger is the same in the other direction: `Transaction.amount` and
    `reference_id` — the idempotency and reconciliation handle — were editable, and rows
    were deletable.

    Read-only, then. Money moves through `move_funds_atomic`, `PaymentService` and the
    escrow helpers, all of which write an audit row; nothing here should be a second way.
    """

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(Wallet)
class WalletAdmin(LedgerReadOnlyAdmin):
    list_display = ("owner", "owner_type", "balance", "currency", "created_at")
    search_fields = ("owner__email", "owner__username")
    list_filter = ("owner_type", "currency")


@admin.register(Transaction)
class TransactionAdmin(LedgerReadOnlyAdmin):
    list_display = (
        "reference_id",
        "tx_type",
        "agent_name",
        "api_key_used",
        "charged_amount",
        "client_name",
        "client_current_balance",
        "created_at",
    )
    list_filter = ("tx_type", "currency", "created_at", "actor", ApiKeyIdFilter)
    search_fields = (
        "reference_id",
        "source_wallet__owner__email",
        "destination_wallet__owner__email",
        "metadata__api_key_id",
    )

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        return qs.select_related(
            "actor",
            "source_wallet__owner",
            "destination_wallet__owner",
        )

    def agent_name(self, obj):
        user = getattr(obj, "actor", None)
        return getattr(user, "username", None) or getattr(user, "email", None) or "-"
    agent_name.short_description = "Agent"

    def api_key_used(self, obj):
        return (obj.metadata or {}).get("api_key_id") or "-"
    api_key_used.short_description = "API Key"

    def charged_amount(self, obj):
        return obj.amount
    charged_amount.short_description = "Charged"

    def client_name(self, obj):
        dst = getattr(obj, "destination_wallet", None)
        if dst and getattr(dst, "owner", None):
            owner = dst.owner
            return getattr(owner, "username", None) or getattr(owner, "email", None)
        return "-"
    client_name.short_description = "Client"

    def client_current_balance(self, obj):
        dst = getattr(obj, "destination_wallet", None)
        return getattr(dst, "balance", None)
    client_current_balance.short_description = "Client Balance"


@admin.register(AgentProfile)
class AgentProfileAdmin(admin.ModelAdmin):
    #: The spend ceiling, the kill-switch and the IP allowlist are deliberate operator
    #: controls and stay editable; which account the profile belongs to does not.
    readonly_fields = ("user", "wallet_type", "key_revoked_at")
    list_display = ("user", "display_name", "city", "is_listed", "wallet_type",
                    "per_transaction_limit", "daily_limit", "monthly_limit",
                    "receivable_ceiling", "daily_limit_usd", "monthly_limit_usd",
                    "status")
    list_filter = ("wallet_type", "status", "is_listed", "city")
    search_fields = ("user__email", "user__username", "display_name", "city", "area")
    fieldsets = (
        (None, {"fields": ("user", "wallet_type", "status", "key_revoked_at")}),
        # One set of caps per currency: the plain names are Syrian pounds, `_usd` US
        # dollars. 0 means no top-ups in that currency (SIDE_BUGS S-059). An empty
        # per-top-up cap means none of its own (the daily cap still bounds it); an empty
        # ceiling means the monthly cap (plan step 9, wallet/agents.py).
        ("Limits and access", {"fields": ("per_transaction_limit", "daily_limit",
                                          "monthly_limit", "per_transaction_limit_usd",
                                          "daily_limit_usd", "monthly_limit_usd",
                                          "ip_allowlist")}),
        # The cash an agent may hold for the platform, unsettled, before top-ups stop;
        # and the next settlement date the agent's app shows (nothing schedules one: it
        # is whatever an operator writes here, and nothing once it has passed).
        ("Cash held for the platform", {"fields": ("receivable_ceiling",
                                                   "receivable_ceiling_usd",
                                                   "next_settlement_on")}),
        # What a client sees on the TopUp screen. `is_listed` is the switch, and it is
        # off until the agent has agreed to be published: `public_phone` is the shop
        # line they gave us for that, never the number they registered with.
        ("Public listing (TopUp screen)", {
            "fields": ("is_listed", "display_name", "city", "area", "opening_hours",
                       "public_phone")}),
    )


@admin.register(AgentAPIKey)
class AgentAPIKeyAdmin(admin.ModelAdmin):
    list_display = ("agent", "key_id", "device_name", "is_active", "created_at",
                    "last_signature_at", "last_rotated_at")
    list_filter = ("is_active",)
    search_fields = ("key_id", "agent__user__email")
    #: `hashed_key` and `secret_ciphertext` were editable inputs, and the ciphertext was
    #: rendered into the page. Overwriting either bricks a live agent's credential with
    #: no log entry. Only the kill-switch stays writable, which is the one thing an
    #: operator legitimately needs here.
    readonly_fields = ("agent", "key_id", "hashed_key", "device_name", "created_at",
                       "last_signature_at", "last_rotated_at")
    #: Never rendered at all, read-only or not.
    exclude = ("secret_ciphertext",)

    def has_add_permission(self, request):
        # A key is minted by the rotation endpoint, which also encrypts the secret and
        # writes the audit row. A key created by hand cannot sign anything.
        return False

    def changelist_view(self, request, extra_context=None):
        # Alert if any agent has multiple active keys (should not happen)
        multi_active = (
            AgentAPIKey.objects.filter(is_active=True)
            .values("agent")
            .annotate(cnt=Count("id"))
            .filter(cnt__gt=1)
        )
        if multi_active.exists():
            messages.warning(
                request,
                "Multiple active API keys detected for one or more agents. Please rotate to a single active key.",
            )
        return super().changelist_view(request, extra_context=extra_context)


@admin.register(WalletAuditLog)
class WalletAuditLogAdmin(LedgerReadOnlyAdmin):
    """Append-only means append-only, including from here.

    `WalletAuditLog.delete()` raises `PermissionError`, and Django's `delete_selected`
    action calls `QuerySet.delete()`, which never invokes the model's `delete()` — so the
    chain was deletable from the admin. `verify_chain()` walks only the rows that still
    exist, starting from a zero hash, so deleting the NEWEST entries (exactly the ones
    recording what was just done) left it reporting VALID. Migration 0012 now refuses the
    DELETE at the database, which is the only layer every path goes through.
    """

    list_display = ("event_type", "actor", "ip_address", "request_id", "created_at")
    list_filter = ("event_type", "created_at")
    search_fields = ("request_id", "actor__email", "path")


@admin.register(IdempotencyKey)
class IdempotencyKeyAdmin(LedgerReadOnlyAdmin):
    """The replay guard. Flipping `processed` back to false re-opens a spent request."""

    list_display = ("key", "created_by", "processed", "created_at")
    list_filter = ("processed",)
    search_fields = ("key", "created_by__email")




@admin.register(ExchangeRate)
class ExchangeRateAdmin(admin.ModelAdmin):
    """The operator's page for the USD/SYP rate: set a new one, read the history.

    THE OWNER SETS THE RATE (2026-09-21): "1 usd = x sp", from here. Three things this
    page has to get right, all of them about the fact that a rate is history and not a
    setting:

    * **Adding is the only edit.** A rate row is append-only in the model and here:
      nothing is changeable, nothing is deletable, and bulk delete is removed because
      `QuerySet.delete()` skips the model's `delete()`. Editing yesterday's rate would
      re-value every purchase struck at it.
    * **Who and when are recorded**, like `policy.CapabilityChangeLog`: `set_by` is
      stamped from the signed-in operator and the whole table IS the change log, so
      there is no second one to keep in step.
    * **The age is on the page.** A rate nobody has refreshed is a money bug that looks
      like nothing at all, so the list shows how old the live one is and says loudly
      when it is past `EXCHANGE_RATE_MAX_AGE_DAYS` — the point at which cross-currency
      selling stops (`wallet.exchange`). Same-currency selling is unaffected, and the
      warning says so rather than implying the platform is down.
    """
    list_display = ("pair", "rate", "effective_from", "status", "set_by", "note",
                    "created_at")
    list_filter = ("base", "quote")
    ordering = ("-effective_from", "-id")
    #: No `base`/`quote` on the form: there is ONE pair, in one direction
    #: (`wallet.currencies.RATE_BASE/RATE_QUOTE`), and `save_model` stamps it. The form
    #: offered both, so an operator could enter "1 SYP = 0.000077 USD", see it marked
    #: live, and have every conversion keep using the stale USD/SYP row it never
    #: replaced (SIDE_BUGS S-156).
    fields = ("rate", "effective_from", "note")
    readonly_fields = ()

    def get_queryset(self, request):
        """The page's rows, each told which one is live — decided ONCE per page.

        The status column asked `ExchangeRate.current()` per row, an N+1 on the rate
        history, and re-implemented the staleness rule beside the one the conversion
        uses (SIDE_BUGS S-157). `wallet.exchange.rate_health()` is that rule, read once
        here and carried on every row.
        """
        from django.db.models import BigIntegerField, Value

        from .exchange import rate_health

        live = rate_health()["rate"]
        return super().get_queryset(request).annotate(
            fx_live_pk=Value(live.pk if live is not None else None,
                             output_field=BigIntegerField()))

    @admin.display(description="Pair")
    def pair(self, obj):
        return f"1 {obj.base} = ? {obj.quote}"

    @admin.display(description="Status")
    def status(self, obj):
        """Live / superseded / queued / not used, and how old the live one is.

        Live means the row `wallet.exchange.current_rate()` returns — the one every
        conversion uses — and stale means what `wallet.exchange.is_stale` says, the same
        rule `convert_at_the_operators_rate` refuses on. A row for any other pair is
        never read by a conversion and says so.
        """
        from .currencies import RATE_BASE, RATE_QUOTE
        from .exchange import current_rate, is_stale, max_age_days, rate_age_days

        if (obj.base, obj.quote) != (RATE_BASE, RATE_QUOTE):
            return format_html(
                '<span style="color:#b00">not used — conversions read only '
                '1 {} = ? {}</span>', RATE_BASE, RATE_QUOTE)
        live_pk = getattr(obj, "fx_live_pk", None)
        if not hasattr(obj, "fx_live_pk"):
            # A row read outside the changelist queryset (a detail page).
            live = current_rate()
            live_pk = live.pk if live is not None else None
        # The label is an argument: format_html() with none is deprecated since Django
        # 5.0 and a TypeError in 6.0.
        if obj.effective_from > timezone.now():
            return format_html('<span style="color:#888">{}</span>', 'queued')
        if live_pk != obj.pk:
            return format_html('<span style="color:#888">{}</span>', 'superseded')
        age = rate_age_days(obj) or 0
        if is_stale(obj):
            return format_html(
                '<b style="color:#b00">LIVE — {} days old, past the {}-day limit: '
                'cross-currency purchases are refused until a new rate is set '
                '(same-currency sales are unaffected)</b>',
                f"{age:.1f}", max_age_days())
        return format_html('<b style="color:#070">live — {} days old</b>', f"{age:.1f}")

    def get_form(self, request, obj=None, **kwargs):
        # The one direction the number is read in, said on the form itself: with no pair
        # to pick, the operator must know which way round to type it.
        from .currencies import RATE_BASE, RATE_QUOTE

        form = super().get_form(request, obj, **kwargs)
        if obj is None and "rate" in form.base_fields:
            form.base_fields["rate"].help_text = (
                f"1 {RATE_BASE} = this many {RATE_QUOTE}. Conversions read only this "
                f"direction.")
        return form

    def get_fields(self, request, obj=None):
        # An existing row shows its pair; the add form has none to choose.
        if obj is not None:
            return ("base", "quote", "rate", "effective_from", "set_by", "note",
                    "created_at")
        return self.fields

    def get_readonly_fields(self, request, obj=None):
        # Every field, once the row exists: append-only means the form may create and
        # may never change.
        if obj is not None:
            return ("base", "quote", "rate", "effective_from", "set_by", "note",
                    "created_at")
        return ()

    def save_model(self, request, obj, form, change):
        from .currencies import RATE_BASE, RATE_QUOTE

        if change:   # defence in depth; the model raises too
            raise PermissionError("ExchangeRate is append-only: add a new rate.")
        # The one pair a conversion reads, whatever a crafted POST carried.
        obj.base, obj.quote = RATE_BASE, RATE_QUOTE
        obj.set_by = request.user
        obj.save()

    def has_change_permission(self, request, obj=None):
        # False for a ROW, true for the changelist, which is how Django asks.
        return obj is None and super().has_change_permission(request, obj)

    def has_delete_permission(self, request, obj=None):
        return False

    def get_actions(self, request):
        actions = super().get_actions(request)
        actions.pop("delete_selected", None)
        return actions


# ---------------------------------------------------------------------------------
# What agents owe the platform (plan step 9; the spec, section 8 "Agents": "show the
# running total in the console, and let operators record settlements against it").
# Registered on `/dj-admin/` in admin_dashboard/admin.py.
#
# Two pages:
#
# * **Agent receivables** — one row per agent: the cash it holds for the platform,
#   unsettled, in each currency, beside the ceiling that stops its top-ups. Opening a
#   row shows the running account and a form that RECORDS A SETTLEMENT: the currency,
#   the amount handed over, a note the agent reads. It goes through
#   `wallet.agents.record_settlement` — under the agent's profile lock, idempotent on the
#   form's own token (a double submit records one settlement), refused above what the
#   agent holds — and writes an immutable `AgentSettlement`. Nothing on the agent is
#   edited here; its caps and ceiling are on the agent profile page.
# * **Agent settlements** — every settlement recorded, read-only.
#
# Operators only — a superuser or an `admin` account; a bare staff account gets 403 (as
# payouts/admin.py) — and recording needs the change permission on agent receivables.
# Console strings are plain English, like the rest of the console.
# ---------------------------------------------------------------------------------
RECEIVABLE_CHANGE_PERMISSION = "wallet.change_agentreceivable"


def _is_operator(user) -> bool:
    """An actual admin, not merely a staff account."""
    return bool(user and user.is_active
                and (user.is_superuser or getattr(user, "is_admin", False)))


def _held(currency):
    return Coalesce(Sum("receivable_entries__amount",
                        filter=Q(receivable_entries__currency=currency)),
                    Value(Decimal("0")),
                    output_field=DecimalField(max_digits=14, decimal_places=2))


class SettlementForm(forms.ModelForm):
    """Nothing on the agent is typed here: only the settlement. The inputs are named
    apart from the model's columns, or `get_form` would drop them as read-only
    (agent-briefing: "An admin form field named like a read-only model field
    vanishes")."""
    settle_currency = forms.ChoiceField(
        required=False, label="Currency", choices=[("", "— no settlement —"),
                                                   ("SYP", "SYP"), ("USD", "USD")])
    settle_amount = forms.DecimalField(
        required=False, max_digits=14, decimal_places=2, min_value=Decimal("0.01"),
        label="Amount handed over",
        help_text="The cash the agent handed to the platform. At most what they hold.")
    settle_note = forms.CharField(
        required=False, max_length=300, label="Note",
        widget=forms.Textarea(attrs={"rows": 2}),
        help_text="The agent reads this on their settlement list.")
    #: The form's own idempotency token: rendered once per page, so submitting the same
    #: page twice records one settlement.
    settle_token = forms.CharField(required=False, widget=forms.HiddenInput,
                                   initial=lambda: uuid.uuid4().hex)

    class Meta:
        model = AgentReceivable
        fields = []

    def clean(self):
        from .agents import receivable_of

        data = super().clean()
        currency, amount = data.get("settle_currency"), data.get("settle_amount")
        if not currency and amount is None:
            return data
        if not currency or amount is None:
            raise forms.ValidationError("A settlement needs both a currency and an amount.")
        if not data.get("settle_token"):
            raise forms.ValidationError("The form lost its token; reload the page.")
        if AgentSettlement.objects.filter(idempotency_key=data["settle_token"]).exists():
            return data          # this very form was already recorded: a replay
        held = receivable_of(self.instance, currency)
        if amount > held:
            raise forms.ValidationError(
                f"The agent holds {held} {currency} for the platform; a settlement of "
                f"{amount} would leave the platform owing them.")
        return data


class AgentReceivableAdmin(admin.ModelAdmin):
    form = SettlementForm
    list_select_related = ("user",)
    list_display = ("agent_account", "display_name", "status", "held_syp",
                    "ceiling_syp", "held_usd", "ceiling_usd", "last_settlement",
                    "next_settlement_on")
    list_filter = ("status", "city")
    search_fields = ("user__email", "user__username", "display_name", "city", "area")
    ordering = ("-created_at", "-id")
    readonly_fields = ("agent_account", "display_name", "status", "held_syp",
                       "ceiling_syp", "held_usd", "ceiling_usd", "next_settlement_on",
                       "account_timeline")

    # --- who ------------------------------------------------------------------
    def has_module_permission(self, request):
        return _is_operator(request.user)

    def has_view_permission(self, request, obj=None):
        return _is_operator(request.user)

    def has_change_permission(self, request, obj=None):
        return (_is_operator(request.user)
                and request.user.has_perm(RECEIVABLE_CHANGE_PERMISSION))

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def get_queryset(self, request):
        return super().get_queryset(request).annotate(held_syp_total=_held("SYP"),
                                                      held_usd_total=_held("USD"))

    # --- columns --------------------------------------------------------------
    @admin.display(description="Agent", ordering="user__email")
    def agent_account(self, obj):
        return obj.user.email

    def _held_in(self, obj, currency):
        total = getattr(obj, f"held_{currency.lower()}_total", None)
        if total is None:
            from .agents import receivable_of
            total = receivable_of(obj, currency)
        return Decimal(total).quantize(Decimal("0.01"))

    def _ceiling_in(self, obj, currency):
        ceiling = obj.receivable_ceiling_for(currency)
        if ceiling is None:
            limits = obj.limits_for(currency)
            ceiling = limits[1] if limits else Decimal("0")
        return Decimal(ceiling).quantize(Decimal("0.01"))

    @admin.display(description="Cash held (SYP)", ordering="held_syp_total")
    def held_syp(self, obj):
        return self._held_in(obj, "SYP")

    @admin.display(description="Ceiling (SYP)")
    def ceiling_syp(self, obj):
        return self._ceiling_in(obj, "SYP")

    @admin.display(description="Cash held (USD)", ordering="held_usd_total")
    def held_usd(self, obj):
        return self._held_in(obj, "USD")

    @admin.display(description="Ceiling (USD)")
    def ceiling_usd(self, obj):
        return self._ceiling_in(obj, "USD")

    @admin.display(description="Last settlement")
    def last_settlement(self, obj):
        row = obj.settlements.order_by("-created_at", "-id").first()
        if row is None:
            return "-"
        return f"{row.number}: {row.amount} {row.currency}"

    @admin.display(description="Account (newest first)")
    def account_timeline(self, obj):
        """The last 50 movements of the account: when, what, how much, the running
        total. A read-only field rather than an inline: nothing here is ever edited."""
        rows = obj.receivable_entries.order_by("-created_at", "-id")[:50]
        return format_html_join(
            mark_safe("<br>"), "{} &middot; {} &middot; {} {} &middot; held after: {} {}",
            ((timezone.localtime(e.created_at).strftime("%Y-%m-%d %H:%M"), e.kind,
              e.amount, e.currency, e.balance_after, e.currency) for e in rows)) or "-"

    # --- the settlement ---------------------------------------------------------
    def save_model(self, request, obj, form, change):
        """Nothing is saved as a field; a settlement goes through the service."""
        from training_platform.admin_utils import log_admin_action

        from . import agents

        data = form.cleaned_data
        if not data.get("settle_currency") or data.get("settle_amount") is None:
            return
        try:
            settlement = agents.record_settlement(
                obj.pk, data["settle_currency"], data["settle_amount"], request.user,
                note=data.get("settle_note", ""), key=data["settle_token"])
        except agents.SettlementRefused as exc:
            self.message_user(request, f"{obj.user.email}: {exc.message}", messages.ERROR)
            return
        log_admin_action(request, obj, f"Settlement {settlement.number} recorded: "
                                       f"{settlement.amount} {settlement.currency}")
        self.message_user(request, f"{settlement.number}: {settlement.amount} "
                                   f"{settlement.currency} recorded.", messages.SUCCESS)


class AgentSettlementAdmin(LedgerReadOnlyAdmin):
    """Every settlement an operator recorded. Read-only: a settlement is never edited or
    deleted (migration wallet/0020 refuses both at the database)."""
    list_select_related = ("agent__user", "recorded_by")
    list_display = ("number", "agent", "amount", "currency", "receivable_before",
                    "receivable_after", "recorded_by", "note", "created_at")
    list_filter = ("currency",)
    search_fields = ("agent__user__email", "agent__display_name", "idempotency_key")
    ordering = ("-created_at", "-id")

    def has_module_permission(self, request):
        return _is_operator(request.user)

    def has_view_permission(self, request, obj=None):
        return _is_operator(request.user)

    def get_actions(self, request):
        actions = super().get_actions(request)
        actions.pop("delete_selected", None)
        return actions


admin.site.register(AgentReceivable, AgentReceivableAdmin)
admin.site.register(AgentSettlement, AgentSettlementAdmin)
