from django.contrib import admin
from django.utils.html import format_html
from django.utils import timezone
from .models import (
    SubscriptionPlan, Subscription, Payment, PaymentEvent,
    SubscriptionFeature, SubscriptionUsage
)
from training_platform.admin_utils import bulk_apply, log_admin_action


def _each_subscription_under_lock(request, queryset, message, change):
    """Apply `change(subscription)` to each selected row as it is UNDER the row lock.

    `change` sets what it changes and returns those field names; only they are written.
    These actions went through `bulk_apply`, which saves EVERY column of the copy the
    changelist had read — and the payment service extends cover on the same row under
    `select_for_update`. A renewal that cleared between that read and the save had its
    new `end_date` written back to the old one: the customer paid 5,000 SYP and the
    administrator's gesture was all that moved the date. The same stale full-row write
    made the customer's own cancel erase a paid renewal. Each row still gets its own
    admin log entry, which is why a bulk UPDATE on the queryset is not the answer either.
    """
    from django.db import transaction

    from .services.payment_service import lock_subscription

    count = 0
    for pk in queryset.values_list('pk', flat=True):
        with transaction.atomic():
            # The row alone (`lock_subscription`): joined to its plan, a lock that
            # waited on an upgrade lost the row when the plan switched.
            subscription = lock_subscription(pk)
            fields = change(subscription)
            subscription.save(update_fields=[*fields, 'updated_at'])
        log_admin_action(request, subscription, message)
        count += 1
    return count


def _set(subscription, **fields):
    for name, value in fields.items():
        setattr(subscription, name, value)
    return list(fields)


# Shared by both admin sites. Each goes through `save()` and writes an admin log entry per
# row: `queryset.update()` skipped both, so granting or cancelling paid access in bulk left
# no record of who did it — on the table that decides what a customer has paid for.
@admin.action(description="Activate selected subscriptions", permissions=["change"])
def activate_subscriptions(modeladmin, request, queryset):
    count = _each_subscription_under_lock(
        request, queryset, "Activated via bulk admin action",
        # Access with no payment behind it: recorded as the operator's grant it is.
        lambda s: _set(s, status='active', source='operator_grant'))
    modeladmin.message_user(request, f"Activated {count} subscriptions.")


@admin.action(description="Cancel selected subscriptions", permissions=["change"])
def cancel_subscriptions(modeladmin, request, queryset):
    now = timezone.now()
    count = _each_subscription_under_lock(
        request, queryset, "Cancelled via bulk admin action",
        lambda s: _set(s, status='cancelled', cancelled_at=now))
    modeladmin.message_user(request, f"Cancelled {count} subscriptions.")


@admin.action(description="Extend subscriptions by 30 days", permissions=["change"])
def extend_subscriptions(modeladmin, request, queryset):
    count = _each_subscription_under_lock(
        request, queryset, "Extended by 30 days via bulk admin action",
        # Keeps the source of cover somebody paid for; names itself only where there was none.
        lambda s: _set(s, end_date=s.end_date + timezone.timedelta(days=30),
                       source=s.source or 'operator_grant'))
    modeladmin.message_user(request, f"Extended {count} subscriptions.")

#: A plan's price is a number AND a unit. Both admin sites show and edit `currency` next
#: to `price`: neither did, so every plan an operator created took the field default
#: 'SYP' — an operator could not price a plan in dollars, and one typing "10" meaning
#: dollars made a 10-pound plan that every dollar reader was then shown converted from
#: ten pounds (SIDE_BUGS S-074). Shared, so the two sites cannot drift apart again.
PLAN_BASIC_FIELDS = ('name', 'plan_type', 'description', 'price', 'currency',
                     'duration_days', 'is_active', 'is_featured')


@admin.register(SubscriptionPlan)
class SubscriptionPlanAdmin(admin.ModelAdmin):
    list_display = ('name', 'plan_type', 'price', 'currency', 'duration_days', 'is_active', 'is_featured', 'has_diet_access', 'has_routine_access')
    list_filter = ('plan_type', 'currency', 'is_active', 'is_featured', 'has_diet_access', 'has_routine_access')
    search_fields = ('name',)
    readonly_fields = ['created_at', 'updated_at']
    
    fieldsets = (
        ('Basic Information', {
            # `is_featured` is the "الأكثر اختياراً" badge on the Plans screen.
            'fields': PLAN_BASIC_FIELDS
        }),
        ('Features', {
            'fields': (
                'has_diet_access', 'has_routine_access', 'has_challenges_access',
                'has_ai_advice', 'has_priority_support', 'max_meals_per_day', 'max_routines'
            )
        }),
        ('Timestamps', {
            'fields': ('created_at', 'updated_at'),
            'classes': ('collapse',)
        })
    )
    
    actions = ['activate_plans', 'deactivate_plans']
    
    def activate_plans(self, request, queryset):
        count = bulk_apply(request, queryset, "Activated via bulk admin action", is_active=True)
        self.message_user(request, f"Activated {count} plans.")
    activate_plans.short_description = "Activate selected plans"
    activate_plans.allowed_permissions = ('change',)
    
    def deactivate_plans(self, request, queryset):
        count = bulk_apply(request, queryset, "Deactivated via bulk admin action", is_active=False)
        self.message_user(request, f"Deactivated {count} plans.")
    deactivate_plans.short_description = "Deactivate selected plans"
    deactivate_plans.allowed_permissions = ('change',)

