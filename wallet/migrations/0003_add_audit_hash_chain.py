# Generated migration for tamper-proof audit log hash chain

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('wallet', '0002_alter_agentprofile_daily_limit_and_more'),
    ]

    operations = [
        migrations.AddField(
            model_name='walletauditlog',
            name='prev_hash',
            field=models.CharField(
                default='0' * 64,
                help_text='SHA-256 hash of the previous audit entry',
                max_length=64,
            ),
        ),
        migrations.AddField(
            model_name='walletauditlog',
            name='entry_hash',
            field=models.CharField(
                default='',
                help_text='SHA-256 hash of this entry (prev_hash + event data)',
                max_length=64,
            ),
        ),
    ]
