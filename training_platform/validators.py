"""
Custom validators for Training Platform

This module provides comprehensive input validation for all API endpoints,
ensuring data integrity and security across the platform.
"""

import re
import magic
from django.core.exceptions import ValidationError
from django.core.validators import RegexValidator
from django.utils.translation import gettext_lazy as _
from rest_framework import serializers


class BaseValidator:
    """Base validator class with common functionality"""
    
    def __init__(self, message=None):
        self.message = message or self.default_message
    
    def __call__(self, value):
        self.validate(value)
    
    def validate(self, value):
        raise NotImplementedError


class PasswordStrengthValidator(BaseValidator):
    """
    Validates password strength with specific requirements
    """
    default_message = _(
        "Password must contain at least 8 characters, including uppercase, "
        "lowercase, number, and special character"
    )
    
    def validate(self, password):
        if len(password) < 8:
            raise ValidationError(
                _("Password must be at least 8 characters long")
            )
        
        if not re.search(r'[A-Z]', password):
            raise ValidationError(
                _("Password must contain at least one uppercase letter")
            )
        
        if not re.search(r'[a-z]', password):
            raise ValidationError(
                _("Password must contain at least one lowercase letter")
            )
        
        if not re.search(r'\d', password):
            raise ValidationError(
                _("Password must contain at least one number")
            )
        
        if not re.search(r'[!@#$%^&*(),.?":{}|<>]', password):
            raise ValidationError(
                _("Password must contain at least one special character")
            )


class FileTypeValidator(BaseValidator):
    """
    Validates file types using python-magic for accurate detection
    """
    default_message = _("Invalid file type")
    
    def __init__(self, allowed_types, max_size=None, message=None):
        self.allowed_types = allowed_types
        self.max_size = max_size or 5 * 1024 * 1024  # 5MB default
        super().__init__(message)
    
    def validate(self, file):
        if not file:
            return
        
        # Check file size
        if file.size > self.max_size:
            raise ValidationError(_("File size exceeds %(size)s bytes") % {"size": self.max_size}, code="file_too_large")
        
        # Check file type using magic
        file_type = magic.from_buffer(file.read(1024), mime=True)
        file.seek(0)  # Reset file pointer
        
        if file_type not in self.allowed_types:
            allowed_str = ', '.join(self.allowed_types)
            raise ValidationError(
                _("File type %(type)s not allowed. Allowed types: %(allowed)s") % {"type": file_type, "allowed": allowed_str},
                code="invalid_file_type"
            )


class ImageFileValidator(FileTypeValidator):
    """
    Specific validator for image files
    """
    default_message = _("Invalid image file")
    
    def __init__(self, max_size=None, message=None):
        allowed_types = [
            'image/jpeg',
            'image/png', 
            'image/gif',
            'image/webp'
        ]
        super().__init__(allowed_types, max_size, message)


class PhoneNumberValidator(BaseValidator):
    """
    International phone number validator
    """
    default_message = _("Invalid phone number format")
    
    def validate(self, phone):
        # Remove all non-digit characters
        digits_only = re.sub(r'\D', '', phone)
        
        # Check length (7-15 digits as per E.164)
        if len(digits_only) < 7 or len(digits_only) > 15:
            raise ValidationError(self.message)
        
        # Check for valid international format
        if not re.match(r'^\+?[1-9]\d{6,14}$', phone.replace(' ', '').replace('-', '')):
            raise ValidationError(self.message)


class NumericRangeValidator(BaseValidator):
    """
    Validates numeric values within a specific range
    """
    default_message = _("Value out of range")
    
    def __init__(self, min_value=None, max_value=None, message=None):
        self.min_value = min_value
        self.max_value = max_value
        self.message = message or f"Value must be between {min_value} and {max_value}"
    
    def validate(self, value):
        if self.min_value is not None and value < self.min_value:
            raise ValidationError(_("Value must be at least %(min)s") % {"min": self.min_value}, code="value_too_low")
        
        if self.max_value is not None and value > self.max_value:
            raise ValidationError(_("Value must be at most %(max)s") % {"max": self.max_value}, code="value_too_high")


