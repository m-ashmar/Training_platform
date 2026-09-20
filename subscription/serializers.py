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
            'id', 'name', 'plan_type', 'description', 'price', 'currency',
            'duration_days', 'is_active', 'is_featured', 'has_diet_access',
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
    payment state is owned exclusively by PaymentService.

    The "مدفوعات سابقة" list on the Receipt screen draws one row per payment as
    `item` ("Premium · 30 يوماً"), the date and the amount, with a receipt to open —
    so this answers `item`, `completed_at`, `reference` and `receipt_available`
    alongside the stored columns. All of them describe money ALREADY PAID, which is a
    record and not a sale, so nothing here is removed on a store build.
    """
    item = serializers.SerializerMethodField()
    receipt_available = serializers.SerializerMethodField()
    reference = serializers.CharField(source='gateway_transaction_reference',
                                      read_only=True, allow_null=True)

    class Meta:
        model = Payment
        fields = [
            'id', 'subscription', 'amount', 'currency', 'status',
            'payment_method', 'transaction_id', 'payment_intent_id',
            'invoice_number', 'description', 'metadata', 'created_at',
            'money_type', 'item', 'completed_at', 'reference',
            'receipt_available',
        ]
        read_only_fields = fields

    def get_item(self, obj):
        """What was bought, in pieces — the app joins them with the language's own
        separator rather than being handed a sentence it cannot lay out RTL."""
        plan = obj.subscription.plan
        return {'plan_id': plan.id, 'plan_name': plan.name,
                'plan_type': plan.plan_type, 'duration_days': plan.duration_days}

    def get_receipt_available(self, obj):
        """Whether `payments/{id}/receipt/` will answer. Only a completed payment has
        a receipt; the app hides the row's receipt icon for anything else."""
        return obj.status == 'completed'

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
        # `auto_renew` is read-only here too (plan step 5): it is answered, never taken.
        fields = ['id', 'plan_id', 'auto_renew', 'status']
        read_only_fields = ['id', 'status', 'auto_renew']
    
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
    """What a customer may change about their own subscription: nothing, today.

    `auto_renew` is READ-ONLY since plan step 5. Nothing charges a plan automatically —
    `subscription.send_renewal_reminders` reminds "every subscriber, whatever
    `auto_renew` says" — so the switch promised an automatic renewal that does not
    exist, and a store build may not offer one at all. The key stays in every response
    (the app reads it; removing it would break the contract), and a PATCH that carries
    it answers 400 `auto_renew_read_only` rather than accepting the write and ignoring
    it. The same shape as `profile_picture_read_only` (the owner, 2026-09-20).
    """

    class Meta:
        model = Subscription
        fields = ['auto_renew', 'status']
        read_only_fields = ['status', 'auto_renew']

    def validate(self, data):
        # Only allow updating auto_renew for active subscriptions
        if self.instance and self.instance.status not in ['active', 'trial']:
            raise serializers.ValidationError(_("Cannot update inactive subscription"))
        if 'auto_renew' in getattr(self, 'initial_data', {}) or {}:
            raise serializers.ValidationError({'auto_renew': serializers.ErrorDetail(
                _("Renewal is not automatic: nothing is charged without you. Cancelling "
                  "is on the subscription itself."), code='auto_renew_read_only')})
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
            'id', 'name', 'plan_type', 'description', 'price', 'currency',
            'duration_days', 'is_featured', 'has_diet_access', 'has_routine_access',
            'has_challenges_access', 'has_ai_advice', 'has_priority_support',
            'max_meals_per_day', 'max_routines',
        ]

class UserSubscriptionSummarySerializer(PlanPriceByChannelMixin, serializers.ModelSerializer):
    """Summary serializer for user's current subscription.

    `access`: 'active' | 'frozen' (it ended after entitling you: your diet plans stay
    readable and nothing can be used until you subscribe again — show the renewal
    banner) | 'inactive' (never entitled, e.g. a checkout never paid).
    `renewal_due`: true while active and ending within SUBSCRIPTION_RENEWAL_NOTICE_DAYS
    (3) — show the renewal reminder. Neither carries a price: the same on every build.

    For the Subscription screen (`screens/07-money/Subscription.png`):
    `plan_duration_days` is how long a renewal adds ("جدّد 30 يوماً", and the
    denominator of the days-left gauge — `start_date` cannot serve as one, because a
    renewal extends `end_date` and leaves `start_date` where it was);
    `plan_currency` is the unit `plan_price` is in, so the app never hard-codes "ل.س";
    `plan_features` is what the plan includes, which is what PaySuccess lists as
    "ما فُتح لك الآن" and what the Paywall compares against. Only `plan_price` is
    removed on a build that may not show prices — a duration, a currency code and a
    feature list are not a price and are the same on every build.
    """
    plan_name = serializers.CharField(source='plan.name', read_only=True)
    plan_price = serializers.DecimalField(source='plan.price', max_digits=10, decimal_places=2, read_only=True)
    plan_currency = serializers.CharField(source='plan.currency', read_only=True)
    plan_duration_days = serializers.IntegerField(source='plan.duration_days', read_only=True)
    plan_features = serializers.SerializerMethodField()
    is_active = serializers.ReadOnlyField()
    days_remaining = serializers.ReadOnlyField()
    access = serializers.ReadOnlyField()
    renewal_due = serializers.SerializerMethodField()

    price_fields = ('plan_price',)

    class Meta:
        model = Subscription
        fields = [
            'id', 'plan_name', 'plan_price', 'plan_currency',
            'plan_duration_days', 'plan_features', 'status', 'start_date',
            'end_date', 'is_active', 'days_remaining', 'auto_renew', 'source',
            'access', 'renewal_due',
        ]

    def get_plan_features(self, obj):
        """What this plan includes — PaySuccess's "ما فُتح لك الآن" and what the
        Paywall compares against.

        Read from the subscription's own flag columns, which is where every
        permission check reads them (`subscription/permissions.py`), so the screen
        says what the server will actually allow. They are copies of the plan's:
        `Subscription.save()` re-inherits all five on every save, so a per-subscriber
        override does not survive one — see the note in `PAYMENTS_COMPLIANCE_PLAN.md`.
        """
        plan = obj.plan
        return {
            'diet': obj.has_diet_access,
            'routines': obj.has_routine_access,
            'challenges': obj.has_challenges_access,
            'ai_advice': obj.has_ai_advice,
            'priority_support': obj.has_priority_support,
            # 0 means unlimited, as everywhere else in the quota code.
            'max_meals_per_day': plan.max_meals_per_day,
            'max_routines': plan.max_routines,
        }

    def get_renewal_due(self, obj):
        from datetime import timedelta

        from django.conf import settings
        from django.utils import timezone

        if not obj.is_active or obj.effective_end is None:
            return False
        lead = timedelta(days=int(getattr(settings, 'SUBSCRIPTION_RENEWAL_NOTICE_DAYS', 3)))
        return obj.effective_end - timezone.now() <= lead 