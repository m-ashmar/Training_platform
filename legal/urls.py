from django.urls import path

from .views import delete_account_page, privacy_page, terms_page

app_name = "legal"

urlpatterns = [
    path("privacy/", privacy_page, name="privacy"),
    path("terms/", terms_page, name="terms"),
    path("delete-account/", delete_account_page, name="delete-account"),
]
