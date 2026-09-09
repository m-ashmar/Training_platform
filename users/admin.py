from django.contrib import admin
from django.contrib.auth.admin import UserAdmin
from django.contrib.auth.forms import UserChangeForm, UserCreationForm
from django.contrib import messages
from django.contrib.auth.hashers import make_password
from django.utils.html import format_html
from django.urls import reverse
from django.http import HttpResponseRedirect
from .models import CustomUser
import logging

logger = logging.getLogger(__name__)

class ClientInline(admin.TabularInline):
    model = CustomUser
    fk_name = 'assigned_trainer'
    fields = ('username', 'email', 'height', 'weight', 'age', 'activity_level')
    extra = 0
    verbose_name = 'Client'
    verbose_name_plural = 'Clients'
    show_change_link = True

@admin.action(description="Mark selected trainers as verified")
def make_verified(modeladmin, request, queryset):
    queryset.filter(user_type='trainer').update(trainer_is_verified=True)
    messages.success(request, f"Marked {queryset.filter(user_type='trainer').count()} trainers as verified.")

@admin.action(description="Mark selected trainers as available")
def make_available(modeladmin, request, queryset):
    queryset.filter(user_type='trainer').update(trainer_is_available=True)
    messages.success(request, f"Marked {queryset.filter(user_type='trainer').count()} trainers as available.")

# The bulk "reset to a known password" action is gone. It set every selected account
# to one shared, source-visible string, which is an account takeover of every user an
# operator can select. Password recovery goes through the OTP flow like everyone else.
@admin.action(description="Activate selected users")
def activate_users(modeladmin, request, queryset):
    queryset.update(is_active=True)
    messages.success(request, f"Activated {queryset.count()} users.")

@admin.action(description="Deactivate selected users")
def deactivate_users(modeladmin, request, queryset):
    queryset.update(is_active=False)
    messages.success(request, f"Deactivated {queryset.count()} users.")

class CustomUserChangeForm(UserChangeForm):
    """Custom form for changing user data in admin"""
    class Meta(UserChangeForm.Meta):
        model = CustomUser
        fields = '__all__'

class CustomUserCreationForm(UserCreationForm):
    """Custom form for creating users in admin"""
    class Meta(UserCreationForm.Meta):
        model = CustomUser
        fields = ('email', 'username', 'phone_number', 'user_type')

