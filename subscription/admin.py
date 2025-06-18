from django.contrib import admin
from django.utils.html import format_html
from django.urls import reverse
from django.utils import timezone
from .models import (
    SubscriptionPlan, Subscription, Payment, 
    SubscriptionFeature, SubscriptionUsage
)

@admin.register(SubscriptionPlan)
class SubscriptionPlanAdmin(admin.ModelAdmin):
    list_display = [
        'name', 'plan_type', 'price', 'duration_days', 
        'is_active', 'created_at'
    ]
    list_filter = ['plan_type', 'is_active', 'created_at']
    search_fields = ['name', 'description']
    readonly_fields = ['created_at', 'updated_at']
    
    fieldsets = (
        ('Basic Information', {
            'fields': ('name', 'plan_type', 'description', 'price', 'duration_days', 'is_active')
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
        queryset.update(is_active=True)
    activate_plans.short_description = "Activate selected plans"
    
    def deactivate_plans(self, request, queryset):
        queryset.update(is_active=False)
    deactivate_plans.short_description = "Deactivate selected plans"

@admin.register(Subscription)
class SubscriptionAdmin(admin.ModelAdmin):
    list_display = [
        'user', 'plan', 'status', 'is_active_display', 
        'days_remaining_display', 'start_date', 'end_date'
    ]
    list_filter = ['status', 'plan', 'auto_renew', 'created_at']
    search_fields = ['user__username', 'user__email', 'plan__name']
    readonly_fields = ['id', 'created_at', 'updated_at', 'is_active', 'is_trial', 'days_remaining', 'start_date']
    
    fieldsets = (
        ('User & Plan', {
            'fields': ('user', 'plan', 'status')
        }),
        ('Dates', {
            'fields': ('end_date', 'trial_end_date', 'start_date')
        }),
        ('Settings', {
            'fields': ('auto_renew', 'cancelled_at')
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
    
    actions = ['activate_subscriptions', 'cancel_subscriptions', 'extend_subscriptions']
    
    def is_active_display(self, obj):
        if obj.is_active:
            return format_html('<span style="color: green;">✓ Active</span>')
        return format_html('<span style="color: red;">✗ Inactive</span>')
    is_active_display.short_description = 'Active'
    
    def days_remaining_display(self, obj):
        days = obj.days_remaining
        if days > 30:
            return format_html('<span style="color: green;">{} days</span>', days)
        elif days > 7:
            return format_html('<span style="color: orange;">{} days</span>', days)
        else:
            return format_html('<span style="color: red;">{} days</span>', days)
    days_remaining_display.short_description = 'Days Remaining'
    
    def activate_subscriptions(self, request, queryset):
        queryset.update(status='active')
    activate_subscriptions.short_description = "Activate selected subscriptions"
    
    def cancel_subscriptions(self, request, queryset):
        queryset.update(status='cancelled', cancelled_at=timezone.now())
    cancel_subscriptions.short_description = "Cancel selected subscriptions"
    
    def extend_subscriptions(self, request, queryset):
        for subscription in queryset:
            subscription.end_date = subscription.end_date + timezone.timedelta(days=30)
            subscription.save()
    extend_subscriptions.short_description = "Extend subscriptions by 30 days"

@admin.register(Payment)
class PaymentAdmin(admin.ModelAdmin):
    list_display = [
        'subscription_user', 'amount', 'currency', 'status', 
        'payment_method', 'created_at'
    ]
    list_filter = ['status', 'payment_method', 'currency', 'created_at']
    search_fields = ['subscription__user__username', 'transaction_id', 'payment_intent_id']
    readonly_fields = ['id', 'created_at', 'updated_at']
    
    fieldsets = (
        ('Payment Details', {
            'fields': ('subscription', 'amount', 'currency', 'status', 'payment_method')
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
    
    def subscription_user(self, obj):
        return obj.subscription.user.username
    subscription_user.short_description = 'User'
    
    def mark_as_completed(self, request, queryset):
        queryset.update(status='completed')
    mark_as_completed.short_description = "Mark as completed"
    
    def mark_as_failed(self, request, queryset):
        queryset.update(status='failed')
    mark_as_failed.short_description = "Mark as failed"
    
    def mark_as_refunded(self, request, queryset):
        queryset.update(status='refunded')
    mark_as_refunded.short_description = "Mark as refunded"

@admin.register(SubscriptionFeature)
class SubscriptionFeatureAdmin(admin.ModelAdmin):
    list_display = ['name', 'is_active', 'created_at']
    list_filter = ['is_active', 'created_at']
    search_fields = ['name', 'description']
    readonly_fields = ['created_at']
    
    actions = ['activate_features', 'deactivate_features']
    
    def activate_features(self, request, queryset):
        queryset.update(is_active=True)
    activate_features.short_description = "Activate selected features"
    
    def deactivate_features(self, request, queryset):
        queryset.update(is_active=False)
    deactivate_features.short_description = "Deactivate selected features"

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