class NoScriptValidator(BaseValidator):
    """
    Prevents script injection in text fields
    """
    default_message = _("Script tags and JavaScript are not allowed")
    
    def validate(self, value):
        if isinstance(value, str):
            # Check for script tags and javascript
            script_patterns = [
                r'<script.*?>.*?</script>',
                r'javascript:',
                r'on\w+\s*=',
                r'<iframe.*?>',
                r'<object.*?>',
                r'<embed.*?>'
            ]
            
            for pattern in script_patterns:
                if re.search(pattern, value, re.IGNORECASE):
                    raise ValidationError(self.message)


class SQLInjectionValidator(BaseValidator):
    """
    Basic SQL injection prevention
    """
    default_message = _("Potential SQL injection detected")
    
    def validate(self, value):
        if isinstance(value, str):
            sql_patterns = [
                r'\bunion\b.*\bselect\b',
                r'\bdrop\b.*\btable\b',
                r'\bdelete\b.*\bfrom\b',
                r'\binsert\b.*\binto\b',
                r'\bupdate\b.*\bset\b',
                r'[\'"];.*--',
                r'\bor\b\s+\d+\s*=\s*\d+',
                r'\band\b\s+\d+\s*=\s*\d+'
            ]
            
            for pattern in sql_patterns:
                if re.search(pattern, value, re.IGNORECASE):
                    raise ValidationError(self.message)


# Composite validators
def validate_user_input(value):
    """
    Combines multiple validators for user input
    """
    NoScriptValidator()(value)
    SQLInjectionValidator()(value)


def validate_nutrition_value(value):
    """
    Validates nutritional values (calories, protein, etc.)
    """
    NumericRangeValidator(0, 10000)(value)


def validate_weight(value):
    """
    Validates weight values
    """
    NumericRangeValidator(20, 500)(value)  # 20kg to 500kg


def validate_height(value):
    """
    Validates height values in centimeters
    """
    NumericRangeValidator(50, 300)(value)  # 50cm to 300cm


def validate_age(value):
    """
    Validates age values
    """
    NumericRangeValidator(13, 120)(value)  # 13 to 120 years


# Custom serializer fields with validation
class ValidatedCharField(serializers.CharField):
    """
    CharField with built-in XSS and SQL injection protection
    """
    
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.validators.append(validate_user_input)


class ValidatedTextField(serializers.CharField):
    """
    TextField with built-in XSS and SQL injection protection
    """
    
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.validators.append(validate_user_input)


class ValidatedEmailField(serializers.EmailField):
    """
    EmailField with enhanced validation
    """
    
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
    
    def validate(self, value):
        super().validate(value)
        
        # Additional email validation
        if value and len(value) > 254:
            raise serializers.ValidationError(_("Email address too long"), code="email_too_long")
        
        # Check for suspicious patterns
        suspicious_patterns = [
            r'[<>]',  # Angle brackets
            r'javascript:',  # JavaScript protocol
            r'data:',  # Data protocol
        ]
        
        for pattern in suspicious_patterns:
            if re.search(pattern, value, re.IGNORECASE):
                raise serializers.ValidationError(_("Invalid email format"), code="invalid_email")


class SecureImageField(serializers.ImageField):
    """
    ImageField with security validation
    """
    
    def __init__(self, **kwargs):
        self.max_size = kwargs.pop('max_size', 5 * 1024 * 1024)  # 5MB default
        super().__init__(**kwargs)
    
    def validate(self, value):
        super().validate(value)
        
        if value:
            # Validate file type and security
            ImageFileValidator(max_size=self.max_size)(value)


# Common regex validators
alphanumeric_validator = RegexValidator(
    regex=r'^[a-zA-Z0-9_]+$',
    message='Only alphanumeric characters and underscores are allowed.'
)

username_validator = RegexValidator(
    regex=r'^[a-zA-Z0-9_.-]+$',
    message=_('Username can only contain letters, numbers, dots, hyphens, and underscores.')
)

# Pre-configured validators for common use cases
COMMON_VALIDATORS = {
    'password': PasswordStrengthValidator(),
    'phone': PhoneNumberValidator(),
    'image': ImageFileValidator(),
    'user_input': validate_user_input,
    'nutrition': validate_nutrition_value,
    'weight': validate_weight,
    'height': validate_height,
    'age': validate_age,
    'alphanumeric': alphanumeric_validator,
    'username': username_validator,
} 