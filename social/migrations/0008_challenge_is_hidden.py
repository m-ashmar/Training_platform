"""`Challenge.is_hidden`: the operator's switch for a user-written challenge (SIDE_BUGS
S-065).

Additive and reversible: one boolean column, default False, so every existing challenge
stays visible exactly as before. Reversing drops the column (and with it any hide an
operator made in between).
"""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("social", "0007_challengeparticipation_challengepa_recent_idx_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="challenge",
            name="is_hidden",
            field=models.BooleanField(default=False),
        ),
    ]
