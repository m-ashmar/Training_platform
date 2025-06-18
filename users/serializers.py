# users/serializers.py

from dj_rest_auth.registration.serializers import RegisterSerializer
from dj_rest_auth.serializers import LoginSerializer
from .models import CustomUser
from rest_framework import serializers
import logging
import re  # Add this import
from django.db import IntegrityError


logger = logging.getLogger(__name__)

class CustomRegisterSerializer(RegisterSerializer):
    phone_number = serializers.CharField(max_length=15,  required=True)
    def validate_phone_number(self, value):
        # Regex for phone number validation (international format)
        phone_regex = re.compile(r'^\+?1?\d{9,15}$')
        if not phone_regex.match(value):
            raise serializers.ValidationError(
                "Phone number must be entered in the format: '+999999999'. Up to 15 digits allowed."
            )
        return value
    
    
    
    def validate_email(self, value):
        if CustomUser.objects.filter(email=value).exists():
            raise serializers.ValidationError("A user with this email already exists.")
        return value

    
    
    class Meta:
        model = CustomUser
        fields = ['username', 'email', 'phone_number', 'password1', 'password2']

        
        
    def save(self, request):
        logger.info("CustomRegisterSerializer.save() called")
        try:
            user = super().save(request)
            user.phone_number = self.validated_data.get('phone_number')
            user.save()
            return user
        except IntegrityError as e:
            if "UNIQUE constraint failed: users_customuser.email" in str(e):
                raise serializers.ValidationError({"email": "A user with this email already exists."})
            if "UNIQUE constraint failed: users_customuser.phone_number" in str(e):
                raise serializers.ValidationError({"phone_number": "A user with this phone number already exists."})
            raise e  # Re-raise other exceptions if not related to unique constraintsr
    
    
    
    
class CustomLoginSerializer(LoginSerializer):
    username = None  # Remove the username field

    email = serializers.EmailField(required=True)  # Ensure email is required

    def validate(self, attrs):
        logger.info("CustomLoginSerializer.validate() called")
        email = attrs.get('email')
        password = attrs.get('password')

        if email and password:
            # Authenticate using email and password
            self.user = self.authenticate(email=email, password=password)
            if not self.user:
                raise serializers.ValidationError(
                    {"non_field_errors": ["Unable to log in with provided credentials."]}
                )
        else:
            raise serializers.ValidationError(
                {"non_field_errors": ["Must include 'email' and 'password'."]}
            )

        attrs['user'] = self.user
        return attrs
    
class UserDetailsSerializer(serializers.ModelSerializer):
    class Meta:
        model = CustomUser
        fields = ['height', 'weight', 'age', 'gender', 'specific_injury']    

class CustomUserSerializer(serializers.ModelSerializer):
    class Meta:
        model = CustomUser
        fields = [
            'id', 'username', 'email', 'first_name', 'last_name',
            'phone_number', 'date_joined', 'is_active', 'is_staff', 'is_superuser'
        ]
        read_only_fields = [
            'id', 'date_joined', 'is_active', 'is_staff', 'is_superuser'
        ]    