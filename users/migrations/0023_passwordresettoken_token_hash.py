from django.db import migrations, models


def drop_pending_tokens(apps, schema_editor):
    """Existing rows hold RAW tokens and cannot be converted.

    A hash is one-way, so there is nothing to migrate — the raw values were the secret.
    Any pending reset is invalidated; those users request a new link, which is the
    correct outcome for tokens that were stored in the clear.
    """
    apps.get_model('users', 'PasswordResetToken').objects.all().delete()


class Migration(migrations.Migration):

    dependencies = [
        ('users', '0022_customuser_ai_training_consent'),
    ]

    operations = [
        # Clear first: the new column is unique and non-null, and old rows cannot supply it.
        migrations.RunPython(drop_pending_tokens, migrations.RunPython.noop),
        migrations.RemoveIndex(
            model_name='passwordresettoken',
            name='users_passw_token_766a81_idx',
        ),
        migrations.RemoveField(model_name='passwordresettoken', name='token'),
        migrations.AddField(
            model_name='passwordresettoken',
            name='token_hash',
            field=models.CharField(db_index=True, default='', max_length=64, unique=True),
            preserve_default=False,
        ),
        migrations.AddIndex(
            model_name='passwordresettoken',
            index=models.Index(fields=['token_hash', 'is_used'],
                               name='users_passw_token_h_idx'),
        ),
    ]
