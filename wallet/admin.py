from django.contrib import admin, messages
from django.db.models import Count
from .models import (
    Wallet,
    Transaction,
    AgentProfile,
    AgentAPIKey,
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
    readonly_fields = ("user", "wallet_type")
    list_display = ("user", "display_name", "city", "is_listed", "wallet_type",
                    "daily_limit", "monthly_limit", "status")
    list_filter = ("wallet_type", "status", "is_listed", "city")
    search_fields = ("user__email", "user__username", "display_name", "city", "area")
    fieldsets = (
        (None, {"fields": ("user", "wallet_type", "status")}),
        ("Limits and access", {"fields": ("daily_limit", "monthly_limit", "ip_allowlist")}),
        # What a client sees on the TopUp screen. `is_listed` is the switch, and it is
        # off until the agent has agreed to be published: `public_phone` is the shop
        # line they gave us for that, never the number they registered with.
        ("Public listing (TopUp screen)", {
            "fields": ("is_listed", "display_name", "city", "area", "opening_hours",
                       "public_phone")}),
    )


@admin.register(AgentAPIKey)
class AgentAPIKeyAdmin(admin.ModelAdmin):
    list_display = ("agent", "key_id", "is_active", "created_at", "last_rotated_at")
    list_filter = ("is_active",)
    search_fields = ("key_id", "agent__user__email")
    #: `hashed_key` and `secret_ciphertext` were editable inputs, and the ciphertext was
    #: rendered into the page. Overwriting either bricks a live agent's credential with
    #: no log entry. Only the kill-switch stays writable, which is the one thing an
    #: operator legitimately needs here.
    readonly_fields = ("agent", "key_id", "hashed_key", "created_at", "last_rotated_at")
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


