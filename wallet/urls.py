from django.urls import path
from .views import (
    WalletBalanceView,
    WalletTransactionsView,
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
    path("transactions/", WalletTransactionsView.as_view(), name="transactions"),
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


