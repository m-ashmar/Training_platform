"""Two new report targets: `coach_message` (a message in the trainer-client chat,
SIDE_BUGS S-105) and `challenge` (a community challenge, S-065).

Choices only: the column, its width and every existing row are untouched — no report
changes type. Reversing narrows the choices back; a report filed against one of the two
new types would keep its value in the column and simply have no label.
"""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("moderation", "0001_initial"),
    ]

    operations = [
        migrations.AlterField(
            model_name="report",
            name="target_type",
            field=models.CharField(
                choices=[
                    ("user", "User"),
                    ("chat_message", "Chat message"),
                    ("routine", "Routine"),
                    ("diet_plan", "Diet plan"),
                    ("exercise", "Exercise"),
                    ("social_post", "Post"),
                    ("comment", "Comment"),
                    ("coach_message", "Trainer chat message"),
                    ("challenge", "Challenge"),
                ],
                max_length=16,
            ),
        ),
    ]
