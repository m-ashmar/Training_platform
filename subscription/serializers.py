from rest_framework import serializers
from .models import (
    SubscriptionPlan, Subscription, Payment, 
    SubscriptionFeature, SubscriptionUsage
)
from users.serializers import CustomUserSerializer
from django.utils.translation import gettext_lazy as _
from policy.money import MoneyType
from policy.services import can_show_price


class PlanPriceByChannelMixin:
    """Leaves a plan's price out of a response to a build that may not show it.

    A store build shows what the user holds — plan, status, expiry — and never what a plan
    costs, so the number is not sent to it at all rather than sent and trusted to be
    hidden. Decided from the request's channel alone, so it is the same for every user of
    a build. With no request in the context the caller is server-side code (the operator
    console, a task), and nothing is removed.
    """
    price_fields = ('price',)

    def to_representation(self, instance):
        data = super().to_representation(instance)
        request = self.context.get('request')
        if request is not None and not can_show_price(request, MoneyType.PLATFORM_SUB):
            for name in self.price_fields:
                data.pop(name, None)
        return data


class SubscriptionPlanSerializer(PlanPriceByChannelMixin, serializers.ModelSerializer):
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
    """Read-only serializer for payments. Business/state fields are never client-writable;
    payment state is owned exclusively by PaymentService."""

    class Meta:
        model = Payment
        fields = [
            'id', 'subscription', 'amount', 'currency', 'status',
            'payment_method', 'transaction_id', 'payment_intent_id',
            'invoice_number', 'description', 'metadata', 'created_at',
            'money_type',
        ]
        read_only_fields = fields

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
    plan_id = serializers.IntegerField(write_only=True, required=False)
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
            'created_at', 'updated_at', 'source',
        ]
        read_only_fields = [
            'id', 'user', 'start_date', 'end_date', 'trial_end_date',
            'cancelled_at', 'is_active', 'is_trial', 'days_remaining',
            'payments', 'usage', 'created_at', 'updated_at', 'source',
        ]
    
    def create(self, validated_data):
        plan_id = validated_data.pop('plan_id', None)
        if plan_id:
            try:
                plan = SubscriptionPlan.objects.get(id=plan_id)
                validated_data['plan'] = plan
            except SubscriptionPlan.DoesNotExist:
                raise serializers.ValidationError(_("Invalid plan ID"))
        
        return super().create(validated_data)

class SubscriptionCreateSerializer(serializers.ModelSerializer):
    """Serializer for creating new subscriptions"""
    plan_id = serializers.IntegerField()

    class Meta:
        model = Subscription
        # `id` and `status` in the response: the next step (POST /v1/gateways/) needs
        # the subscription id, and the create answered only {plan_id, auto_renew}, so
        # the app had to call /subscriptions/current/ to learn what it had just made.
        fields = ['id', 'plan_id', 'auto_renew', 'status']
        read_only_fields = ['id', 'status']
    
    def validate_plan_id(self, value):
        try:
            plan = SubscriptionPlan.objects.get(id=value, is_active=True)
            return value
        except SubscriptionPlan.DoesNotExist:
            raise serializers.ValidationError(_("Invalid or inactive plan"))
    
    def create(self, validated_data):
        plan_id = validated_data.pop('plan_id')
        plan = SubscriptionPlan.objects.get(id=plan_id)
        user = self.context['request'].user
        
        # Check if user already has a subscription
        if hasattr(user, 'subscription'):
            raise serializers.ValidationError(_("User already has a subscription"))
        
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
            raise serializers.ValidationError(_("Cannot update inactive subscription"))
        return data

class SubscriptionCancelSerializer(serializers.Serializer):
    """Serializer for cancelling subscriptions"""
    reason = serializers.CharField(max_length=500, required=False)
    immediate = serializers.BooleanField(default=False)
    
    def validate(self, data):
        subscription = self.context.get('subscription')
        if not subscription:
            raise serializers.ValidationError(_("No subscription found"))
        
        if subscription.status in ['cancelled', 'expired']:
            raise serializers.ValidationError(_("Subscription is already cancelled or expired"))
        
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
            raise serializers.ValidationError(_("Invalid subscription"))
        return value

class SubscriptionPlanListSerializer(PlanPriceByChannelMixin, serializers.ModelSerializer):
    """Simplified serializer for listing subscription plans"""
    
    class Meta:
        model = SubscriptionPlan
        fields = [
            'id', 'name', 'plan_type', 'description', 'price',
            'duration_days', 'has_diet_access', 'has_routine_access',
            'has_challenges_access', 'has_ai_advice', 'has_priority_support'
        ]

class UserSubscriptionSummarySerializer(PlanPriceByChannelMixin, serializers.ModelSerializer):
    """Summary serializer for user's current subscription.

    `access`: 'active' | 'frozen' (it ended after entitling you: your diet plans stay
    readable and nothing can be used until you subscribe again — show the renewal
    banner) | 'inactive' (never entitled, e.g. a checkout never paid).
    `renewal_due`: true while active and ending within SUBSCRIPTION_RENEWAL_NOTICE_DAYS
    (3) — show the renewal reminder. Neither carries a price: the same on every build.
    """
    plan_name = serializers.CharField(source='plan.name', read_only=True)
    plan_price = serializers.DecimalField(source='plan.price', max_digits=10, decimal_places=2, read_only=True)
    is_active = serializers.ReadOnlyField()
    days_remaining = serializers.ReadOnlyField()
    access = serializers.ReadOnlyField()
    renewal_due = serializers.SerializerMethodField()

    price_fields = ('plan_price',)

    class Meta:
        model = Subscription
        fields = [
            'id', 'plan_name', 'plan_price', 'status', 'start_date',
            'end_date', 'is_active', 'days_remaining', 'auto_renew', 'source',
            'access', 'renewal_due',
        ]

    def get_renewal_due(self, obj):
        from datetime import timedelta

        from django.conf import settings
        from django.utils import timezone

        if not obj.is_active or obj.effective_end is None:
            return False
        lead = timedelta(days=int(getattr(settings, 'SUBSCRIPTION_RENEWAL_NOTICE_DAYS', 3)))
        return obj.effective_end - timezone.now() <= lead 