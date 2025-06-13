from django.contrib import admin
from django.contrib.auth.admin import UserAdmin
from .models import CustomUser

class CustomUserAdmin(UserAdmin):
    model = CustomUser

    #78ool alt3deel
    fieldsets = (
        (None, {'fields': ('username', 'email', 'phone_number', 'password')}),
        ('Permissions', {'fields': ('is_staff', 'is_active', 'is_superuser', 'groups', 'user_permissions')}),
        ('Important Dates', {'fields': ('last_login', 'date_joined')}),
    )

    # Define fields for creating users
    add_fieldsets = (
        (None, {
            'classes': ('wide',),
            'fields': ('username', 'email', 'phone_number', 'password1', 'password2', 'is_staff', 'is_active'),
        }),
    )

    # Fields to display in the user list view
    list_display = ['username','activity_level', 'email','gender','height','weight','age', 'phone_number', 'is_staff', 'is_active']
    list_editable = ['height','activity_level','weight','gender','age']
    search_fields = ['username', 'email', 'phone_number']
    ordering = ['email']

# Register the CustomUser model and its admin class
admin.site.register(CustomUser, CustomUserAdmin)