@admin.register(Subscription)
class SubscriptionAdmin(admin.ModelAdmin):
    list_display = ('user', 'plan', 'is_active', 'start_date', 'end_date')
    list_filter = ('plan',)
    search_fields = ('user__email', 'user__username')
    # Allow extending subscriptions from admin by editing end_date (keep start_date readonly)
    readonly_fields = ('id', 'start_date', 'created_at', 'updated_at')
    
    fieldsets = (
        ('User & Plan', {
            'fields': ('user', 'plan')
        }),
        ('Dates', {
            'fields': ('end_date', 'trial_end_date', 'start_date')
        }),
        ('Settings', {
            'fields': ('status', 'auto_renew', 'cancelled_at')
        }),
        ('Access Permissions', {
            'fields': (
                'has_diet_access', 'has_routine_access', 'has_challenges_access',
                'has_ai_advice', 'has_priority_support'
            )
        }),
        ('System Info', {
            'fields': ('id', 'created_at', 'updated_at'),
            'classes': ('collapse',)
        })
    )
    
    actions = [activate_subscriptions, cancel_subscriptions, extend_subscriptions]

#: Everything about a payment that `PaymentService` (or the payout receiver) owns. None of
#: it is editable in either admin site: the change form left `status`, `amount`,
#: `currency` and `subscription` open, so an operator could write 'completed' with no
#: activation and no ledger, or edit the amount of a converted payment until it no
#: longer matched its snapshot and the gateway refused it (SIDE_BUGS S-128). A state
#: change goes through the actions below, which call the service.
PAYMENT_READONLY_FIELDS = (
    'id', 'subscription', 'buyer', 'routine_product', 'amount', 'currency', 'status', 'payment_method', 'money_type',
    'price_amount', 'price_currency', 'exchange_rate', 'exchange_rate_value',
    'transaction_id', 'payment_intent_id', 'gateway_transaction_reference',
    'gateway_event_id', 'gateway_response', 'gateway_error', 'invoice_number',
    'completed_at', 'covers_from', 'covers_until', 'payout_settled_at', 'metadata',
    'created_at', 'updated_at',
)


class PaymentEventInline(admin.TabularInline):
    """Every transition of the payment, oldest first: who or what, when, from, to, and
    the evidence it rested on (the spec, section 7). Read-only in every way — the rows
    are immutable at the database (migration 0016) — and written only by
    `PaymentService`. A row marked `backfill` was written for a payment older than the
    table: the state it was in then, not a transition anyone saw."""
    model = PaymentEvent
    extra = 0
    can_delete = False
    ordering = ('created_at', 'id')
    fields = ('created_at', 'from_status', 'to_status', 'actor_role', 'actor', 'evidence',
              'backfill')
    readonly_fields = fields

    def has_add_permission(self, request, obj=None):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(Payment)
