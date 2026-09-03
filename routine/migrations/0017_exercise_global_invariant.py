"""Make "global exercise" mean one thing.

The column `is_global` and the predicate `created_by IS NULL` were both used as the
definition, in different places, and they disagreed: Exercise.clean() forces
is_global=False whenever there is a creator, and clean() never ran because save() does
not call full_clean(). Rows accumulated with both a creator and is_global=True, so
`can_be_accessed_by` (which reads the column) and the viewset queryset (which reads the
predicate) gave different answers about the same exercise.

Normalise the rows, then let a constraint hold the invariant.
"""
from django.db import migrations, models


def normalise(apps, schema_editor):
    Exercise = apps.get_model("routine", "Exercise")
    Exercise.objects.filter(created_by__isnull=False, is_global=True).update(is_global=False)
    Exercise.objects.filter(created_by__isnull=True, is_global=False).update(is_global=True)


def noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):
    dependencies = [("routine", "0016_remove_routine_scheduled_date")]
    operations = [
        migrations.RunPython(normalise, noop),
        migrations.AddConstraint(
            model_name="exercise",
            constraint=models.CheckConstraint(
                condition=~models.Q(is_global=True, created_by__isnull=False),
                name="exercise_global_implies_no_owner",
            ),
        ),
    ]
