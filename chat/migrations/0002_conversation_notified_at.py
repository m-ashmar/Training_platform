"""When each side of a conversation was last pushed about it (SIDE_BUGS S-095).

Additive and reversible: two nullable columns, no default written. Existing rows get
NULL, which reads as "never pushed", so the next message in each existing thread pushes
once and the claim rule (`chat.events._claim_nudge`) takes over from there. Reversing
drops the two columns; nothing else reads them.
"""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("chat", "0001_initial"),
    ]

    operations = [
        migrations.AddField(
            model_name="conversation",
            name="client_notified_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="conversation",
            name="trainer_notified_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
