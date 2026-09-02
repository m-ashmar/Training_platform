# users/serializers.py

from dj_rest_auth.registration.serializers import RegisterSerializer
from dj_rest_auth.serializers import LoginSerializer
from .models import CustomUser, DeviceToken
from rest_framework import serializers
import logging
import re  # Add this import
from django.db import IntegrityError
from django.conf import settings
from django.core.files.images import get_image_dimensions
from django.utils.translation import gettext_lazy as _


logger = logging.getLogger(__name__)

class CustomRegisterSerializer(RegisterSerializer):
    phone_number = serializers.CharField(max_length=15,  required=True)
    user_type = serializers.ChoiceField(choices=[('client', 'Client'), ('trainer', 'Trainer'), ('agent', 'Agent'), ('admin', 'Administrator')], required=False, default='client')

    def validate_phone_number(self, value):
        # Regex for phone number validation (international format)
        phone_regex = re.compile(r'^\+?1?\d{9,15}$')
        if not phone_regex.match(value):
            raise serializers.ValidationError(
                _("Phone number must be entered in the format: '+999999999'. Up to 15 digits allowed.")
            )
        return value
    
    def validate_user_type(self, value):
        request = self.context.get('request')
        # Privileged roles cannot be self-provisioned. 'agent' has wallet +
        # API-key access; 'admin' is a superuser. Both require an authenticated
        # superuser to create.
        if value in ('admin', 'agent'):
            if not request or not request.user.is_authenticated or not request.user.is_superuser:
                raise serializers.ValidationError(_("This user type cannot be self-registered."))
        # Only client/trainer are valid for self-service registration
        if value not in ['client', 'trainer', 'agent', 'admin']:
            raise serializers.ValidationError(_("Invalid user type."))
        return value

    def validate_email(self, value):
        if CustomUser.objects.filter(email=value).exists():
            raise serializers.ValidationError(_("A user with this email already exists."))
        return value

    class Meta:
        model = CustomUser
        fields = ['username', 'email', 'phone_number', 'password1', 'password2', 'user_type']

    def save(self, request):
        logger.info("CustomRegisterSerializer.save() called")
        try:
            user = super().save(request)
            user.phone_number = self.validated_data.get('phone_number')
            user.user_type = self.validated_data.get('user_type', 'client')
            # Set user as inactive until email is verified
            user.is_active = False
            user.save()
            return user
        except IntegrityError as e:
            if "UNIQUE constraint failed: users_customuser.email" in str(e):
                raise serializers.ValidationError({"email": _("A user with this email already exists.")})
            if "UNIQUE constraint failed: users_customuser.phone_number" in str(e):
                raise serializers.ValidationError({"phone_number": _("A user with this phone number already exists.")})
            raise e  # Re-raise other exceptions if not related to unique constraintsr
    
    
    

class CustomLoginSerializer(LoginSerializer):
    username = None  # Remove the username field

    email = serializers.EmailField(required=True)  # Ensure email is required

    def validate(self, attrs):
        logger.info("CustomLoginSerializer.validate() called")
        email = attrs.get('email')
        password = attrs.get('password')

        if email and password:
            from .utils import check_login_lockout, record_login_failure, clear_login_attempts

            # Per-account brute-force lockout (10 failures → 15-min cooldown)
            is_locked, _remaining = check_login_lockout(email)
            if is_locked:
                raise serializers.ValidationError(
                    {"non_field_errors": [_("Too many failed login attempts. Please try again in 15 minutes.")]}
                )

            # Authenticate using email and password
            self.user = self.authenticate(email=email, password=password)
            if not self.user:
                record_login_failure(email)
                raise serializers.ValidationError(
                    {"non_field_errors": [_("Unable to log in with provided credentials.")]}
                )

            # Check if user account is active (email verified)
            if not self.user.is_active:
                raise serializers.ValidationError(
                    {
                        "non_field_errors": [_("Please verify your email address before logging in. Check your inbox for the OTP code.")],
                        "requires_verification": True,
                        "email": self.user.email
                    }
                )

            # Successful auth — reset the failure counter
            clear_login_attempts(email)
        else:
            raise serializers.ValidationError(
                {"non_field_errors": [_("Must include 'email' and 'password'.")]}
            )

        attrs['user'] = self.user
        return attrs
    
