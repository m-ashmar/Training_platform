from rest_framework import serializers
from .models import (
    SubscriptionPlan, Subscription, Payment, 
    SubscriptionFeature, SubscriptionUsage
)
from users.serializers import CustomUserSerializer
from django.utils.translation import gettext_lazy as _
from policy.money import MoneyType
from policy.services import can_show_price


def _pricing_memo(request) -> dict:
    """What pricing a list needs to know ONCE per request, not once per row.

    The plan list converted each row on its own: one Wallet query in `currency_for`, one
    ExchangeRate query for a reader in the other currency, and one policy query for
    whether the build may see a price — three queries per plan on the Plans screen and
    on every cache miss (SIDE_BUGS S-130). The reader, their currency, the rate and the
    build are the same for every row of one response, so they are read once and kept on
    the request, which is exactly as long as they are true for.
    """
    memo = getattr(request, '_plan_pricing_memo', None)
    if memo is None:
        memo = {}
        try:
            request._plan_pricing_memo = memo
        except AttributeError:
            pass
    return memo


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
        request = self.context.get('request')
        data = self.add_price_fields(super().to_representation(instance),
                                     instance, request)
        # LAST, after any price a subclass added. It used to be the whole method, so a
        # subclass that converts the price into the reader's currency would have handed
        # the number straight back to a build that may not show one.
        if request is not None and not self._build_may_show_price(request):
            for name in self.price_fields:
                data.pop(name, None)
        return data

    @staticmethod
    def _build_may_show_price(request) -> bool:
        """`can_show_price` for this request's build, asked once per request."""
        memo = _pricing_memo(request)
        if 'can_show_price' not in memo:
            memo['can_show_price'] = can_show_price(request, MoneyType.PLATFORM_SUB)
        return memo['can_show_price']

    def add_price_fields(self, data, instance, request):
        """Hook: a subclass adds or rewrites its price fields here, not in
        `to_representation`, so the channel rule always gets the last word."""
        return data


class PlanPriceInReadersCurrency:
    """Rewrites a plan's price into the currency the reader trades in.

    ONE NUMBER, ONE CURRENCY (the owner, 2026-09-21): a person picks SYP or USD and
    every price they see is in it. The plan keeps the price of record the operator set;
    this converts it for THIS reader at the operator's live rate
    (`subscription.pricing.price_for`), and says so.

    A mixin rather than a method on one serializer because the catalogue is served by
    three classes — the detail serializer, the LIST serializer (`get_serializer_class`
    swaps it in for `action == 'list'`, which is the one the app actually reads) and the
    subscription summary. Putting the conversion on one of them is how two of the three
    kept answering dollars to a pound reader.

    A stale rate does not blank the catalogue: `price_for` falls back to the price of
    record marked `price_converted: false`, and the PURCHASE refuses with
    `exchange_rate_stale` when they tap Buy. Browsing is not selling.
    """

    #: `currency` stays on a build that may not show prices: a currency CODE is not a
    #: price, and `API_CONTRACT.md` promises it on every build. Everything that carries
    #: a number goes.
    price_fields = ('price', 'price_converted', 'exchange_rate',
                    'price_of_record', 'price_of_record_currency')

    #: Which fields on THIS serializer hold the amount and the unit.
    amount_field, currency_field = 'price', 'currency'

    def add_price_fields(self, data, instance, request):
        from .pricing import price_for

        user = getattr(request, 'user', None)
        blank = {'price_converted': False, 'exchange_rate': None,
                 'price_of_record': None, 'price_of_record_currency': None}
        if request is None or user is None or not user.is_authenticated:
            # Anonymous, or server-side code (the console, a task): nobody to price it
            # for. The columns as they stand, and the extra keys present and empty so
            # the shape never varies between callers.
            data.update(blank)
            return data

        from wallet import currency_for
        from wallet.exchange import current_rate

        # Once per request, not once per plan (S-130): the reader's currency, and — only
        # if some row is priced in the other one — the operator's rate.
        memo = _pricing_memo(request)
        if memo.get('reader') != user.pk:
            memo.pop('rate', None)
            memo['reader'], memo['currency'] = user.pk, currency_for(user)
        reader_currency = memo['currency']
        if instance.currency != reader_currency and 'rate' not in memo:
            memo['rate'] = current_rate()
        priced = (price_for(instance.price, instance.currency, reader_currency,
                            rate=memo['rate'])
                  if instance.currency != reader_currency else
                  price_for(instance.price, instance.currency, reader_currency))
        payload = priced.as_payload()
        data[self.amount_field] = payload.pop('price')
        data[self.currency_field] = payload.pop('currency')
        data.update(payload)
        return data


