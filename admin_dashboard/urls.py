from django.urls import path

from .admin import admin_site
from .qr_login import qr_login_page, qr_login_status

urlpatterns = [
    # Signing in on a computer with a link made on the operator's phone (B-120). Before
    # the site's own urls: the admin's catch-all would otherwise send them to its login.
    path('qr-login/<str:secret>/', qr_login_page, name='ops_qr_login'),
    path('qr-login/<str:secret>/status/', qr_login_status, name='ops_qr_login_status'),
    path('', admin_site.urls),
]
