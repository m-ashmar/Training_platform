"""The operator's phone: `/api/v1/ops/` (see `ops_api.py`)."""
from django.urls import path

from .ops_api import (ConsoleLoginApproveView, ConsoleLoginCancelView,
                      ConsoleLoginDetailView, ConsoleLoginView, QueueCountsView)

app_name = 'ops'

urlpatterns = [
    path('queue-counts/', QueueCountsView.as_view(), name='queue_counts'),
    path('console-login/', ConsoleLoginView.as_view(), name='console_login'),
    path('console-login/<uuid:ticket_id>/', ConsoleLoginDetailView.as_view(),
         name='console_login_detail'),
    path('console-login/<uuid:ticket_id>/approve/', ConsoleLoginApproveView.as_view(),
         name='console_login_approve'),
    path('console-login/<uuid:ticket_id>/cancel/', ConsoleLoginCancelView.as_view(),
         name='console_login_cancel'),
]
