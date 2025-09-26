from django.db import models
from django.core.validators import MinValueValidator, MaxValueValidator
from django.core.exceptions import ValidationError
from users.models import CustomUser
from datetime import timedelta
import uuid
from django.utils import timezone

class SubscriptionPlan(models.Model):
    """Subscription plans with different features and pricing"""
    PLAN_TYPES = [
        ('basic', 'Basic'),
        ('premium', 'Premium'),
        ('pro', 'Professional'),
        ('enterprise', 'Enterprise'),
    ]
    
    name = models.CharField(max_length=100, unique=True, db_index=True)
    plan_type = models.CharField(max_length=20, choices=PLAN_TYPES, default='basic', db_index=True)
    description = models.TextField()
    price = models.DecimalField(
        max_digits=10, 
        decimal_places=2,
        validators=[MinValueValidator(0, "Price cannot be negative")]
    )
    duration_days = models.IntegerField(
        default=30,
        validators=[MinValueValidator(1, "Duration must be at least 1 day")]
    )
    is_active = models.BooleanField(default=True, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    # Feature flags
    has_diet_access = models.BooleanField(default=False, db_index=True)
    has_routine_access = models.BooleanField(default=False, db_index=True)
    has_challenges_access = models.BooleanField(default=False, db_index=True)
    has_ai_advice = models.BooleanField(default=False, db_index=True)
    has_priority_support = models.BooleanField(default=False, db_index=True)
    max_meals_per_day = models.IntegerField(
        default=3,
        validators=[MinValueValidator(0, "Max meals cannot be negative")]
    )
    max_routines = models.IntegerField(
        default=5,
        validators=[MinValueValidator(0, "Max routines cannot be negative")]
    )
    
    class Meta:
        ordering = ['price']
        indexes = [
            models.Index(fields=['is_active', 'plan_type']),
            models.Index(fields=['price', 'is_active']),
        ]
        verbose_name = "Subscription Plan"
        verbose_name_plural = "Subscription Plans"
    
    def clean(self):
        """Custom validation"""
        if self.price < 0:
            raise ValidationError("Price cannot be negative")
        if self.duration_days <= 0:
            raise ValidationError("Duration must be greater than 0")
    
    def save(self, *args, **kwargs):
        self.full_clean()
        super().save(*args, **kwargs)
    
    def __str__(self):
        return f"{self.name} - {self.price} SYP"

class Subscription(models.Model):
    """User subscription details"""
    STATUS_CHOICES = [
        ('active', 'Active'),
        ('expired', 'Expired'),
        ('cancelled', 'Cancelled'),
        ('pending', 'Pending'),
        ('trial', 'Trial'),
    ]
    
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.OneToOneField(
        CustomUser, 
        on_delete=models.CASCADE, 
        related_name='subscription',
        db_index=True
    )
    plan = models.ForeignKey(
        SubscriptionPlan, 
        on_delete=models.PROTECT, 
        related_name='subscriptions',
        db_index=True
    )
    status = models.CharField(
        max_length=20, 
        choices=STATUS_CHOICES, 
        default='pending',
        db_index=True
    )
    
    start_date = models.DateTimeField(auto_now_add=True, db_index=True)
    end_date = models.DateTimeField(db_index=True)
    trial_end_date = models.DateTimeField(null=True, blank=True, db_index=True)
    
    auto_renew = models.BooleanField(default=True, db_index=True)
    cancelled_at = models.DateTimeField(null=True, blank=True)
    
    # Access flags (inherited from plan but can be overridden)
    has_diet_access = models.BooleanField(default=False, db_index=True)
    has_routine_access = models.BooleanField(default=False, db_index=True)
    has_challenges_access = models.BooleanField(default=False, db_index=True)
    has_ai_advice = models.BooleanField(default=False, db_index=True)
    has_priority_support = models.BooleanField(default=False, db_index=True)
    
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['status', 'end_date']),
            models.Index(fields=['user', 'status']),
            models.Index(fields=['trial_end_date', 'status']),
            models.Index(fields=['auto_renew', 'status']),
        ]
        verbose_name = "Subscription"
        verbose_name_plural = "Subscriptions"
    
    def clean(self):
        """Custom validation"""
        if self.end_date and self.start_date and self.end_date <= self.start_date:
            raise ValidationError("End date must be after start date")
        if self.trial_end_date and self.start_date and self.trial_end_date <= self.start_date:
            raise ValidationError("Trial end date must be after start date")
    
    def save(self, *args, **kwargs):
        from django.utils import timezone
        
        # Set start_date if not provided (for new objects)
        if not self.start_date:
            self.start_date = timezone.now()
        
        if not self.end_date and self.plan:
            self.end_date = self.start_date + timedelta(days=self.plan.duration_days)
        
        # Inherit features from plan
        if self.plan:
            self.has_diet_access = self.plan.has_diet_access
            self.has_routine_access = self.plan.has_routine_access
            self.has_challenges_access = self.plan.has_challenges_access
            self.has_ai_advice = self.plan.has_ai_advice
            self.has_priority_support = self.plan.has_priority_support
        
        self.full_clean()
        super().save(*args, **kwargs)
    
    @property
    def is_active(self):
        """Check if subscription is currently active"""
        now = timezone.now()
        return (
            self.status == 'active' and 
            self.end_date > now and
            (not self.trial_end_date or self.trial_end_date > now)
        )
    
    @property
    def is_trial(self):
        """Check if subscription is in trial period"""
        now = timezone.now()
        return (
            self.status == 'trial' and 
            self.trial_end_date and 
            self.trial_end_date > now
        )
    
    @property
    def days_remaining(self):
        """Get days remaining in subscription"""
        now = timezone.now()
        if self.trial_end_date and self.trial_end_date > now:
            return (self.trial_end_date - now).days
        return max(0, (self.end_date - now).days)
    
    def __str__(self):
        return f"{self.user.username} - {self.plan.name} ({self.status})"

