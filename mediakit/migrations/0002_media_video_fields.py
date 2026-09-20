"""Video in a media slot: the poster, the measurements and the pipeline state.

Purely additive, and nothing is backfilled. `processing_state` defaults to `ready`
because that is TRUE of every row this migration finds: an image is finished the moment
`mediakit.services.store` returns — every rendition is written inside that call. Only a
video starts `pending`, and only rows written after this migration can be videos.
"""

import mediakit.models
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("mediakit", "0001_initial"),
    ]

    operations = [
        migrations.AddField(
            model_name="mediaasset",
            name="duration_seconds",
            field=models.FloatField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="mediaasset",
            name="hls_url",
            field=models.TextField(blank=True, default=""),
        ),
        migrations.AddField(
            model_name="mediaasset",
            name="poster",
            field=models.FileField(
                blank=True, max_length=255, upload_to=mediakit.models._upload_to
            ),
        ),
        migrations.AddField(
            model_name="mediaasset",
            name="processing_state",
            field=models.CharField(
                choices=[
                    ("pending", "Pending"),
                    ("ready", "Ready"),
                    ("failed", "Failed"),
                ],
                db_index=True,
                default="ready",
                max_length=10,
            ),
        ),
        migrations.AlterField(
            model_name="mediaasset",
            name="file_1x",
            field=models.FileField(
                blank=True, max_length=255, upload_to=mediakit.models._upload_to
            ),
        ),
        migrations.AlterField(
            model_name="mediaasset",
            name="file_2x",
            field=models.FileField(
                blank=True, max_length=255, upload_to=mediakit.models._upload_to
            ),
        ),
        migrations.AlterField(
            model_name="mediaasset",
            name="original",
            field=models.FileField(
                help_text=(
                    "The graded master, full size — or, in a video slot, the "
                    "video exactly as it was uploaded."
                ),
                max_length=255,
                upload_to=mediakit.models._upload_to,
            ),
        ),
    ]
