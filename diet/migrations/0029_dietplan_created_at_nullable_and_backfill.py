from django.db import migrations, models
import datetime
from django.utils.timezone import make_aware
from datetime import timezone as dt_timezone


def backfill_created_at(apps, schema_editor):
    DietPlan = apps.get_model('diet', 'DietPlan')
    # Set any NULL created_at to 2025-10-01 00:00:00 UTC (user-requested default)
    default_dt = datetime.datetime(2025, 10, 1, 0, 0, 0, tzinfo=dt_timezone.utc)
    DietPlan.objects.filter(created_at__isnull=True).update(created_at=default_dt)


class Migration(migrations.Migration):

    dependencies = [
        ('diet', '0028_dietplan_created_at_dietplan_updated_at_and_more'),
    ]

    operations = [
        migrations.AlterField(
            model_name='dietplan',
            name='created_at',
            field=models.DateTimeField(null=True, blank=True),
        ),
        migrations.RunPython(backfill_created_at, migrations.RunPython.noop),
    ]


