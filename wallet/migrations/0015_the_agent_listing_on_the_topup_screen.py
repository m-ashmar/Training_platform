"""What a client needs to walk into an agent and hand over cash.

Every field defaults to empty and `is_listed` to False, so no existing agent is
published by this migration: an operator lists them, one at a time, after the agent
has agreed to it.
"""

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("wallet", "0014_transaction_money_type_known"),
    ]

    operations = [
        migrations.AddField(
            model_name="agentprofile",
            name="area",
            field=models.CharField(blank=True, default="", max_length=120),
        ),
        migrations.AddField(
            model_name="agentprofile",
            name="city",
            field=models.CharField(
                blank=True, db_index=True, default="", max_length=80
            ),
        ),
        migrations.AddField(
            model_name="agentprofile",
            name="display_name",
            field=models.CharField(blank=True, default="", max_length=120),
        ),
        migrations.AddField(
            model_name="agentprofile",
            name="is_listed",
            field=models.BooleanField(db_index=True, default=False),
        ),
        migrations.AddField(
            model_name="agentprofile",
            name="opening_hours",
            field=models.CharField(blank=True, default="", max_length=120),
        ),
        migrations.AddField(
            model_name="agentprofile",
            name="public_phone",
            field=models.CharField(blank=True, default="", max_length=32),
        ),
    ]
