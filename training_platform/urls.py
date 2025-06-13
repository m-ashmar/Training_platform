from django.contrib import admin
from django.urls import path, include
from users.views import CustomLoginView, CustomRegisterView, UpdateUserDetailsView

urlpatterns = [
    # Admin site
    path("admin/", admin.site.urls),

    # Authentication
    path('api/auth/login/', CustomLoginView.as_view(), name='custom_login'),
    path('api/auth/registration/', CustomRegisterView.as_view(), name='custom_registration'),
    path('api/auth/update-details/', UpdateUserDetailsView.as_view(), name='update_user_details'),
    path('api/auth/', include('dj_rest_auth.urls')),  # Default dj-rest-auth endpoints

    # Users app URLs
    path('api/auth/', include('users.urls')),

    # Routine app URLs
    path('api/', include('routine.urls')),
]