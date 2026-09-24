"""Mounted at `/api/v1/trainer/` (training_platform/urls.py), beside the trainer's media
kit, verification and home. See payouts/views.py."""
from django.urls import path

from .views import (PayoutMethodDefaultView, PayoutMethodDetailView, PayoutMethodListView,
                    WithdrawalCancelView, WithdrawalDetailView, WithdrawalListView,
                    WithdrawalSetupView)

app_name = "payouts"

urlpatterns = [
    path("withdrawals/", WithdrawalListView.as_view(), name="withdrawals"),
    path("withdrawals/setup/", WithdrawalSetupView.as_view(), name="withdrawal-setup"),
    path("withdrawals/<int:withdrawal_id>/", WithdrawalDetailView.as_view(),
         name="withdrawal-detail"),
    path("withdrawals/<int:withdrawal_id>/cancel/", WithdrawalCancelView.as_view(),
         name="withdrawal-cancel"),
    path("payout-methods/", PayoutMethodListView.as_view(), name="payout-methods"),
    path("payout-methods/<int:method_id>/", PayoutMethodDetailView.as_view(),
         name="payout-method-detail"),
    path("payout-methods/<int:method_id>/default/", PayoutMethodDefaultView.as_view(),
         name="payout-method-default"),
]
