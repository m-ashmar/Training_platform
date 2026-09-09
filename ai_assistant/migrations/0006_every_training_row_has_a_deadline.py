"""Give every AI training row the date it must be gone by.

`AITrainingData.retain_until` is written by `services/data_collector.py` as
`now + retention_days`, and the retention job deletes rows whose deadline has passed. Rows
written before that column existed have `retain_until = NULL`, and BOTH purge queries
filter on the column — so they were never going to be deleted by anything. Measured on the
development database: 89 rows, all NULL, all with `consented=False`, holding injury text,
weight, BMI and goals.

Backfilled from `created_at` plus the declared window, so the deadline they should always
have had is the one they get. Rows already past it go on the next purge.
"""
from datetime import timedelta

from django.db import migrations
from django.utils import timezone


#: The window declared in `training_platform/privacy/sources.py` for this source.
RETENTION_DAYS = 365


def give_every_row_a_deadline(apps, schema_editor):
    AITrainingData = apps.get_model("ai_assistant", "AITrainingData")
    now = timezone.now()
    for row in AITrainingData.objects.filter(retain_until__isnull=True).iterator():
        written = getattr(row, "created_at", None) or now
        row.retain_until = written + timedelta(days=RETENTION_DAYS)
        row.save(update_fields=["retain_until"])


def clear(apps, schema_editor):
    """No reverse worth having: putting the deadlines back to NULL restores rows that
    nothing can delete."""


class Migration(migrations.Migration):

    dependencies = [("ai_assistant", "0005_chatsession_chatsession_recent_idx")]

    operations = [migrations.RunPython(give_every_row_a_deadline, clear)]
