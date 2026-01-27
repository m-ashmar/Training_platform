from django.contrib.auth.models import AbstractBaseUser, BaseUserManager, PermissionsMixin
from django.db import models
from django.db.models import Q
from django.utils import timezone
import logging
from datetime import date, timedelta
from django.conf import settings
import os
import uuid
from django.core.files.storage import default_storage

logger = logging.getLogger(__name__)

class CustomUserManager(BaseUserManager):
    """
    Custom user manager for creating users and superusers.
    Supports both regular users and trainer accounts.
    """
    def create_user(self, email, username, password=None, phone_number=None, **extra_fields):
        """
        Create and save a regular user or trainer.
        
        Args:
            email: User's email address
            username: Unique username
            password: User password
            phone_number: User's phone number
            **extra_fields: Additional fields including user_type
            
        Returns:
            CustomUser instance
        """
        if not email:
            raise ValueError("The Email field must be set")
        
        extra_fields.setdefault('is_active', True)
        email = self.normalize_email(email)
        user = self.model(email=email, username=username, phone_number=phone_number, **extra_fields)
        user.set_password(password)
        user.save(using=self._db)
        return user

    def create_superuser(self, email, username, password=None, phone_number=None, **extra_fields):
        """
        Create and save a superuser with all permissions.
        """
        extra_fields.setdefault('is_staff', True)
        extra_fields.setdefault('is_superuser', True)
        extra_fields.setdefault('user_type', 'admin')  # Superusers are admins

        return self.create_user(email, username, password, phone_number, **extra_fields)

    def create_trainer(self, email, username, password=None, phone_number=None, **extra_fields):
        """
        Create and save a trainer account with appropriate defaults.
        
        Args:
            email: Trainer's email address
            username: Unique username
            password: Trainer password
            phone_number: Trainer's phone number
            **extra_fields: Additional trainer-specific fields
            
        Returns:
            CustomUser instance with trainer type
        """
        extra_fields.setdefault('user_type', 'trainer')
        extra_fields.setdefault('is_active', True)
        extra_fields.setdefault('is_staff', False)  # Trainers are not staff by default
        
        return self.create_user(email, username, password, phone_number, **extra_fields)

def user_profile_picture_upload_path(instance, filename):
    ext = filename.split('.')[-1]
    # Use uuid4 for unique filenames
    filename = f"user_{instance.pk or 'new'}_{uuid.uuid4().hex[:8]}.{ext}"
    return os.path.join('profile_pictures', filename)

