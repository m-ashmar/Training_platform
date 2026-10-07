from django.utils import timezone
from django.utils.translation import gettext as _
from django.db import transaction
from datetime import timedelta
from .models import (Subscription, SubscriptionPlan, Payment, SubscriptionUsage,
                     SubscriptionFeature)


def checkout_ttl() -> timedelta:
    """How long a checkout stays open before the server closes it."""
    from django.conf import settings

    return timedelta(minutes=int(getattr(settings, 'PAYMENT_CHECKOUT_TTL_MINUTES', 60)))


def checkout_expires_at(payment):
    """When this checkout closes, or None once it is no longer open.

    The Pay screen counts down to it ("الرمز صالح لمدة 71:42:10"): the reference is
    payable until the poller expires the checkout, and until now nothing told the app
    when that was — `POST v1/gateways/` answered `expires_at` straight out of the
    gateway's response, and the ShamCash adapter has never set that key, so the field
    was null on every checkout ever opened and the countdown had no source.

    Derived from the same two values `subscription.poll_open_payments` decides on, so
    the screen and the server cannot disagree. A payment that is finished — completed,
    failed, cancelled, expired, refunded — has no deadline left to show.
    """
    from .models import HELD_PAYMENT_STATUSES, OPEN_PAYMENT_STATUSES

    # A checkout held for review (B-051) is open but never expires: money was seen for
    # it and an operator decides. No countdown.
    if (payment.status not in OPEN_PAYMENT_STATUSES
            or payment.status in HELD_PAYMENT_STATUSES or payment.created_at is None):
        return None
    return payment.created_at + checkout_ttl()


def payment_instruction_allowed(request, payment, cache: dict | None = None) -> bool:
    """Whether a payment read may carry this payment's `amount` and `reference` to the
    build that asked (SIDE_BUGS S-239).

    While the payment is OPEN (`OPEN_PAYMENT_STATUSES`: pending, processing,
    authorized, needs_review) they tell the reader what to pay and what to write in the
    transfer's note — a payment instruction — so only a build that may buy this kind of
    money gets them (`policy.services.require_purchase_allowed`; the spec, section 4,
    rule zero), which is the rule `checkout/sessions/{id}/` already follows. A payment
    completed, refunded or closed is a record of money that moved, answered on every
    build. By build only. `cache` (money type -> answer) spares a list asking per row.
    """
    from policy.exceptions import AppChannelRequired, NotAvailableOnChannel
    from policy.services import require_purchase_allowed

    from .models import OPEN_PAYMENT_STATUSES

    if payment.status not in OPEN_PAYMENT_STATUSES:
        return True
    if request is None:
        return True          # server-side code (a task, the console): no build to ask
    money_type = payment.money_type
    if cache is not None and money_type in cache:
        return cache[money_type]
    try:
        require_purchase_allowed(request, money_type)
        allowed = True
    except (AppChannelRequired, NotAvailableOnChannel):
        allowed = False
    if cache is not None:
        cache[money_type] = allowed
    return allowed

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
                source='trial',
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
        
        # `subscription.quota` owns the period, the limit lookup and the increment.
        # The version that lived here resolved the limit from `max_{feature_name}` —
        # `max_daily_meals` for the `daily_meals` feature, a field the plan does not
        # have — so it read 0 and every caller saw "unlimited". It also had no callers.
        from subscription import quota
        return quota.consume(user, feature_name, increment)
        
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
    
    # Revenue is reported PER CURRENCY. Summing a single 'amount' column across a
    # ledger that holds both SYP and USD rows produced one meaningless number; the
    # currencies differ by four orders of magnitude.
    revenue_by_currency = {
        row['currency']: row['total']
        for row in Payment.objects.filter(status='completed')
        .values('currency')
        .annotate(total=models.Sum('amount'))
        .order_by('currency')
    }
    
    return {
        'total_subscriptions': total_subscriptions,
        'active_subscriptions': active_subscriptions,
        'trial_subscriptions': trial_subscriptions,
        'expired_subscriptions': expired_subscriptions,
        'cancelled_subscriptions': cancelled_subscriptions,
        'revenue_by_currency': revenue_by_currency,
    }

def expire_subscriptions():
    """Deprecated shim. Use the scheduled task instead.

    Kept so any caller outside this repo keeps working, but the implementation now
    lives in subscription.tasks.expire_lapsed_subscriptions, which is registered with
    Celery and listed in CELERY_BEAT_SCHEDULE. Two copies of an expiry rule is one
    copy too many.
    """
    from .tasks import expire_lapsed_subscriptions

    return expire_lapsed_subscriptions()


# `count_subscriptions_needing_notice` stood here: it counted subscriptions about to
# lapse and told nobody. The notices exist now: `subscription.send_renewal_reminders`
# (subscription/tasks.py) sends "ends soon" and, after the end, "has ended" (the owner,
# 2026-09-19).

# Import models at the top
from django.db import models 