@admin.register(CustomUser)
class CustomUserAdmin(UserAdmin):
    form = CustomUserChangeForm
    add_form = CustomUserCreationForm
    
    list_display = (
        'username', 'email', 'user_type', 'is_active', 'is_staff',
        'assigned_trainer', 'trainer_is_verified', 'trainer_is_available',
        'date_joined', 'password_status'
    )
    list_editable = ('user_type', 'is_active', 'is_staff', 'trainer_is_available')
    list_filter = (
        'user_type', 'is_active', 'is_staff', 'trainer_is_verified', 
        'trainer_is_available', 'date_joined'
    )
    search_fields = ('username', 'email', 'phone_number', 'first_name', 'last_name')
    ordering = ('-date_joined',)
    inlines = [ClientInline]
    
    actions = [
        make_verified, 
        make_available, 
        activate_users,
        deactivate_users
    ]
    
    fieldsets = (
        (None, {
            'fields': ('username', 'email', 'phone_number', 'password')
        }),
        ('Personal info', {
            'fields': (
                'first_name', 'last_name', 'profile_picture', 'height', 
                'weight', 'age', 'gender', 'activity_level', 'specific_injury'
            )
        }),
        ('Trainer info', {
            'fields': (
                'trainer_bio', 'trainer_specializations', 'trainer_certifications', 
                'trainer_experience_years', 'trainer_hourly_rate', 
                'trainer_is_verified', 'trainer_is_available'
            ),
            'classes': ('collapse',)
        }),
        ('Client info', {
            'fields': ('assigned_trainer', 'client_goals', 'client_preferences'),
            'description': ('Setting a trainer here writes the pointer only. The '
                            'relationship, the client\'s consent and the escrow hold '
                            'live on TrainerClientRelation, and save() below keeps '
                            'them in step.'),
            'classes': ('collapse',)
        }),
        ('Permissions', {
            'fields': ('is_active', 'is_staff', 'is_superuser', 'groups', 'user_permissions')
        }),
        ('Important dates', {
            'fields': ('last_login', 'date_joined'),
            'classes': ('collapse',)
        }),
    )
    
    add_fieldsets = (
        (None, {
            'classes': ('wide',),
            'fields': ('username', 'email', 'phone_number', 'user_type', 'password1', 'password2'),
        }),
    )
    
    readonly_fields = ('date_joined', 'last_login', 'password_status')
    
    def password_status(self, obj):
        """Display password status in admin list"""
        if obj.password and obj.password.startswith('pbkdf2'):
            return format_html(
                '<span style="color: green;">✓ Set</span>'
            )
        else:
            return format_html(
                '<span style="color: red;">✗ Not Set</span>'
            )
    password_status.short_description = 'Password Status'
    
    def save_model(self, request, obj, form, change):
        """Hash a changed password, and keep the pointer and the relationship in step.

        There were TWO `save_model` definitions in this class body. The second replaced
        the first, so only the password half ever ran and the consent sync below was
        dead code — an admin setting `assigned_trainer` wrote the pointer with no
        relation, no consent and no charge.

        `assigned_trainer` is editable here, and every authorization check in the diet
        app requires BOTH the pointer and an approved `TrainerClientRelation`. Setting
        the pointer alone produced a client whose app showed a trainer who could not
        actually do anything, and clearing it left an approved relation behind.
        """
        from .models import TrainerClientRelation

        if change and 'password' in form.changed_data:
            if obj.password and not obj.password.startswith('pbkdf2'):
                obj.password = make_password(obj.password)
        super().save_model(request, obj, form, change)
        if 'assigned_trainer' not in getattr(form, 'changed_data', []):
            return
        if obj.assigned_trainer_id:
            TrainerClientRelation.objects.update_or_create(
                trainer_id=obj.assigned_trainer_id, client=obj,
                defaults={'status': 'approved', 'initiated_by': 'trainer'})
            TrainerClientRelation.objects.filter(client=obj, status='approved').exclude(
                trainer_id=obj.assigned_trainer_id).update(status='rejected')
        else:
            TrainerClientRelation.objects.filter(client=obj, status='approved').update(
                status='rejected')

    def get_queryset(self, request):
        """Optimize queryset with select_related"""
        return super().get_queryset(request).select_related('assigned_trainer')
    

    
    def response_change(self, request, obj):
        """Custom response after user change.

        The reset button set the account to one shared, source-visible password and
        told the operator what it was. Anyone who read this file could sign in as any
        account an operator had ever "helped". An account is recovered through the OTP
        flow, which is the only path that proves the person owns the address.
        """
        if '_reset_password' in request.POST:
            obj.set_unusable_password()
            obj.save(update_fields=['password'])
            messages.success(
                request,
                f"{obj.email} can no longer sign in with a password. "
                f"Ask them to use 'forgot password' to set a new one."
            )
            return HttpResponseRedirect('.')
        return super().response_change(request, obj)
    
    def change_view(self, request, object_id, form_url='', extra_context=None):
        """Add custom buttons to change view"""
        extra_context = extra_context or {}
        extra_context['show_reset_password'] = True
        return super().change_view(request, object_id, form_url, extra_context)

# Register other models if they exist
try:
    from .models import TrainerClientRelation, DeviceToken
    
    @admin.register(TrainerClientRelation)
    class TrainerClientRelationAdmin(admin.ModelAdmin):
        list_display = ('trainer', 'client', 'status', 'created_at')
        list_filter = ('status', 'created_at')
        search_fields = ('trainer__email', 'client__email')
        readonly_fields = ('created_at', 'updated_at')
        
    @admin.register(DeviceToken)
    class DeviceTokenAdmin(admin.ModelAdmin):
        list_display = ('user', 'token', 'platform', 'last_used_at', 'is_active')
        list_filter = ('platform', 'is_active', 'last_used_at')
        search_fields = ('user__email', 'token')
        readonly_fields = ('created_at', 'last_used_at')
        
except ImportError:
    # Optional side effect: swallowing this silently is what made the
    # surrounding failures invisible in logs. Control flow is unchanged.
    logger.debug('suppressed non-fatal error', exc_info=True)