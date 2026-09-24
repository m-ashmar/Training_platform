from django.urls import path
from .views import (
    AgentDirectoryView,
    WalletBalanceView,
    WalletCurrencyView,
    WalletTransactionDetailView,
    WalletTransactionsView,
    WalletTransactionsSummaryView,
    AgentTopUpView,
    ClientTransferToTrainerView,
    AdminReversalView,
    AdminAuditExportView,
    AdminSuspiciousActivityView,
    AgentApiKeyCreateView,
    AgentApiKeyStatusView,
    AgentApiKeyEnsureView,
    AgentTopUpProxyView,
)


app_name = "wallet"

urlpatterns = [
    path("balance/", WalletBalanceView.as_view(), name="balance"),
    # The ONE currency this person trades in: read it, and set it while the
    # wallet is still untouched (the owner, 2026-09-21).
    path("currency/", WalletCurrencyView.as_view(), name="currency"),
    path("transactions/", WalletTransactionsView.as_view(), name="transactions"),
    # The same ledger summed by month and by kind of money (B-101). Declared BEFORE the
    # detail route: "summary" would otherwise be read as a reference_id (never a real
    # one — references are UUIDs).
    path("transactions/summary/", WalletTransactionsSummaryView.as_view(),
         name="transactions-summary"),
    # One row of the caller's own ledger, by its public reference. Same shape as a
    # row of the list above.
    path("transactions/<str:reference_id>/", WalletTransactionDetailView.as_view(),
         name="transaction-detail"),
    # The agents a client can hand cash to (the TopUp screen).
    path("agents/", AgentDirectoryView.as_view(), name="agents"),
    path("agent/topup/", AgentTopUpView.as_view(), name="agent-topup"),
    # Both spellings are routed. This was the only wallet path declared without a
    # trailing slash, so a client following the convention of every neighbouring
    # endpoint got a hard 404: APPEND_SLASH cannot redirect a POST without
    # discarding its body, so there was no recovery.
    path("agent/topup/proxy", AgentTopUpProxyView.as_view(), name="agent-topup-proxy"),
    path("agent/topup/proxy/", AgentTopUpProxyView.as_view(), name="agent-topup-proxy-slash"),
    path("agent/apikey/create/", AgentApiKeyCreateView.as_view(), name="agent-apikey-create"),
    path("agent/apikey/status/", AgentApiKeyStatusView.as_view(), name="agent-apikey-status"),
    path("agent/apikey/ensure/", AgentApiKeyEnsureView.as_view(), name="agent-apikey-ensure"),
    path("client/transfer/", ClientTransferToTrainerView.as_view(), name="client-transfer"),
    path("admin/reversal/", AdminReversalView.as_view(), name="admin-reversal"),
    path("admin/audit/export/", AdminAuditExportView.as_view(), name="admin-audit-export"),
    path("admin/alerts/suspicious/", AdminSuspiciousActivityView.as_view(), name="admin-suspicious-activity"),
]


