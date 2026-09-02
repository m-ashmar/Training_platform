from django.urls import path

from .views import PersonalDataEraseView, PersonalDataExportView

app_name = "privacy"

urlpatterns = [
    path("export/", PersonalDataExportView.as_view(), name="export"),
    path("erase/", PersonalDataEraseView.as_view(), name="erase"),
]