class CustomUser(AbstractBaseUser, PermissionsMixin):
    """
    Enhanced CustomUser model supporting multi-trainer functionality.
    
    This model supports three user types:
    - 'client': Regular users who can be assigned to trainers
    - 'trainer': Users who can manage clients and create content
    - 'admin': Superusers with full system access
    
    TODO: Consider implementing soft delete for user accounts
    TODO: Add audit logging for user type changes
    TODO: Implement user activity tracking
    TODO: Add support for user profile pictures
    TODO: Consider implementing user verification system
    """
    
    # Core authentication fields
    username = models.CharField(max_length=150, unique=True)
    email = models.EmailField(unique=True)
    date_joined = models.DateTimeField(default=timezone.now)
    phone_number = models.CharField(max_length=15, default="0000000000", null=False, blank=True)
    
    # Basic profile fields
    first_name = models.CharField(max_length=30, blank=True)
    last_name = models.CharField(max_length=30, blank=True)

    # Profile picture
    profile_picture = models.ImageField(
        upload_to=user_profile_picture_upload_path,
        blank=True,
        null=True,
        help_text="User's profile picture"
    )
    
    # User type and permissions
    USER_TYPE_CHOICES = [
        ('client', 'Client'),
        ('trainer', 'Trainer'),
        ('agent', 'Agent'),
        ('admin', 'Administrator'),
    ]
    user_type = models.CharField(
        max_length=10,
        choices=USER_TYPE_CHOICES,
        default='client',
        help_text="Determines user permissions and access levels"
    )
    
    # Django auth fields
    is_active = models.BooleanField(default=True)
    is_staff = models.BooleanField(default=False)
    
    # Physical attributes (migrated from old fields - preserved for backward compatibility)
    height = models.FloatField(null=True, blank=True, help_text="Height in cm")
    weight = models.FloatField(null=True, blank=True, help_text="Weight in kg")
    age = models.PositiveIntegerField(null=True, blank=True)
    gender = models.CharField(
        max_length=10, 
        choices=[('Male', 'Male'), ('Female', 'Female')], 
        null=True, 
        blank=True
    )
    specific_injury = models.TextField(null=True, blank=True, help_text="Any injuries or health conditions")
    
    # Activity and fitness data
    activity_level = models.CharField(
        max_length=20,
        choices=[
            ('Sedentary', 'Sedentary'),
            ('Light', 'Light Exercise'),
            ('Moderate', 'Moderate Exercise'),
            ('Active', 'Active'),
            ('VeryActive', 'Very Active')
        ],
        default='Sedentary'
    )
    
    # Trainer-specific fields
    trainer_bio = models.TextField(blank=True, null=True, help_text="Trainer's professional bio")
    trainer_specializations = models.JSONField(
        default=list, 
        blank=True,
        help_text="List of trainer specializations (e.g., ['Strength Training', 'Cardio'])"
    )
    trainer_certifications = models.JSONField(
        default=list, 
        blank=True,
        help_text="List of trainer certifications"
    )
    trainer_experience_years = models.PositiveIntegerField(
        null=True, 
        blank=True,
        help_text="Years of training experience"
    )
    trainer_hourly_rate = models.DecimalField(
        max_digits=8, 
        decimal_places=2, 
        null=True, 
        blank=True,
        help_text="Hourly rate in local currency"
    )
    trainer_is_verified = models.BooleanField(
        default=False,
        help_text="Whether trainer has been verified by admin"
    )
    trainer_is_available = models.BooleanField(
        default=True,
        help_text="Whether trainer is currently accepting new clients"
    )
    
    # Client-specific fields
    assigned_trainer = models.ForeignKey(
        'self',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='clients',
        help_text="Trainer assigned to this client",
        limit_choices_to={'user_type': 'trainer'}
    )
    client_goals = models.JSONField(
        default=list,
        blank=True,
        help_text="Client's fitness goals (e.g., ['Weight Loss', 'Muscle Gain'])"
    )
    client_preferences = models.JSONField(
        default=dict,
        blank=True,
        help_text="Client's training preferences"
    )
    
    # System fields
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    last_login = models.DateTimeField(null=True, blank=True)
    
    objects = CustomUserManager()

    USERNAME_FIELD = 'email'
    REQUIRED_FIELDS = ['username', 'phone_number']

    class Meta:
        verbose_name = "User"
        verbose_name_plural = "Users"
        indexes = [
            models.Index(fields=['user_type']),
            models.Index(fields=['email']),
            models.Index(fields=['username']),
            models.Index(fields=['assigned_trainer']),
        ]

    def __str__(self):
        return f"{self.username} ({self.get_user_type_display()})"

    @property
    def is_trainer(self):
        """Check if user is a trainer."""
        return self.user_type == 'trainer'

    @property
    def is_client(self):
        """Check if user is a client."""
        return self.user_type == 'client'

    @property
    def is_admin(self):
        """Check if user is an admin."""
        return self.user_type == 'admin'

    @property
    def is_agent(self):
        """Check if user is an agent."""
        return self.user_type == 'agent'

    @property
    def is_onboarding_completed(self):
        """
        Check if user has completed onboarding (filled basic profile).
        
        Criteria:
        - All users: First Name, Last Name
        - Clients: Height, Weight, Age, Gender
        """
        basic_info = bool(self.first_name and self.last_name)
        if not basic_info:
            return False
            
        if self.is_client:
            return all([self.height, self.weight, self.age, self.gender])
            
        return True

    @property
    def full_name(self):
        """Get user's full name."""
        if self.first_name and self.last_name:
            return f"{self.first_name} {self.last_name}"
        return self.username

    def get_client_count(self):
        """Get number of clients assigned to this trainer."""
        if not self.is_trainer:
            return 0
        return self.clients.count()

    def get_active_routines_count(self):
        """Get number of active routines for this user."""
        if self.is_client:
            return self.assigned_routines.filter(is_active=True).count()
        elif self.is_trainer:
            return self.created_routines.filter(is_active=True).count()
        return 0

    def calculate_bmi(self):
        """Calculate BMI if height and weight are available."""
        if self.height and self.weight:
            return round(self.weight / ((self.height/100) ** 2), 2)
        return None

    def calculate_bmr(self):
        """Calculate Basal Metabolic Rate using Mifflin-St Jeor Equation."""
        if self.gender and self.height and self.weight and self.age:
            if self.gender == 'Male':
                return 10 * self.weight + 6.25 * self.height - 5 * self.age + 5
            return 10 * self.weight + 6.25 * self.height - 5 * self.age - 161
        return None

    def calculate_daily_calories(self, goal='Maintain'):
        """
        Calculate daily calorie needs based on BMR and activity level.
        
        Args:
            goal: 'Lose', 'Gain', or 'Maintain'
            
        Returns:
            Daily calorie target or None if insufficient data
        """
        required_fields = [self.height, self.weight, self.age, self.gender]
        if any(field is None for field in required_fields):
            raise ValueError("Complete profile required: height, weight, age, gender")
        
        bmr = self.calculate_bmr()
        if bmr is None:
            return None
            
        activity_factors = {
            'Sedentary': 1.2,
            'Light': 1.375,
            'Moderate': 1.55,
            'Active': 1.725,
            'VeryActive': 1.9
        }
        maintenance = bmr * activity_factors.get(self.activity_level, 1.2)
        
        # Apply goal-based adjustments
        if goal == 'Lose':
            return maintenance - 500
        elif goal == 'Gain':
            return maintenance + 500
        return maintenance

    def can_access_user_data(self, target_user):
        """
        Check if this user can access another user's data.
        
        Args:
            target_user: CustomUser instance to check access for
            
        Returns:
            bool: True if access is allowed
        """
        # Admins can access all data
        if self.is_admin:
            return True
            
        # Trainers can access their clients' data
        if self.is_trainer and target_user.is_client:
            return target_user.assigned_trainer == self
            
        # Users can access their own data
        if target_user == self:
            return True
            
        return False

    def get_accessible_clients(self):
        """
        Get all clients this user can access.
        
        Returns:
            QuerySet of accessible clients
        """
        if self.is_admin:
            return CustomUser.objects.filter(user_type='client')
        elif self.is_trainer:
            return self.clients.all()
        return CustomUser.objects.none()

    def save(self, *args, **kwargs):
        """Override save to ensure data consistency."""
        # Ensure admins have staff permissions
        if self.is_admin:
            self.is_staff = True
            
        # Ensure trainers can't be assigned to other trainers
        if self.is_trainer and self.assigned_trainer:
            self.assigned_trainer = None
            
        # Clean up old profile picture if replaced
        if self.pk:
            try:
                old = CustomUser.objects.get(pk=self.pk)
                if old.profile_picture and old.profile_picture != self.profile_picture:
                    if default_storage.exists(old.profile_picture.name):
                        default_storage.delete(old.profile_picture.name)
            except CustomUser.DoesNotExist:
                pass
        super().save(*args, **kwargs)

    # NOTE: Diet plan generation is handled by the diet app's GPT-based system.
    # This model does not generate diet plans directly. Use the diet app API or Celery task for plan creation.