class Payment(models.Model):
    """Payment history for subscriptions"""
    PAYMENT_STATUS = [
        ('pending', 'Pending'),
        ('completed', 'Completed'),
        ('failed', 'Failed'),
        ('refunded', 'Refunded'),
        ('cancelled', 'Cancelled'),
    ]
    
    PAYMENT_METHOD = [
        # Syrian Payment Gateways
        ('syriatel_cash', 'Syriatel Cash'),
        ('baraka_bank', 'Al-Baraka Bank'),
        ('bemo_bank', 'BEMO Bank'),
        # International Payment Methods
        ('stripe', 'Stripe'),
        ('paypal', 'PayPal'),
        ('apple_pay', 'Apple Pay'),
        ('google_pay', 'Google Pay'),
        ('manual', 'Manual'),
    ]
    
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    subscription = models.ForeignKey(
        Subscription, 
        on_delete=models.CASCADE, 
        related_name='payments',
        db_index=True
    )
    amount = models.DecimalField(
        max_digits=10, 
        decimal_places=2,
        validators=[MinValueValidator(0, "Amount cannot be negative")]
    )
    currency = models.CharField(max_length=3, default='USD', db_index=True)
    status = models.CharField(
        max_length=20, 
        choices=PAYMENT_STATUS, 
        default='pending',
        db_index=True
    )
    payment_method = models.CharField(
        max_length=20, 
        choices=PAYMENT_METHOD, 
        default='stripe',
        db_index=True
    )
    
    # External payment provider details
    transaction_id = models.CharField(max_length=255, blank=True, null=True, db_index=True)
    payment_intent_id = models.CharField(max_length=255, blank=True, null=True, db_index=True)
    
    # Gateway-specific fields
    gateway_response = models.JSONField(default=dict, blank=True)
    gateway_error = models.TextField(blank=True, null=True)
    gateway_transaction_reference = models.CharField(max_length=255, blank=True, null=True, db_index=True)
    
    # Metadata
    description = models.TextField(blank=True)
    metadata = models.JSONField(default=dict, blank=True)
    
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['status', 'created_at']),
            models.Index(fields=['payment_method', 'status']),
            models.Index(fields=['transaction_id']),
            models.Index(fields=['payment_intent_id']),
        ]
        verbose_name = "Payment"
        verbose_name_plural = "Payments"
    
    def clean(self):
        """Custom validation"""
        if self.amount < 0:
            raise ValidationError("Payment amount cannot be negative")

    def save(self, *args, **kwargs):
        self.full_clean()
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.subscription.user.username} - ${self.amount} ({self.status})"

class SubscriptionFeature(models.Model):
    """Individual features that can be enabled/disabled per subscription"""
    name = models.CharField(max_length=100, unique=True, db_index=True)
    description = models.TextField()
    is_active = models.BooleanField(default=True, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    
    class Meta:
        ordering = ['name']
        verbose_name = "Subscription Feature"
        verbose_name_plural = "Subscription Features"
    
    def __str__(self):
        return self.name

class SubscriptionUsage(models.Model):
    """Track usage of subscription features"""
    subscription = models.ForeignKey(
        Subscription, 
        on_delete=models.CASCADE, 
        related_name='usage',
        db_index=True
    )
    feature = models.ForeignKey(
        SubscriptionFeature, 
        on_delete=models.CASCADE,
        db_index=True
    )
    usage_count = models.IntegerField(
        default=0,
        validators=[MinValueValidator(0, "Usage count cannot be negative")]
    )
    limit = models.IntegerField(
        default=0,  # 0 means unlimited
        validators=[MinValueValidator(0, "Limit cannot be negative")]
    )
    period_start = models.DateTimeField(auto_now_add=True, db_index=True)
    period_end = models.DateTimeField(db_index=True)
    
    class Meta:
        unique_together = ['subscription', 'feature', 'period_start']
        indexes = [
            models.Index(fields=['subscription', 'feature']),
            models.Index(fields=['period_end']),
        ]
        verbose_name = "Subscription Usage"
        verbose_name_plural = "Subscription Usage"
    
    def clean(self):
        """Custom validation"""
        if self.usage_count < 0:
            raise ValidationError("Usage count cannot be negative")
        if self.limit < 0:
            raise ValidationError("Limit cannot be negative")
        if self.period_end and self.period_start and self.period_end <= self.period_start:
            raise ValidationError("Period end must be after period start")

    def save(self, *args, **kwargs):
        self.full_clean()
        super().save(*args, **kwargs)

    @property
    def usage_percentage(self):
        """Get usage as percentage of limit"""
        if self.limit == 0:
            return 0
        return min(100, (self.usage_count / self.limit) * 100)
    
    def __str__(self):
        return f"{self.subscription.user.username} - {self.feature.name}: {self.usage_count}/{self.limit}"