class UserDetailsSerializer(serializers.ModelSerializer):
    profile_picture = serializers.SerializerMethodField()
    onboarding_completed = serializers.BooleanField(source='is_onboarding_completed', read_only=True)
    
    class Meta:
        model = CustomUser
        fields = [
            'height', 'weight', 'age', 'gender', 'specific_injury',
            'activity_level', 'user_type', 'first_name', 'last_name', 'profile_picture',
            'trainer_bio', 'trainer_specializations', 'trainer_certifications',
            'trainer_experience_years', 'trainer_hourly_rate', 'trainer_is_verified',
            'trainer_is_available', 'assigned_trainer', 'client_goals', 'client_preferences',
            'is_active', 'onboarding_completed'
        ]
        # assigned_trainer is read-only here: trainer assignment must go through
        # the request/approval + wallet-escrow flow, never self-service update.
        read_only_fields = ['user_type', 'trainer_is_verified', 'is_active', 'onboarding_completed', 'assigned_trainer']

    def get_profile_picture(self, obj):
        request = self.context.get('request')
        if obj.profile_picture:
            url = obj.profile_picture.url
            if request is not None:
                return request.build_absolute_uri(url)
            return url
        return None

    def validate_profile_picture(self, value):
        # Content-based validation. The previous check trusted `content_type`, an
        # attacker-controlled header, so this second upload path (/api/auth/user/update/)
        # bypassed the hardening applied to the dedicated profile-picture endpoint.
        if not value:
            return value
        from django.core.exceptions import ValidationError as DjangoValidationError
        from training_platform.file_security import process_uploaded_image
        import os as _os
        try:
            safe_file, ext = process_uploaded_image(value, max_bytes=2 * 1024 * 1024)
        except DjangoValidationError as e:
            raise serializers.ValidationError(e.messages[0] if getattr(e, 'messages', None) else str(e))
        base = _os.path.splitext(getattr(value, 'name', 'profile'))[0][:60] or 'profile'
        safe_file.name = f"{base}.{ext}"
        return safe_file

    def validate(self, attrs):
        user = self.instance
        if user and user.user_type == 'trainer':
            # Trainers can't have assigned_trainer
            if 'assigned_trainer' in attrs:
                attrs.pop('assigned_trainer')
        elif user and user.user_type == 'client':
            # Clients can't have trainer-specific fields
            trainer_fields = ['trainer_bio', 'trainer_specializations', 'trainer_certifications',
                            'trainer_experience_years', 'trainer_hourly_rate', 'trainer_is_available']
            for field in trainer_fields:
                if field in attrs:
                    attrs.pop(field)
        return attrs

class TrainerProfileSerializer(serializers.ModelSerializer):
    """Serializer for trainer-specific profile data"""
    client_count = serializers.SerializerMethodField()
    profile_picture = serializers.SerializerMethodField()
    
    onboarding_completed = serializers.BooleanField(source='is_onboarding_completed', read_only=True)

    class Meta:
        model = CustomUser
        fields = [
            'id', 'username', 'email', 'first_name', 'last_name', 'profile_picture',
            'trainer_bio', 'trainer_specializations', 'trainer_certifications',
            'trainer_experience_years', 'trainer_hourly_rate', 'trainer_is_verified',
            'trainer_is_available', 'client_count', 'is_active', 'onboarding_completed'
        ]
        read_only_fields = ['id', 'username', 'email', 'trainer_is_verified', 'is_active', 'onboarding_completed']

    def get_client_count(self, obj):
        return obj.get_client_count()

    def get_profile_picture(self, obj):
        request = self.context.get('request')
        if obj.profile_picture:
            url = obj.profile_picture.url
            if request is not None:
                return request.build_absolute_uri(url)
            return url
        return None

    def validate_profile_picture(self, value):
        # Content-based validation. The previous check trusted `content_type`, an
        # attacker-controlled header, so this second upload path (/api/auth/user/update/)
        # bypassed the hardening applied to the dedicated profile-picture endpoint.
        if not value:
            return value
        from django.core.exceptions import ValidationError as DjangoValidationError
        from training_platform.file_security import process_uploaded_image
        import os as _os
        try:
            safe_file, ext = process_uploaded_image(value, max_bytes=2 * 1024 * 1024)
        except DjangoValidationError as e:
            raise serializers.ValidationError(e.messages[0] if getattr(e, 'messages', None) else str(e))
        base = _os.path.splitext(getattr(value, 'name', 'profile'))[0][:60] or 'profile'
        safe_file.name = f"{base}.{ext}"
        return safe_file

    def validate(self, attrs):
        # Ensure only trainers can use this serializer
        if self.instance and not self.instance.is_trainer:
            raise serializers.ValidationError(_("This serializer is only for trainers"))
        return attrs