class TrainerClientRelation(models.Model):
    STATUS_CHOICES = [
        ('pending', 'Pending'),
        ('approved', 'Approved'),
        ('rejected', 'Rejected'),
    ]
    trainer = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='trainer_relations',
        limit_choices_to={'user_type': 'trainer'}
    )
    client = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='client_relations',
        limit_choices_to={'user_type': 'client'}
    )
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default='pending')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ('trainer', 'client')
        verbose_name = 'Trainer-Client Relation'
        verbose_name_plural = 'Trainer-Client Relations'
        constraints = [
            # A client can have at most one approved trainer
            models.UniqueConstraint(
                fields=['client'],
                condition=Q(status='approved'),
                name='unique_approved_trainer_per_client'
            ),
            # A client can have at most one pending request at a time
            models.UniqueConstraint(
                fields=['client'],
                condition=Q(status='pending'),
                name='unique_pending_trainer_request_per_client'
            ),
        ]

    def __str__(self):
        return f"{self.trainer.username} ↔ {self.client.username} ({self.status})"

class DeviceToken(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='device_tokens')
    token = models.CharField(max_length=255, unique=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.user.username} - {self.token[:10]}..."

class OTPVerification(models.Model):
    """
    Model to store OTP codes for email verification during registration.
    OTP expires after 10 minutes and can only be used once.
    """
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='otp_verifications'
    )
    otp_code = models.CharField(max_length=6, help_text="6-digit OTP code")
    email = models.EmailField(help_text="Email address for lookup")
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField(help_text="OTP expiration time (10 minutes from creation)")
    is_verified = models.BooleanField(default=False, help_text="Whether this OTP has been used")
    verified_at = models.DateTimeField(null=True, blank=True, help_text="When OTP was verified")

    class Meta:
        verbose_name = "OTP Verification"
        verbose_name_plural = "OTP Verifications"
        indexes = [
            models.Index(fields=['email', 'otp_code', 'is_verified']),
            models.Index(fields=['expires_at']),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=['user', 'otp_code'],
                condition=models.Q(is_verified=False),
                name='unique_unverified_otp_per_user'
            ),
        ]

    def __str__(self):
        return f"OTP for {self.email} - {self.otp_code} ({'verified' if self.is_verified else 'pending'})"

    def is_valid(self):
        """Check if OTP is still valid (not expired and not used)."""
        from django.utils import timezone
        return not self.is_verified and timezone.now() < self.expires_at