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


@admin.register(Wallet)
class WalletAdmin(admin.ModelAdmin):
    list_display = ("owner", "owner_type", "balance", "currency", "created_at")
    search_fields = ("owner__email", "owner__username")
    list_filter = ("owner_type", "currency")


@admin.register(Transaction)
class TransactionAdmin(admin.ModelAdmin):
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
    list_display = ("user", "wallet_type", "daily_limit", "monthly_limit", "status")
    list_filter = ("wallet_type", "status")
    search_fields = ("user__email", "user__username")


@admin.register(AgentAPIKey)
class AgentAPIKeyAdmin(admin.ModelAdmin):
    list_display = ("agent", "key_id", "is_active", "created_at", "last_rotated_at")
    list_filter = ("is_active",)
    search_fields = ("key_id", "agent__user__email")

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
class WalletAuditLogAdmin(admin.ModelAdmin):
    list_display = ("event_type", "actor", "ip_address", "request_id", "created_at")
    list_filter = ("event_type", "created_at")
    search_fields = ("request_id", "actor__email", "path")


@admin.register(IdempotencyKey)
class IdempotencyKeyAdmin(admin.ModelAdmin):
    list_display = ("key", "created_by", "processed", "created_at")
    list_filter = ("processed",)
    search_fields = ("key", "created_by__email")


