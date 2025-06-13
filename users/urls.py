from django.urls import path
from .views import CustomLoginView, CustomRegisterView, UpdateUserDetailsView, UserDetailsView

urlpatterns = [
    path('login/', CustomLoginView.as_view(), name='custom_login'),
    path('register/', CustomRegisterView.as_view(), name='custom_register'),
    path('user/update/', UpdateUserDetailsView.as_view(), name='update_user_details'),  # Renamed for clarity
    path('user/details/', UserDetailsView.as_view(), name='user_details'),  # Fetch user data
]