class PaymentAdmin(admin.ModelAdmin):
    list_display = [
        'subscription_user', 'amount', 'currency', 'status', 
        'payment_method', 'created_at'
    ]
    list_filter = ['status', 'payment_method', 'currency', 'created_at']
    search_fields = ['subscription__user__username', 'transaction_id', 'payment_intent_id']
    # Written only by the payment service and the payout receiver. A completed payment with
    # no `payout_settled_at` is a trainer payout still owed; `subscription.settle_owed_payouts`
    # sends it, and this is where a person sees one that keeps failing.
    readonly_fields = PAYMENT_READONLY_FIELDS
    # The payment's history, under it: every transition and what it rested on.
    inlines = [PaymentEventInline]
    
    fieldsets = (
        ('Payment Details', {
            # A plan's payment names its subscription; a routine product checkout (the
            # web hub) its buyer and product.
            'fields': ('subscription', 'buyer', 'routine_product', 'amount', 'currency',
                       'status', 'payment_method', 'money_type')
        }),
        ('Price of record', {
            # How `amount` was reached when the buyer trades in the other currency.
            'fields': ('price_amount', 'price_currency', 'exchange_rate',
                       'exchange_rate_value')
        }),
        ('What it bought', {
            'fields': ('covers_from', 'covers_until', 'payout_settled_at')
        }),
        ('External Provider', {
            'fields': ('transaction_id', 'payment_intent_id')
        }),
        ('Additional Info', {
            'fields': ('description', 'metadata')
        }),
        ('Timestamps', {
            'fields': ('created_at', 'updated_at'),
            'classes': ('collapse',)
        })
    )
    
    actions = ['mark_as_completed', 'mark_as_failed', 'mark_as_refunded']

    def has_add_permission(self, request):
        # A payment is opened by the checkout (`PaymentService.start_payment`) and by
        # nothing else. An add form would write a row in any status with no activation,
        # no price of record and no reference — the bypass S-128 closes, from the front.
        return False

    def subscription_user(self, obj):
        # The payer: a routine product checkout (the web hub) has a buyer, no subscription.
        return getattr(obj.payer, 'username', '-')
    subscription_user.short_description = 'User'
    
    # `queryset.update()` writes the column and nothing else: it skips save(), so it
    # skips the state machine that subscription/models.py declares is the only legal
    # way to move a payment, and it skips every side effect the move is supposed to
    # have. Marking a payment completed granted the customer nothing, and marking one
    # refunded took the money back while leaving the subscription active. Each of these
    # now does what the same transition does when a gateway asks for it.
    def _apply(self, request, queryset, action, verb):
        from django.contrib import messages
        from subscription.services.payment_service import (
            InvalidPaymentTransition, PaymentError,
        )

        done, refused = 0, []
        for payment in queryset:
            try:
                action(payment)
                done += 1
            except (InvalidPaymentTransition, PaymentError) as exc:
                refused.append(f"{payment.id}: {exc}")
        if done:
            self.message_user(request, f"{verb} {done} payment(s).", messages.SUCCESS)
        for line in refused:
            self.message_user(request, line, messages.ERROR)

    def mark_as_completed(self, request, queryset):
        """Only for a payment already settled out of band — it activates the subscription.

        Not for a payment held for review (B-051): money arrived for it that is not what
        it asked for, and completing it here minted the price that never arrived. It is
        decided in the transfer review queue, which records what did."""
        from django.contrib import messages
        from subscription.services.payment_service import By, PaymentService

        held = queryset.filter(status='needs_review')
        for payment in held:
            self.message_user(request, f"{payment.id}: held for review — approve or reject "
                                       f"its transfer in Transfer reviews", messages.ERROR)
        queryset = queryset.exclude(status='needs_review')
        self._apply(request, queryset, lambda p: PaymentService.complete_payment(
            p.id,
            {'amount': p.amount, 'currency': p.currency, 'status': 'completed',
             'event_id': f'admin-{p.id}'},
            by=By.OPERATOR, actor=getattr(request, "user", None),
            evidence={'action': 'mark_as_completed'},
        ), "Completed")
    mark_as_completed.short_description = "Mark as completed"
    mark_as_completed.allowed_permissions = ('change',)

    def mark_as_failed(self, request, queryset):
        from subscription.services.payment_service import By, PaymentService
        self._apply(request, queryset, lambda p: PaymentService.fail_payment(
            p.id, "Marked failed by an administrator", by=By.OPERATOR,
            actor=getattr(request, "user", None)), "Failed")
    mark_as_failed.short_description = "Mark as failed"
    mark_as_failed.allowed_permissions = ('change',)

    def mark_as_refunded(self, request, queryset):
        """Refunds the payment and withdraws the access it paid for."""
        from subscription.services.payment_service import By, PaymentService
        self._apply(request, queryset, lambda p: PaymentService.refund_payment(
            p.id, "Refunded by an administrator", by=By.OPERATOR,
            actor=getattr(request, "user", None)), "Refunded")
    mark_as_refunded.short_description = "Mark as refunded"
    mark_as_refunded.allowed_permissions = ('change',)

@admin.register(SubscriptionFeature)
class SubscriptionFeatureAdmin(admin.ModelAdmin):
    list_display = ['name', 'is_active', 'created_at']
    list_filter = ['is_active', 'created_at']
    search_fields = ['name', 'description']
    readonly_fields = ['created_at']
    
    actions = ['activate_features', 'deactivate_features']
    
    def activate_features(self, request, queryset):
        count = bulk_apply(request, queryset, "Activated via bulk admin action", is_active=True)
        self.message_user(request, f"Activated {count} features.")
    activate_features.short_description = "Activate selected features"
    activate_features.allowed_permissions = ('change',)
    
    def deactivate_features(self, request, queryset):
        count = bulk_apply(request, queryset, "Deactivated via bulk admin action", is_active=False)
        self.message_user(request, f"Deactivated {count} features.")
    deactivate_features.short_description = "Deactivate selected features"
    deactivate_features.allowed_permissions = ('change',)

@admin.register(SubscriptionUsage)
class SubscriptionUsageAdmin(admin.ModelAdmin):
    list_display = [
        'subscription_user', 'feature_name', 'usage_count', 
        'limit', 'usage_percentage_display', 'period_start'
    ]
    list_filter = ['feature', 'period_start']
    search_fields = ['subscription__user__username', 'feature__name']
    readonly_fields = ['usage_percentage']
    
    def subscription_user(self, obj):
        return obj.subscription.user.username
    subscription_user.short_description = 'User'
    
    def feature_name(self, obj):
        return obj.feature.name
    feature_name.short_description = 'Feature'
    
    def usage_percentage_display(self, obj):
        percentage = obj.usage_percentage
        if percentage > 80:
            return format_html('<span style="color: red;">{}%</span>', percentage)
        elif percentage > 60:
            return format_html('<span style="color: orange;">{}%</span>', percentage)
        else:
            return format_html('<span style="color: green;">{}%</span>', percentage)
    usage_percentage_display.short_description = 'Usage %'
