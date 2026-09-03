"""Hold `is_global` and `created_by` together, in both directions.

0017 normalised the rows so that a global exercise has no owner and an owned one is
not global, then guarded only half of that with a constraint: it forbade
`is_global AND owner`, and said nothing about `NOT is_global AND no owner` — a row
belonging to nobody and visible to nobody, which is how an exercise disappears.

`Exercise.save()` now derives the column from ownership, so the pair cannot drift.
This makes the database say the same thing.

Also retires `target_muscle='Legs'`. The column's vocabulary is deliberately granular
(Front Quads, Hamstrings, Glutes, Calves); 'Legs' is a coarser word from somewhere
else that 30 rows picked up, and every one of them fails the field's own choices.
"""
from django.db import migrations, models


def normalise(apps, schema_editor):
    Exercise = apps.get_model("routine", "Exercise")
    Exercise.objects.filter(created_by__isnull=False, is_global=True).update(is_global=False)
    Exercise.objects.filter(created_by__isnull=True, is_global=False).update(is_global=True)
    Exercise.objects.filter(target_muscle="Legs").update(target_muscle="Other")


def noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):
    dependencies = [("routine", "0017_exercise_global_invariant")]
    operations = [
        migrations.RunPython(normalise, noop),
        migrations.RemoveConstraint(
            model_name="exercise", name="exercise_global_implies_no_owner",
        ),
        migrations.AddConstraint(
            model_name="exercise",
            constraint=models.CheckConstraint(
                condition=(
                    models.Q(is_global=True, created_by__isnull=True)
                    | models.Q(is_global=False, created_by__isnull=False)
                ),
                name="exercise_visibility_follows_ownership",
            ),
        ),
    ]
