# Generated manually for multi-trainer support
# This migration safely adds trainer and client functionality while preserving existing data

from django.db import migrations, models
import django.db.models.deletion
import django.utils.timezone


class Migration(migrations.Migration):

    dependencies = [
        ('users', '0007_customuser_activity_level'),
    ]

    operations = [
        # Add user_type field with default 'client' to preserve existing users
        migrations.AddField(
            model_name='customuser',
            name='user_type',
            field=models.CharField(
                choices=[('client', 'Client'), ('trainer', 'Trainer'), ('admin', 'Administrator')],
                default='client',
                help_text='Determines user permissions and access levels',
                max_length=10,
            ),
        ),
        
        # Add trainer-specific fields
        migrations.AddField(
            model_name='customuser',
            name='trainer_bio',
            field=models.TextField(blank=True, help_text="Trainer's professional bio", null=True),
        ),
        migrations.AddField(
            model_name='customuser',
            name='trainer_specializations',
            field=models.JSONField(blank=True, default=list, help_text="List of trainer specializations (e.g., ['Strength Training', 'Cardio'])"),
        ),
        migrations.AddField(
            model_name='customuser',
            name='trainer_certifications',
            field=models.JSONField(blank=True, default=list, help_text="List of trainer certifications"),
        ),
        migrations.AddField(
            model_name='customuser',
            name='trainer_experience_years',
            field=models.PositiveIntegerField(blank=True, help_text='Years of training experience', null=True),
        ),
        migrations.AddField(
            model_name='customuser',
            name='trainer_hourly_rate',
            field=models.DecimalField(blank=True, decimal_places=2, help_text='Hourly rate in local currency', max_digits=8, null=True),
        ),
        migrations.AddField(
            model_name='customuser',
            name='trainer_is_verified',
            field=models.BooleanField(default=False, help_text='Whether trainer has been verified by admin'),
        ),
        migrations.AddField(
            model_name='customuser',
            name='trainer_is_available',
            field=models.BooleanField(default=True, help_text='Whether trainer is currently accepting new clients'),
        ),
        
        # Add client-specific fields
        migrations.AddField(
            model_name='customuser',
            name='assigned_trainer',
            field=models.ForeignKey(
                blank=True,
                help_text='Trainer assigned to this client',
                limit_choices_to={'user_type': 'trainer'},
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name='clients',
                to='users.customuser'
            ),
        ),
        migrations.AddField(
            model_name='customuser',
            name='client_goals',
            field=models.JSONField(blank=True, default=list, help_text="Client's fitness goals (e.g., ['Weight Loss', 'Muscle Gain'])"),
        ),
        migrations.AddField(
            model_name='customuser',
            name='client_preferences',
            field=models.JSONField(blank=True, default=dict, help_text="Client's training preferences"),
        ),
        
        # Add system fields
        migrations.AddField(
            model_name='customuser',
            name='created_at',
            field=models.DateTimeField(auto_now_add=True, default=django.utils.timezone.now),
            preserve_default=False,
        ),
        migrations.AddField(
            model_name='customuser',
            name='updated_at',
            field=models.DateTimeField(auto_now=True),
        ),
        
        # Add database indexes for performance
        migrations.AddIndex(
            model_name='customuser',
            index=models.Index(fields=['user_type'], name='users_custom_user_t_123456_idx'),
        ),
        migrations.AddIndex(
            model_name='customuser',
            index=models.Index(fields=['assigned_trainer'], name='users_custom_assigne_123456_idx'),
        ),
        
        # Update existing superusers to be admins
        migrations.RunPython(
            # Forward migration: Set existing superusers as admins
            lambda apps, schema_editor: apps.get_model('users', 'CustomUser').objects.filter(
                is_superuser=True
            ).update(user_type='admin'),
            
            # Reverse migration: Reset user_type to client
            lambda apps, schema_editor: apps.get_model('users', 'CustomUser').objects.filter(
                user_type='admin'
            ).update(user_type='client'),
        ),
    ] 