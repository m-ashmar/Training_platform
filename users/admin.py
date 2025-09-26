from django.contrib import admin
from django.contrib.auth.admin import UserAdmin
from django.contrib.auth.forms import UserChangeForm, UserCreationForm
from django.contrib import messages
from django.contrib.auth.hashers import make_password
from django.utils.html import format_html
from django.urls import reverse
from django.http import HttpResponseRedirect
from .models import CustomUser

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

@admin.action(description="Reset password to 'testpass123' for selected users")
def reset_password_to_default(modeladmin, request, queryset):
    default_password = "testpass123"
    count = 0
    for user in queryset:
        user.password = make_password(default_password)
        user.save()
        count += 1
    messages.success(request, f"Reset password to '{default_password}' for {count} users.")

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
        reset_password_to_default,
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
    
    def get_queryset(self, request):
        """Optimize queryset with select_related"""
        return super().get_queryset(request).select_related('assigned_trainer')
    
    def save_model(self, request, obj, form, change):
        """Handle password changes properly"""
        if change and 'password' in form.changed_data:
            # If password field was changed, hash it properly
            if obj.password and not obj.password.startswith('pbkdf2'):
                obj.password = make_password(obj.password)
        super().save_model(request, obj, form, change)
    
    def response_change(self, request, obj):
        """Custom response after user change"""
        if '_reset_password' in request.POST:
            # Handle password reset action
            default_password = "testpass123"
            obj.password = make_password(default_password)
            obj.save()
            messages.success(
                request, 
                f"Password for {obj.email} has been reset to '{default_password}'"
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
        list_display = ('user', 'token', 'created_at', 'updated_at')
        list_filter = ('created_at', 'updated_at')
        search_fields = ('user__email', 'token')
        readonly_fields = ('created_at', 'updated_at')
        
except ImportError:
    pass