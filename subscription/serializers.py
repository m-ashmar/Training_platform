from rest_framework import serializers
from .models import (
    SubscriptionPlan, Subscription, Payment, 
    SubscriptionFeature, SubscriptionUsage
)
from users.serializers import CustomUserSerializer

class SubscriptionPlanSerializer(serializers.ModelSerializer):
    """Serializer for subscription plans"""
    
    class Meta:
        model = SubscriptionPlan
        fields = [
            'id', 'name', 'plan_type', 'description', 'price', 
            'duration_days', 'is_active', 'has_diet_access', 
            'has_routine_access', 'has_challenges_access', 
            'has_ai_advice', 'has_priority_support', 
            'max_meals_per_day', 'max_routines', 'created_at'
        ]
        read_only_fields = ['id', 'created_at']

class SubscriptionFeatureSerializer(serializers.ModelSerializer):
    """Serializer for subscription features"""
    
    class Meta:
        model = SubscriptionFeature
        fields = ['id', 'name', 'description', 'is_active', 'created_at']
        read_only_fields = ['id', 'created_at']

class PaymentSerializer(serializers.ModelSerializer):
    """Serializer for payments"""
    
    class Meta:
        model = Payment
        fields = [
            'id', 'subscription', 'amount', 'currency', 'status',
            'payment_method', 'transaction_id', 'payment_intent_id',
            'description', 'metadata', 'created_at'
        ]
        read_only_fields = ['id', 'created_at']

class SubscriptionUsageSerializer(serializers.ModelSerializer):
    """Serializer for subscription usage tracking"""
    feature_name = serializers.CharField(source='feature.name', read_only=True)
    usage_percentage = serializers.ReadOnlyField()
    
    class Meta:
        model = SubscriptionUsage
        fields = [
            'id', 'subscription', 'feature', 'feature_name',
            'usage_count', 'limit', 'usage_percentage',
            'period_start', 'period_end'
        ]
        read_only_fields = ['id', 'usage_percentage']

class SubscriptionSerializer(serializers.ModelSerializer):
    """Serializer for user subscriptions"""
    plan = SubscriptionPlanSerializer(read_only=True)
    plan_id = serializers.UUIDField(write_only=True, required=False)
    user = CustomUserSerializer(read_only=True)
    is_active = serializers.ReadOnlyField()
    is_trial = serializers.ReadOnlyField()
    days_remaining = serializers.ReadOnlyField()
    payments = PaymentSerializer(many=True, read_only=True)
    usage = SubscriptionUsageSerializer(many=True, read_only=True)
    
    class Meta:
        model = Subscription
        fields = [
            'id', 'user', 'plan', 'plan_id', 'status', 'start_date',
            'end_date', 'trial_end_date', 'auto_renew', 'cancelled_at',
            'has_diet_access', 'has_routine_access', 'has_challenges_access',
            'has_ai_advice', 'has_priority_support', 'is_active',
            'is_trial', 'days_remaining', 'payments', 'usage',
            'created_at', 'updated_at'
        ]
        read_only_fields = [
            'id', 'user', 'start_date', 'end_date', 'trial_end_date',
            'cancelled_at', 'is_active', 'is_trial', 'days_remaining',
            'payments', 'usage', 'created_at', 'updated_at'
        ]
    
    def create(self, validated_data):
        plan_id = validated_data.pop('plan_id', None)
        if plan_id:
            try:
                plan = SubscriptionPlan.objects.get(id=plan_id)
                validated_data['plan'] = plan
            except SubscriptionPlan.DoesNotExist:
                raise serializers.ValidationError("Invalid plan ID")
        
        return super().create(validated_data)

class SubscriptionCreateSerializer(serializers.ModelSerializer):
    """Serializer for creating new subscriptions"""
    plan_id = serializers.UUIDField()
    
    class Meta:
        model = Subscription
        fields = ['plan_id', 'auto_renew']
    
    def validate_plan_id(self, value):
        try:
            plan = SubscriptionPlan.objects.get(id=value, is_active=True)
            return value
        except SubscriptionPlan.DoesNotExist:
            raise serializers.ValidationError("Invalid or inactive plan")
    
    def create(self, validated_data):
        plan_id = validated_data.pop('plan_id')
        plan = SubscriptionPlan.objects.get(id=plan_id)
        user = self.context['request'].user
        
        # Check if user already has a subscription
        if hasattr(user, 'subscription'):
            raise serializers.ValidationError("User already has a subscription")
        
        validated_data['user'] = user
        validated_data['plan'] = plan
        return super().create(validated_data)

class SubscriptionUpdateSerializer(serializers.ModelSerializer):
    """Serializer for updating subscriptions"""
    
    class Meta:
        model = Subscription
        fields = ['auto_renew', 'status']
        read_only_fields = ['status']
    
    def validate(self, data):
        # Only allow updating auto_renew for active subscriptions
        if self.instance and self.instance.status not in ['active', 'trial']:
            raise serializers.ValidationError("Cannot update inactive subscription")
        return data

class SubscriptionCancelSerializer(serializers.Serializer):
    """Serializer for cancelling subscriptions"""
    reason = serializers.CharField(max_length=500, required=False)
    immediate = serializers.BooleanField(default=False)
    
    def validate(self, data):
        subscription = self.context.get('subscription')
        if not subscription:
            raise serializers.ValidationError("No subscription found")
        
        if subscription.status in ['cancelled', 'expired']:
            raise serializers.ValidationError("Subscription is already cancelled or expired")
        
        return data

class PaymentCreateSerializer(serializers.ModelSerializer):
    """Serializer for creating payments"""
    
    class Meta:
        model = Payment
        fields = [
            'subscription', 'amount', 'currency', 'payment_method',
            'description', 'metadata'
        ]
        read_only_fields = ['status', 'transaction_id', 'payment_intent_id']
    
    def validate_subscription(self, value):
        # Ensure the subscription belongs to the current user
        user = self.context['request'].user
        if value.user != user:
            raise serializers.ValidationError("Invalid subscription")
        return value

class SubscriptionPlanListSerializer(serializers.ModelSerializer):
    """Simplified serializer for listing subscription plans"""
    
    class Meta:
        model = SubscriptionPlan
        fields = [
            'id', 'name', 'plan_type', 'description', 'price',
            'duration_days', 'has_diet_access', 'has_routine_access',
            'has_challenges_access', 'has_ai_advice', 'has_priority_support'
        ]

class UserSubscriptionSummarySerializer(serializers.ModelSerializer):
    """Summary serializer for user's current subscription"""
    plan_name = serializers.CharField(source='plan.name', read_only=True)
    plan_price = serializers.DecimalField(source='plan.price', max_digits=10, decimal_places=2, read_only=True)
    is_active = serializers.ReadOnlyField()
    days_remaining = serializers.ReadOnlyField()
    
    class Meta:
        model = Subscription
        fields = [
            'id', 'plan_name', 'plan_price', 'status', 'start_date',
            'end_date', 'is_active', 'days_remaining', 'auto_renew'
        ] 