class SubscriptionPlanSerializer(PlanPriceInReadersCurrency, PlanPriceByChannelMixin,
                                 serializers.ModelSerializer):
    """A plan, priced in the READER'S currency and in no other.

    ONE NUMBER, ONE CURRENCY. The owner (2026-09-21): a person picks SYP or USD and
    every price they see is in it. So `price` and `currency` here are not the columns —
    they are the plan's price of record converted into the reader's own currency
    (`wallet.currency_for`, which is their wallet's) at the operator's live rate. The
    app must never convert anything itself and must never show two numbers; if it had
    both it would eventually show the wrong one.

    Fields, on top of the plan's own:

    | Field | Means |
    |---|---|
    | `price`, `currency` | what this reader would pay, in their currency |
    | `price_converted` | true when a rate was applied to get there |
    | `exchange_rate` | the rate used, as a string, or null |
    | `price_of_record`, `price_of_record_currency` | what the operator priced it at, ONLY when converted — so a receipt and a support call can say what was sold |

    With no request in the context the caller is server-side code (the operator
    console, a task) and the columns are answered as they stand, unconverted.

    A stale rate does not blank the catalogue: the reader is shown the price of record
    marked `price_converted: false`, and the PURCHASE refuses with
    `exchange_rate_stale` when they tap Buy (`subscription/views.py`). Browsing is not
    selling.

    `price` is still removed entirely for a build that may not show a price
    (`PlanPriceByChannelMixin`), and so are the four fields beside it — they are the
    same number said differently.
    """

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
        # The plan THIS payment bought, not the one the subscription is on now — a
        # plan change (B-047) must not rename every earlier row (`Payment.plan_bought`).
        return obj.plan_bought()

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

    #: What the refusal says. The view raises it before this serializer runs, with
    #: `auto_renew_read_only` as the TOP-LEVEL `code` (SIDE_BUGS S-131); `validate`
    #: below keeps the same refusal for any other caller of this serializer.
    AUTO_RENEW_READ_ONLY = _("Renewal is not automatic: nothing is charged without you. "
                             "Cancelling is on the subscription itself.")

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
                self.AUTO_RENEW_READ_ONLY, code='auto_renew_read_only')})
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

class SubscriptionPlanListSerializer(PlanPriceInReadersCurrency, PlanPriceByChannelMixin,
                                     serializers.ModelSerializer):
    """The plan LIST — the one the Plans screen actually reads.

    Same contract as `SubscriptionPlanSerializer`: `price`/`currency` are already in the
    reader's currency, with `price_converted`, `exchange_rate` and (only when converted)
    `price_of_record` and `price_of_record_currency` beside them. The app must never
    convert anything itself and must never show two numbers.
    """
    
    class Meta:
        model = SubscriptionPlan
        fields = [
            'id', 'name', 'plan_type', 'description', 'price', 'currency',
            'duration_days', 'is_featured', 'has_diet_access', 'has_routine_access',
            'has_challenges_access', 'has_ai_advice', 'has_priority_support',
            'max_meals_per_day', 'max_routines',
        ]

class UserSubscriptionSummarySerializer(PlanPriceInReadersCurrency,
                                        PlanPriceByChannelMixin,
                                        serializers.ModelSerializer):
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
    `plan_features` is what the plan includes, which is what PaySuccess lists as
    "ما فُتح لك الآن" and what the Paywall compares against.

    The renewal's price, in the READER'S currency (`PlanPriceInReadersCurrency`) —
    what they will be charged when they renew, not the operator's price of record:

    | Field | Means |
    |---|---|
    | `plan_price` | the plan's price converted into the reader's currency |
    | `plan_currency` | the READER'S currency — the unit `plan_price` is in, so the app never hard-codes "ل.س". Not necessarily the plan's |
    | `price_converted` | true when a rate was applied to get `plan_price` |
    | `exchange_rate` | the rate used, as a string, or null |
    | `price_of_record`, `price_of_record_currency` | what the operator priced the plan at, ONLY when converted; null otherwise |

    On a build that may not show prices (`can_show_prices` off: `play`, `ios`) ALL FIVE
    of `plan_price`, `price_converted`, `exchange_rate`, `price_of_record` and
    `price_of_record_currency` are ABSENT from the response — not null. A duration, a
    currency code and a feature list are not a price and are the same on every build.
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

    #: This serializer's plan price lives under a different pair of names, and the
    #: renewal it describes will be charged in the reader's currency like any other
    #: purchase — so it is shown in that currency too, rather than in the operator's.
    price_fields = ('plan_price', 'price_converted', 'exchange_rate',
                    'price_of_record', 'price_of_record_currency')
    amount_field, currency_field = 'plan_price', 'plan_currency'

    def add_price_fields(self, data, instance, request):
        return super().add_price_fields(data, instance.plan, request)

    plan_id = serializers.IntegerField(source='plan.id', read_only=True)

    class Meta:
        model = Subscription
        # `plan_id` so the app can tell WHICH catalogue plan this is without matching
        # names: a renamed plan, or two with one name in different currencies, broke its
        # "same plan -> renew, another -> refuse" decision (BACKEND_BUGS B-050).
        fields = [
            'id', 'plan_id', 'plan_name', 'plan_price', 'plan_currency',
            'plan_duration_days', 'plan_features', 'status', 'start_date',
            'end_date', 'is_active', 'days_remaining', 'auto_renew', 'source',
            'access', 'renewal_due',
        ]

    def get_plan_features(self, obj):
        """What this plan includes — PaySuccess's "ما فُتح لك الآن" and what the
        Paywall compares against.

        Read from the subscription's own flag columns, which is where every
        permission check reads them (`subscription/permissions.py`), so the screen
        says what the server will actually allow. They are copied from the plan when
        the subscription is created or moved to another plan, and an operator's
        per-subscriber override survives every other save (`Subscription.save()`,
        SIDE_BUGS S-049) — so this may differ from the plan's own flags, and when it
        does this is the answer that holds.
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