class ClientProfileSerializer(serializers.ModelSerializer):
    """Serializer for client-specific profile data"""
    assigned_trainer_name = serializers.SerializerMethodField()
    profile_picture = serializers.SerializerMethodField()
    
    class Meta:
        model = CustomUser
        fields = [
            'id', 'username', 'email', 'first_name', 'last_name', 'profile_picture',
            'height', 'weight', 'age', 'gender', 'specific_injury',
            'activity_level', 'assigned_trainer', 'assigned_trainer_name',
            'client_goals', 'client_preferences'
        ]
        read_only_fields = ['id', 'username', 'email']

    def get_assigned_trainer_name(self, obj):
        if obj.assigned_trainer:
            return obj.assigned_trainer.full_name
        return None

    def get_profile_picture(self, obj):
        request = self.context.get('request')
        if obj.profile_picture:
            url = obj.profile_picture.url
            if request is not None:
                return request.build_absolute_uri(url)
            return url
        return None

    def validate_profile_picture(self, value):
        # Content-based validation. The previous check trusted `content_type`, an
        # attacker-controlled header, so this second upload path (/api/auth/user/update/)
        # bypassed the hardening applied to the dedicated profile-picture endpoint.
        if not value:
            return value
        from django.core.exceptions import ValidationError as DjangoValidationError
        from training_platform.file_security import process_uploaded_image
        import os as _os
        try:
            safe_file, ext = process_uploaded_image(value, max_bytes=2 * 1024 * 1024)
        except DjangoValidationError as e:
            raise serializers.ValidationError(e.messages[0] if getattr(e, 'messages', None) else str(e))
        base = _os.path.splitext(getattr(value, 'name', 'profile'))[0][:60] or 'profile'
        safe_file.name = f"{base}.{ext}"
        return safe_file

    def validate(self, attrs):
        # Ensure only clients can use this serializer
        if self.instance and not self.instance.is_client:
            raise serializers.ValidationError(_("This serializer is only for clients"))
        return attrs

class CustomUserSerializer(serializers.ModelSerializer):
    profile_picture = serializers.SerializerMethodField()
    class Meta:
        model = CustomUser
        fields = [
            'id', 'username', 'email', 'first_name', 'last_name', 'profile_picture',
            'phone_number', 'date_joined', 'is_active', 'is_staff', 'is_superuser', 'user_type'
        ]
        read_only_fields = [
            'id', 'date_joined', 'is_active', 'is_staff', 'is_superuser', 'user_type'
        ]    

    def get_profile_picture(self, obj):
        request = self.context.get('request')
        if obj.profile_picture:
            url = obj.profile_picture.url
            if request is not None:
                return request.build_absolute_uri(url)
            return url
        return None

    def validate_profile_picture(self, value):
        # Content-based validation. The previous check trusted `content_type`, an
        # attacker-controlled header, so this second upload path (/api/auth/user/update/)
        # bypassed the hardening applied to the dedicated profile-picture endpoint.
        if not value:
            return value
        from django.core.exceptions import ValidationError as DjangoValidationError
        from training_platform.file_security import process_uploaded_image
        import os as _os
        try:
            safe_file, ext = process_uploaded_image(value, max_bytes=2 * 1024 * 1024)
        except DjangoValidationError as e:
            raise serializers.ValidationError(e.messages[0] if getattr(e, 'messages', None) else str(e))
        base = _os.path.splitext(getattr(value, 'name', 'profile'))[0][:60] or 'profile'
        safe_file.name = f"{base}.{ext}"
        return safe_file

class DeviceTokenSerializer(serializers.ModelSerializer):
    class Meta:
        model = DeviceToken
        fields = ['id', 'user', 'token', 'created_at', 'updated_at']
        read_only_fields = ['id', 'user', 'created_at', 'updated_at']    


# ============================================================================
# PASSWORD RESET SERIALIZERS
# ============================================================================

class PasswordResetRequestSerializer(serializers.Serializer):
    """Serializer for requesting a password reset OTP."""
    email = serializers.EmailField(required=True)


class PasswordResetVerifySerializer(serializers.Serializer):
    """Serializer for verifying the password reset OTP."""
    email = serializers.EmailField(required=True)
    otp_code = serializers.CharField(max_length=6, min_length=6, required=True)

    def validate_otp_code(self, value):
        if not value.isdigit():
            raise serializers.ValidationError(_("OTP code must be a 6-digit number."))
        return value


class PasswordResetConfirmSerializer(serializers.Serializer):
    """Serializer for setting a new password using the reset token."""
    # A url-safe random string now, not a UUID: the raw token is generated by
    # PasswordResetToken.issue() and only its keyed hash is stored.
    reset_token = serializers.CharField(required=True, max_length=128)
    new_password = serializers.CharField(min_length=8, required=True, write_only=True)

    def validate_new_password(self, value):
        from django.contrib.auth.password_validation import validate_password
        validate_password(value)
        return value