from django.utils import timezone
from django.utils.translation import gettext as _
from django.db import transaction
from datetime import timedelta
from .models import Subscription, SubscriptionPlan, Payment, SubscriptionUsage, SubscriptionFeature

def create_trial_subscription(user, plan_id, trial_days=7):
    """
    Create a trial subscription for a user.
    
    Args:
        user: CustomUser instance
        plan_id: UUID of the subscription plan
        trial_days: Number of days for trial (default: 7)
    
    Returns:
        Subscription instance or None if failed
    """
    try:
        plan = SubscriptionPlan.objects.get(id=plan_id, is_active=True)
        
        # Check if user already has a subscription
        if hasattr(user, 'subscription'):
            return None
        
        with transaction.atomic():
            trial_end = timezone.now() + timedelta(days=trial_days)
            subscription = Subscription.objects.create(
                user=user,
                plan=plan,
                status='trial',
                trial_end_date=trial_end
            )
            
            return subscription
            
    except SubscriptionPlan.DoesNotExist:
        return None

def activate_subscription(subscription_id):
    """
    DEPRECATED / REMOVED. Subscriptions may only be activated by
    PaymentService.complete_payment after verified payment. This helper used to
    flip status to 'active' and mark pending payments completed with no payment
    verification — a paywall bypass — so it now refuses to run.
    """
    raise NotImplementedError(
        "activate_subscription is disabled; activation happens only via "
        "PaymentService.complete_payment after verified payment."
    )

def cancel_subscription(subscription_id, immediate=False, reason=""):
    """
    Cancel a subscription.
    
    Args:
        subscription_id: UUID of the subscription
        immediate: If True, cancel immediately. If False, disable auto-renewal
        reason: Reason for cancellation
    
    Returns:
        bool: True if successful, False otherwise
    """
    try:
        with transaction.atomic():
            subscription = Subscription.objects.get(id=subscription_id)
            
            if immediate:
                subscription.status = 'cancelled'
                subscription.cancelled_at = timezone.now()
            else:
                subscription.auto_renew = False
            
            subscription.save()
            
            # Log the cancellation
            Payment.objects.create(
                subscription=subscription,
                amount=0,
                currency='USD',
                status='cancelled',
                description=f"Subscription cancelled: {reason}",
                metadata={'reason': reason, 'immediate': immediate}
            )
            
            return True
            
    except Subscription.DoesNotExist:
        return False

def renew_subscription(subscription_id):
    """
    DEPRECATED / REMOVED. Renewal must not extend a subscription for free.
    Use PaymentService.start_renewal to create a pending payment and complete it
    via PaymentService.complete_payment after the gateway confirms funds.
    """
    raise NotImplementedError(
        "renew_subscription is disabled; use PaymentService.start_renewal + "
        "complete_payment (verified) instead."
    )

def check_subscription_access(user, required_features=None):
    """
    Check if user has access to specific features.
    
    Args:
        user: CustomUser instance
        required_features: List of required features (e.g., ['diet', 'routine'])
    
    Returns:
        dict: Access results for each feature
    """
    try:
        subscription = user.subscription
        
        if not subscription.is_active:
            return {
                'has_access': False,
                'message': _('No active subscription'),
                'access_details': {}
            }
        
        if not required_features:
            return {
                'has_access': True,
                'access_details': {},
                'subscription_status': subscription.status
            }
        
        access_results = {}
        for feature in required_features:
            if feature == 'diet':
                access_results[feature] = subscription.has_diet_access
            elif feature == 'routine':
                access_results[feature] = subscription.has_routine_access
            elif feature == 'challenges':
                access_results[feature] = subscription.has_challenges_access
            elif feature == 'ai_advice':
                access_results[feature] = subscription.has_ai_advice
            elif feature == 'priority_support':
                access_results[feature] = subscription.has_priority_support
            else:
                access_results[feature] = False
        
        return {
            'has_access': any(access_results.values()),
            'access_details': access_results,
            'subscription_status': subscription.status,
            'days_remaining': subscription.days_remaining
        }
        
    except Subscription.DoesNotExist:
        return {
            'has_access': False,
            'message': _('No subscription found'),
            'access_details': {}
        }

def track_feature_usage(user, feature_name, increment=1):
    """
    Track usage of a subscription feature.
    
    Args:
        user: CustomUser instance
        feature_name: Name of the feature being used
        increment: Amount to increment usage (default: 1)
    
    Returns:
        bool: True if usage was tracked successfully
    """
    try:
        subscription = user.subscription
        
        if not subscription.is_active:
            return False
        
        feature = SubscriptionFeature.objects.get(name=feature_name, is_active=True)
        
        # Get or create usage record
        usage, created = SubscriptionUsage.objects.get_or_create(
            subscription=subscription,
            feature=feature,
            defaults={
                'limit': getattr(subscription.plan, f'max_{feature_name}', 0),
                'period_end': timezone.now() + timedelta(days=30)
            }
        )
        
        # Atomic increment — avoids a read-modify-write race that could lose
        # concurrent increments and let a user exceed their plan limit.
        SubscriptionUsage.objects.filter(pk=usage.pk).update(
            usage_count=models.F('usage_count') + increment
        )

        return True
        
    except (Subscription.DoesNotExist, SubscriptionFeature.DoesNotExist):
        return False

def get_subscription_statistics():
    """
    Get overall subscription statistics.
    
    Returns:
        dict: Statistics about subscriptions
    """
    total_subscriptions = Subscription.objects.count()
    active_subscriptions = Subscription.objects.filter(status='active').count()
    trial_subscriptions = Subscription.objects.filter(status='trial').count()
    expired_subscriptions = Subscription.objects.filter(status='expired').count()
    cancelled_subscriptions = Subscription.objects.filter(status='cancelled').count()
    
    # Revenue calculation (completed payments)
    total_revenue = Payment.objects.filter(status='completed').aggregate(
        total=models.Sum('amount')
    )['total'] or 0
    
    return {
        'total_subscriptions': total_subscriptions,
        'active_subscriptions': active_subscriptions,
        'trial_subscriptions': trial_subscriptions,
        'expired_subscriptions': expired_subscriptions,
        'cancelled_subscriptions': cancelled_subscriptions,
        'total_revenue': total_revenue
    }

def expire_subscriptions():
    """
    Expire subscriptions that have passed their end date.
    This should be run as a scheduled task.
    
    Returns:
        int: Number of subscriptions expired
    """
    now = timezone.now()
    expired_count = 0
    
    # Find subscriptions that should be expired
    expired_subscriptions = Subscription.objects.filter(
        status__in=['active', 'trial'],
        end_date__lt=now
    )
    
    for subscription in expired_subscriptions:
        subscription.status = 'expired'
        subscription.save()
        expired_count += 1
    
    return expired_count

def send_subscription_notifications():
    """
    Send notifications for subscription events.
    This should be run as a scheduled task.
    
    Returns:
        int: Number of notifications sent
    """
    now = timezone.now()
    notification_count = 0
    
    # Notify users about expiring subscriptions
    expiring_soon = Subscription.objects.filter(
        status='active',
        end_date__range=[now, now + timedelta(days=7)]
    )
    
    for subscription in expiring_soon:
        # Here you would send email/push notification
        # For now, we'll just count them
        notification_count += 1
    
    # Notify users about trial ending
    trial_ending = Subscription.objects.filter(
        status='trial',
        trial_end_date__range=[now, now + timedelta(days=3)]
    )
    
    for subscription in trial_ending:
        # Here you would send email/push notification
        notification_count += 1
    
    return notification_count

# Import models at the top
from django.db import models 