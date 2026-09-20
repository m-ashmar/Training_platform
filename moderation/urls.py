from django.urls import path

from .views import (
    AIConsentView,
    BlockDeleteView,
    BlockListCreateView,
    MyReportsView,
    ReportCreateView,
)

app_name = "moderation"

urlpatterns = [
    # `reports/mine/` before `reports/`: both are exact paths so the order does not
    # change resolution, but the queue route reads first the way the app calls it.
    path("reports/", ReportCreateView.as_view(), name="report-create"),
    path("reports/mine/", MyReportsView.as_view(), name="report-mine"),
    path("blocks/", BlockListCreateView.as_view(), name="block-list"),
    path("blocks/<int:user_id>/", BlockDeleteView.as_view(), name="block-delete"),
    path("ai-consent/", AIConsentView.as_view(), name="ai-consent"),
]
