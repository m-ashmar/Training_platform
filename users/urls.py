from django.urls import path
from .views import (
    CustomLoginView, CustomRegisterView, UpdateUserDetailsView, 
    UserDetailsView, JWTAuthLogoutView, TrainerProfileView, TrainerClientsView,
    AssignClientView, UnassignClientView, ClientProfileView, AvailableTrainersView,
    PublicTrainersListView, PublicTrainerClientStatsView, ClientProfileViewSet, DeviceTokenRegisterView, CustomTokenObtainPairView,
    ClientRequestTrainerView, TrainerPendingRequestsView, TrainerRespondToRequestView,
    ClientRequestStatusView, ProfilePictureUploadView, OTPVerificationView, ResendOTPView,
    ClientUnassignTrainerView, ClientCancelTrainerRequestView,
    PasswordResetRequestView, PasswordResetVerifyView, PasswordResetConfirmView,
)
from .device_token_views import FCMTokenView
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
    path('verify-otp/', OTPVerificationView.as_view(), name='verify_otp'),
    path('resend-otp/', ResendOTPView.as_view(), name='resend_otp'),
    
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
    path('client/unassign-trainer/', ClientUnassignTrainerView.as_view(), name='client_unassign_trainer'),
    path('client/cancel-trainer-request/', ClientCancelTrainerRequestView.as_view(), name='client_cancel_trainer_request'),
    
    # Public endpoints (no authentication required)
    path('trainers/public/', PublicTrainersListView.as_view(), name='public_trainers_list'),
    path('trainers/stats/', PublicTrainerClientStatsView.as_view(), name='public_trainer_client_stats'),
    
    # Client-Trainer Request System endpoints
    path('client/request-trainer/', ClientRequestTrainerView.as_view(), name='client_request_trainer'),
    path('client/request-status/', ClientRequestStatusView.as_view(), name='client_request_status'),
    path('trainer/pending-requests/', TrainerPendingRequestsView.as_view(), name='trainer_pending_requests'),
    path('trainer/respond-to-request/', TrainerRespondToRequestView.as_view(), name='trainer_respond_to_request'),
    
    # Password reset endpoints (OTP-based REST API)
    path('forgot-password/', PasswordResetRequestView.as_view(), name='password_reset_request'),
    path('forgot-password/verify/', PasswordResetVerifyView.as_view(), name='password_reset_verify'),
    path('forgot-password/confirm/', PasswordResetConfirmView.as_view(), name='password_reset_confirm'),
    
    # Device Token Management (FCM)
    path('device-token/', FCMTokenView.as_view(), name='fcm_token_manage'),
]

router = DefaultRouter()
router.register(r'trainer/client-profile', ClientProfileViewSet, basename='trainer-client-profile')
urlpatterns += router.urls