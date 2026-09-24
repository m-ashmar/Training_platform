"""A photo, a voice note or a video on a chat message (BACKEND_BUGS B-091).

Additive and reversible. Every existing row is a text message: `kind` defaults to
`text`, the file is empty and the four new columns are NULL. `body` becomes blank-able
at the form level only (a media message's caption may be empty); the column is
unchanged. The partial index holds only form checks, so it costs nothing on text.
"""

import chat.models
import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("chat", "0002_conversation_notified_at"),
        ("routine", "0025_session_effort_rating"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name="message",
            name="attachment",
            field=models.FileField(
                blank=True, max_length=255, upload_to=chat.models._attachment_upload_to
            ),
        ),
        migrations.AddField(
            model_name="message",
            name="duration_seconds",
            field=models.FloatField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="message",
            name="exercise",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="+",
                to="routine.exercise",
            ),
        ),
        migrations.AddField(
            model_name="message",
            name="height",
            field=models.PositiveIntegerField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="message",
            name="kind",
            field=models.CharField(
                choices=[
                    ("text", "Text"),
                    ("image", "Image"),
                    ("voice", "Voice"),
                    ("video", "Video"),
                ],
                default="text",
                max_length=8,
            ),
        ),
        migrations.AddField(
            model_name="message",
            name="width",
            field=models.PositiveIntegerField(blank=True, null=True),
        ),
        migrations.AlterField(
            model_name="message",
            name="body",
            field=models.TextField(blank=True),
        ),
        migrations.AddIndex(
            model_name="message",
            index=models.Index(
                condition=models.Q(
                    ("deleted_at__isnull", True),
                    ("exercise__isnull", False),
                    ("kind", "video"),
                ),
                fields=["conversation", "id"],
                name="chat_msg_formcheck_idx",
            ),
        ),
    ]
