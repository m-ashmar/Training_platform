from django.urls import path
from .views import (
    CustomLoginView, CustomRegisterView, UpdateUserDetailsView, 
    UserDetailsView, JWTAuthLogoutView, TrainerProfileView, TrainerClientsView,
    AssignClientView, UnassignClientView, ClientProfileView, AvailableTrainersView,
    ClientProfileViewSet, DeviceTokenRegisterView, CustomTokenObtainPairView,
    ClientRequestTrainerView, TrainerPendingRequestsView, TrainerRespondToRequestView,
    ClientRequestStatusView, ProfilePictureUploadView
)
from django.contrib.auth import views as auth_views
from rest_framework_simplejwt.views import TokenRefreshView
from rest_framework.routers import DefaultRouter

app_name = 'users'

urlpatterns = [
    # JWT Authentication endpoints
    path('token/', CustomTokenObtainPairView.as_view(), name='token_obtain_pair'),
    path('token/refresh/', TokenRefreshView.as_view(), name='token_refresh'),
    path('token/logout/', JWTAuthLogoutView.as_view(), name='jwt_logout'),
    
    # Custom authentication endpoints
    path('login/', CustomLoginView.as_view(), name='custom_login'),
    path('register/', CustomRegisterView.as_view(), name='custom_register'),
    
    # User management endpoints
    path('user/update/', UpdateUserDetailsView.as_view(), name='update_user_details'),
    path('user/details/', UserDetailsView.as_view(), name='user_details'),
    path('user/profile-picture/', ProfilePictureUploadView.as_view(), name='profile_picture_upload'),
    
    # Trainer-specific endpoints
    path('trainer/profile/', TrainerProfileView.as_view(), name='trainer_profile'),
    path('trainer/clients/', TrainerClientsView.as_view(), name='trainer_clients'),
    path('trainer/assign-client/', AssignClientView.as_view(), name='assign_client'),
    path('trainer/unassign-client/', UnassignClientView.as_view(), name='unassign_client'),
    
    # Client-specific endpoints
    path('client/profile/', ClientProfileView.as_view(), name='client_profile'),
    path('client/available-trainers/', AvailableTrainersView.as_view(), name='available_trainers'),
    
    # Client-Trainer Request System endpoints
    path('client/request-trainer/', ClientRequestTrainerView.as_view(), name='client_request_trainer'),
    path('client/request-status/', ClientRequestStatusView.as_view(), name='client_request_status'),
    path('trainer/pending-requests/', TrainerPendingRequestsView.as_view(), name='trainer_pending_requests'),
    path('trainer/respond-to-request/', TrainerRespondToRequestView.as_view(), name='trainer_respond_to_request'),
    
    # Password reset endpoints (email-based)
    path('password-reset/', auth_views.PasswordResetView.as_view(), name='password_reset'),
    path('password-reset/done/', auth_views.PasswordResetDoneView.as_view(), name='password_reset_done'),
    path('reset/<uidb64>/<token>/', auth_views.PasswordResetConfirmView.as_view(), name='password_reset_confirm'),
    path('reset/done/', auth_views.PasswordResetCompleteView.as_view(), name='password_reset_complete'),
    
    # TODO: Add phone-based password reset endpoint for SMS in the future
    # TODO: Ensure email/phone verification is implemented before enabling in production
    path('device-token/', DeviceTokenRegisterView.as_view(), name='devicetokenregisterview'),
]

router = DefaultRouter()
router.register(r'trainer/client-profile', ClientProfileViewSet, basename='trainer-client-profile')
urlpatterns += router.urls