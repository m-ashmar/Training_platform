from rest_framework import permissions
from django.utils import timezone

class HasActiveSubscription(permissions.BasePermission):
    """
    Permission to check if user has an active subscription.
    """
    message = "You need an active subscription to access this feature."
    
    def has_permission(self, request, view):
        if not request.user.is_authenticated:
            return False
        
        try:
            subscription = request.user.subscription
            return subscription.is_active
        except:
            return False

class HasSubscriptionFeature(permissions.BasePermission):
    """
    Permission to check if user's subscription includes specific features.
    """
    
    def __init__(self, required_feature=None):
        self.required_feature = required_feature
        super().__init__()
    
    def has_permission(self, request, view):
        if not request.user.is_authenticated:
            return False
        
        try:
            subscription = request.user.subscription
            
            if not subscription.is_active:
                return False
            
            if self.required_feature == 'diet':
                return subscription.has_diet_access
            elif self.required_feature == 'routine':
                return subscription.has_routine_access
            elif self.required_feature == 'challenges':
                return subscription.has_challenges_access
            elif self.required_feature == 'ai_advice':
                return subscription.has_ai_advice
            elif self.required_feature == 'priority_support':
                return subscription.has_priority_support
            else:
                return True
                
        except:
            return False

class HasDietAccess(permissions.BasePermission):
    """Permission to check if user has diet feature access."""
    
    def has_permission(self, request, view):
        return HasSubscriptionFeature('diet').has_permission(request, view)

class HasRoutineAccess(permissions.BasePermission):
    """Permission to check if user has routine feature access."""
    
    def has_permission(self, request, view):
        return HasSubscriptionFeature('routine').has_permission(request, view)

class HasChallengesAccess(permissions.BasePermission):
    """Permission to check if user has challenges feature access."""
    
    def has_permission(self, request, view):
        return HasSubscriptionFeature('challenges').has_permission(request, view)

class HasAIAdviceAccess(permissions.BasePermission):
    """Permission to check if user has AI advice feature access."""
    
    def has_permission(self, request, view):
        return HasSubscriptionFeature('ai_advice').has_permission(request, view)

class HasPrioritySupportAccess(permissions.BasePermission):
    """Permission to check if user has priority support access."""
    
    def has_permission(self, request, view):
        return HasSubscriptionFeature('priority_support').has_permission(request, view)

class SubscriptionUsageLimit(permissions.BasePermission):
    """
    Permission to check if user hasn't exceeded usage limits for a feature.
    """
    
    def __init__(self, feature_name=None, limit_field=None):
        self.feature_name = feature_name
        self.limit_field = limit_field
        super().__init__()
    
    def has_permission(self, request, view):
        if not request.user.is_authenticated:
            return False
        
        try:
            subscription = request.user.subscription
            
            if not subscription.is_active:
                return False
            
            # Get the limit from subscription plan
            if self.limit_field:
                limit = getattr(subscription.plan, self.limit_field, 0)
            else:
                limit = 0
            
            # If no limit (unlimited), allow access
            if limit == 0:
                return True
            
            # Check current usage
            from .models import SubscriptionUsage, SubscriptionFeature
            
            try:
                feature = SubscriptionFeature.objects.get(name=self.feature_name)
                usage, created = SubscriptionUsage.objects.get_or_create(
                    subscription=subscription,
                    feature=feature,
                    defaults={'limit': limit, 'period_end': timezone.now() + timezone.timedelta(days=30)}
                )
                
                return usage.usage_count < limit
                
            except SubscriptionFeature.DoesNotExist:
                return False
                
        except:
            return False

class MealUsageLimit(permissions.BasePermission):
    """Permission to check daily meal creation limits."""
    
    def has_permission(self, request, view):
        return SubscriptionUsageLimit('daily_meals', 'max_meals_per_day').has_permission(request, view)

class RoutineUsageLimit(permissions.BasePermission):
    """Permission to check routine creation limits."""
    
    def has_permission(self, request, view):
        return SubscriptionUsageLimit('routines', 'max_routines').has_permission(request, view) 